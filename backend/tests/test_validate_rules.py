"""Правила валидации (`domain/validate.py`) — прямые проверки.

Проверяется не «схема описана», а поведение, в котором легко ошибиться
незаметно: что лишние поля отбрасываются белым списком, что телефон
приводится к одной форме, что пароль не обрезается по краям.
"""

from __future__ import annotations

import unittest

from backend.app.domain import validate as V


class WhitelistBehaviour(unittest.TestCase):

    def test_unknown_fields_are_dropped(self):
        """Подмена полей закрывается белым списком, а не чёрным.

        Иначе клиент прислал бы вместе с заказом `role: admin` или
        `total: 1`, и лишнее поле доехало бы до записи.
        """
        errs: dict[str, str] = {}
        out = V.run(V.SCHEMAS["cartAdd"],
                    {"product_id": 1, "qty": 2, "role": "admin", "price": 0}, errs)
        self.assertEqual(errs, {})
        self.assertEqual(set(out), {"product_id", "qty", "weight"})
        self.assertNotIn("role", out)
        self.assertNotIn("price", out)

    def test_validate_raises_with_field_map(self):
        with self.assertRaises(V.ValidationError) as ctx:
            V.validate(V.SCHEMAS["otpVerify"], {"phone": "нет", "code": "1"})
        self.assertEqual(ctx.exception.status, 422)
        self.assertIn("phone", ctx.exception.fields)
        self.assertIn("code", ctx.exception.fields)

    def test_phone_is_normalised_to_one_form(self):
        """Один номер — одна запись, иначе у покупателя две истории."""
        forms = ["+7 916 606-06-06", "89166060606", "9166060606",
                 "+7(916)606-06-06", "7 916 606 06 06"]
        for form in forms:
            with self.subTest(form=form):
                out = V.validate(V.SCHEMAS["otpRequest"], {"phone": form})
                self.assertEqual(out["phone"], "+79166060606")

    def test_control_characters_are_stripped(self):
        """Управляющие символы ломают журнал и прячут часть строки."""
        out = V.validate(V.SCHEMAS["orderCancel"], {"reason": "пере\x00ду\x1bмал"})
        self.assertEqual(out["reason"], "передумал")

    def test_password_keeps_surrounding_spaces(self):
        """Пробел по краям пароля — часть пароля, обрезать его нельзя."""
        out = V.validate(V.SCHEMAS["staffLogin"],
                         {"login": "admin", "password": "  тайна  "})
        self.assertEqual(out["password"], "  тайна  ")

    def test_impossible_date_is_rejected(self):
        with self.assertRaises(V.ValidationError):
            V.validate(V.SCHEMAS["preorder"], {
                "product_id": 1, "weight": 1, "pickup_date": "2026-02-31",
                "name": "Иван", "phone": "+79166060606"})


if __name__ == "__main__":
    unittest.main()
