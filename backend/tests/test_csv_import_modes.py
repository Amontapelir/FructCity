"""Импорт CSV: три режима сопоставления по SKU (ТЗ 10.7, ROADMAP 2.10).

`import_products` раньше знал только один режим — фактически нынешний
`full`. Остальные два ограничивают, что именно можно затронуть строкой
CSV: `prices_stock` не создаёт новых товаров и не трогает карточку,
`new_only` не трогает уже существующие. Тесты проверяют именно эти
границы — не то, что импорт вообще работает (это покрывал бы `full`
один), а то, что режимы друг друга не подменяют.
"""

from __future__ import annotations

import unittest

from backend.app.domain import admin as AD
from backend.app.domain import validate as V


def _state():
    return {
        "categories": [{"id": "fruits", "name": "Фрукты", "is_system": False,
                        "sort_order": 0, "is_active": True}],
        "products": [{
            "id": 1, "sku": "EXIST-1", "slug": "existing", "name": "Старое имя",
            "category_id": "fruits", "type": "unit", "price": 100, "price_per_kg": 0,
            "sale_price": None, "vat_rate": 20, "stock": 5, "is_active": True,
            "min_weight": 0.5, "weight_step": 0.5, "emoji": "📦", "image_key": None,
            "description": "", "created_at": "x", "updated_at": "x",
        }],
    }


def _row(**over):
    row = {"sku": "EXIST-1", "name": "Новое имя", "category": "fruits", "type": "unit",
           "price": "150", "sale_price": "", "stock": "9", "vat": "10", "active": "1"}
    row.update(over)
    return row


def _new_row(**over):
    row = {"sku": "NEW-1", "name": "Новый товар", "category": "fruits", "type": "unit",
           "price": "200", "sale_price": "", "stock": "3", "vat": "20", "active": "1"}
    row.update(over)
    return row


class FullModeUnchanged(unittest.TestCase):
    """`full` — прежнее поведение: есть SKU, обновляем карточку целиком; нет — создаём."""

    def test_updates_existing_card_entirely(self):
        state = _state()
        result = AD.import_products(state, next_id=lambda k: 99, now_iso=lambda: "now",
                                    rows=[_row()], mode="full")
        self.assertEqual(result, {"created": 0, "updated": 1, "skipped": []})
        p = state["products"][0]
        self.assertEqual(p["name"], "Новое имя")
        self.assertEqual(p["price"], 150)
        self.assertEqual(p["vat_rate"], 10)

    def test_creates_new_sku(self):
        state = _state()
        result = AD.import_products(state, next_id=lambda k: 99, now_iso=lambda: "now",
                                    rows=[_new_row()], mode="full")
        self.assertEqual(result["created"], 1)
        self.assertEqual(len(state["products"]), 2)


class PricesStockMode(unittest.TestCase):
    """`prices_stock` — только цена/акция/остаток/НДС, карточку не трогает."""

    def test_updates_only_pricing_fields(self):
        state = _state()
        result = AD.import_products(state, next_id=lambda k: 99, now_iso=lambda: "now",
                                    rows=[_row(name="ИГНОРИРУЕТСЯ", category="")],
                                    mode="prices_stock")
        self.assertEqual(result, {"created": 0, "updated": 1, "skipped": []})
        p = state["products"][0]
        self.assertEqual(p["price"], 150, "цена обязана обновиться")
        self.assertEqual(p["stock"], 9, "остаток обязан обновиться")
        self.assertEqual(p["vat_rate"], 10, "НДС обязан обновиться")
        self.assertEqual(p["name"], "Старое имя", "название режим трогать не должен")

    def test_does_not_create_unknown_sku(self):
        """Подмена сути режима: без этой проверки prices_stock вёл бы себя как full."""
        state = _state()
        result = AD.import_products(state, next_id=lambda k: 99, now_iso=lambda: "now",
                                    rows=[_new_row()], mode="prices_stock")
        self.assertEqual(result["created"], 0)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(len(state["products"]), 1, "новый товар не должен появиться")


class NewOnlyMode(unittest.TestCase):
    """`new_only` — только добавление, существующий SKU не трогается вовсе."""

    def test_creates_new_sku(self):
        state = _state()
        result = AD.import_products(state, next_id=lambda k: 99, now_iso=lambda: "now",
                                    rows=[_new_row()], mode="new_only")
        self.assertEqual(result["created"], 1)
        self.assertEqual(len(state["products"]), 2)

    def test_leaves_existing_sku_untouched(self):
        """Подмена: без проверки exist is not None new_only обновил бы карточку, как full."""
        state = _state()
        result = AD.import_products(state, next_id=lambda k: 99, now_iso=lambda: "now",
                                    rows=[_row(name="НЕ ДОЛЖНО ПРИМЕНИТЬСЯ")],
                                    mode="new_only")
        self.assertEqual(result, {"created": 0, "updated": 0,
                                  "skipped": [{"sku": "EXIST-1",
                                              "reason": "уже есть в каталоге — режим добавляет только новые"}]})
        self.assertEqual(state["products"][0]["name"], "Старое имя")


class UnknownMode(unittest.TestCase):

    def test_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            AD.import_products(_state(), next_id=lambda k: 1, now_iso=lambda: "now",
                              rows=[], mode="не режим")


class CsvImportSchemaMode(unittest.TestCase):

    def test_missing_mode_defaults_to_full(self):
        out = V.validate(V.SCHEMAS["csvImport"], {"csv": "sku;x"})
        self.assertEqual(out["mode"], "full")

    def test_invalid_mode_rejected(self):
        errs = {}
        V.SCHEMAS["csvImport"]["mode"]("не режим", "mode", errs)
        self.assertIn("mode", errs)


if __name__ == "__main__":
    unittest.main()
