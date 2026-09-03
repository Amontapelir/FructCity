"""Изоляция окружения для тестов.

Здесь одно правило, которое стоило трёх упавших тестов подряд:

    **Настройку нельзя погасить через `os.environ.pop()`.**

`pop()` убирает переменную окружения — но `Settings` читает ещё и `.env`
рядом с проектом, а там у разработчика лежат настоящие `FC_DB_*` и
(после переезда 1.7) `FC_WRITE_ENABLED=1`. Убрав переменную, тест
получает не «пусто», а значение из файла. Гасить можно только явным
пустым/ложным значением: оно переменную окружения ПЕРЕОПРЕДЕЛЯЕТ, и
файл проигрывает.

Чем это било на самом деле:

* тест «флаг выключен» проверял включённый флаг (`test_write_enabled`);
* тест «без базы источник — JSON» шёл в настоящую базу разработчика и
  проходил по случайности, пока снимок был отставшим
  (`test_source_staleness`);
* тест «флаг включён, базы нет» писал DELETE в рабочую базу.

Чего здесь делать нельзя: возвращать `pop()` «ради чистоты окружения».
Чистота окружения тут ничего не значит — значение придёт из `.env`.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

__all__ = ["DB_ENV_KEYS", "NO_DATABASE", "isolated_env"]

DB_ENV_KEYS = ("DATABASE_URL", "FC_DB_NAME", "FC_DB_HOST", "FC_DB_PASSWORD", "FC_DB_USER")

# «Базы нет и писать запрещено» — набор значений, а не набор удалений.
# Пустая строка годится для строковых настроек; для булева флага нужен
# именно "0" — пустую строку булево поле не разберёт.
NO_DATABASE: dict[str, str] = {
    **{key: "" for key in DB_ENV_KEYS},
    "FC_WRITE_ENABLED": "0",
}


def _refresh() -> None:
    """Сбрасывает кэш настроек и выбранный источник данных.

    Импорты внутри функции: `db.source` тянет SQLAlchemy, а этот модуль
    должен оставаться пригодным там, где её нет.
    """
    try:
        from backend.app.config import get_settings

        get_settings.cache_clear()
    except Exception:  # noqa: BLE001 — нет настроек, нечего и сбрасывать
        pass
    try:
        from backend.app.db import source

        source.reset()
    except Exception:  # noqa: BLE001 — нет SQLAlchemy, источника тоже нет
        pass


@contextmanager
def isolated_env(**overrides: str) -> Iterator[None]:
    """Временно выставляет переменные и возвращает прежние значения.

    Значения только выставляются, никогда не удаляются, — см. docstring
    модуля. По выходе прежнее состояние восстанавливается точно: то,
    чего не было, снова убирается.
    """
    saved = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    _refresh()
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        _refresh()
