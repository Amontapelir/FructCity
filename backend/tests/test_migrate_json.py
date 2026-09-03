"""Перенос store.json в базу — на SQLite и на настоящих данных магазина.

Перенос опасен тем, что легко «удаётся» наполовину: строки легли, а
часть данных потерялась по дороге — вложенные структуры не разложены,
поля не совпали по именам. Поэтому проверяется не «отработало без
ошибки», а совпадение с исходником.

Пропускается, если не установлен SQLAlchemy.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

try:
    from sqlalchemy import func, select
    from backend.app.db import models as M
    from backend.app.db import migrate_json as MJ
    HAS_SA = True
except Exception:  # noqa: BLE001
    HAS_SA = False

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "data" / "store.json"


@unittest.skipUnless(HAS_SA, "SQLAlchemy не установлен")
class MigrateJson(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fructcity-migrate-"))
        self.engine = M.get_engine(f"sqlite:///{self.tmp / 'test.db'}")
        M.create_all(self.engine)
        self.store = self._load_store()

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _load_store(self) -> dict:
        from backend.app.db.store import reconcile

        if STORE.exists():
            return reconcile(json.loads(STORE.read_text(encoding="utf-8")))
        return reconcile(SYNTHETIC)

    # -- основной путь ------------------------------------------------------
    def test_migrate_then_verify_is_clean(self):
        """После переноса сверка не должна находить ничего."""
        MJ.migrate(self.engine, self.store)
        self.assertEqual(MJ.verify(self.engine, self.store), [])

    def test_every_collection_lands_somewhere(self):
        """Ни одна непустая коллекция не теряется по дороге."""
        written = MJ.migrate(self.engine, self.store)
        for key, table in (("products", "products"), ("categories", "categories"),
                           ("orders", "orders"), ("order_items", "order_items"),
                           ("promocodes", "promocodes"), ("delivery_zones", "delivery_zones"),
                           ("preorders", "preorders"), ("users", "users")):
            expected = len(self.store.get(key) or [])
            if expected:
                self.assertEqual(written[table], expected, f"{key}: перенесено не всё")

    def test_repeat_without_force_is_refused(self):
        """Повторный перенос удвоил бы данные, поэтому запрещён."""
        MJ.migrate(self.engine, self.store)
        with self.assertRaises(RuntimeError) as ctx:
            MJ.migrate(self.engine, self.store)
        self.assertIn("уже есть данные", str(ctx.exception))

    def test_force_replaces_instead_of_doubling(self):
        MJ.migrate(self.engine, self.store)
        before = self._count(M.Product.__table__)
        MJ.migrate(self.engine, self.store, force=True)
        self.assertEqual(self._count(M.Product.__table__), before, "данные удвоились")
        self.assertEqual(MJ.verify(self.engine, self.store), [])

    def test_refuses_incomplete_schema(self):
        with self.engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE order_items")
        with self.assertRaises(RuntimeError) as ctx:
            MJ.migrate(self.engine, self.store)
        self.assertIn("order_items", str(ctx.exception))

    # -- вложенные структуры ------------------------------------------------
    def test_cart_moves_from_session_into_rows(self):
        st = json.loads(json.dumps(self.store))     # копия, исходник не трогаем
        st["sessions"] = [{
            "id": 901, "sid": "s-901", "role": "guest",
            "cart": [{"product_id": st["products"][0]["id"], "qty": 3, "weight": None},
                     {"product_id": 10 ** 9, "qty": 1, "weight": None}],   # товара нет
            "recent_orders": [o["id"] for o in st["orders"]] + [10 ** 9],
            "recent_preorders": [],
        }]
        MJ.migrate(self.engine, st, force=True)

        rows = self._rows(M.CartItem.__table__)
        self.assertEqual(len(rows), 1, "позиция на несуществующий товар не отброшена")
        self.assertEqual(rows[0].qty, 3)

        recent = self._rows(M.SessionRecent.__table__)
        self.assertEqual(len(recent), len(st["orders"]),
                         "ссылка на несуществующий заказ не отброшена")

    def test_slot_bookings_key_is_split_into_columns(self):
        st = json.loads(json.dumps(self.store))
        st["slot_bookings"] = {"delivery|2026-08-25|14": 2, "pickup|2026-08-26|10": 1,
                               "мусор": 5}
        MJ.migrate(self.engine, st, force=True)
        rows = sorted(self._rows(M.SlotBooking.__table__), key=lambda r: r.ymd)
        self.assertEqual(len(rows), 2, "мусорный ключ не отброшен")
        self.assertEqual((rows[0].method, rows[0].ymd, rows[0].slot_from, rows[0].booked),
                         ("delivery", "2026-08-25", 14, 2))

    def test_settings_and_home_config_land_as_json(self):
        MJ.migrate(self.engine, self.store)
        rows = {r.key: r.value for r in self._rows(M.Setting.__table__)}
        self.assertIn("home_config", rows)
        json.loads(rows["home_config"])              # значение обязано быть разбираемым
        for key in (self.store.get("settings") or {}):
            self.assertIn(key, rows, f"настройка {key} потерялась")

    def test_rows_with_different_field_sets(self):
        """Записи одной коллекции могут иметь разный набор полей.

        Так и есть в жизни: у категории «Акции» есть `is_system`, у
        остальных тринадцати его нет — в JSON поле со значением по
        умолчанию просто не пишется. Для многострочной вставки это не
        одно и то же: SQLAlchemy собирает один запрос на всю пачку и
        требует одинаковый набор ключей, иначе отказывает на второй
        строке. Недостающее берётся из значения по умолчанию в схеме.
        """
        st = json.loads(json.dumps(self.store))
        st["categories"] = [
            {"id": "sale", "name": "Акции", "is_system": True, "sort_order": 0,
             "emoji": "🔥", "is_active": True},
            {"id": "fruit", "name": "Фрукты", "sort_order": 1, "is_active": True},
            {"id": "berry", "name": "Ягоды", "sort_order": 2},
        ]
        st["products"] = [p for p in st["products"]
                          if p.get("category_id") in {"sale", "fruit", "berry"}]
        MJ.migrate(self.engine, st, force=True)

        rows = {r.id: r for r in self._rows(M.Category.__table__)}
        self.assertEqual(len(rows), 3)
        self.assertTrue(rows["sale"].is_system, "своё значение потеряно")
        self.assertFalse(rows["fruit"].is_system, "не подставлено значение по умолчанию")
        self.assertIsNone(rows["berry"].emoji)
        self.assertTrue(rows["berry"].is_active, "не подставлено значение по умолчанию")

    def test_align_fills_from_schema_defaults(self):
        """Значение берётся из схемы, а не выдумывается на месте."""
        rows = MJ._align([{"id": "a", "name": "А", "is_system": True},
                          {"id": "b", "name": "Б"}], M.Category.__table__)
        self.assertEqual({frozenset(r) for r in rows},
                         {frozenset({"id", "name", "is_system"})},
                         "наборы ключей не выровнены")
        self.assertIs(rows[1]["is_system"], False)

    def test_sequences_only_for_integer_keys(self):
        """Счётчик правится только там, где ключ числовой.

        У категорий он текстовый («fruit», «meat»), у броней мяса —
        дата. Попытка подвинуть за ними счётчик кончается в PostgreSQL
        отказом о несовместимых типах, и весь перенос падает уже после
        того, как данные легли. На SQLite этот шаг вообще не
        выполняется, поэтому проверяем сам отбор таблиц.
        """
        names = MJ.sequence_tables()
        self.assertIn("orders", names)
        self.assertIn("products", names)
        self.assertNotIn("categories", names, "текстовый ключ принят за счётчик")
        self.assertNotIn("meat_bookings", names, "ключ-дата принят за счётчик")
        self.assertNotIn("settings", names, "таблица без числового ключа")

    # -- расхождения ловятся ------------------------------------------------
    def test_verify_notices_changed_price(self):
        """Сверка проверяет значения, а не только число строк."""
        MJ.migrate(self.engine, self.store)
        pid = self.store["products"][0]["id"]
        with self.engine.begin() as conn:
            conn.execute(M.Product.__table__.update()
                         .where(M.Product.__table__.c.id == pid)
                         .values(price=99999))
        problems = MJ.verify(self.engine, self.store)
        self.assertTrue(any("price" in p for p in problems),
                        "подменённая цена не замечена")

    def test_verify_notices_missing_row(self):
        MJ.migrate(self.engine, self.store)
        with self.engine.begin() as conn:
            conn.execute(M.Product.__table__.delete()
                         .where(M.Product.__table__.c.id == self.store["products"][0]["id"]))
        problems = MJ.verify(self.engine, self.store)
        self.assertTrue(problems, "пропавший товар не замечен")

    def test_verify_notices_changed_order_total(self):
        if not self.store["orders"]:
            self.skipTest("в хранилище нет заказов")
        MJ.migrate(self.engine, self.store)
        with self.engine.begin() as conn:
            conn.execute(M.Order.__table__.update().values(total=1))
        problems = MJ.verify(self.engine, self.store)
        self.assertTrue(any("total" in p for p in problems), "подменённая сумма не замечена")

    # -- вспомогательное ----------------------------------------------------
    def _count(self, table) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(select(func.count()).select_from(table)).scalar() or 0)

    def _rows(self, table):
        with self.engine.connect() as conn:
            return conn.execute(select(table)).all()


SYNTHETIC = {
    "categories": [{"id": "fruit", "name": "Фрукты", "sort_order": 1, "is_active": True}],
    "products": [{"id": 1, "sku": "A-1", "slug": "a-1", "name": "Яблоки",
                  "category_id": "fruit", "type": "weighted", "price": 0,
                  "price_per_kg": 120, "stock": 10, "is_active": True}],
    "users": [], "sessions": [], "orders": [], "order_items": [],
    "order_status_history": [], "preorders": [], "promocodes": [],
    "promocode_usages": [], "delivery_zones": [], "otp": [], "tg_links": [],
    "consents": [], "audit": [],
    "slot_bookings": {}, "meat_bookings": {},
    "settings": {"shop_name": "FructCity"}, "home_config": {},
}


if __name__ == "__main__":
    unittest.main()
