"""Чтение состояния магазина из базы.

Возвращает **тот же словарь**, каким состояние лежало в JSON: те же
имена коллекций, полей и вложенных структур. Благодаря этому доменный
слой не знает, откуда пришли данные, и остаётся набором чистых функций,
которые проверяются без базы и без HTTP.

Выглядит как шаг назад («зачем собирать словарь, если есть ORM?») — и
это честный компромисс, а не задумка: каждый запрос поднимает всё
состояние целиком. Форма осталась от переезда, когда домен обязан был
работать одинаково поверх файла и поверх базы. Переход на запросы по
одной сущности и удаление этого модуля — отдельная задача.

Читаем всё разом и кладём в кэш: каталог магазина — это сорок товаров
и полтора десятка категорий, отдельные запросы на каждый чих здесь
дороже, чем один снимок.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

from ..domain.calc import js_number
from . import models as M

__all__ = ["load_state", "DbState", "js_number"]

# Коллекции, которые читаются построчно без всякой обработки.
PLAIN = (
    ("categories", M.Category),
    ("products", M.Product),
    ("delivery_zones", M.DeliveryZone),
    ("promocodes", M.Promocode),
    ("users", M.User),
    ("orders", M.Order),
    ("order_items", M.OrderItem),
    ("order_status_history", M.OrderStatusHistory),
    ("preorders", M.Preorder),
    ("promocode_usages", M.PromocodeUsage),
    ("consents", M.Consent),
    ("otp", M.Otp),
    ("tg_links", M.TelegramLink),
    ("audit", M.AuditRecord),
)


def _row(row) -> dict[str, Any]:
    return {k: js_number(v) for k, v in row._mapping.items()}


def load_state(engine: Engine) -> dict[str, Any]:
    """Снимок магазина в форме, привычной доменному слою."""
    state: dict[str, Any] = {}
    with engine.connect() as conn:
        for name, model in PLAIN:
            table = model.__table__
            state[name] = [_row(row) for row in conn.execute(select(table))]

        state["sessions"] = _sessions(conn)
        state["carts"] = []
        state["slot_bookings"] = _slot_bookings(conn)
        state["meat_bookings"] = _meat_bookings(conn)
        settings, home = _settings(conn)
        state["settings"] = settings
        state["home_config"] = home

    # Поля, которых в базе нет, но которые ждёт доменный слой.
    state.setdefault("meta", {})
    state["seq"] = _seq(state)
    return state


# Коллекции с целочисленным `id`: для них восстанавливается счётчик
# `seq[имя]` от максимального значения. У категорий ключ строковый
# («fruit», «meat») — счётчика у них нет и не нужно.
_SEQ_COLLECTIONS = (
    "products", "delivery_zones", "promocodes", "users", "sessions",
    "otp", "tg_links", "orders", "order_items", "order_status_history",
    "preorders", "promocode_usages", "consents", "audit",
)


def _seq(state: dict[str, Any]) -> dict[str, int]:
    """Восстанавливает счётчики id из уже загруженных данных.

    В JSON `state.seq[table]` — тот же самый счётчик, что выдаёт id: он
    живёт в одном файле и переживает между запросами естественным
    образом. В базе id выдаёт `next_id_factory` (счётчик или
    `MAX(id)`), а сам объект `seq` нигде не хранится — таблицы для
    него нет. Не восстановить его здесь значит вернуть с каждым
    чтением состояния пустой `seq`, и тогда номер заказа
    (`1000 + seq.orders + 1`) каждый раз считался бы от нуля: второй
    заказ подряд получил бы тот же номер, что первый, и запись упала бы
    на уникальности. Правильное значение — максимум среди уже
    выданных id, что и означает «сколько записей когда-либо создано».
    """
    out: dict[str, int] = {}
    for name in _SEQ_COLLECTIONS:
        ids = [r.get("id") for r in (state.get(name) or [])
               if isinstance(r.get("id"), int)]
        if ids:
            out[name] = max(ids)
    return out


def _sessions(conn) -> list[dict[str, Any]]:
    """Сессии вместе с корзиной и списками недавних заказов.

    В JSON это были вложенные списки, в базе — отдельные таблицы.
    Собираем обратно, потому что так их читает доменный слой.
    Три запроса вместо запроса на каждую сессию: сессий бывает много,
    и обход по одной превратился бы в сотни обращений.
    """
    rows = [_row(r) for r in conn.execute(select(M.Session.__table__))]
    by_id = {r["id"]: r for r in rows}
    for r in rows:
        r["cart"] = []
        r["recent_orders"] = []
        r["recent_preorders"] = []

    for item in conn.execute(select(M.CartItem.__table__)):
        session = by_id.get(item.session_id)
        if session is not None:
            session["cart"].append({"product_id": item.product_id,
                                    "qty": js_number(item.qty),
                                    "weight": js_number(item.weight)})

    for ref in conn.execute(select(M.SessionRecent.__table__)):
        session = by_id.get(ref.session_id)
        if session is None:
            continue
        field = "recent_orders" if ref.kind == "order" else "recent_preorders"
        session[field].append(ref.ref_id)

    return rows


def _slot_bookings(conn) -> dict[str, int]:
    """Обратно в ключ «метод|дата|час» — так его ищет расчёт слотов."""
    return {
        f"{r.method}|{r.ymd}|{r.slot_from}": js_number(r.booked)
        for r in conn.execute(select(M.SlotBooking.__table__))
    }


def _meat_bookings(conn) -> dict[str, float]:
    return {r.ymd: js_number(r.booked_kg)
            for r in conn.execute(select(M.MeatBooking.__table__))}


def _settings(conn) -> tuple[dict[str, Any], dict[str, Any]]:
    """Настройки и конструктор главной из таблицы «ключ-значение».

    Значение хранится текстом JSON. Неразбираемое не роняет магазин:
    отдаём как строку и работаем дальше — витрина без одной настройки
    полезнее, чем витрина, которая не открылась.
    """
    settings: dict[str, Any] = {}
    home: dict[str, Any] = {}
    for row in conn.execute(select(M.Setting.__table__)):
        # Служебные отметки (ключ с подчёркивания) — не настройки
        # магазина, а внутренняя кухня переноса. Наружу уходить не
        # должны: админка показывает настройки списком, и `_snapshot`
        # оказался бы в нём наравне с телефоном магазина.
        if str(row.key).startswith("_"):
            continue
        try:
            value = json.loads(row.value)
        except (TypeError, ValueError):
            value = row.value
        if row.key == "home_config":
            home = value if isinstance(value, dict) else {}
        else:
            settings[row.key] = value
    return settings, home


class DbState:
    """Снимок состояния с коротким кэшем.

    Кэш короткий и по времени, а не бессрочный: молча отдавать
    вчерашний каталог хуже, чем лишний раз сходить в базу. Записи он не
    касается — `uow.py` читает состояние сам, внутри транзакции, иначе
    остаток проверялся бы по устаревшему снимку.
    """

    def __init__(self, engine: Engine, ttl_seconds: float = 2.0):
        self._engine = engine
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._state: dict[str, Any] | None = None
        self._at = 0.0

    def read(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if self._state is not None and (now - self._at) < self._ttl:
                return self._state
        state = load_state(self._engine)
        with self._lock:
            self._state = state
            self._at = time.monotonic()
        return state

    def invalidate(self) -> None:
        with self._lock:
            self._state = None
            self._at = 0.0
