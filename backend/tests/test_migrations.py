"""Миграции схемы: обвязка на месте, модели и ревизии не разъехались.

Главная проверка — последняя: база, собранная миграциями, обязана иметь
те же таблицы и колонки, что описаны в `models.py`. Без неё alembic
превращается в украшение: кто-то добавляет поле в модель, забывает
ревизию, на своей машине всё работает (`create_all` в тестах строит
схему прямо из моделей), а на боевой базе колонки нет — и падает уже
там, при первом запросе.

Проверка идёт на SQLite: она про состав схемы, а не про типы. Сверять
типы на SQLite бессмысленно — он их почти не различает; для этого есть
`test_schema_fits_data.py` и настоящий PostgreSQL.
"""

from __future__ import annotations

import configparser
import shutil
import tempfile
import unittest
from pathlib import Path

from backend.tests.envtools import NO_DATABASE, isolated_env
from backend.tests.paths import ROOT

INI = ROOT / "alembic.ini"
ALEMBIC_DIR = ROOT / "backend" / "alembic"
VERSIONS = ALEMBIC_DIR / "versions"

try:
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    from backend.app.db.models import Base
    HAS_ALEMBIC = True
except Exception:  # noqa: BLE001
    HAS_ALEMBIC = False


def revision_files() -> list[Path]:
    if not VERSIONS.exists():
        return []
    return sorted(p for p in VERSIONS.glob("*.py") if not p.name.startswith("__"))


class AlembicSetup(unittest.TestCase):
    """Обвязка: файлы на месте, пароль не продублирован."""

    def test_ini_exists_and_points_at_backend_alembic(self):
        self.assertTrue(INI.exists(), "нет alembic.ini в корне проекта")
        parser = configparser.ConfigParser()
        parser.read(INI, encoding="utf-8")
        self.assertEqual(parser["alembic"]["script_location"], "backend/alembic")

    def test_ini_has_no_connection_string(self):
        """Пароль от боевой базы живёт в одном месте — `.env`.

        `alembic.ini` обычно кладут в репозиторий; строка подключения в
        нём означает либо пароль в истории правок, либо два источника
        правды, которые однажды разойдутся.
        """
        text = INI.read_text(encoding="utf-8")
        for line in text.splitlines():
            bare = line.strip()
            if bare.startswith("#") or not bare:
                continue
            self.assertFalse(bare.startswith("sqlalchemy.url"),
                             "строка подключения не должна лежать в alembic.ini")

    def test_env_takes_url_from_settings(self):
        env = (ALEMBIC_DIR / "env.py").read_text(encoding="utf-8")
        self.assertIn("get_settings", env, "env.py обязан брать базу из настроек")
        self.assertIn("target_metadata", env, "без метаданных не работает autogenerate")

    def test_env_does_not_create_schema_itself(self):
        """Схему создаёт миграция, иначе база собрана мимо истории.

        Проверяем ВЫЗОВ, а не наличие строки: в docstring `env.py` этот
        запрет описан словами, и поиск по тексту падал на собственном
        объяснении. Разбор синтаксиса отличает «сделано» от «написано,
        что так делать нельзя».
        """
        import ast

        tree = ast.parse((ALEMBIC_DIR / "env.py").read_text(encoding="utf-8"))
        calls = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Call)
                 and getattr(node.func, "attr", None) == "create_all"]
        self.assertFalse(calls, "env.py не должен создавать таблицы сам")

    def test_script_template_exists(self):
        """Без шаблона `alembic revision` создаёт файл без наших правил."""
        self.assertTrue((ALEMBIC_DIR / "script.py.mako").exists())


@unittest.skipUnless(HAS_ALEMBIC, "alembic или SQLAlchemy не установлены")
class MigrationsMatchModels(unittest.TestCase):

    def setUp(self):
        if not revision_files():
            self.skipTest(
                "базовая ревизия ещё не создана — см. backend/alembic/versions/README.md "
                "(`alembic revision --autogenerate -m \"baseline\"` + `alembic stamp head`)")
        self.tmp = Path(tempfile.mkdtemp(prefix="fructcity-alembic-"))

    def tearDown(self):
        if hasattr(self, "tmp"):
            shutil.rmtree(self.tmp, ignore_errors=True)

    def test_upgrade_head_builds_the_schema_models_describe(self):
        url = f"sqlite:///{self.tmp / 'migrated.db'}"

        # DATABASE_URL перекрывает FC_DB_*: env.py возьмёт именно его и
        # не пойдёт в базу разработчика.
        with isolated_env(**{**NO_DATABASE, "DATABASE_URL": url}):
            cfg = Config(str(INI))
            # `script_location` в ini задан относительным путём и
            # разрешается от текущего каталога. Тесты обычно запускают из
            # корня, но не обязаны — подставляем абсолютный.
            cfg.set_main_option("script_location", str(ALEMBIC_DIR))
            command.upgrade(cfg, "head")

        engine = create_engine(url, future=True)
        try:
            inspector = inspect(engine)
            built = set(inspector.get_table_names()) - {"alembic_version"}
            described = set(Base.metadata.tables)

            missing = sorted(described - built)
            extra = sorted(built - described)
            self.assertFalse(missing,
                             f"в моделях есть таблицы, которых миграции не создают: {missing}")
            self.assertFalse(extra,
                             f"миграции создают таблицы, которых нет в моделях: {extra}")

            for name in sorted(described):
                with self.subTest(table=name):
                    in_db = {c["name"] for c in inspector.get_columns(name)}
                    in_models = set(Base.metadata.tables[name].columns.keys())
                    self.assertEqual(
                        in_db, in_models,
                        f"таблица {name}: колонки в базе и в моделях разошлись — "
                        "не хватает ревизии")
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
