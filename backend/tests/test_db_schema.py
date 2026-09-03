"""Проверка схемы и управления базой — на SQLite, без Postgres.

Смысл: убедиться, что модели вообще превращаются в рабочий DDL и что
создание, очистка и проверка делают то, что обещают. Для этого
настоящий сервер не нужен — SQLAlchemy соберёт те же таблицы во
временном файле SQLite.

Чего этот набор НЕ проверяет: диалектные мелочи Postgres. SQLite,
например, по умолчанию не следит за внешними ключами. Поэтому тест
подтверждает, что схема осмысленна и код управления работает, но не
заменяет прогон на настоящей базе.

Пропускается, если не установлен SQLAlchemy.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

try:
    from sqlalchemy import insert, inspect
    from backend.app.db import models as M
    HAS_SA = True
except Exception:  # noqa: BLE001
    HAS_SA = False


@unittest.skipUnless(HAS_SA, "SQLAlchemy не установлен")
class DbSchema(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fructcity-db-"))
        self.engine = M.get_engine(f"sqlite:///{self.tmp / 'test.db'}")

    def tearDown(self):
        self.engine.dispose()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- создание -----------------------------------------------------------
    def test_create_all_makes_every_table(self):
        created = M.create_all(self.engine)
        self.assertEqual(sorted(created), sorted(M.TABLE_NAMES),
                         "созданы не все таблицы схемы")
        self.assertEqual(M.missing_tables(self.engine), [])
        self.assertTrue(M.is_ready(self.engine))

    def test_create_all_is_idempotent(self):
        """Повторный вызов ничего не создаёт и не ломает.

        Это важно: строка создания остаётся в запуске приложения, и
        если бы второй вызов падал, сервер не поднимался бы после
        первого же перезапуска.
        """
        M.create_all(self.engine)
        again = M.create_all(self.engine)
        self.assertEqual(again, [], "второй вызов что-то создал заново")
        self.assertTrue(M.is_ready(self.engine))

    def test_ensure_database_skips_sqlite(self):
        """Для SQLite отдельного шага создания базы нет — файл сам появится."""
        self.assertFalse(M.ensure_database(f"sqlite:///{self.tmp / 'x.db'}"))

    # -- проверка -----------------------------------------------------------
    def test_describe_on_empty_database(self):
        state = M.describe(self.engine)
        self.assertTrue(state["connected"])
        self.assertFalse(state["ready"], "пустая база не может быть готовой")
        self.assertEqual(sorted(state["missing"]), sorted(M.TABLE_NAMES))
        self.assertNotIn("пароль", state["url"])

    def test_describe_hides_password(self):
        """Строка подключения печатается в консоль — пароля в ней быть не должно."""
        engine = M.get_engine("postgresql+psycopg://user:s3cret@localhost:5432/db")
        try:
            self.assertNotIn("s3cret", M._safe_url(engine))
        finally:
            engine.dispose()

    def test_describe_counts_rows(self):
        M.create_all(self.engine)
        with self.engine.begin() as conn:
            conn.execute(insert(M.Category.__table__),
                         [{"id": "fruit", "name": "Фрукты", "sort_order": 1}])
            conn.execute(insert(M.Product.__table__), [{
                "id": 1, "sku": "A-1", "slug": "a-1", "name": "Яблоки",
                "category_id": "fruit", "type": "weighted", "price": 0,
                "price_per_kg": 120, "stock": 10,
            }])
        state = M.describe(self.engine)
        self.assertTrue(state["ready"])
        self.assertEqual(state["tables"]["categories"], 1)
        self.assertEqual(state["tables"]["products"], 1)
        self.assertEqual(state["tables"]["orders"], 0)

    def test_missing_table_is_noticed(self):
        """Неполная схема — это не «готова».

        Прерванное создание оставляет часть таблиц, и приложение с
        такой базой падает на первом же запросе к недостающей. Лучше
        узнать об этом от проверки.
        """
        M.create_all(self.engine)
        with self.engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE order_items")
        self.assertIn("order_items", M.missing_tables(self.engine))
        self.assertFalse(M.is_ready(self.engine))

    # -- очистка ------------------------------------------------------------
    def test_clear_all_removes_rows_but_keeps_schema(self):
        M.create_all(self.engine)
        with self.engine.begin() as conn:
            conn.execute(insert(M.Category.__table__),
                         [{"id": "fruit", "name": "Фрукты"}])
            conn.execute(insert(M.Product.__table__), [{
                "id": 1, "sku": "A-1", "slug": "a-1", "name": "Яблоки",
                "category_id": "fruit", "type": "unit", "price": 99,
                "price_per_kg": 0, "stock": 5,
            }])

        removed = M.clear_all(self.engine)
        self.assertEqual(removed["products"], 1)
        self.assertEqual(removed["categories"], 1)
        self.assertTrue(M.is_ready(self.engine), "очистка снесла схему")
        self.assertEqual(M.describe(self.engine)["tables"]["products"], 0)

    def test_clear_all_respects_foreign_keys(self):
        """Порядок удаления — обратный порядку зависимостей.

        Товар ссылается на категорию; если удалять в прямом порядке,
        Postgres не даст убрать категорию, на которую ещё ссылаются.
        SQLite такое пропустит, поэтому здесь проверяется сам порядок,
        а не отсутствие ошибки.
        """
        names = [t.name for t in reversed(M.ALL_TABLES)]
        self.assertLess(names.index("products"), names.index("categories"),
                        "товары должны удаляться раньше категорий")
        self.assertLess(names.index("order_items"), names.index("orders"),
                        "позиции заказа должны удаляться раньше заказов")

    def test_clear_all_on_empty_database_does_not_fail(self):
        M.create_all(self.engine)
        self.assertEqual(sum(M.clear_all(self.engine).values()), 0)

    # -- снос ---------------------------------------------------------------
    def test_drop_all_removes_schema(self):
        M.create_all(self.engine)
        dropped = M.drop_all(self.engine)
        self.assertEqual(sorted(dropped), sorted(M.TABLE_NAMES))
        self.assertEqual(sorted(M.existing_tables(self.engine)), [])
        self.assertFalse(M.is_ready(self.engine))

    # -- соответствие хранилищу --------------------------------------------
    def test_schema_covers_store_collections(self):
        """Каждая коллекция store.json должна иметь место в схеме.

        Иначе перенос данных тихо потеряет часть магазина, а заметится
        это на восстановлении из резервной копии — то есть в худший из
        возможных моментов.
        """
        expected = {
            "users", "sessions", "otp", "categories", "products", "orders",
            "order_items", "order_status_history", "preorders", "promocodes",
            "promocode_usages", "delivery_zones", "slot_bookings",
            "meat_bookings", "tg_links", "consents", "settings", "audit",
        }
        missing = expected - set(M.TABLE_NAMES)
        self.assertEqual(missing, set(), f"в схеме нет таблиц под: {missing}")

    def test_money_is_integer_everywhere(self):
        """Деньги — целые рубли во всём проекте, включая базу.

        Дробные суммы в базе означали бы второй источник правды об
        округлении: расчётное ядро считает в целых, и разойтись они
        обязаны рано или поздно.
        """
        money_columns = [
            (M.Order.__table__, ["items_total", "total", "discount_amount",
                                 "delivery_cost", "agreed_delivery_cost",
                                 "delivery_discount", "hold_amount", "planned_total"]),
            (M.OrderItem.__table__, ["price_at_purchase"]),
            (M.Product.__table__, ["price", "price_per_kg", "sale_price"]),
            (M.Preorder.__table__, ["price_per_kg", "estimate"]),
        ]
        for table, names in money_columns:
            for name in names:
                col = table.columns[name]
                self.assertEqual(col.type.python_type, int,
                                 f"{table.name}.{name} — не целое число")

    def test_weight_is_fractional(self):
        """А вес, наоборот, обязан быть дробным: шаг 0.5 кг."""
        for table, name in [(M.OrderItem.__table__, "requested_weight"),
                            (M.OrderItem.__table__, "actual_weight"),
                            (M.Preorder.__table__, "requested_weight"),
                            (M.Product.__table__, "stock")]:
            self.assertEqual(table.columns[name].type.python_type, float,
                             f"{table.name}.{name} — не дробное")

    def test_zone_cost_allows_null(self):
        """NULL в стоимости зоны — это «расчёт вручную», а не «бесплатно».

        Если сделать колонку обязательной, зону из ТЗ 5.2 придётся
        записать нулём, и доставка в неё станет бесплатной.
        """
        self.assertTrue(M.DeliveryZone.__table__.columns["cost"].nullable)

    def test_indexes_on_hot_columns(self):
        """Поля, по которым ищут в админке, должны быть проиндексированы."""
        M.create_all(self.engine)
        insp = inspect(self.engine)
        for table, column in [("orders", "status"), ("orders", "phone"),
                              ("products", "slug"), ("products", "sku"),
                              ("preorders", "pickup_date")]:
            indexed = {c for idx in insp.get_indexes(table) for c in idx["column_names"]}
            unique = {c for uc in insp.get_unique_constraints(table)
                      for c in uc["column_names"]}
            self.assertTrue(column in indexed or column in unique,
                            f"{table}.{column} без индекса")


class _FakeDbError(Exception):
    """Ошибка драйвера с кодом состояния — как её отдаёт psycopg."""

    def __init__(self, sqlstate: str):
        super().__init__(f"ошибка с кодом {sqlstate}")
        self.sqlstate = sqlstate


@unittest.skipUnless(HAS_SA, "SQLAlchemy не установлен")
class Diagnostics(unittest.TestCase):
    """Разбор причины, когда подключиться не удалось.

    Текст ошибки от PostgreSQL на русской Windows приходит в кодировке
    системы и до приложения доезжает нечитаемым — по нему причину не
    определить. Поэтому разбор опирается на проверку сокетом, а она от
    кодировок не зависит.
    """

    def test_closed_port_is_detected(self):
        import socket

        with socket.socket() as s:          # занимаем порт и сразу отпускаем
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.assertFalse(M.port_open("127.0.0.1", port, timeout=0.5))

    def test_open_port_is_detected(self):
        import socket

        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        try:
            self.assertTrue(M.port_open("127.0.0.1", srv.getsockname()[1], timeout=1.0))
        finally:
            srv.close()

    def test_hint_when_server_is_down(self):
        """Порт закрыт — значит служба не запущена, а не пароль неверный."""
        hints = "\n".join(M.diagnose("postgresql+psycopg://u:p@127.0.0.1:1/db"))
        self.assertIn("не отвечает", hints)
        self.assertIn("PostgreSQL", hints)
        self.assertNotIn("пароль", hints.replace("FC_DB_PASSWORD", ""))

    def test_missing_database_is_not_reported_as_wrong_password(self):
        """Разбор по коду состояния, а не по тексту ошибки.

        До первого --create базы ещё нет, и подключение отказывает с
        кодом 3D000. Разбор по тексту тут бессилен: на русской Windows
        сообщение приходит нечитаемым — и подсказка отправляла бы
        проверять правильный пароль.
        """
        err = _FakeDbError(M.SQLSTATE_NO_DATABASE)
        hints = "\n".join(M.diagnose("postgresql+psycopg://u:p@127.0.0.1:5432/fructcity", err))
        self.assertIn("базы «fructcity» нет", hints)
        self.assertIn("--create", hints)
        # Проверяем не наличие слова «пароль» — сообщение как раз
        # сообщает, что пароль подошёл, и это правильно. Проверяем,
        # что человека не отправляют чинить пароль.
        self.assertNotIn("FC_DB_PASSWORD", hints, "советует править пароль")
        self.assertNotIn("не подошёл", hints)

    def test_bad_password_is_recognised_by_code(self):
        err = _FakeDbError(M.SQLSTATE_BAD_PASSWORD)
        hints = "\n".join(M.diagnose("postgresql+psycopg://postgres:p@127.0.0.1:5432/db", err))
        self.assertIn("пароль", hints)
        self.assertIn("postgres", hints)
        self.assertIn("FC_DB_PASSWORD", hints)

    def test_hint_when_server_answers_without_code(self):
        """Порт открыт, кода состояния нет — перечисляем обе причины."""
        import socket

        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        try:
            hints = "\n".join(
                M.diagnose(f"postgresql+psycopg://postgres:p@127.0.0.1:{srv.getsockname()[1]}/db"))
            self.assertIn("пароль", hints)
            self.assertIn("--create", hints)
            self.assertIn("кодировке", hints, "не объяснено нечитаемое сообщение сервера")
        finally:
            srv.close()

    def test_sqlstate_is_read_from_wrapped_error(self):
        """Код достаётся и из обёртки SQLAlchemy, где он лежит в .orig."""
        inner = _FakeDbError(M.SQLSTATE_NO_DATABASE)
        wrapper = RuntimeError("обёртка")
        wrapper.orig = inner
        self.assertEqual(M._sqlstate(wrapper), M.SQLSTATE_NO_DATABASE)

    def test_describe_includes_hint(self):
        state = M.describe(M.get_engine("postgresql+psycopg://u:p@127.0.0.1:1/nope"))
        self.assertFalse(state["connected"])
        self.assertTrue(state["hint"], "разбор причины не попал в результат проверки")


if __name__ == "__main__":
    unittest.main()
