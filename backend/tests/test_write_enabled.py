"""Переключатель права записи `FC_WRITE_ENABLED`.

Три состояния, и различать их обязательно:

* флаг снят — изменяющие маршруты отвечают 503 `write_disabled`. Это
  режим «только чтение»: витрина работает, заказ не оформить;
* флаг стоит, а базы фактически нет — `store_unavailable`, а не тихий
  откат на запись в файл. Разные коды потому, что чинятся разным: один
  правкой настроек, другой поднятием базы;
* флаг стоит, база готова — запись проходит, и гостевая сессия
  переживает следующий запрос.

База здесь — SQLite во временном файле, не рабочий PostgreSQL:
проверяется сам переключатель, а не поведение типов после круга через
базу.

Пропускается, если не установлены FastAPI/SQLAlchemy/httpx.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

try:
    import httpx

    from backend.app.config import get_settings
    from backend.app.db import migrate_json as MJ
    from backend.app.db import models as M
    from backend.app.db import source as SRC
    from backend.app.main import create_app
    HAS_STACK = True
except Exception:  # noqa: BLE001
    HAS_STACK = False

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "data" / "store.json"

# Переменные, которые тест трогает и обязан вернуть как было — иначе
# следующий тест в том же процессе унаследует чужую базу или чужой флаг.
DB_ENV_KEYS = ("DATABASE_URL", "FC_DB_NAME", "FC_DB_HOST", "FC_DB_PASSWORD", "FC_DB_USER")
ENV_KEYS = ("FC_DATA_DIR", "FC_WRITE_ENABLED", *DB_ENV_KEYS)


@unittest.skipUnless(HAS_STACK, "не установлены FastAPI, SQLAlchemy или httpx")
@unittest.skipUnless(STORE.exists(), "нет data/store.json")
class WriteGate(unittest.IsolatedAsyncioTestCase):
    """DELETE /api/cart — простой изменяющий маршрут без прав персонала."""

    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fructcity-write-gate-"))
        self.data_dir = self.tmp / "data"
        self.data_dir.mkdir()
        self.store_path = self.data_dir / "store.json"
        shutil.copy(STORE, self.store_path)

        self.saved_env = {k: os.environ.get(k) for k in ENV_KEYS}
        os.environ["FC_DATA_DIR"] = str(self.data_dir)
        # "0", а не `pop()`: та же ловушка, что и с FC_DB_* ниже.
        # `pop()` снимает только переменную окружения, а настройки
        # читают ещё и `.env`, где в бою стоит FC_WRITE_ENABLED=1 — и
        # тест «флаг выключен» проверял бы включённый флаг. Пустая
        # строка не годится: булево поле её не разберёт.
        os.environ["FC_WRITE_ENABLED"] = "0"
        # Базу гасим явными пустыми значениями по той же причине: в
        # `.env` разработчика лежат рабочие FC_DB_*, и после `pop()`
        # тест «без базы» тихо писал бы в настоящую базу.
        for key in DB_ENV_KEYS:
            os.environ[key] = ""
        get_settings.cache_clear()
        SRC.reset()

    async def asyncTearDown(self):
        M.dispose_engines()
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()
        SRC.reset()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _delete_cart(self, app) -> httpx.Response:
        """Гостевой DELETE /api/cart с настоящим CSRF-хороводом.

        `check_csrf` требует совпадения cookie и заголовка плюс
        разрешённый Origin — без этого маршрут отказал бы раньше, чем
        успел бы проверить право записи, и тест проверял бы не то.

        Cookie сессии (`fc_sid`) гостю НЕ ставится на голом GET —
        `_resolve_session()` в `Ctx` намеренно её не сохраняет вне
        транзакции (см. docstring `context.py`). Она появляется только
        внутри `tx()`, из `live_session()` — значит забирать `sid`
        нужно после DELETE, а не после `bootstrap`.
        """
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            boot = await client.get("/api/bootstrap")
            token = boot.json()["csrf"]
            resp = await client.delete("/api/cart", headers={
                "X-CSRF-Token": token, "Origin": "http://test"})
            sid = client.cookies.get("fc_sid")
            return resp, sid

    async def test_write_disabled_by_default(self):
        """Без базы и без флага — старое поведение, без изменений."""
        resp, _ = await self._delete_cart(create_app())
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["error"], "write_disabled")

    async def test_flag_on_without_database_is_store_unavailable(self):
        """Флаг включён, а FC_DB_* не заполнены — писать всё равно некуда.

        Не `write_disabled` (это про сам режим) и не тихий откат на
        запись в файл — отдельный код, чтобы по ответу было понятно,
        что чинить: не «переключи флаг», а «настрой базу».
        """
        os.environ["FC_WRITE_ENABLED"] = "1"
        get_settings.cache_clear()
        resp, _ = await self._delete_cart(create_app())
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["error"], "store_unavailable")

    async def _ready_db(self):
        """Настраивает флаг и SQLite-базу, реально готовую к записи.

        Общая часть двух тестов ниже: без неё каждый повторял бы одни и
        те же пять строк переноса, а расхождение между копиями рано или
        поздно тестировало бы разные вещи под одинаковым названием.
        """
        sqlite_path = self.tmp / "test.db"
        os.environ["DATABASE_URL"] = f"sqlite:///{sqlite_path}"
        os.environ["FC_WRITE_ENABLED"] = "1"
        get_settings.cache_clear()
        SRC.reset()

        engine = M.get_engine()
        M.create_all(engine)
        state = MJ.load_store(self.store_path)
        MJ.migrate(engine, state, snapshot=self.store_path)
        SRC.reset()  # база заполнена уже после того, как _db() мог закэшировать «пусто»

        self.assertEqual(SRC.current_source(), "postgres",
                         "источник обязан стать базой сразу — сверка снимка тут отключена")
        return engine

    async def test_write_succeeds_when_flag_on_and_database_ready(self):
        """Флаг включён, база настроена и заполнена — запись реально проходит.

        Проверяется не только код ответа: сессия, которую создал и
        очистил запрос, читается ПОСЛЕ него напрямую из базы — иначе
        200 мог бы означать и «транзакция тихо ничего не сделала».
        """
        engine = await self._ready_db()

        resp, sid = await self._delete_cart(create_app())
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNotNone(sid, "гостевая сессия не завелась")

        from backend.app.db.repository import DbState

        persisted = DbState(engine).read()
        saved = next((s for s in persisted.get("sessions") or []
                     if s.get("sid") == sid), None)
        self.assertIsNotNone(saved, "сессия не нашлась в базе — запись не дошла до неё")
        self.assertEqual(saved.get("cart"), [], "корзина не очистилась в самой базе")

    async def test_cart_survives_second_request(self):
        """Корзина гостя переживает второй запрос.

        Сессия, которую заводит
        голый GET, cookie не получает (см. `_delete_cart`) — переживает
        второй запрос только та сессия, что хоть раз прошла через
        `tx()`. Здесь это добавление товара (`POST /api/cart`), а не
        `DELETE`, чтобы было что находить во втором запросе.

        Второй запрос идёт из ОТДЕЛЬНОГО `AsyncClient` с тем же cookie,
        а не тем же клиентом подряд — иначе тест доказывал бы только то,
        что объект в памяти одного процесса не потерял поле, а не то,
        что состояние действительно долетело до базы и его правда можно
        поднять заново, как это делает второй запрос настоящего браузера.
        """
        await self._ready_db()
        app = create_app()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            boot = await client.get("/api/bootstrap")
            token = boot.json()["csrf"]
            add = await client.post("/api/cart", json={"product_id": 1, "qty": 2},
                                    headers={"X-CSRF-Token": token, "Origin": "http://test"})
            self.assertEqual(add.status_code, 200, add.text)
            self.assertTrue(add.json().get("items"), "корзина пуста сразу после добавления")
            sid = client.cookies.get("fc_sid")
            self.assertIsNotNone(sid, "cookie сессии не выставилась при первой записи")
            csrf = client.cookies.get("fc_csrf")

        # Новый клиент — те же cookie, но ни разбора Ctx, ни состояния
        # от первого не разделяет; единственный мост между ними — база.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
            cookies={"fc_sid": sid, "fc_csrf": csrf},
        ) as second:
            got = await second.get("/api/cart")
            self.assertEqual(got.status_code, 200, got.text)
            items = got.json().get("items") or []
            self.assertEqual(len(items), 1, "второй запрос не увидел корзину первого")
            self.assertEqual(items[0]["product"]["id"], 1)
            self.assertEqual(items[0]["qty"], 2)


if __name__ == "__main__":
    unittest.main()
