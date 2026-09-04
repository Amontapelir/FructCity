"""Несколько фотографий на товар (ТЗ 2.2.4, 13; ROADMAP 2.11).

Обложка (`products.image_key`) не трогается — новое здесь только
дополнительные фото, отдельная плоская таблица `product_images`,
пишется тем же диффом снимков, что и `order_items` (`db/uow.py`), а на
чтении группируется по товару (`db/repository.py::_attach_extra_images`)
и склеивается в одну галерею (`catalog.py::image_keys`).

Уровни проверки снизу вверх: чистая склейка галереи → чистая пересборка
списка при сохранении → круг через настоящую SQLite-базу (диф пишет,
чтение группирует).
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from backend.app.domain import admin as AD
from backend.app.domain import catalog as K
from backend.app.domain import validate as V

try:
    from backend.app.db import migrate_json as MJ
    from backend.app.db import models as M
    from backend.app.db import repository as R
    from backend.app.db import uow as U
    HAS_SA = True
except Exception:  # noqa: BLE001
    HAS_SA = False

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "data" / "store.json"


class ImageKeysView(unittest.TestCase):
    """`catalog.image_keys` — обложка первой, затем дополнительные."""

    def test_cover_and_extra_combined_in_order(self):
        p = {"image_key": "avocado", "extra_images": ["mango", "banana"]}
        self.assertEqual(K.image_keys(p), ["avocado", "mango", "banana"])

    def test_no_cover_keeps_only_extra(self):
        p = {"image_key": None, "extra_images": ["mango"]}
        self.assertEqual(K.image_keys(p), ["mango"])

    def test_missing_extra_images_field_does_not_crash(self):
        """`extra_images` кладёт repository.py при чтении из базы — в
        прямом вызове (тесты, синтетика) его может не быть вовсе."""
        self.assertEqual(K.image_keys({"image_key": "avocado"}), ["avocado"])

    def test_public_product_exposes_image_keys(self):
        p = {"id": 1, "sku": "X", "slug": "x", "name": "Тест", "type": "unit",
            "price": 100, "vat_rate": 10, "stock": 1,
            "image_key": "avocado", "extra_images": ["mango"], "emoji": "📦"}
        view = K.public_product(p)
        self.assertEqual(view["image_keys"], ["avocado", "mango"])
        self.assertEqual(view["image_key"], "avocado", "старое поле не должно исчезнуть")


class ReplaceProductImages(unittest.TestCase):
    """`admin.replace_product_images` — чистая пересборка плоского списка."""

    def test_adds_rows_for_new_product(self):
        out = AD.replace_product_images([], product_id=1, keys=["mango", "banana"],
                                        next_id=lambda k: {"product_images": 10}[k])
        self.assertEqual([r["image_key"] for r in out], ["mango", "banana"])
        self.assertEqual([r["sort_order"] for r in out], [0, 1])
        self.assertTrue(all(r["product_id"] == 1 for r in out))

    def test_replaces_only_target_product(self):
        existing = [{"id": 1, "product_id": 1, "image_key": "old", "sort_order": 0},
                   {"id": 2, "product_id": 2, "image_key": "untouched", "sort_order": 0}]
        out = AD.replace_product_images(existing, product_id=1, keys=["new"],
                                        next_id=lambda k: 99)
        self.assertEqual(len(out), 2)
        other = next(r for r in out if r["product_id"] == 2)
        self.assertEqual(other["image_key"], "untouched", "чужой товар задет не должен быть")
        mine = next(r for r in out if r["product_id"] == 1)
        self.assertEqual(mine["image_key"], "new")

    def test_empty_keys_clears_the_list(self):
        """Подмена: без полной пересборки список остался бы прежним при пустом keys."""
        existing = [{"id": 1, "product_id": 1, "image_key": "old", "sort_order": 0}]
        out = AD.replace_product_images(existing, product_id=1, keys=[], next_id=lambda k: 1)
        self.assertEqual(out, [])


class ExtraImageKeysSchema(unittest.TestCase):

    def test_missing_defaults_to_empty_list(self):
        out = V.validate(V.SCHEMAS["product"], {
            "sku": "XX1", "name": "Тест", "slug": "", "category_id": "c", "type": "unit",
            "price": "10", "price_per_kg": "0", "vat_rate": "10", "stock": "1",
            "image_key": "",
        })
        self.assertEqual(out["extra_image_keys"], [])

    def test_too_many_rejected(self):
        errs = {}
        V.SCHEMAS["product"]["extra_image_keys"](["a"] * 9, "extra_image_keys", errs)
        self.assertTrue(errs, "список из 9 элементов не должен пройти при max=8")

    def test_bad_key_pattern_rejected(self):
        errs = {}
        V.SCHEMAS["product"]["extra_image_keys"](["Not Valid!"], "extra_image_keys", errs)
        self.assertTrue(errs, "ключ с пробелом и заглавными буквами не должен пройти")


@unittest.skipUnless(HAS_SA, "SQLAlchemy не установлен")
@unittest.skipUnless(STORE.exists(), "нет data/store.json")
class DatabaseRoundTrip(unittest.TestCase):
    """Диф пишет плоскую коллекцию, чтение группирует её обратно по товару."""

    @classmethod
    def setUpClass(cls):
        from backend.app.db.store import reconcile

        cls.original = reconcile(json.loads(STORE.read_text(encoding="utf-8")))

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fructcity-product-images-"))
        self.engine = M.get_engine(f"sqlite:///{self.tmp / 'test.db'}")
        M.create_all(self.engine)
        MJ.migrate(self.engine, self.original)

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_extra_images_survive_the_round_trip_and_are_grouped_by_product(self):
        with U.transaction(self.engine, lock=False) as unit:
            product = unit.state["products"][0]
            other = unit.state["products"][1]
            unit.state["product_images"] = AD.replace_product_images(
                unit.state.get("product_images") or [], product_id=product["id"],
                keys=["mango", "banana"], next_id=unit.next_id)

        after = R.load_state(self.engine)
        mine = next(p for p in after["products"] if p["id"] == product["id"])
        untouched = next(p for p in after["products"] if p["id"] == other["id"])
        self.assertEqual(mine["extra_images"], ["mango", "banana"])
        self.assertEqual(untouched["extra_images"], [], "у чужого товара фото взяться неоткуда")
        self.assertEqual(K.image_keys(mine), [mine["image_key"], "mango", "banana"])

    def test_replacing_again_drops_the_old_rows(self):
        """Подмена сути full-replace: без удаления старых строк здесь
        осталось бы три фото вместо одного."""
        with U.transaction(self.engine, lock=False) as unit:
            pid = unit.state["products"][0]["id"]
            unit.state["product_images"] = AD.replace_product_images(
                [], product_id=pid, keys=["a", "b"], next_id=unit.next_id)
        with U.transaction(self.engine, lock=False) as unit:
            pid = unit.state["products"][0]["id"]
            unit.state["product_images"] = AD.replace_product_images(
                unit.state.get("product_images") or [], product_id=pid,
                keys=["only-one"], next_id=unit.next_id)

        after = R.load_state(self.engine)
        mine = next(p for p in after["products"] if p["id"] == pid)
        self.assertEqual(mine["extra_images"], ["only-one"])


if __name__ == "__main__":
    unittest.main()
