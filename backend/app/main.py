"""Точка входа FastAPI.

Запуск для разработки, из корня проекта:

    uvicorn backend.app.main:app --reload --port 8000

Порядок подключения роутеров в `create_app()` несущий, а не
косметический: `static` идёт последним, потому что у него маршрут-
ловушка `/{path:path}`. Поставь его выше — и он перехватит и API, и
SSR-страницы.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

from .api.routers import admin, cart, pages, shop, static
from .config import get_settings
from .domain.validate import ValidationError as DomainValidationError

# Политика подобрана под витрину: `script-src 'self'` без
# `'unsafe-inline'` работает только потому, что в разметке нет ни одного
# обработчика в атрибуте (инвариант 13). Ослабишь здесь — перестанет
# ловиться целый класс XSS; добавишь `onclick` в разметку — страница
# молча перестанет работать в браузере.
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",       # инлайн-стили в карточках
    "img-src 'self' data: https://commons.wikimedia.org https://upload.wikimedia.org",
    "connect-src 'self'",
    "font-src 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "object-src 'none'",
])

SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}

# Страница /docs — единственное исключение из строгой политики.
#
# Swagger UI подтягивает свои стили и скрипты с cdn.jsdelivr.net и
# запускает встроенный скрипт инициализации. Под общей политикой
# браузер это блокирует, и страница открывается пустой — что и
# произошло. Вариантов было три:
#
#   1) ослабить политику для всего приложения — нет, тогда защита
#      теряется там, где она и нужна, ради страницы для разработчика;
#   2) положить файлы Swagger UI рядом с собой и раздавать со своего
#      домена — правильно, но это полтора мегабайта в репозитории и
#      ручное обновление;
#   3) послабление ровно для двух адресов документации.
#
# Выбран третий. Он безопасен, потому что в бою документация вообще
# не поднимается (`openapi_url=None`), и до продакшена это исключение
# не доезжает. Если документация понадобится на боевом сервере —
# нужен вариант 2, а не расширение этого списка.
DOCS_PATHS = frozenset({"/docs", "/redoc"})
DOCS_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
    "img-src 'self' data: https://fastapi.tiangolo.com",
    "font-src 'self' data: https://cdn.jsdelivr.net",
    "connect-src 'self'",
    "worker-src 'self' blob:",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "object-src 'none'",
])


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="FructCity API",
        version="0.1.0",
        # Схему прячем в бою: список всех маршрутов с формами данных —
        # подсказка для того, кто ищет, что тут можно потрогать.
        openapi_url=None if settings.is_production else "/openapi.json",
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        # Послабление только для самой страницы документации. Данные,
        # которые она показывает (/openapi.json), отдаются под общей
        # политикой — послабление на них не распространяется.
        if request.url.path in DOCS_PATHS and not settings.is_production:
            response.headers["Content-Security-Policy"] = DOCS_CSP
        if settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    # Форма ошибки в этом API — {"error": "код"}. FastAPI по умолчанию
    # отдаёт {"detail": …}, и витрина такую ошибку не разберёт: она
    # читает поле error и показывает по нему понятный текст. Переопределяем
    # оба обработчика, иначе разойдёмся ровно там, где пользователю и так
    # что-то не удалось.
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        code = exc.detail if isinstance(exc.detail, str) else "error"
        if exc.status_code == 404 and code in ("Not Found", "error"):
            code = "not_found"
        # К коду отказа иногда прилагаются подробности: сколько секунд
        # ждать до повтора, какого права не хватило. Витрина их
        # показывает, поэтому теряться они не должны — но наружу
        # уходит только то, что положил сам обработчик.
        body: dict[str, object] = {"error": code}
        body.update(getattr(exc, "extra", None) or {})
        return JSONResponse(body, status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))

    @app.exception_handler(DomainValidationError)
    async def domain_validation_error(request: Request, exc: DomainValidationError):
        """Ошибка схемы — 422 с картой полей.

        Витрина подсвечивает по этой карте конкретные поля формы.
        Отдать один общий код значило бы заставить человека искать,
        что именно он ввёл не так.
        """
        return JSONResponse({"error": "validation_failed", "fields": exc.fields},
                            status_code=422)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # 400 с кодом, а не портянка с описанием полей: подробности
        # схемы наружу не выносим — по ним удобно подбирать запросы.
        return JSONResponse({"error": "validation_failed"}, status_code=400)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> JSONResponse:
        """Живость плюс доступность хранилища.

        Проверять только «процесс отвечает» бесполезно: приложение
        может стоять с нечитаемым store.json и бодро отдавать 200.

        Источник данных возвращается тоже: во время переезда важнее
        всего знать, откуда именно сейчас отвечает витрина.
        """
        from .db.source import current_source, read_state, snapshot_status
        from .db.store import StoreUnavailable

        try:
            state = read_state()
        except StoreUnavailable as e:
            return JSONResponse({"ok": False, "store": str(e)}, status_code=503)

        snapshot = snapshot_status()
        return JSONResponse({
            "ok": True,
            "source": current_source(),
            "products": len(state.get("products", [])),
            # Во время переезда важнее всего понимать, откуда сейчас
            # отвечает витрина и не отстала ли база от файла.
            "db_snapshot": snapshot,
            # Видно снаружи, не заглядывая в .env: включён ли режим
            # «база — источник истины».
            "write_enabled": get_settings().write_enabled,
        })

    app.include_router(shop.router)
    app.include_router(cart.router)
    app.include_router(admin.router)
    app.include_router(pages.router)
    # Строго последним: у него маршрут-ловушка `/{path:path}`, который
    # иначе перехватил бы и API, и SSR-страницы (см. docstring роутера).
    app.include_router(static.router)
    return app


def ensure_schema() -> None:
    """Создаёт базу и недостающие таблицы, если база настроена.

    Ошибку подключения не проглатываем молча, но и приложение из-за
    неё не роняем: пока данные лежат в JSON, недоступный Postgres —
    не повод не отдавать каталог.
    """
    settings = get_settings()
    if not settings.db_configured:
        return
    try:
        from .db import models

        report = models.provision()
        created = models.create_all()
    except Exception as e:  # noqa: BLE001 — причин много, реакция одна
        print(f"[FructCity] база недоступна, работаю на JSON: {e}", flush=True)
        for line in _db_hint(e):
            print(f"[FructCity] {line}", flush=True)
        return

    if report.get("role_created"):
        print(f"[FructCity] создана роль {report['app_user']}", flush=True)
    if report.get("database_created"):
        print(f"[FructCity] создана база {report['database']}", flush=True)
    if created:
        print(f"[FructCity] созданы таблицы: {', '.join(created)}", flush=True)


def _db_hint(error: Exception) -> list[str]:
    """Разбор причины отказа. Без него в журнале остаётся только след
    исключения, по которому непонятно, что чинить."""
    try:
        from .db.models import diagnose

        return diagnose(get_settings().database_url, error)
    except Exception:  # noqa: BLE001 — подсказка не должна ронять запуск
        return []


# --- Создание базы при первом запуске ------------------------------------
#
# ЗАКОММЕНТИРУЙТЕ строку ниже после первого успешного запуска.
#
# Забыть её не страшно: вызов идемпотентен, создаёт только недостающее
# и никогда не трогает данные. Но на каждом старте он опрашивает схему,
# а приложению это ни к чему.
#
# Если база не настроена (нет FC_DB_PASSWORD и нет DATABASE_URL), шаг
# молча пропускается: сейчас питоновская версия читает JSON, и база ей
# ещё не нужна.
#
# Вызов стоит здесь, а не внутри create_app(), намеренно. Внутри он
# срабатывал бы и в тестах — а тест не должен по дороге создавать
# роль и базу на машине разработчика.
ensure_schema()

app = create_app()
