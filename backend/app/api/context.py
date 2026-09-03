"""Контекст запроса: сессия, CSRF, лимиты, транзакция.

Всё, что нужно сделать до вызова маршрута — разобрать сессию, проверить
CSRF и лимиты, при необходимости открыть транзакцию, — собрано в один
объект, который роутеры получают зависимостью.

Три вещи, которые легко потерять при переносе и дорого чинить потом:

**Сессия перевыпускается при входе.** Идентификатор, который был у
гостя, не должен становиться идентификатором вошедшего: иначе заранее
навязанная жертве сессия после входа даёт доступ к аккаунту.

**Cookie сессии — httpOnly.** Скрипту она недоступна, поэтому XSS не
превращается в кражу сессии. CSRF-токен, наоборот, читается скриптом —
он и обязан уехать в заголовке.

**Изменяющий запрос идёт под транзакцией.** Проверка остатка и его
списание не могут быть разорваны другим запросом.

**Запись включается флагом.** `FC_WRITE_ENABLED` (`Settings.write_enabled`)
по умолчанию выключен, и тогда изменяющие маршруты отвечают 503
`write_disabled`. В бою он включён (переезд 1.7 выполнен). Флаг остался
не для красоты: он же переводит приложение в режим «только чтение» —
например, на время работ с базой, — и отличает «писать запрещено» от
«писать некуда» (`store_unavailable`, база настроена, но недоступна).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator

from fastapi import HTTPException, Request, Response

from ..config import get_settings
from ..db.source import current_source, read_state
from ..db.store import StoreUnavailable
from ..domain import auth as A
from ..domain import security as sec

__all__ = ["Ctx", "get_ctx", "Fail", "LIMITER"]

# Счётчики частоты живут в процессе. При нескольких воркерах они
# переедут в общее хранилище, но интерфейс останется прежним.
LIMITER = sec.RateLimiter()

SESSION_COOKIE_MAX_AGE = A.SESSION_TTL_MS // 1000
CSRF_COOKIE_MAX_AGE = 30 * 24 * 3600

# last_seen обновляем не чаще раза в пять минут: иначе каждый переход
# по каталогу превращался бы в запись в базу.
LAST_SEEN_INTERVAL = timedelta(minutes=5)


class Fail(HTTPException):
    """Отказ с кодом в теле: {"error": "код", ...}.

    Наружу уходит именно `error` — витрина читает это поле и по нему
    показывает человеку понятный текст. `detail` от FastAPI она бы не
    разобрала.
    """

    def __init__(self, status: int, code: str, **extra: Any):
        super().__init__(status_code=status, detail=code)
        self.extra = extra


def _dev() -> bool:
    return not get_settings().is_production


def _expose_otp() -> bool:
    """Код из SMS в ответе API — только по явному флагу.

    Раньше это было привязано к «не продакшен», то есть любой запуск без
    выставленной переменной отдавал код в ответе — а значит позволял
    войти в чужой профиль, зная один номер телефона.
    """
    return os.environ.get("FC_DEV_OTP") == "1" or os.environ.get("NODE_ENV") == "test"


def _client_ip(request: Request) -> str:
    """Адрес клиента. За прокси — только при явном разрешении.

    Доверять `X-Forwarded-For` без прокси нельзя: заголовок ставит сам
    клиент, и лимиты обходились бы одной строкой в запросе.
    """
    if os.environ.get("FC_TRUST_PROXY") == "1":
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


def _allowed_origins(request: Request) -> set[str]:
    """Origin, с которых принимаем изменяющие запросы.

    Набор строится под каждый запрос: собственный адрес плюс явно
    настроенные. Фиксировать список на старте нельзя — сервер может
    слушать другой порт (за прокси, в тестах), и законные запросы
    начали бы отклоняться.
    """
    configured = {o.strip().rstrip("/") for o in
                  (os.environ.get("FC_ORIGIN") or "").split(",") if o.strip()}
    host = request.headers.get("host")
    if host:
        proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip() \
            or request.url.scheme
        configured.add(f"{proto}://{host}")
    return configured


class Ctx:
    """Всё, что маршруту нужно знать о запросе."""

    def __init__(self, request: Request, response: Response):
        self.request = request
        self.response = response
        self.ip = _client_ip(request)
        self.cookies = request.cookies
        self.expose_otp = _expose_otp()
        self.dev = _dev()

        self.state = self._read()
        self.session = self._resolve_session()
        self.user = self._resolve_user()
        self.csrf_token = self._ensure_csrf()

    # -- данные -------------------------------------------------------------
    def _read(self) -> dict[str, Any]:
        try:
            return read_state()
        except StoreUnavailable as e:
            # 503, а не 500: сервер жив, недоступны данные — клиенту
            # есть смысл повторить, а мониторингу видно, что чинить.
            raise Fail(503, "store_unavailable") from e

    def _resolve_session(self) -> dict[str, Any]:
        """Живая сессия из cookie либо гостевая «на лету».

        Гостевая сессия здесь **не сохраняется**: запись идёт только
        внутри транзакции (инвариант 2), а безопасный GET транзакцию не
        открывает. Сессия появляется в базе при первом изменяющем
        запросе — из `live_session()` внутри `tx()`. Поэтому cookie
        `fc_sid` гость получает не на любом запросе, а на первом, что
        реально меняет состояние.
        """
        sid = self.cookies.get(sec.SESSION_COOKIE)
        found = A.find_session(self.state, sid) if sid else None
        if found is not None:
            return found
        return A.new_session(next_id=lambda _k: 0, ip=self.ip)

    def _resolve_user(self) -> dict[str, Any] | None:
        user_id = self.session.get("user_id")
        if not user_id:
            return None
        return next((u for u in self.state.get("users") or []
                     if u.get("id") == user_id), None)

    # -- CSRF ---------------------------------------------------------------
    def _ensure_csrf(self) -> str:
        token = self.cookies.get(sec.CSRF_COOKIE)
        if token and len(token) >= 20:
            return token
        token = sec.token(24)
        # httponly=False намеренно: этот токен обязан читаться скриптом
        # витрины, чтобы уехать в заголовке. Секретность ему не нужна —
        # нужна недоступность чужому домену, и её даёт сам браузер.
        self.response.set_cookie(
            sec.CSRF_COOKIE, token, max_age=CSRF_COOKIE_MAX_AGE,
            httponly=False, samesite="lax", secure=not self.dev, path="/")
        return token

    def check_csrf(self) -> None:
        result = sec.check_csrf(self.request.method, self.request.headers,
                                self.cookies, _allowed_origins(self.request))
        if not result["ok"]:
            raise Fail(403, result["reason"])

    # -- cookie сессии ------------------------------------------------------
    def set_session_cookie(self, sid: str) -> None:
        self.response.set_cookie(
            sec.SESSION_COOKIE, sid, max_age=SESSION_COOKIE_MAX_AGE,
            httponly=True, samesite="lax", secure=not self.dev, path="/")

    def clear_session_cookie(self) -> None:
        self.response.delete_cookie(
            sec.SESSION_COOKIE, path="/", httponly=True,
            samesite="lax", secure=not self.dev)

    # -- лимиты -------------------------------------------------------------
    def rate_limit(self, name: str, cfg: dict | None = None) -> dict[str, Any]:
        conf = cfg or sec.LIMITS[name]
        return LIMITER.check(f"{name}:{self.ip}", conf["limit"], conf["windowMs"])

    def rate_limit_key(self, key: str, cfg: dict) -> dict[str, Any]:
        return LIMITER.check(key, cfg["limit"], cfg["windowMs"])

    def rate_limit_reset(self, key: str) -> None:
        LIMITER.reset(key)
        LIMITER.reset(f"{key}:{self.ip}")

    def require_rate(self, name: str, cfg: dict | None = None,
                     code: str = "rate_limited") -> dict[str, Any]:
        result = self.rate_limit(name, cfg)
        if not result["allowed"]:
            raise Fail(429, code, retry_after=result["retryAfter"])
        return result

    # -- права --------------------------------------------------------------
    def require_staff(self, permission: str | None = None) -> dict[str, Any]:
        """Проверка на сервере, на каждом маршруте (ТЗ 10.8).

        Скрытие пункта меню на клиенте защитой не является: адрес
        маршрута видно в исходниках витрины.
        """
        user = self.user
        if not user or user.get("role") not in A.STAFF_ROLES:
            raise Fail(401, "unauthorized")
        if permission and not A.can(user.get("role"), permission):
            raise Fail(403, "forbidden", need=permission)
        return user

    def require_user(self) -> dict[str, Any]:
        if not self.user:
            raise Fail(401, "unauthorized")
        return self.user

    # -- запись -------------------------------------------------------------
    @contextmanager
    def tx(self, csrf: bool = True) -> Iterator[Any]:
        """Транзакция с живой сессией внутри неё.

        Изменяющий запрос обязан проходить проверку CSRF — она стоит
        здесь, а не в каждом маршруте: маршрут, где её забыли добавить,
        внешне работает правильно, и пропажу замечают не сразу.

        `csrf=False` — исключение ровно для вебхука с общим секретом
        (`/api/telegram/confirm`). Он приходит извне, cookie у него нет,
        и токену взяться неоткуда; вместо этого его подлинность
        доказывает секрет. Добавляя сюда новый маршрут, убедись, что он
        не опирается на cookie — иначе снимаешь защиту, а не обходишь
        неудобство.
        """
        if csrf:
            self.check_csrf()
        if not get_settings().write_enabled:
            # Режим «только чтение»: флаг снят намеренно (работы с
            # базой, разбор инцидента). Код отличается от
            # `store_unavailable` ниже — там писать разрешено, но
            # некуда, и это чинится совсем другим действием.
            raise Fail(503, "write_disabled")
        if current_source() != "postgres":
            # Флаг включён, а базы фактически нет (не настроена,
            # недоступна, пуста) — писать некуда. Отдать это отдельным
            # кодом, а не молча откатиться на JSON: в этом режиме файл
            # уже не тот источник, которому можно доверять запись.
            raise Fail(503, "store_unavailable")

        from ..db import source
        from ..db.uow import transaction

        with transaction(source.engine()) as unit:
            unit.ctx_session = self.live_session(unit)
            yield unit
        # После записи снимок в кэше устарел.
        self._invalidate()

    def live_session(self, unit: Any) -> dict[str, Any]:
        """Ссылка на сессию **внутри** транзакции.

        Возвращать объект, прочитанный до транзакции, нельзя: запись в
        него не попала бы в базу, а клиент получил бы успешный ответ на
        несохранённое действие. Если сессию успели вычистить — заводим
        новую здесь же и ставим cookie.
        """
        sid = self.session.get("sid")
        live = next((s for s in unit.state.get("sessions") or []
                     if s.get("sid") == sid), None)
        if live is not None:
            self._touch(live)
            return live

        revived = A.create_session(
            unit.state, next_id=unit.next_id,
            user_id=self.session.get("user_id"), role=self.session.get("role", "guest"),
            cart=list(self.session.get("cart") or []), ip=self.ip)
        revived["promo_code"] = self.session.get("promo_code") or None
        revived["recent_orders"] = list(self.session.get("recent_orders") or [])
        revived["recent_preorders"] = list(self.session.get("recent_preorders") or [])
        self.set_session_cookie(revived["sid"])
        self.session = revived
        return revived

    def _touch(self, session: dict[str, Any]) -> None:
        last = A.parse_iso(session.get("last_seen"))
        now = datetime.now(timezone.utc)
        if last is None or now - last > LAST_SEEN_INTERVAL:
            session["last_seen"] = A.iso_now(now)

    @staticmethod
    def _invalidate() -> None:
        from ..db import source

        source.invalidate()


def get_ctx(request: Request, response: Response) -> Ctx:
    """Зависимость FastAPI. Один контекст на запрос."""
    return Ctx(request, response)
