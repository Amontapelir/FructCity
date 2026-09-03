"""Откуда брать состояние магазина: из базы или из JSON.

Одно место принятия решения на всё приложение. Разбросать этот выбор
по роутерам значило бы получить приложение, часть которого читает
базу, а часть — файл, и расхождение между ними заметил бы покупатель,
а не тест.

Правило простое: **база, если она настроена и в ней есть данные.**
Пустая база — это ещё не переехавший магазин, а не магазин без
товаров; отдавать на витрину пустой каталог в такой момент нельзя.

Сверка снимка (`_snapshot`) осталась от времени, когда в `store.json`
писала Node-версия: тогда отставшая от файла база считалась непригодной
и уступала место JSON. Сейчас пишет само приложение, и при включённом
`FC_WRITE_ENABLED` сверка не выполняется вовсе — база и есть источник
истины, сравнивать её с файлом, который она заменила, незачем.

Механизм не удалён намеренно: он же обслуживает обратный случай — базу,
которая настроена, но пуста или недоступна. Тогда витрина отдаётся из
файла, а не падает.
"""

from __future__ import annotations

import threading
from typing import Any

from ..config import get_settings
from .store import StoreUnavailable, store

__all__ = ["read_state", "current_source", "snapshot_status", "reset",
           "invalidate", "engine"]

_lock = threading.Lock()
_db_state: Any = None          # DbState, создаётся лениво
_db_checked = False
_db_usable = False


def _db() -> Any:
    """Снимок из базы. None, если базой пользоваться нельзя.

    Проверка делается один раз за процесс: если база не настроена,
    незачем ходить к ней на каждом запросе. Сбросить решение можно
    через ``reset()`` — это нужно тестам.
    """
    global _db_state, _db_checked, _db_usable

    with _lock:
        if _db_checked:
            return _db_state if _db_usable else None
        _db_checked = True

        settings = get_settings()
        if not settings.db_configured:
            return None
        try:
            from .models import get_engine
            from .repository import DbState

            engine = get_engine()
            state = DbState(engine)
            # Пустая база — признак незавершённого переезда, а не
            # пустого магазина. Пока товаров нет, читаем JSON.
            if not state.read().get("products"):
                return None
            _db_state = state
            _db_usable = True
            return _db_state
        except Exception as e:  # noqa: BLE001 — причин много, поведение одно
            print(f"[FructCity] база недоступна, читаю JSON: {e}", flush=True)
            return None


def snapshot_status() -> dict[str, Any]:
    """Не отстала ли база от файла, из которого сделана.

    Смысл проверки — время, когда в `store.json` писала другая
    программа: база оставалась со старым каталогом и остатками, но
    выглядела рабочей, и покупатель купил бы то, чего уже нет.

    Сейчас файл не обновляется, и при `FC_WRITE_ENABLED` эта сверка не
    вызывается. Осталась она для случая, когда базу подняли заново или
    ещё не наполнили: отметка о снимке кладётся переносом, и её
    отсутствие честно означает «неизвестно, свежая ли база».
    """
    settings = get_settings()
    if not settings.db_configured:
        return {"known": False, "stale": False, "reason": "база не настроена"}
    try:
        from .migrate_json import read_snapshot, snapshot_marker
        from .models import get_engine

        marker = read_snapshot(get_engine())
        if not marker:
            return {"known": False, "stale": True,
                    "reason": "в базе нет отметки о переносе"}

        path = settings.store_path
        if not path.exists():
            # Файла нет — сверять не с чем, база и есть единственный
            # источник. Обычная ситуация на новом сервере.
            return {"known": True, "stale": False, "reason": "файла хранилища нет"}

        now = snapshot_marker(path)
        stale = (now["size"], now["mtime_ns"]) != (marker.get("size"), marker.get("mtime_ns"))
        return {
            "known": True,
            "stale": stale,
            "reason": "store.json изменён после переноса" if stale else "совпадает",
        }
    except Exception as e:  # noqa: BLE001
        return {"known": False, "stale": True, "reason": f"проверить не удалось: {e}"}


def read_state() -> dict[str, Any]:
    source = _db()
    if source is not None and (get_settings().write_enabled or not snapshot_status()["stale"]):
        return source.read()
    return store.read()


def current_source() -> str:
    """«postgres» или «json» — для /healthz и для тестов.

    Отставшая база — это «json»: если база не отражает актуальные
    данные, читать надо из файла, иначе витрина показывала бы остатки,
    которых давно нет.

    Когда включён `FC_WRITE_ENABLED`, сверка снимка отключается
    сознательно: в этом режиме база сама — источник истины, а не копия
    файла, и сравнивать её с тем, что она заменила, незачем. Если база
    при этом недоступна или пуста, `_db()` вернёт `None`, и источником
    всё равно останется «json» — так безопаснее, чем отдавать пустой
    каталог.
    """
    if _db() is None:
        return "json"
    if get_settings().write_enabled:
        return "postgres"
    return "json" if snapshot_status()["stale"] else "postgres"


def invalidate() -> None:
    """Сбросить кэш снимка, не пересматривая выбор источника.

    Вызывается после записи. Полный `reset()` здесь не подходит: он
    заново опрашивает базу и пересоздаёт подключение на каждую покупку,
    хотя менялись только данные.
    """
    with _lock:
        state = _db_state
    if state is not None:
        state.invalidate()
    store.invalidate()


def engine() -> Any:
    """Подключение к базе — то же самое, что использует чтение.

    Создавать своё на каждый запрос нельзя: у каждого движка свой пул,
    и число соединений растёт вместе с числом запросов, пока Postgres
    не начнёт отказывать.
    """
    from .models import get_engine

    state = _db()
    if state is not None:
        return state._engine        # noqa: SLF001 — тот же объект, а не копия
    return get_engine()


def reset() -> None:
    """Забыть выбранный источник. Нужно тестам и после переноса данных."""
    global _db_state, _db_checked, _db_usable
    with _lock:
        _db_state = None
        _db_checked = False
        _db_usable = False
    store.invalidate()
