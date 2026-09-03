"""Перенос магазина из data/store.json в базу.

Из корня проекта:

    python -m backend.app.db.migrate_json --verify   # сверить, ничего не записывая
    python -m backend.app.db.migrate_json            # перенести
    python -m backend.app.db.migrate_json --force    # очистить базу и перенести заново

Файл только читается — испортить его перенос не может.

Три вещи, из-за которых такой перенос обычно портит данные.

**Последовательности.** Идентификаторы переносятся как есть, из JSON.
PostgreSQL при этом не двигает счётчик: он не знает, что мы вставили
строку с id=36. Первый же новый товар получит id=1 и упрётся в
занятый ключ. Поэтому после переноса счётчики выставляются вручную —
см. ``_fix_sequences``.

**Вложенные структуры.** Корзина лежала списком внутри сессии, брони
слотов и мяса — словарями с составным ключом, настройки и конструктор
главной — двумя объектами. В базе у всего этого свои таблицы.

**Молчаливая потеря.** Поэтому перенос заканчивается сверкой: число
строк в каждой таблице сравнивается с числом записей в JSON, и
расхождение — ошибка, а не строчка в журнале.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from sqlalchemy import func, insert, select, text
from sqlalchemy.engine import Engine

from ..config import get_settings
from . import models as M


def load_store(path: Path | None = None) -> dict[str, Any]:
    """Читает store.json и приводит к ожидаемой форме.

    `reconcile` берётся из слоя чтения, а не пишется здесь заново:
    правило «пропущенная коллекция — это пустой список» должно быть
    одним на весь проект, иначе перенос и приложение однажды разойдутся
    в том, что считать пустой базой.
    """
    from .store import reconcile

    path = path or get_settings().store_path
    if not path.exists():
        raise FileNotFoundError(f"нет файла хранилища: {path}")
    return reconcile(json.loads(path.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# Отбор полей
# ---------------------------------------------------------------------------
def _pick(row: dict, table) -> dict:
    """Оставляет только те поля, которые есть в таблице.

    Список полей берётся из самой схемы, а не переписывается рядом:
    иначе новая колонка появлялась бы в модели и молча не переносилась.
    """
    return {c.name: row.get(c.name) for c in table.columns if c.name in row}


def _column_default(column) -> Any:
    """Значение по умолчанию из описания колонки."""
    default = column.default
    if default is None:
        return None
    arg = getattr(default, "arg", None)
    if callable(arg):
        return arg(None)
    return arg


def _align(rows: list[dict], table) -> list[dict]:
    """Приводит все словари пачки к одному набору ключей.

    В JSON поля со значением по умолчанию просто отсутствуют: у
    категории «Акции» есть `is_system`, у остальных тринадцати его нет.
    Для человека это одно и то же, для многострочной вставки — нет:
    SQLAlchemy собирает один запрос на всю пачку и требует, чтобы
    набор ключей у всех строк совпадал. Иначе — отказ на второй строке
    с сообщением про недостающий параметр.

    Недостающее заполняем значением по умолчанию из самой схемы, а не
    придумываем: `is_system` без значения — это False, ровно как
    считает JS-версия.
    """
    if not rows:
        return rows
    keys: set[str] = set()
    for row in rows:
        keys |= row.keys()
    defaults = {k: _column_default(table.columns[k]) for k in keys if k in table.columns}
    return [{k: row.get(k, defaults.get(k)) for k in keys} for row in rows]


def _rows_products(st: dict) -> list[dict]:
    return [_pick(p, M.Product.__table__) for p in st["products"]]


def _rows_sessions(st: dict) -> list[dict]:
    return [_pick(s, M.Session.__table__) for s in st["sessions"]]


def _rows_cart_items(st: dict) -> list[dict]:
    """Корзина: список внутри сессии → строки таблицы.

    Позиции на несуществующие товары отбрасываем: внешний ключ такую
    строку всё равно не пропустит, а падать посреди переноса из-за
    товара, удалённого месяц назад, незачем.
    """
    known = {p["id"] for p in st["products"] if p.get("id") is not None}
    out = []
    for s in st["sessions"]:
        for item in s.get("cart") or []:
            pid = item.get("product_id")
            if pid not in known:
                continue
            out.append({"session_id": s["id"], "product_id": pid,
                        "qty": item.get("qty"), "weight": item.get("weight")})
    return out


def _rows_session_recent(st: dict) -> list[dict]:
    """Списки «недавних» из сессии → строки со ссылками."""
    orders = {o["id"] for o in st["orders"] if o.get("id") is not None}
    preorders = {p["id"] for p in st["preorders"] if p.get("id") is not None}
    out = []
    for s in st["sessions"]:
        for kind, field, known in (("order", "recent_orders", orders),
                                   ("preorder", "recent_preorders", preorders)):
            seen = set()
            for ref in s.get(field) or []:
                if ref in known and ref not in seen:
                    seen.add(ref)
                    out.append({"session_id": s["id"], "kind": kind, "ref_id": ref})
    return out


def _rows_slot_bookings(st: dict) -> list[dict]:
    """Ключ «метод|дата|час» → три колонки."""
    out = []
    for key, value in (st.get("slot_bookings") or {}).items():
        parts = str(key).split("|")
        if len(parts) != 3:
            continue
        method, ymd, from_h = parts
        try:
            out.append({"method": method, "ymd": ymd,
                        "slot_from": int(from_h), "booked": int(value)})
        except (TypeError, ValueError):
            continue
    return out


def _rows_meat_bookings(st: dict) -> list[dict]:
    return [{"ymd": ymd, "booked_kg": float(kg)}
            for ymd, kg in (st.get("meat_bookings") or {}).items()]


# Ключ, под которым база помнит, из какого снимка файла она сделана.
# Начинается с подчёркивания, чтобы не спутать с настройкой магазина.
SNAPSHOT_KEY = "_snapshot"


def snapshot_marker(path: Path) -> dict[str, Any]:
    """Отпечаток файла хранилища: размер и время изменения.

    По нему видно, что файл переписали уже после переноса и данные в
    базе устарели. Содержимое не хешируем: файл читается
    целиком при каждой проверке, а размер со временем меняются при
    любой записи — этого достаточно, чтобы заметить расхождение.
    """
    st = path.stat()
    return {"path": str(path), "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _rows_settings(st: dict) -> list[dict]:
    """Настройки и конструктор главной — в таблицу «ключ-значение».

    Значение хранится текстом JSON: список дней поставки мяса или
    блоки главной страницы иначе пришлось бы раскладывать на свои
    таблицы, а меняются они чаще всего остального.
    """
    out = [{"key": k, "value": json.dumps(v, ensure_ascii=False)}
           for k, v in (st.get("settings") or {}).items()]
    out.append({"key": "home_config",
                "value": json.dumps(st.get("home_config") or {}, ensure_ascii=False)})
    return out


# Порядок переноса — от родителей к детям. Иначе внешние ключи не дадут
# вставить строку, которая ссылается на ещё не перенесённую.
PLAN: Sequence[tuple[str, Any, Callable[[dict], list[dict]]]] = (
    ("categories", M.Category.__table__, lambda st: [_pick(c, M.Category.__table__) for c in st["categories"]]),
    ("delivery_zones", M.DeliveryZone.__table__, lambda st: [_pick(z, M.DeliveryZone.__table__) for z in st["delivery_zones"]]),
    ("promocodes", M.Promocode.__table__, lambda st: [_pick(p, M.Promocode.__table__) for p in st["promocodes"]]),
    ("users", M.User.__table__, lambda st: [_pick(u, M.User.__table__) for u in st["users"]]),
    ("products", M.Product.__table__, _rows_products),
    ("sessions", M.Session.__table__, _rows_sessions),
    ("cart_items", M.CartItem.__table__, _rows_cart_items),
    ("otp", M.Otp.__table__, lambda st: [_pick(o, M.Otp.__table__) for o in st["otp"]]),
    ("tg_links", M.TelegramLink.__table__, lambda st: [_pick(t, M.TelegramLink.__table__) for t in st["tg_links"]]),
    ("orders", M.Order.__table__, lambda st: [_pick(o, M.Order.__table__) for o in st["orders"]]),
    ("order_items", M.OrderItem.__table__, lambda st: [_pick(i, M.OrderItem.__table__) for i in st["order_items"]]),
    ("order_status_history", M.OrderStatusHistory.__table__, lambda st: [_pick(h, M.OrderStatusHistory.__table__) for h in st["order_status_history"]]),
    ("preorders", M.Preorder.__table__, lambda st: [_pick(p, M.Preorder.__table__) for p in st["preorders"]]),
    ("session_recent", M.SessionRecent.__table__, _rows_session_recent),
    ("promocode_usages", M.PromocodeUsage.__table__, lambda st: [_pick(u, M.PromocodeUsage.__table__) for u in st["promocode_usages"]]),
    ("consents", M.Consent.__table__, lambda st: [_pick(c, M.Consent.__table__) for c in st["consents"]]),
    ("slot_bookings", M.SlotBooking.__table__, _rows_slot_bookings),
    ("meat_bookings", M.MeatBooking.__table__, _rows_meat_bookings),
    ("settings", M.Setting.__table__, _rows_settings),
    ("audit", M.AuditRecord.__table__, lambda st: [_pick(a, M.AuditRecord.__table__) for a in st["audit"]]),
)


# ---------------------------------------------------------------------------
# Перенос
# ---------------------------------------------------------------------------
def migrate(engine: Engine | None = None, store: dict | None = None,
            force: bool = False, snapshot: Path | None = None) -> dict[str, int]:
    """Переносит данные. Возвращает, сколько строк легло в каждую таблицу."""
    engine = engine or M.get_engine()
    st = store if store is not None else load_store()
    snapshot_path = snapshot or get_settings().store_path

    missing = M.missing_tables(engine)
    if missing:
        raise RuntimeError(
            "схема неполная, не хватает таблиц: " + ", ".join(missing) +
            "\nСоздайте их: python -m backend.app.db.models --create")

    occupied = {name: n for name, n in _counts(engine).items() if n}
    if occupied and not force:
        raise RuntimeError(
            "в базе уже есть данные: " +
            ", ".join(f"{k} — {v}" for k, v in sorted(occupied.items())) +
            "\nПовторный перенос удвоил бы их. Запустите с --force, чтобы "
            "очистить базу и перенести заново.")
    if occupied and force:
        M.clear_all(engine)

    written: dict[str, int] = {}
    # Один транзакционный блок на весь перенос, включая правку счётчиков.
    # Раньше счётчики правились отдельно, уже после фиксации данных, и
    # отказ на этом шаге оставлял базу с данными, но с неверными
    # счётчиками — а повторный перенос отклонялся как «данные уже есть».
    with engine.begin() as conn:
        for name, table, build in PLAN:
            rows = _align([r for r in build(st) if r], table)
            if rows:
                conn.execute(insert(table), rows)
            written[name] = len(rows)

        # Отпечаток файла, из которого сделан перенос. Без него нельзя
        # отличить свежую базу от отставшей, а отставшая отдаёт старые
        # остатки — и покупатель покупает то, чего уже нет.
        if snapshot_path.exists():
            conn.execute(insert(M.Setting.__table__), [{
                "key": SNAPSHOT_KEY,
                "value": json.dumps(snapshot_marker(snapshot_path), ensure_ascii=False),
            }])
            written["settings"] += 1

        _fix_sequences(conn)

    return written


def sequence_tables() -> list[str]:
    """Таблицы, у которых идентификатор выдаёт счётчик базы.

    Только целочисленный первичный ключ с именем ``id``. У категорий
    ключ текстовый («fruit», «meat»), у броней мяса — дата: счётчика за
    ними нет, и попытка его подвинуть кончается отказом о несовместимых
    типах. Отдельной функцией — чтобы этот отбор можно было проверить
    тестом, не поднимая PostgreSQL.
    """
    out = []
    for _, table, _ in PLAN:
        col = table.columns.get("id")
        if col is None or not col.primary_key:
            continue
        try:
            if col.type.python_type is not int:
                continue
        except NotImplementedError:      # тип без питоновского соответствия
            continue
        out.append(table.name)
    return out


def _fix_sequences(conn) -> None:
    """Двигает счётчики идентификаторов за перенесённые значения.

    Без этого первый же созданный заказ получил бы id=1 и упёрся в
    занятый ключ: PostgreSQL не следит за тем, что мы вставили строки
    с готовыми идентификаторами. У SQLite такой проблемы нет.
    """
    if conn.dialect.name != "postgresql":
        return
    for name in sequence_tables():
        # Имя последовательности спрашиваем у самой базы: у таблицы её
        # может не быть вовсе, и тогда пропускаем, а не падаем.
        seq = conn.execute(
            text("SELECT pg_get_serial_sequence(:t, 'id')"), {"t": name}).scalar()
        if not seq:
            continue
        conn.execute(
            text(f"SELECT setval(:s, GREATEST(COALESCE((SELECT MAX(id) FROM {name}), 0), 1))"),
            {"s": seq})


def _snapshot_rows(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(
            select(func.count()).select_from(M.Setting.__table__)
            .where(M.Setting.__table__.c.key == SNAPSHOT_KEY)).scalar() or 0)


def read_snapshot(engine: Engine) -> dict[str, Any] | None:
    """Из какого снимка файла сделана база. None — отметки нет."""
    with engine.connect() as conn:
        row = conn.execute(
            select(M.Setting.__table__.c.value)
            .where(M.Setting.__table__.c.key == SNAPSHOT_KEY)).scalar()
    if not row:
        return None
    try:
        return json.loads(row)
    except (TypeError, ValueError):
        return None


def _counts(engine: Engine) -> dict[str, int]:
    out: dict[str, int] = {}
    with engine.connect() as conn:
        for name, table, _ in PLAN:
            out[name] = int(conn.execute(select(func.count()).select_from(table)).scalar() or 0)
    return out


# ---------------------------------------------------------------------------
# Сверка
# ---------------------------------------------------------------------------
def verify(engine: Engine | None = None, store: dict | None = None) -> list[str]:
    """Сравнивает базу с JSON. Возвращает список расхождений.

    Проверяем не только количество строк: совпадение чисел ничего не
    доказывает, если поля переехали не туда. Поэтому для товаров и
    заказов сверяются ещё и значения — те, что про деньги и остатки.
    """
    engine = engine or M.get_engine()
    st = store if store is not None else load_store()
    problems: list[str] = []

    counts = _counts(engine)
    for name, _table, build in PLAN:
        expected = len([r for r in build(st) if r])
        got = counts.get(name, 0)
        if name == "settings":
            # Служебная отметка о снимке в JSON не хранится и
            # расхождением не является.
            got -= _snapshot_rows(engine)
        if expected != got:
            problems.append(f"{name}: в JSON {expected}, в базе {got}")

    with engine.connect() as conn:
        problems += _compare_products(conn, st)
        problems += _compare_orders(conn, st)
    return problems


def _compare_products(conn, st: dict) -> list[str]:
    out = []
    rows = conn.execute(select(
        M.Product.__table__.c.id, M.Product.__table__.c.sku,
        M.Product.__table__.c.price, M.Product.__table__.c.price_per_kg,
        M.Product.__table__.c.stock, M.Product.__table__.c.sale_price,
    )).all()
    in_db = {r.id: r for r in rows}
    for p in st["products"]:
        row = in_db.get(p.get("id"))
        if row is None:
            out.append(f"товара {p.get('sku')} нет в базе")
            continue
        if row.sku != p.get("sku"):
            out.append(f"товар {p.get('id')}: артикул было {p.get('sku')}, стало {row.sku}")
        for field in ("price", "price_per_kg", "stock", "sale_price"):
            was, now = p.get(field), getattr(row, field)
            if was is None and now is None:
                continue
            # Сравниваем числами: в JSON целое, в базе может быть
            # дробное того же значения — расхождением это не является.
            if float(was or 0) != float(now or 0):
                out.append(f"товар {p.get('sku')}: {field} было {was}, стало {now}")
    return out


def _compare_orders(conn, st: dict) -> list[str]:
    out = []
    rows = conn.execute(select(
        M.Order.__table__.c.id, M.Order.__table__.c.number,
        M.Order.__table__.c.total, M.Order.__table__.c.items_total,
        M.Order.__table__.c.status, M.Order.__table__.c.payment_status,
    )).all()
    in_db = {r.id: r for r in rows}
    for o in st["orders"]:
        row = in_db.get(o.get("id"))
        if row is None:
            out.append(f"заказа №{o.get('number')} нет в базе")
            continue
        for field in ("number", "total", "items_total", "status", "payment_status"):
            if getattr(row, field) != o.get(field):
                out.append(f"заказ №{o.get('number')}: {field} было "
                           f"{o.get(field)}, стало {getattr(row, field)}")
    return out


# ---------------------------------------------------------------------------
# Командная строка
# ---------------------------------------------------------------------------
def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.app.db.migrate_json",
        description="Перенос магазина из data/store.json в базу.")
    parser.add_argument("--verify", action="store_true",
                        help="только сверить базу с JSON, ничего не записывая")
    parser.add_argument("--force", action="store_true",
                        help="очистить базу перед переносом")
    parser.add_argument("--store", default=None, help="путь к store.json")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        engine = M.get_engine()
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 2

    try:
        st = load_store(Path(args.store) if args.store else None)
    except (OSError, ValueError) as e:
        print(f"Не удалось прочитать хранилище: {e}", file=sys.stderr)
        return 2

    if args.verify:
        problems = verify(engine, st)
        if problems:
            print(f"Расхождений: {len(problems)}", file=sys.stderr)
            for p in problems[:40]:
                print(f"  • {p}", file=sys.stderr)
            return 1
        print("База и store.json совпадают.")
        return 0

    try:
        written = migrate(engine, st, force=args.force)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 2

    total = sum(written.values())
    print(f"Перенесено строк: {total}")
    width = max(len(k) for k in written)
    for name, n in written.items():
        if n:
            print(f"  {name.ljust(width)}  {n}")

    problems = verify(engine, st)
    if problems:
        print(f"\nСверка после переноса нашла расхождения ({len(problems)}):", file=sys.stderr)
        for p in problems[:40]:
            print(f"  • {p}", file=sys.stderr)
        return 1
    print("\nСверка после переноса: расхождений нет.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
