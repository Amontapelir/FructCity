"""Каждый маршрут админки проверяет право на сервере.

Проверка статическая — обходом исходника, а не запросами. Причина в
том, какую ошибку она ловит: забытый `require_staff` не ломает ничего
видимого. Маршрут работает, страница открывается, тесты на его
поведение зелёные — просто открыт он всем. Такое находится не тестом
поведения, а перебором всех обработчиков подряд.

Скрытие пункта меню на клиенте защитой не является: адрес известен, и
запрос отправляется без интерфейса.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADMIN_PY = ROOT / "backend" / "app" / "api" / "routers" / "admin.py"


class AdminRoutesGuarded(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not ADMIN_PY.exists():
            raise unittest.SkipTest("admin.py ещё не создан")
        cls.text = ADMIN_PY.read_text(encoding="utf-8")

    def _handlers(self) -> list[tuple[str, str, str]]:
        """(метод, имя_функции, тело) для каждого обработчика маршрута."""
        chunks = re.split(r"\n@router\.", self.text)[1:]
        out = []
        for chunk in chunks:
            head, _, body = chunk.partition("\n")
            method = head.split("(")[0].upper()
            name = re.search(r"def (\w+)", body)
            out.append((method, name.group(1) if name else "?", body))
        return out

    def test_every_handler_checks_staff(self):
        """`require_staff` — единственная проверка доступа в этом файле.

        Спрятанный на клиенте пункт меню защитой не является: адрес
        маршрута виден в исходниках витрины, и без проверки на сервере
        любой запрос напрямую получил бы данные.
        """
        offenders = [name for _, name, body in self._handlers()
                    if name != "login"           # вход — единственная точка входа без сессии
                    and "ctx.require_staff(" not in body]
        self.assertEqual(offenders, [],
                         "обработчики без проверки прав: " + ", ".join(offenders))

    def test_every_mutating_handler_opens_a_transaction(self):
        offenders = [name for method, name, body in self._handlers()
                    if method in ("POST", "PUT", "DELETE", "PATCH")
                    and name != "login"          # вход выдаёт сессию — своя транзакция
                    and "ctx.tx(" not in body]
        self.assertEqual(offenders, [],
                         "изменяющие обработчики без транзакции: " + ", ".join(offenders))
        # Вход — не исключение из правила, а частный случай: транзакция
        # там есть, просто это единственный маршрут, создающий сессию
        # до проверки пароля.
        login_body = next(body for _, name, body in self._handlers() if name == "login")
        self.assertIn("ctx.tx(", login_body)

    def test_no_csrf_exemptions(self):
        """В админке нет вебхуков — исключений из CSRF быть не должно."""
        self.assertNotIn("csrf=False", self.text)


if __name__ == "__main__":
    unittest.main()
