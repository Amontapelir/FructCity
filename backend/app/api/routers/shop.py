"""Публичное API витрины — часть только для чтения.

Имена полей и формат чисел менять нельзя: их читает `public/app.js`,
который поставляется вместе с сервером и обновляется одновременно с
ним. Расхождение — это не ошибка в журнале, а сломанная страница у
покупателя (инвариант 19).

Изменяющие маршруты — корзина, заказы, вход — в `cart.py`: они идут
через транзакцию, и держать их рядом с чтением значило бы смешивать
маршруты с разными требованиями к безопасности.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ...db.source import read_state
from ...db.store import StoreUnavailable
from ...domain import catalog as K
from ...domain.query import int_param, num_param

router = APIRouter(prefix="/api", tags=["shop"])


def get_state() -> dict[str, Any]:
    """Состояние магазина — из базы, если она настроена, иначе из JSON.

    Источник выбирается здесь, в одном месте: доменный слой получает
    одинаковый словарь в обоих случаях и о разнице не знает. Это и
    позволяет сверять ответы обеих версий — они проходят через один
    и тот же код.
    """
    try:
        return read_state()
    except StoreUnavailable as e:
        # 503, а не 500: сервер жив, недоступны данные — клиенту есть
        # смысл повторить запрос, а мониторингу видно, что чинить.
        raise HTTPException(status_code=503, detail="store_unavailable") from e


@router.get("/products")
def products(
    state: dict = Depends(get_state),
    category: str = Query(default="all"),
    q: str = Query(default=""),
    sort: str = Query(default="pop"),
    in_stock: str = Query(default="", alias="in_stock"),
    on_sale: str = Query(default="", alias="on_sale"),
    price_min: str | None = Query(default=None),
    price_max: str | None = Query(default=None),
    # Строками, а не int: FastAPI на `?offset=abc` ответил бы 422, а
    # витрина такого ответа не ждёт и показала бы пустой каталог.
    # Мусор приводится к нулю в `domain/query.py` — опечатка в адресе
    # не повод ломать покупателю страницу.
    offset: str | None = Query(default=None),
    limit: str | None = Query(default=None),
) -> dict[str, Any]:
    res = K.list_products(
        state,
        category=category or "all",
        search=q or "",
        sort=sort or "pop",
        # Ровно строка '1', а не «что-нибудь истинное»: витрина шлёт
        # именно её, а '?in_stock=true' фильтр не включает. Расширишь
        # условие — изменится выдача у тех, кто правит адрес руками.
        in_stock=in_stock == "1",
        on_sale=on_sale == "1",
        price_min=num_param(price_min),
        price_max=num_param(price_max),
        offset=int_param(offset, 0),
        limit=int_param(limit, 60),
    )
    res["price_range"] = K.price_range(state)
    return res


@router.get("/products/{key}")
def product(key: str, state: dict = Depends(get_state)) -> dict[str, Any]:
    p = K.find_product(state, key)
    if not p:
        raise HTTPException(status_code=404, detail="product_not_found")
    return {"product": K.public_product(p)}


@router.get("/slots")
def slots(state: dict = Depends(get_state), method: str = Query(default="delivery")) -> dict[str, Any]:
    return K.slots_view(state, "pickup" if method == "pickup" else "delivery")


@router.get("/meat-dates")
def meat_dates(state: dict = Depends(get_state)) -> dict[str, Any]:
    return K.meat_dates_view(state)
