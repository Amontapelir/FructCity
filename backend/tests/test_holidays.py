"""Праздники и выходные в слотах (ТЗ 4.4, ROADMAP 2.9).

Список дат-исключений живёт в `settings.holidays` (обычная запись в
таблице «ключ-значение» — новой колонки/таблицы не потребовалось).
Слот на такую дату обязан быть закрыт целиком, независимо от часа,
и в расчёте свободных слотов (`catalog.slots_view`), и при
оформлении заказа (`shop.place_order`) — оба места читают один и тот
же `calc.slots_for_date`, поэтому один тест на каждый слой достаточен,
дублировать перебор часов не нужно.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from backend.app.domain import calc as C
from backend.app.domain import catalog as CAT
from backend.app.domain import shop as S
from backend.app.domain import validate as V

NOW = datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)   # вторник, МСК день тот же


def _settings(**over):
    base = {
        "work_from": 9, "work_to": 21, "cutoff_h": 0, "horizon_d": 5,
        "slot_capacity_pickup": 5, "slot_capacity_delivery": 5,
        "holidays": ["2026-08-20"],
    }
    base.update(over)
    return base


class SlotsForDateHoliday(unittest.TestCase):
    """Ядро: `calc.slots_for_date` — общее для сервера и браузера (lib/calc.js)."""

    def test_holiday_closes_every_hour_of_the_day(self):
        slots = C.slots_for_date(
            ymd="2026-08-20", now=NOW, work_from=9, work_to=21,
            cutoff_h=0, capacity=5, booked={}, holidays=["2026-08-20"])
        self.assertTrue(slots, "должны быть сгенерированы часовые интервалы")
        for s in slots:
            with self.subTest(hour=s["from"]):
                self.assertFalse(s["ok"], "праздничный день не должен быть открыт")
                self.assertEqual(s["reason"], "выходной")

    def test_same_date_without_holiday_stays_open(self):
        """Подмена: без даты в списке исключений тот же день открыт."""
        slots = C.slots_for_date(
            ymd="2026-08-20", now=NOW, work_from=9, work_to=21,
            cutoff_h=0, capacity=5, booked={}, holidays=[])
        self.assertTrue(any(s["ok"] for s in slots),
                         "без праздника в списке день обязан остаться рабочим")

    def test_holiday_wins_over_capacity_reason(self):
        """Причина — «выходной», а не «занято», даже если слот и так забронирован под завязку."""
        slots = C.slots_for_date(
            ymd="2026-08-20", now=NOW, work_from=9, work_to=11,
            cutoff_h=0, capacity=1, booked={"2026-08-20|9": 5}, holidays=["2026-08-20"])
        self.assertEqual(slots[0]["reason"], "выходной")

    def test_default_has_no_holidays(self):
        """holidays не обязателен — старые вызовы без параметра не ломаются."""
        slots = C.slots_for_date(
            ymd="2026-08-20", now=NOW, work_from=9, work_to=21,
            cutoff_h=0, capacity=5, booked={})
        self.assertTrue(any(s["ok"] for s in slots))


class SlotsViewRespectsHolidays(unittest.TestCase):
    """`catalog.slots_view` — то, что реально уходит в `/api/slots`."""

    def test_holiday_day_has_no_free_slots(self):
        state = {"settings": _settings(), "slot_bookings": {}}
        view = CAT.slots_view(state, "pickup", now=NOW)
        holiday_day = next(d for d in view["days"] if d["ymd"] == "2026-08-20")
        self.assertTrue(holiday_day["slots"], "день должен присутствовать в списке")
        self.assertFalse(any(s["ok"] for s in holiday_day["slots"]))

    def test_first_available_skips_holiday(self):
        """Ближайшая свободная дата — не праздничная, даже если она раньше по календарю."""
        state = {"settings": _settings(), "slot_bookings": {}}
        view = CAT.slots_view(state, "pickup", now=NOW)
        self.assertNotEqual(view["first_available"], "2026-08-20")


class PlaceOrderRejectsHoliday(unittest.TestCase):
    """Бронь через `place_order` — второе место, читающее тот же `slots_for_date`."""

    def _state(self):
        return {
            "products": [{
                "id": 1, "sku": "T-1", "slug": "t-1", "name": "Тест",
                "type": "unit", "price": 100, "price_per_kg": 0,
                "vat_rate": 0, "stock": 10, "is_active": True,
            }],
            "settings": _settings(),
            "users": [], "seq": {"orders": 0},
        }

    def _session(self):
        return {"cart": [{"product_id": 1, "qty": 1, "weight": None}], "user_id": None}

    def test_booking_holiday_slot_is_rejected(self):
        result = S.place_order(
            self._state(), next_id=lambda k: 1, now_iso=lambda: "2026-08-18T09:00:00.000Z",
            session=self._session(), now=NOW,
            data={"name": "Тест", "phone": "+79161234567", "method": "pickup",
                  "payment": "cash", "consent": True,
                  "slot_ymd": "2026-08-20", "slot_from": 9})
        self.assertEqual(result.get("error"), "slot_unavailable")
        self.assertEqual(result.get("reason"), "выходной")

    def test_booking_non_holiday_slot_succeeds(self):
        """Подмена: тот же вызов без праздника в settings обязан пройти."""
        state = self._state()
        state["settings"] = _settings(holidays=[])
        result = S.place_order(
            state, next_id=lambda k: 1, now_iso=lambda: "2026-08-18T09:00:00.000Z",
            session=self._session(), now=NOW,
            data={"name": "Тест", "phone": "+79161234567", "method": "pickup",
                  "payment": "cash", "consent": True,
                  "slot_ymd": "2026-08-20", "slot_from": 9})
        self.assertNotIn("error", result)


class SettingsSchemaAcceptsHolidays(unittest.TestCase):

    def test_valid_dates_pass(self):
        out = V.validate(V.SCHEMAS["settings"], {
            "work_from": 9, "work_to": 21, "cutoff_h": 0, "horizon_d": 5,
            "slot_capacity_delivery": 5, "slot_capacity_pickup": 5,
            "pickup_address": "г. Москва, ул. Тестовая, 1", "phone": "+79161234567",
            "email": "", "meat_days": [], "meat_limit_kg": 0, "meat_cutoff_days": 1,
            "requisites": "", "holidays": ["2026-01-01", "2026-01-07"],
        })
        self.assertEqual(out["holidays"], ["2026-01-01", "2026-01-07"])

    def test_missing_holidays_defaults_to_empty_list(self):
        """Старая форма админки без поля holidays не должна падать с ошибкой валидации."""
        errs = {}
        value = V.SCHEMAS["settings"]["holidays"](None, "holidays", errs)
        self.assertEqual(value, [])
        self.assertFalse(errs)

    def test_malformed_date_is_rejected(self):
        errs = {}
        V.SCHEMAS["settings"]["holidays"](["31 февраля"], "holidays", errs)
        self.assertIn("holidays[0]", errs)


if __name__ == "__main__":
    unittest.main()
