"""Бизнес-операции магазина: корзина, заказ, отмена, предзаказ, пересчёт.

Корзина, оформление заказа, отмена, предзаказ мяса, пересчёт после
взвешивания. Как и остальной домен — функции над состоянием-словарём,
без базы и HTTP.

Состояние операции **меняют на месте**: добавляют строки в списки,
списывают остаток, двигают счётчики броней. Транзакцию вокруг этого
держит вызывающий слой — и это не деталь, а главное правило проекта:
проверка остатка и его списание обязаны быть в одной критической
секции, иначе два покупателя купят последнюю банку.

Отказы возвращаются значением, а не исключением: `{"error": код,
"status": код HTTP}`. Так вызывающий не может забыть их обработать —
результат надо посмотреть, чтобы понять, что произошло.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Mapping

from . import calc as C
from .catalog import (active_products, booking_map, public_product, slot_key,
                      slots_view)

__all__ = [
    "cart_view", "add_to_cart", "update_cart", "apply_promo",
    "place_order", "cancel_order", "quote_delivery_cost", "create_preorder",
    "order_view", "recalc_order", "line_total_of", "billable_weight",
    "count_promo_usage",
]

MAX_CART_ITEMS = 100
RECENT_ORDERS_KEPT = 20      # столько заказов гостя помнит сессия
ORDER_NUMBER_BASE = 1000
PREORDER_NUMBER_BASE = 5000


def _round2(v: float) -> float:
    """Округление до сотых, как в JS: иначе 0.1 + 0.2 копит хвост."""
    return C.js_number(C._js_round(v * 100) / 100)


# ---------------------------------------------------------------------------
# Корзина (ТЗ 4.1)
# ---------------------------------------------------------------------------
def cart_view(state: Mapping[str, Any], session: Mapping[str, Any],
              now: datetime | None = None) -> dict[str, Any]:
    """Корзина всегда пересчитывается сервером.

    Клиент не может прислать «свою» цену: он присылает только товар и
    количество, всё остальное считается здесь.
    """
    products = active_products(state)
    by_id = {p.get("id"): p for p in products}

    # Позиции удалённых или отключённых товаров выпадают сами
    items = [it for it in (session.get("cart") or []) if it.get("product_id") in by_id]

    promo = None
    if session.get("promo_code"):
        promo = next((p for p in state.get("promocodes", [])
                      if p.get("code") == session["promo_code"]), None)

    calc = C.calc_order(items=items, products=products, promo=promo,
                        method="pickup", now=now)
    by_product = {line["product_id"]: line for line in calc.lines}

    lines = []
    for it in items:
        p = by_id[it["product_id"]]
        line = by_product.get(p.get("id"), {"total": 0})
        is_pre = p.get("type") == "preorder"
        want = it.get("qty") if p.get("type") == "unit" else it.get("weight")
        available = p.get("stock")
        lines.append({
            "product": public_product(p, now),
            "qty": it.get("qty"), "weight": it.get("weight"),
            "total": line.get("total", 0),
            # ТЗ 4.1 — нехватку помечаем явно. Предзаказное мясо остаток
            # не расходует, поэтому недоступным по складу не бывает.
            "unavailable": not is_pre and not C.in_stock(p),
            "insufficient": (not is_pre and C.in_stock(p)
                             and C.num(want) > C.num(available)),
            "available": available,
        })

    return {
        "items": lines,
        "count": len(lines),
        "promo_code": session.get("promo_code"),
        "promo_note": promo.get("note") if promo else None,
        "promo_error": calc.promo_error,
        "items_total": calc.items_total,
        "sale_total": calc.sale_total,
        "discount": calc.discount,
        "total": calc.items_total - calc.discount,
        "has_weighted": any(l["product"]["type"] == "weighted" for l in lines),
        "blocking": [l["product"]["name"] for l in lines
                     if l["unavailable"] or l["insufficient"]],
    }


def add_to_cart(state: Mapping[str, Any], session: dict[str, Any],
                data: Mapping[str, Any]) -> dict[str, Any]:
    p = next((x for x in state.get("products", [])
              if x.get("id") == data.get("product_id") and x.get("is_active") is not False),
             None)
    if p is None:
        return {"error": "product_not_found", "status": 404}

    is_pre = p.get("type") == "preorder"
    if not is_pre and not C.in_stock(p):
        return {"error": "out_of_stock", "status": 409}

    cap = float("inf") if is_pre else C.num(p.get("stock"))
    norm = C.normalize_item(p, data)
    cart = session.setdefault("cart", [])
    existing = next((it for it in cart if it.get("product_id") == p.get("id")), None)

    if existing:
        if p.get("type") == "unit":
            existing["qty"] = min(cap, C.num(existing.get("qty")) + (norm.get("qty") or 1))
            existing["qty"] = int(existing["qty"])
        else:
            existing["weight"] = _round2(
                min(cap, C.num(existing.get("weight")) + C.num(norm.get("weight"))))
    else:
        if p.get("type") == "unit":
            norm["qty"] = int(min(cap, norm["qty"]))
        else:
            norm["weight"] = C.js_number(min(cap, norm["weight"]))
        if len(cart) >= MAX_CART_ITEMS:
            return {"error": "cart_full", "status": 409}
        cart.append(norm)
    return {"ok": True}


def update_cart(state: Mapping[str, Any], session: dict[str, Any],
                data: Mapping[str, Any]) -> dict[str, Any]:
    p = next((x for x in state.get("products", [])
              if x.get("id") == data.get("product_id")), None)
    if p is None:
        return {"error": "product_not_found", "status": 404}

    cart = session.get("cart") or []
    idx = next((i for i, it in enumerate(cart)
                if it.get("product_id") == p.get("id")), -1)
    if idx < 0:
        return {"error": "not_in_cart", "status": 404}

    cap = float("inf") if p.get("type") == "preorder" else C.num(p.get("stock"))
    if p.get("type") == "unit":
        import math

        q = math.trunc(C.num(data.get("qty")))
        if q <= 0:
            cart.pop(idx)
        else:
            cart[idx]["qty"] = int(min(cap, q))
    else:
        w = C.num(data.get("weight"))
        # Ниже минимального веса позиция просто исчезает: так понятнее,
        # чем блокировать кнопку «минус».
        if w < C.MIN_WEIGHT_KG:
            cart.pop(idx)
        else:
            # Тот же шаг 0.5 кг, что и при добавлении. Иначе через
            # изменение можно было положить 1.37 кг, которые весы на
            # складе всё равно не отмерят.
            norm = C.normalize_item(p, {"weight": w})
            cart[idx]["weight"] = C.js_number(min(cap, norm["weight"]))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Промокод (ТЗ 6)
# ---------------------------------------------------------------------------
def count_promo_usage(state: Mapping[str, Any], promo_id: Any,
                      user_id: Any, phone: Any) -> int:
    """Сколько раз промокод использован этим клиентом.

    Считаем и по профилю, и по телефону: иначе гостевые заказы обходили
    бы ограничение сколько угодно раз, просто не входя в кабинет.
    """
    return sum(
        1 for u in state.get("promocode_usages", [])
        if u.get("promocode_id") == promo_id
        and ((user_id and u.get("user_id") == user_id)
             or (phone and u.get("phone") == phone))
    )


def apply_promo(state: Mapping[str, Any], session: dict[str, Any],
                code: Any, user_id: Any = None,
                now: datetime | None = None) -> dict[str, Any]:
    norm = str(code or "").strip().upper()
    promo = next((p for p in state.get("promocodes", []) if p.get("code") == norm), None)
    if promo is None:
        return {"error": "promo_not_found", "status": 404}
    if not promo.get("is_active"):
        return {"error": "promo_inactive", "status": 409}
    # Срок включительный — та же семантика, что у акции товара
    if C.date_expired(promo.get("valid_until"), now):
        return {"error": "promo_expired", "status": 409}
    if C.num(promo.get("uses_limit")) > 0 and \
            C.num(promo.get("uses_count")) >= C.num(promo.get("uses_limit")):
        return {"error": "promo_exhausted", "status": 409}
    if user_id and C.num(promo.get("per_user_limit")) > 0:
        used = sum(1 for u in state.get("promocode_usages", [])
                   if u.get("promocode_id") == promo.get("id")
                   and u.get("user_id") == user_id)
        if used >= C.num(promo.get("per_user_limit")):
            return {"error": "promo_user_limit", "status": 409}

    # ТЗ 6.1 — один промокод на заказ, поэтому просто перезаписываем
    session["promo_code"] = promo.get("code")
    return {"ok": True, "note": promo.get("note")}


# ---------------------------------------------------------------------------
# Оформление заказа (ТЗ 4.2, 4.3, 4.4, 6, 14.2)
# ---------------------------------------------------------------------------
def place_order(state: dict[str, Any], *, next_id: Callable[[str], int],
                now_iso: Callable[[], str], session: dict[str, Any],
                data: Mapping[str, Any], now: datetime | None = None,
                ip: str | None = None, user_agent: str = "") -> dict[str, Any]:
    """Оформление. Вызывать только внутри транзакции.

    Проверка остатка и его списание не могут быть разорваны другим
    запросом — иначе один и тот же последний товар продастся дважды.
    """
    from datetime import datetime as _dt, timezone as _tz

    moment = now or _dt.now(_tz.utc)
    cart = list(session.get("cart") or [])
    if not cart:
        return {"error": "cart_empty", "status": 409}

    # ТЗ 14.2 — согласие на обработку персональных данных обязательно
    if not data.get("consent"):
        return {"error": "consent_required", "status": 422}

    # --- 1. Товары и остатки (ТЗ 4.2) ---
    by_id = {p.get("id"): p for p in state.get("products", [])}
    problems = []
    for it in cart:
        p = by_id.get(it.get("product_id"))
        if p is None or p.get("is_active") is False:
            problems.append({"product_id": it.get("product_id"), "reason": "unavailable"})
            continue
        want = C.num(it.get("qty")) if p.get("type") == "unit" else C.num(it.get("weight"))
        if want <= 0:
            problems.append({"product_id": p.get("id"), "name": p.get("name"),
                             "reason": "bad_quantity"})
            continue
        # Предзаказное мясо остаток не расходует — оно заказывается отдельно
        if p.get("type") != "preorder" and want > C.num(p.get("stock")):
            problems.append({"product_id": p.get("id"), "name": p.get("name"),
                             "reason": "insufficient", "available": p.get("stock")})
    if problems:
        return {"error": "items_unavailable", "status": 409, "problems": problems}

    # --- 2. Способ получения и зона (ТЗ 5.1) ---
    zone = None
    if data.get("method") == "delivery":
        zone = next((z for z in state.get("delivery_zones", [])
                     if z.get("id") == data.get("zone_id") and z.get("is_active") is not False),
                    None)
        if zone is None:
            return {"error": "zone_required", "status": 422}
        if not data.get("address") or len(str(data["address"])) < 5:
            return {"error": "address_required", "status": 422}
        # Зона без тарифа (ТЗ 5.2) больше не отклоняет заказ (ROADMAP
        # 2.12) — calc_order сам отдаст needs_quote=True и delivery=0,
        # ниже это превращается в статус awaiting_delivery_quote вместо
        # обычного new/awaiting_payment.

    # --- 3. Слот (ТЗ 4.4) ---
    s = state.get("settings", {})
    capacity = (s.get("slot_capacity_pickup") if data.get("method") == "pickup"
                else s.get("slot_capacity_delivery"))
    day_slots = C.slots_for_date(
        ymd=data.get("slot_ymd"), now=moment,
        work_from=int(C.num(s.get("work_from"))),
        work_to=int(C.num(s.get("work_to"))),
        cutoff_h=s.get("cutoff_h"), capacity=capacity,
        booked=booking_map(state, data.get("method")),
        holidays=s.get("holidays") or [])
    slot = next((x for x in day_slots if x["from"] == data.get("slot_from")), None)
    if slot is None:
        return {"error": "slot_not_found", "status": 422}
    if not slot["ok"]:
        return {"error": "slot_unavailable", "status": 409, "reason": slot["reason"]}

    horizon_last = C.add_days_ymd(C.msk_parts(moment).ymd,
                                  int(C.num(s.get("horizon_d"))) - 1)
    if str(data.get("slot_ymd")) > horizon_last:
        return {"error": "slot_out_of_horizon", "status": 422}

    # --- 4. Промокод (ТЗ 6) ---
    promo = None
    want_code = str(data.get("promo_code") or session.get("promo_code") or "").strip().upper()
    if want_code:
        promo = next((p for p in state.get("promocodes", [])
                      if p.get("code") == want_code), None)
        if promo is not None:
            if C.num(promo.get("per_user_limit")) > 0:
                used = count_promo_usage(state, promo.get("id"),
                                         session.get("user_id"), data.get("phone"))
                if used >= C.num(promo.get("per_user_limit")):
                    promo = None
            if promo is not None and C.num(promo.get("uses_limit")) > 0 and \
                    C.num(promo.get("uses_count")) >= C.num(promo.get("uses_limit")):
                promo = None
            if promo is not None and not promo.get("is_active"):
                promo = None
            if promo is not None and C.date_expired(promo.get("valid_until"), moment):
                promo = None

    # --- 5. Расчёт: то же ядро, что считает витрина ---
    calc = C.calc_order(items=cart, products=state.get("products", []), promo=promo,
                        zone=zone, method=data.get("method"), now=moment)
    if calc.promo_error:
        promo = None       # не применился — использование не засчитываем

    # --- 6. Запись ---
    has_weighted = any((by_id.get(it.get("product_id")) or {}).get("type") == "weighted"
                       for it in cart)
    user = None
    if session.get("user_id"):
        user = next((u for u in state.get("users", [])
                     if u.get("id") == session["user_id"]), None)

    order_number = ORDER_NUMBER_BASE + int(C.num(state.get("seq", {}).get("orders"))) + 1
    payment = data.get("payment")

    order = {
        "id": next_id("orders"),
        "number": order_number,
        "user_id": user.get("id") if user else None,
        "is_guest": not user,
        "name": data.get("name"), "phone": data.get("phone"), "email": data.get("email"),
        "method": data.get("method"),
        "delivery_zone_id": zone.get("id") if zone else None,
        "address": data.get("address") if data.get("method") == "delivery" else None,
        "slot_ymd": data.get("slot_ymd"),
        "slot_from": data.get("slot_from"),
        "slot_to": C.num(data.get("slot_from")) + C.SLOT_INTERVAL_H,
        "comment": data.get("comment") or "",
        "payment_method": payment,
        "payment_status": "pending",              # ТЗ 13 — независимая ось
        # Зона без тарифа (ТЗ 5.2) — заказ ждёт согласования доставки
        # персоналом (quote_delivery_cost) раньше, чем что-либо ещё:
        # без известной суммы предоплату не запросить, а с оплатой при
        # получении сборка началась бы по неполной цене.
        # Иначе — предоплаченные способы ждут подтверждения платежа.
        "status": ("awaiting_delivery_quote" if calc.needs_quote
                  else "awaiting_payment" if C.is_prepaid(payment) else "new"),
        "promocode_id": promo.get("id") if promo else None,
        "promocode": promo.get("code") if promo else None,
        "discount_amount": calc.discount,
        "delivery_discount": calc.delivery_discount,
        "delivery_cost": calc.delivery,
        # То, о чём договорились с клиентом: при сборке доставка может
        # стать только дешевле, но не дороже (см. recalc_order).
        "agreed_delivery_cost": calc.delivery,
        "items_total": calc.items_total,
        "total": calc.total,
        # ТЗ 3.4 — холд нужен ТОЛЬКО чтобы покрыть уточнение веса при
        # сборке. На заказе без весовых позиций сумма известна точно, и
        # блокировать лишние 10% не за что: это чужие деньги, замороженные
        # без причины.
        "hold_amount": calc.hold if (C.supports_hold(payment) and has_weighted) else 0,
        "planned_total": calc.total,
        "telegram_optin": bool(data.get("telegram_optin")),
        "created_at": now_iso(), "updated_at": now_iso(),
    }
    order["slot_to"] = C.js_number(order["slot_to"])
    state.setdefault("orders", []).append(order)

    for it in cart:
        p = by_id[it["product_id"]]
        state.setdefault("order_items", []).append({
            "id": next_id("order_items"),
            "order_id": order["id"],
            "product_id": p.get("id"),
            "sku": p.get("sku"), "name": p.get("name"), "type": p.get("type"),
            "requested_quantity": it.get("qty") if p.get("type") == "unit" else None,
            "requested_weight": None if p.get("type") == "unit" else it.get("weight"),
            "actual_weight": None,
            "is_removed": False,
            "price_at_purchase": C.js_number(C.unit_price(p)),   # ТЗ 13 — фиксируем
            "was_sale": C.is_sale(p),                            # нужно при пересчёте
            "vat_rate": p.get("vat_rate"),
        })
        # ТЗ 4.2 — остаток списывается ИМЕННО ЗДЕСЬ, при создании заказа
        if p.get("type") != "preorder":
            want = C.num(it.get("qty")) if p.get("type") == "unit" else C.num(it.get("weight"))
            left = _round2(C.num(p.get("stock")) - want)
            p["stock"] = left if left > 0 else 0   # страховка, сюда дойти не должно

    # ТЗ 4.4 — бронь места в слоте
    key = slot_key(data.get("method"), data.get("slot_ymd"), data.get("slot_from"))
    bookings = state.setdefault("slot_bookings", {})
    bookings[key] = int(C.num(bookings.get(key))) + 1

    # ТЗ 6.3 — учёт использования промокода
    if promo:
        promo["uses_count"] = int(C.num(promo.get("uses_count"))) + 1
        state.setdefault("promocode_usages", []).append({
            "id": next_id("promocode_usages"),
            "promocode_id": promo.get("id"),
            "user_id": user.get("id") if user else None,
            "order_id": order["id"],
            "phone": data.get("phone"),
            "at": now_iso(),
        })

    # ТЗ 14.2 — факт согласия логируется с датой и адресом
    state.setdefault("consents", []).append({
        "id": next_id("consents"),
        "user_id": user.get("id") if user else None,
        "order_id": order["id"],
        "phone": data.get("phone"),
        "personal_data": True,
        "marketing": bool(data.get("marketing_consent")),
        "ip": ip,
        "user_agent": str(user_agent or "")[:200],
        "at": now_iso(),
    })

    state.setdefault("order_status_history", []).append({
        "id": next_id("order_status_history"),
        "order_id": order["id"], "status": order["status"],
        "actor": "customer", "comment": None, "at": now_iso(),
    })

    # Корзина и промокод очищаются — заказ оформлен
    session["cart"] = []
    session["promo_code"] = None

    # Гостевой заказ привязывается к сессии: иначе покупатель, оформивший
    # заказ без регистрации, не сможет ни открыть его, ни отменить (ТЗ 4.6).
    recent = list(session.get("recent_orders") or []) + [order["id"]]
    session["recent_orders"] = recent[-RECENT_ORDERS_KEPT:]

    return {"ok": True, "order": order, "calc": calc}


# ---------------------------------------------------------------------------
# Отмена заказа (ТЗ 4.6)
# ---------------------------------------------------------------------------
def cancel_order(state: dict[str, Any], *, next_id: Callable[[str], int],
                 now_iso: Callable[[], str], order: dict[str, Any],
                 actor: str, reason: Any = None) -> dict[str, Any]:
    if order.get("status") == "cancelled":
        return {"error": "already_cancelled", "status": 409}
    if order.get("status") == "delivered":
        return {"error": "already_delivered", "status": 409}

    # ТЗ 4.6 — остатки возвращаются
    items = [i for i in state.get("order_items", []) if i.get("order_id") == order.get("id")]
    for it in items:
        if it.get("type") == "preorder" or it.get("is_removed"):
            continue
        p = next((x for x in state.get("products", [])
                  if x.get("id") == it.get("product_id")), None)
        if p is None:
            continue
        back = (C.num(it.get("requested_quantity")) if it.get("type") == "unit"
                else C.num(it.get("requested_weight")))
        p["stock"] = _round2(C.num(p.get("stock")) + back)

    # ТЗ 4.6 — лимит промокода возвращается
    if order.get("promocode_id"):
        promo = next((p for p in state.get("promocodes", [])
                      if p.get("id") == order["promocode_id"]), None)
        if promo and C.num(promo.get("uses_count")) > 0:
            promo["uses_count"] = int(C.num(promo["uses_count"])) - 1
        usages = state.get("promocode_usages", [])
        idx = next((i for i, u in enumerate(usages)
                    if u.get("order_id") == order.get("id")), -1)
        if idx >= 0:
            usages.pop(idx)

    # Освобождаем место в слоте
    key = slot_key(order.get("method"), order.get("slot_ymd"), order.get("slot_from"))
    bookings = state.setdefault("slot_bookings", {})
    if C.num(bookings.get(key)) > 0:
        bookings[key] = int(C.num(bookings[key])) - 1

    # ТЗ 4.6 — оплаченный онлайн заказ уходит в возврат
    if order.get("payment_status") == "paid":
        order["payment_status"] = "refunded"

    order["status"] = "cancelled"
    order["cancel_reason"] = reason or None
    order["updated_at"] = now_iso()

    state.setdefault("order_status_history", []).append({
        "id": next_id("order_status_history"),
        "order_id": order.get("id"), "status": "cancelled",
        "actor": actor, "comment": reason or None, "at": now_iso(),
    })
    return {"ok": True, "order": order}


# ---------------------------------------------------------------------------
# Зона без тарифа (ТЗ 5.2, ROADMAP 2.12)
# ---------------------------------------------------------------------------
def quote_delivery_cost(state: dict[str, Any], *, next_id: Callable[[str], int],
                        now_iso: Callable[[], str], order: dict[str, Any],
                        cost: float, actor: str) -> dict[str, Any]:
    """Персонал называет стоимость доставки для зоны без тарифа.

    Раньше такой заказ вообще не создавался — `create_order` отказывал
    кодом `zone_manual_quote`. Теперь он создаётся сразу со статусом
    `awaiting_delivery_quote` (`calc.needs_quote`, delivery=0), а эта
    функция — единственный выход из него: тариф зоны (`delivery_zones
    .cost`) остаётся пустым и после согласования — сумма привязана к
    ЭТОМУ заказу, не к зоне в целом, следующий заказ туда же снова
    попросит расчёта. Дальше заказ идёт в тот же статус, что получил бы
    при оформлении, знай `calc_order` стоимость заранее.
    """
    if order.get("status") != "awaiting_delivery_quote":
        return {"error": "quote_not_expected", "status": 409}
    if cost < 0:
        return {"error": "bad_cost", "status": 422}

    cost = C.js_number(cost)
    order["delivery_cost"] = cost
    # agreed_delivery_cost — тот же потолок, что recalc_order применяет
    # к обычным заказам (инвариант 8: сумма меняется только вниз); здесь
    # он же не даёт будущему пересчёту откатить согласованную сумму к
    # нулю зоны (см. комментарий в recalc_order).
    order["agreed_delivery_cost"] = cost
    order["total"] = max(0, int(C.num(order.get("items_total")))
                         - int(C.num(order.get("discount_amount"))) + int(cost))
    order["planned_total"] = order["total"]
    order["status"] = "awaiting_payment" if C.is_prepaid(order.get("payment_method")) else "new"
    order["updated_at"] = now_iso()

    state.setdefault("order_status_history", []).append({
        "id": next_id("order_status_history"),
        "order_id": order.get("id"), "status": order["status"],
        "actor": actor, "comment": f"доставка согласована: {cost} ₽", "at": now_iso(),
    })
    return {"ok": True, "order": order}


# ---------------------------------------------------------------------------
# Предзаказ мяса (ТЗ 7)
# ---------------------------------------------------------------------------
def create_preorder(state: dict[str, Any], *, next_id: Callable[[str], int],
                    now_iso: Callable[[], str], session: Mapping[str, Any],
                    data: Mapping[str, Any], now: datetime | None = None,
                    ip: str | None = None, user_agent: str = "") -> dict[str, Any]:
    from datetime import datetime as _dt, timezone as _tz

    moment = now or _dt.now(_tz.utc)
    s = state.get("settings", {})

    p = next((x for x in state.get("products", [])
              if x.get("id") == data.get("product_id") and x.get("is_active") is not False),
             None)
    if p is None:
        return {"error": "product_not_found", "status": 404}
    if p.get("type") != "preorder":
        return {"error": "not_preorder_product", "status": 422}
    if not data.get("consent"):
        return {"error": "consent_required", "status": 422}

    dates = C.meat_dates(now=moment, days=s.get("meat_days") or [],
                         limit_kg=s.get("meat_limit_kg"),
                         cutoff_days=s.get("meat_cutoff_days"),
                         booked_kg=state.get("meat_bookings") or {}, horizon_d=21)
    day = next((d for d in dates if d["ymd"] == data.get("pickup_date")), None)
    if day is None:
        return {"error": "date_not_available", "status": 422}
    if not day["ok"]:
        return {"error": "date_closed", "status": 409, "reason": day["reason"]}

    # ТЗ 7.1 — дневной объём ограничен; проверка и бронь под транзакцией
    limit = C.num(s.get("meat_limit_kg"))
    booked = C.num((state.get("meat_bookings") or {}).get(data.get("pickup_date")))
    weight = C.num(data.get("weight"))
    if limit > 0 and booked + weight > limit:
        return {"error": "daily_limit_exceeded", "status": 409,
                "available_kg": C.js_number(max(0, limit - booked))}

    user = None
    if session.get("user_id"):
        user = next((u for u in state.get("users", [])
                     if u.get("id") == session["user_id"]), None)

    pre = {
        "id": next_id("preorders"),
        "number": PREORDER_NUMBER_BASE + int(C.num(state.get("seq", {}).get("preorders"))),
        "user_id": user.get("id") if user else None,
        "product_id": p.get("id"), "sku": p.get("sku"), "product_name": p.get("name"),
        "requested_weight": data.get("weight"),
        "price_per_kg": C.js_number(C.unit_price(p)),
        "estimate": C.money(C.unit_price(p) * weight),
        "pickup_date": data.get("pickup_date"),
        "name": data.get("name"), "phone": data.get("phone"),
        "comment": data.get("comment") or "",
        "status": "new",
        "created_at": now_iso(), "updated_at": now_iso(),
    }
    state.setdefault("preorders", []).append(pre)
    # Округляем: иначе 0.1 + 0.2 копит хвост и дневной лимит «плывёт»
    state.setdefault("meat_bookings", {})[data["pickup_date"]] = _round2(booked + weight)

    state.setdefault("consents", []).append({
        "id": next_id("consents"),
        "user_id": user.get("id") if user else None,
        "preorder_id": pre["id"], "phone": data.get("phone"),
        "personal_data": True, "marketing": False,
        "ip": ip, "user_agent": str(user_agent or "")[:200],
        "at": now_iso(),
    })
    return {"ok": True, "preorder": pre}


# ---------------------------------------------------------------------------
# Представление заказа и пересчёт
# ---------------------------------------------------------------------------
def billable_weight(i: Mapping[str, Any]) -> Any:
    """Вес, по которому считаются деньги (ТЗ 3.4).

    Согласование нужно только тогда, когда пересчёт УВЕЛИЧИВАЕТ сумму:
    списать с покупателя больше, чем он видел при оформлении, без
    звонка нельзя. Недовес применяется сразу, даже если он больше 10%:
    покупатель платит за то, что реально получил, и заставлять его
    платить за недостающие килограммы было бы хуже любого отклонения.
    """
    if i.get("actual_weight") is None:
        return i.get("requested_weight")
    if i.get("weight_confirmed") is False and \
            C.num(i.get("actual_weight")) > C.num(i.get("requested_weight")):
        return i.get("requested_weight")
    return i.get("actual_weight")


def line_total_of(i: Mapping[str, Any]) -> int:
    if i.get("is_removed"):
        return 0
    if i.get("type") == "unit":
        return C.money(C.num(i.get("price_at_purchase")) * C.num(i.get("requested_quantity")))
    return C.money(C.num(i.get("price_at_purchase")) * C.num(billable_weight(i)))


def order_view(state: Mapping[str, Any], order: Mapping[str, Any],
               for_staff: bool = False) -> dict[str, Any]:
    items = [{
        "id": i.get("id"), "product_id": i.get("product_id"), "sku": i.get("sku"),
        "name": i.get("name"), "type": i.get("type"),
        "requested_quantity": i.get("requested_quantity"),
        "requested_weight": i.get("requested_weight"),
        "actual_weight": i.get("actual_weight"), "is_removed": i.get("is_removed"),
        "weight_confirmed": i.get("weight_confirmed") is not False,
        "price_at_purchase": i.get("price_at_purchase"), "vat_rate": i.get("vat_rate"),
        "line_total": line_total_of(i),
    } for i in state.get("order_items", []) if i.get("order_id") == order.get("id")]

    zone = next((z for z in state.get("delivery_zones", [])
                 if z.get("id") == order.get("delivery_zone_id")), None)

    view = {
        "id": order.get("id"), "number": order.get("number"),
        "status": order.get("status"),
        "status_label": C.STATUS_LABEL.get(order.get("status")),
        "payment_status": order.get("payment_status"),
        "payment_label": C.PAYMENT_LABEL.get(order.get("payment_status")),
        "payment_method": order.get("payment_method"),
        "method": order.get("method"),
        "zone": zone.get("name") if zone else None,
        "address": order.get("address"),
        "slot_ymd": order.get("slot_ymd"), "slot_from": order.get("slot_from"),
        "slot_to": order.get("slot_to"),
        "name": order.get("name"), "phone": order.get("phone"),
        "email": order.get("email"), "comment": order.get("comment"),
        "items": items,
        "items_total": order.get("items_total"),
        "discount_amount": order.get("discount_amount"),
        "delivery_cost": order.get("delivery_cost"), "total": order.get("total"),
        "planned_total": order.get("planned_total"),
        "hold_amount": order.get("hold_amount"),
        "promocode": order.get("promocode"),
        "can_cancel": C.customer_can_cancel(order.get("status")),
        "created_at": order.get("created_at"),
    }

    if for_staff:
        view["history"] = [{
            "status": h.get("status"), "label": C.STATUS_LABEL.get(h.get("status")),
            "actor": h.get("actor"), "comment": h.get("comment"), "at": h.get("at"),
        } for h in state.get("order_status_history", [])
            if h.get("order_id") == order.get("id")]
        view["is_guest"] = order.get("is_guest")
        view["cancel_reason"] = order.get("cancel_reason") or None
        # Следующий шаг зависит от способа оплаты, а не только от позиции
        # в цепочке статусов.
        transitions = C.allowed_transitions(order)
        view["next_status"] = (transitions["allowed"] or [None])[0]
        view["blocked_reason"] = transitions["blockedReason"]
        # Позиции, где сборщик вписал вес вне допуска и ждёт согласования
        view["awaiting_weight"] = [{"item_id": i["id"], "name": i["name"]}
                                   for i in items if i["weight_confirmed"] is False]
    return view


def recalc_order(state: dict[str, Any], order: dict[str, Any]) -> dict[str, Any]:
    """Пересчёт после правок сборщика (ТЗ 3.4, 8.4).

    Считаем по ЗАФИКСИРОВАННЫМ в заказе ценам, а не по текущему
    каталогу: если после оформления администратор поменял цену,
    покупатель всё равно платит ту, что видел при заказе (ТЗ 13).
    Для этого собирается псевдо-каталог из позиций самого заказа.
    """
    items = [i for i in state.get("order_items", []) if i.get("order_id") == order.get("id")]

    frozen = []
    for i in items:
        price = C.num(i.get("price_at_purchase"))
        is_unit = i.get("type") == "unit"
        entry = {
            "id": i.get("product_id"),
            "type": i.get("type"),
            "price": price if is_unit else 0,
            "price_per_kg": 0 if is_unit else price,
            # Акционность уже учтена в зафиксированной цене, повторно
            # скидку не даём, но признак сохраняем: от него зависит
            # применимость промокода-процента.
            "sale_price": price if i.get("was_sale") else None,
            "stock": float("inf"),
        }
        if entry["sale_price"] is not None:
            # is_sale сравнивает акционную цену с базовой, поэтому для
            # акционных позиций базовую поднимаем на рубль: признак акции
            # сохраняется, а расчётная цена остаётся зафиксированной.
            base = price + 1
            if is_unit:
                entry["price"] = base
            else:
                entry["price_per_kg"] = base
        frozen.append(entry)

    promo = None
    if order.get("promocode_id"):
        promo = next((p for p in state.get("promocodes", [])
                      if p.get("id") == order["promocode_id"]), None)
    zone = next((z for z in state.get("delivery_zones", [])
                 if z.get("id") == order.get("delivery_zone_id")), None)

    pseudo = [{"product_id": i.get("product_id"),
               "qty": i.get("requested_quantity"),
               "weight": billable_weight(i)}
              for i in items if not i.get("is_removed")]

    calc = C.calc_order(items=pseudo, products=frozen, promo=promo,
                        zone=zone, method=order.get("method"))

    order["items_total"] = calc.items_total
    order["discount_amount"] = calc.discount
    order["delivery_discount"] = calc.delivery_discount

    # Стоимость доставки зафиксирована при оформлении. Если сборщик снял
    # позицию и сумма упала ниже порога бесплатной доставки, брать с
    # покупателя деньги, о которых он не договаривался, нельзя: недостача
    # товара — проблема магазина. Вниз доставка меняться может.
    agreed = C.num(order.get("agreed_delivery_cost")
                   if order.get("agreed_delivery_cost") is not None
                   else order.get("delivery_cost"))
    # Зона без тарифа (ТЗ 5.2): у неё calc.delivery ВСЕГДА 0 (zone.cost
    # так и остаётся пустым — тариф согласован персоналом на этот
    # заказ, а не на зону в целом). min(agreed, 0) обнулил бы уже
    # согласованную стоимость при первом же пересчёте после сборки
    # (снятие позиции, уточнение веса) — ровно то, что нельзя молча
    # терять (инвариант 8: сумма меняется только вниз С СОГЛАСИЯ, а не
    # произвольно).
    delivery = agreed if calc.needs_quote else min(agreed, C.num(calc.delivery))

    if not any(not i.get("is_removed") for i in items):
        delivery = 0        # везти нечего — доставка не платится

    order["delivery_cost"] = C.js_number(delivery)
    order["total"] = max(0, calc.items_total - calc.discount + int(delivery))

    out = calc.to_wire()
    out["delivery"] = C.js_number(delivery)
    out["total"] = order["total"]
    return out
