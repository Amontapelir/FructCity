"""SSR-страницы: мета-теги, JSON-LD, sitemap, robots (`domain/pages.py`).

Две проверки здесь закрывают настоящие дефекты: порядок ключей в
JSON-LD и экранирование круглых скобок в адресе фотографии. Оба
относятся к тому, что читает не человек, а поисковик и Викисклад, —
поэтому и не проявлялись до сверки текста.
"""

from __future__ import annotations

import unittest

from backend.app.domain import pages as P
from backend.app.domain.photos import PHOTOS


class PhotoUrl(unittest.TestCase):
    """Адрес фотографии на Викискладе."""

    def test_parentheses_are_not_escaped(self):
        """`encodeURIComponent` в JS не трогает `!~*'()`, а
        `urllib.parse.quote(safe='')` экранирует. Часть имён файлов на
        Викискладе содержит круглые скобки буквально
        (`Honey_(Italian-miele)_in_a_jar.jpg`), и экранированные скобки
        Викисклад не находит — фотография просто не открывается.
        Ошибиться здесь легко снова: `quote()` по умолчанию выглядит
        безобиднее, чем есть.
        """
        with_parens = [k for k, v in PHOTOS.items() if "(" in str(v) or ")" in str(v)]
        if not with_parens:
            self.skipTest("в таблице фотографий нет имён со скобками")
        for key in with_parens:
            with self.subTest(key=key):
                url = P.photo_url(key)
                self.assertNotIn("%28", url, "скобка не должна экранироваться")
                self.assertNotIn("%29", url, "скобка не должна экранироваться")


class PagesMetaAssertions(unittest.TestCase):
    """Сборка meta по типам страниц."""

    STATE = {
        "settings": {"shop_name": "ТестСити", "phone": "+7 999 000-00-00",
                    "email": "info@test.ru", "pickup_address": "ул. Тестовая, 1",
                    "work_from": 9, "work_to": 21, "requisites": "ИП Тестов"},
        "categories": [{"id": "fruit", "name": "Фрукты", "is_active": True},
                      {"id": "sale", "name": "Акции", "is_active": True, "is_system": True}],
        "products": [
            {"id": 1, "sku": "AP-1", "slug": "apple", "name": "Яблоко", "type": "unit",
             "price": 99, "stock": 10, "category_id": "fruit", "is_active": True,
             "image_key": "apple_green", "min_weight": 0, "description": ""},
            {"id": 2, "sku": "PR-2", "slug": "pear", "name": "Груша", "type": "weighted",
             "price_per_kg": 149.5, "stock": 0, "category_id": "fruit", "is_active": True,
             "image_key": None, "min_weight": 0.5, "description": "Спелая груша"},
        ],
    }

    def test_product_meta_not_found(self):
        self.assertIsNone(P.product_meta(self.STATE, "https://x", "S", "нет-такого"))

    def test_product_meta_found_has_offer_and_availability(self):
        meta = P.product_meta(self.STATE, "https://x", "S", "apple")
        self.assertIn("99", meta["title"])
        self.assertEqual(meta["json_ld"]["offers"]["availability"], "https://schema.org/InStock")
        self.assertEqual(meta["canonical"], "https://x/product/apple")

    def test_product_meta_json_ld_key_order(self):
        """Порядок ключей — часть выдачи, а не деталь реализации.

        `json.dumps` печатает их в порядке вставки, и этот текст
        попадает в страницу, которую читает поисковик. Ожидаемый
        порядок: `@context, @type, name, description, sku, image?,
        category?, offers` — при отсутствии фото или категории ключ не
        появляется вовсе, а порядок остальных не сдвигается. Однажды
        `offers` уехал выше `image`, и заметить это удалось только
        сравнением текста страницы.
        """
        with_photo = P.product_meta(self.STATE, "https://x", "S", "apple")
        self.assertEqual(list(with_photo["json_ld"].keys()),
                         ["@context", "@type", "name", "description", "sku",
                          "image", "category", "offers"])

        # "pear" в фикстуре — без image_key: ключ image обязан не появиться
        # в словаре вовсе (а не оказаться пустым/None), как `undefined`
        # в JS не печатается `JSON.stringify`.
        meta = P.product_meta(self.STATE, "https://x", "S", "pear")
        self.assertEqual(list(meta["json_ld"].keys()),
                         ["@context", "@type", "name", "description", "sku",
                          "category", "offers"], "нет фото — ключ image пропущен, не пуст")

    def test_product_meta_out_of_stock(self):
        meta = P.product_meta(self.STATE, "https://x", "S", "pear")
        self.assertEqual(meta["json_ld"]["offers"]["availability"], "https://schema.org/OutOfStock")
        self.assertIn("eligibleQuantity", meta["json_ld"]["offers"])

    def test_catalog_meta_unknown_category_is_404(self):
        self.assertIsNone(P.catalog_meta(self.STATE, "https://x", "S", "нет-такой"))

    def test_catalog_meta_all(self):
        meta = P.catalog_meta(self.STATE, "https://x", "S", None)
        self.assertEqual(meta["json_ld"]["numberOfItems"], 2)

    def test_catalog_meta_category_filters(self):
        meta = P.catalog_meta(self.STATE, "https://x", "S", "fruit")
        self.assertEqual(meta["json_ld"]["numberOfItems"], 2)

    def test_spa_meta_home_has_jsonld(self):
        meta = P.spa_meta("https://x", "S", "/", self.STATE["settings"])
        self.assertIn("json_ld", meta)
        self.assertFalse(meta["noindex"])

    def test_spa_meta_other_route_noindex(self):
        meta = P.spa_meta("https://x", "S", "/cart", self.STATE["settings"])
        self.assertTrue(meta["noindex"])
        self.assertNotIn("json_ld", meta)

    def test_legal_meta_policy_vs_offer(self):
        policy = P.legal_meta("https://x", "S", "/policy", self.STATE["settings"])
        offer = P.legal_meta("https://x", "S", "/offer", self.STATE["settings"])
        self.assertIn("конфиденциальности", policy["title"])
        self.assertIn("оферта", offer["title"])

    def test_sitemap_includes_active_only(self):
        state = {**self.STATE, "products": [
            *self.STATE["products"],
            {"id": 3, "sku": "X", "slug": "hidden", "name": "Скрытый", "type": "unit",
             "price": 1, "stock": 1, "category_id": "fruit", "is_active": False},
        ]}
        xml = P.sitemap_xml("https://x", state)
        self.assertIn("/product/apple", xml)
        self.assertNotIn("/product/hidden", xml)

    def test_robots_txt_points_at_sitemap(self):
        self.assertIn("Sitemap: https://x/sitemap.xml", P.robots_txt("https://x"))


if __name__ == "__main__":
    unittest.main()
