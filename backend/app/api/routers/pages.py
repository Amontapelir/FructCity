"""SSR-страницы и SEO: мета-теги, JSON-LD, sitemap, правовые страницы.

Тонкая обёртка вокруг `domain/pages.py`: читает файлы (`public/index.html`,
`public/admin.html`), определяет базовый URL запроса и вызывает чистые
функции сборки мета-тегов. Сама разметка не собирается здесь — только
подстановка уже готового HTML-фрагмента в оболочку.

Остальную статику (`app.js`, картинки, стили) отдаёт
`routers/static.py`. Он подключается последним и ловит всё, что не
разобрали API и эти страницы, — поэтому маршруты здесь объявлены явно,
а не собраны в одну ловушку.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Request, Response

from ...config import ROOT, get_settings
from ...domain import pages as P
from .shop import get_state

router = APIRouter(tags=["pages"])

PUBLIC_DIR = ROOT / "public"
_shell_cache: str | None = None


def _shell() -> str:
    """HTML-оболочка витрины. Кэшируется только в проде: правка
    `index.html` в разработке видна без перезапуска сервера."""
    global _shell_cache
    if _shell_cache is not None and get_settings().is_production:
        return _shell_cache
    _shell_cache = (PUBLIC_DIR / "index.html").read_text(encoding="utf-8")
    return _shell_cache


def _base_url(request: Request) -> str:
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip() \
        or request.url.scheme
    host = request.headers.get("host") or "localhost"
    return f"{proto}://{host}"


def _html(body: str, status: int = 200, *, cache: str | None = None,
         extra: dict[str, str] | None = None) -> Response:
    headers = dict(extra or {})
    if cache:
        headers["Cache-Control"] = cache
    return Response(content=body, status_code=status,
                    media_type="text/html; charset=utf-8", headers=headers)


# ---------------------------------------------------------------------------
# Служебные файлы
# ---------------------------------------------------------------------------
@router.get("/photos.json")
def photos_json() -> Response:
    import json

    from ...domain.photos import PHOTOS

    return Response(content=json.dumps(PHOTOS, ensure_ascii=False),
                    media_type="application/json; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=3600"})


@router.get("/robots.txt")
def robots_txt(request: Request) -> Response:
    return Response(content=P.robots_txt(_base_url(request)),
                    media_type="text/plain; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=3600"})


@router.get("/sitemap.xml")
def sitemap_xml(request: Request, state: dict = Depends(get_state)) -> Response:
    return Response(content=P.sitemap_xml(_base_url(request), state),
                    media_type="application/xml; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=3600"})


# ---------------------------------------------------------------------------
# Карточка товара (ТЗ 15.3 — Schema.org Product/Offer)
# ---------------------------------------------------------------------------
@router.get("/product/{slug}")
def product_page(slug: str, request: Request, state: dict = Depends(get_state)) -> Response:
    settings = state.get("settings") or {}
    shop_name = settings.get("shop_name") or "FructCity"
    base = _base_url(request)

    meta = P.product_meta(state, base, shop_name, unquote(slug))
    if meta is None:
        # ТЗ 15.3 — 404 отдаём именно для несуществующих, а не для «нет в наличии»
        return _html(P.with_meta(_shell(), P.not_found_meta(base, shop_name, "product")), 404,
                    cache="no-cache")
    return _html(P.with_meta(_shell(), meta), cache="no-cache")


# ---------------------------------------------------------------------------
# Каталог и категория
# ---------------------------------------------------------------------------
@router.get("/catalog")
def catalog_page(request: Request, state: dict = Depends(get_state)) -> Response:
    return _catalog(request, state, None)


@router.get("/catalog/{cat_id}")
def catalog_category_page(cat_id: str, request: Request, state: dict = Depends(get_state)) -> Response:
    return _catalog(request, state, unquote(cat_id))


def _catalog(request: Request, state: dict[str, Any], cat_id: str | None) -> Response:
    settings = state.get("settings") or {}
    shop_name = settings.get("shop_name") or "FructCity"
    base = _base_url(request)

    meta = P.catalog_meta(state, base, shop_name, cat_id)
    if meta is None:
        return _html(P.with_meta(_shell(), P.not_found_meta(base, shop_name, "category")), 404,
                    cache="no-cache")
    return _html(P.with_meta(_shell(), meta), cache="no-cache")


# ---------------------------------------------------------------------------
# Правовые страницы (ТЗ 14.3)
# ---------------------------------------------------------------------------
@router.get("/policy")
def policy_page(request: Request, state: dict = Depends(get_state)) -> Response:
    return _legal(request, state, "/policy")


@router.get("/offer")
def offer_page(request: Request, state: dict = Depends(get_state)) -> Response:
    return _legal(request, state, "/offer")


def _legal(request: Request, state: dict[str, Any], pathname: str) -> Response:
    settings = state.get("settings") or {}
    shop_name = settings.get("shop_name") or "FructCity"
    meta = P.legal_meta(_base_url(request), shop_name, pathname, settings)
    return _html(P.with_meta(_shell(), meta), cache="no-cache")


# ---------------------------------------------------------------------------
# Админка — статическая раздача одного файла, без SSR-подстановок
# ---------------------------------------------------------------------------
@router.get("/admin")
@router.get("/admin/")
def admin_page() -> Response:
    body = (PUBLIC_DIR / "admin.html").read_text(encoding="utf-8")
    return _html(body, cache=None,
                extra={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"})


# ---------------------------------------------------------------------------
# Главная и клиентские SPA-маршруты
# ---------------------------------------------------------------------------
def _spa(pathname: str, request: Request, state: dict[str, Any]) -> Response:
    settings = state.get("settings") or {}
    shop_name = settings.get("shop_name") or "FructCity"
    meta = P.spa_meta(_base_url(request), shop_name, pathname, settings)
    return _html(P.with_meta(_shell(), meta), cache="no-cache")


@router.get("/")
def home_page(request: Request, state: dict = Depends(get_state)) -> Response:
    return _spa("/", request, state)


@router.get("/cart")
def cart_page(request: Request, state: dict = Depends(get_state)) -> Response:
    return _spa("/cart", request, state)


@router.get("/checkout")
def checkout_page(request: Request, state: dict = Depends(get_state)) -> Response:
    return _spa("/checkout", request, state)


@router.get("/profile")
def profile_page(request: Request, state: dict = Depends(get_state)) -> Response:
    return _spa("/profile", request, state)


@router.get("/login")
def login_page(request: Request, state: dict = Depends(get_state)) -> Response:
    return _spa("/login", request, state)


@router.get("/preorder")
def preorder_page(request: Request, state: dict = Depends(get_state)) -> Response:
    return _spa("/preorder", request, state)
