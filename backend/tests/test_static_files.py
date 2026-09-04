"""Раздача статики: разбор пути, типы, кэш, 404.

Два уровня, намеренно разделённые:

1. `StaticPathResolution` — прямые проверки `domain/static_files.py`.
   Это логика, ошибка в которой отдаёт наружу файл за пределами
   `public/`, поэтому она проверяется без HTTP и без FastAPI — так
   набор гоняется всегда, а не только там, где поднят весь стек.
2. `StaticOverHttp` — то же через живое приложение: что отдаётся, с
   каким типом и правилом кэша, и что маршрут-ловушка не перехватила
   ни API, ни SSR-страницы.

Эталон во втором наборе — сам файл на диске: отдавать надо ровно его,
байт в байт.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from backend.app.domain import static_files as SF
from backend.tests.paths import PUBLIC, ROOT, STORE

try:
    import httpx

    from backend.app.main import create_app
    HAS_STACK = True
except Exception:  # noqa: BLE001
    HAS_STACK = False


class StaticPathResolution(unittest.TestCase):
    """Разбор пути и правила кэша — без HTTP."""

    def test_serves_file_inside_public(self):
        found = SF.resolve_static(PUBLIC, "/app.js")
        self.assertIsNotNone(found, "витринный скрипт обязан находиться")
        self.assertEqual(found.name, "app.js")

    # Цель обхода обязана СУЩЕСТВОВАТЬ, иначе проверка пустая: путь к
    # несуществующему файлу отвергается по причине «файла нет», и тест
    # остаётся зелёным даже со снятой защитой. Прежние проверки целились
    # в файл, который позже удалили, и стали проходить вхолостую —
    # заметно это стало только при чтении. `README.md` лежит в корне
    # проекта, то есть ровно на один уровень выше `public/`.
    OUTSIDE = "README.md"

    def test_target_of_traversal_really_exists(self):
        """Страховка для самих проверок ниже: пропадёт файл — они
        замолчат, и об этом надо узнать здесь, а не через год."""
        self.assertTrue((ROOT / self.OUTSIDE).is_file(),
                        f"{self.OUTSIDE} нет в корне — проверки обхода стали пустыми")

    def test_rejects_parent_traversal(self):
        for attempt in (f"/../{self.OUTSIDE}", "/../../etc/passwd",
                        f"/img/../../{self.OUTSIDE}"):
            with self.subTest(attempt=attempt):
                self.assertIsNone(SF.resolve_static(PUBLIC, attempt),
                                  "выход за public/ обязан быть закрыт")

    def test_rejects_encoded_traversal(self):
        """`%2e%2e%2f` — тот же `../`, только мимо наивной проверки строки."""
        self.assertIsNone(SF.resolve_static(PUBLIC, f"/%2e%2e/{self.OUTSIDE}"))
        self.assertIsNone(SF.resolve_static(PUBLIC, f"/%2e%2e%2f{self.OUTSIDE}"))

    def test_rejects_null_byte_and_directory(self):
        self.assertIsNone(SF.resolve_static(PUBLIC, "/app.js\0.png"),
                          "нулевой байт в пути — отказ")
        self.assertIsNone(SF.resolve_static(PUBLIC, "/img"),
                          "каталог не файл, отдавать нечего")
        self.assertIsNone(SF.resolve_static(PUBLIC, "/"),
                          "пустой путь не адрес файла")

    def test_rejects_symlink_pointing_outside(self):
        """Ссылка внутри `public/` наружу — тоже выход за корень."""
        tmp = Path(tempfile.mkdtemp(prefix="fructcity-static-"))
        try:
            root = tmp / "public"
            root.mkdir()
            (tmp / "secret.txt").write_text("нельзя", encoding="utf-8")
            link = root / "secret.txt"
            try:
                link.symlink_to(tmp / "secret.txt")
            except (OSError, NotImplementedError):
                self.skipTest("символические ссылки недоступны в этой системе")
            self.assertIsNone(SF.resolve_static(root, "/secret.txt"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_content_type_table_matches_node(self):
        cases = {
            "app.js": "application/javascript; charset=utf-8",
            "style.css": "text/css; charset=utf-8",
            "favicon.svg": "image/svg+xml",
            "img/shop.png": "image/png",
            "ok.txt": "text/plain; charset=utf-8",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(SF.content_type(PUBLIC / name), expected)
        self.assertEqual(SF.content_type(Path("unknown.bin")), SF.DEFAULT_MIME,
                         "незнакомое расширение — поток байт, а не угаданный тип")

    def test_cache_control_is_immutable_only_for_versioned_asset(self):
        js = PUBLIC / "app.js"
        self.assertEqual(SF.cache_control(js, versioned=True),
                         "public, max-age=31536000, immutable")
        self.assertEqual(SF.cache_control(js, versioned=False),
                         "public, max-age=0, must-revalidate",
                         "без ?v= вечный кэш означал бы, что правку витрины не увидят")
        self.assertEqual(SF.cache_control(PUBLIC / "index.html", versioned=True),
                         "public, max-age=0, must-revalidate",
                         "разметка не версионируется адресом — кэшировать её вечно нельзя")

    def test_etag_is_strong_and_depends_on_file(self):
        one = SF.etag_for(100, 1_700_000_000_000_000_000)
        same = SF.etag_for(100, 1_700_000_000_000_000_000)
        other_size = SF.etag_for(101, 1_700_000_000_000_000_000)
        other_time = SF.etag_for(100, 1_700_000_001_000_000_000)
        self.assertEqual(one, same, "тот же файл — тот же ETag")
        self.assertNotEqual(one, other_size, "другой размер — другой ETag")
        self.assertNotEqual(one, other_time, "другое время правки — другой ETag")
        self.assertTrue(one.startswith('"') and one.endswith('"'),
                        "ETag обязан быть сильным и в кавычках, иначе браузер его не пришлёт")
        self.assertNotIn("=", one, "base64url без выравнивающих знаков")


@unittest.skipUnless(HAS_STACK, "не установлены FastAPI, SQLAlchemy или httpx")
@unittest.skipUnless(STORE.exists(), "нет data/store.json")
class StaticOverHttp(unittest.IsolatedAsyncioTestCase):
    """Живое приложение: что отдаётся, с каким типом и правилом кэша."""

    @classmethod
    def setUpClass(cls):
        cls.app = create_app()

    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def _file(self, path: str, disk: Path) -> httpx.Response:
        """Запрос через приложение + сверка тела с файлом на диске."""
        r = await self.client.get(path)
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertEqual(r.text, disk.read_text(encoding="utf-8"),
                         f"{path}: отдаётся не то, что лежит на диске")
        return r

    async def test_app_js_is_served_verbatim(self):
        """Витринный скрипт — тот же файл, тот же тип, то же правило кэша."""
        r = await self._file("/app.js", PUBLIC / "app.js")
        self.assertEqual(r.headers["content-type"],
                         "application/javascript; charset=utf-8")
        self.assertEqual(r.headers["cache-control"], "public, max-age=0, must-revalidate")

    async def test_style_css_is_served_verbatim(self):
        r = await self._file("/style.css", PUBLIC / "style.css")
        self.assertEqual(r.headers["content-type"], "text/css; charset=utf-8")

    async def test_calc_core_is_the_same_file_the_server_runs(self):
        """Инвариант 1: браузер получает ровно тот `calc.js`, которым
        считает сам сервер, — из `lib/`, а не копию в `public/`."""
        await self._file("/lib/calc.js", ROOT / "lib" / "calc.js")
        self.assertFalse((PUBLIC / "calc.js").exists(),
                         "копия расчётного ядра в public/ — это две разные правды о деньгах")

    async def test_versioned_asset_is_immutable(self):
        r = await self.client.get("/app.js?v=7")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["cache-control"], "public, max-age=31536000, immutable")

    async def test_repeat_request_with_etag_gets_304(self):
        first = await self.client.get("/app.js")
        etag = first.headers["etag"]
        again = await self.client.get("/app.js", headers={"If-None-Match": etag})
        self.assertEqual(again.status_code, 304, "повторный запрос обязан отдавать 304")
        self.assertEqual(again.content, b"", "у 304 тела быть не должно")

    async def test_traversal_over_http_is_refused(self):
        """Тот же выход за корень, но через живой HTTP — сервер обязан
        ответить 404, а не отдать файл из корня проекта."""
        marker = (ROOT / "README.md").read_text(encoding="utf-8")[:40]
        for attempt in ("/../README.md", "/%2e%2e/README.md", "/img/../../README.md"):
            with self.subTest(attempt=attempt):
                r = await self.client.get(attempt)
                self.assertEqual(r.status_code, 404)
                self.assertNotIn(marker, r.text, "файл из корня всё-таки уехал наружу")

    async def test_unknown_page_is_plain_text_404(self):
        """Не оболочка витрины, а простой текст: иначе поисковик получил
        бы 404 с телом главной страницы и проиндексировал бы её как
        дубль."""
        r = await self.client.get("/no-such-page")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.text, "Страница не найдена")
        self.assertTrue(r.headers["content-type"].startswith("text/plain"))

    async def test_unknown_api_path_is_json_error(self):
        """Для `/api/...` ответ обязан остаться JSON-ошибкой: витрина
        читает поле `error`, а не текст страницы."""
        r = await self.client.get("/api/no-such-route")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json(), {"error": "not_found"})

    async def test_api_routes_still_answer(self):
        """Маршрут-ловушка не должна перехватывать ни API, ни SSR."""
        api = await self.client.get("/api/products")
        self.assertEqual(api.status_code, 200)
        # каталог отдаётся как {total, items, price_range} — проверяем
        # именно эту форму, а не выдуманный ключ "products"
        self.assertIn("items", api.json())
        self.assertTrue(api.json()["items"], "каталог не должен приходить пустым")

        home = await self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertTrue(home.headers["content-type"].startswith("text/html"))

    async def test_ssr_pages_all_carry_explicit_cache_control(self):
        """Issue #16 — карточка товара и каталог отдавались вовсе без
        Cache-Control (проверено вживую curl'ом), в отличие от главной.
        Цена и остаток на этих страницах меняются — без явного правила
        их рискует закэшировать прокси/CDN перед приложением."""
        api = await self.client.get("/api/products")
        slug = api.json()["items"][0]["slug"]

        for path in ("/", "/catalog", f"/product/{slug}", "/policy", "/offer"):
            with self.subTest(path=path):
                r = await self.client.get(path)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.headers.get("cache-control"), "no-cache",
                                 f"{path}: нет (или не тот) Cache-Control")


if __name__ == "__main__":
    unittest.main()
