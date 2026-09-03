"""Корзина, заказы, предзаказ и вход по SMS — изменяющая часть API.

Каждый обработчик — тонкая обёртка: разобрать тело, проверить лимит,
вызвать доменную операцию внутри транзакции. Логика живёт в
`domain/shop.py` и `domain/auth.py`, где она проверяется прямыми
вызовами, без поднятого приложения.

Порядок проверок не случаен:

1. **Лимит частоты — до разбора тела.** Иначе перебор кода стоит
   ровно столько же, сколько честный запрос.
2. **CSRF — до любой записи.** Проверка стоит в `ctx.tx()`, а не в
   каждом обработчике: маршрут, где её забыли, внешне работает
   правильно, и пропажу замечают не сразу.
3. **Успех сбрасывает счётчик попыток.** Подобравший код до лимита не
   дойдёт, а честный клиент, меняющий промокоды в корзине, упёрся бы.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, Request

from ...domain import auth as A
from ...domain import calc as C
from ...domain import security as sec
from ...domain import shop as S
from ...domain.validate import SCHEMAS, validate
from ..context import Ctx, Fail, get_ctx

router = APIRouter(prefix="/api", tags=["cart"])


# ---------------------------------------------------------------------------
# Первый запрос витрины
# ---------------------------------------------------------------------------
@router.get("/bootstrap")
def bootstrap(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    """Всё, что витрине нужно на старте, одним запросом.

    Здесь же уезжает CSRF-токен: скрипт витрины читает его отсюда и
    возвращает заголовком в каждом изменяющем запросе.
    """
    state = ctx.state
    settings = state.get("settings") or {}
    categories = sorted(
        (c for c in state.get("categories") or [] if c.get("is_active") is not False),
        key=lambda c: C.num(c.get("sort_order")))
    return {
        "shop": {
            "name": settings.get("shop_name"), "phone": settings.get("phone"),
            "email": settings.get("email"),
            "work_from": settings.get("work_from"), "work_to": settings.get("work_to"),
            "pickup_address": settings.get("pickup_address"),
            "requisites": settings.get("requisites"),
        },
        "categories": [{"id": c.get("id"), "name": c.get("name"),
                        "emoji": c.get("emoji"), "is_system": bool(c.get("is_system"))}
                       for c in categories],
        "zones": [{"id": z.get("id"), "name": z.get("name"),
                   "cost": C.js_number(z.get("cost")),
                   "free_from": C.js_number(z.get("free_from")),
                   "manual_quote": bool(z.get("manual_quote"))}
                  for z in state.get("delivery_zones") or []
                  if z.get("is_active") is not False],
        "home": state.get("home_config") or {},
        "user": ({"id": ctx.user["id"], "name": ctx.user.get("name"),
                  "phone": ctx.user.get("phone"), "role": ctx.user.get("role")}
                 if ctx.user else None),
        "cart_count": len(ctx.session.get("cart") or []),
        "csrf": ctx.csrf_token,
    }


# ---------------------------------------------------------------------------
# Корзина (ТЗ 4.1)
# ---------------------------------------------------------------------------
@router.get("/cart")
def cart(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    return S.cart_view(ctx.state, ctx.session)


@router.post("/cart")
async def cart_add(request: Request, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    data = validate(SCHEMAS["cartAdd"], await _body(request))
    with ctx.tx() as unit:
        session = unit.ctx_session
        result = S.add_to_cart(unit.state, session, data)
        _raise_if_error(result)
        return S.cart_view(unit.state, session)


@router.put("/cart")
async def cart_update(request: Request, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    data = validate(SCHEMAS["cartUpdate"], await _body(request))
    with ctx.tx() as unit:
        session = unit.ctx_session
        result = S.update_cart(unit.state, session, data)
        _raise_if_error(result)
        return S.cart_view(unit.state, session)


@router.delete("/cart")
def cart_clear(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    with ctx.tx() as unit:
        session = unit.ctx_session
        session["cart"] = []
        # Промокод снимается вместе с корзиной: он мог быть выдан под
        # сумму, которой больше нет, и «висящая» скидка на новой
        # корзине — это уже другая сделка.
        session["promo_code"] = None
        return S.cart_view(unit.state, session)


@router.post("/cart/promo")
async def promo_apply(request: Request, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    # ТЗ 6.1 — не больше пяти попыток за десять минут: коды короткие,
    # и без лимита их подбирают перебором.
    limit = ctx.require_rate("promo")
    data = validate(SCHEMAS["promoApply"], await _body(request))

    with ctx.tx() as unit:
        session = unit.ctx_session
        result = S.apply_promo(unit.state, session, data["code"], session.get("user_id"))
        if result.get("error"):
            raise Fail(result.get("status", 400), result["error"],
                       attempts_left=limit["remaining"])
        view = S.cart_view(unit.state, session)

    # Код подошёл — это не подбор. Счётчик обнуляем, иначе честный
    # клиент, примеряющий промокоды в корзине, упрётся в лимит.
    ctx.rate_limit_reset("promo")
    return view


@router.delete("/cart/promo")
def promo_drop(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    with ctx.tx() as unit:
        session = unit.ctx_session
        session["promo_code"] = None
        return S.cart_view(unit.state, session)


# ---------------------------------------------------------------------------
# Заказы (ТЗ 4.3)
# ---------------------------------------------------------------------------
@router.post("/orders")
async def place_order(request: Request, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_rate("orderPlace")
    data = validate(SCHEMAS["checkout"], await _body(request))

    with ctx.tx() as unit:
        session = unit.ctx_session
        result = S.place_order(
            unit.state, next_id=unit.next_id, now_iso=A.iso_now,
            session=session, data=data, ip=ctx.ip,
            user_agent=request.headers.get("user-agent", ""))
        if result.get("error"):
            raise Fail(result.get("status", 409), result["error"],
                       **{k: v for k, v in result.items()
                          if k not in ("error", "status")})
        order = result["order"]
        return {"order": S.order_view(unit.state, order),
                "picker_message": picker_message(unit.state, order)}


@router.get("/orders/{order_id}")
def order(order_id: str, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    found = next((o for o in ctx.state.get("orders") or []
                  if str(o.get("id")) == order_id), None)
    if found is None:
        raise Fail(404, "order_not_found")
    _require_own(ctx, found)
    return {"order": S.order_view(ctx.state, found),
            "picker_message": picker_message(ctx.state, found)}


@router.post("/orders/{order_id}/cancel")
async def cancel_order(order_id: str, request: Request,
                       ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    data = validate(SCHEMAS["orderCancel"], await _body(request))
    with ctx.tx() as unit:
        session = unit.ctx_session
        found = next((o for o in unit.state.get("orders") or []
                      if str(o.get("id")) == order_id), None)
        if found is None:
            raise Fail(404, "order_not_found")
        _require_own(ctx, found, session)
        # ТЗ 4.6 — покупатель отменяет только до начала сборки.
        if not C.customer_can_cancel(found.get("status")):
            raise Fail(409, "cancel_not_allowed")

        result = S.cancel_order(unit.state, next_id=unit.next_id, now_iso=A.iso_now,
                                order=found, actor="customer",
                                reason=data.get("reason"))
        _raise_if_error(result)
        return {"order": S.order_view(unit.state, found)}


def _require_own(ctx: Ctx, order: dict, session: dict | None = None) -> None:
    """Чужой заказ не отдаём даже по прямой ссылке.

    Номер заказа перебирается за минуту, а в заказе лежат имя, телефон
    и адрес. Владение определяется профилем либо списком недавних
    заказов этой сессии — угадать его нельзя.
    """
    s = session or ctx.session
    user_id = (ctx.user or {}).get("id") or s.get("user_id")
    mine = (user_id is not None and order.get("user_id") == user_id) or \
        order.get("id") in (s.get("recent_orders") or [])
    if not mine:
        raise Fail(403, "forbidden")


# ---------------------------------------------------------------------------
# Предзаказ мяса (ТЗ 7)
# ---------------------------------------------------------------------------
@router.post("/preorders")
async def create_preorder(request: Request, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    ctx.require_rate("orderPlace")
    data = validate(SCHEMAS["preorder"], await _body(request))

    with ctx.tx() as unit:
        session = unit.ctx_session
        result = S.create_preorder(
            unit.state, next_id=unit.next_id, now_iso=A.iso_now,
            session=session, data=data, ip=ctx.ip,
            user_agent=request.headers.get("user-agent", ""))
        if result.get("error"):
            raise Fail(result.get("status", 409), result["error"],
                       **{k: v for k, v in result.items()
                          if k not in ("error", "status")})
        pre = result["preorder"]
        # Последние двадцать: список нужен, чтобы гость видел свой
        # предзаказ, а не чтобы хранить историю — она есть в профиле.
        session["recent_preorders"] = (
            list(session.get("recent_preorders") or []) + [pre["id"]])[-20:]
        return {"preorder": pre, "picker_message": _preorder_message(pre)}


# ---------------------------------------------------------------------------
# Вход по коду из SMS (ТЗ 9.1)
# ---------------------------------------------------------------------------
@router.post("/auth/request-code")
async def request_code(request: Request, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    data = validate(SCHEMAS["otpRequest"], await _body(request))

    # Два лимита, и оба нужны. По номеру — чтобы чужой телефон нельзя
    # было завалить сообщениями (и чтобы не платить за них). По адресу —
    # чтобы с одного места не перебирали номера по одному.
    by_phone = ctx.rate_limit_key(f"otp:{data['phone']}", sec.LIMITS["otpSend"])
    if not by_phone["allowed"]:
        raise Fail(429, "too_many_codes", retry_after=by_phone["retryAfter"])
    ctx.require_rate("otpSendIp", {"limit": 10, "windowMs": 15 * 60 * 1000})

    with ctx.tx() as unit:
        issued = A.issue_otp(unit.state, next_id=unit.next_id, phone=data["phone"])

    response = {"sent": True, "expires_in": issued["expires_in_sec"],
                "phone_mask": sec.mask_phone(data["phone"])}
    # Провайдера SMS в MVP нет. Код возвращается в ответе ТОЛЬКО по
    # явному флагу: иначе войти в чужой профиль можно, зная один номер.
    if ctx.expose_otp:
        response["dev_code"] = issued["code"]
    else:
        print(f"[FructCity] код для {sec.mask_phone(data['phone'])} выпущен, "
              f"отправка в SMS", flush=True)
    return response


@router.post("/auth/verify-code")
async def verify_code(request: Request, ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    data = validate(SCHEMAS["otpVerify"], await _body(request))

    key = f"otpv:{data['phone']}"
    limited = ctx.rate_limit_key(key, sec.LIMITS["otpVerify"])
    if not limited["allowed"]:
        raise Fail(429, "too_many_attempts", retry_after=limited["retryAfter"])

    with ctx.tx() as unit:
        checked = A.verify_otp(unit.state, data["phone"], data["code"])
        if not checked["ok"]:
            raise Fail(401, checked["reason"],
                       attempts_left=checked.get("attempts_left"))

        user = A.ensure_customer(unit.state, next_id=unit.next_id, phone=data["phone"])
        linked = A.link_guest_orders(unit.state, user)     # ТЗ 9.1

        # Смена уровня доступа — новый идентификатор сессии. Иначе
        # заранее навязанный жертве идентификатор после входа стал бы
        # идентификатором входа.
        old = unit.ctx_session
        A.destroy_session(unit.state, old.get("sid"))
        fresh = A.rotated_session(old, next_id=unit.next_id,
                                  user_id=user["id"], role="customer",
                                  ttl_ms=A.SESSION_TTL_MS)
        unit.state["sessions"].append(fresh)
        payload = {"sid": fresh["sid"], "user": user, "linked": linked}

    ctx.rate_limit_reset(key)
    ctx.set_session_cookie(payload["sid"])
    user = payload["user"]
    return {"user": {"id": user["id"], "name": user.get("name"),
                     "phone": user.get("phone"), "role": user.get("role")},
            "linked_orders": payload["linked"]}


@router.post("/auth/logout")
def logout(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    with ctx.tx() as unit:
        A.destroy_session(unit.state, ctx.session.get("sid"))
    ctx.clear_session_cookie()
    return {"ok": True}


@router.get("/me")
def me(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    user = ctx.require_user()
    orders = sorted((o for o in ctx.state.get("orders") or []
                     if o.get("user_id") == user["id"]),
                    key=lambda o: str(o.get("created_at") or ""), reverse=True)[:50]
    preorders = sorted((p for p in ctx.state.get("preorders") or []
                        if p.get("user_id") == user["id"]),
                       key=lambda p: str(p.get("created_at") or ""), reverse=True)
    return {
        "user": {"id": user["id"], "name": user.get("name"), "phone": user.get("phone")},
        "orders": [S.order_view(ctx.state, o) for o in orders],
        "preorders": preorders,
    }


# ---------------------------------------------------------------------------
# Привязка Telegram (ТЗ 2.1.12, 8.2)
# ---------------------------------------------------------------------------
@router.post("/telegram/link")
def telegram_link(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    user = ctx.require_user()
    ctx.require_rate("tgLink", {"limit": 5, "windowMs": 10 * 60 * 1000})
    with ctx.tx() as unit:
        rec = A.issue_telegram_link(unit.state, next_id=unit.next_id,
                                    user_id=user["id"])
        token = rec["token"]
    bot = os.environ.get("FC_TELEGRAM_BOT") or "fructcity_bot"
    return {"deeplink": f"https://t.me/{bot}?start={token}", "bot": bot,
            "expires_in": A.TG_LINK_TTL_MS // 1000}


@router.get("/telegram/status")
def telegram_status(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    user = ctx.require_user()
    return {"linked": bool(user.get("telegram_chat_id"))}


@router.delete("/telegram/link")
def telegram_unlink(ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    user = ctx.require_user()
    with ctx.tx() as unit:
        A.unlink_telegram(unit.state, user["id"])
    return {"linked": False}


@router.post("/telegram/confirm")
async def telegram_confirm(request: Request,
                           ctx: Ctx = Depends(get_ctx)) -> dict[str, Any]:
    """Вебхук бота: приходит извне, без сессии и без CSRF-токена.

    Поэтому защищён общим секретом. Секрет обязателен: без него любой
    желающий привязал бы свой чат к чужому профилю, зная один токен из
    переписки. Нет секрета — маршрут не работает вовсе, а не работает
    без проверки.
    """
    secret = os.environ.get("FC_TELEGRAM_SECRET")
    if not secret:
        raise Fail(503, "telegram_not_configured")

    got = request.headers.get("x-telegram-secret")
    if not got or not sec.timing_safe_equal(got, secret):
        print(f"[FructCity] отклонён вебхук Telegram с {ctx.ip}: неверный секрет",
              flush=True)
        raise Fail(403, "bad_secret")

    ctx.require_rate("tgConfirm", {"limit": 60, "windowMs": 60 * 1000})
    data = validate(SCHEMAS["telegramConfirm"], await _body(request))

    with ctx.tx(csrf=False) as unit:
        result = A.confirm_telegram_link(unit.state, data["token"], data["chat_id"])
        if not result["ok"]:
            raise Fail(409, result["reason"])
        name = result["user"].get("name") or None
    return {"linked": True, "name": name}


# ---------------------------------------------------------------------------
# Сообщение сборщику (ТЗ 8.3)
# ---------------------------------------------------------------------------
def picker_message(state: dict[str, Any], order: dict[str, Any]) -> str:
    """Текст для рабочего чата. В MVP показывается на экране."""
    items = [i for i in state.get("order_items") or []
             if i.get("order_id") == order.get("id")]
    lines = []
    for i in items:
        if i.get("type") == "unit":
            amount = f"{C.js_number(i.get('requested_quantity'))} шт"
        else:
            total = C.money(C.num(i.get("price_at_purchase"))
                            * C.num(i.get("requested_weight")))
            amount = (f"~{C.js_number(i.get('requested_weight'))} кг "
                      f"(≈{C.js_number(total)} ₽)")
        lines.append(f"• {i.get('name')} — {amount}")

    zone = next((z for z in state.get("delivery_zones") or []
                 if z.get("id") == order.get("delivery_zone_id")), None)
    settings = state.get("settings") or {}
    where = (f"🚚 Доставка · {zone['name'] if zone else ''} · {order.get('address')}"
             if order.get("method") == "delivery"
             else f"🏪 Самовывоз · {settings.get('pickup_address')}")

    parts = [
        f"🧾 Заказ #{order.get('number')}",
        f"{order.get('name')}, {order.get('phone')}",
        where,
        f"🕒 {order.get('slot_ymd')}, {order.get('slot_from')}:00–{order.get('slot_to')}:00",
        "",
        *lines,
        "",
        f"Скидка: −{C.js_number(order.get('discount_amount'))} ₽"
        if order.get("discount_amount") else None,
        f"Доставка: {C.js_number(order.get('delivery_cost'))} ₽"
        if order.get("delivery_cost") else "Доставка: 0 ₽",
        f"💰 Итого: {C.js_number(order.get('total'))} ₽ "
        f"({C.payment_label(order.get('payment_method'), True)})",
        f"🔒 Холд: {C.js_number(order.get('hold_amount'))} ₽"
        if order.get("hold_amount") else None,
        "⚖️ Есть весовые позиции — взвесить при сборке"
        if any(i.get("type") == "weighted" for i in items) else None,
        f"💬 {order.get('comment')}" if order.get("comment") else None,
        "",
        "🧾 Чек по 54-ФЗ придёт на " + (order.get("email") or "телефон"),
    ]
    # `if p`, а не `if p is not None`: выбрасываются и пустые строки.
    # Пустая строка здесь — разделитель блоков, и если соседняя
    # необязательная строка отсутствует (нет скидки, холда, весовых,
    # комментария), разделитель остаётся в одиночестве и даёт лишний
    # перевод строки. Сборщик получает сообщение с дырой посередине.
    # Регресс — `test_picker_message.py`.
    return "\n".join(p for p in parts if p)


def _preorder_message(pre: dict[str, Any]) -> str:
    parts = [
        f"🥩 ПРЕДЗАКАЗ #{pre.get('number')}",
        f"{pre.get('name')}, {pre.get('phone')}",
        f"{pre.get('product_name')} — ~{C.js_number(pre.get('requested_weight'))} кг",
        f"Дата выдачи: {pre.get('pickup_date')}",
        f"Ориентир: ≈{C.js_number(pre.get('estimate'))} ₽ "
        f"({C.js_number(pre.get('price_per_kg'))} ₽/кг, оплата по факту)",
        f"💬 {pre.get('comment')}" if pre.get("comment") else None,
    ]
    # `if p`, а не `if p is not None`: выбрасываются и пустые строки.
    # Пустая строка здесь — разделитель блоков, и если соседняя
    # необязательная строка отсутствует (нет скидки, холда, весовых,
    # комментария), разделитель остаётся в одиночестве и даёт лишний
    # перевод строки. Сборщик получает сообщение с дырой посередине.
    # Регресс — `test_picker_message.py`.
    return "\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Мелочи
# ---------------------------------------------------------------------------
async def _body(request: Request) -> dict[str, Any]:
    """Тело запроса. Негодный JSON — это пустое тело, а не отказ 500.

    Дальше его всё равно проверит схема, и человек увидит, каких полей
    не хватает, вместо «внутренней ошибки сервера».
    """
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001 — форма тела проверяется схемой
        return {}
    return data if isinstance(data, dict) else {}


def _raise_if_error(result: dict[str, Any]) -> None:
    if result.get("error"):
        raise Fail(result.get("status", 400), result["error"])
