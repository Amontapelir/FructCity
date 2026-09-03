"""Разбор пути статики, типы содержимого и правила кэша.

Почему отдельным модулем, а не в роутере: путь к файлу и защита от
выхода за корень — ровно та логика, ошибка в которой отдаёт наружу
`/etc/passwd`. Её надо проверять прямыми тестами, без HTTP и без
поднятого приложения.

Чего здесь делать нельзя: складывать сюда чтение файла и сборку
ответа — это работа роутера. Здесь только «какой файл имелся в виду и
можно ли его отдавать».
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from urllib.parse import unquote

__all__ = [
    "MIME", "DEFAULT_MIME", "IMMUTABLE_SUFFIXES",
    "resolve_static", "content_type", "etag_for", "cache_control",
]

# Тип указывается явно, а не угадывается `mimetypes`: тот берёт таблицу
# из реестра Windows и системных файлов, то есть ответ сервера зависел
# бы от машины. Шрифт или скрипт с неверным типом браузер при строгой
# политике безопасности просто не примет.
MIME: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".ico": "image/x-icon",
    ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8",
    ".xml": "application/xml; charset=utf-8", ".webmanifest": "application/manifest+json",
}
DEFAULT_MIME = "application/octet-stream"

# Неизменяемым считается файл с этими расширениями И с параметром `?v=`
# в адресе. Без версии в адресе вечный кэш означал бы, что правку
# витрины пользователь увидит только после очистки кэша браузера.
IMMUTABLE_SUFFIXES = frozenset({".js", ".css", ".woff2", ".png", ".svg", ".jpg"})


def resolve_static(root: Path, url_path: str) -> Path | None:
    """Путь к файлу внутри `root` или ``None``, если отдавать нельзя.

    Отказ (а не исключение) — на любой из причин: неразбираемое
    процентное кодирование, нулевой байт в пути, выход за корень,
    отсутствие файла, каталог вместо файла, символическая ссылка
    наружу. Выход за корень закрыт не проверкой строки на `..`, а
    приведением пути к каноническому виду: `resolve()` схлопывает `..`
    И разыменовывает символические ссылки, после чего проверяется, что
    результат физически лежит внутри канонического корня. Поэтому
    `%2e%2e%2f` и ссылка наружу закрываются тем же кодом, что и
    обычный `../` — их не нужно перечислять.
    """
    try:
        decoded = unquote(url_path, errors="strict")
    except (UnicodeDecodeError, ValueError):
        return None
    if "\0" in decoded:
        return None

    # ведущие разделители убираем, иначе путь считается абсолютным и
    # `root / rel` молча отбросит корень
    rel = decoded.lstrip("/\\")
    if not rel:
        return None

    root_real = root.resolve()
    try:
        target = (root_real / rel).resolve()
    except (OSError, ValueError):        # слишком длинный путь, битые символы
        return None

    if target != root_real and root_real not in target.parents:
        return None
    if not target.is_file():             # нет файла или это каталог
        return None
    return target


def content_type(path: Path) -> str:
    return MIME.get(path.suffix.lower(), DEFAULT_MIME)


def etag_for(size: int, mtime_ns: int) -> str:
    """ETag по размеру и времени правки.

    Считать хеш содержимого не нужно: файл может быть в мегабайты, а
    отвечать надо на каждый запрос. Размер и время правки меняются при
    любой правке файла, и этого достаточно.

    ETag сильный (в кавычках, без префикса `W/`): по слабому браузер не
    станет запрашивать диапазон, а по этому пришлёт `If-None-Match` и
    получит 304 вместо повторной загрузки.

    Значения на двух машинах не совпадут — время правки у копий файла
    своё. Это нормально: ETag сравнивается с самим собой, выданным
    ранее тем же сервером.
    """
    raw = f"{size}-{mtime_ns // 1_000_000}".encode()
    digest = hashlib.sha1(raw, usedforsecurity=False).digest()
    return '"' + base64.urlsafe_b64encode(digest).rstrip(b"=").decode() + '"'


def cache_control(path: Path, *, versioned: bool) -> str:
    """Вечный кэш — только для версионированного адреса."""
    if versioned and path.suffix.lower() in IMMUTABLE_SUFFIXES:
        return "public, max-age=31536000, immutable"
    return "public, max-age=0, must-revalidate"
