"""Разбор параметров строки запроса по правилам JavaScript.

Отдельный модуль, а не пара функций внутри роутера: во-первых, здесь
нет зависимости от FastAPI, и функции проверяются сверкой с JS без
поднятия приложения; во-вторых, это ровно то место, где две реализации
расходятся незаметнее всего.

Пример, ради которого модуль и появился: на `?offset=abc` FastAPI с
параметром типа ``int`` отвечает 422. Витрина рассчитывает на другое —
мусор приводится к нулю и отдаётся первая страница. Опечатка в адресе
не повод показывать покупателю пустой каталог, а `public/app.js`
собирает эти адреса сам и обработки 422 для них не имеет.
"""

from __future__ import annotations

import math
import re

__all__ = ["num_param", "int_param"]

_INT_PREFIX = re.compile(r"^[+-]?\d+")


def num_param(raw: str | None) -> float | None:
    """Граница цены. ``None`` означает «границу не задали».

    Повторяет ``Number(String(raw).replace(',', '.'))`` из JavaScript, а
    не ``parseFloat``: ``Number`` требует, чтобы вся строка была числом,
    поэтому «100abc» границей не считается. Пустая строка и мусор дают
    отсутствие границы, а не ноль, — иначе `price_min=` выкинул бы из
    выдачи весь каталог.

    Запятая принимается как разделитель: в русской раскладке её
    набирают чаще точки.
    """
    if raw is None or raw == "":
        return None
    s = str(raw).replace(",", ".").strip()
    if s == "":
        return 0.0                     # Number(" ") === 0, повторяем буквально
    try:
        n = float(s)
    except ValueError:
        # Number() понимает шестнадцатеричную, восьмеричную и двоичную
        # записи: Number("0x10") это 16. Мелочь, но дифференциальная
        # сверка с прежней JS-версией показала расхождение именно
        # здесь, а чинить разбор выборочно — значит оставить
        # следующую такую же мину.
        try:
            n = float(int(s, 0))
        except ValueError:
            return None
    if math.isnan(n) or math.isinf(n):
        return None
    return n if n >= 0 else None


def int_param(raw: str | None, fallback: int) -> int:
    """Аналог ``parseInt(raw || String(fallback), 10) || fallback``.

    ``parseInt`` берёт числовой префикс: «5.9» это 5, «12abc» это 12,
    «abc» числом не является. Ноль в JS ложен, поэтому ``limit=0``
    превращается в 60 — повторяем и это, иначе страницы разъедутся.
    """
    if raw is None or raw == "":
        return fallback
    m = _INT_PREFIX.match(str(raw).strip())
    if not m:
        return fallback
    n = int(m.group(0))
    return n if n != 0 else fallback
