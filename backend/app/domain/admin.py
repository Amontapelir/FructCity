"""Чистые функции админки: отчёты, проверки, представления.

Как и остальной домен — без базы и HTTP: принимают срез состояния,
отдают структуру. Поэтому агрегации дашборда и виды сущностей
проверяются прямым вызовом, без поднятого приложения и без базы.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from . import calc as C
from . import security as sec
from .auth import parse_iso
from .catalog import public_product
from .validate import SCHEMAS, ValidationError
from .validate import _to_number as _loose_number   # тот же разбор "1,5" → 1.5, что и в схемах
from .validate import validate as validate_schema

__all__ = [
    "admin_product", "category_out", "zone_out", "product_sanity", "promo_sanity",
    "replace_product_images",
    "order_closed_for_edits", "packing_list_html",
    "revenue_by_period", "daily_revenue", "orders_by_status", "orders_by_slot",
    "CSV_COLUMNS", "csv_cell", "parse_csv", "import_products", "IMPORT_MODES",
]


# ---------------------------------------------------------------------------
# Товары (ТЗ 10.1)
# ---------------------------------------------------------------------------
def admin_product(p: Mapping[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Вид товара для админки.

    Отличается от витринного: показываем ЗАПИСАННУЮ акционную цену, даже
    если срок акции истёк, — иначе поле в форме окажется пустым и
    администратор не сможет ни увидеть, ни исправить старую акцию.
    """
    view = public_product(p, now)
    view.update({
        "is_active": p.get("is_active"),
        "sale_price": p.get("sale_price") or None,
        "sale_until": p.get("sale_until") or None,
        # Отдельно от объединённой витринной `image_keys` (обложка +
        # доп. фото) — форме редактирования нужен именно список ДОП.
        # фото, без обложки, чтобы не путать его с полем `image_key`.
        "extra_images": list(p.get("extra_images") or []),
        "sale_expired": bool(p.get("sale_price") and p.get("sale_until")
                             and not C.is_sale(p, now)),
    })
    return view


# ---------------------------------------------------------------------------
# Категории (ТЗ 10.2)
# ---------------------------------------------------------------------------
def category_out(c: Mapping[str, Any]) -> dict[str, Any]:
    """Категория в виде, который ждёт админка, — не строка таблицы как есть.

    В исходном `store.json` ключ `is_system` есть только у одной
    категории («Акции» из сида) — у остальных тринадцати его в объекте
    нет вовсе (`migrate_json.py` прямо об этом предупреждает: «is_system
    без значения — это False»). В SQL у столбца `is_system` есть
    `DEFAULT false`, и он присутствует у КАЖДОЙ строки — без этой
    функции админка отдавала бы `"is_system": false` там, где ключа
    не было вовсе. Разница не косметическая: по наличию ключа админка
    решает, можно ли категорию удалять, и системной внезапно оказалась
    бы каждая. Дефект найден дифференциальной сверкой ответов во время
    переезда; регресс — `test_admin_views.py`.
    """
    out = dict(c)
    if not out.get("is_system"):
        out.pop("is_system", None)
    return out


# ---------------------------------------------------------------------------
# Доставка (ТЗ 10.6)
# ---------------------------------------------------------------------------
def zone_out(z: Mapping[str, Any]) -> dict[str, Any]:
    """Зона доставки для админки — тот же приём, что и `category_out`.

    В `store.json` ключ `manual_quote` есть только у зоны «Другой
    район» (`true`), у остальных его нет вовсе; в SQL у столбца
    `DEFAULT false`, и он есть у КАЖДОЙ строки. Лишний `false` здесь не
    мелочь: он говорит «стоимость считается вручную» про зону, где она
    считается обычным образом.

    Публичный `GET /api/zones` (`routers/cart.py`), наоборот,
    нормализует явным `bool(...)` — там ключ нужен ВСЕГДА, фронту негде
    взять значение по умолчанию. Дефект найден дифференциальной
    сверкой ответов во время переезда; регресс — `test_admin_views.py`.
    """
    out = dict(z)
    if not out.get("manual_quote"):
        out.pop("manual_quote", None)
    return out


def product_sanity(input: Mapping[str, Any]) -> dict[str, str] | None:
    """Цена должна быть в том поле, которое соответствует типу товара."""
    errs: dict[str, str] = {}
    price = input.get("price") if input.get("type") == "unit" else input.get("price_per_kg")
    price_n = C.num(price)
    if not (price_n > 0):
        errs["price" if input.get("type") == "unit" else "price_per_kg"] = \
            "цена должна быть больше нуля"
    sale = input.get("sale_price")
    if sale is not None and C.num(sale) > 0 and price_n > 0 and C.num(sale) >= price_n:
        errs["sale_price"] = "акционная цена должна быть ниже базовой"
    return errs or None


def replace_product_images(images: Sequence[Mapping[str, Any]], *, product_id: int,
                           keys: Sequence[str],
                           next_id: Callable[[str], int]) -> list[dict[str, Any]]:
    """Полная замена дополнительных фото товара (ROADMAP 2.11).

    Обложка остаётся в `products.image_key` — эта функция про ОСТАЛЬНЫЕ,
    из отдельной таблицы `product_images`. Список заменяется целиком, как
    и `settings.holidays`: администратор видит порядок и правит его весь
    сразу, а не по одной строке. `db/uow.py` пишет разницу снимков сам —
    здесь только пересобрать плоский список: строки этого товара долой,
    новые — по порядку `keys`.
    """
    kept = [row for row in images if row.get("product_id") != product_id]
    added = [{"id": next_id("product_images"), "product_id": product_id,
             "image_key": key, "sort_order": i}
            for i, key in enumerate(keys)]
    return kept + added


# ---------------------------------------------------------------------------
# Промокоды (ТЗ 10.5)
# ---------------------------------------------------------------------------
def promo_sanity(input: Mapping[str, Any]) -> dict[str, str] | None:
    """Процентные типы (в том числе скидка на доставку) не превышают 100."""
    if input.get("type") in ("percent", "delivery") and C.num(input.get("value")) > 100:
        return {"value": "процент не может быть больше 100"}
    return None


# ---------------------------------------------------------------------------
# Заказы (ТЗ 10.3)
# ---------------------------------------------------------------------------
def order_closed_for_edits(o: Mapping[str, Any]) -> str | None:
    """Можно ли ещё править состав заказа.

    Отменённый — остатки уже возвращены, повторная правка задвоила бы
    склад. Доставленный — деньги получены и чек пробит: менять сумму
    задним числом значит расходиться с фискальными данными.
    """
    if o.get("status") == "cancelled":
        return "order_cancelled"
    if o.get("status") == "delivered":
        return "order_delivered"
    return None


# ---------------------------------------------------------------------------
# Дашборд (ТЗ 10.9)
# ---------------------------------------------------------------------------
def _day_of(o: Mapping[str, Any], now: datetime | None) -> str:
    return C.msk_parts(parse_iso(o.get("created_at")) or now).ymd


def revenue_by_period(orders: Sequence[Mapping[str, Any]],
                      now: datetime | None = None) -> dict[str, Any]:
    """Выручка за день, неделю и месяц.

    Границы считаются по московскому календарю. Для каждого периода
    отдаём и предыдущий отрезок — без него цифра не читается, непонятно,
    много это или мало.
    """
    today = C.msk_parts(now).ymd

    def sum_between(from_ymd: str, to_ymd: str) -> dict[str, Any]:
        total = 0.0
        count = 0
        for o in orders:
            d = _day_of(o, now)
            if from_ymd <= d <= to_ymd:
                total += C.num(o.get("total"))
                count += 1
        avg = C.money(total / count) if count else 0
        return {"sum": C.js_number(total), "count": count, "avg": avg}

    spans = {"day": 1, "week": 7, "month": 30}
    out: dict[str, Any] = {}
    for name, days in spans.items():
        frm = C.add_days_ymd(today, -(days - 1))
        prev_to = C.add_days_ymd(frm, -1)
        prev_from = C.add_days_ymd(prev_to, -(days - 1))
        cur = sum_between(frm, today)
        prev = sum_between(prev_from, prev_to)
        # рост в процентах; при нулевой базе процент не считаем — он был
        # бы бесконечным и только вводил в заблуждение
        delta_pct = (C.money((cur["sum"] - prev["sum"]) / prev["sum"] * 100)
                    if prev["sum"] > 0 else None)
        out[name] = {**cur, "from": frm, "to": today,
                     "prev_sum": prev["sum"], "delta_pct": delta_pct}
    return out


def daily_revenue(orders: Sequence[Mapping[str, Any]], days: int = 30,
                  now: datetime | None = None) -> list[dict[str, Any]]:
    """Выручка по дням за последние N суток — ряд для графика.

    Дни без заказов отдаём нулями, а не пропускаем: иначе линия
    соединит две далёкие даты и покажет плавный рост там, где на самом
    деле была пауза.
    """
    today = C.msk_parts(now).ymd
    bucket: dict[str, dict[str, Any]] = {}
    for i in range(days - 1, -1, -1):
        bucket[C.add_days_ymd(today, -i)] = {"sum": 0.0, "count": 0}
    for o in orders:
        ymd = _day_of(o, now)
        b = bucket.get(ymd)
        if b is not None:
            b["sum"] += C.num(o.get("total"))
            b["count"] += 1
    return [{"ymd": ymd, "sum": C.js_number(v["sum"]), "count": v["count"]}
            for ymd, v in bucket.items()]


def orders_by_status(orders: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Сколько заказов в каждом статусе. Отменённые тоже считаем — на
    дашборде важно видеть их долю, а не прятать."""
    count: dict[Any, int] = {}
    for o in orders:
        s = o.get("status")
        count[s] = count.get(s, 0) + 1
    # Порядок — как в жизни заказа, отменённые в конец. Незнакомые
    # статусы не выбрасываем, а дописываем в конец: иначе заказ с
    # испорченным статусом исчезал бы из подсчёта незаметно.
    known = list(C.STATUS_FLOW) + ["cancelled"]
    extra = sorted(s for s in count if s not in known)
    order = known + extra
    return [{"status": s, "label": C.STATUS_LABEL.get(s, s), "count": count[s]}
            for s in order if s in count]


# ---------------------------------------------------------------------------
# Упаковочный лист (ТЗ 10.3) — печатная форма для сборщика
# ---------------------------------------------------------------------------
def _ru_datetime(iso: Any) -> str:
    """Дата и время в привычном виде: 02.09.2026, 16:30:33.

    Формат унаследован от прежней версии (`toLocaleString('ru-RU')`) и
    сознательно не сверяется ни с чем побайтово: он зависит от часового
    пояса машины, а лист печатает сборщик на месте и в секунды не
    смотрит. Денег здесь не считается — то, что обязано совпадать
    точно, живёт в `calc.py`.
    """
    dt = parse_iso(iso)
    if dt is None:
        return str(iso or "")
    return dt.strftime("%d.%m.%Y, %H:%M:%S")


def packing_list_html(state: Mapping[str, Any], order: Mapping[str, Any]) -> str:
    """Готовый HTML, а не JSON: лист печатают, а не отображают в
    интерфейсе. Всё пользовательское экранируется — имя и комментарий
    клиента попадают прямо в разметку."""
    e = sec.escape_html
    items = [i for i in state.get("order_items") or [] if i.get("order_id") == order.get("id")]
    zone = next((z for z in state.get("delivery_zones") or []
                if z.get("id") == order.get("delivery_zone_id")), None)

    rows = []
    for i in items:
        want = (f"{i.get('requested_quantity')} шт" if i.get("type") == "unit"
                else f"~{i.get('requested_weight')} кг")
        # для весовых оставляем пустую графу — сборщик вписывает факт от руки
        fact_cell = "—" if i.get("type") == "unit" else '<span class="fill"></span>'
        cls = ' class="removed"' if i.get("is_removed") else ""
        unit_label = "шт" if i.get("type") == "unit" else "кг"
        rows.append(
            f'<tr{cls}>'
            f'<td class="chk"></td>'
            f'<td><b>{e(i.get("name"))}</b><br><span class="sku">{e(i.get("sku"))}</span></td>'
            f'<td class="num">{e(want)}</td>'
            f'<td class="num">{fact_cell}</td>'
            f'<td class="num">{e(str(i.get("price_at_purchase")))} ₽/{unit_label}</td>'
            f'</tr>'
        )

    has_weighted = any(i.get("type") == "weighted" for i in items)
    number = e(str(order.get("number")))

    if order.get("method") == "delivery":
        where = ("Доставка" + (" · " + e(zone.get("name")) if zone else "")
                + " · " + e(order.get("address") or ""))
    else:
        where = "Самовывоз · " + e((state.get("settings") or {}).get("pickup_address") or "")

    hold = order.get("hold_amount")
    payment_line = ("Оплата: " + e(C.payment_label(order.get("payment_method")))
                    + (f" · холд {e(str(hold))} ₽" if hold else ""))

    weighted_note = ('<div class="note"><b>Весовые позиции:</b> взвесить и вписать '
                     'фактический вес. Отклонение больше ±10% — согласовать с клиентом '
                     'до отправки.</div>') if has_weighted else ""
    comment_note = (f'<div class="note"><b>Комментарий клиента:</b> {e(order.get("comment"))}</div>'
                    if order.get("comment") else "")

    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8">
<title>Лист сборки заказа №{number}</title>
<style>
  @page {{ margin: 12mm; }}
  body {{ font: 13px/1.45 -apple-system, "Segoe UI", Roboto, Arial, sans-serif; color: #111; margin: 0; }}
  h1 {{ font-size: 19px; margin: 0 0 2px; }}
  .sub {{ color: #555; font-size: 12px; margin-bottom: 12px; }}
  .box {{ border: 1px solid #bbb; padding: 9px 11px; margin-bottom: 12px; }}
  .box div {{ padding: 2px 0; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 6px; }}
  th {{ text-align: left; font-size: 10px; text-transform: uppercase; letter-spacing: .04em;
       color: #555; border-bottom: 1.5px solid #333; padding: 5px 6px; }}
  td {{ border-bottom: 1px solid #ddd; padding: 8px 6px; vertical-align: top; }}
  td.num {{ text-align: right; white-space: nowrap; }}
  td.chk {{ width: 22px; }}
  td.chk::before {{ content: ""; display: block; width: 13px; height: 13px; border: 1.5px solid #333; }}
  .fill {{ display: inline-block; width: 62px; border-bottom: 1px solid #333; height: 15px; }}
  .sku {{ font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 10.5px; color: #777; }}
  tr.removed td {{ opacity: .45; text-decoration: line-through; }}
  .note {{ border-left: 3px solid #333; padding: 7px 10px; background: #f4f4f2; font-size: 12px; margin-top: 12px; }}
  .sign {{ margin-top: 26px; display: flex; gap: 34px; font-size: 12px; color: #555; }}
  .sign span {{ flex: 1; border-top: 1px solid #333; padding-top: 4px; }}
  @media print {{ .noprint {{ display: none; }} }}
</style></head><body>
<script src="/print.js" defer></script>
<button class="noprint" data-print="1"
  style="float:right;padding:7px 14px;cursor:pointer">Печать</button>
<h1>Лист сборки — заказ №{number}</h1>
<div class="sub">Оформлен {e(_ru_datetime(order.get("created_at")))} ·
  статус: {e(C.STATUS_LABEL.get(order.get("status"), order.get("status")))}</div>

<div class="box">
  <div><b>{e(order.get("name"))}</b> · {e(order.get("phone"))}</div>
  <div>{where}</div>
  <div>Интервал: {e(order.get("slot_ymd"))}, {e(str(order.get("slot_from")))}:00–{e(str(order.get("slot_to")))}:00</div>
  <div>{payment_line}</div>
</div>

<table>
  <thead><tr><th></th><th>Товар</th><th style="text-align:right">Заказано</th>
    <th style="text-align:right">Факт</th><th style="text-align:right">Цена</th></tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>

{weighted_note}
{comment_note}

<div class="sign"><span>Собрал</span><span>Проверил</span><span>Дата и время</span></div>
</body></html>"""


def orders_by_slot(orders: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Загрузка интервалов доставки: сколько заказов на какой слот."""
    count: dict[float, int] = {}
    for o in orders:
        if o.get("slot_from") is None or o.get("slot_to") is None:
            continue
        key = C.num(o.get("slot_from"))
        count[key] = count.get(key, 0) + 1
    out = []
    for frm in sorted(count):
        frm_n = C.js_number(frm)
        out.append({"from": frm_n, "to": C.js_number(frm + 2),
                    "label": f"{frm_n}:00", "count": count[frm]})
    return out


# ---------------------------------------------------------------------------
# CSV импорт/экспорт (ТЗ 10.7)
# ---------------------------------------------------------------------------
CSV_COLUMNS = ("sku", "name", "category", "type", "price", "sale_price", "stock", "vat", "active")

_CSV_LEADING = re.compile(r"^[=+\-@\t\r]")
_CSV_NEEDS_QUOTES = re.compile(r'[;"\n\r]')


def _js_str(v: Any) -> str:
    """`String(v)` из JS для значений, которые уходят в ячейку CSV."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(int(v)) if float(v).is_integer() else str(v)
    return str(v)


def csv_cell(v: Any) -> str:
    """Экранирование ячейки + защита от CSV-инъекции в Excel."""
    s = _js_str(v)
    # формула, начинающаяся с = + - @, выполнится при открытии файла
    if _CSV_LEADING.match(s):
        s = "'" + s
    if _CSV_NEEDS_QUOTES.search(s):
        s = '"' + s.replace('"', '""') + '"'
    return s


def parse_csv(text: Any) -> dict[str, Any]:
    """Разбор CSV с учётом кавычек и переводов строк внутри полей."""
    src = re.sub(r"^﻿", "", str(text), count=1)
    rows: list[list[str]] = []
    field = ""
    row: list[str] = []
    in_quotes = False
    i = 0
    n = len(src)
    while i < n:
        ch = src[i]
        if in_quotes:
            if ch == '"':
                if i + 1 < n and src[i + 1] == '"':
                    field += '"'
                    i += 1
                else:
                    in_quotes = False
            else:
                field += ch
        elif ch == '"':
            in_quotes = True
        elif ch == ";":
            row.append(field)
            field = ""
        elif ch == "\n":
            row.append(field)
            rows.append(row)
            row = []
            field = ""
        elif ch != "\r":
            field += ch
        i += 1
    if field != "" or row:
        row.append(field)
        rows.append(row)
    if in_quotes:
        return {"error": "csv_unclosed_quote"}
    if not rows:
        return {"error": "csv_empty"}

    header = [h.strip().lower() for h in rows[0]]
    for col in CSV_COLUMNS:
        if col not in header:
            return {"error": "csv_missing_column", "line": col}
    idx = {c: header.index(c) for c in CSV_COLUMNS}

    out = []
    for r in rows[1:]:
        if all(c.strip() == "" for c in r):
            continue
        o = {c: (r[idx[c]] if idx[c] < len(r) else "").strip() for c in CSV_COLUMNS}
        out.append(o)
    return {"rows": out}


def _strict_number(v: Any) -> float:
    """`Number(v)` из JS: вся строка целиком должна быть числом,
    запятая не заменяется точкой (в отличие от `_loose_number`,
    которым разбираются цена, акция и остаток)."""
    s = str(v).strip()
    if s == "":
        return 0.0
    try:
        return float(s)
    except ValueError:
        return math.nan


IMPORT_MODES = ("full", "prices_stock", "new_only")


def _import_prices_stock(row: Mapping[str, str], exist: dict[str, Any] | None,
                         now_iso: Callable[[], str]) -> dict[str, Any]:
    """Режим «цены и остатки» (ТЗ 10.7): только цена/акция/остаток/НДС
    у СУЩЕСТВУЮЩЕГО товара. Карточку (название, категория, тип) не
    трогает и новых товаров не создаёт — для этого есть `new_only`."""
    if exist is None:
        raise ValueError("товар с таким SKU не найден — режим не создаёт новые")

    price = _loose_number(row.get("price"))
    if not math.isfinite(price) or price <= 0:
        raise ValueError("некорректная цена")
    sale_raw = row.get("sale_price")
    sale = None if sale_raw == "" else _loose_number(sale_raw)
    if sale is not None and (not math.isfinite(sale) or sale <= 0 or sale >= price):
        raise ValueError("некорректная акционная цена")
    stock = _loose_number(row.get("stock"))
    if not math.isfinite(stock) or stock < 0:
        raise ValueError("некорректный остаток")
    vat = _strict_number(row.get("vat"))
    if vat not in (0, 10, 20):
        raise ValueError("НДС может быть 0, 10 или 20")

    is_weighted = exist.get("type") != "unit"
    exist.update({
        "price": 0 if is_weighted else price,
        "price_per_kg": price if is_weighted else 0,
        "sale_price": sale, "stock": stock,
        "vat_rate": int(vat), "updated_at": now_iso(),
    })
    return {"updated": True}


def import_products(state: dict[str, Any], *, next_id: Callable[[str], int],
                    now_iso: Callable[[], str],
                    rows: Sequence[Mapping[str, str]],
                    mode: str = "full") -> dict[str, Any]:
    """Импорт строк CSV в каталог (ТЗ 10.7). Одна битая строка не должна
    валить весь импорт — отказ по строке уходит в `skipped`.

    Три режима сопоставления по SKU (`IMPORT_MODES`):

    - ``full`` — есть SKU: карточка обновляется целиком; нет — создаётся.
      Прежнее (и единственное до этой задачи) поведение.
    - ``prices_stock`` — только цена/акция/остаток/НДС у существующих;
      новые товары не создаются (см. `_import_prices_stock`).
    - ``new_only`` — только добавление; существующий SKU не трогается —
      уходит в `skipped` с понятной причиной, это не ошибка формата.
    """
    if mode not in IMPORT_MODES:
        raise ValueError(f"неизвестный режим импорта: {mode}")

    cat_ids = {c.get("id") for c in state.get("categories") or []}
    result: dict[str, Any] = {"created": 0, "updated": 0, "skipped": []}

    for row in rows:
        sku_for_error = row.get("sku") or "(без SKU)"
        try:
            exist = next((p for p in state.get("products") or []
                         if str(p.get("sku", "")).lower() == str(row.get("sku") or "").lower()),
                        None)

            if mode == "prices_stock":
                _import_prices_stock(row, exist, now_iso)
                result["updated"] += 1
                continue

            if mode == "new_only" and exist is not None:
                raise ValueError("уже есть в каталоге — режим добавляет только новые")

            rtype = row.get("type") if row.get("type") in ("unit", "weighted", "preorder") else None
            if not rtype:
                raise ValueError("неизвестный тип")
            if row.get("category") not in cat_ids:
                raise ValueError("нет такой категории")

            price = _loose_number(row.get("price"))
            if not math.isfinite(price) or price <= 0:
                raise ValueError("некорректная цена")

            sale_raw = row.get("sale_price")
            sale = None if sale_raw == "" else _loose_number(sale_raw)
            if sale is not None and (not math.isfinite(sale) or sale <= 0 or sale >= price):
                raise ValueError("некорректная акционная цена")

            stock = _loose_number(row.get("stock"))
            if not math.isfinite(stock) or stock < 0:
                raise ValueError("некорректный остаток")

            vat = _strict_number(row.get("vat"))
            if vat not in (0, 10, 20):
                raise ValueError("НДС может быть 0, 10 или 20")

            data = validate_schema(SCHEMAS["product"], {
                "sku": row.get("sku"), "name": row.get("name"),
                "category_id": row.get("category"), "type": rtype,
                "price": price if rtype == "unit" else 0,
                "price_per_kg": 0 if rtype == "unit" else price,
                "sale_price": sale, "vat_rate": str(int(vat)), "stock": stock,
                "is_active": row.get("active") in ("1", "true"),
                "slug": "", "image_key": "", "emoji": "", "description": "",
            })

            # full (и создание в new_only) — сопоставление по SKU:
            # есть — обновляем, нет — создаём.
            now = now_iso()
            if exist is not None:
                exist.update({
                    "name": data["name"], "category_id": data["category_id"],
                    "type": data["type"], "price": data["price"],
                    "price_per_kg": data["price_per_kg"], "sale_price": data["sale_price"],
                    "vat_rate": int(vat), "stock": data["stock"],
                    "is_active": data["is_active"], "updated_at": now,
                })
                result["updated"] += 1
            else:
                state.setdefault("products", []).append({
                    **data, "id": next_id("products"), "vat_rate": int(vat),
                    "slug": C.slugify(data["name"]) + "-" + data["sku"].lower(),
                    "emoji": "📦", "image_key": None, "description": "",
                    "min_weight": 0.5, "weight_step": 0.5,
                    "created_at": now, "updated_at": now,
                })
                result["created"] += 1
        except ValidationError as e:
            msg = "; ".join(f"{k}: {v}" for k, v in e.fields.items())
            result["skipped"].append({"sku": sku_for_error, "reason": msg})
        except ValueError as e:
            result["skipped"].append({"sku": sku_for_error, "reason": str(e)})

    return result
