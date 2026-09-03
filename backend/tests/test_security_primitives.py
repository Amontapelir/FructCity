"""Лимитер запросов и CSRF (`domain/security.py`) — прямые проверки.

Проверки смысловые: лимитер блокирует и отпускает, не путает ключи и
чистит память; CSRF требует и токен, и происхождение запроса.

Пароли отдельной проверки здесь не требуют: формат хеша самоописателен
(`scrypt$N$r$p$соль$хеш`), и `verify_password` разбирает ровно то, что
записал `hash_password`, какими бы параметрами хеш ни был посчитан.
"""

from __future__ import annotations

import unittest

from backend.app.domain import security as S


class RateLimiterBehaviour(unittest.TestCase):
    """Лимитер: блокирует, отпускает, не путает ключи, чистит память."""

    def test_blocks_after_limit_and_recovers(self):
        limiter = S.RateLimiter()
        for i in range(3):
            self.assertTrue(limiter.check("k", 3, 1000, now=i)["allowed"])
        blocked = limiter.check("k", 3, 1000, now=3)
        self.assertFalse(blocked["allowed"])
        self.assertGreaterEqual(blocked["retryAfter"], 1)
        # окно проехало — снова можно
        self.assertTrue(limiter.check("k", 3, 1000, now=1500)["allowed"])

    def test_keys_are_independent(self):
        limiter = S.RateLimiter()
        for i in range(3):
            limiter.check("первый", 3, 1000, now=i)
        self.assertTrue(limiter.check("второй", 3, 1000, now=3)["allowed"])

    def test_reset_clears_attempts(self):
        limiter = S.RateLimiter()
        for i in range(3):
            limiter.check("k", 3, 1000, now=i)
        limiter.reset("k")
        self.assertTrue(limiter.check("k", 3, 1000, now=3)["allowed"])

    def test_sweep_frees_memory(self):
        """Без чистки поток запросов с разных адресов съедает память."""
        limiter = S.RateLimiter()
        for i in range(100):
            limiter.check(f"ip-{i}", 5, 1000, now=0)
        self.assertEqual(len(limiter.hits), 100)
        limiter.sweep(max_age_ms=1000, now=10_000)
        self.assertEqual(len(limiter.hits), 0, "старые ключи не убраны")


class Csrf(unittest.TestCase):
    """Двойная защита: токен и происхождение запроса."""

    ORIGINS = {"http://127.0.0.1:3000"}

    def test_safe_methods_pass_without_token(self):
        for method in ("GET", "HEAD", "OPTIONS", "get"):
            with self.subTest(method=method):
                self.assertTrue(S.check_csrf(method, {}, {}, self.ORIGINS)["ok"])

    def test_matching_tokens_pass(self):
        got = S.check_csrf("POST", {"x-csrf-token": "t0k"},
                           {S.CSRF_COOKIE: "t0k"}, self.ORIGINS)
        self.assertTrue(got["ok"])

    def test_missing_or_mismatched_token_fails(self):
        cases = [
            ({}, {}),
            ({"x-csrf-token": "t0k"}, {}),
            ({}, {S.CSRF_COOKIE: "t0k"}),
            ({"x-csrf-token": "другой"}, {S.CSRF_COOKIE: "t0k"}),
        ]
        for headers, cookies in cases:
            with self.subTest(headers=headers, cookies=cookies):
                got = S.check_csrf("POST", headers, cookies, self.ORIGINS)
                self.assertFalse(got["ok"])
                self.assertEqual(got["reason"], "csrf_token_mismatch")


if __name__ == "__main__":
    unittest.main()
