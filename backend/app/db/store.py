"""Чтение `data/store.json` — запасной путь, когда база недоступна.

Боевые данные лежат в PostgreSQL. Файл
остался снимком на момент переключения и **только читается**: писать в
него некому и незачем, а сам он нужен для двух вещей — чтобы витрина
не легла, если база вдруг не отвечает, и как путь отката.

Записи здесь нет и не будет. Две программы, пишущие в один файл, рано
или поздно затрут работу друг друга, и произойдёт это на заказе
клиента. Единственный писатель — приложение, и пишет оно в базу через
`db/uow.py` (инвариант 17).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from ..config import get_settings

# Коллекции, которые обязаны быть списками, и те, что словарями.
# Проверяем тип, а не наличие: `null` вместо списка проходит проверку
# «ключ есть» и падает на первом же запросе — так уже случалось.
LIST_KEYS = (
    "users", "sessions", "otp", "categories", "products", "carts", "orders",
    "order_items", "order_status_history", "preorders", "promocodes",
    "promocode_usages", "delivery_zones", "tg_links", "consents", "audit",
)
DICT_KEYS = ("meta", "seq", "slot_bookings", "meat_bookings", "settings", "home_config")


class StoreUnavailable(RuntimeError):
    """Хранилище не прочитать. Наверх уходит как 503, а не как 500."""


def reconcile(raw: Any) -> dict[str, Any]:
    """Приводит прочитанное к ожидаемой форме, ничего не выдумывая."""
    if not isinstance(raw, dict):
        raise StoreUnavailable("хранилище не является объектом")
    state: dict[str, Any] = dict(raw)
    for k in LIST_KEYS:
        if not isinstance(state.get(k), list):
            state[k] = []
    for k in DICT_KEYS:
        if not isinstance(state.get(k), dict):
            state[k] = {}
    return state


class JsonStore:
    """Читает файл и кэширует разобранное состояние до изменения файла.

    «Файл изменился» определяется по паре (размер, время изменения):
    перечитывать его на каждый запрос не нужно, а отдать устаревшее
    содержимое невозможно. Пригождается это при восстановлении из
    резервной копии — файл подменяют снаружи, и приложение обязано
    заметить подмену само, без перезапуска.
    """

    def __init__(self, path: Path | None = None):
        self._path = path
        self._lock = threading.Lock()
        self._stamp: tuple[int, int] | None = None
        self._state: dict[str, Any] | None = None

    @property
    def path(self) -> Path:
        return self._path or get_settings().store_path

    def read(self) -> dict[str, Any]:
        path = self.path
        try:
            st = path.stat()
        except OSError as e:
            # Файл подменяют через временный и переименование (так его
            # восстанавливают из копии). В момент подмены по прежнему
            # имени файла может не быть доли секунды — и если состояние
            # уже прочитано, отдать его честнее, чем ответить ошибкой.
            with self._lock:
                if self._state is not None:
                    return self._state
            raise StoreUnavailable(f"нет файла хранилища: {path}") from e

        stamp = (st.st_size, st.st_mtime_ns)
        with self._lock:
            if self._state is not None and self._stamp == stamp:
                return self._state
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                # Могли прочитать файл в момент подмены — тогда есть
                # предыдущее состояние, и отдать его честнее, чем
                # уронить запрос.
                if self._state is not None:
                    return self._state
                raise StoreUnavailable(f"хранилище не читается: {e}") from e
            self._state = reconcile(raw)
            self._stamp = stamp
            return self._state

    def invalidate(self) -> None:
        with self._lock:
            self._state = None
            self._stamp = None


store = JsonStore()
