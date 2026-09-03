"""Сборка строки подключения к базе из частей.

Отдельный модуль без зависимостей — по двум причинам.

Первая: строку подключения нельзя склеивать вручную. Пароль попадает
в неё в той части адреса, где символы `@`, `:`, `/` и `#` имеют
служебное значение. Пароль вида `p@ss:w/ord` обрывает разбор адреса,
и приложение сообщает что-нибудь вроде «неизвестный хост ss» — искать
причину в этом сообщении можно долго.

Вторая: функция чистая, поэтому проверяется тестами без установки
pydantic и SQLAlchemy.

В `.env` живут только секреты — пароль и ключи. Хост, порт и имя базы
это конфигурация: их видно в репозитории, они разные на машине
разработчика и на сервере, и прятать их незачем.
"""

from __future__ import annotations

from urllib.parse import quote

__all__ = ["build_url", "mask_url", "DEFAULT_DRIVER"]

DEFAULT_DRIVER = "postgresql+psycopg"


def build_url(
    *,
    driver: str = DEFAULT_DRIVER,
    user: str = "",
    password: str = "",
    host: str = "127.0.0.1",
    port: int | None = 5432,
    name: str = "",
) -> str:
    """Собирает строку подключения, экранируя учётные данные.

    >>> build_url(user="postgres", password="p@ss:w/ord", name="fructcity")
    'postgresql+psycopg://postgres:p%40ss%3Aw%2Ford@127.0.0.1:5432/fructcity'

    Пустой пароль допустим — тогда двоеточия в адресе не будет:
    `user:@host` некоторые драйверы понимают как пароль из одного
    пустого символа.
    """
    if not name:
        raise ValueError("не задано имя базы")
    if not driver:
        raise ValueError("не задан драйвер")

    # safe="" — экранируем всё, включая слэш: он в пароле встречается
    # чаще, чем кажется, и рвёт адрес ровно посередине.
    credentials = ""
    if user:
        credentials = quote(user, safe="")
        if password:
            credentials += ":" + quote(password, safe="")
        credentials += "@"

    place = host or "127.0.0.1"
    if port:
        place = f"{place}:{int(port)}"

    return f"{driver}://{credentials}{place}/{quote(str(name), safe='')}"


def mask_url(url: str) -> str:
    """Прячет пароль: такую строку можно печатать в консоль и в журнал.

    Пароль в логе — это пароль в логе, независимо от того, кто и с
    какими намерениями его туда написал.
    """
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" not in rest:
        return url
    creds, tail = rest.rsplit("@", 1)
    if ":" not in creds:
        return url
    user, _ = creds.split(":", 1)
    return f"{scheme}://{user}:***@{tail}"
