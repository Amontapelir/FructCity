"""SEO и SSR-страницы: мета-теги, JSON-LD, sitemap, robots.

Витрина — SPA, но карточки товаров и категории обязаны быть
индексируемыми. Поэтому HTML-оболочка отдаётся с уже подставленными
title/description/canonical и микроразметкой Schema.org: поисковый
робот видит содержимое без выполнения JS.

Здесь — то, что можно проверить без файловой системы и HTTP: сборка
мета-тегов, JSON-LD, robots.txt и sitemap.xml, а также подбор товаров
и категорий из состояния. Чтение `public/index.html` и определение
base URL запроса — забота роутера (`api/routers/pages.py`), они не
чистые функции и сверке не подлежат.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping
from urllib.parse import quote

from . import calc as C
from .photos import photo_url
from .security import escape_html


def _breadcrumb_list(items: list[tuple[str, str]]) -> dict[str, Any]:
    """Schema.org BreadcrumbList (ROADMAP: расширенная микроразметка).

    `items` — путь от корня, `(название, url)`. Отдельный JSON-LD блок
    от основного (`Product`/`ItemList`), а не один документ с обоими:
    так `product_meta`/`catalog_meta` не переписывают уже сверенный
    порядок ключей своего узла (`test_pages_meta.py`,
    `test_product_meta_json_ld_key_order`) — `with_meta` просто
    добавляет второй `<script>`, когда он есть.
    """
    return {
        "@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": name, "item": url}
            for i, (name, url) in enumerate(items)
        ],
    }


def _uri(s: Any) -> str:
    """`encodeURIComponent` из JS: `quote()` по умолчанию экранирует
    больше символов (`!*'()` не входят в его "always safe"), а часть
    slug'ов и файлов на Wikimedia их содержит буквально."""
    return quote(str(s), safe="!~*'()")


__all__ = [
    "with_meta", "json_ld_safe", "robots_txt", "sitemap_xml",
    "product_meta", "catalog_meta", "not_found_meta",
    "legal_meta", "legal_noscript", "spa_meta",
    "SPA_ROUTES",
]

SPA_ROUTES = ("/", "/cart", "/checkout", "/profile", "/login", "/preorder")


# ---------------------------------------------------------------------------
# Подстановка в оболочку
# ---------------------------------------------------------------------------
def json_ld_safe(obj: Any) -> str:
    """JSON-LD внутри <script> — закрываем возможность выйти из тега."""
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def with_meta(shell_html: str, meta: Mapping[str, Any]) -> str:
    """Подстановка мета-данных в HTML-оболочку.

    Заменитель — функция, а не строка: в строковом заменителе
    последовательности `\\g<0>` и подобные интерпретировались бы как
    спецсимволы регэкспа и подставляли в страницу куски исходной
    разметки, если название товара их случайно содержит.
    """
    e = escape_html
    parts = [
        f'<title>{e(meta.get("title"))}</title>',
        f'<meta name="description" content="{e(meta.get("description"))}">',
        f'<link rel="canonical" href="{e(meta.get("canonical"))}">',
        f'<meta property="og:type" content="{e(meta.get("og_type") or "website")}">',
        f'<meta property="og:title" content="{e(meta.get("title"))}">',
        f'<meta property="og:description" content="{e(meta.get("description"))}">',
        f'<meta property="og:url" content="{e(meta.get("canonical"))}">',
        f'<meta property="og:image" content="{e(meta["image"])}">' if meta.get("image") else "",
        '<meta name="robots" content="noindex, nofollow">' if meta.get("noindex") else "",
        (f'<script type="application/ld+json">{json_ld_safe(meta["json_ld"])}</script>'
         if meta.get("json_ld") else ""),
        (f'<script type="application/ld+json">{json_ld_safe(meta["breadcrumbs"])}</script>'
         if meta.get("breadcrumbs") else ""),
        (f'<script type="application/json" id="preload">{json_ld_safe(meta["preload"])}</script>'
         if meta.get("preload") else ""),
    ]
    head = "\n  ".join(p for p in parts if p)

    # `str.replace` в Python, в отличие от `String.replace` в JS, не
    # разбирает спецпоследовательности вроде `$&` в замене. Поэтому
    # название товара с таким сочетанием подставляется как есть, и
    # обходных приёмов здесь не нужно.
    out = shell_html.replace("<!--SEO-->", head, 1)
    out = out.replace("<!--NOSCRIPT-->", meta.get("noscript") or "", 1)
    return out


# ---------------------------------------------------------------------------
# robots.txt / sitemap.xml (ТЗ 15.3)
# ---------------------------------------------------------------------------
def robots_txt(base: str) -> str:
    return "\n".join([
        "User-agent: *",
        "Allow: /",
        "Disallow: /admin",
        "Disallow: /api/",
        "Disallow: /cart",
        "Disallow: /checkout",
        "Disallow: /profile",
        # /login и /preorder тоже noindex (spa_meta) — тот же приём, что
        # и у трёх маршрутов выше, для единообразия (issue #17: раньше
        # были noindex только через мета-тег, без Disallow здесь).
        "Disallow: /login",
        "Disallow: /preorder",
        "",
        f"Sitemap: {base}/sitemap.xml",
    ])


def sitemap_xml(base: str, state: Mapping[str, Any]) -> str:
    urls: list[dict[str, str]] = [
        {"loc": f"{base}/", "priority": "1.0", "changefreq": "daily"},
        {"loc": f"{base}/catalog", "priority": "0.9", "changefreq": "daily"},
        {"loc": f"{base}/policy", "priority": "0.3", "changefreq": "yearly"},
        {"loc": f"{base}/offer", "priority": "0.3", "changefreq": "yearly"},
    ]
    for c in state.get("categories") or []:
        if c.get("is_active") is False:
            continue
        urls.append({"loc": f"{base}/catalog/{_uri(c.get('id'))}",
                     "priority": "0.8", "changefreq": "daily"})
    # ТЗ 15.3 — карточки с нулевым остатком остаются в карте сайта
    for p in state.get("products") or []:
        if p.get("is_active") is False:
            continue
        urls.append({
            "loc": f"{base}/product/{_uri(p.get('slug'))}",
            "priority": "0.7", "changefreq": "weekly",
            "lastmod": str(p.get("updated_at") or p.get("created_at") or "")[:10],
        })

    body = "\n".join(
        "  <url>\n"
        f"    <loc>{escape_html(u['loc'])}</loc>\n"
        + (f"    <lastmod>{u['lastmod']}</lastmod>\n" if u.get("lastmod") else "")
        + f"    <changefreq>{u['changefreq']}</changefreq>\n"
        + f"    <priority>{u['priority']}</priority>\n"
        "  </url>"
        for u in urls
    )
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"{body}\n</urlset>")


# ---------------------------------------------------------------------------
# Карточка товара (Schema.org Product/Offer)
# ---------------------------------------------------------------------------
def product_meta(state: Mapping[str, Any], base: str, shop_name: str, slug: str,
                 now: datetime | None = None) -> dict[str, Any] | None:
    """Мета для карточки товара, или `None`, если товара нет (→ 404)."""
    p = next((x for x in state.get("products") or []
             if x.get("is_active") is not False and (x.get("slug") == slug or x.get("sku") == slug)),
             None)
    if p is None:
        return None

    price = C.unit_price(p, now)
    img = photo_url(p.get("image_key"), 800)
    unit = "шт" if p.get("type") == "unit" else "кг"
    canonical = f"{base}/product/{_uri(p.get('slug'))}"

    offers: dict[str, Any] = {
        "@type": "Offer", "url": canonical, "priceCurrency": "RUB",
        # String(price) из JS: у price там нет отдельного целого типа
        "price": str(C.js_number(price)),
        # ТЗ 3.2 — нулевой остаток не прячем, помечаем статусом
        "availability": ("https://schema.org/InStock" if C.in_stock(p)
                         else "https://schema.org/OutOfStock"),
        "itemCondition": "https://schema.org/NewCondition",
        "seller": {"@type": "Organization", "name": shop_name},
    }
    if p.get("sale_until"):
        offers["priceValidUntil"] = p["sale_until"]
    if p.get("type") == "weighted":
        offers["eligibleQuantity"] = {"@type": "QuantitativeValue", "unitCode": "KGM",
                                      "minValue": p.get("min_weight")}

    # Порядок ключей значим: `json.dumps` печатает их в порядке
    # вставки, и этот текст уходит в страницу, которую читает поисковик.
    # `image` и `category` встают МЕЖДУ `sku` и `offers`, а при их
    # отсутствии ключ просто не добавляется — порядок остальных от
    # этого не сдвигается. Поэтому словарь собирается по шагам, а не
    # задаётся целиком: расхождение нашлось сверкой текста страницы,
    # регресс — `test_pages_meta.py`.
    json_ld: dict[str, Any] = {
        "@context": "https://schema.org", "@type": "Product",
        "name": p.get("name"),
        "description": p.get("description") or f"{p.get('name')} — {shop_name}",
        "sku": p.get("sku"),
    }
    if img:
        json_ld["image"] = [img]
    cat = next((c for c in state.get("categories") or [] if c.get("id") == p.get("category_id")), None)
    if cat:
        json_ld["category"] = cat.get("name")
    json_ld["offers"] = offers

    e = escape_html
    noscript = (
        f"<article><h1>{e(p.get('name'))}</h1>"
        f"<p>{e(p.get('description') or '')}</p>"
        f"<p><strong>{C.js_number(price)} ₽</strong> за {unit} · "
        f"{'в наличии' if C.in_stock(p) else 'нет в наличии'} · артикул {e(p.get('sku'))}</p>"
        '<p><a href="/catalog">Весь каталог</a></p></article>'
    )
    description = (p.get("description")
                   or f"{p.get('name')} с доставкой по Южному Бутово и Коммунарке за 2 часа.")

    breadcrumbs = [("Главная", f"{base}/"), ("Каталог", f"{base}/catalog")]
    if cat:
        breadcrumbs.append((cat.get("name"), f"{base}/catalog/{_uri(cat.get('id'))}"))
    breadcrumbs.append((p.get("name"), canonical))

    return {
        "title": f"{p.get('name')} — купить за {C.js_number(price)} ₽/{unit} · {shop_name}",
        "description": description[:300],
        "canonical": canonical, "og_type": "product", "image": img, "json_ld": json_ld,
        "breadcrumbs": _breadcrumb_list(breadcrumbs),
        "preload": {"route": "product", "slug": p.get("slug")},
        "noscript": noscript,
    }


# ---------------------------------------------------------------------------
# Каталог и категория
# ---------------------------------------------------------------------------
def catalog_meta(state: Mapping[str, Any], base: str, shop_name: str,
                 cat_id: str | None, now: datetime | None = None) -> dict[str, Any] | None:
    """Мета для каталога/категории, или `None`, если категория указана,
    но не найдена (→ 404)."""
    cat = None
    if cat_id is not None:
        cat = next((c for c in state.get("categories") or []
                   if c.get("id") == cat_id and c.get("is_active") is not False), None)
        if cat is None:
            return None

    def matches(p: Mapping[str, Any]) -> bool:
        if p.get("is_active") is False:
            return False
        if cat is None:
            return True
        if cat.get("id") == "sale":
            return C.is_sale(p, now)
        return p.get("category_id") == cat.get("id")

    items = [p for p in state.get("products") or [] if matches(p)]
    e = escape_html

    noscript = (f"<h1>{e(cat.get('name') if cat else 'Каталог')}</h1><ul>"
               + "".join(f'<li><a href="/product/{e(p.get("slug"))}">{e(p.get("name"))}</a>'
                         f' — {C.js_number(C.unit_price(p, now))} ₽</li>'
                         for p in items[:100])
               + "</ul>")

    breadcrumbs = [("Главная", f"{base}/"), ("Каталог", f"{base}/catalog")]
    if cat:
        breadcrumbs.append((cat.get("name"), f"{base}/catalog/{_uri(cat.get('id'))}"))

    return {
        "title": f"{cat['name']} — {shop_name}" if cat else f"Каталог продуктов — {shop_name}",
        "description": (f"{cat['name']}: {len(items)} товаров с доставкой за 2 часа "
                        "по Южному Бутово и Коммунарке." if cat else
                        "Свежие фрукты, овощи, ягоды, зелень и мясо по предзаказу. "
                        "Доставка за 2 часа."),
        "canonical": (f"{base}/catalog/{_uri(cat['id'])}" if cat
                     else f"{base}/catalog"),
        "breadcrumbs": _breadcrumb_list(breadcrumbs),
        "preload": {"route": "catalog", "category": cat["id"] if cat else "all"},
        "json_ld": {
            "@context": "https://schema.org", "@type": "ItemList",
            "name": cat["name"] if cat else "Каталог", "numberOfItems": len(items),
            "itemListElement": [
                {"@type": "ListItem", "position": i + 1,
                 "url": f"{base}/product/{_uri(p.get('slug'))}",
                 "name": p.get("name")}
                for i, p in enumerate(items[:40])
            ],
        },
        "noscript": noscript,
    }


def not_found_meta(base: str, shop_name: str, kind: str) -> dict[str, Any]:
    """`kind` — 'product' или 'category': текст 404 отличается."""
    if kind == "product":
        return {
            "title": f"Товар не найден — {shop_name}",
            "description": "Такого товара нет в каталоге.",
            "canonical": f"{base}/catalog", "noindex": True,
            "noscript": '<h1>Товар не найден</h1><p><a href="/catalog">Перейти в каталог</a></p>',
        }
    return {
        "title": f"Категория не найдена — {shop_name}",
        "description": "Такой категории нет.",
        "canonical": f"{base}/catalog", "noindex": True,
        "noscript": '<h1>Категория не найдена</h1><p><a href="/catalog">Весь каталог</a></p>',
    }


# ---------------------------------------------------------------------------
# Правовые страницы (ТЗ 14.3)
# ---------------------------------------------------------------------------
def legal_noscript(is_policy: bool, s: Mapping[str, Any]) -> str:
    e = escape_html
    if is_policy:
        return f"""<h1>Политика конфиденциальности</h1>
<p>Оператор персональных данных: {e(s.get('requisites') or '')}.</p>
<p>Мы обрабатываем имя, телефон, email и адрес доставки исключительно для исполнения заказа,
информирования о его статусе и выполнения требований 54-ФЗ (отправка электронного чека).</p>
<p>Правовое основание — согласие субъекта, которое фиксируется при оформлении заказа
с указанием даты и IP-адреса. Согласие на рекламные рассылки запрашивается отдельно
и не является условием покупки.</p>
<p>Срок хранения — 5 лет с даты последнего заказа (требования бухгалтерского учёта),
после чего данные обезличиваются. Передача третьим лицам — только курьерской службе
и оператору фискальных данных в объёме, необходимом для доставки и выдачи чека.</p>
<p>Вы вправе отозвать согласие, запросить копию, исправление или удаление своих данных,
написав на {e(s.get('email') or 'info@fructcity.ru')} или позвонив {e(s.get('phone') or '')}.</p>"""
    work_from = str(s.get("work_from")).zfill(2)
    work_to = str(s.get("work_to")).zfill(2)
    return f"""<h1>Публичная оферта</h1>
<p>{e(s.get('requisites') or '')} (далее — Продавец) предлагает заключить договор
розничной купли-продажи дистанционным способом на условиях, изложенных ниже.</p>
<p><strong>Товар и цена.</strong> Ассортимент и цены указаны в каталоге на сайте.
Цена весового товара является ориентировочной: окончательная сумма определяется
по фактическому весу при сборке. Допустимое отклонение — ±10%; при большем
отклонении Продавец согласовывает изменение с Покупателем.</p>
<p><strong>Оплата.</strong> Наличными или картой курьеру, либо онлайн. При онлайн-оплате
весового заказа резервируется сумма с запасом 10%; списывается фактическая сумма,
разница возвращается.</p>
<p><strong>Доставка.</strong> {e(s.get('pickup_address') or '')} — самовывоз ежедневно
{work_from}:00–{work_to}:00.
Доставка по зонам, указанным при оформлении, в выбранный двухчасовой интервал.</p>
<p><strong>Возврат.</strong> Продовольственные товары надлежащего качества возврату
не подлежат. Товар ненадлежащего качества принимается к возврату в день доставки.
Покупатель вправе отменить заказ до начала сборки.</p>
<p><strong>Чек.</strong> Электронный чек по 54-ФЗ направляется на email или в SMS.</p>"""


def legal_meta(base: str, shop_name: str, pathname: str, s: Mapping[str, Any]) -> dict[str, Any]:
    is_policy = pathname == "/policy"
    return {
        "title": ("Политика конфиденциальности" if is_policy else "Публичная оферта")
                 + f" — {shop_name}",
        "description": ("Как мы обрабатываем персональные данные согласно 152-ФЗ."
                        if is_policy else "Условия продажи товаров дистанционным способом."),
        "canonical": f"{base}{pathname}",
        "preload": {"route": "policy" if is_policy else "offer"},
        "noscript": legal_noscript(is_policy, s),
    }


# ---------------------------------------------------------------------------
# Главная и клиентские SPA-маршруты
# ---------------------------------------------------------------------------
def spa_meta(base: str, shop_name: str, pathname: str, s: Mapping[str, Any]) -> dict[str, Any]:
    is_home = pathname == "/"
    meta: dict[str, Any] = {
        "title": (f"{shop_name} — продукты, фрукты и мясо по предзаказу с доставкой за 2 часа"
                 if is_home else shop_name),
        "description": ("Свежие продукты, фрукты, овощи, ягоды и фермерские товары. Доставка по "
                        "Южному Бутово и Коммунарке за 2 часа, самовывоз. Весовые товары — "
                        "оплата по фактическому весу."),
        "canonical": f"{base}{'/' if is_home else pathname}",
        "noindex": not is_home,
        "preload": {"route": "home" if is_home else pathname.lstrip("/")},
    }
    if is_home:
        meta["json_ld"] = {
            "@context": "https://schema.org", "@type": "GroceryStore", "name": shop_name,
            "telephone": s.get("phone"), "email": s.get("email"),
            "address": {"@type": "PostalAddress", "streetAddress": s.get("pickup_address"),
                       "addressCountry": "RU"},
            "openingHours": (f"Mo-Su {str(s.get('work_from')).zfill(2)}:00-"
                            f"{str(s.get('work_to')).zfill(2)}:00"),
            "url": f"{base}/",
        }
        meta["noscript"] = (f"<h1>{escape_html(shop_name)}</h1>"
                            "<p>Продуктовый магазин с доставкой за 2 часа.</p>"
                            '<p><a href="/catalog">Открыть каталог</a></p>')
    else:
        meta["noscript"] = ""
    return meta
