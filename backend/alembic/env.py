"""Среда выполнения миграций.

Строка подключения берётся из тех же настроек, что и у приложения
(`FC_DB_*` плюс `.env`), а не из `alembic.ini`. Иначе пароль от боевой
базы лежал бы в двух местах, и однажды они разошлись бы — обычно в тот
момент, когда миграцию накатывают второпях.

`target_metadata` — метаданные моделей. От них работает
`--autogenerate`: alembic сравнивает описание таблиц в коде с тем, что
есть в базе, и пишет разницу.

Чего здесь делать нельзя: вызывать `create_all()`. Схему создаёт
миграция, иначе база окажется собранной мимо истории — и следующий
`upgrade` не будет знать, что уже применено.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

# Корень проекта в путях: alembic запускается из него, но при вызове из
# другого каталога (например, из планировщика) `backend` иначе не
# импортируется.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.config import get_settings          # noqa: E402
from backend.app.db.models import Base               # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    """Та же база, что и у приложения.

    `-x url=...` перекрывает настройки — так миграцию гоняют на копии
    базы, не трогая боевую: `alembic -x url=postgresql+psycopg://... upgrade head`.
    """
    override = context.get_x_argument(as_dictionary=True).get("url")
    if override:
        return override
    url = get_settings().database_url
    if not url:
        raise RuntimeError(
            "не настроена база: заполните FC_DB_* в .env либо передайте "
            "alembic -x url=postgresql+psycopg://пользователь:пароль@хост/база")
    return url


def run_migrations_offline() -> None:
    """Печатает SQL, не подключаясь к базе.

    Нужно, когда правку схемы на боевом сервере выполняет не приложение,
    а администратор: `alembic upgrade head --sql` отдаёт готовый скрипт,
    который можно прочитать глазами перед применением.
    """
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(get_url(), poolclass=pool.NullPool, future=True)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Без этих двух флагов autogenerate молча пропускает смену
            # типа колонки и значения по умолчанию — а это ровно те
            # правки, которые ломают данные незаметно.
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
