"""Круг «JSON → база → словарь» не должен ничего терять.

Перенос и чтение написаны порознь, и разойтись им проще всего на
вложенных структурах: корзина уехала в отдельную таблицу, брони — в
колонки, настройки — в «ключ-значение». Ошибка в любом из двух
направлений даёт витрину, которая работает, но показывает не то.

Поэтому проверяем не каждую сторону отдельно, а круг целиком: берём
`store.json`, переносим в базу, читаем обратно — и сравниваем с
исходником. Всё на SQLite, PostgreSQL не нужен.

Отдельно прогоняем через круг доменные функции: совпадение словарей
ещё не значит, что каталог соберётся одинаково.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

try:
    from backend.app.db import migrate_json as MJ
    from backend.app.db import models as M
    from backend.app.db import repository as R
    from backend.app.domain import catalog as K
    HAS_SA = True
except Exception:  # noqa: BLE001
    HAS_SA = False

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "data" / "store.json"
NOW = datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)


@unittest.skipUnless(HAS_SA, "SQLAlchemy не установлен")
@unittest.skipUnless(STORE.exists(), "нет data/store.json")
class RoundTrip(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from backend.app.db.store import reconcile

        cls.original = reconcile(json.loads(STORE.read_text(encoding="utf-8")))

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fructcity-roundtrip-"))
        self.engine = M.get_engine(f"sqlite:///{self.tmp / 'test.db'}")
        M.create_all(self.engine)
        MJ.migrate(self.engine, self.original)
        self.restored = R.load_state(self.engine)

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- коллекции ----------------------------------------------------------
    def test_collection_sizes_match(self):
        for name in ("products", "categories", "orders", "order_items",
                     "promocodes", "delivery_zones", "preorders", "users",
                     "sessions", "consents", "audit"):
            self.assertEqual(len(self.restored.get(name) or []),
                             len(self.original.get(name) or []),
                             f"{name}: потеряно при круге")

    def test_products_survive_field_by_field(self):
        """Товар — это деньги и остатки, тут сверяем каждое поле."""
        was = {p["id"]: p for p in self.original["products"]}
        now = {p["id"]: p for p in self.restored["products"]}
        self.assertEqual(set(was), set(now), "состав товаров изменился")
        for pid, before in was.items():
            after = now[pid]
            for field in ("sku", "slug", "name", "category_id", "type",
                          "price", "price_per_kg", "sale_price", "sale_until",
                          "vat_rate", "is_active", "emoji", "image_key", "description"):
                self.assertEqual(after.get(field), before.get(field),
                                 f"товар {before['sku']}: поле {field}")
            # Строго, а не через float(): 39 и 39.0 для витрины разные.
            # Колонка остатка дробная, и без обратного приведения
            # покупатель увидел бы «39.0 шт».
            self.assertEqual(after["stock"], before["stock"],
                             f"товар {before['sku']}: остаток")
            self.assertIs(type(after["stock"]), type(before["stock"]),
                          f"товар {before['sku']}: тип остатка изменился")

    def test_orders_and_items_survive(self):
        was = {o["id"]: o for o in self.original["orders"]}
        now = {o["id"]: o for o in self.restored["orders"]}
        for oid, before in was.items():
            after = now[oid]
            for field in ("number", "status", "payment_status", "payment_method",
                          "items_total", "total", "delivery_cost",
                          "agreed_delivery_cost", "discount_amount", "hold_amount"):
                self.assertEqual(after.get(field), before.get(field),
                                 f"заказ №{before['number']}: поле {field}")

    # -- вложенные структуры ------------------------------------------------
    def test_cart_returns_into_session(self):
        st = json.loads(json.dumps(self.original))
        st["sessions"][0]["cart"] = [
            {"product_id": st["products"][0]["id"], "qty": 2, "weight": None},
            {"product_id": st["products"][1]["id"], "qty": None, "weight": 1.5},
        ]
        MJ.migrate(self.engine, st, force=True)
        back = R.load_state(self.engine)

        session = next(s for s in back["sessions"] if s["id"] == st["sessions"][0]["id"])
        cart = sorted(session["cart"], key=lambda i: i["product_id"])
        self.assertEqual(len(cart), 2, "корзина не вернулась в сессию")
        self.assertEqual(cart[0]["qty"], 2)
        self.assertEqual(cart[1]["weight"], 1.5)

    def test_recent_lists_return_into_session(self):
        st = json.loads(json.dumps(self.original))
        order_ids = [o["id"] for o in st["orders"]]
        pre_ids = [p["id"] for p in st["preorders"]]
        st["sessions"][0]["recent_orders"] = order_ids
        st["sessions"][0]["recent_preorders"] = pre_ids
        MJ.migrate(self.engine, st, force=True)
        back = R.load_state(self.engine)

        session = next(s for s in back["sessions"] if s["id"] == st["sessions"][0]["id"])
        self.assertEqual(sorted(session["recent_orders"]), sorted(order_ids))
        self.assertEqual(sorted(session["recent_preorders"]), sorted(pre_ids))

    def test_slot_booking_key_is_restored(self):
        st = json.loads(json.dumps(self.original))
        st["slot_bookings"] = {"delivery|2026-08-25|14": 2, "pickup|2026-08-26|10": 1}
        MJ.migrate(self.engine, st, force=True)
        back = R.load_state(self.engine)
        self.assertEqual(back["slot_bookings"], st["slot_bookings"],
                         "составной ключ брони не собрался обратно")

    def test_meat_bookings_survive(self):
        st = json.loads(json.dumps(self.original))
        st["meat_bookings"] = {"2026-08-25": 12.5, "2026-08-28": 30}
        MJ.migrate(self.engine, st, force=True)
        back = R.load_state(self.engine)
        self.assertEqual({k: float(v) for k, v in back["meat_bookings"].items()},
                         {k: float(v) for k, v in st["meat_bookings"].items()})

    def test_settings_and_home_config_survive(self):
        self.assertEqual(self.restored["settings"], self.original["settings"],
                         "настройки изменились после круга")
        self.assertEqual(self.restored["home_config"], self.original["home_config"],
                         "конструктор главной изменился после круга")

    def test_settings_keep_their_types(self):
        """Список остаётся списком, число — числом, а не строкой."""
        settings = self.restored["settings"]
        self.assertIsInstance(settings.get("meat_days"), list)
        self.assertIsInstance(settings.get("work_from"), int)
        self.assertIsInstance(settings.get("shop_name"), str)

    # -- домен поверх круга -------------------------------------------------
    def test_catalog_is_identical_on_both_sources(self):
        """Главная проверка: каталог собирается одинаково.

        Совпадение словарей ещё не значит, что витрина покажет то же
        самое — фильтры и сортировки чувствительны к типам и к порядку.
        """
        for kwargs in (
            {"category": "all", "sort": "pop"},
            {"category": "all", "sort": "asc"},
            {"category": "all", "sort": "desc"},
            {"category": "sale"},
            {"in_stock": True},
            {"on_sale": True},
            {"search": "помидор"},
            {"price_min": 100, "price_max": 300},
            {"offset": 5, "limit": 7},
        ):
            with self.subTest(**kwargs):
                self.assertEqual(K.list_products(self.restored, now=NOW, **kwargs),
                                 K.list_products(self.original, now=NOW, **kwargs))

    def test_price_range_and_slots_identical(self):
        self.assertEqual(K.price_range(self.restored, NOW), K.price_range(self.original, NOW))
        for method in ("delivery", "pickup"):
            self.assertEqual(K.slots_view(self.restored, method, NOW),
                             K.slots_view(self.original, method, NOW),
                             f"расписание слотов разошлось: {method}")
        self.assertEqual(K.meat_dates_view(self.restored, NOW),
                         K.meat_dates_view(self.original, NOW))

    def test_product_cards_identical(self):
        for p in self.original["products"]:
            for key in (p["slug"], str(p["id"]), p["sku"]):
                with self.subTest(key=key):
                    a = K.find_product(self.original, key)
                    b = K.find_product(self.restored, key)
                    self.assertEqual(K.public_product(b, NOW), K.public_product(a, NOW))


@unittest.skipUnless(HAS_SA, "SQLAlchemy не установлен")
class JsNumber(unittest.TestCase):
    """Приведение чисел к тому виду, в каком их отдаёт JavaScript."""

    def test_whole_float_becomes_int(self):
        self.assertIs(type(R.js_number(39.0)), int)
        self.assertEqual(R.js_number(39.0), 39)
        self.assertEqual(R.js_number(0.0), 0)
        self.assertEqual(R.js_number(-3.0), -3)

    def test_fractional_stays_fractional(self):
        """Остаток весового товара 12.5 кг — это 12.5, а не 12."""
        self.assertEqual(R.js_number(12.5), 12.5)
        self.assertEqual(R.js_number(0.5), 0.5)

    def test_other_types_untouched(self):
        for value in (39, None, "текст", True, False, [1.0]):
            self.assertIs(R.js_number(value), value)


if __name__ == "__main__":
    unittest.main()
