"""Сервер и браузер считают деньги одинаково (инвариант 1).

Расчётное ядро существует в двух видах: `domain/calc.py` считает на
сервере, `lib/calc.js` — в браузере (его грузят `index.html` и
`admin.html`). Дублирование вынужденное: витрина показывает сумму до
отправки заказа, и сделать это она может только своим кодом.

Опасность ровно одна и она тихая. Формулы расходятся — покупатель видит
в корзине одну сумму, а в подтверждении заказа другую. Ничего не падает,
в журнале пусто; узнаём от покупателя.

`node` здесь — инструмент, а не сервер: единственный способ выполнить
`calc.js` тем же движком, каким его исполняет браузер, и сравнить
результат с питоновской реализацией функция за функцией.

**Пропуск здесь — это «не проверено».** Если `node` не установлен, набор
молчит, и расхождение формул никто не заметит. В CI node ставится
специально.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from datetime import datetime, timezone

from backend.app.domain import calc as C
from backend.tests.paths import LIB, ROOT

NODE = shutil.which("node") if (LIB / "calc.js").exists() else None
PROBE = str((ROOT / "backend" / "tests" / "_js_calc_probe.js"))

NOW = datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)
# Мост превращает такую пометку в `new Date(...)`: `mskParts` в calc.js
# зовёт `Intl.DateTimeFormat.formatToParts`, а тот принимает Date, не строку.
NOW_ARG = {"__date": "2026-08-18T09:00:00.000Z"}

UNIT = {"id": 1, "sku": "A-1", "type": "unit", "price": 99, "price_per_kg": 0,
        "sale_price": None, "sale_until": None, "stock": 5}
WEIGHTED = {"id": 2, "sku": "A-2", "type": "weighted", "price": 0, "price_per_kg": 149.5,
            "sale_price": 109, "sale_until": "2099-01-01", "stock": 3,
            "min_weight": 0.5, "weight_step": 0.5}
EXPIRED_SALE = {**UNIT, "sale_price": 50, "sale_until": "2020-01-01"}


def js(cases: list[dict]) -> list[dict]:
    proc = subprocess.run(
        [NODE, PROBE], input=json.dumps(cases, ensure_ascii=False),
        capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT))
    if proc.returncode != 0:
        raise AssertionError("мост к calc.js упал: " + (proc.stderr or "")[:800])
    return json.loads(proc.stdout)


def norm(v):
    """Приводит к сравнимому виду то, что в двух языках печатается по-разному.

    JSON из JS не различает 2 и 2.0; `null` и `undefined` после
    сериализации оба становятся `null`. Сравнивать надо смысл.
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return round(float(v), 9)
    if isinstance(v, dict):
        return {k: norm(x) for k, x in v.items() if x is not None}
    if isinstance(v, (list, tuple)):
        return [norm(x) for x in v]
    return v


@unittest.skipUnless(NODE, "node не найден — сверка формул НЕ ВЫПОЛНЕНА")
class CalcMatchesBrowser(unittest.TestCase):

    def compare(self, cases: list[tuple[str, list, object]]) -> None:
        got = js([{"op": op, "args": args} for op, args, _ in cases])
        self.assertEqual(len(got), len(cases), "мост вернул другое число ответов")
        for (op, args, mine), theirs in zip(cases, got):
            with self.subTest(op=op, args=args):
                self.assertNotIn("error", theirs, f"{op}: {theirs.get('error')}")
                self.assertEqual(
                    norm(mine), norm(theirs["value"]),
                    f"{op}{args}: сервер и браузер посчитали по-разному")

    # -- деньги и цены ------------------------------------------------------
    def test_rounding_matches(self):
        """`Math.round` округляет .5 вверх, `round()` в Python — к чётному.
        Это не мелочь: на 2.5 они дают 3 и 2 соответственно."""
        values = [0, 1, 1.4, 1.5, 2.5, 3.5, -1.5, 99.49, 99.5, 1234.567]
        self.compare([("money", [v], C.money(v)) for v in values])

    def test_num_coercion_matches(self):
        """`num()` превращает мусор в 0 — но по правилам JS, а не Python."""
        values = ["12", "12.5", "12,5", "", "нет", None, True, "0x10", " 7 "]
        self.compare([("num", [v], C.num(v)) for v in values])

    def test_prices_match(self):
        cases = []
        for p in (UNIT, WEIGHTED, EXPIRED_SALE):
            cases.append(("basePrice", [p], C.base_price(p)))
            cases.append(("unitPrice", [p, NOW_ARG], C.unit_price(p, NOW)))
            cases.append(("isSale", [p, NOW_ARG], C.is_sale(p, NOW)))
            cases.append(("inStock", [p], C.in_stock(p)))
        self.compare(cases)

    def test_line_total_matches(self):
        """Строка корзины: штучный по количеству, весовой по весу.

        Дробное количество отбрасывается (`Math.trunc`), вес — нет; на
        этом месте два языка расходятся особенно охотно.
        """
        cases = []
        for qty in (1, 2, 3, 2.7, 0, -1):
            item = {"qty": qty, "weight": None}
            cases.append(("lineTotal", [UNIT, item, NOW_ARG], C.line_total(UNIT, item, NOW)))
        for weight in (0.5, 1.5, 2.25, 0):
            item = {"qty": None, "weight": weight}
            cases.append(("lineTotal", [WEIGHTED, item, NOW_ARG],
                          C.line_total(WEIGHTED, item, NOW)))
        self.compare(cases)

    def test_normalize_item_matches(self):
        """Шаг веса и минимум — то, что видит покупатель в корзине."""
        cases = []
        for raw in ({"weight": 0.3}, {"weight": 0.7}, {"weight": 1.24}, {"weight": 0}):
            cases.append(("normalizeItem", [WEIGHTED, raw],
                          C.normalize_item(WEIGHTED, raw)))
        for raw in ({"qty": 0}, {"qty": 1}, {"qty": 2.9}, {"qty": -5}):
            cases.append(("normalizeItem", [UNIT, raw], C.normalize_item(UNIT, raw)))
        self.compare(cases)

    # -- вес и холд ---------------------------------------------------------
    def test_actual_weight_check_matches(self):
        """Граница ±10% — та, по которой сборщика отправляют звонить
        клиенту. Разъедься она на 0.1% — звонок либо не случится, либо
        случится зря."""
        pairs = [(1, 1), (1, 1.1), (1, 1.100001), (1, 0.9), (1, 0.89),
                 (1.5, 1.6), (2, 2.2), (0, 1)]
        self.compare([("checkActualWeight", [req, act], C.check_actual_weight(req, act))
                      for req, act in pairs])

    def test_hold_amount_matches(self):
        self.compare([("holdAmount", [v], C.hold_amount(v))
                      for v in (0, 1, 999, 1000.5, 1234)])

    # -- календарь ----------------------------------------------------------
    def test_date_expired_matches(self):
        """Срок включительный: «до 20 августа» действует весь день 20-го."""
        dates = ["2026-08-17", "2026-08-18", "2026-08-19", "", None]
        self.compare([("dateExpired", [d, NOW_ARG], C.date_expired(d, NOW)) for d in dates])

    def test_weekday_and_days_math_match(self):
        cases = [("addDaysYmd", ["2026-08-18", n], C.add_days_ymd("2026-08-18", n))
                 for n in (0, 1, 14, 400)]
        cases += [("weekdayOf", [d], C.weekday_of(d))
                  for d in ("2026-08-18", "2026-01-01", "2026-12-31")]
        self.compare(cases)

    # -- поиск и статусы ----------------------------------------------------
    def test_search_helpers_match(self):
        words = ["Яблоко", "ЯБЛОКО", "  яблоко  ", "ёлка", "елка", "Grenny Smith"]
        cases = [("normalizeQuery", [w], C.normalize_query(w)) for w in words]
        cases += [("slugify", [w], C.slugify(w)) for w in words]
        self.compare(cases)

    def test_status_flow_matches(self):
        cases = [("STATUS_FLOW", [], C.STATUS_FLOW),
                 ("PAYMENT_FLOW", [], C.PAYMENT_FLOW),
                 ("PAYMENT_METHODS", [], C.PAYMENT_METHODS)]
        for method in ("cash", "card_courier", "sbp", "online"):
            cases.append(("isPrepaid", [method], C.is_prepaid(method)))
            cases.append(("paymentLabel", [method], C.payment_label(method)))
        for status in C.STATUS_FLOW:
            cases.append(("customerCanCancel", [status], C.customer_can_cancel(status)))
        self.compare(cases)

    def test_allowed_transitions_match(self):
        orders = [
            {"status": "new", "payment_method": "cash", "payment_status": "pending"},
            {"status": "new", "payment_method": "online", "payment_status": "pending"},
            {"status": "new", "payment_method": "online", "payment_status": "paid"},
            {"status": "assembling", "payment_method": "sbp", "payment_status": "paid"},
            {"status": "delivered", "payment_method": "cash", "payment_status": "paid"},
        ]
        self.compare([("allowedTransitions", [o], C.allowed_transitions(o)) for o in orders])


if __name__ == "__main__":
    unittest.main()
