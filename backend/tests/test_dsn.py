"""Сборка строки подключения.

Проверяется без pydantic и SQLAlchemy: функция чистая и лежит
отдельно именно для этого.

Главное здесь — экранирование. Пароль попадает в ту часть адреса, где
`@`, `:`, `/` и `#` имеют служебное значение, и склеенная вручную
строка с таким паролем разбирается неправильно. Сообщение об ошибке
при этом говорит о чём угодно, кроме настоящей причины.
"""

from __future__ import annotations

import unittest

from backend.app.db.dsn import DEFAULT_DRIVER, build_url, mask_url


class BuildUrl(unittest.TestCase):

    def test_plain(self):
        self.assertEqual(
            build_url(user="postgres", password="secret", host="127.0.0.1",
                      port=5432, name="fructcity"),
            "postgresql+psycopg://postgres:secret@127.0.0.1:5432/fructcity")

    def test_special_characters_in_password(self):
        """Пароль со служебными символами не должен рвать адрес."""
        url = build_url(user="postgres", password="p@ss:w/ord#1", name="fructcity")
        self.assertIn("p%40ss%3Aw%2Ford%231", url)
        # Разделителем остаётся ровно одна собака — та, что перед хостом
        self.assertEqual(url.count("@"), 1)
        self.assertTrue(url.endswith("@127.0.0.1:5432/fructcity"))

    def test_password_with_cyrillic(self):
        url = build_url(user="postgres", password="пароль", name="db")
        self.assertNotIn("пароль", url, "кириллица не закодирована")
        self.assertIn("%D0%BF", url)

    def test_user_is_escaped_too(self):
        url = build_url(user="почта@домен", password="x", name="db")
        self.assertEqual(url.count("@"), 1)

    def test_empty_password_omits_colon(self):
        """`user:@host` некоторые драйверы читают как пароль из пустоты."""
        url = build_url(user="postgres", password="", name="db")
        self.assertEqual(url, f"{DEFAULT_DRIVER}://postgres@127.0.0.1:5432/db")

    def test_no_user_no_credentials(self):
        url = build_url(user="", password="", host="db.local", name="db")
        self.assertEqual(url, f"{DEFAULT_DRIVER}://db.local:5432/db")

    def test_port_can_be_omitted(self):
        url = build_url(user="u", password="p", host="db.local", port=None, name="db")
        self.assertEqual(url, f"{DEFAULT_DRIVER}://u:p@db.local/db")

    def test_name_required(self):
        with self.assertRaises(ValueError):
            build_url(user="u", password="p", name="")

    def test_driver_is_configurable(self):
        url = build_url(driver="postgresql+asyncpg", user="u", password="p", name="db")
        self.assertTrue(url.startswith("postgresql+asyncpg://"))


class MaskUrl(unittest.TestCase):

    def test_hides_password(self):
        url = build_url(user="postgres", password="s3cret", name="db")
        masked = mask_url(url)
        self.assertNotIn("s3cret", masked)
        self.assertIn("postgres:***@", masked)

    def test_hides_escaped_password(self):
        url = build_url(user="postgres", password="p@ss:w/ord", name="db")
        masked = mask_url(url)
        self.assertNotIn("%40ss", masked, "экранированный пароль всё равно виден")
        self.assertIn("***", masked)

    def test_keeps_host_and_database_visible(self):
        """Скрывать надо пароль, а не всё подряд: адрес нужен для диагностики."""
        url = build_url(user="postgres", password="s3cret", host="db.local",
                        port=6432, name="fructcity")
        masked = mask_url(url)
        self.assertIn("db.local:6432", masked)
        self.assertIn("/fructcity", masked)

    def test_url_without_password_unchanged(self):
        url = build_url(user="postgres", password="", name="db")
        self.assertEqual(mask_url(url), url)

    def test_garbage_is_returned_as_is(self):
        self.assertEqual(mask_url("не адрес вовсе"), "не адрес вовсе")


class RoundTrip(unittest.TestCase):
    """Собранную строку должен разобрать сам драйвер, а не только глаз."""

    def test_sqlalchemy_parses_what_we_build(self):
        try:
            from sqlalchemy.engine import make_url
        except Exception:  # noqa: BLE001
            self.skipTest("SQLAlchemy не установлен")

        password = "p@ss:w/ord#1 и пробел"
        url = make_url(build_url(user="postgres", password=password,
                                 host="db.local", port=6432, name="fructcity"))
        self.assertEqual(url.username, "postgres")
        self.assertEqual(url.password, password, "пароль разобрался не тем, чем был")
        self.assertEqual(url.host, "db.local")
        self.assertEqual(url.port, 6432)
        self.assertEqual(url.database, "fructcity")


if __name__ == "__main__":
    unittest.main()
