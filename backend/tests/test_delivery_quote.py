"""Зона доставки без тарифа — не отказ, а согласование (ТЗ 5.2, ROADMAP 2.12).

До этой задачи `place_order` отклонял заказ в зону с `cost: None`
кодом `zone_manual_quote` (409) — покупатель просто не мог оформить
заказ. Теперь такой заказ создаётся сразу, `calc.needs_quote` уводит
его в статус `awaiting_delivery_quote` вместо обычного `new`/
`awaiting_payment`, а `quote_delivery_cost` — единственный выход из
этого статуса, когда персонал называет сумму.

Отдельно проверяется найденная по пути ловушка в `recalc_order`: у
зоны без тарифа `calc.delivery` всегда 0 (тариф зоны не появляется —
согласована сумма конкретного заказа), и наивный `min(agreed, 0)`
обнулил бы уже согласованную доставку на первом же пересчёте после
сборки.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from backend.app.domain import calc as C
from backend.app.domain import shop as S

NOW = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)


def _settings(**over):
    base = {
        "work_from": 9, "work_to": 21, "cutoff_h": 0, "horizon_d": 5,
        "slot_capacity_pickup": 5, "slot_capacity_delivery": 5, "holidays": [],
    }
    base.update(over)
    return base


def _zone(**over):
    zone = {"id": 9, "name": "Другой район", "cost": None, "free_from": 0,
            "manual_quote": True, "is_active": True}
    zone.update(over)
    return zone


def _state(**over):
    base = {
        "products": [{
            "id": 1, "sku": "T-1", "slug": "t-1", "name": "Тест",
            "type": "unit", "price": 500, "price_per_kg": 0,
            "vat_rate": 0, "stock": 10, "is_active": True,
        }],
        "delivery_zones": [_zone()],
        "settings": _settings(), "users": [], "seq": {"orders": 0},
    }
    base.update(over)
    return base


def _session():
    return {"cart": [{"product_id": 1, "qty": 1, "weight": None}], "user_id": None}


def _order_data(**over):
    base = {"name": "Тест", "phone": "+79161234567", "email": None,
            "method": "delivery", "zone_id": 9, "address": "ул. Тестовая, 1",
            "payment": "cash", "consent": True,
            "slot_ymd": "2026-09-05", "slot_from": 9}
    base.update(over)
    return base


def _place(state=None, **data_over):
    return S.place_order(
        state or _state(), next_id=lambda k: 1, now_iso=lambda: "2026-09-04T09:00:00.000Z",
        session=_session(), now=NOW, data=_order_data(**data_over))


class PlaceOrderIntoManualQuoteZone(unittest.TestCase):

    def test_order_is_created_not_rejected(self):
        """Раньше здесь было {"error": "zone_manual_quote", "status": 409}."""
        result = _place()
        self.assertNotIn("error", result)
        self.assertTrue(result["ok"])

    def test_status_is_awaiting_delivery_quote(self):
        result = _place(payment="cash")
        self.assertEqual(result["order"]["status"], "awaiting_delivery_quote")

    def test_prepaid_method_also_waits_for_quote_first(self):
        """needs_quote важнее предоплаты: без известной суммы просить
        оплату нечем — иначе заказ повис бы в awaiting_payment навсегда."""
        result = _place(payment="online")
        self.assertEqual(result["order"]["status"], "awaiting_delivery_quote")

    def test_delivery_cost_is_zero_until_quoted(self):
        result = _place()
        self.assertEqual(result["order"]["delivery_cost"], 0)
        self.assertEqual(result["order"]["total"], result["order"]["items_total"])

    def test_known_zone_cost_is_unaffected(self):
        """Подмена: обычная зона с тарифом не должна попасть в новый статус."""
        state = _state(delivery_zones=[_zone(cost=300)])
        result = _place(state)
        self.assertEqual(result["order"]["status"], "new")
        self.assertEqual(result["order"]["delivery_cost"], 300)


class QuoteDeliveryCost(unittest.TestCase):

    def _order(self, **data_over):
        return _place(**data_over)["order"]

    def test_sets_cost_and_moves_cash_order_to_new(self):
        order = self._order(payment="cash")
        result = S.quote_delivery_cost(
            _state(), next_id=lambda k: 2, now_iso=lambda: "2026-09-04T10:00:00.000Z",
            order=order, cost=400, actor="admin")
        self.assertTrue(result["ok"])
        self.assertEqual(order["status"], "new")
        self.assertEqual(order["delivery_cost"], 400)
        self.assertEqual(order["total"], order["items_total"] + 400)

    def test_prepaid_order_moves_to_awaiting_payment(self):
        order = self._order(payment="online")
        S.quote_delivery_cost(_state(), next_id=lambda k: 2, now_iso=lambda: "x",
                              order=order, cost=400, actor="admin")
        self.assertEqual(order["status"], "awaiting_payment")

    def test_rejects_when_not_awaiting_quote(self):
        order = self._order()
        order["status"] = "new"       # уже согласовано (или никогда не требовалось)
        result = S.quote_delivery_cost(_state(), next_id=lambda k: 2, now_iso=lambda: "x",
                                       order=order, cost=400, actor="admin")
        self.assertEqual(result.get("error"), "quote_not_expected")
        self.assertEqual(result.get("status"), 409)

    def test_rejects_negative_cost(self):
        order = self._order()
        result = S.quote_delivery_cost(_state(), next_id=lambda k: 2, now_iso=lambda: "x",
                                       order=order, cost=-1, actor="admin")
        self.assertEqual(result.get("error"), "bad_cost")

    def test_logs_status_history(self):
        state = _state()
        order = _place(state, payment="cash")["order"]
        S.quote_delivery_cost(state, next_id=lambda k: 99, now_iso=lambda: "2026-09-04T10:00:00Z",
                              order=order, cost=400, actor="admin")
        last = state["order_status_history"][-1]
        self.assertEqual(last["status"], "new")
        self.assertEqual(last["actor"], "admin")
        self.assertIn("400", last["comment"])


class RecalcAfterQuoteDoesNotZeroOutDelivery(unittest.TestCase):
    """Ловушка, найденная по пути: min(agreed, calc.delivery=0) обнулял бы
    уже согласованную доставку зоны без тарифа на первом же пересчёте."""

    def test_recalc_keeps_the_quoted_delivery_cost(self):
        state = _state()
        order = _place(state, payment="cash")["order"]
        S.quote_delivery_cost(state, next_id=lambda k: 99, now_iso=lambda: "x",
                              order=order, cost=400, actor="admin")
        self.assertEqual(order["delivery_cost"], 400, "предпосылка теста не выполнена")

        S.recalc_order(state, order)

        self.assertEqual(order["delivery_cost"], 400,
                         "пересчёт не должен откатывать согласованную доставку к 0")


class TransitionsAndCancellation(unittest.TestCase):

    def test_allowed_transitions_blocks_the_normal_advance_button(self):
        out = C.allowed_transitions({"status": "awaiting_delivery_quote"})
        self.assertEqual(out["allowed"], [])
        self.assertEqual(out["blockedReason"], "awaiting_delivery_quote")

    def test_customer_can_cancel_before_the_quote(self):
        self.assertTrue(C.customer_can_cancel("awaiting_delivery_quote"))


if __name__ == "__main__":
    unittest.main()
