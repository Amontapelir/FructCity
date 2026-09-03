"""Перенос данных из `store.json` в базу с проверками.

**Выполнен 2 сентября 2026.** Модуль оставлен рабочим: тем же путём
поднимается магазин на новом сервере из имеющегося файла, и им же
восстанавливаются данные из резервной копии.

Порядок шагов продиктован риском: после включения `FC_WRITE_ENABLED`
файл перестаёт обновляться, и вернуться к нему можно только
восстановлением из копии. Поэтому:

1. убедиться, что в файл никто не пишет (иначе перенесём снимок,
   который устареет через секунду — заказ, оформленный между шагами,
   потеряется молча);
2. сделать резервную копию `store.json` рядом, с отметкой времени;
3. перенести данные в базу;
4. сверить базу с файлом (`verify`) — не «перенеслось без исключения», а
   совпало по числу строк, деньгам и остаткам.

Чего этот модуль НЕ делает намеренно: не включает `FC_WRITE_ENABLED` сам.
Правка боевой конфигурации — решение человека, а не побочный эффект
скрипта; к тому же флаг читается при старте приложения, и переписать
файл окружения из работающего процесса значило бы сделать вид, что
переключение уже произошло.

    python -m backend.app.db.cutover              # проверить и перенести
    python -m backend.app.db.cutover --dry-run    # только проверки
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

__all__ = ["backup_name", "store_is_quiet", "backup_store", "cutover"]

# `migrate_json`, `models` и `config` подтягиваются внутри `cutover()`, а
# не здесь. Причина не в скорости запуска: без этого модуль нельзя было
# бы импортировать без SQLAlchemy, и проверки «в файл ещё пишут» и
# «резервная копия сделана» — та самая логика, ошибка в которой стоит
# потерянных заказов, — гонялись бы только там, где поднят весь стек.

# Сколько ждать, проверяя, что файл больше не меняется. Прежний сервер
# писал `store.json` на КАЖДЫЙ запрос без cookie (заводил гостя),
# поэтому даже фоновый обход поисковика был виден как правка. Двух
# секунд хватает, чтобы отличить остановленный сервер от работающего
# вхолостую.
QUIET_SECONDS = 2.0


def backup_name(store: Path, now: datetime | None = None) -> Path:
    """Имя резервной копии: рядом с оригиналом, с отметкой времени UTC.

    Рядом, а не во временном каталоге: копия нужна тому, кто будет
    откатывать переключение в три часа ночи, и искать её он будет там
    же, где лежат данные.
    """
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    return store.with_name(f"{store.name}.before-cutover-{stamp}")


def store_is_quiet(store: Path, *, seconds: float = QUIET_SECONDS,
                   sleep: Callable[[float], None] = time.sleep) -> bool:
    """`True`, если файл не менялся в течение паузы.

    Проверка «в файл никто не пишет», сделанная единственным доступным
    отсюда способом: спросить чужой процесс мы не можем, а следы его
    работы в файле видны. Ложное «тихо» возможно (сервер поднят, но к
    нему никто не обращается) — поэтому это не единственная защита, а
    первая: вторая — резервная копия, третья — сверка после переноса.
    """
    if not store.exists():
        return True
    before = _fingerprint(store)
    sleep(seconds)
    return _fingerprint(store) == before


def _fingerprint(store: Path) -> tuple[int, int, str]:
    """Размер, время правки и хеш содержимого.

    Одного времени правки мало, и это не теоретическая придирка: на
    файловой системе с грубым разрешением времени (а таких хватает)
    перезапись за ту же миллисекунду не сдвигает `mtime` вовсе — и
    проверка бодро отвечала бы «тихо» ровно тогда, когда файл пишут.
    Поймано собственным тестом `test_file_written_during_the_pause_is_
    not_quiet`, который до этого падал. Хеш отвечает на настоящий
    вопрос: изменились ли данные, — а перезапись тем же содержимым
    ничего не теряет и расхождением не считается.
    """
    stat = store.stat()
    digest = hashlib.sha256(store.read_bytes()).hexdigest()
    return stat.st_size, stat.st_mtime_ns, digest


def backup_store(store: Path, now: datetime | None = None) -> Path:
    target = backup_name(store, now)
    shutil.copy2(store, target)          # copy2 сохраняет время правки
    return target


def cutover(*, store: Path | None = None, dry_run: bool = False,
            force_quiet: bool = False, out: Callable[[str], None] = print) -> int:
    """Возвращает код возврата процесса: 0 — успех, иначе отказ.

    `store` берётся из настроек, если не передан явно. Явный параметр —
    ради теста: подменять свойство настроек на лету значило бы проверять
    подмену, а не поведение.
    """
    if store is None:
        from ..config import get_settings

        store = get_settings().store_path

    if not store.exists():
        out(f"Не найден {store} — переносить нечего.")
        return 2

    out(f"Хранилище: {store}")
    if store_is_quiet(store):
        out(f"Файл не менялся {QUIET_SECONDS:g} с — в него никто не пишет.")
    elif force_quiet:
        out("ВНИМАНИЕ: файл всё ещё меняется, но запуск продолжен по --force-quiet.")
    else:
        out("ОТКАЗ: store.json меняется прямо сейчас — в него кто-то пишет.")
        out("Остановите пишущий процесс и повторите; иначе заказ, оформленный")
        out("между переносом и переключением флага, потеряется без следа.")
        return 1

    from . import migrate_json as MJ
    from . import models as M

    try:
        engine = M.get_engine()
    except RuntimeError as e:
        out(f"ОТКАЗ: база не настроена: {e}")
        return 2

    state = MJ.load_store(store)

    if dry_run:
        problems = MJ.verify(engine, state)
        out(f"Проверка без записи: расхождений {len(problems)}.")
        for p in problems[:40]:
            out(f"  • {p}")
        return 1 if problems else 0

    copy = backup_store(store)
    out(f"Резервная копия: {copy}")

    written = MJ.migrate(engine, state, force=True, snapshot=store)
    out(f"Перенесено строк: {sum(written.values())}")

    problems = MJ.verify(engine, state)
    if problems:
        out(f"ОТКАЗ: сверка нашла расхождения ({len(problems)}):")
        for p in problems[:40]:
            out(f"  • {p}")
        out(f"Данные в файле не тронуты, копия — {copy}. Флаг включать НЕЛЬЗЯ.")
        return 1

    out("Сверка после переноса: расхождений нет.")
    out("")
    out("Осталось вручную (намеренно не делается скриптом):")
    out("  1) FC_WRITE_ENABLED=1 в .env;")
    out("  2) перезапустить FastAPI;")
    out("  3) убедиться, что /healthz отдаёт write_enabled: true и source: postgres;")
    out("  4) оформить один тестовый заказ и увидеть его в базе.")
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.app.db.cutover",
        description="Перенос данных из store.json в базу с проверками.")
    parser.add_argument("--dry-run", action="store_true",
                        help="только проверки и сверка, без записи")
    parser.add_argument("--force-quiet", action="store_true",
                        help="не отказываться, если store.json ещё меняется")
    args = parser.parse_args(list(argv) if argv is not None else None)
    return cutover(dry_run=args.dry_run, force_quiet=args.force_quiet)


if __name__ == "__main__":
    sys.exit(main())
