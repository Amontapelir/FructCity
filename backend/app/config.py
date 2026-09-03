"""Настройки приложения.

Имена переменных достались от прежней версии магазина (`NODE_ENV`,
`PORT`) и намеренно не переименованы: они уже прописаны в `.env` на
машинах и в развёртывании, а переименование ради красоты стоило бы
одного тихого запуска с чужими настройками.

**В `.env` попадают только секреты.** Пароль базы, секрет вебхука
Telegram, ключи платёжного шлюза, когда они появятся. Хост, порт и имя
базы секретами не являются: они разные на машине разработчика и на
сервере, но прятать их незачем, а держать в коде — удобнее, потому что
видно в репозитории и не забывается при развёртывании.

Отсюда и решение не хранить `DATABASE_URL` целиком: собранная строка
склеивает пароль с конфигурацией, и вынести её из `.env` уже нельзя.
Строка собирается здесь из частей (см. `db/dsn.py`), а из окружения
берётся только пароль.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .db.dsn import DEFAULT_DRIVER, build_url, mask_url

# Корень проекта: backend/app/config.py → backend/app → backend → корень
ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",          # в .env бывают чужие переменные — не наше дело
        case_sensitive=False,
    )

    host: str = Field(default="127.0.0.1", alias="HOST")
    port: int = Field(default=3000, alias="PORT")
    env: str = Field(default="development", alias="NODE_ENV")

    # Где лежит хранилище. Пустое значение — папка data рядом с проектом.
    data_dir: str = Field(default="", alias="FC_DATA_DIR")

    # Код из SMS в ответе API. Только для разработки, в бою обязан быть выключен.
    dev_otp: bool = Field(default=False, alias="FC_DEV_OTP")

    telegram_bot: str = Field(default="", alias="FC_TELEGRAM_BOT")
    telegram_secret: SecretStr = Field(default=SecretStr(""), alias="FC_TELEGRAM_SECRET")

    # --- право записи -----------------------------------------------------
    # В бою включено (`FC_WRITE_ENABLED=1`). По умолчанию выключено, и
    # это не про переезд: снятый флаг переводит приложение в режим
    # «только чтение» — витрина работает, заказ не оформить
    # («write_disabled»). Пригождается на время работ с базой и при
    # разборе инцидента, когда писать нельзя, а показывать каталог надо.
    #
    # Умолчание «выключено» выбрано намеренно: свежий запуск с чужим или
    # пустым `.env` не должен молча начать писать в базу.
    write_enabled: bool = Field(default=False, alias="FC_WRITE_ENABLED")

    # --- база данных ------------------------------------------------------
    # Строка подключения НЕ хранится целиком. В `.env` лежит только пароль,
    # остальное — обычная конфигурация: она разная на машине разработчика
    # и на сервере, но секретом не является и прячется зря.
    #
    # Пароль обёрнут в SecretStr, поэтому не попадает в repr настроек и в
    # трассировку исключения. Достать его можно только явным вызовом
    # `.get_secret_value()` — то есть намеренно.
    db_driver: str = Field(default=DEFAULT_DRIVER, alias="FC_DB_DRIVER")
    db_host: str = Field(default="127.0.0.1", alias="FC_DB_HOST")
    db_port: int = Field(default=5432, alias="FC_DB_PORT")
    db_name: str = Field(default="fructcity", alias="FC_DB_NAME")
    db_user: str = Field(default="postgres", alias="FC_DB_USER")
    db_password: SecretStr = Field(default=SecretStr(""), alias="FC_DB_PASSWORD")

    # --- административная учётная запись ---------------------------------
    # Нужна ровно один раз: чтобы создать роль приложения и саму базу.
    # Создать базу, уже находясь в ней, нельзя — надо подключиться к
    # служебной (`postgres`) под учётной записью, которой это позволено.
    #
    # Приложение этими данными не пользуется никогда. Держать их в `.env`
    # постоянно незачем: создали базу — можно стереть.
    db_admin_user: str = Field(default="", alias="FC_DB_ADMIN_USER")
    db_admin_password: SecretStr = Field(default=SecretStr(""), alias="FC_DB_ADMIN_PASSWORD")
    db_admin_db: str = Field(default="postgres", alias="FC_DB_ADMIN_DB")

    # Готовая строка целиком. Нужна там, где её выдаёт хостинг одним
    # значением — Docker, Heroku, Render. Если задана, побеждает: спорить
    # с окружением, которое само знает свой адрес базы, бессмысленно.
    database_url_override: str = Field(default="", alias="DATABASE_URL")

    @property
    def is_production(self) -> bool:
        return self.env.lower() == "production"

    @property
    def database_url(self) -> str:
        """Строка подключения: либо готовая из окружения, либо собранная."""
        if self.database_url_override:
            return self.database_url_override
        if not self.db_name:
            return ""
        return build_url(
            driver=self.db_driver,
            user=self.db_user,
            password=self.db_password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            name=self.db_name,
        )

    @property
    def database_url_safe(self) -> str:
        """То же самое без пароля — для консоли и журнала."""
        return mask_url(self.database_url)

    @property
    def db_admin_credentials(self) -> tuple[str, str]:
        """Учётные данные для создания базы.

        Если административная запись не задана отдельно, берём запись
        приложения: на машине разработчика это обычно один и тот же
        `postgres`, и требовать заполнять два поля одинаково — лишний
        повод ошибиться.
        """
        if self.db_admin_user:
            return self.db_admin_user, self.db_admin_password.get_secret_value()
        return self.db_user, self.db_password.get_secret_value()

    @property
    def admin_database_url(self) -> str:
        """Подключение к служебной базе — только для создания.

        Имя базы здесь `postgres`, а не наше: создать базу можно лишь
        находясь в какой-то другой.
        """
        user, password = self.db_admin_credentials
        return build_url(
            driver=self.db_driver,
            user=user,
            password=password,
            host=self.db_host,
            port=self.db_port,
            name=self.db_admin_db or "postgres",
        )

    @property
    def db_admin_is_separate(self) -> bool:
        """Отличается ли административная запись от записи приложения."""
        return bool(self.db_admin_user) and self.db_admin_user != self.db_user

    @property
    def db_missing(self) -> list[str]:
        """Какие переменные для подключения не заполнены.

        Возвращаем именно список имён, а не «да/нет»: сообщение «база
        не настроена» ничего не даёт человеку, который только что
        заполнил `.env` и уверен, что всё на месте. Переменных две
        пары, и перепутать пароль приложения с административным —
        обычное дело.
        """
        if self.database_url_override:
            return []
        missing = []
        if not self.db_name:
            missing.append("FC_DB_NAME")
        if not self.db_user:
            missing.append("FC_DB_USER")
        if not self.db_password.get_secret_value():
            missing.append("FC_DB_PASSWORD")
        return missing

    @property
    def db_configured(self) -> bool:
        """Настроена ли база.

        Пустой пароль считаем «не настроено»: сейчас данные лежат
        в JSON, и поднимать соединение с базой, которой, возможно,
        нет, приложение не должно. Доступность порта тут ни при чём —
        это вопрос намерения, а не связи.
        """
        return not self.db_missing

    def db_settings_report(self) -> list[tuple[str, str, bool]]:
        """Состояние переменных подключения: имя, что видно, задано ли.

        Значения паролей не возвращаются никогда — только признак
        «пусто» или «задано». Отчёт печатается в консоль, а пароль
        в консоли остаётся в истории команд.
        """
        secret = lambda v: "задан" if v else "ПУСТО"  # noqa: E731
        return [
            ("FC_DB_HOST", self.db_host, True),
            ("FC_DB_PORT", str(self.db_port), True),
            ("FC_DB_NAME", self.db_name or "ПУСТО", bool(self.db_name)),
            ("FC_DB_USER", self.db_user or "ПУСТО", bool(self.db_user)),
            ("FC_DB_PASSWORD", secret(self.db_password.get_secret_value()),
             bool(self.db_password.get_secret_value())),
            ("FC_DB_ADMIN_USER", self.db_admin_user or "(берётся FC_DB_USER)", True),
            ("FC_DB_ADMIN_PASSWORD", secret(self.db_admin_password.get_secret_value()),
             bool(self.db_admin_password.get_secret_value())),
            ("DATABASE_URL", "задан" if self.database_url_override else "не задан", True),
        ]

    @property
    def store_path(self) -> Path:
        base = Path(self.data_dir) if self.data_dir else ROOT / "data"
        return base / "store.json"


@lru_cache
def get_settings() -> Settings:
    """Настройки читаются один раз за процесс.

    Кэш нужен и тестам: они подменяют его через
    ``get_settings.cache_clear()``, а не правят глобальную переменную.
    """
    return Settings()
