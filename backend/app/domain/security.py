"""Безопасность: пароли, токены, экранирование, лимиты.

**Ни одной внешней зависимости.** Это не аскеза: слой, который решает,
пускать человека или нет, должен проверяться без установки чего-либо.
`hashlib.scrypt`, `hmac` и `secrets` дают ровно то, что нужно, и входят
в стандартную библиотеку.

**Формат хеша самоописателен:** `scrypt$N$r$p$соль$хеш`. Параметры
лежат в самой строке, поэтому проверка не зависит от того, какими
настройками пароль был записан, — а записаны часть паролей ещё прежней
версией магазина. Менять параметры по умолчанию можно, старые хеши
продолжат приниматься; менять формат строки — нет, персонал перестанет
входить с сообщением «неверный пароль» при верном пароле.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from base64 import b64decode, b64encode, urlsafe_b64encode
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "hash_password", "verify_password", "password_problems",
    "token", "otp_code", "hash_otp", "timing_safe_equal", "mask_phone",
    "escape_html", "escape_json_for_html",
    "check_csrf", "safe_origin", "CSRF_COOKIE", "SESSION_COOKIE", "SAFE_METHODS",
    "RateLimiter", "LIMITS",
]

# Параметры записываются в саму строку хеша, поэтому старые пароли
# продолжат проверяться со старыми значениями, а новые получат эти.
# Менять можно; понижать — нельзя, это ослабление защиты задним числом.
SCRYPT_N = 16384
SCRYPT_R = 8
SCRYPT_P = 1
KEYLEN = 64

# Предел памяти задаётся явно: без него `hashlib.scrypt` упирается во
# внутреннее ограничение и падает на больших N — то есть ровно тогда,
# когда параметры усилили ради безопасности.
_MAXMEM = 64 * 1024 * 1024


def _scrypt(password: str, salt: bytes, keylen: int,
            n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> bytes:
    return hashlib.scrypt(str(password).encode("utf-8"), salt=salt,
                          n=n, r=r, p=p, dklen=keylen, maxmem=_MAXMEM)


def hash_password(password: Any) -> str:
    """Хеш пароля персонала.

    scrypt, а не sha256: он намеренно медленный и требует памяти, что
    делает перебор украденной базы дорогим. Соль своя у каждого хеша,
    поэтому одинаковые пароли дают разные строки.
    """
    salt = secrets.token_bytes(16)
    key = _scrypt(password, salt, KEYLEN)
    return (f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}$"
            f"{b64encode(salt).decode()}${b64encode(key).decode()}")


def verify_password(password: Any, stored: Any) -> bool:
    """Проверка пароля. Любая неожиданность — «не подошёл», без исключений.

    Параметры берутся из самой строки хеша, а не из констант выше:
    иначе смена параметров в коде разом обесценила бы все записанные
    пароли.
    """
    try:
        parts = str(stored or "").split("$")
        if len(parts) != 6 or parts[0] != "scrypt":
            return False
        _, n, r, p, salt_b64, key_b64 = parts
        salt = b64decode(salt_b64)
        expected = b64decode(key_b64)
        actual = _scrypt(password, salt, len(expected), int(n), int(r), int(p))
        return hmac.compare_digest(actual, expected)
    except Exception:  # noqa: BLE001 — битый хеш это «не подошёл», а не сбой
        return False


def timing_safe_equal(a: Any, b: Any) -> bool:
    """Сравнение, не выдающее ответ временем работы.

    Сравниваются **байты**, а не строки: `hmac.compare_digest` на строке
    с не-ASCII символом бросает TypeError, а сравниваемое приходит от
    клиента — и запрос падал бы с ошибкой сервера вместо честного
    отказа. Нашлось дифференциальной сверкой во время переезда.
    """
    ba = a if isinstance(a, (bytes, bytearray)) else str(a).encode("utf-8")
    bb = b if isinstance(b, (bytes, bytearray)) else str(b).encode("utf-8")
    return hmac.compare_digest(bytes(ba), bytes(bb))


def mask_phone(phone: Any) -> str:
    """Номер для журнала и для ответа «код отправлен».

    Целиком его показывать нельзя: ответ на запрос кода виден любому,
    кто знает начало номера, и полный номер из него собирать не надо.
    """
    s = str(phone or "")
    if _js_length(s) < 7:
        return "***"
    return s[:2] + "***" + s[-2:]


_LOWER = re.compile(r"[a-zа-я]")
_UPPER = re.compile(r"[A-ZА-Я]")
_DIGIT = re.compile(r"[0-9]")
WEAK_PARTS = ("password", "123456", "qwerty", "admin", "фруктовый", "fructcity")


def _js_length(s: str) -> int:
    """Длина строки так, как её считает JavaScript.

    В JS `.length` — это число единиц UTF-16, и эмодзи считается за
    две. В Python `len` считает кодовые точки, поэтому пароль с эмодзи
    одна реализация признавала бы достаточно длинным, а другая — нет.
    Пароль, принятый при смене и отвергнутый при входе, — худший из
    возможных исходов.
    """
    return len(s.encode("utf-16-le")) // 2


def password_problems(pw: Any) -> list[str]:
    """Что не так с паролем персонала. Пустой список — всё в порядке."""
    s = str(pw or "")
    out = []
    if _js_length(s) < 10:
        out.append("минимум 10 символов")
    if not _LOWER.search(s):
        out.append("нужна строчная буква")
    if not _UPPER.search(s):
        out.append("нужна заглавная буква")
    if not _DIGIT.search(s):
        out.append("нужна цифра")
    if any(w in s.lower() for w in WEAK_PARTS):
        out.append("слишком предсказуемый")
    return out


# ---------------------------------------------------------------------------
# Токены
# ---------------------------------------------------------------------------
def token(nbytes: int = 32) -> str:
    """256 бит энтропии — перебором не угадать.

    base64url без выравнивающих знаков «=»: токен попадает в cookie и в
    адресную строку, где «=» и «/» пришлось бы экранировать.
    """
    return urlsafe_b64encode(secrets.token_bytes(nbytes)).decode().rstrip("=")


def otp_code() -> str:
    """Шестизначный код для SMS (ТЗ 9.1).

    Криптостойкий генератор и отбрасывание хвоста диапазона: обычный
    остаток от деления сделал бы первые цифры чуть вероятнее
    остальных, а перебор шестизначного кода и так недорог.
    """
    while True:
        n = int.from_bytes(secrets.token_bytes(4), "big")
        if n < 4_294_000_000:
            return f"{n % 1_000_000:06d}"


def hash_otp(code: Any, salt: Any) -> str:
    """Хеш кода подтверждения — в базе кода в открытом виде нет."""
    digest = hmac.new(str(salt).encode("utf-8"),
                      str(code).encode("utf-8"), hashlib.sha256).digest()
    return b64encode(digest).decode()


# ---------------------------------------------------------------------------
# Экранирование
# ---------------------------------------------------------------------------
_HTML_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}
_HTML_RE = re.compile(r"[&<>\"']")


def escape_html(s: Any) -> str:
    """Всё, что ввели люди, проходит через это перед вставкой в разметку."""
    if s is None:
        return ""
    return _HTML_RE.sub(lambda m: _HTML_ESCAPES[m.group(0)], str(s))


def escape_json_for_html(obj: Any) -> str:
    """JSON для вставки внутрь тега script.

    U+2028 и U+2029 — валидный JSON, но в литерале JavaScript это
    обрыв строки: страница ломается на ровном месте. Угловые скобки
    экранируем, чтобы данные не закрыли тег преждевременно.
    """
    import json

    # separators без пробелов: JSON.stringify в JS печатает {"a":1},
    # а json.dumps по умолчанию {"a": 1}. Для витрины разницы нет, но
    # это тот же класс расхождения, что и 99 против 99.0.
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------
CSRF_COOKIE = "fc_csrf"
SESSION_COOKIE = "fc_sid"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def safe_origin(url: Any) -> str | None:
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(str(url))
        if not parts.scheme or not parts.netloc:
            return None
        return f"{parts.scheme}://{parts.netloc}"
    except ValueError:
        return None


def check_csrf(method: str, headers: Any, cookies: Any,
               allowed_origins: Any) -> dict[str, Any]:
    """Двойная защита.

    Первая: токен из cookie обязан совпасть с заголовком. Прочитать
    cookie чужого домена нельзя, поэтому подставить оба значения
    межсайтовый запрос не может.

    Вторая: проверка Origin. Она отсекает запрос, даже если токен
    как-то угадан. Origin приходит не всегда — тогда смотрим Referer.
    """
    if str(method).upper() in SAFE_METHODS:
        return {"ok": True}

    cookie_token = _get(cookies, CSRF_COOKIE)
    header_token = _get(headers, "x-csrf-token")
    if not cookie_token or not header_token:
        return {"ok": False, "reason": "csrf_token_mismatch"}
    # Сравниваем байты, а не строки: compare_digest на строке с
    # символом вне ASCII бросает TypeError. Токен приходит от клиента,
    # то есть подставить туда кириллицу может кто угодно — и запрос
    # падал бы с ошибкой сервера вместо честного отказа.
    if not hmac.compare_digest(str(cookie_token).encode("utf-8"),
                               str(header_token).encode("utf-8")):
        return {"ok": False, "reason": "csrf_token_mismatch"}

    origin = _get(headers, "origin")
    if not origin:
        referer = _get(headers, "referer")
        origin = safe_origin(referer) if referer else None
    if origin and origin not in set(allowed_origins or ()):
        return {"ok": False, "reason": "bad_origin"}
    return {"ok": True}


def _get(container: Any, key: str) -> Any:
    """Достаёт значение из словаря или из заголовков без учёта регистра."""
    if container is None:
        return None
    try:
        value = container.get(key)
    except AttributeError:
        return None
    if value is not None:
        return value
    lowered = key.lower()
    try:
        for k, v in container.items():
            if str(k).lower() == lowered:
                return v
    except AttributeError:
        return None
    return None


# ---------------------------------------------------------------------------
# Ограничение частоты
# ---------------------------------------------------------------------------
@dataclass
class RateLimiter:
    """Скользящее окно в памяти.

    Для одного процесса этого достаточно. При нескольких счётчики
    переедут в общее хранилище, но интерфейс останется тем же —
    поэтому вызывающий код о замене не узнает.
    """

    hits: dict[str, list[float]] = field(default_factory=dict)

    def check(self, key: str, limit: int, window_ms: int,
              now: float | None = None) -> dict[str, Any]:
        now = now if now is not None else time.time() * 1000
        cutoff = now - window_ms
        live = [t for t in self.hits.get(key, []) if t > cutoff]

        if len(live) >= limit:
            self.hits[key] = live
            retry_after = -(-(live[0] + window_ms - now) // 1000)   # округление вверх
            return {"allowed": False, "remaining": 0,
                    "retryAfter": max(1, int(retry_after))}

        live.append(now)
        self.hits[key] = live
        return {"allowed": True, "remaining": limit - len(live), "retryAfter": 0}

    def reset(self, key: str) -> None:
        """Откатить попытки — например, когда вход удался."""
        self.hits.pop(key, None)

    def sweep(self, max_age_ms: int = 60 * 60 * 1000, now: float | None = None) -> None:
        """Чистка, чтобы словарь не рос бесконечно.

        Без неё поток запросов с разных адресов медленно съедает
        память процесса — отказ в обслуживании без единой ошибки в коде.
        """
        now = now if now is not None else time.time() * 1000
        for key in [k for k, arr in self.hits.items()
                    if not arr or arr[-1] <= now - max_age_ms]:
            self.hits.pop(key, None)


# Лимиты в одном месте — их легко пересмотреть, не бегая по коду.
LIMITS = {
    "otpSend":    {"limit": 3,   "windowMs": 15 * 60 * 1000},   # ТЗ 9.1
    "otpVerify":  {"limit": 3,   "windowMs": 15 * 60 * 1000},   # ТЗ 9.1
    "promo":      {"limit": 5,   "windowMs": 10 * 60 * 1000},   # ТЗ 6.1
    "adminLogin": {"limit": 5,   "windowMs": 15 * 60 * 1000},
    "orderPlace": {"limit": 10,  "windowMs": 10 * 60 * 1000},
    "api":        {"limit": 300, "windowMs": 60 * 1000},
}
