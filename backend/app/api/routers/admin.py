"""Админ-API: вход персонала, дашборд, товары, категории, заказы,
сборка, предзаказы, промокоды, настройки, персонал, CSV — 38 маршрутов.

Доступ проверяется на КАЖДОМ маршруте через `ctx.require_staff(право)` —
проверка на сервере, а не «скрытием пункта меню»: спрятанный в
интерфейсе раздел всё равно доступен по прямому запросу, если не
проверять права на бэкенде.

Логика живёт в `domain/admin.py`, `domain/catalog.py`, `domain/calc.py` —
роутер только разбирает запрос, проверяет право и вызывает домен внутри
транзакции.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response

from ...domain import admin as AD
from ...domain import auth as A
from ...domain import calc as C
from ...domain import security as sec
from ...domain import shop as S
from ...domain.validate import SCHEMAS, validate
from ..context import Ctx, Fail, get_ctx
from .cart import _body

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Вход персонала
# ---------------------------------------------------------------------------
@router.post("/login")
async def login(request: Request, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    rl = ctx.rate_limit("adminLogin")
    if not rl["allowed"]:
        raise Fail(429, "rate_limited", retry_after=rl["retryAfter"])

    data = validate(SCHEMAS["staffLogin"], await _body(request))

    with ctx.tx() as unit:
        user = A.verify_staff(unit.state, data["login"], data["password"])
        if user is None:
            out = None
        else:
            old = unit.ctx_session
            A.destroy_session(unit.state, old.get("sid"))
            # Вход в админку явно сбрасывает корзину — даже
            # если у анонимной сессии, с которой входит сотрудник, она
            # почему-то не пуста (например, тот же браузер листал витрину).
            fresh = A.rotated_session(old, next_id=unit.next_id,
                                      user_id=user["id"], role=user["role"],
                                      ttl_ms=A.STAFF_TTL_MS, cart=[])
            unit.state["sessions"].append(fresh)
            unit.state.setdefault("audit", []).append({
                "id": unit.next_id("audit"), "actor": user["login"], "action": "login",
                "details": ctx.ip, "at": A.iso_now(),
            })
            out = {"sid": fresh["sid"], "user": user}

    if out is None:
        print(f"[FructCity] неудачный вход в админку: {data['login']} с {ctx.ip}", flush=True)
        raise Fail(401, "bad_credentials")

    ctx.rate_limit_reset("adminLogin")
    ctx.set_session_cookie(out["sid"])
    user = out["user"]
    return {
        "user": {"id": user["id"], "name": user.get("name"), "login": user.get("login"),
                 "role": user.get("role")},
        "permissions": A.permissions_of(user.get("role")),
    }


@router.get("/me")
def me(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    user = ctx.require_staff()
    return {
        "user": {"id": user["id"], "name": user.get("name"), "login": user.get("login"),
                 "role": user.get("role")},
        "permissions": A.permissions_of(user.get("role")),
    }


# ---------------------------------------------------------------------------
# Дашборд (ТЗ 10.9)
# ---------------------------------------------------------------------------
@router.get("/dashboard")
def dashboard(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("dashboard")
    st = ctx.state

    orders = [o for o in st.get("orders") or [] if o.get("status") != "cancelled"]
    revenue = sum(C.num(o.get("total")) for o in orders)

    # выручка по товарам — из позиций, а не из «примерных» цифр
    by_product: dict[str, float] = {}
    by_cat: dict[Any, float] = {}
    order_ids = {o.get("id") for o in orders}
    products_by_id = {p.get("id"): p for p in st.get("products") or []}
    for i in st.get("order_items") or []:
        if i.get("order_id") not in order_ids:
            continue
        t = S.line_total_of(i)
        by_product[i.get("name")] = by_product.get(i.get("name"), 0) + t
        p = products_by_id.get(i.get("product_id"))
        cid = p.get("category_id") if p else "other"
        by_cat[cid] = by_cat.get(cid, 0) + t

    top = [{"name": name, "sum": s} for name, s in
           sorted(by_product.items(), key=lambda kv: -kv[1])[:5]]
    cats_by_id = {c.get("id"): c for c in st.get("categories") or []}
    cats = [{"id": cid, "name": (cats_by_id.get(cid) or {}).get("name") or cid, "sum": s,
             # money(), не round(): Python округляет .5 к чётному, JS — вверх
             "pct": C.money(s / revenue * 100) if revenue else 0}
            for cid, s in sorted(by_cat.items(), key=lambda kv: -kv[1])[:6]]

    all_orders = st.get("orders") or []
    return {
        "kpi": {
            "orders_total": len(all_orders),
            "orders_active": sum(1 for o in all_orders
                                 if o.get("status") not in ("delivered", "cancelled")),
            "revenue": C.js_number(revenue),
            "avg_check": C.money(revenue / len(orders)) if orders else 0,
            "preorders_new": sum(1 for p in st.get("preorders") or [] if p.get("status") == "new"),
            "low_stock": sum(1 for p in st.get("products") or []
                             if p.get("type") != "preorder" and C.num(p.get("stock")) <= 5),
            "cancelled": sum(1 for o in all_orders if o.get("status") == "cancelled"),
        },
        "periods": AD.revenue_by_period(orders),
        "daily": AD.daily_revenue(orders),
        "by_status": AD.orders_by_status(all_orders),
        "by_slot": AD.orders_by_slot(orders),
        "top_products": top,
        "top_categories": cats,
        "recent": [S.order_view(st, o, True) for o in list(all_orders)[-8:][::-1]],
    }


# ---------------------------------------------------------------------------
# Товары (ТЗ 10.1)
# ---------------------------------------------------------------------------
@router.get("/products")
def products_list(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("products")
    return {"products": [AD.admin_product(p) for p in ctx.state.get("products") or []]}


@router.post("/products")
async def products_create(request: Request, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("products")
    data = validate(SCHEMAS["product"], await _body(request))
    bad = AD.product_sanity(data)
    if bad:
        raise Fail(422, "validation_failed", fields=bad)

    with ctx.tx() as unit:
        st = unit.state
        if any(p.get("sku", "").lower() == data["sku"].lower() for p in st.get("products") or []):
            raise Fail(409, "sku_exists")
        if not any(c.get("id") == data["category_id"] for c in st.get("categories") or []):
            raise Fail(422, "category_not_found")

        slug = data.get("slug") or (C.slugify(data["name"]) + "-" + data["sku"].lower())
        # slug — это адрес карточки: дубль сделал бы второй товар
        # недоступным по ЧПУ и продублировал бы страницу в sitemap
        if any(p.get("slug") == slug for p in st.get("products") or []):
            raise Fail(409, "slug_exists")

        now = A.iso_now()
        product = {**data, "id": unit.next_id("products"), "vat_rate": int(data["vat_rate"]),
                   "slug": slug, "created_at": now, "updated_at": now}
        st.setdefault("products", []).append(product)
        st.setdefault("audit", []).append({
            "id": unit.next_id("audit"), "actor": ctx.user["login"], "action": "product.create",
            "details": product["sku"], "at": now,
        })
        return {"product": AD.admin_product(product)}


@router.put("/products/{product_id}")
async def products_update(product_id: str, request: Request,
                          ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("products")
    data = validate(SCHEMAS["product"], await _body(request))
    bad = AD.product_sanity(data)
    if bad:
        raise Fail(422, "validation_failed", fields=bad)

    with ctx.tx() as unit:
        st = unit.state
        p = next((x for x in st.get("products") or [] if str(x.get("id")) == product_id), None)
        if p is None:
            raise Fail(404, "product_not_found")
        if any(x.get("id") != p.get("id") and x.get("sku", "").lower() == data["sku"].lower()
               for x in st.get("products") or []):
            raise Fail(409, "sku_exists")
        if not any(c.get("id") == data["category_id"] for c in st.get("categories") or []):
            raise Fail(422, "category_not_found")

        slug = data.get("slug") or p.get("slug")
        if any(x.get("id") != p.get("id") and x.get("slug") == slug
               for x in st.get("products") or []):
            raise Fail(409, "slug_exists")

        now = A.iso_now()
        p.update({**data, "vat_rate": int(data["vat_rate"]), "slug": slug, "updated_at": now})
        st.setdefault("audit", []).append({
            "id": unit.next_id("audit"), "actor": ctx.user["login"], "action": "product.update",
            "details": p["sku"], "at": now,
        })
        return {"product": AD.admin_product(p)}


@router.delete("/products/{product_id}")
def products_delete(product_id: str, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("products")
    with ctx.tx() as unit:
        st = unit.state
        products = st.get("products") or []
        idx = next((i for i, x in enumerate(products) if str(x.get("id")) == product_id), -1)
        if idx < 0:
            raise Fail(404, "product_not_found")
        p = products[idx]
        now = A.iso_now()
        # товар, который уже в заказах, не удаляем физически — иначе история
        # заказов потеряет ссылку; деактивируем (ТЗ 3.2)
        if any(oi.get("product_id") == p.get("id") for oi in st.get("order_items") or []):
            p["is_active"] = False
            p["updated_at"] = now
            st.setdefault("audit", []).append({
                "id": unit.next_id("audit"), "actor": ctx.user["login"],
                "action": "product.deactivate", "details": p["sku"], "at": now,
            })
            return {"deactivated": True, "product": AD.admin_product(p)}
        products.pop(idx)
        st.setdefault("audit", []).append({
            "id": unit.next_id("audit"), "actor": ctx.user["login"], "action": "product.delete",
            "details": p["sku"], "at": now,
        })
        return {"deleted": True}


# ---------------------------------------------------------------------------
# Категории (ТЗ 10.2)
# ---------------------------------------------------------------------------
@router.get("/categories")
def categories_list(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("categories")
    st = ctx.state
    products = st.get("products") or []
    ordered = sorted(st.get("categories") or [], key=lambda c: C.num(c.get("sort_order")))
    return {"categories": [
        {**AD.category_out(c),
         "product_count": sum(1 for p in products if p.get("category_id") == c.get("id"))}
        for c in ordered
    ]}


@router.post("/categories")
async def categories_create(request: Request, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("categories")
    data = validate(SCHEMAS["category"], await _body(request))
    with ctx.tx() as unit:
        st = unit.state
        if any(c.get("id") == data["id"] for c in st.get("categories") or []):
            raise Fail(409, "category_exists")
        category = {**data, "sort_order": len(st.get("categories") or []),
                    "created_at": A.iso_now()}
        st.setdefault("categories", []).append(category)
        return {"category": AD.category_out(category)}


@router.put("/categories/{category_id}")
async def categories_update(category_id: str, request: Request,
                            ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("categories")
    data = validate(SCHEMAS["category"], await _body(request))
    with ctx.tx() as unit:
        c = next((x for x in unit.state.get("categories") or [] if x.get("id") == category_id), None)
        if c is None:
            raise Fail(404, "category_not_found")
        # id категории — внешний ключ у товаров, менять его нельзя
        c.update({"name": data["name"], "emoji": data["emoji"], "is_active": data["is_active"]})
        return {"category": AD.category_out(c)}


@router.post("/categories/reorder")
async def categories_reorder(request: Request, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("categories")
    body = await _body(request)
    ids = [str(x) for x in body.get("order")] if isinstance(body.get("order"), list) else []

    with ctx.tx() as unit:
        st = unit.state
        categories = st.get("categories") or []
        if len(ids) != len(categories):
            raise Fail(422, "order_length_mismatch")
        known = {c.get("id") for c in categories}
        if not all(i in known for i in ids):
            raise Fail(422, "unknown_category")
        # без этой проверки список из дубликатов проходил бы по длине, и
        # часть категорий сохранила бы старый порядок
        if len(set(ids)) != len(ids):
            raise Fail(422, "duplicate_category")
        by_id = {c.get("id"): c for c in categories}
        for i, cid in enumerate(ids):
            by_id[cid]["sort_order"] = i
        categories.sort(key=lambda c: c.get("sort_order"))
        return {"categories": [AD.category_out(c) for c in categories]}


@router.delete("/categories/{category_id}")
def categories_delete(category_id: str, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("categories")
    with ctx.tx() as unit:
        st = unit.state
        categories = st.get("categories") or []
        idx = next((i for i, x in enumerate(categories) if x.get("id") == category_id), -1)
        if idx < 0:
            raise Fail(404, "category_not_found")
        if categories[idx].get("is_system"):
            raise Fail(409, "category_is_system")
        if any(p.get("category_id") == category_id for p in st.get("products") or []):
            raise Fail(409, "category_not_empty")
        categories.pop(idx)
        return {"deleted": True}


# ---------------------------------------------------------------------------
# Заказы (ТЗ 10.3) — доступны и менеджеру
# ---------------------------------------------------------------------------
@router.get("/orders")
def orders_list(ctx: Ctx = Depends(get_ctx), status: str | None = None,
                q: str = "") -> dict[str, Any]:
    ctx.require_staff("orders")
    st = ctx.state
    query = (q or "").lower().strip()

    items = list(reversed(st.get("orders") or []))
    if status and status != "all":
        items = [o for o in items if o.get("status") == status]
    if query:
        items = [o for o in items
                if query in str(o.get("number", ""))
                or query in (o.get("name") or "").lower()
                or query in (o.get("phone") or "")]
    return {"orders": [S.order_view(st, o, True) for o in items[:200]]}


@router.get("/orders/{order_id}")
def order_detail(order_id: str, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("orders")
    o = next((x for x in ctx.state.get("orders") or [] if str(x.get("id")) == order_id), None)
    if o is None:
        raise Fail(404, "order_not_found")
    return {"order": S.order_view(ctx.state, o, True)}


@router.get("/orders/{order_id}/packing-list")
def packing_list(order_id: str, ctx: Ctx = Depends(get_ctx)) -> Response:
    ctx.require_staff("orders")
    o = next((x for x in ctx.state.get("orders") or [] if str(x.get("id")) == order_id), None)
    if o is None:
        raise Fail(404, "order_not_found")
    html = AD.packing_list_html(ctx.state, o)
    return Response(content=html, media_type="text/html; charset=utf-8",
                    headers={"X-Robots-Tag": "noindex, nofollow"})


@router.post("/orders/{order_id}/status")
async def order_status(order_id: str, request: Request,
                       ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("orders")
    data = validate(SCHEMAS["orderStatus"], await _body(request))

    with ctx.tx() as unit:
        st = unit.state
        o = next((x for x in st.get("orders") or [] if str(x.get("id")) == order_id), None)
        if o is None:
            raise Fail(404, "order_not_found")
        if o.get("status") == "cancelled":
            raise Fail(409, "order_cancelled")

        if data["status"] == "cancelled":
            result = S.cancel_order(st, next_id=unit.next_id, now_iso=A.iso_now, order=o,
                                    actor=ctx.user["login"], reason=data.get("comment"))
            if result.get("error"):
                raise Fail(result.get("status", 409), result["error"])
            return {"order": S.order_view(st, o, True)}

        # Движение по цепочке ТЗ 13 с учётом способа оплаты: предоплаченный
        # заказ не уходит в сборку без подтверждённого платежа, а заказ с
        # оплатой при получении перешагивает «ожидает оплаты».
        if data["status"] not in C.STATUS_FLOW:
            raise Fail(422, "bad_status")
        transitions = C.allowed_transitions(o)
        if transitions["blockedReason"]:
            raise Fail(409, transitions["blockedReason"])
        if data["status"] not in transitions["allowed"]:
            cur = C.STATUS_FLOW.index(o.get("status"))
            want = C.STATUS_FLOW.index(data["status"])
            raise Fail(409, "status_cannot_go_back" if want < cur else "status_skip_not_allowed",
                      allowed=transitions["allowed"])

        o["status"] = data["status"]
        o["updated_at"] = A.iso_now()
        # ТЗ 10.3 — доставленный заказ с оплатой при получении считается
        # оплаченным. Оплата при получении: доставили — значит, деньги
        # взяли. Предоплатные способы так не помечаем: подтверждение
        # приходит от платёжного шлюза.
        if (o["status"] == "delivered" and o.get("payment_status") == "pending"
                and not C.is_prepaid(o.get("payment_method"))):
            o["payment_status"] = "paid"
        st.setdefault("order_status_history", []).append({
            "id": unit.next_id("order_status_history"), "order_id": o.get("id"),
            "status": o["status"], "actor": ctx.user["login"],
            "comment": data.get("comment") or None, "at": A.iso_now(),
        })
        return {"order": S.order_view(st, o, True)}


# Смена статуса оплаты — отдельное право: ТЗ 10.8 отводит менеджеру заказы,
# но не деньги. Раньше маршрут довольствовался правом `orders`, и менеджер
# мог пометить любой заказ оплаченным или возвращённым.
@router.post("/orders/{order_id}/payment")
async def order_payment(order_id: str, request: Request,
                        ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("payments")
    data = validate(SCHEMAS["paymentStatus"], await _body(request))

    with ctx.tx() as unit:
        st = unit.state
        o = next((x for x in st.get("orders") or [] if str(x.get("id")) == order_id), None)
        if o is None:
            raise Fail(404, "order_not_found")
        if o.get("payment_status") == data["payment_status"]:
            return {"order": S.order_view(st, o, True)}          # идемпотентно
        # возврат — конечное состояние; отменённый заказ можно только вернуть
        if not C.can_change_payment(o, data["payment_status"]):
            raise Fail(409, "payment_transition_not_allowed",
                      **{"from": o.get("payment_status"), "to": data["payment_status"]})
        o["payment_status"] = data["payment_status"]
        o["updated_at"] = A.iso_now()
        st.setdefault("audit", []).append({
            "id": unit.next_id("audit"), "actor": ctx.user["login"], "action": "order.payment",
            "details": f"#{o.get('number')}:{data['payment_status']}", "at": A.iso_now(),
        })
        return {"order": S.order_view(st, o, True)}


@router.post("/orders/{order_id}/item-weight")
async def order_item_weight(order_id: str, request: Request,
                            ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    """Фактический вес позиции (ТЗ 3.4, 8.4)."""
    ctx.require_staff("orders")
    data = validate(SCHEMAS["orderItemWeight"], await _body(request))

    with ctx.tx() as unit:
        st = unit.state
        o = next((x for x in st.get("orders") or [] if str(x.get("id")) == order_id), None)
        if o is None:
            raise Fail(404, "order_not_found")
        closed = AD.order_closed_for_edits(o)
        if closed:
            raise Fail(409, closed)
        it = next((i for i in st.get("order_items") or []
                  if i.get("id") == data["item_id"] and i.get("order_id") == o.get("id")), None)
        if it is None:
            raise Fail(404, "item_not_found")
        if it.get("type") == "unit":
            raise Fail(422, "not_weighted")

        check = C.check_actual_weight(it.get("requested_weight"), data["actual_weight"])

        # ТЗ 3.4 — отклонение больше ±10% требует согласования с клиентом.
        # Фактический вес записываем всегда (сборщик его действительно
        # измерил), но в деньги он идёт только в допуске либо после явного
        # подтверждения. Иначе сумма молча вырастала бы выше согласованной,
        # а при онлайн-оплате — выше заблокированного холда.
        it["actual_weight"] = data["actual_weight"]
        it["weight_confirmed"] = check["ok"] or bool(data.get("confirm"))
        if data.get("confirm") and not check["ok"]:
            it["weight_confirmed_by"] = ctx.user["login"]
            it["weight_confirmed_at"] = A.iso_now()
            st.setdefault("audit", []).append({
                "id": unit.next_id("audit"), "actor": ctx.user["login"],
                "action": "order.weight_override",
                "details": (f"#{o.get('number')} {it.get('sku')}: "
                           f"{it.get('requested_weight')}→{data['actual_weight']} кг "
                           f"({check['deviation']}%)"),
                "at": A.iso_now(),
            })

        S.recalc_order(st, o)
        o["updated_at"] = A.iso_now()

        return {
            "order": S.order_view(st, o, True),
            "check": check,
            "needs_call": bool(check["needsCall"] and not it["weight_confirmed"]),
            # сумма не изменится, пока сборщик не подтвердит вес после звонка
            "awaiting_confirmation": not it["weight_confirmed"],
        }


@router.post("/orders/{order_id}/item-remove")
async def order_item_remove(order_id: str, request: Request,
                            ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("orders")
    data = validate(SCHEMAS["orderItemRemove"], await _body(request))

    with ctx.tx() as unit:
        st = unit.state
        o = next((x for x in st.get("orders") or [] if str(x.get("id")) == order_id), None)
        if o is None:
            raise Fail(404, "order_not_found")
        closed = AD.order_closed_for_edits(o)
        if closed:
            raise Fail(409, closed)
        it = next((i for i in st.get("order_items") or []
                  if i.get("id") == data["item_id"] and i.get("order_id") == o.get("id")), None)
        if it is None:
            raise Fail(404, "item_not_found")

        p = next((x for x in st.get("products") or [] if x.get("id") == it.get("product_id")), None)
        # возвращаем/списываем остаток при снятии и возврате позиции
        if p is not None and it.get("type") != "preorder":
            amount = (C.num(it.get("requested_quantity")) if it.get("type") == "unit"
                      else C.num(it.get("requested_weight")))
            # `_js_round`, а не `round()`: Python округляет .5 к чётному,
            # JS — вверх. На границе `Math.round`/`round()` расходятся,
            # и остаток на складе получил бы другое число.
            if data["is_removed"] and not it.get("is_removed"):
                p["stock"] = C.js_number(C._js_round((C.num(p.get("stock")) + amount) * 100) / 100)
            if not data["is_removed"] and it.get("is_removed"):
                p["stock"] = C.js_number(
                    max(0, C._js_round((C.num(p.get("stock")) - amount) * 100) / 100))
        it["is_removed"] = data["is_removed"]
        S.recalc_order(st, o)
        o["updated_at"] = A.iso_now()
        return {"order": S.order_view(st, o, True)}


# ---------------------------------------------------------------------------
# Предзаказы (ТЗ 10.4)
# ---------------------------------------------------------------------------
@router.get("/preorders")
def preorders_list(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("preorders")
    st = ctx.state
    settings = st.get("settings") or {}
    return {
        "preorders": list(reversed(st.get("preorders") or [])),
        "meat": {
            "days": settings.get("meat_days"),
            "limit_kg": settings.get("meat_limit_kg"),
            "cutoff_days": settings.get("meat_cutoff_days"),
            "bookings": st.get("meat_bookings"),
        },
    }


@router.post("/preorders/{preorder_id}/status")
async def preorder_status(preorder_id: str, request: Request,
                          ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("preorders")
    data = validate(SCHEMAS["preorderStatus"], await _body(request))

    with ctx.tx() as unit:
        st = unit.state
        p = next((x for x in st.get("preorders") or [] if str(x.get("id")) == preorder_id), None)
        if p is None:
            raise Fail(404, "preorder_not_found")
        bookings = st.setdefault("meat_bookings", {})
        cur = C.num(bookings.get(p.get("pickup_date")))
        kg = C.num(p.get("requested_weight"))
        # отмена возвращает килограммы в дневной лимит…
        if data["status"] == "cancelled" and p.get("status") != "cancelled":
            bookings[p["pickup_date"]] = C.js_number(max(0, C._js_round((cur - kg) * 100) / 100))
        # …а возврат заявки из отмены снова их занимает, иначе дневной
        # объём «протекал» бы при каждом цикле отмена → восстановление
        if data["status"] != "cancelled" and p.get("status") == "cancelled":
            bookings[p["pickup_date"]] = C.js_number(C._js_round((cur + kg) * 100) / 100)
        p["status"] = data["status"]
        p["updated_at"] = A.iso_now()
        return {"preorder": p}


# ---------------------------------------------------------------------------
# Промокоды (ТЗ 10.5)
# ---------------------------------------------------------------------------
@router.get("/promocodes")
def promocodes_list(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("promos")
    return {"promocodes": ctx.state.get("promocodes") or []}


@router.post("/promocodes")
async def promocodes_create(request: Request, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("promos")
    data = validate(SCHEMAS["promocode"], await _body(request))
    bad = AD.promo_sanity(data)
    if bad:
        raise Fail(422, "validation_failed", fields=bad)

    with ctx.tx() as unit:
        st = unit.state
        if any(p.get("code") == data["code"] for p in st.get("promocodes") or []):
            raise Fail(409, "code_exists")
        promo = {**data, "id": unit.next_id("promocodes"), "uses_count": 0, "note": "",
                "created_at": A.iso_now()}
        st.setdefault("promocodes", []).append(promo)
        return {"promocode": promo}


@router.put("/promocodes/{promocode_id}")
async def promocodes_update(promocode_id: str, request: Request,
                            ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("promos")
    data = validate(SCHEMAS["promocode"], await _body(request))
    bad = AD.promo_sanity(data)
    if bad:
        raise Fail(422, "validation_failed", fields=bad)

    with ctx.tx() as unit:
        st = unit.state
        p = next((x for x in st.get("promocodes") or [] if str(x.get("id")) == promocode_id), None)
        if p is None:
            raise Fail(404, "promocode_not_found")
        if any(x.get("id") != p.get("id") and x.get("code") == data["code"]
               for x in st.get("promocodes") or []):
            raise Fail(409, "code_exists")
        p.update(data)   # uses_count не трогаем — он служебный
        return {"promocode": p}


@router.delete("/promocodes/{promocode_id}")
def promocodes_delete(promocode_id: str, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("promos")
    with ctx.tx() as unit:
        st = unit.state
        promocodes = st.get("promocodes") or []
        idx = next((i for i, x in enumerate(promocodes) if str(x.get("id")) == promocode_id), -1)
        if idx < 0:
            raise Fail(404, "promocode_not_found")
        promocodes.pop(idx)
        return {"deleted": True}


# ---------------------------------------------------------------------------
# Доставка и настройки (ТЗ 10.6)
# ---------------------------------------------------------------------------
@router.get("/settings")
def settings_get(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("delivery")
    zones = [AD.zone_out(z) for z in ctx.state.get("delivery_zones") or []]
    return {"settings": ctx.state.get("settings"), "zones": zones}


@router.put("/settings")
async def settings_update(request: Request, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("delivery")
    data = validate(SCHEMAS["settings"], await _body(request))
    if data["work_to"] <= data["work_from"]:
        raise Fail(422, "validation_failed",
                  fields={"work_to": "конец рабочего дня должен быть позже начала"})

    with ctx.tx() as unit:
        st = unit.state
        st.setdefault("settings", {}).update(data)
        st.setdefault("audit", []).append({
            "id": unit.next_id("audit"), "actor": ctx.user["login"], "action": "settings.update",
            "details": None, "at": A.iso_now(),
        })
        return {"settings": st["settings"]}


@router.put("/zones/{zone_id}")
async def zone_update(zone_id: str, request: Request, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("delivery")
    body = await _body(request)
    data = validate(SCHEMAS["deliveryZone"], {**body, "id": zone_id})

    with ctx.tx() as unit:
        z = next((x for x in unit.state.get("delivery_zones") or []
                 if str(x.get("id")) == str(data["id"])), None)
        if z is None:
            raise Fail(404, "zone_not_found")
        if z.get("manual_quote"):
            raise Fail(409, "zone_manual_quote")           # ТЗ 5.2
        z["cost"] = data["cost"]
        z["free_from"] = data["free_from"]
        return {"zone": AD.zone_out(z)}


# ---------------------------------------------------------------------------
# Сотрудники (ТЗ 10.8) — только администратор
# ---------------------------------------------------------------------------
@router.get("/staff")
def staff_list(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("staff")
    return {"staff": [
        {"id": u["id"], "name": u.get("name"), "login": u.get("login"), "phone": u.get("phone"),
         "role": u.get("role"), "is_active": u.get("is_active")}
        for u in ctx.state.get("users") or [] if u.get("role") in ("admin", "manager")
    ]}


@router.post("/staff")
async def staff_create(request: Request, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("staff")
    data = validate(SCHEMAS["staff"], await _body(request))
    pw_problems = sec.password_problems(data["password"])
    if pw_problems:
        raise Fail(422, "weak_password", fields={"password": ", ".join(pw_problems)})

    with ctx.tx() as unit:
        st = unit.state
        if any(u.get("login") == data["login"] for u in st.get("users") or []):
            raise Fail(409, "login_exists")
        u = {
            "id": unit.next_id("users"), "name": data["name"], "login": data["login"],
            "phone": data["phone"], "email": None, "role": data["role"],
            "password_hash": sec.hash_password(data["password"]),
            "telegram_chat_id": None, "is_active": True, "created_at": A.iso_now(),
        }
        st.setdefault("users", []).append(u)
        st.setdefault("audit", []).append({
            "id": unit.next_id("audit"), "actor": ctx.user["login"], "action": "staff.create",
            "details": f"{u['login']}:{u['role']}", "at": A.iso_now(),
        })
        return {"staff": {"id": u["id"], "name": u["name"], "login": u["login"],
                          "phone": u["phone"], "role": u["role"], "is_active": True}}


@router.put("/staff/{staff_id}")
async def staff_update(staff_id: str, request: Request,
                       ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("staff")
    data = validate(SCHEMAS["staff"], await _body(request))
    if data.get("password"):
        pw = sec.password_problems(data["password"])
        if pw:
            raise Fail(422, "weak_password", fields={"password": ", ".join(pw)})

    with ctx.tx() as unit:
        st = unit.state
        u = next((x for x in st.get("users") or []
                 if str(x.get("id")) == staff_id and x.get("role") in ("admin", "manager")), None)
        if u is None:
            raise Fail(404, "staff_not_found")
        if any(x.get("id") != u.get("id") and x.get("login") == data["login"]
               for x in st.get("users") or []):
            raise Fail(409, "login_exists")

        # нельзя разжаловать самого себя и остаться без администраторов
        if u.get("id") == ctx.user["id"] and data["role"] != "admin":
            raise Fail(409, "cannot_demote_self")
        if u.get("role") == "admin" and data["role"] != "admin":
            admins = sum(1 for x in st.get("users") or []
                        if x.get("role") == "admin" and x.get("is_active"))
            if admins <= 1:
                raise Fail(409, "last_admin")

        u["name"] = data["name"]; u["login"] = data["login"]
        u["phone"] = data["phone"]; u["role"] = data["role"]
        if data.get("password"):
            u["password_hash"] = sec.hash_password(data["password"])
        st.setdefault("audit", []).append({
            "id": unit.next_id("audit"), "actor": ctx.user["login"], "action": "staff.update",
            "details": u["login"], "at": A.iso_now(),
        })
        return {"staff": {"id": u["id"], "name": u["name"], "login": u["login"],
                          "phone": u["phone"], "role": u["role"], "is_active": u.get("is_active")}}


@router.delete("/staff/{staff_id}")
def staff_delete(staff_id: str, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("staff")
    with ctx.tx() as unit:
        st = unit.state
        u = next((x for x in st.get("users") or []
                 if str(x.get("id")) == staff_id and x.get("role") in ("admin", "manager")), None)
        if u is None:
            raise Fail(404, "staff_not_found")
        if u.get("id") == ctx.user["id"]:
            raise Fail(409, "cannot_delete_self")
        if u.get("role") == "admin" and sum(
                1 for x in st.get("users") or []
                if x.get("role") == "admin" and x.get("is_active")) <= 1:
            raise Fail(409, "last_admin")
        # сессии удалённого сотрудника обрываем немедленно
        st["sessions"] = [s for s in st.get("sessions") or [] if s.get("user_id") != u.get("id")]
        st["users"].remove(u)
        st.setdefault("audit", []).append({
            "id": unit.next_id("audit"), "actor": ctx.user["login"], "action": "staff.delete",
            "details": u["login"], "at": A.iso_now(),
        })
        return {"deleted": True}


# ---------------------------------------------------------------------------
# Конструктор главной
# ---------------------------------------------------------------------------
@router.get("/home")
def home_get(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("home")
    return {"home": ctx.state.get("home_config"),
            "skus": [{"sku": p.get("sku"), "name": p.get("name")}
                    for p in ctx.state.get("products") or []]}


@router.put("/home")
async def home_update(request: Request, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("home")
    data = validate(SCHEMAS["homeConfig"], await _body(request))

    with ctx.tx() as unit:
        st = unit.state
        known = {p.get("sku") for p in st.get("products") or []}
        bad = [s for s in [*data["sale_skus"], *data["meat_skus"]] if s not in known]
        if bad:
            raise Fail(422, "unknown_sku", skus=bad)

        names = {s.get("id"): s.get("name")
                for s in (st.get("home_config") or {}).get("sections") or []}
        st["home_config"] = {
            "hero_tag": data["hero_tag"], "hero_title": data["hero_title"],
            "hero_text": data["hero_text"], "sale_skus": data["sale_skus"],
            "meat_skus": data["meat_skus"],
            "sections": [{"id": s["id"], "name": names.get(s["id"], s["id"]),
                         "is_visible": s["is_visible"]} for s in data["sections"]],
        }
        return {"home": st["home_config"]}


# ---------------------------------------------------------------------------
# Журнал действий
# ---------------------------------------------------------------------------
@router.get("/audit")
def audit_list(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("staff")
    return {"audit": list(reversed((ctx.state.get("audit") or [])[-200:]))}


# ---------------------------------------------------------------------------
# CSV импорт/экспорт (ТЗ 10.7)
# ---------------------------------------------------------------------------
def _csv_response(rows: list[str], filename: str) -> Response:
    # BOM в начале — чтобы Excel открыл файл в UTF-8, а не угадывал
    # кодировку по эвристике и не показал кириллицу «кракозябрами».
    body = "﻿" + "\r\n".join(rows)
    return Response(content=body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/products.csv")
def products_csv(ctx: Ctx = Depends(get_ctx)) -> Response:
    ctx.require_staff("export")
    rows = [";".join(AD.CSV_COLUMNS)]
    for p in ctx.state.get("products") or []:
        price = p.get("price") if p.get("type") == "unit" else p.get("price_per_kg")
        # csv_cell на все поля, не только на текстовые: он же переводит
        # None в пустую строку (`str(None)` дал бы буквальное «None») и
        # безвреден на числах — экранировать там нечего.
        rows.append(";".join(AD.csv_cell(x) for x in [
            p.get("sku"), p.get("name"), p.get("category_id"), p.get("type"),
            price, p.get("sale_price") or "", p.get("stock"), p.get("vat_rate"),
            "1" if p.get("is_active") else "0",
        ]))
    return _csv_response(rows, "products.csv")


@router.post("/products/import")
async def products_import(request: Request, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_staff("import")
    data = validate(SCHEMAS["csvImport"], await _body(request))
    parsed = AD.parse_csv(data["csv"])
    if parsed.get("error"):
        raise Fail(422, parsed["error"], line=parsed.get("line"))

    with ctx.tx() as unit:
        result = AD.import_products(unit.state, next_id=unit.next_id, now_iso=A.iso_now,
                                    rows=parsed["rows"], mode=data["mode"])
        unit.state.setdefault("audit", []).append({
            "id": unit.next_id("audit"), "actor": ctx.user["login"], "action": "products.import",
            "details": f"+{result['created']} ~{result['updated']} !{len(result['skipped'])}",
            "at": A.iso_now(),
        })
        return result


@router.get("/orders.csv")
def orders_csv(ctx: Ctx = Depends(get_ctx)) -> Response:
    ctx.require_staff("export")
    head = ["number", "created_at", "status", "payment_status", "method", "name", "phone",
           "items_total", "discount", "delivery", "total"]
    rows = [";".join(head)]
    for o in ctx.state.get("orders") or []:
        rows.append(";".join(AD.csv_cell(x) for x in [
            o.get("number"), o.get("created_at"), o.get("status"), o.get("payment_status"),
            o.get("method"), o.get("name"), o.get("phone"),
            o.get("items_total"), o.get("discount_amount"), o.get("delivery_cost"), o.get("total"),
        ]))
    return _csv_response(rows, "orders.csv")
