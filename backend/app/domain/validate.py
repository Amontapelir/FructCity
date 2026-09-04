"""Валидация входных данных: 23 схемы, белый список полей.

Принцип: сервер не доверяет клиенту ни в чём. Каждое поле проверяется
по схеме — тип, длина, диапазон, формат. Всё лишнее отбрасывается: в
результат попадают **только** ключи схемы.

Это не педантизм, а защита от подмены полей. Без белого списка клиент
прислал бы вместе с заказом `{"role": "admin"}` или `{"total": 1}`,
и лишнее поле доехало бы до записи. Отсюда и правило проекта: цена
приходит только из базы, а всё присланное игнорируется.

Сообщения об ошибках написаны для покупателя: он видит их дословно.
«Минимум 10 символов» не должно вдруг стать «too short» или `E_SHORT`.
"""

from __future__ import annotations

import math
import re
from typing import Any, Callable, Mapping, Sequence

__all__ = ["ValidationError", "validate", "run", "T", "SCHEMAS",
           "SKU_RE", "PROMO_RE", "SLUG_RE"]


class ValidationError(Exception):
    """Ошибка входных данных. Наружу уходит как 422 с картой полей."""

    status = 422

    def __init__(self, fields: dict[str, str]):
        super().__init__("validation_failed")
        self.fields = fields


# Значение, означающее «поле не прошло проверку». None использовать
# нельзя: у части полей None — законный результат.
_MISSING = object()

# Управляющие символы вырезаются всегда: смысла не несут, но ломают
# вывод в журнал и позволяют спрятать часть строки от глаз.
_CONTROL = re.compile("[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]")


def _js_len(s: str) -> int:
    """Длина как в JavaScript — в единицах UTF-16.

    Эмодзи в JS считается за два символа. Без этого поле, принятое
    одной реализацией, отвергалось бы другой.
    """
    return len(s.encode("utf-16-le")) // 2


def _to_number(v: Any) -> float:
    """`Number(String(v).replace(',', '.'))` из JavaScript.

    `Number` требует, чтобы числом была вся строка целиком: «12abc» —
    не число. Запятая принимается как разделитель, её набирают чаще.
    """
    if isinstance(v, bool):
        return math.nan
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", ".").strip()
    if s == "":
        return 0.0
    try:
        return float(s)
    except ValueError:
        try:
            return float(int(s, 0))          # Number("0x10") === 16
        except ValueError:
            return math.nan


def _js_round(x: float) -> float:
    """Округление `.5` вверх, как `Math.round`."""
    f = math.floor(x)
    return f if (x - f) < 0.5 else f + 1


class T:
    """Примитивы схемы. Каждый возвращает функцию (значение, имя, ошибки)."""

    @staticmethod
    def str_(min: int = 0, max: int = 500, trim: bool = True,
             pattern: re.Pattern | None = None,
             pattern_msg: str = "недопустимый формат") -> Callable:
        def check(v: Any, name: str, errs: dict) -> Any:
            if v is None:
                v = ""
            if isinstance(v, bool):
                errs[name] = "ожидается строка"
                return _MISSING
            if isinstance(v, (int, float)):
                v = _number_to_js_string(v)
            if not isinstance(v, str):
                errs[name] = "ожидается строка"
                return _MISSING
            v = _CONTROL.sub("", v)
            if trim:
                v = v.strip()
            length = _js_len(v)
            if length < min:
                errs[name] = ("обязательное поле" if min == 1
                              else f"минимум {min} символов")
                return _MISSING
            if length > max:
                errs[name] = f"не длиннее {max} символов"
                return _MISSING
            if pattern is not None and v and not pattern.search(v):
                errs[name] = pattern_msg
                return _MISSING
            return v
        return check

    @staticmethod
    def int_(min: int = -10**9, max: int = 10**9,
             required: bool = True, default: int = 0) -> Callable:
        def check(v: Any, name: str, errs: dict) -> Any:
            if v is None or v == "":
                if required:
                    errs[name] = "обязательное поле"
                    return _MISSING
                return default
            n = _to_number(v)
            if not math.isfinite(n):
                errs[name] = "ожидается число"
                return _MISSING
            i = math.trunc(n)
            if i < min or i > max:
                errs[name] = f"допустимо от {_num_text(min)} до {_num_text(max)}"
                return _MISSING
            return int(i)
        return check

    @staticmethod
    def num(min: float = -10**9, max: float = 10**9, required: bool = True,
            default: float = 0, decimals: int = 2) -> Callable:
        def check(v: Any, name: str, errs: dict) -> Any:
            if v is None or v == "":
                if required:
                    errs[name] = "обязательное поле"
                    return _MISSING
                return default
            n = _to_number(v)
            if not math.isfinite(n):
                errs[name] = "ожидается число"
                return _MISSING
            if n < min or n > max:
                errs[name] = f"допустимо от {_num_text(min)} до {_num_text(max)}"
                return _MISSING
            f = 10 ** decimals
            return _js_number(_js_round(n * f) / f)
        return check

    @staticmethod
    def bool_(default: bool = False) -> Callable:
        def check(v: Any, name: str, errs: dict) -> Any:
            if v is None or v == "":
                return default
            if isinstance(v, bool):
                return v
            return str(v).lower() in ("true", "1", "on", "yes")
        return check

    @staticmethod
    def enum_(values: Sequence[str], required: bool = True,
              default: Any = None) -> Callable:
        allowed = set(values)

        def check(v: Any, name: str, errs: dict) -> Any:
            if v is None or v == "":
                if required:
                    errs[name] = "обязательное поле"
                    return _MISSING
                return default
            s = _to_js_string(v)
            if s not in allowed:
                errs[name] = "допустимо: " + ", ".join(values)
                return _MISSING
            return s
        return check

    @staticmethod
    def phone(required: bool = True) -> Callable:
        """Телефон РФ, приведённый к +7XXXXXXXXXX.

        Один номер — одна запись: без нормализации «8 916…» и
        «+7 916…» стали бы разными покупателями с разной историей.
        """
        def check(v: Any, name: str, errs: dict) -> Any:
            if v is None or v == "":
                if required:
                    errs[name] = "укажите телефон"
                    return _MISSING
                return None
            digits = re.sub(r"\D", "", str(v))
            if len(digits) == 11 and digits[0] in ("8", "7"):
                digits = "7" + digits[1:]
            elif len(digits) == 10:
                digits = "7" + digits
            else:
                errs[name] = "телефон в формате +7 XXX XXX-XX-XX"
                return _MISSING
            if not (re.fullmatch(r"79\d{9}", digits)
                    or re.fullmatch(r"7[3-8]\d{9}", digits)):
                errs[name] = "телефон в формате +7 XXX XXX-XX-XX"
                return _MISSING
            return "+" + digits
        return check

    @staticmethod
    def email(required: bool = True) -> Callable:
        """Проверка намеренно нестрогая.

        Строгий разбор по стандарту даёт больше ложных отказов, чем
        пользы. Настоящая проверка адреса — письмо с подтверждением.
        """
        pattern = re.compile(r'^[^\s@,;<>"]+@[^\s@,;<>"]+\.[a-zA-Zа-яА-Я]{2,}$')

        def check(v: Any, name: str, errs: dict) -> Any:
            if v is None or v == "":
                if required:
                    errs[name] = "укажите email"
                    return _MISSING
                return None
            s = str(v).strip().lower()
            if _js_len(s) > 254 or not pattern.match(s):
                errs[name] = "проверьте email"
                return _MISSING
            return s
        return check

    @staticmethod
    def ymd(required: bool = True) -> Callable:
        """Дата ГГГГ-ММ-ДД с проверкой существования: 31 февраля не пройдёт."""
        shape = re.compile(r"^\d{4}-\d{2}-\d{2}$")

        def check(v: Any, name: str, errs: dict) -> Any:
            if not v:
                if required:
                    errs[name] = "укажите дату"
                    return _MISSING
                return None
            s = str(v).strip()
            if not shape.match(s):
                errs[name] = "дата в формате ГГГГ-ММ-ДД"
                return _MISSING
            from datetime import date

            y, m, d = (int(x) for x in s.split("-"))
            try:
                date(y, m, d)
            except ValueError:
                errs[name] = "такой даты не существует"
                return _MISSING
            return s
        return check

    @staticmethod
    def array_of(item: Callable, max: int = 200, min: int = 0) -> Callable:
        def check(v: Any, name: str, errs: dict) -> Any:
            if v is None:
                v = []
            if not isinstance(v, list):
                errs[name] = "ожидается список"
                return _MISSING
            if len(v) < min:
                errs[name] = f"минимум {min} элементов"
                return _MISSING
            if len(v) > max:
                errs[name] = f"не более {max} элементов"
                return _MISSING
            out = []
            for i, element in enumerate(v):
                sub: dict[str, str] = {}
                result = item(element, f"{name}[{i}]", sub)
                if sub:
                    errs.update(sub)
                    return _MISSING
                out.append(result)
            return out
        return check

    @staticmethod
    def object_(schema: Mapping[str, Callable]) -> Callable:
        def check(v: Any, name: str, errs: dict) -> Any:
            if v is None:
                v = {}
            if not isinstance(v, dict):
                errs[name] = "ожидается объект"
                return _MISSING
            sub: dict[str, str] = {}
            result = run(schema, v, sub)
            for k, msg in sub.items():
                errs[f"{name}.{k}"] = msg
            return result
        return check

    @staticmethod
    def optional(fn: Callable, default: Any = None) -> Callable:
        """Пустое значение допустимо и даёт значение по умолчанию."""
        def check(v: Any, name: str, errs: dict) -> Any:
            if v is None or v == "":
                return default
            return fn(v, name, errs)
        return check


def _num_text(v: float) -> str:
    """Число в текст так, как его печатает JS: 50, а не 50.0."""
    return str(int(v)) if float(v).is_integer() else str(v)


def _js_number(v: float) -> Any:
    return int(v) if float(v).is_integer() else v


def _number_to_js_string(v: Any) -> str:
    """`String(число)` из JS: 5 → «5», 5.5 → «5.5»."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _to_js_string(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return _number_to_js_string(v)
    return str(v)


def run(schema: Mapping[str, Callable], data: Any,
        errs: dict[str, str]) -> dict[str, Any]:
    """Прогон схемы. В результат попадают только ключи схемы."""
    src = data if isinstance(data, dict) else {}
    out: dict[str, Any] = {}
    for key, check in schema.items():
        value = check(src.get(key), key, errs)
        if value is not _MISSING:
            out[key] = value
    return out


def validate(schema: Mapping[str, Callable], data: Any) -> dict[str, Any]:
    """Основная точка входа. Ошибки — исключением, а не полем в ответе."""
    errs: dict[str, str] = {}
    out = run(schema, data, errs)
    if errs:
        raise ValidationError(errs)
    return out


# ---------------------------------------------------------------------------
# Готовые схемы по разделам ТЗ
# ---------------------------------------------------------------------------
SKU_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,31}$")
PROMO_RE = re.compile(r"^[A-ZА-Я0-9_-]{3,24}$")
SLUG_RE = re.compile(r"^[a-z0-9-]{1,80}$")
_DIGITS6 = re.compile(r"^\d{6}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CHAT_ID_RE = re.compile(r"^-?\d+$")
_LOGIN_RE = re.compile(r"^[a-z0-9._-]+$")
_CATEGORY_RE = re.compile(r"^[a-z0-9_]+$")
_IMAGE_KEY_RE = re.compile(r"^[a-z0-9_]*$")
_SECTION_RE = re.compile(r"^[a-z_]+$")

SCHEMAS: dict[str, dict[str, Callable]] = {
    # ---- авторизация покупателя (ТЗ 9.1) ----
    "otpRequest": {
        "phone": T.phone(),
        "consent": T.bool_(),                       # согласие 152-ФЗ
    },
    "otpVerify": {
        "phone": T.phone(),
        "code": T.str_(min=6, max=6, pattern=_DIGITS6, pattern_msg="код из 6 цифр"),
    },

    # ---- вход персонала ----
    "staffLogin": {
        "login": T.str_(min=3, max=64),
        # trim=False: пробел в начале или конце — часть пароля
        "password": T.str_(min=1, max=200, trim=False),
    },

    # ---- корзина (ТЗ 4.1) ----
    "cartAdd": {
        "product_id": T.int_(min=1),
        "qty": T.optional(T.int_(min=1, max=999), None),
        "weight": T.optional(T.num(min=0.1, max=50, decimals=2), None),
    },
    "cartUpdate": {
        "product_id": T.int_(min=1),
        "qty": T.optional(T.int_(min=0, max=999), None),
        "weight": T.optional(T.num(min=0, max=50, decimals=2), None),
    },
    "promoApply": {
        "code": T.str_(min=1, max=24),
    },

    # ---- оформление (ТЗ 4.3) ----
    "checkout": {
        "method": T.enum_(["delivery", "pickup"]),
        "zone_id": T.optional(T.int_(min=1), None),
        "address": T.str_(min=0, max=300),
        "slot_ymd": T.ymd(),
        "slot_from": T.int_(min=0, max=23),
        "name": T.str_(min=2, max=100),
        "phone": T.phone(),
        "email": T.email(),
        "comment": T.str_(min=0, max=500),
        "payment": T.enum_(["cash", "card_courier", "sbp", "online"]),
        "promo_code": T.str_(min=0, max=24),
        "consent": T.bool_(),                       # ТЗ 14.2 — обязателен
        "marketing_consent": T.bool_(),             # ТЗ 14.2 — по умолчанию нет
        "telegram_optin": T.bool_(),
    },

    # ---- предзаказ мяса (ТЗ 7.2) ----
    "preorder": {
        "product_id": T.int_(min=1),
        "weight": T.num(min=0.5, max=50, decimals=2),
        "pickup_date": T.ymd(),
        "name": T.str_(min=2, max=100),
        "phone": T.phone(),
        "comment": T.str_(min=0, max=500),
        "consent": T.bool_(),
    },

    "orderCancel": {
        "reason": T.str_(min=0, max=300),
    },

    # ---- привязка Telegram (ТЗ 2.1.12) ----
    "telegramConfirm": {
        "token": T.str_(min=10, max=64, pattern=_TOKEN_RE,
                        pattern_msg="некорректный токен"),
        "chat_id": T.str_(min=1, max=32, pattern=_CHAT_ID_RE,
                          pattern_msg="chat_id — число"),
    },

    # ---- админ: товары (ТЗ 10.1) ----
    "product": {
        "sku": T.str_(min=2, max=32, pattern=SKU_RE,
                      pattern_msg="латиница, цифры, дефис"),
        "name": T.str_(min=2, max=160),
        "slug": T.str_(min=0, max=80, pattern=SLUG_RE,
                       pattern_msg="латиница, цифры, дефис"),
        "category_id": T.str_(min=1, max=40),
        "type": T.enum_(["unit", "weighted", "preorder"]),
        "price": T.optional(T.num(min=0, max=1000000), 0),
        "price_per_kg": T.optional(T.num(min=0, max=1000000), 0),
        "sale_price": T.optional(T.num(min=0, max=1000000), None),
        "sale_until": T.optional(T.ymd(required=False), None),
        "vat_rate": T.enum_(["0", "10", "20"]),
        "stock": T.num(min=0, max=1000000, decimals=2),
        "min_weight": T.optional(T.num(min=0.1, max=50), 0.5),
        "weight_step": T.optional(T.num(min=0.1, max=10), 0.5),
        "is_active": T.bool_(default=True),
        "image_key": T.str_(min=0, max=40, pattern=_IMAGE_KEY_RE,
                            pattern_msg="ключ фото: латиница и подчёркивание"),
        # Дополнительные фото — та же галерея, но за обложкой (ROADMAP
        # 2.11). Восемь достаточно для карточки продукта в маленьком
        # магазине; required=False — старые вызовы (например, тесты
        # прежних сессий) без этого поля не ломаются.
        "extra_image_keys": T.optional(T.array_of(
            T.str_(min=0, max=40, pattern=_IMAGE_KEY_RE,
                  pattern_msg="ключ фото: латиница и подчёркивание"), max=8), []),
        "emoji": T.str_(min=0, max=8),
        "description": T.str_(min=0, max=2000),
    },

    # ---- админ: категории (ТЗ 10.2) ----
    "category": {
        "id": T.str_(min=2, max=40, pattern=_CATEGORY_RE,
                     pattern_msg="латиница и подчёркивание"),
        "name": T.str_(min=2, max=80),
        "emoji": T.str_(min=0, max=8),
        "is_active": T.bool_(default=True),
    },

    # ---- админ: промокоды (ТЗ 10.5) ----
    "promocode": {
        "code": T.str_(min=3, max=24, pattern=PROMO_RE,
                       pattern_msg="заглавные буквы, цифры, дефис"),
        "type": T.enum_(["percent", "fixed", "delivery"]),
        "value": T.num(min=1, max=100000),
        "min_order": T.num(min=0, max=1000000, required=False, default=0),
        "uses_limit": T.int_(min=0, max=1000000, required=False, default=0),
        "per_user_limit": T.int_(min=0, max=1000, required=False, default=1),
        "valid_until": T.optional(T.ymd(required=False), None),
        "is_active": T.bool_(default=True),
    },

    # ---- админ: сотрудники (ТЗ 10.8) ----
    "staff": {
        "name": T.str_(min=2, max=120),
        "login": T.str_(min=3, max=64, pattern=_LOGIN_RE,
                        pattern_msg="латиница, цифры, точка, дефис"),
        "phone": T.phone(required=False),
        "role": T.enum_(["admin", "manager"]),
        "password": T.str_(min=0, max=200, trim=False),
    },

    # ---- админ: заказы (ТЗ 10.3) ----
    "orderStatus": {
        "status": T.enum_(["new", "awaiting_payment", "assembling",
                           "partially_assembled", "ready", "in_delivery",
                           "delivered", "cancelled"]),
        "comment": T.str_(min=0, max=300),
    },
    "orderItemWeight": {
        "item_id": T.int_(min=1),
        "actual_weight": T.num(min=0, max=50, decimals=3),
        # подтверждение веса вне допуска ±10% после звонка клиенту (ТЗ 3.4)
        "confirm": T.bool_(),
    },
    "orderItemRemove": {
        "item_id": T.int_(min=1),
        "is_removed": T.bool_(),
    },
    "paymentStatus": {
        "payment_status": T.enum_(["pending", "paid", "refunded"]),
    },

    # ---- админ: доставка (ТЗ 10.6) ----
    "deliveryZone": {
        "id": T.int_(min=1),
        "cost": T.optional(T.num(min=0, max=100000), None),
        "free_from": T.optional(T.num(min=0, max=1000000), None),
    },
    "settings": {
        "work_from": T.int_(min=0, max=23),
        "work_to": T.int_(min=1, max=24),
        "cutoff_h": T.int_(min=0, max=24),
        "horizon_d": T.int_(min=1, max=30),
        "slot_capacity_delivery": T.int_(min=1, max=1000),
        "slot_capacity_pickup": T.int_(min=1, max=1000),
        "pickup_address": T.str_(min=3, max=300),
        "phone": T.str_(min=3, max=40),
        "email": T.email(required=False),
        "meat_days": T.array_of(
            T.enum_(["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]), max=7),
        "meat_limit_kg": T.num(min=0, max=100000),
        "meat_cutoff_days": T.int_(min=0, max=14),
        "requisites": T.str_(min=0, max=300),
        # Даты-исключения слотов доставки/самовывоза (ТЗ 4.4) — список
        # растёт руками из админки, сотней с запасом хватит на годы вперёд.
        "holidays": T.array_of(T.ymd(), max=100),
    },

    # ---- админ: предзаказы (ТЗ 10.4) ----
    "preorderStatus": {
        "status": T.enum_(["new", "confirmed", "ready", "done", "cancelled"]),
    },

    # ---- админ: конструктор главной ----
    "homeConfig": {
        "hero_tag": T.str_(min=0, max=120),
        "hero_title": T.str_(min=1, max=120),
        "hero_text": T.str_(min=0, max=1000),
        "sale_skus": T.array_of(T.str_(min=1, max=32, pattern=SKU_RE), max=12),
        "meat_skus": T.array_of(T.str_(min=1, max=32, pattern=SKU_RE), max=12),
        "sections": T.array_of(T.object_({
            "id": T.str_(min=1, max=24, pattern=_SECTION_RE),
            "is_visible": T.bool_(default=True),
        }), max=20),
    },

    # ---- импорт CSV (ТЗ 10.7) ----
    "csvImport": {
        "csv": T.str_(min=1, max=2 * 1024 * 1024, trim=False),
        # required=False + default: старые вызовы без поля режима не
        # ломаются — ведут себя как раньше, единственным известным
        # способом ("full").
        "mode": T.enum_(["full", "prices_stock", "new_only"],
                       required=False, default="full"),
    },
}
