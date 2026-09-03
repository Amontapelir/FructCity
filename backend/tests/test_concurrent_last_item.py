"""Одновременный заказ последнего товара.

Проверяемое утверждение: когда остаток равен единице, а покупателей
несколько, продаётся ровно один товар, а остаток не уходит в минус.

Почему не через HTTP. Маршруты объявлены `async def`, а запись —
обычный синхронный SQLAlchemy внутри них. Отправить десять запросов
через `ASGITransport` и получить настоящую одновременность нельзя: они
выполнятся друг за другом на одном потоке событий, и «проверка гонки»
проверяла бы только то, что очередь есть очередь. Поэтому запросы
имитируются на том уровне, где гонка настоящая, — потоки, каждый со
своей транзакцией. Ниже `ctx.tx()` HTTP-слой ничего не добавляет:
и остаток, и его списание живут в `place_order` под той же
`transaction()`.

Почему только PostgreSQL. Очерёдность держит `pg_advisory_xact_lock`
(`db/uow.py`), которой в SQLite нет вовсе. Прогон на SQLite показывал
бы поведение, которого в бою не будет, — поэтому набор пропускается, а
не подменяет проверку чем-то похожим.

**Своя база, не рабочая.** Тест пишет по-настоящему, поэтому создаёт
отдельную базу `<имя>_concurrency` на том же сервере, переносит в неё
копию `data/store.json` и удаляет её в конце. Писать проверками в базу,
на которой ведётся разработка, нельзя: один неудачный прогон — и
каталог с заказами испорчен.
"""

from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor

from backend.tests.paths import STORE

try:
    from sqlalchemy import create_engine, select, text

    from backend.app.config import get_settings
    from backend.app.db import migrate_json as MJ
    from backend.app.db import models as M
    from backend.app.db import uow as U
    from backend.app.domain import auth as A
    from backend.app.domain import shop as S
    HAS_STACK = True
except Exception:  # noqa: BLE001
    HAS_STACK = False

BUYERS = 8                      # покупателей на один последний товар
TEST_DB_SUFFIX = "_concurrency"


def postgres_ready() -> bool:
    if not HAS_STACK:
        return False
    try:
        get_settings.cache_clear()
        settings = get_settings()
        return settings.db_configured and settings.database_url.startswith("postgresql")
    except Exception:  # noqa: BLE001
        return False


def _test_database_url() -> tuple[str, str, str]:
    """(url тестовой базы, url служебной базы, имя тестовой базы)."""
    settings = get_settings()
    base = settings.database_url
    head, _, name = base.rpartition("/")
    test_name = name.split("?")[0] + TEST_DB_SUFFIX
    return f"{head}/{test_name}", settings.admin_database_url, test_name


@unittest.skipUnless(HAS_STACK, "не установлены SQLAlchemy или FastAPI")
@unittest.skipUnless(postgres_ready(), "нужен PostgreSQL: очерёдность держит advisory-блокировка")
@unittest.skipUnless(STORE.exists(), "нет data/store.json")
class ConcurrentLastItem(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.url, admin_url, cls.db_name = _test_database_url()

        # AUTOCOMMIT обязателен: DROP/CREATE DATABASE не выполняются
        # внутри транзакции.
        cls.admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", future=True)
        try:
            with cls.admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{cls.db_name}"'))
            # Не своим `CREATE DATABASE`, а `provision()`: он создаёт базу
            # с ВЛАДЕЛЬЦЕМ — записью приложения. Своя команда создавала
            # базу от административной записи, и приложение потом не могло
            # создать в ней ни одной таблицы («нет доступа к схеме public»:
            # с PostgreSQL 15 схема принадлежит владельцу базы, а не всем).
            M.provision(cls.url)
        except Exception as e:  # noqa: BLE001
            cls.admin.dispose()
            raise unittest.SkipTest(f"не удалось создать тестовую базу: {e}")

        cls.engine = M.get_engine(cls.url)
        M.create_all(cls.engine)
        cls.state = MJ.load_store(STORE)
        MJ.migrate(cls.engine, cls.state, force=True, snapshot=STORE)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "engine"):
            cls.engine.dispose()
        M.dispose_engines()
        if hasattr(cls, "admin"):
            with cls.admin.connect() as conn:
                try:
                    conn.execute(text(f'DROP DATABASE IF EXISTS "{cls.db_name}"'))
                except Exception:  # noqa: BLE001
                    # Кто-то ещё держит соединение (например, пул, который
                    # не успел закрыться). WITH (FORCE) есть с PostgreSQL 13,
                    # поэтому он вторым шагом, а не первым.
                    conn.execute(
                        text(f'DROP DATABASE IF EXISTS "{cls.db_name}" WITH (FORCE)'))
            cls.admin.dispose()

    # -- вспомогательное ----------------------------------------------------
    def _set_stock(self, product_id: int, stock: float) -> None:
        with self.engine.begin() as conn:
            conn.execute(M.Product.__table__.update()
                         .where(M.Product.__table__.c.id == product_id)
                         .values(stock=stock))

    def _widen_slot_capacity(self) -> None:
        """Снимает ограничение слота на время проверки.

        Иначе тест доказывал бы не то, что заявляет: вместимость слота
        самовывоза в исходных данных — 6, покупателей здесь 8, и часть
        отказов пришла бы от переполненного слота, а не от кончившегося
        остатка. Проверяется остаток, поэтому мешать ему нечему.
        """
        import json

        with self.engine.begin() as conn:
            for key in ("slot_capacity_pickup", "slot_capacity_delivery"):
                conn.execute(M.Setting.__table__.update()
                             .where(M.Setting.__table__.c.key == key)
                             .values(value=json.dumps(BUYERS * 10)))

    def _stock(self, product_id: int) -> float:
        with self.engine.connect() as conn:
            return float(conn.execute(
                select(M.Product.__table__.c.stock)
                .where(M.Product.__table__.c.id == product_id)).scalar())

    def _unit_product(self) -> dict:
        for p in self.state["products"]:
            if p.get("type") == "unit" and p.get("is_active"):
                return p
        self.skipTest("в хранилище нет штучного товара")

    def _slot(self) -> tuple[str, str]:
        """Первый свободный слот самовывоза — заказ без него не оформить.

        Через `slots_view`, а не своим перебором дат: слот должен быть
        тем же, который увидел бы покупатель на витрине, вместе с
        вместимостью и отсечкой.
        """
        from backend.app.domain import catalog as CAT

        view = CAT.slots_view(self.state, "pickup")
        for day in view["days"]:
            for s in day["slots"]:
                if s["ok"]:
                    return day["ymd"], s["from"]
        self.skipTest("нет свободного слота самовывоза")

    def _buy(self, product_id: int, ymd: str, slot_from: str, n: int) -> dict:
        """Одна покупка в своей транзакции — то же, что делает маршрут."""
        with U.transaction(self.engine) as unit:
            session = A.new_session(next_id=unit.next_id, ip="127.0.0.1",
                                    cart=[{"product_id": product_id, "qty": 1}])
            unit.state.setdefault("sessions", []).append(session)
            return S.place_order(
                unit.state, next_id=unit.next_id, now_iso=A.iso_now,
                session=session,
                data={
                    "method": "pickup", "address": "", "slot_ymd": ymd,
                    "slot_from": slot_from, "name": f"Покупатель {n}",
                    "phone": "+7916000000" + str(n % 10), "email": "race@example.com",
                    "comment": "", "payment": "cash", "consent": True,
                    "marketing_consent": False, "telegram_optin": False,
                },
                ip="127.0.0.1", user_agent="test")

    # -- сама проверка ------------------------------------------------------
    def test_only_one_buyer_gets_the_last_item(self):
        product = self._unit_product()
        pid = product["id"]
        ymd, slot_from = self._slot()
        self._widen_slot_capacity()
        self._set_stock(pid, 1)

        with ThreadPoolExecutor(max_workers=BUYERS) as pool:
            results = list(pool.map(
                lambda n: self._buy(pid, ymd, slot_from, n), range(BUYERS)))

        sold = [r for r in results if not r.get("error")]
        refused = [r for r in results if r.get("error")]

        self.assertEqual(len(sold), 1,
                         f"последний товар продан {len(sold)} раз(а), а не один: {results}")
        self.assertEqual(len(refused), BUYERS - 1, "остальные обязаны получить отказ")
        self.assertEqual(self._stock(pid), 0, "остаток после продажи обязан стать нулём")

    def test_stock_never_goes_negative_under_load(self):
        """Три штуки на восьмерых: продано ровно три, остаток — ноль."""
        product = self._unit_product()
        pid = product["id"]
        ymd, slot_from = self._slot()
        self._widen_slot_capacity()
        self._set_stock(pid, 3)

        with ThreadPoolExecutor(max_workers=BUYERS) as pool:
            results = list(pool.map(
                lambda n: self._buy(pid, ymd, slot_from, n), range(BUYERS)))

        sold = [r for r in results if not r.get("error")]
        self.assertEqual(len(sold), 3, f"продано {len(sold)} из трёх доступных")
        self.assertEqual(self._stock(pid), 0, "остаток не должен уходить в минус")


if __name__ == "__main__":
    unittest.main()
