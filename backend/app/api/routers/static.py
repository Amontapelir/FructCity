"""Раздача `public/` и общего расчётного ядра `lib/calc.js`.

Роутер тонкий: весь разбор пути и правила кэша — в
`domain/static_files.py`, здесь только чтение файла и сборка ответа.

Подключается ПОСЛЕ всех остальных роутеров (см. `main.py`): последний
маршрут ловит всё, что не разобрали API и SSR-страницы. Порядок здесь
несущий, а не косметический: зарегистрируй этот роутер раньше — и он
перехватит `/`, `/catalog` и вообще всё.

Чего здесь делать нельзя: отдавать что-либо по пути, начинающемуся с
`/api/`. Для такого пути ответ обязан быть JSON-ошибкой — витрина
разбирает поле `error`, а не текст страницы.

Ловушка объявлена только для GET (и HEAD, который Starlette добавляет
сам). Из-за этого `POST` на несуществующую не-API страницу даёт 405
вместо 404 — мелочь, которой не бывает у живого клиента: витрина не
отправляет POST на страницы. Расширять ловушку на все методы нельзя:
тогда она перехватила бы и `POST /api/products`, где 405 как раз
правильный ответ (маршрут есть, метод не тот), и превратила бы его в
404 — то есть спрятала бы ошибку в клиенте.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request, Response

from ...config import ROOT
from ...domain import static_files as SF
from ..context import Fail

router = APIRouter(include_in_schema=False)

PUBLIC_DIR = ROOT / "public"
LIB_DIR = ROOT / "lib"

# Единственный адрес, который обслуживается не из `public/`: расчётное
# ядро браузера лежит в `lib/` рядом с серверным, чтобы формулы правили
# в одном месте. Копия в `public/` немедленно начала бы расходиться.
CALC_PATH = "/lib/calc.js"

NOT_FOUND_TEXT = "Страница не найдена"


def _serve(request: Request, root: Path, url_path: str) -> Response | None:
    found = SF.resolve_static(root, url_path)
    if found is None:
        return None

    stat = found.stat()
    etag = SF.etag_for(stat.st_size, stat.st_mtime_ns)
    headers = {
        "ETag": etag,
        "Cache-Control": SF.cache_control(found, versioned="v" in request.query_params),
        "Last-Modified": _http_date(stat.st_mtime),
    }
    # Браузер прислал тот же ETag — тело можно не гонять по сети.
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    return Response(content=found.read_bytes(), media_type=SF.content_type(found),
                    headers=headers)


def _http_date(mtime: float) -> str:
    """Формат RFC 7231 — тот, который браузер ждёт в `Last-Modified`."""
    from email.utils import formatdate

    return formatdate(mtime, usegmt=True)


@router.get("/{path:path}")
def static_or_not_found(path: str, request: Request) -> Response:
    pathname = "/" + path

    # Неизвестный адрес API — JSON-ошибка: витрина разбирает поле
    # `error`, а текст страницы разобрать не сможет.
    if pathname.startswith("/api/"):
        raise Fail(404, "not_found")

    if pathname == CALC_PATH:
        served = _serve(request, LIB_DIR, "/calc.js")
        if served is not None:
            return served

    served = _serve(request, PUBLIC_DIR, pathname)
    if served is not None:
        return served

    # Простой текст, а не оболочка витрины: иначе поисковик получил бы
    # 404 с телом главной страницы и проиндексировал бы её как дубль.
    return Response(content=NOT_FOUND_TEXT, status_code=404,
                    media_type="text/plain; charset=utf-8")
