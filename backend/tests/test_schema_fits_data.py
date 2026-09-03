"""Помещаются ли данные магазина в объявленную схему.

Появился после трёх подряд отказов на PostgreSQL, которых тесты на
SQLite не увидели. Причина у них одна: **SQLite почти ничего не
проверяет**. Он не следит за длиной `varchar`, спокойно кладёт строку
в числовую колонку и по умолчанию не проверяет внешние ключи.
PostgreSQL проверяет всё это — и отказывает в момент переноса, когда
данных уже жалко.

Здесь проверка идёт по объявлениям колонок, без всякой базы: каждое
значение из `data/store.json` сверяется с типом, длиной и
обязательностью колонки, куда оно ляжет. Это ловит целый класс
расхождений заранее — например длинное описание товара, которое
завтра допишет администратор.

Чего этот набор не заменяет: прогон переноса на настоящем PostgreSQL.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

try:
    from sqlalchemy import Boolean, Float, Integer, String, Text
    from backend.app.db import models as M
    from backend.app.db import migrate_json as MJ
    HAS_SA = True
except Exception:  # noqa: BLE001
    HAS_SA = False

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "data" / "store.json"

# Коллекция JSON → таблица. Вложенные структуры (корзина, брони,
# настройки) сюда не входят: они раскладываются построителями, и их
# проверяют тесты переноса.
COLLECTIONS = {
    "categories": "categories", "products": "products",
    "delivery_zones": "delivery_zones", "promocodes": "promocodes",
    "users": "users", "sessions": "sessions", "orders": "orders",
    "order_items": "order_items", "order_status_history": "order_status_history",
    "preorders": "preorders", "consents": "consents", "audit": "audit",
    "otp": "otp", "tg_links": "tg_links", "promocode_usages": "promocode_usages",
}

# Поля, которые намеренно живут в отдельных таблицах, а не в колонках.
MOVED_OUT = {"sessions": {"cart", "recent_orders", "recent_preorders"}}


@unittest.skipUnless(HAS_SA, "SQLAlchemy не установлен")
@unittest.skipUnless(STORE.exists(), "нет data/store.json")
class SchemaFitsData(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.store = json.loads(STORE.read_text(encoding="utf-8"))
        cls.tables = {t.name: t for t in M.ALL_TABLES}

    def _rows(self, collection: str):
        return self.store.get(collection) or []

    def test_strings_fit_declared_length(self):
        """PostgreSQL обрежет? Нет — откажет. SQLite бы промолчал."""
        problems = []
        for collection, table_name in COLLECTIONS.items():
            table = self.tables[table_name]
            for row in self._rows(collection):
                for key, value in row.items():
                    col = table.columns.get(key)
                    if col is None or not isinstance(value, str):
                        continue
                    limit = getattr(col.type, "length", None)
                    if limit and len(value) > limit:
                        problems.append(
                            f"{table_name}.{key}: {len(value)} символов при пределе "
                            f"{limit} (запись {row.get('id')})")
        self.assertEqual(problems, [], "значения не влезают в колонки")

    def test_required_columns_are_never_null(self):
        problems = []
        for collection, table_name in COLLECTIONS.items():
            table = self.tables[table_name]
            for row in self._rows(collection):
                for key, value in row.items():
                    col = table.columns.get(key)
                    if col is not None and value is None and not col.nullable:
                        problems.append(f"{table_name}.{key}: null в обязательной колонке "
                                        f"(запись {row.get('id')})")
        self.assertEqual(problems, [], "пустые значения в обязательных колонках")

    def test_value_types_match_columns(self):
        """Строка в числовой колонке: SQLite примет, PostgreSQL откажет."""
        allowed = {String: (str,), Text: (str,), Integer: (int,),
                   Float: (int, float), Boolean: (bool,)}
        problems = []
        for collection, table_name in COLLECTIONS.items():
            table = self.tables[table_name]
            for row in self._rows(collection):
                for key, value in row.items():
                    col = table.columns.get(key)
                    if col is None or value is None:
                        continue
                    types = next((v for k, v in allowed.items()
                                  if isinstance(col.type, k)), None)
                    if types is None:
                        continue
                    # bool в Python — подкласс int, поэтому отдельно
                    if isinstance(col.type, (Integer, Float)) and isinstance(value, bool):
                        problems.append(f"{table_name}.{key}: булево в числовой колонке")
                    elif not isinstance(value, types):
                        problems.append(
                            f"{table_name}.{key}: {type(value).__name__} вместо "
                            f"{type(col.type).__name__} (запись {row.get('id')})")
        self.assertEqual(problems, [], "значения не того типа")

    def test_no_field_is_silently_dropped(self):
        """Каждое поле JSON либо имеет колонку, либо вынесено осознанно.

        Без этой проверки поле, добавленное в данные, тихо не доезжало
        бы до базы: перенос молчит, схема выглядит полной, а значение
        пропадает.
        """
        problems = []
        for collection, table_name in COLLECTIONS.items():
            columns = set(self.tables[table_name].columns.keys())
            moved = MOVED_OUT.get(collection, set())
            present: set[str] = set()
            for row in self._rows(collection):
                present |= set(row)
            unknown = present - columns - moved
            if unknown:
                problems.append(f"{collection}: некуда положить {sorted(unknown)}")
        self.assertEqual(problems, [], "поля теряются при переносе")

    def test_foreign_keys_point_at_existing_rows(self):
        """SQLite по умолчанию внешние ключи не проверяет, PostgreSQL — да."""
        ids = {name: {r.get("id") for r in self._rows(name)}
               for name in ("products", "orders", "users", "preorders", "promocodes")}
        ids["categories"] = {c.get("id") for c in self._rows("categories")}
        ids["delivery_zones"] = {z.get("id") for z in self._rows("delivery_zones")}

        checks = [
            ("products", "category_id", "categories"),
            ("orders", "user_id", "users"),
            ("orders", "delivery_zone_id", "delivery_zones"),
            ("orders", "promocode_id", "promocodes"),
            ("order_items", "order_id", "orders"),
            ("order_items", "product_id", "products"),
            ("order_status_history", "order_id", "orders"),
            ("preorders", "product_id", "products"),
            ("consents", "order_id", "orders"),
            ("consents", "preorder_id", "preorders"),
        ]
        problems = []
        for collection, field, target in checks:
            for row in self._rows(collection):
                value = row.get(field)
                if value is not None and value not in ids[target]:
                    problems.append(f"{collection}.{field}={value!r} — такой записи "
                                    f"в {target} нет (строка {row.get('id')})")
        self.assertEqual(problems, [], "ссылки в никуда")

    def test_sequence_tables_have_integer_keys(self):
        """Счётчик правится только у числовых ключей.

        Проверка того же класса: у категорий ключ текстовый, и попытка
        подвинуть за ними счётчик кончается в PostgreSQL отказом о
        несовместимых типах — на SQLite шаг не выполняется вовсе.
        """
        for name in MJ.sequence_tables():
            col = self.tables[name].columns["id"]
            self.assertIs(col.type.python_type, int, f"{name}.id не число")


if __name__ == "__main__":
    unittest.main()
