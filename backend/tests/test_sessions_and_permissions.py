"""Сессии и права (`domain/auth.py`) — прямые проверки.

Порядок прав зафиксирован здесь явным списком: он уходит наружу в
ответе `/api/admin/me`, и по нему админка рисует меню. Однажды он уже
разъезжался — права отдавались отсортированными по алфавиту, — и
заметить это удалось только сравнением ответа целиком.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from backend.app.domain import auth as A


class Permissions(unittest.TestCase):
    """Матрица прав: кто что может."""

    RIGHTS = ["dashboard", "products", "categories", "orders", "preorders",
              "promos", "delivery", "staff", "home", "import", "export",
              "payments", "выдуманное"]

    def test_admin_permission_order_is_fixed(self):
        """Порядок — часть ответа `/api/admin/me`, а не деталь реализации."""
        self.assertEqual(
            A.permissions_of("admin"),
            ["dashboard", "products", "categories", "orders", "preorders",
             "promos", "delivery", "staff", "home", "import", "export", "payments"])

    def test_manager_permission_order_is_fixed(self):
        self.assertEqual(A.permissions_of("manager"), ["orders", "preorders", "export"])

    def test_unknown_role_has_no_permissions(self):
        for role in ("customer", "guest", "выдуманная", None):
            with self.subTest(role=role):
                self.assertEqual(A.permissions_of(role), [])

    def test_manager_cannot_touch_money(self):
        """Вести заказ по статусам — работа менеджера, помечать деньги
        полученными или возвращёнными — нет (ТЗ 10.8)."""
        self.assertTrue(A.can("manager", "orders"))
        self.assertFalse(A.can("manager", "payments"), "менеджер получил доступ к деньгам")
        self.assertFalse(A.can("manager", "staff"))
        self.assertFalse(A.can("manager", "products"))
        self.assertTrue(A.can("admin", "payments"))

    def test_unknown_role_can_nothing(self):
        for right in self.RIGHTS:
            self.assertFalse(A.can("выдуманная", right))
            self.assertFalse(A.can(None, right))

    def test_permissions_list_is_a_copy(self):
        """Вызывающий не должен уметь испортить таблицу прав на будущее."""
        first = A.permissions_of("admin")
        first.append("выдуманное")
        self.assertNotIn("выдуманное", A.permissions_of("admin"))


class SessionLifecycle(unittest.TestCase):
    """Создание и перевыпуск сессии."""

    def setUp(self):
        self.counter = {"sessions": 0}

        def next_id(name: str) -> int:
            self.counter[name] += 1
            return self.counter[name]

        self.next_id = next_id
        self.now = datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)

    def test_new_session_is_unpredictable(self):
        a = A.new_session(next_id=self.next_id, now=self.now)
        b = A.new_session(next_id=self.next_id, now=self.now)
        self.assertNotEqual(a["sid"], b["sid"])
        self.assertGreaterEqual(len(a["sid"]), 40, "идентификатор слишком короткий")
        self.assertRegex(a["sid"], r"^[A-Za-z0-9_-]+$")

    def test_staff_session_expires_sooner(self):
        guest = A.new_session(next_id=self.next_id, now=self.now)
        staff = A.new_session(next_id=self.next_id, role="admin",
                              ttl_ms=A.STAFF_TTL_MS, now=self.now)
        self.assertLess(staff["expires_at"], guest["expires_at"])
        self.assertLess(A.STAFF_TTL_MS, A.SESSION_TTL_MS,
                        "сессия персонала должна жить меньше покупательской")

    def test_rotation_changes_id_but_keeps_everything_else(self):
        """Подмена сессии закрывается сменой идентификатора,
        а покупатель не должен за это платить корзиной."""
        old = A.new_session(next_id=self.next_id, cart=[{"product_id": 1, "qty": 2}],
                            ip="127.0.0.1", now=self.now)
        old["promo_code"] = "FRUCT10"
        old["recent_orders"] = [7, 8]
        old["recent_preorders"] = [3]

        fresh = A.rotated_session(old, next_id=self.next_id, user_id=42,
                                  role="customer", now=self.now)

        self.assertNotEqual(fresh["sid"], old["sid"], "идентификатор не сменился")
        self.assertEqual(fresh["cart"], old["cart"], "корзина потеряна при входе")
        self.assertEqual(fresh["promo_code"], "FRUCT10", "промокод слетел при входе")
        self.assertEqual(fresh["recent_orders"], [7, 8], "заказы гостя стали недоступны")
        self.assertEqual(fresh["recent_preorders"], [3])
        self.assertEqual(fresh["user_id"], 42)
        self.assertEqual(fresh["role"], "customer")
        self.assertEqual(fresh["ip"], "127.0.0.1")

    def test_rotation_lets_patch_override_cart(self):
        """Вход в админку явно сбрасывает корзину — `patch` обязан
        побеждать перенос из старой сессии. Раньше `cart` в `patch` падал
        с «got multiple values for keyword argument 'cart'», потому что
        `rotated_session` уже передавал его отдельным аргументом."""
        old = A.new_session(next_id=self.next_id, cart=[{"product_id": 1, "qty": 2}],
                            now=self.now)
        fresh = A.rotated_session(old, next_id=self.next_id, role="admin",
                                  ttl_ms=A.STAFF_TTL_MS, cart=[], now=self.now)
        self.assertEqual(fresh["cart"], [])

    def test_rotation_does_not_share_mutable_state(self):
        """Копия, а не ссылка: иначе правка новой корзины меняла бы старую."""
        old = A.new_session(next_id=self.next_id, cart=[{"product_id": 1}], now=self.now)
        fresh = A.rotated_session(old, next_id=self.next_id, now=self.now)
        fresh["cart"].append({"product_id": 2})
        self.assertEqual(len(old["cart"]), 1, "корзины связаны одной ссылкой")

    def test_prune_keeps_the_freshest(self):
        sessions = [{"id": i, "last_seen": A.iso_now(self.now + timedelta(seconds=i))}
                    for i in range(10)]
        kept = A.prune_sessions(sessions, limit=3)
        self.assertEqual([s["id"] for s in kept], [7, 8, 9])

    def test_prune_does_nothing_below_limit(self):
        sessions = [{"id": 1, "last_seen": A.iso_now(self.now)}]
        self.assertIs(A.prune_sessions(sessions, limit=10), sessions)


if __name__ == "__main__":
    unittest.main()
