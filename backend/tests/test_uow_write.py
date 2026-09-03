"""Слой записи: транзакция, счётчики, применение изменений.

Проверяется не «функция что-то записала», а три свойства, ради которых
слой существует:

1. **Разница переносится целиком и точно.** Круг «изменил словарь →
   записал → прочитал» должен вернуть ровно то, что положили. Особенно
   на вложенном: корзина, брони, настройки живут в отдельных таблицах.
2. **Ошибка не оставляет следа.** Исключение посреди оформления
   откатывает всё: наполовину созданный заказ хуже, чем несозданный.
3. **Остаток не продаётся дважды.** Проверка и списание идут в одной
   транзакции, и повторный вызов на последней банке обязан отказать.

Всё на SQLite: PostgreSQL в песочнице нет, а логика разницы от диалекта
не зависит. То, что зависит (последовательности, рекомендательная
блокировка), в SQLite просто не включается — и это отмечено в тестах,
чтобы никто не принял их за проверку блокировки.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

try:
    from sqlalchemy import select

    from backend.app.db import migrate_json as MJ
    from backend.app.db import models as M
    from backend.app.db import repository as R
    from backend.app.db import uow as U
    from backend.app.domain import shop as S
    HAS_SA = True
except Exception:  # noqa: BLE001
    HAS_SA = False

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "data" / "store.json"
NOW = datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)


@unittest.skipUnless(HAS_SA, "SQLAlchemy не установлен")
@unittest.skipUnless(STORE.exists(), "нет data/store.json")
class WriteLayer(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from backend.app.db.store import reconcile

        cls.original = reconcile(json.loads(STORE.read_text(encoding="utf-8")))

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fructcity-uow-"))
        self.engine = M.get_engine(f"sqlite:///{self.tmp / 'test.db'}")
        M.create_all(self.engine)
        MJ.migrate(self.engine, self.original)

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def state(self) -> dict:
        return R.load_state(self.engine)

    # -- плоские коллекции --------------------------------------------------

    def test_insert_update_delete_reach_the_database(self):
        with U.transaction(self.engine, lock=False) as unit:
            new_id = unit.next_id("categories") if False else "тестовая"
            unit.state["categories"].append(
                {"id": new_id, "name": "Тест", "emoji": "🧪", "is_system": False,
                 "sort_order": 99, "is_active": True, "created_at": None})
            unit.state["products"][0]["name"] = "Переименован"
            victim = unit.state["promocodes"][0]["id"]
            unit.state["promocodes"] = [p for p in unit.state["promocodes"]
                                        if p["id"] != victim]

        after = self.state()
        self.assertIn("тестовая", [c["id"] for c in after["categories"]])
        self.assertEqual(after["products"][0]["name"], "Переименован")
        self.assertNotIn(victim, [p["id"] for p in after["promocodes"]])

    def test_untouched_rows_are_not_rewritten(self):
        """Пустая операция не должна порождать ни одного запроса на запись.

        Если разница считается неверно, слой перепишет весь каталог на
        каждом запросе. Работать это будет, а нагрузка вырастет молча.
        """
        with U.transaction(self.engine, lock=False) as unit:
            pass
        self.assertEqual(unit.stats, {"insert": 0, "update": 0, "delete": 0})

    def test_only_changed_fields_are_written(self):
        with U.transaction(self.engine, lock=False) as unit:
            unit.state["products"][0]["stock"] = 777
        self.assertEqual(unit.stats["update"], 1)
        self.assertEqual(unit.stats["insert"], 0)

    # -- вложенное ----------------------------------------------------------

    def test_cart_survives_the_round_trip(self):
        sid = "тест-корзина"
        with U.transaction(self.engine, lock=False) as unit:
            product = next(p for p in unit.state["products"] if p["type"] == "unit")
            unit.state["sessions"].append({
                "id": unit.next_id("sessions"), "sid": sid, "user_id": None,
                "role": "guest", "promo_code": None, "ip": None,
                "created_at": None, "last_seen": None,
                "expires_at": "2099-01-01T00:00:00.000Z",
                "cart": [{"product_id": product["id"], "qty": 3, "weight": None}],
                "recent_orders": [], "recent_preorders": []})

        restored = next(s for s in self.state()["sessions"] if s["sid"] == sid)
        self.assertEqual(len(restored["cart"]), 1)
        self.assertEqual(restored["cart"][0]["qty"], 3)

    def test_cart_removal_removes_the_row(self):
        sid = "тест-очистка"
        with U.transaction(self.engine, lock=False) as unit:
            product = next(p for p in unit.state["products"] if p["type"] == "unit")
            unit.state["sessions"].append({
                "id": unit.next_id("sessions"), "sid": sid, "role": "guest",
                "expires_at": "2099-01-01T00:00:00.000Z",
                "cart": [{"product_id": product["id"], "qty": 1, "weight": None}],
                "recent_orders": [], "recent_preorders": []})

        with U.transaction(self.engine, lock=False) as unit:
            session = next(s for s in unit.state["sessions"] if s["sid"] == sid)
            session["cart"] = []

        restored = next(s for s in self.state()["sessions"] if s["sid"] == sid)
        self.assertEqual(restored["cart"], [],
                         "позиция осталась в базе после очистки корзины")

    def test_slot_and_meat_bookings_round_trip(self):
        key = "delivery|2099-01-02|10"
        with U.transaction(self.engine, lock=False) as unit:
            unit.state["slot_bookings"][key] = 2
            unit.state["meat_bookings"]["2099-01-02"] = 1.5

        after = self.state()
        self.assertEqual(after["slot_bookings"][key], 2)
        self.assertEqual(after["meat_bookings"]["2099-01-02"], 1.5)

        with U.transaction(self.engine, lock=False) as unit:
            unit.state["slot_bookings"][key] = 5
        self.assertEqual(self.state()["slot_bookings"][key], 5)

        with U.transaction(self.engine, lock=False) as unit:
            del unit.state["slot_bookings"][key]
        self.assertNotIn(key, self.state()["slot_bookings"])

    def test_settings_round_trip(self):
        with U.transaction(self.engine, lock=False) as unit:
            unit.state["settings"]["shop_name"] = "ФруктСити-тест"
            unit.state["settings"]["новая_настройка"] = {"a": [1, 2]}

        after = self.state()
        self.assertEqual(after["settings"]["shop_name"], "ФруктСити-тест")
        self.assertEqual(after["settings"]["новая_настройка"], {"a": [1, 2]})

    def test_service_keys_are_not_erased_by_a_write(self):
        """Отметка о снимке — служебная, репозиторий её не отдаёт.

        Значит в снимке «до» её нет, и наивная разница сочла бы её
        удалённой. Тогда после первой же записи база перестала бы
        понимать, отстала она от JSON или нет.
        """
        marker = self.engine.connect()
        with marker:
            before = marker.execute(
                select(M.Setting.__table__.c.key)
                .where(M.Setting.__table__.c.key.like("\\_%", escape="\\"))).all()
        self.assertTrue(before, "в базе нет служебных ключей — тест ничего не проверит")

        with U.transaction(self.engine, lock=False) as unit:
            unit.state["settings"]["shop_name"] = "что-нибудь"

        with self.engine.connect() as conn:
            after = conn.execute(
                select(M.Setting.__table__.c.key)
                .where(M.Setting.__table__.c.key.like("\\_%", escape="\\"))).all()
        self.assertEqual(sorted(after), sorted(before),
                         "запись стёрла служебные ключи из настроек")

    # -- счётчики -----------------------------------------------------------

    def test_next_id_is_unique_and_recorded_in_seq(self):
        with U.transaction(self.engine, lock=False) as unit:
            a = unit.next_id("orders")
            b = unit.next_id("orders")
            self.assertNotEqual(a, b, "два вызова подряд выдали один номер")
            self.assertEqual(unit.state["seq"]["orders"], max(a, b),
                             "счётчик в состоянии не обновлён — номер заказа задвоится")

    def test_next_id_does_not_collide_with_existing_rows(self):
        with U.transaction(self.engine, lock=False) as unit:
            used = {o["id"] for o in unit.state["orders"]}
            self.assertNotIn(unit.next_id("orders"), used)

    # -- откат --------------------------------------------------------------

    def test_exception_leaves_nothing_behind(self):
        before = len(self.state()["categories"])

        class Boom(Exception):
            pass

        with self.assertRaises(Boom):
            with U.transaction(self.engine, lock=False) as unit:
                unit.state["categories"].append(
                    {"id": "призрак", "name": "Призрак", "is_system": False,
                     "sort_order": 0, "is_active": True})
                raise Boom

        after = self.state()
        self.assertEqual(len(after["categories"]), before)
        self.assertNotIn("призрак", [c["id"] for c in after["categories"]])


@unittest.skipUnless(HAS_SA, "SQLAlchemy не установлен")
@unittest.skipUnless(STORE.exists(), "нет data/store.json")
class OrderThroughTheWriteLayer(unittest.TestCase):
    """Оформление целиком: домен меняет словарь, слой записи — базу."""

    @classmethod
    def setUpClass(cls):
        from backend.app.db.store import reconcile

        cls.original = reconcile(json.loads(STORE.read_text(encoding="utf-8")))

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fructcity-uow-order-"))
        self.engine = M.get_engine(f"sqlite:///{self.tmp / 'test.db'}")
        M.create_all(self.engine)
        MJ.migrate(self.engine, self.original)
        self.sid = "покупатель"
        with U.transaction(self.engine, lock=False) as unit:
            self.product = next(p for p in unit.state["products"]
                                if p["type"] == "unit" and p["is_active"])
            # Остаток ставим ровно в 1: тогда второй заказ обязан отказать.
            self.product["stock"] = 1
            unit.state["sessions"].append({
                "id": unit.next_id("sessions"), "sid": self.sid, "role": "guest",
                "expires_at": "2099-01-01T00:00:00.000Z",
                "cart": [{"product_id": self.product["id"], "qty": 1, "weight": None}],
                "recent_orders": [], "recent_preorders": []})

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _order(self) -> dict:
        from backend.app.domain.auth import iso_now

        with U.transaction(self.engine, lock=False) as unit:
            session = next(s for s in unit.state["sessions"] if s["sid"] == self.sid)
            return S.place_order(
                unit.state, next_id=unit.next_id, now_iso=lambda: iso_now(NOW),
                session=session, now=NOW, ip="127.0.0.1",
                # Слот обязателен и для самовывоза (ТЗ 4.4) — "2026-08-19"
                # укладывается в горизонт settings.horizon_d=3 от NOW.
                # Ключ схемы — "payment", а не "payment_method" (то, как
                # называется столбец в базе). Передать столбец вместо
                # ключа схемы — незамеченная опечатка: place_order молча
                # писал бы NULL, потому что читает data.get("payment").
                data={"name": "Тест", "phone": "+79161234567", "method": "pickup",
                      "payment": "cash", "consent": True,
                      "slot_ymd": "2026-08-19", "slot_from": 14})

    def test_order_and_its_items_are_stored(self):
        result = self._order()
        self.assertNotIn("error", result, result)

        state = self.state = R.load_state(self.engine)
        order = next((o for o in state["orders"] if o["id"] == result["order"]["id"]), None)
        self.assertIsNotNone(order, "заказ не доехал до базы")
        items = [i for i in state["order_items"] if i["order_id"] == order["id"]]
        self.assertEqual(len(items), 1, "позиции заказа не записаны")

    def test_stock_is_decremented_in_the_same_transaction(self):
        self._order()
        state = R.load_state(self.engine)
        product = next(p for p in state["products"] if p["id"] == self.product["id"])
        self.assertEqual(product["stock"], 0, "остаток не списан")

    def test_last_item_cannot_be_sold_twice(self):
        """Главное свойство слоя: второй заказ на тот же остаток отказывает.

        Это не тест на гонку — параллельных потоков здесь нет. Он
        проверяет, что списание действительно доехало до базы: если бы
        запись терялась, второй заказ увидел бы прежний остаток и
        прошёл.
        """
        self.assertNotIn("error", self._order())

        with U.transaction(self.engine, lock=False) as unit:
            session = next(s for s in unit.state["sessions"] if s["sid"] == self.sid)
            session["cart"] = [{"product_id": self.product["id"], "qty": 1,
                                "weight": None}]

        second = self._order()
        self.assertIn("error", second, "последний товар продан дважды")

    def test_cart_is_empty_after_checkout(self):
        self._order()
        state = R.load_state(self.engine)
        session = next(s for s in state["sessions"] if s["sid"] == self.sid)
        self.assertEqual(session["cart"], [], "корзина не очищена после оформления")

    def test_recent_orders_reaches_the_database(self):
        result = self._order()
        state = R.load_state(self.engine)
        session = next(s for s in state["sessions"] if s["sid"] == self.sid)
        self.assertIn(result["order"]["id"], session["recent_orders"],
                      "гость не увидит свой заказ: список недавних не записан")


if __name__ == "__main__":
    unittest.main()
