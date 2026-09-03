"""Сессии, роли и вход персонала.

Чистые функции над состоянием, как и остальной домен. Записи в базу
здесь нет: функции, которые *создают* сессию, возвращают готовую
запись, а сохраняет её слой хранилища внутри транзакции. Так сессия не
может появиться в обход `uow.py` — и, например, на безопасном GET.

Два решения перенесены дословно, и оба неочевидны:

**Ответ на неверный логин не отличается от ответа на неверный пароль.**
Иначе перебором можно узнать список логинов персонала. Хеш считается
даже когда пользователя нет — иначе разницу выдало бы время ответа.

**При смене уровня доступа выдаётся новый идентификатор сессии.**
Защита от подмены сессии: если злоумышленник заранее навязал жертве
известный ему идентификатор, после входа он стал бы идентификатором
входа. При этом переносится всё пользовательское состояние — корзина,
промокод, ссылки на недавние заказы, — иначе гость, оформивший заказ
и вошедший потом, терял бы к нему доступ.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from . import security as sec

__all__ = [
    "SESSION_TTL_MS", "STAFF_TTL_MS", "OTP_TTL_MS", "OTP_MAX_ATTEMPTS",
    "TG_LINK_TTL_MS", "MAX_SESSIONS",
    "new_session", "find_session", "rotated_session", "prune_sessions",
    "create_session", "destroy_session",
    "issue_otp", "verify_otp",
    "issue_telegram_link", "confirm_telegram_link", "unlink_telegram",
    "find_user_by_phone", "ensure_customer", "link_guest_orders",
    "verify_staff", "can", "permissions_of", "PERMISSIONS", "STAFF_ROLES",
    "iso_now", "parse_iso",
]

SESSION_TTL_MS = 30 * 24 * 3600 * 1000    # 30 дней для покупателя
STAFF_TTL_MS = 12 * 3600 * 1000           # 12 часов для персонала — короче
OTP_TTL_MS = 5 * 60 * 1000                # ТЗ 9.1 — код живёт 5 минут
OTP_MAX_ATTEMPTS = 3                      # ТЗ 9.1
TG_LINK_TTL_MS = 15 * 60 * 1000
MAX_SESSIONS = 20000                      # потолок, чтобы хранилище не пухло


def iso_now(moment: datetime | None = None) -> str:
    """Время как его печатает JavaScript: ISO с «Z» и миллисекундами.

    `toISOString()` в JS всегда даёт три знака после запятой и «Z» на
    конце. Питоновский `isoformat()` даёт микросекунды и «+00:00» —
    строки перестали бы сравниваться, а по ним ищется просроченность.
    """
    dt = (moment or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_iso(value: Any) -> datetime | None:
    """Разбор времени из хранилища. Мусор — это «нет времени», а не сбой."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Сессии
# ---------------------------------------------------------------------------
def new_session(
    *,
    next_id: Callable[[str], int],
    user_id: int | None = None,
    role: str = "guest",
    ttl_ms: int = SESSION_TTL_MS,
    cart: list | None = None,
    ip: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Готовая запись сессии. Сохранение — забота вызывающего."""
    moment = now or datetime.now(timezone.utc)
    return {
        "id": next_id("sessions"),
        "sid": sec.token(32),
        "user_id": user_id,
        "role": role,
        "cart": list(cart or []),
        "promo_code": None,
        "recent_orders": [],
        "recent_preorders": [],
        "created_at": iso_now(moment),
        "last_seen": iso_now(moment),
        "expires_at": iso_now(moment + timedelta(milliseconds=ttl_ms)),
        "ip": ip,
    }


def find_session(state: Mapping[str, Any], sid: Any,
                 now: datetime | None = None) -> dict[str, Any] | None:
    """Живая сессия по идентификатору из cookie.

    Просроченная — это отсутствующая: возвращаем None, а не запись с
    признаком. Иначе вызывающий код однажды забудет проверить признак,
    и просроченная сессия продолжит работать.
    """
    if not sid or not isinstance(sid, str):
        return None
    moment = now or datetime.now(timezone.utc)
    for s in state.get("sessions", []):
        if s.get("sid") != sid:
            continue
        expires = parse_iso(s.get("expires_at"))
        if expires is None or expires <= moment:
            return None
        return s
    return None


def rotated_session(session: Mapping[str, Any], *, next_id: Callable[[str], int],
                    now: datetime | None = None, **patch: Any) -> dict[str, Any]:
    """Новая сессия взамен старой — с переносом всего пользовательского.

    Идентификатор меняется при смене уровня доступа. Не перенести
    корзину или список недавних заказов значит наказать покупателя за
    то, что он вошёл: заказ, оформленный гостем, стал бы недоступен.
    """
    # `cart` и `ip` по умолчанию берём у старой сессии, но `patch` их
    # обязан уметь переопределить: вход в админку явно сбрасывает
    # корзину, даже если у анонимной сессии она почему-то не пуста.
    # Раньше `cart`/`ip` передавались отдельным именованным аргументом
    # и падали с «got multiple values», если `patch` тоже нёс `cart`.
    defaults: dict[str, Any] = {"cart": list(session.get("cart") or []),
                                "ip": session.get("ip")}
    defaults.update(patch)
    fresh = new_session(next_id=next_id, now=now, **defaults)
    fresh["promo_code"] = session.get("promo_code") or None
    fresh["recent_orders"] = list(session.get("recent_orders") or [])
    fresh["recent_preorders"] = list(session.get("recent_preorders") or [])
    return fresh


def prune_sessions(sessions: list[dict[str, Any]],
                   limit: int = MAX_SESSIONS) -> list[dict[str, Any]]:
    """Оставляет самые свежие сессии.

    Анонимные заходы создают сессию каждому посетителю, и без потолка
    хранилище растёт бесконечно — отказ по памяти без единой ошибки
    в коде.
    """
    if len(sessions) <= limit:
        return sessions
    ordered = sorted(sessions, key=lambda s: str(s.get("last_seen") or ""))
    return ordered[len(ordered) - limit:]


def create_session(state: dict[str, Any], *, next_id: Callable[[str], int],
                   now: datetime | None = None, **kw: Any) -> dict[str, Any]:
    """Заводит сессию и кладёт её в состояние.

    В отличие от `new_session` меняет состояние: это операция, а не
    расчёт. Здесь же срабатывает потолок числа сессий — иначе поток
    анонимных заходов раздувает хранилище без единой ошибки в коде.
    """
    session = new_session(next_id=next_id, now=now, **kw)
    sessions = state.setdefault("sessions", [])
    sessions.append(session)
    if len(sessions) > MAX_SESSIONS:
        state["sessions"] = prune_sessions(sessions)
    return session


def destroy_session(state: dict[str, Any], sid: Any) -> bool:
    """Выход. Удаляем запись, а не помечаем: помеченную однажды забудут
    проверить, и «вышедший» пользователь останется внутри."""
    sessions = state.get("sessions") or []
    for i, s in enumerate(sessions):
        if s.get("sid") == sid:
            sessions.pop(i)
            return True
    return False


# ---------------------------------------------------------------------------
# Одноразовый код (ТЗ 9.1)
# ---------------------------------------------------------------------------
def issue_otp(state: dict[str, Any], *, next_id: Callable[[str], int],
              phone: str, now: datetime | None = None) -> dict[str, Any]:
    """Выпускает код. В хранилище уходит только HMAC, как и с паролем.

    Сам код возвращается вызывающему — его отправляют в SMS. Прежние
    коды на этот номер гасим: иначе их можно копить и подбирать по
    очереди, обходя лимит попыток на одну запись.
    """
    moment = now or datetime.now(timezone.utc)
    state["otp"] = [o for o in (state.get("otp") or []) if o.get("phone") != phone]

    code = sec.otp_code()
    salt = sec.token(16)
    state["otp"].append({
        "id": next_id("otp"),
        "phone": phone,
        "salt": salt,
        "code_hash": sec.hash_otp(code, salt),
        "attempts": 0,
        "created_at": iso_now(moment),
        "expires_at": iso_now(moment + timedelta(milliseconds=OTP_TTL_MS)),
        "used_at": None,
    })
    return {"code": code, "expires_in_sec": OTP_TTL_MS // 1000}


def verify_otp(state: dict[str, Any], phone: Any, code: Any,
               now: datetime | None = None) -> dict[str, Any]:
    """Проверка кода. Счётчик попыток растёт до сравнения.

    Именно до: иначе неудачная попытка, прерванная на полпути, не
    стоила бы ничего, и код перебирался бы за тысячу запросов.
    """
    moment = now or datetime.now(timezone.utc)
    rec = next((o for o in (state.get("otp") or [])
                if o.get("phone") == phone and not o.get("used_at")), None)
    if rec is None:
        return {"ok": False, "reason": "code_not_requested"}

    expires = parse_iso(rec.get("expires_at"))
    if expires is None or expires <= moment:
        return {"ok": False, "reason": "code_expired"}
    if int(rec.get("attempts") or 0) >= OTP_MAX_ATTEMPTS:
        return {"ok": False, "reason": "too_many_attempts"}

    rec["attempts"] = int(rec.get("attempts") or 0) + 1
    actual = sec.hash_otp(code, rec.get("salt"))
    if not sec.timing_safe_equal(actual, rec.get("code_hash")):
        return {"ok": False, "reason": "code_invalid",
                "attempts_left": OTP_MAX_ATTEMPTS - rec["attempts"]}

    rec["used_at"] = iso_now(moment)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Привязка Telegram (ТЗ 2.1.12, 8.2)
# ---------------------------------------------------------------------------
def issue_telegram_link(state: dict[str, Any], *, next_id: Callable[[str], int],
                        user_id: int, now: datetime | None = None) -> dict[str, Any]:
    """Одноразовый короткоживущий токен для ссылки в бота.

    Токен уезжает в чужой мессенджер и остаётся в истории переписки.
    Поэтому он живёт пятнадцать минут и срабатывает один раз: перехват
    не должен давать доступ к профилю навсегда.
    """
    moment = now or datetime.now(timezone.utc)
    state["tg_links"] = [l for l in (state.get("tg_links") or [])
                         if l.get("user_id") != user_id or l.get("used_at")]
    rec = {
        "id": next_id("tg_links"),
        "user_id": user_id,
        "token": sec.token(24),
        "chat_id": None,
        "created_at": iso_now(moment),
        "expires_at": iso_now(moment + timedelta(milliseconds=TG_LINK_TTL_MS)),
        "used_at": None,
    }
    state["tg_links"].append(rec)
    return rec


def confirm_telegram_link(state: dict[str, Any], token: Any, chat_id: Any,
                          now: datetime | None = None) -> dict[str, Any]:
    moment = now or datetime.now(timezone.utc)
    rec = next((l for l in (state.get("tg_links") or [])
                if l.get("token") == token), None)
    if rec is None:
        return {"ok": False, "reason": "link_not_found"}
    if rec.get("used_at"):
        return {"ok": False, "reason": "link_already_used"}
    expires = parse_iso(rec.get("expires_at"))
    if expires is None or expires <= moment:
        return {"ok": False, "reason": "link_expired"}

    user = next((u for u in (state.get("users") or [])
                 if u.get("id") == rec.get("user_id")), None)
    if user is None:
        return {"ok": False, "reason": "user_not_found"}

    # Один chat_id — один профиль. Иначе уведомления о чужих заказах
    # приходили бы в общий чат вместе со своими.
    taken = next((u for u in (state.get("users") or [])
                  if u.get("telegram_chat_id") == str(chat_id)
                  and u.get("id") != user.get("id")), None)
    if taken is not None:
        return {"ok": False, "reason": "chat_already_linked"}

    user["telegram_chat_id"] = str(chat_id)
    rec["used_at"] = iso_now(moment)
    rec["chat_id"] = str(chat_id)
    return {"ok": True, "user": user}


def unlink_telegram(state: dict[str, Any], user_id: Any) -> bool:
    user = next((u for u in (state.get("users") or [])
                 if u.get("id") == user_id), None)
    if user is None:
        return False
    user["telegram_chat_id"] = None
    state["tg_links"] = [l for l in (state.get("tg_links") or [])
                         if l.get("user_id") != user_id]
    return True


# ---------------------------------------------------------------------------
# Покупатели
# ---------------------------------------------------------------------------
def find_user_by_phone(state: Mapping[str, Any], phone: Any) -> dict[str, Any] | None:
    return next((u for u in (state.get("users") or [])
                 if u.get("phone") == phone), None)


def ensure_customer(state: dict[str, Any], *, next_id: Callable[[str], int],
                    phone: str, now: datetime | None = None) -> dict[str, Any]:
    """Профиль по номеру. Есть — возвращаем, нет — заводим."""
    existing = find_user_by_phone(state, phone)
    if existing is not None:
        return existing
    user = {
        "id": next_id("users"),
        "name": "", "login": None, "role": "customer",
        "phone": phone, "email": None,
        "password_hash": None,
        "telegram_chat_id": None,
        "is_active": True,
        "created_at": iso_now(now or datetime.now(timezone.utc)),
    }
    state.setdefault("users", []).append(user)
    return user


def link_guest_orders(state: dict[str, Any], user: Mapping[str, Any]) -> int:
    """Гостевые заказы с этим номером привязываются к профилю (ТЗ 9.1).

    Без этого человек, оформивший заказ гостем и вошедший потом,
    не увидел бы собственный заказ в личном кабинете.
    """
    n = 0
    for o in state.get("orders") or []:
        if o.get("user_id") is None and o.get("phone") == user.get("phone"):
            o["user_id"] = user.get("id")
            o["is_guest"] = False
            n += 1
    for p in state.get("preorders") or []:
        if p.get("user_id") is None and p.get("phone") == user.get("phone"):
            p["user_id"] = user.get("id")
            n += 1
    return n


# ---------------------------------------------------------------------------
# Вход персонала
# ---------------------------------------------------------------------------
# Хеш-пустышка, чтобы проверка несуществующего логина занимала столько
# же времени, сколько проверка существующего. Считается один раз при
# импорте: scrypt намеренно медленный, и делать это на каждый запрос
# значило бы подарить способ нагрузить сервер.
_DUMMY_HASH = sec.hash_password("dummy-password-for-timing-equalization-1A")

STAFF_ROLES = ("admin", "manager")


def verify_staff(state: Mapping[str, Any], login: Any,
                 password: Any) -> dict[str, Any] | None:
    """Сотрудник или None. Причина отказа наружу не различается.

    «Нет такого логина» и «неверный пароль» обязаны выглядеть
    одинаково — иначе перебором составляется список логинов персонала.
    Поэтому хеш считается и тогда, когда пользователя нет.
    """
    found = None
    for u in state.get("users", []):
        if u.get("login") == login and u.get("role") in STAFF_ROLES:
            found = u
            break

    if not found or not found.get("is_active"):
        sec.verify_password(password, _DUMMY_HASH)   # выравниваем время ответа
        return None
    if not sec.verify_password(password, found.get("password_hash")):
        return None
    return found


# ---------------------------------------------------------------------------
# Права (ТЗ 10.8)
# ---------------------------------------------------------------------------
# Единственный источник правды о том, что разрешено роли. Скрытие
# пункта меню на клиенте защитой не является — проверка делается здесь
# и на каждом маршруте.
#
# Порядок здесь — не оформление: `/api/admin/login` и `/api/admin/me`
# отдают этот список наружу как есть, и по нему админка рисует меню.
# Менять порядок — менять вид панели.
#
# Раньше `permissions_of()` сортировал список алфавитно, и это сходило
# с рук: проверка, которая должна была ловить расхождение, сама
# сортировала обе стороны перед сравнением — то есть проверяла состав,
# но никогда упорядоченность. Дефект нашёлся, только когда стали
# сравнивать живое тело ответа целиком. Регресс, уже без сортировки, —
# `test_sessions_and_permissions.py`.
_PERMISSIONS_ORDER: dict[str, tuple[str, ...]] = {
    "admin": ("dashboard", "products", "categories", "orders", "preorders",
             "promos", "delivery", "staff", "home", "import", "export", "payments"),
    # Менеджер: заказы и предзаказы, без денег и настроек. `payments`
    # вынесено из `orders` намеренно: вести заказ по статусам — работа
    # менеджера, а помечать деньги полученными или возвращёнными — нет.
    "manager": ("orders", "preorders", "export"),
}

PERMISSIONS: dict[str, frozenset[str]] = {
    role: frozenset(perms) for role, perms in _PERMISSIONS_ORDER.items()
}


def can(role: Any, permission: Any) -> bool:
    return permission in PERMISSIONS.get(role, frozenset())


def permissions_of(role: Any) -> list[str]:
    """Список прав роли — его отдаёт админка, чтобы рисовать меню.

    Порядок — из `_PERMISSIONS_ORDER`, а не алфавитный: по нему
    рисуется меню панели, см. комментарий там.
    """
    return list(_PERMISSIONS_ORDER.get(role, ()))
