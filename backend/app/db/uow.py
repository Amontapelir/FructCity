"""Запись в базу: транзакция, счётчики, применение изменений.

Доменные операции меняют состояние-словарь на месте. Этот слой берёт
такой словарь до и после операции, находит разницу и переносит её в
базу — одной транзакцией.

**Почему одна транзакция на весь запрос.** Между чтением остатка и его
списанием может вклиниться другой заказ, и последняя банка продастся
дважды. Закрывают это транзакция и рекомендательная блокировка
PostgreSQL (`pg_advisory_xact_lock`). Блокировка одна на весь
магазин: это грубо, но заведомо верно, а нагрузка у магазина такая,
что тонкая блокировка по строкам не окупится. Когда окупится, менять
надо будет только эту функцию.

**Почему сравнение снимков, а не запись «как пойдёт».** Доменный слой
ничего не знает о базе — и не должен: только благодаря этому он
сверяется с JS-версией как чистые функции. Значит кто-то обязан
превратить его правки в SQL. Сравнение до/после делает это одинаково
для всех операций, и новая операция не требует своего кода записи.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.engine import Engine

from . import models as M
from .repository import load_state

__all__ = ["transaction", "Unit", "write_changes", "next_id_factory", "LOCK_KEY"]

# Произвольное, но постоянное число: рекомендательные блокировки
# PostgreSQL различаются только им. Менять нельзя — иначе два процесса
# с разными версиями кода перестанут видеть блокировки друг друга.
LOCK_KEY = 0x46435459        # «FCTY»

# Коллекция состояния → таблица и её ключ.
COLLECTIONS: tuple[tuple[str, Any, tuple[str, ...]], ...] = (
    ("categories", M.Category.__table__, ("id",)),
    ("delivery_zones", M.DeliveryZone.__table__, ("id",)),
    ("promocodes", M.Promocode.__table__, ("id",)),
    ("users", M.User.__table__, ("id",)),
    ("products", M.Product.__table__, ("id",)),
    ("sessions", M.Session.__table__, ("id",)),
    ("otp", M.Otp.__table__, ("id",)),
    ("tg_links", M.TelegramLink.__table__, ("id",)),
    ("orders", M.Order.__table__, ("id",)),
    ("order_items", M.OrderItem.__table__, ("id",)),
    ("order_status_history", M.OrderStatusHistory.__table__, ("id",)),
    ("preorders", M.Preorder.__table__, ("id",)),
    ("promocode_usages", M.PromocodeUsage.__table__, ("id",)),
    ("consents", M.Consent.__table__, ("id",)),
    ("audit", M.AuditRecord.__table__, ("id",)),
)

# Поля сессии, которые в базе живут отдельными таблицами.
SESSION_NESTED = ("cart", "recent_orders", "recent_preorders")


def _key(row: Mapping[str, Any], fields: tuple[str, ...]) -> tuple:
    return tuple(row.get(f) for f in fields)


def _columns(table) -> set[str]:
    return {c.name for c in table.columns}


def _payload(row: Mapping[str, Any], table) -> dict[str, Any]:
    """Только те поля, которые есть в таблице."""
    allowed = _columns(table)
    return {k: v for k, v in row.items() if k in allowed}


def _changed(before: Mapping[str, Any], after: Mapping[str, Any],
             table) -> dict[str, Any]:
    """Поля, которые изменились. Пустой словарь — писать нечего."""
    allowed = _columns(table)
    out = {}
    for name in allowed:
        if name not in after:
            continue
        if before.get(name) != after.get(name):
            out[name] = after[name]
    return out


# ---------------------------------------------------------------------------
# Счётчики идентификаторов
# ---------------------------------------------------------------------------
def next_id_factory(conn) -> Callable[[str], int]:
    """Выдаёт следующий идентификатор для коллекции.

    На PostgreSQL берём его у последовательности таблицы: только так
    два одновременных заказа не получат один номер. На SQLite (тесты)
    последовательностей нет, поэтому считаем максимум плюс один — это
    безопасно, потому что вся операция идёт под блокировкой.

    Значение сразу учитывается в `state["seq"]`: номер заказа
    строится от него, и без обновления два заказа подряд получили бы
    один и тот же номер для покупателя.
    """
    tables = {name: table for name, table, _ in COLLECTIONS}
    is_postgres = conn.dialect.name == "postgresql"
    # Запасной путь (SQLite) не видит собственных ещё не вставленных строк:
    # `write_changes` пишет всё разом в конце транзакции. Без счётчика в
    # памяти два вызова `next_id` подряд для одной коллекции читали бы один
    # и тот же MAX и выдали бы один и тот же номер — например, оба
    # предзаказа. На PostgreSQL этой проблемы нет: `nextval` сам себя
    # не повторяет независимо от момента вставки.
    allocated: dict[str, int] = {}

    def next_id(collection: str) -> int:
        table = tables.get(collection)
        if table is None:
            raise KeyError(f"неизвестная коллекция: {collection}")
        if is_postgres:
            seq = conn.execute(
                text("SELECT pg_get_serial_sequence(:t, 'id')"),
                {"t": table.name}).scalar()
            if seq:
                return int(conn.execute(text("SELECT nextval(:s)"), {"s": seq}).scalar())
        if collection not in allocated:
            current = conn.execute(select(func.max(table.c.id))).scalar()
            allocated[collection] = int(current or 0)
        allocated[collection] += 1
        return allocated[collection]

    return next_id


# ---------------------------------------------------------------------------
# Применение изменений
# ---------------------------------------------------------------------------
def write_changes(conn, before: Mapping[str, Any],
                  after: Mapping[str, Any]) -> dict[str, int]:
    """Переносит разницу двух снимков в базу. Возвращает счётчик правок."""
    stats = {"insert": 0, "update": 0, "delete": 0}

    for name, table, key_fields in COLLECTIONS:
        was = {_key(r, key_fields): r for r in (before.get(name) or [])}
        now = {_key(r, key_fields): r for r in (after.get(name) or [])}

        for key, row in now.items():
            old = was.get(key)
            if old is None:
                conn.execute(insert(table), _payload(row, table))
                stats["insert"] += 1
                continue
            diff = _changed(old, row, table)
            # Ключ в набор для UPDATE не попадает: менять его нельзя,
            # а попытка это сделать означала бы ошибку выше по стеку.
            diff.pop("id", None)
            if diff:
                conn.execute(update(table)
                             .where(*[table.c[f] == key[i]
                                      for i, f in enumerate(key_fields)])
                             .values(**diff))
                stats["update"] += 1

        for key in was.keys() - now.keys():
            conn.execute(delete(table).where(
                *[table.c[f] == key[i] for i, f in enumerate(key_fields)]))
            stats["delete"] += 1

    _write_session_nested(conn, before, after, stats)
    _write_bookings(conn, before, after, stats)
    _write_settings(conn, before, after, stats)
    return stats


def _write_session_nested(conn, before: Mapping[str, Any],
                          after: Mapping[str, Any], stats: dict) -> None:
    """Корзина и списки недавних заказов — из сессии в свои таблицы."""
    was = {s.get("id"): s for s in (before.get("sessions") or [])}
    for session in after.get("sessions") or []:
        sid = session.get("id")
        old = was.get(sid, {})
        if all(session.get(f) == old.get(f) for f in SESSION_NESTED):
            continue

        # Позиции корзины переписываем целиком: их единицы, а следить за
        # добавлением, изменением и удалением по отдельности здесь дороже
        # ошибки, которую это сэкономит.
        conn.execute(delete(M.CartItem.__table__)
                     .where(M.CartItem.__table__.c.session_id == sid))
        rows = [{"session_id": sid, "product_id": it.get("product_id"),
                 "qty": it.get("qty"), "weight": it.get("weight")}
                for it in (session.get("cart") or [])
                if it.get("product_id") is not None]
        if rows:
            conn.execute(insert(M.CartItem.__table__), rows)

        conn.execute(delete(M.SessionRecent.__table__)
                     .where(M.SessionRecent.__table__.c.session_id == sid))
        recent = []
        for kind, field in (("order", "recent_orders"), ("preorder", "recent_preorders")):
            seen = set()
            for ref in session.get(field) or []:
                if ref in seen:
                    continue
                seen.add(ref)
                recent.append({"session_id": sid, "kind": kind, "ref_id": ref})
        if recent:
            conn.execute(insert(M.SessionRecent.__table__), recent)
        stats["update"] += 1


def _write_bookings(conn, before: Mapping[str, Any],
                    after: Mapping[str, Any], stats: dict) -> None:
    """Брони слотов и мяса: словари с составными ключами."""
    slots_before = before.get("slot_bookings") or {}
    slots_after = after.get("slot_bookings") or {}
    table = M.SlotBooking.__table__

    for key, value in slots_after.items():
        if slots_before.get(key) == value:
            continue
        parts = str(key).split("|")
        if len(parts) != 3:
            continue
        method, ymd, from_h = parts
        where = [table.c.method == method, table.c.ymd == ymd,
                 table.c.slot_from == int(from_h)]
        exists = conn.execute(select(table.c.id).where(*where)).scalar()
        if exists is None:
            conn.execute(insert(table), {"method": method, "ymd": ymd,
                                         "slot_from": int(from_h),
                                         "booked": int(value)})
            stats["insert"] += 1
        else:
            conn.execute(update(table).where(*where).values(booked=int(value)))
            stats["update"] += 1

    for key in slots_before.keys() - slots_after.keys():
        parts = str(key).split("|")
        if len(parts) == 3:
            conn.execute(delete(table).where(
                table.c.method == parts[0], table.c.ymd == parts[1],
                table.c.slot_from == int(parts[2])))
            stats["delete"] += 1

    meat_before = before.get("meat_bookings") or {}
    meat_after = after.get("meat_bookings") or {}
    meat = M.MeatBooking.__table__
    for ymd, kg in meat_after.items():
        if meat_before.get(ymd) == kg:
            continue
        exists = conn.execute(select(meat.c.ymd).where(meat.c.ymd == ymd)).scalar()
        if exists is None:
            conn.execute(insert(meat), {"ymd": ymd, "booked_kg": float(kg)})
            stats["insert"] += 1
        else:
            conn.execute(update(meat).where(meat.c.ymd == ymd)
                         .values(booked_kg=float(kg)))
            stats["update"] += 1
    for ymd in meat_before.keys() - meat_after.keys():
        conn.execute(delete(meat).where(meat.c.ymd == ymd))
        stats["delete"] += 1


def _write_settings(conn, before: Mapping[str, Any],
                    after: Mapping[str, Any], stats: dict) -> None:
    """Настройки и конструктор главной — таблица «ключ-значение»."""
    import json

    table = M.Setting.__table__
    pairs_before = dict(before.get("settings") or {})
    pairs_after = dict(after.get("settings") or {})
    if (before.get("home_config") or {}) != (after.get("home_config") or {}):
        pairs_before["home_config"] = before.get("home_config")
        pairs_after["home_config"] = after.get("home_config")

    for key, value in pairs_after.items():
        if key in pairs_before and pairs_before[key] == value:
            continue
        payload = json.dumps(value, ensure_ascii=False)
        exists = conn.execute(select(table.c.key).where(table.c.key == key)).scalar()
        if exists is None:
            conn.execute(insert(table), {"key": key, "value": payload})
            stats["insert"] += 1
        else:
            conn.execute(update(table).where(table.c.key == key).values(value=payload))
            stats["update"] += 1

    for key in pairs_before.keys() - pairs_after.keys():
        conn.execute(delete(table).where(table.c.key == key))
        stats["delete"] += 1


# ---------------------------------------------------------------------------
# Транзакция
# ---------------------------------------------------------------------------
class Unit:
    """Единица работы: состояние, счётчик идентификаторов, связь с базой."""

    def __init__(self, state: dict[str, Any], next_id: Callable[[str], int], conn):
        self.state = state
        self._next_id = next_id
        self.conn = conn
        self.stats: dict[str, int] = {}

    def next_id(self, collection: str) -> int:
        """Новый идентификатор — и сразу же учтённый в счётчиках состояния.

        Номер заказа для покупателя строится от `seq.orders`, поэтому
        не обновить его здесь значит выдать двум заказам подряд один и
        тот же номер.
        """
        value = self._next_id(collection)
        seq = self.state.setdefault("seq", {})
        seq[collection] = max(int(C_num(seq.get(collection))), value)
        return value


def C_num(v: Any) -> float:
    from ..domain.calc import num

    return num(v)


@contextmanager
def transaction(engine: Engine, lock: bool = True) -> Iterator[Unit]:
    """Читает состояние, отдаёт его на изменение, записывает разницу.

    Всё внутри одной транзакции и под блокировкой: проверка остатка и
    его списание не могут быть разорваны другим запросом.

    Исключение внутри блока откатывает транзакцию целиком — в базе не
    останется наполовину оформленного заказа.
    """
    with engine.begin() as conn:
        if lock and conn.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_KEY})

        state = load_state_from(conn)
        before = copy.deepcopy(state)
        unit = Unit(state, next_id_factory(conn), conn)
        yield unit
        unit.stats = write_changes(conn, before, unit.state)


def load_state_from(conn) -> dict[str, Any]:
    """Снимок состояния внутри уже открытой транзакции.

    Отдельная функция, потому что `load_state` из репозитория берёт
    своё соединение — а нам нужно читать тем же, которым потом пишем.
    Иначе чтение шло бы вне транзакции и не видело бы блокировки.
    """
    from . import repository as R

    class _Bound:
        """Обёртка, выдающая уже открытое соединение."""

        def connect(self):
            from contextlib import nullcontext

            return nullcontext(conn)

    return R.load_state(_Bound())
