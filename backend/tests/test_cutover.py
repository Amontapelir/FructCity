"""Проверки переноса данных в базу (`db/cutover.py`).

Гоняются без FastAPI и без базы: здесь проверяются защиты ПЕРЕД
переносом — не пишет ли кто-то в файл, сделана ли резервная копия — и
то, что скрипт отказывается работать, когда работать нельзя. Сам
перенос и сверка проверяются в `test_migrate_json.py`; здесь важна не
запись, а условия, при которых её нельзя начинать.

Почему это отдельный набор: ошибка в этих проверках не видна ни по
одному тесту переноса — данные перенесутся, сверка сойдётся, а потерян
будет заказ, оформленный между снимком и переключением флага.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from backend.app.db import cutover as CO


class BackupNaming(unittest.TestCase):
    def test_backup_lands_next_to_the_original(self):
        store = Path("/srv/fructcity/data/store.json")
        name = CO.backup_name(store, datetime(2026, 9, 2, 14, 5, 6, tzinfo=timezone.utc))
        self.assertEqual(name.parent, store.parent,
                         "копию ищут там же, где данные, а не во временном каталоге")
        self.assertEqual(name.name, "store.json.before-cutover-20260902-140506")

    def test_two_backups_in_different_seconds_do_not_collide(self):
        store = Path("data/store.json")
        first = CO.backup_name(store, datetime(2026, 9, 2, 14, 5, 6, tzinfo=timezone.utc))
        second = CO.backup_name(store, datetime(2026, 9, 2, 14, 5, 7, tzinfo=timezone.utc))
        self.assertNotEqual(first, second, "повторный запуск не должен затирать копию")


class QuietCheck(unittest.TestCase):
    """«В файл никто не пишет» определяется по тому, меняется ли он."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fructcity-cutover-"))
        self.store = self.tmp / "store.json"
        self.store.write_text('{"products":[]}', encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_untouched_file_is_quiet(self):
        # sleep подменён: ждать по-настоящему в тесте незачем, проверяем
        # решение, а не таймер
        self.assertTrue(CO.store_is_quiet(self.store, seconds=0, sleep=lambda _s: None))

    def test_file_written_during_the_pause_is_not_quiet(self):
        def write_meanwhile(_seconds: float) -> None:
            # ровно то, что делает живой писатель: сохраняет состояние
            self.store.write_text('{"products":[1]}', encoding="utf-8")

        self.assertFalse(
            CO.store_is_quiet(self.store, seconds=0, sleep=write_meanwhile),
            "правка файла во время паузы означает, что в него ещё пишут")

    def test_missing_file_counts_as_quiet(self):
        """Нет файла — нечему меняться; отказ будет позже и по другой
        причине («переносить нечего»), с понятным человеку текстом."""
        self.assertTrue(CO.store_is_quiet(self.tmp / "нет.json", seconds=0,
                                          sleep=lambda _s: None))

    def test_backup_keeps_contents_and_mtime(self):
        copy = CO.backup_store(self.store)
        self.assertEqual(copy.read_text(encoding="utf-8"),
                         self.store.read_text(encoding="utf-8"))
        self.assertEqual(copy.stat().st_mtime_ns, self.store.stat().st_mtime_ns,
                         "copy2 обязан сохранить время правки: по нему отличают копии")


class RefusalPaths(unittest.TestCase):
    """Отказы до записи — без базы и без FastAPI."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fructcity-cutover-refuse-"))
        self.lines: list[str] = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, **kw) -> int:
        return CO.cutover(out=self.lines.append, **kw)

    def test_refuses_when_store_is_missing(self):
        self.assertEqual(self._run(store=self.tmp / "нет" / "store.json"), 2,
                         "без файла переносить нечего")
        self.assertTrue(any("переносить нечего" in s for s in self.lines))

    def test_refuses_while_node_still_writes(self):
        """Главная защита: перенос при живом писателе теряет заказы —
        оформленные между снимком и переключением флага, молча."""
        store = self.tmp / "store.json"
        store.write_text('{"products":[]}', encoding="utf-8")

        original_quiet = CO.store_is_quiet
        CO.store_is_quiet = lambda *_a, **_k: False        # как будто в файл пишут
        try:
            self.assertEqual(self._run(store=store), 1)
        finally:
            CO.store_is_quiet = original_quiet

        self.assertTrue(any("ОТКАЗ" in s for s in self.lines),
                        "отказ обязан быть виден человеку, а не только в коде возврата")
        self.assertFalse(list(self.tmp.glob("*before-cutover*")),
                         "резервная копия до отказа не создаётся — незачем")


if __name__ == "__main__":
    unittest.main()
