"""Формат чисел в ответе витрине: целое остаётся целым.

Проверка идёт по ТЕКСТУ ответа, а не по разобранным значениям: любое
сравнение, которое сначала приводит числа к общему виду, разницы между
`99` и `99.0` не увидит. А она есть: `num()` в домене всегда возвращает
`float`, и без `js_number` цены уезжали бы клиенту дробными — «99.0 ₽»
в карточке товара.

Витрина на JavaScript разберёт оба варианта одинаково, поэтому дефект
не падает, а просто некрасиво показывается. Проверка нужна именно
поэтому: сам по себе он не проявится.
"""

from __future__ import annotations

import json
import unittest

from backend.app.domain import catalog as K
from backend.tests.paths import STORE

MONEY_FIELDS = ("price", "price_per_kg", "unit_price", "base_price",
                "sale_price", "stock", "vat_rate")


def load_state() -> dict:
    return json.loads(STORE.read_text(encoding="utf-8"))


@unittest.skipUnless(STORE.exists(), "нет data/store.json")
class WholeNumbersStayWhole(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.state = load_state()

    def test_product_card_has_no_fractional_integers(self):
        """Цена 99 ₽ — это `99`, а не `99.0`."""
        products = self.state.get("products") or []
        self.assertTrue(products, "в хранилище нет товаров")
        for source in products:
            card = K.public_product(source)
            for field in MONEY_FIELDS:
                value = card.get(field)
                if isinstance(value, float):
                    with self.subTest(sku=source.get("sku"), field=field):
                        self.assertFalse(
                            value.is_integer(),
                            f"{source.get('sku')}.{field} = {value} — целое стало дробным")

    def test_price_range_is_whole(self):
        rng = K.price_range(self.state)
        for key, value in rng.items():
            if isinstance(value, float):
                with self.subTest(key=key):
                    self.assertFalse(value.is_integer(),
                                     f"price_range.{key} = {value} — целое стало дробным")

    def test_serialised_card_shows_no_trailing_zero(self):
        """Проверка на самом тексте: именно его увидит браузер."""
        products = self.state.get("products") or []
        for source in products[:5]:
            card = K.public_product(source)
            # только денежные поля: `min_weight` и шаг веса дробные по
            # смыслу (0.5 кг), и искать в них ".0" значило бы ловить
            # исходные данные, а не ошибку сериализации
            money = {k: card[k] for k in MONEY_FIELDS if k in card}
            text = json.dumps(money, ensure_ascii=False)
            with self.subTest(sku=source.get("sku")):
                self.assertNotIn(".0,", text, "в тексте ответа целое напечатано дробным")
                self.assertNotIn(".0}", text, "в тексте ответа целое напечатано дробным")


if __name__ == "__main__":
    unittest.main()
