"""Каталог: витринный вид товара, фильтры, сортировка, слоты.

Как и `calc.py`, это чистые функции над состоянием. Состояние приходит
аргументом — обычным словарём, разобранным из хранилища. Ни базы, ни
HTTP здесь нет, поэтому каждая функция проверяется сверкой с
JS-реализацией на настоящих данных магазина.

Почему слой отдельный от `calc.py`: там расчёт денег, не знающий ни о
каком каталоге, здесь — выборки и сортировки по каталогу. Смешивать
их значит потерять возможность тестировать расчёт в отрыве от данных.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Mapping

from . import calc as C

__all__ = [
    "public_product", "active_products", "list_products", "price_range",
    "find_product", "slot_key", "booking_map", "slots_view", "meat_dates_view",
    "image_keys",
]

SORT_ASC = "asc"
SORT_DESC = "desc"


def image_keys(p: Mapping[str, Any]) -> list[str]:
    """Полная галерея товара: обложка первой, за ней — дополнительные
    (ROADMAP 2.11). `image_key` — как и раньше, отдельное поле для
    старого фронтенда (инвариант 19); это — его расширение, не замена.

    `extra_images` кладёт `db/repository.py` при чтении из базы
    (группировка отдельной таблицы `product_images` по товару) — здесь
    P может не нести его вовсе (например, при прямом вызове в тестах),
    тогда лишних фото просто нет.
    """
    cover = p.get("image_key")
    extra = p.get("extra_images") or []
    return ([cover] if cover else []) + list(extra)


def public_product(p: Mapping[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Вид товара для витрины.

    Акция с истёкшим сроком наружу активной не отдаётся (ТЗ 3.2):
    поле ``sale_price`` становится пустым, хотя в базе значение
    сохраняется — администратору оно ещё понадобится.
    """
    on_sale = C.is_sale(p, now)
    return {
        "id": p.get("id"), "sku": p.get("sku"), "slug": p.get("slug"), "name": p.get("name"),
        "category_id": p.get("category_id"), "type": p.get("type"),
        "price": p.get("price"), "price_per_kg": p.get("price_per_kg"),
        "sale_price": p.get("sale_price") if on_sale else None,
        "sale_until": p.get("sale_until") or None,
        # num() всегда возвращает float, поэтому цена 99 ₽ уехала бы
        # как 99.0 — и витрина напечатала бы «99.0 ₽». Целое обязано
        # остаться целым (инвариант 19, `test_json_wire_format.py`).
        "unit_price": C.js_number(C.unit_price(p, now)),
        "base_price": C.js_number(C.base_price(p)),
        "is_sale": on_sale,
        "vat_rate": p.get("vat_rate"),
        "stock": C.js_number(p.get("stock")),
        "in_stock": C.in_stock(p),
        "min_weight": p.get("min_weight"), "weight_step": p.get("weight_step"),
        "image_key": p.get("image_key"), "emoji": p.get("emoji"),
        "image_keys": image_keys(p),
        "description": p.get("description"),
    }


def active_products(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [p for p in state.get("products", []) if p.get("is_active") is not False]


def list_products(
    state: Mapping[str, Any],
    *,
    category: str = "all",
    search: str = "",
    sort: str = "pop",
    in_stock: bool = False,
    on_sale: bool = False,
    price_min: float | None = None,
    price_max: float | None = None,
    offset: int = 0,
    limit: int = 60,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Каталог с фильтрами (ТЗ 3.1, 3.2).

    Порядок сортировок важен и повторяет JS-версию: сначала по цене,
    если её попросили, затем товары без остатка опускаются в конец.
    Вторая сортировка обязана быть устойчивой, иначе она перемешает
    результат первой. В Python ``list.sort`` устойчив, в JS начиная с
    ES2019 — тоже; на этом совпадении всё и держится.
    """
    items = active_products(state)

    if category == "sale":
        items = [p for p in items if C.is_sale(p, now)]
    elif category and category != "all":
        items = [p for p in items if p.get("category_id") == category]

    if in_stock:
        items = [p for p in items if C.in_stock(p)]
    if on_sale:
        items = [p for p in items if C.is_sale(p, now)]
    if search:
        items = [p for p in items if C.matches_query(p, search)]

    # ТЗ 2.1.3 — фильтр по той цене, которую покупатель видит на витрине:
    # для акционного товара это акционная, а не базовая.
    if price_min is not None and math.isfinite(price_min):
        items = [p for p in items if C.unit_price(p, now) >= price_min]
    if price_max is not None and math.isfinite(price_max):
        items = [p for p in items if C.unit_price(p, now) <= price_max]

    if sort == SORT_ASC:
        items.sort(key=lambda p: C.unit_price(p, now))
    elif sort == SORT_DESC:
        items.sort(key=lambda p: -C.unit_price(p, now))

    # ТЗ 3.2 — товары без остатка не прячем, но опускаем в конец
    items.sort(key=lambda p: 0 if C.in_stock(p) else 1)

    total = len(items)
    off = max(0, _int32(offset))
    lim = min(200, max(1, _int32(limit) or 60))
    return {"total": total, "items": [public_product(p, now) for p in items[off:off + lim]]}


def price_range(state: Mapping[str, Any], now: datetime | None = None) -> dict[str, int]:
    """Границы цен активного каталога — витрина подставляет их в подсказки."""
    prices = [C.unit_price(p, now) for p in active_products(state)]
    prices = [n for n in prices if n > 0]
    if not prices:
        return {"min": 0, "max": 0}
    return {"min": math.floor(min(prices)), "max": math.ceil(max(prices))}


def find_product(state: Mapping[str, Any], key: str) -> dict[str, Any] | None:
    """Карточка по slug (ТЗ 15.3 — ЧПУ), по id или по артикулу."""
    key = str(key)
    for p in state.get("products", []):
        if p.get("is_active") is False:
            continue
        if p.get("slug") == key or str(p.get("id")) == key or p.get("sku") == key:
            return p
    return None


# ---------------------------------------------------------------------------
# Слоты доставки
# ---------------------------------------------------------------------------
def slot_key(method: str, ymd: str, from_h: Any) -> str:
    """Доставка и самовывоз считаются раздельно, отсюда метод в ключе."""
    return f"{method}|{ymd}|{from_h}"


def booking_map(state: Mapping[str, Any], method: str) -> dict[str, float]:
    """Брони нужного метода, приведённые к ключу «дата|час»."""
    out: dict[str, float] = {}
    for k, v in (state.get("slot_bookings") or {}).items():
        parts = str(k).split("|")
        if len(parts) == 3 and parts[0] == method:
            out[f"{parts[1]}|{parts[2]}"] = C.num(v)
    return out


def slots_view(state: Mapping[str, Any], method: str, now: datetime | None = None) -> dict[str, Any]:
    s = state.get("settings", {})
    capacity = s.get("slot_capacity_pickup") if method == "pickup" else s.get("slot_capacity_delivery")
    now_p = C.msk_parts(now)
    booked = booking_map(state, method)

    days = []
    for d in range(int(C.num(s.get("horizon_d")))):
        ymd = C.add_days_ymd(now_p.ymd, d)
        days.append({
            "ymd": ymd,
            "slots": C.slots_for_date(
                ymd=ymd, now=now,
                work_from=int(C.num(s.get("work_from"))),
                work_to=int(C.num(s.get("work_to"))),
                cutoff_h=s.get("cutoff_h"), capacity=capacity, booked=booked,
                holidays=s.get("holidays") or [],
            ),
        })
    first_free = next((d["ymd"] for d in days if any(x["ok"] for x in d["slots"])), None)
    return {"days": days, "first_available": first_free, "capacity": capacity}


def meat_dates_view(state: Mapping[str, Any], now: datetime | None = None) -> dict[str, Any]:
    s = state.get("settings", {})
    return {
        "dates": C.meat_dates(
            days=s.get("meat_days") or [],
            limit_kg=s.get("meat_limit_kg"),
            cutoff_days=s.get("meat_cutoff_days"),
            booked_kg=state.get("meat_bookings") or {},
            horizon_d=21,
            now=now,
        ),
        "limit_kg": s.get("meat_limit_kg"),
        "days": s.get("meat_days"),
    }


def _int32(v: Any) -> int:
    """Аналог ``v | 0`` из JS: приведение к 32-битному целому.

    В JS этим отсекается дробная часть и мусор вроде ``"abc"``. Без
    такого же поведения ``offset=1.9`` дал бы в Python иную страницу.
    """
    n = C.num(v)
    if not math.isfinite(n):
        return 0
    n = int(n)                      # усечение к нулю, как в JS
    n &= 0xFFFFFFFF
    return n - 0x100000000 if n >= 0x80000000 else n
