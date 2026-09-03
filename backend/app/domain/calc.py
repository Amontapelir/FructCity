"""FructCity — расчётное ядро сервера.

Здесь нет ни базы, ни HTTP — только чистые функции, данные приходят
аргументами. Расчёт денег должен быть одним куском кода, который
проверяется тестами в отрыве от всего остального.

**Совместимость с JavaScript обязательна, и это не наследие переезда.**
Тот же расчёт идёт в браузере — `lib/calc.js` грузят `index.html` и
`admin.html`, чтобы показать сумму в корзине до отправки заказа.
Разойдись формулы — покупатель увидит в корзине одну сумму, а в
подтверждении другую. Поэтому здесь воспроизводится арифметика
JavaScript, а не «как принято в Python»:

* ``Math.round`` округляет ``.5`` вверх, а ``round()`` в Python — к
  чётному: ``round(2.5)`` даёт 2, а не 3. Отсюда ``_js_round``.
* ``parseFloat`` берёт числовой префикс строки: ``"1.5 кг"`` → 1.5.
  ``float()`` на такой строке падает. Отсюда ``num``.
* Деления идут в float, как в JS, а не в Decimal. Decimal был бы
  точнее, но дал бы другие результаты на границах округления — а
  совпадать надо с тем, что показано покупателю, и с тем, что уже
  посчитано в прежних заказах.

Расхождения ловит ``backend/tests/test_calc_matches_browser.py``: он
запускает `lib/calc.js` через `node` и сравнивает результаты функция за
функцией. `node` нужен только для этой проверки; без него она молча
пропускается, а значит формулы никто не стережёт.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "WEIGHT_TOLERANCE_PCT", "HOLD_MULTIPLIER", "SLOT_INTERVAL_H",
    "MIN_WEIGHT_KG", "WEIGHT_STEP_KG",
    "money", "num", "js_number", "base_price", "unit_price", "is_sale", "in_stock",
    "line_total", "normalize_item", "date_expired",
    "calc_order", "OrderCalc", "check_actual_weight", "hold_amount",
    "msk_parts", "MskParts", "add_days_ymd", "slots_for_date", "first_available_date",
    "meat_dates", "WEEKDAYS", "weekday_of",
    "normalize_query", "matches_query", "slugify",
    "STATUS_FLOW", "STATUS_LABEL", "PAYMENT_LABEL", "next_status",
    "customer_can_cancel", "allowed_transitions", "next_status_for",
    "can_change_payment", "PAYMENT_FLOW", "PAYMENT_METHODS",
    "PAYMENT_METHOD_LABEL", "PAYMENT_METHOD_SHORT", "payment_label",
    "is_prepaid", "supports_hold",
]

# ---------------------------------------------------------------------------
# Константы предметной области (ТЗ 3.4, 4.4, 5.1)
# ---------------------------------------------------------------------------
WEIGHT_TOLERANCE_PCT = 10   # ТЗ 3.4 — допустимое отклонение фактического веса
HOLD_MULTIPLIER = 1.1       # ТЗ 3.4 — холд = расчёт + 10%
SLOT_INTERVAL_H = 2         # ТЗ 4.4 — интервалы строго по два часа
MIN_WEIGHT_KG = 0.5
WEIGHT_STEP_KG = 0.5


# ---------------------------------------------------------------------------
# Арифметика в стиле JavaScript
# ---------------------------------------------------------------------------
def _js_round(x: float) -> int:
    """``Math.round``: к ближайшему, ровно ``.5`` — вверх (к плюс бесконечности).

    Считаем через ``floor`` и остаток, а не через ``floor(x + 0.5)``:
    прибавление 0.5 у чисел вроде 0.49999999999999994 даёт ровно 1.0
    из-за представления float, и результат оказался бы на единицу
    больше правильного.
    """
    if not math.isfinite(x):
        return 0
    f = math.floor(x)
    d = x - f
    if d < 0.5:
        return int(f)
    return int(f) + 1


_NUMERIC_PREFIX = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


def num(v: Any) -> float:
    """Аналог ``num`` из calc.js: всё, что не число, превращается в 0.

    Строки разбираются как ``parseFloat`` — по числовому префиксу,
    поэтому ``"1.5 кг"`` это 1.5, а ``"кг"`` это 0. Это не мелочь:
    в JSON из формы веса и цены нередко приходят строками.

    ``True`` тоже даёт 0. В JS ``Number.isFinite(true)`` ложно, потому
    что проверка не приводит тип; повторяем это, иначе булево значение
    в поле количества внезапно стало бы единицей.
    """
    if isinstance(v, bool):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else 0.0
    if isinstance(v, str):
        m = _NUMERIC_PREFIX.match(v.strip())
        if not m:
            return 0.0
        try:
            n = float(m.group(0))
        except ValueError:
            return 0.0
        return n if math.isfinite(n) else 0.0
    return 0.0


def money(n: Any) -> int:
    """Все суммы — целые рубли. Единая точка округления."""
    if isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n):
        return 0
    return _js_round(float(n))


def js_number(value: Any) -> Any:
    """Целое дробное — обратно в целое, как это делает JavaScript.

    В JS одно числовое множество: `99` и `99.0` неразличимы, и наружу
    уходит `99`. В Python это разные типы, и `num()` всегда возвращает
    float — поэтому цена 99 ₽ уезжала бы клиенту как `99.0`.

    Для витрины разницы нет (JS разберёт `99.0` обратно в `99`), но
    контракт API обязан совпадать буквально: иначе любой другой
    потребитель получит не то, а сверка ответов двух версий будет
    проходить только потому, что тест округляет числа перед
    сравнением. Такая проверка не проверяет ничего.

    Дробное остаётся дробным: 12.5 кг — это 12.5, а не 12.
    """
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


# ---------------------------------------------------------------------------
# Московский календарь
# ---------------------------------------------------------------------------
# У магазина сутки заканчиваются в полночь по Москве, а не по часовому
# поясу сервера. Пробуем настоящую базу часовых поясов, но не зависим
# от неё: в Windows системной базы нет, а пакет tzdata может не стоять.
# Москва живёт на UTC+3 без перевода часов с 2014 года, поэтому
# запасной вариант с фиксированным смещением даёт тот же результат.
try:  # pragma: no cover — ветка зависит от окружения
    from zoneinfo import ZoneInfo

    MSK = ZoneInfo("Europe/Moscow")
except Exception:  # noqa: BLE001 — любая ошибка означает «базы нет»
    MSK = timezone(timedelta(hours=3), "MSK")


@dataclass(frozen=True)
class MskParts:
    ymd: str
    hour: int
    minute: int


def msk_parts(moment: datetime | None = None) -> MskParts:
    """Дата и время момента по Москве."""
    dt = moment or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        # Наивное время считаем UTC — так же ведёт себя JS с ISO-строкой без зоны
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(MSK)
    return MskParts(ymd=local.strftime("%Y-%m-%d"), hour=local.hour, minute=local.minute)


def add_days_ymd(ymd: str, days: int) -> str:
    y, m, d = (int(x) for x in str(ymd).split("-"))
    return (date(y, m, d) + timedelta(days=days)).isoformat()


def date_expired(until_ymd: Any, now: datetime | None = None) -> bool:
    """Истёк ли срок вида ``YYYY-MM-DD``. Срок ВКЛЮЧИТЕЛЬНЫЙ.

    «До 20 августа» действует весь день 20-го и снимается само 21-го.
    Сравниваем календарные даты по Москве строками: строка «2026-08-20»,
    разобранная как дата, означала бы полночь UTC, и промокод считался
    бы просроченным весь последний день — это уже случалось.
    """
    if not until_ymd:
        return False
    return msk_parts(now).ymd > str(until_ymd)[:10]


# ---------------------------------------------------------------------------
# Цены
# ---------------------------------------------------------------------------
def base_price(p: Mapping[str, Any]) -> float:
    return num(p.get("price")) if p.get("type") == "unit" else num(p.get("price_per_kg"))


def is_sale(p: Mapping[str, Any], now: datetime | None = None) -> bool:
    """Действует ли акция (ТЗ 3.2)."""
    sp = num(p.get("sale_price"))
    if not (sp > 0 and sp < base_price(p)):
        return False
    return not date_expired(p.get("sale_until"), now)


def unit_price(p: Mapping[str, Any], now: datetime | None = None) -> float:
    return num(p.get("sale_price")) if is_sale(p, now) else base_price(p)


def in_stock(p: Mapping[str, Any]) -> bool:
    return num(p.get("stock")) > 0


def line_total(p: Mapping[str, Any], item: Mapping[str, Any], now: datetime | None = None) -> int:
    """Стоимость строки корзины.

    Весовой товар считается по весу; штучный — по количеству. Предзаказ
    оплачивается по факту при выдаче, но в корзине показываем ориентир
    по заявленному весу (ТЗ 7.1).
    """
    t = p.get("type")
    if t == "weighted":
        return money(unit_price(p, now) * num(item.get("weight")))
    if t == "preorder":
        w = item.get("weight")
        return money(unit_price(p, now) * (num(w) if w else 1))
    qty = max(0, math.trunc(num(item.get("qty"))))
    return money(unit_price(p, now) * qty)


def normalize_item(p: Mapping[str, Any], raw: Mapping[str, Any]) -> dict[str, Any]:
    """Единое место, где решается «сколько единиц или килограммов».

    Используется и при расчёте, и при валидации входящего запроса —
    иначе клиент мог бы прислать 0.3 кг там, где шаг 0.5.
    """
    if p.get("type") in ("weighted", "preorder"):
        w = num(raw.get("weight"))
        w = _js_round(w / WEIGHT_STEP_KG) * WEIGHT_STEP_KG
        w = max(MIN_WEIGHT_KG, _js_round(w * 100) / 100)
        return {"product_id": p.get("id"), "weight": js_number(w), "qty": None}
    q = max(1, math.trunc(num(raw.get("qty"))))
    return {"product_id": p.get("id"), "qty": q, "weight": None}


# ---------------------------------------------------------------------------
# Полный расчёт заказа (ТЗ 6.1)
# ---------------------------------------------------------------------------
@dataclass
class OrderCalc:
    """Результат расчёта. Поля повторяют JS-версию один в один."""

    lines: list[dict[str, Any]] = field(default_factory=list)
    items_total: int = 0
    sale_total: int = 0
    discount: int = 0
    delivery_discount: int = 0
    delivery: float = 0
    free_delivery: bool = False
    needs_quote: bool = False
    promo_error: str | None = None
    total: int = 0
    hold: int = 0

    def to_wire(self) -> dict[str, Any]:
        """Ключи как в JS-версии — контракт API менять нельзя.

        Витрина и админка читают ``itemsTotal`` и ``deliveryDiscount``.
        Пока фронтенд общий для обеих реализаций, имена полей обязаны
        совпадать до буквы.
        """
        return {
            "lines": self.lines,
            "itemsTotal": self.items_total,
            "saleTotal": self.sale_total,
            "discount": self.discount,
            "deliveryDiscount": self.delivery_discount,
            # Стоимость доставки приходит из настроек зоны через num()
            # и потому всегда float. Наружу — целым: витрина печатает
            # это число как есть (инвариант 19).
            "delivery": js_number(self.delivery),
            "freeDelivery": self.free_delivery,
            "needsQuote": self.needs_quote,
            "promoError": self.promo_error,
            "total": self.total,
            "hold": self.hold,
        }


def calc_order(
    *,
    items: Sequence[Mapping[str, Any]] = (),
    products: Sequence[Mapping[str, Any]] = (),
    promo: Mapping[str, Any] | None = None,
    zone: Mapping[str, Any] | None = None,
    method: str = "delivery",
    now: datetime | None = None,
) -> OrderCalc:
    """Порядок операций зафиксирован и менять его нельзя (ТЗ 6.1):

    1. сумма товаров;
    2. скидка промокода — процент НЕ применяется к акционным позициям;
    3. порог бесплатной доставки считается от суммы ПОСЛЕ скидки;
    4. промокод на доставку применяется последним.

    Любая перестановка меняет итог, поэтому шаги пронумерованы и в коде.
    """
    by_id = {str(p.get("id")): p for p in products}

    items_total = 0.0
    sale_total = 0.0
    lines: list[dict[str, Any]] = []

    for it in items:
        p = by_id.get(str(it.get("product_id")))
        if p is None:
            continue
        if it.get("is_removed"):
            lines.append({"product_id": p.get("id"), "total": 0, "removed": True})
            continue

        # При сборке считаем по фактическому весу, если он проставлен (ТЗ 3.4)
        aw = it.get("actual_weight")
        eff: Mapping[str, Any] = (
            {"qty": it.get("qty"), "weight": num(aw)}
            if aw is not None and aw != ""
            else it
        )
        # Момент расчёта общий для всего заказа: иначе позиция, посчитанная
        # на границе суток, получила бы акцию, а соседняя — уже нет.
        t = line_total(p, eff, now)
        on_sale = is_sale(p, now)
        items_total += t
        if on_sale:
            sale_total += t
        lines.append({"product_id": p.get("id"), "total": t, "removed": False, "sale": on_sale})

    items_total_i = money(items_total)
    sale_total_i = money(sale_total)

    # ---- промокод ----
    discount = 0
    delivery_discount = 0
    promo_error: str | None = None
    if promo:
        min_order = num(promo.get("min_order"))
        if not promo.get("is_active"):
            promo_error = "Промокод не активен"
        elif date_expired(promo.get("valid_until"), now):
            promo_error = "Срок действия промокода истёк"
        elif num(promo.get("uses_limit")) > 0 and num(promo.get("uses_count")) >= num(promo.get("uses_limit")):
            promo_error = "Промокод исчерпан"
        elif items_total_i < min_order:
            promo_error = f"Промокод действует от {_js_int_str(min_order)} ₽"
        elif promo.get("type") == "percent":
            # ТЗ 6.1 — процент не суммируется с акционной ценой
            base = items_total_i - sale_total_i
            discount = math.floor(base * num(promo.get("value")) / 100)
            if discount == 0:
                promo_error = "В корзине только акционные товары — промокод не применяется"
        elif promo.get("type") == "fixed":
            discount = int(min(num(promo.get("value")), items_total_i))
        # type == "delivery" обрабатывается ниже, после расчёта доставки

    after_discount = items_total_i - discount

    # ---- доставка (ТЗ 5.1) ----
    delivery: float = 0
    free_delivery = False
    needs_quote = False
    if method == "delivery" and zone:
        if zone.get("cost") is None:
            needs_quote = True                      # ТЗ 5.2 — «другие районы», расчёт вручную
        elif num(zone.get("free_from")) > 0 and after_discount >= num(zone.get("free_from")):
            delivery = 0
            free_delivery = True
        else:
            delivery = num(zone.get("cost"))

    if promo and promo.get("type") == "delivery" and not promo_error and delivery > 0:
        # Скидка не может превысить саму доставку: иначе процент больше 100
        # уводил бы стоимость доставки, а с ней и весь заказ, в минус.
        delivery_discount = int(min(delivery, math.floor(delivery * num(promo.get("value")) / 100)))
        delivery = delivery - delivery_discount

    # Страховка: к оплате не бывает отрицательной суммы ни при каких
    # настройках промокодов — деньги покупателю мы не доплачиваем.
    total = max(0, money(after_discount + delivery))

    return OrderCalc(
        lines=lines,
        items_total=items_total_i,
        sale_total=sale_total_i,
        discount=discount,
        delivery_discount=delivery_discount,
        delivery=delivery,
        free_delivery=free_delivery,
        needs_quote=needs_quote,
        promo_error=promo_error,
        total=total,
        hold=hold_amount(total),
    )


def _js_int_str(v: float) -> str:
    """Число в текст так, как его печатает JS: 1000, а не 1000.0."""
    return str(int(v)) if float(v).is_integer() else str(v)


# ---------------------------------------------------------------------------
# Весовые товары (ТЗ 3.4)
# ---------------------------------------------------------------------------
def check_actual_weight(requested: Any, actual: Any) -> dict[str, Any]:
    req = num(requested)
    act = num(actual)
    if req <= 0:
        return {"deviation": 0, "ok": False, "needsCall": True}
    # Округляем до 0.1%: без этого float даёт 10.000000000000002 на ровно
    # десяти процентах, и допустимое отклонение считалось бы превышением.
    dev = _js_round(((act - req) / req) * 1000) / 10
    return {
        "deviation": js_number(dev),
        "ok": abs(dev) <= WEIGHT_TOLERANCE_PCT,
        "needsCall": abs(dev) > WEIGHT_TOLERANCE_PCT,
    }


def hold_amount(total: Any) -> int:
    return math.ceil(num(total) * HOLD_MULTIPLIER)


# ---------------------------------------------------------------------------
# Слоты доставки (ТЗ 4.4)
# ---------------------------------------------------------------------------
def slots_for_date(
    *,
    ymd: str,
    now: datetime | None = None,
    work_from: int,
    work_to: int,
    cutoff_h: Any = 0,
    capacity: Any = 0,
    booked: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Слоты на дату — вместе с причиной недоступности.

    Закрытые слоты не выкидываются из списка: покупателю понятнее
    увидеть серый «занято», чем обнаружить, что интервалов стало меньше.
    """
    now_p = msk_parts(now)
    now_min = now_p.hour * 60 + now_p.minute
    booked = booked or {}
    capacity_n = num(capacity)
    out: list[dict[str, Any]] = []

    h = work_from
    while h + SLOT_INTERVAL_H <= work_to:
        used = num(booked.get(f"{ymd}|{h}"))
        ok = True
        reason: str | None = None
        if ymd < now_p.ymd:
            ok, reason = False, "прошло"
        elif ymd == now_p.ymd and (h * 60 - now_min) < num(cutoff_h) * 60:
            ok, reason = False, "поздно"
        if ok and capacity_n > 0 and used >= capacity_n:
            ok, reason = False, "занято"
        out.append({
            "ymd": ymd, "from": h, "to": h + SLOT_INTERVAL_H,
            # js_number: num() отдаёт float, и «занято 0» уехало бы как
            # 0.0 — в JS такого различия нет, контракт обязан совпасть
            "ok": ok, "reason": reason, "used": js_number(used), "capacity": capacity,
        })
        h += SLOT_INTERVAL_H
    return out


def first_available_date(*, horizon_d: Any, now: datetime | None = None, **slot_opts: Any) -> str | None:
    """Ближайшая дата со свободным слотом (ТЗ 4.4)."""
    now_p = msk_parts(now)
    for d in range(int(num(horizon_d))):
        ymd = add_days_ymd(now_p.ymd, d)
        if any(s["ok"] for s in slots_for_date(ymd=ymd, now=now, **slot_opts)):
            return ymd
    return None


# ---------------------------------------------------------------------------
# Предзаказ мяса (ТЗ 7.1)
# ---------------------------------------------------------------------------
WEEKDAYS = ["Вс", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб"]


def weekday_of(ymd: str) -> int:
    """День недели как в JS: 0 — воскресенье."""
    y, m, d = (int(x) for x in str(ymd).split("-"))
    return (date(y, m, d).weekday() + 1) % 7


def meat_dates(
    *,
    days: Iterable[str] = (),
    limit_kg: Any = 0,
    booked_kg: Mapping[str, Any] | None = None,
    cutoff_days: Any = 1,
    horizon_d: Any = 21,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Даты поставки мяса: дни недели из настроек, приём закрывается
    за ``cutoff_days`` суток, плюс дневной лимит в килограммах."""
    now_p = msk_parts(now)
    days_set = set(days or ())
    booked_kg = booked_kg or {}
    limit = num(limit_kg)
    out: list[dict[str, Any]] = []

    for d in range(int(num(horizon_d) or 21) + 1):
        ymd = add_days_ymd(now_p.ymd, d)
        wd = WEEKDAYS[weekday_of(ymd)]
        if wd not in days_set:
            continue
        booked = num(booked_kg.get(ymd))
        closed = d < num(cutoff_days or 1)
        full = limit > 0 and booked >= limit
        out.append({
            "ymd": ymd, "weekday": wd,
            "booked": js_number(booked), "limit": js_number(limit),
            "ok": not closed and not full,
            "reason": "приём закрыт" if closed else ("дневной объём выбран" if full else None),
        })
    return out


# ---------------------------------------------------------------------------
# Поиск (ТЗ С-6): синонимы и грубый стемминг
# ---------------------------------------------------------------------------
SYNONYMS = {
    "томат": "помидор", "помидорчик": "помидор", "кориандр": "кинза",
    "картошка": "картофель", "морковка": "морковь", "бульба": "картофель",
    "авокадо": "авокадо", "булка": "хлеб",
}
_ENDING = re.compile(r"(ами|ями|ов|ей|ах|ях|ы|и|а|я|у|ю|е|о)$")


def normalize_query(s: Any) -> str:
    s = str(s or "").lower().strip()
    for k, v in SYNONYMS.items():
        if k in s:
            s = s.replace(k, v)
    return s


def _stem(w: str) -> str:
    return _ENDING.sub("", w, count=1)


def matches_query(p: Mapping[str, Any], q: Any) -> bool:
    if not q:
        return True
    hay = normalize_query(f"{p.get('name', '')} {p.get('description') or ''} {p.get('sku', '')}")
    words = [w for w in re.split(r"\s+", normalize_query(q)) if w]
    return all(w in hay or (len(_stem(w)) > 2 and _stem(w) in hay) for w in words)


# ---------------------------------------------------------------------------
# Slug для ЧПУ (ТЗ 15.3)
# ---------------------------------------------------------------------------
TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e",
    "ю": "yu", "я": "ya",
}


def slugify(s: Any) -> str:
    out = "".join(TRANSLIT.get(ch, ch) for ch in str(s or "").lower())
    out = re.sub(r"[^a-z0-9]+", "-", out)
    out = out.strip("-")[:80]
    return out or "item"


# ---------------------------------------------------------------------------
# Статусная модель заказа (ТЗ 13)
# ---------------------------------------------------------------------------
STATUS_FLOW = [
    "new", "awaiting_payment", "assembling", "partially_assembled",
    "ready", "in_delivery", "delivered",
]
STATUS_LABEL = {
    "new": "Новый", "awaiting_payment": "Ожидает оплаты", "assembling": "Сборка",
    "partially_assembled": "Частично собран", "ready": "Готов",
    "in_delivery": "В доставке", "delivered": "Доставлен", "cancelled": "Отменён",
}
PAYMENT_LABEL = {"pending": "Ожидает оплаты", "paid": "Оплачен", "refunded": "Возвращён"}

PAYMENT_METHODS = ["cash", "card_courier", "sbp", "online"]
PAYMENT_METHOD_LABEL = {
    "cash": "Наличными при получении",
    "card_courier": "Картой курьеру",
    "sbp": "СБП",
    "online": "Онлайн картой",
}
PAYMENT_METHOD_SHORT = {
    "cash": "наличные", "card_courier": "карта курьеру", "sbp": "СБП", "online": "онлайн",
}


def payment_label(m: Any, short: bool = False) -> str:
    table = PAYMENT_METHOD_SHORT if short else PAYMENT_METHOD_LABEL
    return table.get(m) or str(m or "—")


def is_prepaid(m: Any) -> bool:
    """Оплачено до получения — заказ ждёт подтверждения платежа."""
    return m in ("online", "sbp")


def supports_hold(m: Any) -> bool:
    """Блокировка суммы с запасом возможна только на карте (ТЗ 3.4).

    СБП — перевод, а не авторизация: блокировать «с запасом» нечего,
    поэтому по весовым позициям списывается расчётная сумма, а разница
    возвращается отдельным платежом после взвешивания.
    """
    return m == "online"


def next_status(s: Any) -> str | None:
    if s not in STATUS_FLOW:
        return None
    i = STATUS_FLOW.index(s)
    return None if i == len(STATUS_FLOW) - 1 else STATUS_FLOW[i + 1]


def allowed_transitions(order: Mapping[str, Any] | None) -> dict[str, Any]:
    """Куда заказ может перейти прямо сейчас (ТЗ 13).

    ``STATUS_FLOW`` линеен, но ``awaiting_payment`` относится только к
    предоплате. Для наличных и карты курьеру шаг пропускается: иначе
    сборщик не смог бы взять заказ в работу, не проставив ему
    бессмысленный статус «ожидает оплаты».

    Обратное правило важнее: предоплаченный заказ не двигается дальше,
    пока платёж не подтверждён, — иначе товар уезжает бесплатно.
    """
    if not order or order.get("status") == "cancelled":
        return {"allowed": [], "blockedReason": "order_cancelled"}
    if order.get("status") == "delivered":
        return {"allowed": [], "blockedReason": "order_delivered"}

    prepaid = is_prepaid(order.get("payment_method"))
    paid = order.get("payment_status") == "paid"
    status = order.get("status")
    if status not in STATUS_FLOW:
        return {"allowed": [], "blockedReason": "bad_status"}

    i = STATUS_FLOW.index(status)
    nxt = STATUS_FLOW[i + 1] if i + 1 < len(STATUS_FLOW) else None
    if nxt == "awaiting_payment" and not prepaid:
        nxt = STATUS_FLOW[i + 2] if i + 2 < len(STATUS_FLOW) else None

    if not nxt:
        return {"allowed": [], "blockedReason": None}
    if status == "awaiting_payment" and prepaid and not paid:
        return {"allowed": [], "blockedReason": "payment_not_confirmed"}
    return {"allowed": [nxt], "blockedReason": None}


def next_status_for(order: Mapping[str, Any] | None) -> str | None:
    allowed = allowed_transitions(order)["allowed"]
    return allowed[0] if allowed else None


PAYMENT_FLOW = {"pending": ["paid", "refunded"], "paid": ["refunded"], "refunded": []}


def can_change_payment(order: Mapping[str, Any] | None, to: str) -> bool:
    """Возврат — конечное состояние: «переоплатить» тот же заказ нельзя."""
    if not order:
        return False
    if order.get("status") == "cancelled" and to != "refunded":
        return False
    return to in PAYMENT_FLOW.get(order.get("payment_status"), [])


def customer_can_cancel(status: Any) -> bool:
    """Клиент отменяет сам только до сборки (ТЗ 4.6)."""
    return status in ("new", "awaiting_payment")
