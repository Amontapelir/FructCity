"""Отставшая база не должна обслуживать витрину.

Механизм родом из времени, когда в `store.json` писал Node: после
первой же покупки база оставалась со старым каталогом и остатками, но
выглядела рабочей — питоновская версия показала бы товар, которого уже
нет. Перенос поэтому кладёт в базу отметку о снимке файла, а выбор
источника её проверяет: файл переписан — читаем файл.

После переезда (1.6/1.7) при включённом `FC_WRITE_ENABLED` эта сверка
не выполняется вовсе: база и есть источник истины, сверять её с файлом,
который она заменила, незачем. Проверки ниже поэтому идут при явно
выключенном флаге — иначе они проверяли бы не тот режим.

Всё на SQLite, PostgreSQL не нужен.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from backend.tests.envtools import NO_DATABASE, isolated_env
from backend.tests.paths import ROOT, STORE  # noqa: F401 — ROOT ждут прежние тесты

try:
    from backend.app.db import migrate_json as MJ
    from backend.app.db import models as M
    HAS_SA = True
except Exception:  # noqa: BLE001
    HAS_SA = False


@unittest.skipUnless(HAS_SA, "SQLAlchemy не установлен")
@unittest.skipUnless(STORE.exists(), "нет data/store.json")
class SnapshotMarker(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fructcity-stale-"))
        self.store_path = self.tmp / "store.json"
        shutil.copy(STORE, self.store_path)
        self.state = MJ.load_store(self.store_path)

        self.engine = M.get_engine(f"sqlite:///{self.tmp / 'test.db'}")
        M.create_all(self.engine)
        MJ.migrate(self.engine, self.state, snapshot=self.store_path)

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_marker_is_written(self):
        marker = MJ.read_snapshot(self.engine)
        self.assertIsNotNone(marker, "перенос не оставил отметки о снимке")
        self.assertEqual(marker["size"], self.store_path.stat().st_size)

    def test_marker_matches_untouched_file(self):
        now = MJ.snapshot_marker(self.store_path)
        marker = MJ.read_snapshot(self.engine)
        self.assertEqual((now["size"], now["mtime_ns"]),
                         (marker["size"], marker["mtime_ns"]))

    def test_marker_differs_after_file_changes(self):
        """Файл дописали после переноса — отметка обязана разойтись."""
        data = json.loads(self.store_path.read_text(encoding="utf-8"))
        data["orders"].append({"id": 999, "number": 999})
        self.store_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        now = MJ.snapshot_marker(self.store_path)
        marker = MJ.read_snapshot(self.engine)
        self.assertNotEqual((now["size"], now["mtime_ns"]),
                            (marker["size"], marker["mtime_ns"]))

    def test_marker_is_not_a_shop_setting(self):
        """Служебная отметка не должна утечь в настройки магазина.

        Иначе она уехала бы в ответ API и показалась администратору
        в списке настроек магазина наравне с телефоном и адресом.
        """
        from backend.app.db import repository as R

        restored = R.load_state(self.engine)
        self.assertNotIn(MJ.SNAPSHOT_KEY, restored["settings"])
        for key in restored["settings"]:
            self.assertFalse(str(key).startswith("_"), f"служебный ключ {key} наружу")

    def test_verify_ignores_the_marker(self):
        """Сверка не должна считать отметку расхождением."""
        self.assertEqual(MJ.verify(self.engine, self.state), [])


@unittest.skipUnless(HAS_SA, "SQLAlchemy не установлен")
@unittest.skipUnless(STORE.exists(), "нет data/store.json")
class SourceChoice(unittest.TestCase):
    """Выбор источника с учётом отставания."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fructcity-source-"))
        self.store_path = self.tmp / "store.json"
        shutil.copy(STORE, self.store_path)

        # Базу гасим ЯВНЫМИ пустыми значениями, а не отсутствием
        # переменных: `Settings` читает `.env`, где у разработчика лежат
        # рабочие `FC_DB_*` и `FC_WRITE_ENABLED=1`. Без этого набор с
        # именем «без базы» шёл в настоящую базу и проходил по
        # случайности — пока снимок был отставшим, `current_source()`
        # отвечал «json» по совсем другой причине. Подробности —
        # `envtools.py`.
        self._env = isolated_env(FC_DATA_DIR=str(self.tmp), **NO_DATABASE)
        self._env.__enter__()

    def tearDown(self):
        self._env.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_without_database_source_is_json(self):
        from backend.app.db import source

        source.reset()
        self.assertEqual(source.current_source(), "json")
        status = source.snapshot_status()
        self.assertFalse(status["stale"], "без базы отставать нечему")

    def test_state_is_readable_from_json(self):
        from backend.app.db import source

        source.reset()
        state = source.read_state()
        self.assertTrue(state["products"], "каталог не прочитался из файла")


if __name__ == "__main__":
    unittest.main()
