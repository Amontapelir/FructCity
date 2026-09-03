"""Представления и проверки админки (`domain/admin.py`) — прямые тесты.

Две проверки здесь закрывают настоящие дефекты, найденные на живых
ответах: лишние `is_system` у категории и `manual_quote` у зоны
доставки.

Почему это важно и без Node. В SQL у обоих столбцов `DEFAULT false`,
поэтому строка из базы ВСЕГДА несёт ключ. В исходных данных его почти
ни у кого нет. Витрина и админка читают эти поля как «есть/нет», и
лишний `false` — не косметика: он говорит «зона считается вручную» там,
где это неправда.
"""

from __future__ import annotations

import unittest
from datetime import datetime

from backend.app.domain import admin as AD

NOW = "2026-08-18T09:00:00Z"


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class AdminProductAndSanity(unittest.TestCase):
    """Вид товара для админки и проверки вводимых значений."""

    def test_admin_product_keeps_expired_sale_visible(self):
        p = {"id": 1, "sku": "A", "slug": "a", "name": "Товар", "type": "unit",
            "price": 200, "sale_price": 150, "sale_until": "2020-01-01",
            "stock": 5, "vat_rate": "10", "min_weight": 0, "weight_step": 0,
            "image_key": None, "emoji": "🍎", "description": "", "is_active": True}
        view = AD.admin_product(p, ts(NOW))
        self.assertEqual(view["sale_price"], 150, "истёкшая акция должна остаться видна админу")
        self.assertTrue(view["sale_expired"])
        self.assertFalse(view["is_sale"], "на витрину акция уже не действует")

    def test_product_sanity_rejects_zero_price(self):
        bad = AD.product_sanity({"type": "unit", "price": 0, "price_per_kg": 0})
        self.assertIn("price", bad)

    def test_product_sanity_rejects_sale_above_base(self):
        bad = AD.product_sanity({"type": "unit", "price": 100, "price_per_kg": 0,
                                 "sale_price": 150})
        self.assertIn("sale_price", bad)

    def test_product_sanity_ok(self):
        self.assertIsNone(AD.product_sanity({"type": "unit", "price": 100, "price_per_kg": 0,
                                             "sale_price": 50}))

    def test_promo_sanity_caps_percent_at_100(self):
        bad = AD.promo_sanity({"type": "percent", "value": 150})
        self.assertIn("value", bad)
        self.assertIsNone(AD.promo_sanity({"type": "percent", "value": 50}))
        self.assertIsNone(AD.promo_sanity({"type": "fixed", "value": 999}))

    def test_order_closed_for_edits(self):
        self.assertEqual(AD.order_closed_for_edits({"status": "cancelled"}), "order_cancelled")
        self.assertEqual(AD.order_closed_for_edits({"status": "delivered"}), "order_delivered")
        self.assertIsNone(AD.order_closed_for_edits({"status": "assembling"}))


class DefaultsDoNotLeakOutward(unittest.TestCase):
    """Столбец с `DEFAULT false` не должен превращаться в ключ ответа."""

    def test_category_out_hides_default_is_system(self):
        regular = {"id": "fruit", "name": "Фрукты", "emoji": "", "is_active": True,
                  "is_system": False, "sort_order": 1}
        out = AD.category_out(regular)
        self.assertNotIn("is_system", out, "обычная категория не должна нести is_system")
        self.assertEqual(out["name"], "Фрукты", "остальные поля не должны пострадать")

    def test_category_out_keeps_true_is_system(self):
        system = {"id": "sale", "name": "Акции", "emoji": "", "is_active": True,
                 "is_system": True, "sort_order": 0}
        out = AD.category_out(system)
        self.assertIs(out["is_system"], True, "системная категория обязана остаться помеченной")

    def test_zone_out_hides_default_manual_quote(self):
        regular = {"id": 1, "name": "Южное Бутово", "cost": 200, "free_from": 2000,
                  "is_active": True, "manual_quote": False}
        out = AD.zone_out(regular)
        self.assertNotIn("manual_quote", out, "обычная зона не должна нести manual_quote")
        self.assertEqual(out["name"], "Южное Бутово", "остальные поля не должны пострадать")

    def test_zone_out_keeps_true_manual_quote(self):
        manual = {"id": 3, "name": "Другой район", "cost": None, "free_from": None,
                 "is_active": True, "manual_quote": True}
        out = AD.zone_out(manual)
        self.assertIs(out["manual_quote"], True,
                      "зона с ручным расчётом обязана остаться помеченной")


if __name__ == "__main__":
    unittest.main()
