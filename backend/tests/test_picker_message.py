"""Регресс: сообщение сборщику не должно нести лишних пустых строк.

Текст собирается из строк, часть которых необязательна (скидка, холд,
весовые позиции, комментарий), а пустая строка `''` служит разделителем
блоков. Фильтр `is not None` убирает только отсутствующие строки, но
оставляет разделитель — и если соседний блок пропущен, в сообщении
появляется дыра из двух переводов строки. Правильный фильтр — `if p`:
он убирает и пустые строки.

Дефект нашёлся сравнением ответа целиком; сборщик получал сообщение с
пустым провалом посередине. Тест ловит это чистым вызовом функции, без
записи и без поднятого приложения.
"""

from __future__ import annotations

import unittest

try:
    from backend.app.api.routers.cart import _preorder_message, picker_message
    HAS_STACK = True
except Exception:  # noqa: BLE001
    HAS_STACK = False


def _order_state(**overrides):
    order = {
        "id": 1, "number": 1002, "method": "pickup",
        "name": "Тест Покупателев", "phone": "+79161234567",
        "slot_ymd": "2026-09-05", "slot_from": 10, "slot_to": 12,
        "discount_amount": 0, "delivery_cost": 0, "total": 198,
        "payment_method": "cash", "hold_amount": 0, "comment": "",
        "email": "test@example.com",
    }
    order.update(overrides)
    state = {
        "order_items": [{
            "order_id": 1, "type": "unit", "name": "Авокадо Хасс",
            "requested_quantity": 2, "price_at_purchase": 99,
        }],
        "delivery_zones": [],
        "settings": {"pickup_address": "Тестовый адрес"},
    }
    return state, order


@unittest.skipUnless(HAS_STACK, "не установлен FastAPI")
class PickerMessageNoDoubleBlankLines(unittest.TestCase):

    def test_minimal_order_has_no_double_newline(self):
        """Ни скидки, ни холда, ни веса, ни комментария — значит и ни
        одной пустой строки в тексте быть не должно."""
        state, order = _order_state()
        text = picker_message(state, order)
        self.assertNotIn("\n\n", text, f"лишняя пустая строка:\n{text!r}")

    def test_order_with_discount_hold_comment_still_has_no_double_newline(self):
        """Заполненные необязательные строки не освобождают от той же
        проверки — падает и пустая, и заполненная комбинация."""
        state, order = _order_state(
            discount_amount=20, delivery_cost=150, hold_amount=500,
            comment="Позвоните заранее",
        )
        text = picker_message(state, order)
        self.assertIn("Скидка: −20 ₽", text)
        self.assertIn("💬 Позвоните заранее", text)
        self.assertNotIn("\n\n", text, f"лишняя пустая строка:\n{text!r}")

    def test_delivery_order_uses_zone_line(self):
        """Не только про пустые строки: способ «доставка» — другая
        первая строка адреса, сверить заодно."""
        state, order = _order_state(method="delivery", address="ул. Тестовая, 1")
        text = picker_message(state, order)
        self.assertIn("🚚 Доставка", text)
        self.assertIn("ул. Тестовая, 1", text)
        self.assertNotIn("\n\n", text)

    def test_preorder_without_comment_has_no_double_newline(self):
        pre = {
            "number": 5, "name": "Тест", "phone": "+79161234567",
            "product_name": "Баранина", "requested_weight": 2.5,
            "pickup_date": "2026-09-10", "estimate": 1500, "price_per_kg": 600,
            "comment": "",
        }
        text = _preorder_message(pre)
        self.assertNotIn("\n\n", text)
        self.assertFalse(text.endswith("\n"), "пустой комментарий не должен оставлять хвост")

    def test_preorder_with_comment_matches(self):
        pre = {
            "number": 5, "name": "Тест", "phone": "+79161234567",
            "product_name": "Баранина", "requested_weight": 2.5,
            "pickup_date": "2026-09-10", "estimate": 1500, "price_per_kg": 600,
            "comment": "Позвоните заранее",
        }
        text = _preorder_message(pre)
        self.assertTrue(text.endswith("💬 Позвоните заранее"))
        self.assertNotIn("\n\n", text)


if __name__ == "__main__":
    unittest.main()
