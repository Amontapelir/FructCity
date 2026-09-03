"""Схема базы данных и управление ею (ТЗ 13).

Один файл на всё: модели, создание, очистка, проверка. Alembic пока не
заводим — пока схема меняется каждый день, миграции только мешают, а
когда стабилизируется, начальную ревизию снимут с готовых моделей.

Как пользоваться из командной строки, из корня проекта:

    python -m backend.app.db.models --check     # что есть, чего нет
    python -m backend.app.db.models --create    # создать базу и таблицы
    python -m backend.app.db.models --clear     # опустошить таблицы
    python -m backend.app.db.models --drop      # снести таблицы совсем

Две учётные записи, и это важно.

**Административная** нужна ровно один раз, для `--create`. Создать
базу, находясь в ней самой, нельзя: надо подключиться к служебной
базе `postgres` под записью, которой это позволено, и уже оттуда
выполнить CREATE DATABASE. Задаётся переменными `FC_DB_ADMIN_USER`
и `FC_DB_ADMIN_PASSWORD`; после создания базы их можно стереть.

**Запись приложения** — та, под которой магазин работает каждый день:
`FC_DB_USER` и `FC_DB_PASSWORD`. Роль создаётся при `--create` и
становится владельцем базы. Прав за пределами своей базы у неё нет,
поэтому ошибка в коде не заденет остальные.

Если административная запись не задана, берётся запись приложения —
на машине разработчика это обычно один и тот же `postgres`.

Хост, порт и имя базы — обычная конфигурация со значениями по
умолчанию (127.0.0.1:5432, база `fructcity`); переопределяются
переменными `FC_DB_HOST`, `FC_DB_PORT`, `FC_DB_NAME`. Готовую строку
целиком можно передать через `DATABASE_URL` — так её выдают Docker и
облачные хостинги, и тогда она побеждает.

Про типы. Деньги — целые рубли, как и во всём проекте: расчёт ведётся
в целых, и хранить их дробными значило бы завести второй источник
правды об округлении. Веса дробные, до сотых килограмма.

Даты и время хранятся строками ISO — так они лежали в JSON, и перенос
не превратился в разбор форматов. Это долг, а не решение: строка не
умеет сравниваться по часовым поясам и не проверяется базой на
осмысленность. Перевод на `timestamptz` — отдельная миграция, и теперь,
когда есть alembic, она делается одной ревизией.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Iterable

from sqlalchemy import (
    Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
    create_engine, func, inspect, select, text,
)
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from ..config import get_settings
from .dsn import mask_url


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Справочники
# ---------------------------------------------------------------------------
class Category(Base):
    __tablename__ = "categories"

    # Идентификатор строковый и осмысленный ("fruit", "meat"): он попадает
    # в адреса каталога, и подменять его числом значило бы завести ещё
    # одно соответствие, которое надо где-то держать.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    emoji: Mapped[str | None] = mapped_column(String(16), default=None)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[str | None] = mapped_column(String(40), default=None)


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    slug: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(256))
    category_id: Mapped[str | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), index=True, default=None)

    # unit | weighted | preorder
    type: Mapped[str] = mapped_column(String(16), index=True)
    price: Mapped[int] = mapped_column(Integer, default=0)
    price_per_kg: Mapped[int] = mapped_column(Integer, default=0)
    sale_price: Mapped[int | None] = mapped_column(Integer, default=None)
    sale_until: Mapped[str | None] = mapped_column(String(10), default=None)
    vat_rate: Mapped[int] = mapped_column(Integer, default=0)
    stock: Mapped[float] = mapped_column(Float, default=0)
    min_weight: Mapped[float] = mapped_column(Float, default=0.5)
    weight_step: Mapped[float] = mapped_column(Float, default=0.5)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    emoji: Mapped[str | None] = mapped_column(String(16), default=None)
    image_key: Mapped[str | None] = mapped_column(String(64), default=None)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[str | None] = mapped_column(String(40), default=None)
    updated_at: Mapped[str | None] = mapped_column(String(40), default=None)


class DeliveryZone(Base):
    __tablename__ = "delivery_zones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    # NULL в cost — это «расчёт вручную», а не «бесплатно» (ТЗ 5.2).
    # Ноль здесь означал бы бесплатную доставку, поэтому поле обязано
    # допускать пустое значение.
    cost: Mapped[int | None] = mapped_column(Integer, default=None)
    free_from: Mapped[int | None] = mapped_column(Integer, default=None)
    manual_quote: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[str | None] = mapped_column(String(40), default=None)


class Promocode(Base):
    __tablename__ = "promocodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    type: Mapped[str] = mapped_column(String(16))          # percent | fixed | delivery
    value: Mapped[int] = mapped_column(Integer, default=0)
    min_order: Mapped[int] = mapped_column(Integer, default=0)
    uses_limit: Mapped[int] = mapped_column(Integer, default=0)
    uses_count: Mapped[int] = mapped_column(Integer, default=0)
    per_user_limit: Mapped[int] = mapped_column(Integer, default=0)
    valid_until: Mapped[str | None] = mapped_column(String(10), default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[str | None] = mapped_column(String(40), default=None)


# ---------------------------------------------------------------------------
# Люди и сессии
# ---------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    login: Mapped[str | None] = mapped_column(String(64), unique=True, index=True, default=None)
    name: Mapped[str | None] = mapped_column(String(128), default=None)
    role: Mapped[str] = mapped_column(String(16), default="customer")
    phone: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    email: Mapped[str | None] = mapped_column(String(128), default=None)
    # Хеш scrypt в самоописательном формате (`scrypt$N$r$p$соль$хеш`).
    # Открытых паролей здесь нет и быть не может.
    password_hash: Mapped[str | None] = mapped_column(String(256), default=None)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[str | None] = mapped_column(String(40), default=None)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sid: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, default=None)
    role: Mapped[str] = mapped_column(String(16), default="guest")
    promo_code: Mapped[str | None] = mapped_column(String(64), default=None)
    ip: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[str | None] = mapped_column(String(40), default=None)
    last_seen: Mapped[str | None] = mapped_column(String(40), default=None)
    expires_at: Mapped[str | None] = mapped_column(String(40), index=True, default=None)


class CartItem(Base):
    """Корзина гостя. В JSON лежала списком внутри сессии.

    Здесь это отдельная таблица: так строку корзины можно связать с
    товаром внешним ключом, и удалённый товар не оставит в корзине
    ссылку в никуда.
    """

    __tablename__ = "cart_items"
    __table_args__ = (UniqueConstraint("session_id", "product_id", name="uq_cart_session_product"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    qty: Mapped[int | None] = mapped_column(Integer, default=None)
    weight: Mapped[float | None] = mapped_column(Float, default=None)


class SessionRecent(Base):
    """Заказы и предзаказы, доступные этой сессии.

    Гость не входит в кабинет, но должен видеть и отменять то, что
    только что оформил. В JSON это были два списка идентификаторов
    внутри сессии; здесь — отдельная таблица.

    Держать те же списки текстом JSON было бы быстрее, но это ровно
    тот случай, когда сокращение мстит: по такому полю нельзя ни
    присоединиться к заказам, ни поставить внешний ключ, и удалённый
    заказ навсегда останется в нём ссылкой в никуда.
    """

    __tablename__ = "session_recent"
    __table_args__ = (
        UniqueConstraint("session_id", "kind", "ref_id", name="uq_session_recent"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))          # order | preorder
    ref_id: Mapped[int] = mapped_column(Integer)


class Otp(Base):
    __tablename__ = "otp"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone: Mapped[str] = mapped_column(String(32), index=True)
    # Хранится хеш кода, а не сам код: утечка базы не должна давать
    # возможность войти в чужой кабинет.
    code_hash: Mapped[str] = mapped_column(String(256))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[str | None] = mapped_column(String(40), default=None)
    created_at: Mapped[str | None] = mapped_column(String(40), default=None)


class TelegramLink(Base):
    __tablename__ = "tg_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), default=None)
    chat_id: Mapped[str | None] = mapped_column(String(64), default=None)
    expires_at: Mapped[str | None] = mapped_column(String(40), default=None)
    created_at: Mapped[str | None] = mapped_column(String(40), default=None)


# ---------------------------------------------------------------------------
# Заказы
# ---------------------------------------------------------------------------
class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    number: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, default=None)
    is_guest: Mapped[bool] = mapped_column(Boolean, default=True)

    name: Mapped[str] = mapped_column(String(128))
    phone: Mapped[str] = mapped_column(String(32), index=True)
    email: Mapped[str | None] = mapped_column(String(128), default=None)

    method: Mapped[str] = mapped_column(String(16))                # delivery | pickup
    delivery_zone_id: Mapped[int | None] = mapped_column(
        ForeignKey("delivery_zones.id", ondelete="SET NULL"), default=None)
    address: Mapped[str | None] = mapped_column(Text, default=None)
    slot_ymd: Mapped[str | None] = mapped_column(String(10), index=True, default=None)
    slot_from: Mapped[int | None] = mapped_column(Integer, default=None)
    slot_to: Mapped[int | None] = mapped_column(Integer, default=None)
    comment: Mapped[str | None] = mapped_column(Text, default=None)

    payment_method: Mapped[str] = mapped_column(String(16))
    payment_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    status: Mapped[str] = mapped_column(String(32), default="new", index=True)

    promocode_id: Mapped[int | None] = mapped_column(
        ForeignKey("promocodes.id", ondelete="SET NULL"), default=None)
    promocode: Mapped[str | None] = mapped_column(String(64), default=None)

    discount_amount: Mapped[int] = mapped_column(Integer, default=0)
    delivery_discount: Mapped[int] = mapped_column(Integer, default=0)
    delivery_cost: Mapped[int] = mapped_column(Integer, default=0)
    # Стоимость доставки, о которой договорились при оформлении.
    # Пересчёт при сборке может её только уменьшить: сумма, названная
    # клиенту, — это обязательство.
    agreed_delivery_cost: Mapped[int] = mapped_column(Integer, default=0)
    items_total: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    hold_amount: Mapped[int] = mapped_column(Integer, default=0)
    planned_total: Mapped[int] = mapped_column(Integer, default=0)

    telegram_optin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str | None] = mapped_column(String(40), index=True, default=None)
    updated_at: Mapped[str | None] = mapped_column(String(40), default=None)


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), default=None)

    # Название и артикул скопированы, а не взяты по ссылке: карточку
    # товара переименуют, а в чеке должно остаться то, что купили.
    sku: Mapped[str | None] = mapped_column(String(64), default=None)
    name: Mapped[str] = mapped_column(String(256))
    type: Mapped[str] = mapped_column(String(16))

    requested_quantity: Mapped[int | None] = mapped_column(Integer, default=None)
    requested_weight: Mapped[float | None] = mapped_column(Float, default=None)
    actual_weight: Mapped[float | None] = mapped_column(Float, default=None)
    weight_confirmed: Mapped[bool | None] = mapped_column(Boolean, default=None)
    is_removed: Mapped[bool] = mapped_column(Boolean, default=False)

    # Цена зафиксирована в момент оформления. Пересчёт после взвешивания
    # идёт по ней, а не по текущему каталогу.
    price_at_purchase: Mapped[int] = mapped_column(Integer, default=0)
    was_sale: Mapped[bool] = mapped_column(Boolean, default=False)
    vat_rate: Mapped[int] = mapped_column(Integer, default=0)


class OrderStatusHistory(Base):
    __tablename__ = "order_status_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str | None] = mapped_column(String(64), default=None)
    comment: Mapped[str | None] = mapped_column(Text, default=None)
    at: Mapped[str | None] = mapped_column(String(40), default=None)


class Preorder(Base):
    __tablename__ = "preorders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    number: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None)
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), default=None)
    sku: Mapped[str | None] = mapped_column(String(64), default=None)
    product_name: Mapped[str] = mapped_column(String(256))
    requested_weight: Mapped[float] = mapped_column(Float, default=0)
    price_per_kg: Mapped[int] = mapped_column(Integer, default=0)
    estimate: Mapped[int] = mapped_column(Integer, default=0)
    pickup_date: Mapped[str | None] = mapped_column(String(10), index=True, default=None)
    name: Mapped[str] = mapped_column(String(128))
    phone: Mapped[str] = mapped_column(String(32), index=True)
    comment: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    created_at: Mapped[str | None] = mapped_column(String(40), default=None)
    updated_at: Mapped[str | None] = mapped_column(String(40), default=None)


class PromocodeUsage(Base):
    __tablename__ = "promocode_usages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    promocode_id: Mapped[int] = mapped_column(
        ForeignKey("promocodes.id", ondelete="CASCADE"), index=True)
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), default=None)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None)
    phone: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    at: Mapped[str | None] = mapped_column(String(40), default=None)


# ---------------------------------------------------------------------------
# Брони, согласия, журнал
# ---------------------------------------------------------------------------
class SlotBooking(Base):
    """Занятость интервала. Доставка и самовывоз считаются раздельно."""

    __tablename__ = "slot_bookings"
    __table_args__ = (
        UniqueConstraint("method", "ymd", "slot_from", name="uq_slot"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    method: Mapped[str] = mapped_column(String(16))
    ymd: Mapped[str] = mapped_column(String(10), index=True)
    slot_from: Mapped[int] = mapped_column(Integer)
    booked: Mapped[int] = mapped_column(Integer, default=0)


class MeatBooking(Base):
    """Сколько килограммов мяса уже заказано на дату (ТЗ 7.1)."""

    __tablename__ = "meat_bookings"

    ymd: Mapped[str] = mapped_column(String(10), primary_key=True)
    booked_kg: Mapped[float] = mapped_column(Float, default=0)


class Consent(Base):
    __tablename__ = "consents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None)
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), default=None)
    preorder_id: Mapped[int | None] = mapped_column(
        ForeignKey("preorders.id", ondelete="SET NULL"), default=None)
    phone: Mapped[str | None] = mapped_column(String(32), default=None)
    personal_data: Mapped[bool] = mapped_column(Boolean, default=False)
    marketing: Mapped[bool] = mapped_column(Boolean, default=False)
    ip: Mapped[str | None] = mapped_column(String(64), default=None)
    user_agent: Mapped[str | None] = mapped_column(Text, default=None)
    at: Mapped[str | None] = mapped_column(String(40), default=None)


class AuditRecord(Base):
    __tablename__ = "audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    action: Mapped[str] = mapped_column(String(64), index=True)
    details: Mapped[str | None] = mapped_column(Text, default=None)
    at: Mapped[str | None] = mapped_column(String(40), index=True, default=None)


class Setting(Base):
    """Настройки магазина и конструктор главной — ключ и значение.

    Отдельных колонок под каждую настройку нет намеренно: их список
    меняется чаще, чем всё остальное, и каждая новая галочка требовала
    бы миграции. Значение хранится текстом JSON.
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


# Порядок важен: очищаем и сносим в обратном порядке зависимостей,
# иначе внешние ключи не дадут удалить строки.
ALL_TABLES = tuple(Base.metadata.sorted_tables)
TABLE_NAMES = tuple(t.name for t in ALL_TABLES)


# ===========================================================================
# Управление базой
# ===========================================================================
# Кэш движков по строке подключения. Каждый вызов `get_engine()` без
# аргументов раньше создавал НОВЫЙ пул соединений — а вызывающих мест
# много (`source.py` на каждый запрос, `models.is_ready`, диагностика,
# `--check`), и ни одно не звало `dispose()`. Соединение закрывается
# сборщиком мусора, а не сразу, поэтому пул рос, пока PostgreSQL не
# начинал отказывать по `53300` — «слишком много соединений». Кэш решает
# это: на один процесс — один пул на одну и ту же базу.
_engine_cache: dict[tuple[str, bool], Engine] = {}


def get_engine(url: str | None = None, echo: bool = False) -> Engine:
    """Подключение к рабочей базе. Один пул на процесс на одну строку."""
    settings = get_settings()
    if url is None and not settings.db_configured:
        raise RuntimeError(_missing_message(settings))
    resolved = url or settings.database_url
    key = (resolved, echo)
    engine = _engine_cache.get(key)
    if engine is None:
        engine = create_engine(resolved, echo=echo, future=True)
        _engine_cache[key] = engine
    return engine


def dispose_engines() -> None:
    """Закрыть все закэшированные пулы. Нужно тестам между прогонами —
    иначе состояние одного теста (например, временная SQLite-база,
    удалённая в `tearDown`) утекает в следующий через кэш."""
    for engine in _engine_cache.values():
        engine.dispose()
    _engine_cache.clear()


def _missing_message(settings) -> str:
    """Сообщение о незаполненных настройках — с именами и состоянием.

    Прежний текст говорил «база не настроена» и предлагал заполнить
    FC_DB_PASSWORD. Человеку, который только что заполнил `.env` и
    видит там пароль, это сообщение бесполезно: паролей два, и пустым
    остался другой. Поэтому печатаем состояние всех переменных сразу.
    """
    missing = ", ".join(settings.db_missing)
    lines = [
        f"Не заполнено в .env: {missing}",
        "",
        "Паролей два, и это разные вещи:",
        "  FC_DB_PASSWORD       — пароль роли приложения, под которой работает магазин.",
        "                         Его вы придумываете сами, роль создаётся командой --create.",
        "  FC_DB_ADMIN_PASSWORD — пароль суперпользователя PostgreSQL, задан при установке.",
        "                         Нужен один раз, чтобы создать роль выше и саму базу.",
        "",
        "Сейчас в настройках:",
    ]
    for name, shown, ok in settings.db_settings_report():
        lines.append(f"  {'  ' if ok else '! '}{name:22} {shown}")
    return "\n".join(lines)


def provision(url: str | None = None) -> dict[str, Any]:
    """Создаёт роль приложения и саму базу под административной записью.

    Порядок здесь не свободный, он продиктован устройством PostgreSQL:

    1. Подключаемся под административной записью к служебной базе
       `postgres`. Создать базу, находясь в ней самой, невозможно —
       нужна какая-то другая, уже существующая.
    2. Заводим роль приложения, если её нет.
    3. Создаём базу и делаем эту роль её владельцем — тогда таблицы
       создадутся без дополнительных выдач прав.

    Только после этого приложение подключается уже под своей записью.
    Административная ему больше не нужна никогда, и её данные можно
    убрать из `.env`.

    Возвращает, что именно было создано: без этого нельзя отличить
    «создали» от «уже было».
    """
    settings = get_settings()
    target_url = make_url(url or settings.database_url)
    if target_url.get_backend_name() == "sqlite":
        return {"role_created": False, "database_created": False, "skipped": "sqlite"}

    target = target_url.database
    if not target:
        raise RuntimeError("не указано имя базы")
    _check_identifier(target)

    app_user = target_url.username or ""
    app_password = target_url.password or ""
    admin_user, _ = settings.db_admin_credentials

    # AUTOCOMMIT обязателен: CREATE DATABASE не выполняется внутри
    # транзакции, и без него команда просто не пройдёт.
    admin = create_engine(settings.admin_database_url, isolation_level="AUTOCOMMIT", future=True)
    result: dict[str, Any] = {
        "role_created": False, "database_created": False,
        "admin_user": admin_user, "app_user": app_user, "database": target,
    }
    try:
        with admin.connect() as conn:
            if app_user and app_user != admin_user:
                result["role_created"] = _ensure_role(conn, app_user, app_password)
            result["database_created"] = _ensure_database(conn, target, app_user or admin_user)
    finally:
        admin.dispose()
    return result


def _ensure_role(conn, user: str, password: str) -> bool:
    """Заводит роль приложения. Существующую не трогает.

    Пароль существующей роли намеренно не переписываем: под этой ролью
    может работать что-то ещё, и молча сменить ей пароль — значит
    сломать это что-то в момент, когда никто не поймёт причину.
    """
    _check_identifier(user)
    exists = conn.execute(
        text("SELECT 1 FROM pg_roles WHERE rolname = :n"), {"n": user}).scalar()
    if exists:
        return False

    # Пароль нельзя передать параметром: CREATE ROLE — команда
    # определения, а не запрос. Собираем её средствами драйвера,
    # он экранирует и имя, и строку по правилам PostgreSQL. Собирать
    # такое склейкой строк нельзя: пароль с кавычкой закончил бы
    # команду досрочно, а всё после неё выполнилось бы как SQL.
    from psycopg import sql

    raw = conn.connection.driver_connection
    with raw.cursor() as cur:
        cur.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
            sql.Identifier(user), sql.Literal(password)))
    return True


def _ensure_database(conn, name: str, owner: str) -> bool:
    """Создаёт базу с указанным владельцем. Существующую не трогает."""
    exists = conn.execute(
        text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}).scalar()
    if exists:
        return False

    _check_identifier(owner)
    from psycopg import sql

    raw = conn.connection.driver_connection
    with raw.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
            sql.Identifier(name), sql.Identifier(owner)))
    return True


def ensure_database(url: str | None = None) -> bool:
    """Совместимость с прежним вызовом: True, если база была создана."""
    return bool(provision(url)["database_created"])


def create_all(engine: Engine | None = None) -> list[str]:
    """Создаёт недостающие таблицы. Существующие не трогает.

    Идемпотентно: повторный вызов ничего не сломает и данные не удалит.
    """
    engine = engine or get_engine()
    before = set(existing_tables(engine))
    Base.metadata.create_all(engine)
    after = set(existing_tables(engine))
    return sorted(after - before)


def clear_all(engine: Engine | None = None) -> dict[str, int]:
    """Опустошает таблицы, оставляя саму схему.

    Возвращает, сколько строк было удалено из каждой таблицы, — без
    этого невозможно отличить «очистили» от «нечего было чистить».
    Удаление идёт в обратном порядке зависимостей: иначе внешние ключи
    не дадут убрать строки, на которые ещё ссылаются.
    """
    engine = engine or get_engine()
    present = set(existing_tables(engine))
    removed: dict[str, int] = {}
    with engine.begin() as conn:
        for table in reversed(ALL_TABLES):
            if table.name not in present:
                continue
            n = conn.execute(select(func.count()).select_from(table)).scalar() or 0
            if n:
                conn.execute(table.delete())
            removed[table.name] = int(n)
    return removed


def drop_all(engine: Engine | None = None) -> list[str]:
    """Сносит таблицы вместе со схемой. Данные теряются безвозвратно."""
    engine = engine or get_engine()
    before = set(existing_tables(engine))
    Base.metadata.drop_all(engine)
    after = set(existing_tables(engine))
    return sorted(before - after)


# ---------------------------------------------------------------------------
# Проверка
# ---------------------------------------------------------------------------
def existing_tables(engine: Engine | None = None) -> list[str]:
    engine = engine or get_engine()
    return sorted(inspect(engine).get_table_names())


def missing_tables(engine: Engine | None = None) -> list[str]:
    present = set(existing_tables(engine))
    return [name for name in TABLE_NAMES if name not in present]


def is_ready(engine: Engine | None = None) -> bool:
    """Все ли таблицы схемы на месте."""
    try:
        return not missing_tables(engine)
    except (OperationalError, ProgrammingError):
        return False


def describe(engine: Engine | None = None) -> dict[str, Any]:
    """Состояние базы для человека и для теста.

    Проверять «подключение живо» недостаточно: база бывает пустой,
    неполной после прерванного создания или полной чужих таблиц.
    Поэтому возвращаем и список лишнего тоже.
    """
    engine = engine or get_engine()
    try:
        present = existing_tables(engine)
    except (OperationalError, ProgrammingError) as e:
        return {"connected": False,
                "error": str(getattr(e, "orig", e)).splitlines()[0][:300],
                "hint": diagnose(str(engine.url), e),
                "url": _safe_url(engine), "tables": {}, "missing": list(TABLE_NAMES),
                "unexpected": [], "ready": False}

    counts: dict[str, int] = {}
    with engine.connect() as conn:
        for table in ALL_TABLES:
            if table.name in present:
                counts[table.name] = int(
                    conn.execute(select(func.count()).select_from(table)).scalar() or 0)

    missing = [n for n in TABLE_NAMES if n not in present]
    return {
        "connected": True,
        "url": _safe_url(engine),
        "tables": counts,
        "missing": missing,
        "unexpected": [n for n in present if n not in TABLE_NAMES],
        "ready": not missing,
    }


def _safe_url(engine: Engine) -> str:
    """Строка подключения без пароля — её печатают в консоль и в логи."""
    return engine.url.render_as_string(hide_password=True)



def port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    """Отвечает ли вообще что-нибудь на этом адресе и порту.

    Нужно для разбора причины отказа. Текст ошибки от Postgres на
    русской Windows приходит в кодировке системы, а не UTF-8, и до
    приложения доезжает нечитаемым — по нему причину не определить.
    Проверка сокетом от кодировок не зависит: если порт открыт,
    сервер работает и дело в учётных данных или в самой базе; если
    закрыт — сервер не запущен или слушает другой порт.
    """
    import socket

    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


# Коды состояния SQL. Они одинаковы на любом языке интерфейса, поэтому
# разбирать причину надо по ним, а не по тексту сообщения: текст от
# PostgreSQL на русской Windows приходит в кодировке системы и доезжает
# нечитаемым. Коды описаны в приложении A документации PostgreSQL.
SQLSTATE_NO_DATABASE = "3D000"      # invalid_catalog_name — базы нет
SQLSTATE_BAD_PASSWORD = "28P01"     # invalid_password
SQLSTATE_NO_AUTH = "28000"          # invalid_authorization_specification
SQLSTATE_TOO_MANY = "53300"         # too_many_connections
SQLSTATE_NO_PRIVILEGE = "42501"     # insufficient_privilege — прав не хватает

ENCODING_NOTE = [
    "",
    "Если сообщение сервера ниже выглядит набором знаков вопроса — это не",
    "поломка: PostgreSQL присылает текст в кодировке системы, и согласовать",
    "её с клиентом до подключения ещё нечем.",
]


def _sqlstate(error: Exception | None) -> str | None:
    """Код состояния из исключения драйвера, если он там есть."""
    for obj in (getattr(error, "orig", None), error):
        code = getattr(obj, "sqlstate", None) or getattr(obj, "pgcode", None)
        if code:
            return str(code)
    return None


def diagnose(url: str, error: Exception | None = None) -> list[str]:
    """Что проверить, если подключиться не удалось. Человеческим языком.

    Причина определяется двумя способами, и оба не зависят от языка и
    кодировки сообщений: код состояния SQL из исключения драйвера и
    проверка сокета. Разбор по тексту ошибки здесь не работает — на
    русской Windows текст доезжает нечитаемым.
    """
    try:
        u = make_url(url)
    except Exception:  # noqa: BLE001
        return ["Не удалось разобрать строку подключения."]

    if u.get_backend_name() == "sqlite":
        return ["Файл базы недоступен — проверьте права на папку."]

    host, port = u.host or "127.0.0.1", u.port or 5432
    user = u.username or "(не задан)"
    dbname = u.database or "(не задана)"
    code = _sqlstate(error)

    if code == SQLSTATE_NO_DATABASE:
        return [
            f"Сервер на {host}:{port} отвечает, пароль подошёл, но базы «{dbname}» нет.",
            "• Создайте её: python -m backend.app.db.models --create",
            "• Имя базы задаётся переменной FC_DB_NAME.",
        ]

    if code in (SQLSTATE_BAD_PASSWORD, SQLSTATE_NO_AUTH):
        return [
            f"Сервер на {host}:{port} отвечает, но пароль пользователя «{user}» не подошёл.",
            "• Пароль задаётся в .env строкой FC_DB_PASSWORD.",
            f"• Проверить отдельно: psql -h {host} -p {port} -U {user} -d postgres",
        ]

    if code == SQLSTATE_NO_PRIVILEGE:
        return [
            f"Пользователю «{user}» не хватает прав на эту операцию.",
            "• Создание базы и роли требует административной записи:",
            "  FC_DB_ADMIN_USER и FC_DB_ADMIN_PASSWORD в .env.",
            "• По умолчанию это суперпользователь postgres.",
        ]

    if code == SQLSTATE_TOO_MANY:
        return [f"Сервер на {host}:{port} отказал: исчерпан лимит подключений."]

    if not port_open(host, port):
        return [
            f"Сервер на {host}:{port} не отвечает.",
            "• Запущена ли служба PostgreSQL? В Windows: services.msc → postgresql-x64-…",
            "• Тот ли порт? Стандартный — 5432, задаётся переменной FC_DB_PORT.",
        ]

    # Порт открыт, а кода состояния нет — до внятного ответа сервера
    # дело не дошло. Чаще всего это всё-таки учётные данные.
    return [
        f"Сервер на {host}:{port} отвечает, но подключиться под «{user}» не удалось.",
        "• Чаще всего это неверный пароль — строка FC_DB_PASSWORD в .env.",
        f"• Проверить отдельно: psql -h {host} -p {port} -U {user} -d postgres",
        f"• База «{dbname}» может ещё не существовать — создаёт команда --create.",
    ] + ENCODING_NOTE


def _check_identifier(name: str) -> None:
    ok = name and all(ch.isalnum() or ch in "_-" for ch in name)
    if not ok:
        raise RuntimeError(f"недопустимое имя базы: {name!r}")


SessionLocal = sessionmaker(autoflush=False, expire_on_commit=False)


# ===========================================================================
# Командная строка
# ===========================================================================
def _print_state(state: dict[str, Any]) -> None:
    print(f"База:      {state['url']}")
    if not state["connected"]:
        print("Состояние: НЕТ ПОДКЛЮЧЕНИЯ")
        for line in state.get("hint", []):
            print(line)
        if state.get("error"):
            print(f"\nСообщение сервера: {state['error']}")
        return
    print(f"Состояние: {'готова' if state['ready'] else 'НЕПОЛНАЯ'}")
    if state["missing"]:
        print(f"Не хватает таблиц ({len(state['missing'])}): {', '.join(state['missing'])}")
    if state["unexpected"]:
        print(f"Посторонние таблицы: {', '.join(state['unexpected'])}")
    if state["tables"]:
        width = max(len(n) for n in state["tables"])
        print("Строк в таблицах:")
        for name, n in sorted(state["tables"].items()):
            print(f"  {name.ljust(width)}  {n}")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.app.db.models",
        description="Создание, очистка и проверка базы FructCity.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--create", action="store_true", help="создать базу и недостающие таблицы")
    group.add_argument("--clear", action="store_true", help="удалить все строки, схему оставить")
    group.add_argument("--drop", action="store_true", help="снести таблицы вместе со схемой")
    group.add_argument("--check", action="store_true", help="показать состояние базы")
    parser.add_argument("--yes", action="store_true", help="не спрашивать подтверждения")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        engine = get_engine()
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 2

    if args.check:
        state = describe(engine)
        _print_state(state)
        # Ненулевой код возврата, чтобы проверку можно было ставить в
        # скрипт развёртывания и полагаться на её результат.
        return 0 if state["connected"] else 2

    # Дальше всё требует живого подключения. Ошибку показываем разбором
    # причины, а не трассировкой на сто строк: трассировка говорит, где
    # в SQLAlchemy это случилось, но не говорит, что делать.
    try:
        if args.create:
            settings = get_settings()
            try:
                report = provision()
            except (OperationalError, ProgrammingError) as e:
                # Отказ на этом шаге — это отказ АДМИНИСТРАТИВНОГО
                # подключения, а не пользовательского. Разбирать надо
                # его, иначе подсказка отправит проверять не тот пароль.
                print("Не удалось подключиться под административной записью: "
                      f"{mask_url(settings.admin_database_url)}", file=sys.stderr)
                for line in diagnose(settings.admin_database_url, e):
                    print(line.replace("FC_DB_PASSWORD", "FC_DB_ADMIN_PASSWORD")
                              .replace("FC_DB_USER", "FC_DB_ADMIN_USER"), file=sys.stderr)
                raw = str(getattr(e, "orig", e)).strip().splitlines()
                if raw:
                    print(f"\nСообщение сервера: {raw[0][:300]}", file=sys.stderr)
                return 2
            if report.get("role_created"):
                print(f"Роль «{report['app_user']}» создана.")
            if report.get("database_created"):
                print(f"База «{report['database']}» создана, владелец — "
                      f"{report['app_user']}.")
            if not report.get("role_created") and not report.get("database_created"):
                print("Роль и база уже существуют.")
            tables = create_all(engine)
            print(f"Создано таблиц: {len(tables)}"
                  + (f" — {', '.join(tables)}" if tables else ""))
            _print_state(describe(engine))
            return 0
        return _destructive(args, engine)
    except (OperationalError, ProgrammingError) as e:
        _print_failure(engine, e)
        return 2


def _print_failure(engine: Engine, error: Exception) -> None:
    print(f"Не удалось подключиться: {_safe_url(engine)}", file=sys.stderr)
    for line in diagnose(str(engine.url), error):
        print(line, file=sys.stderr)
    raw = str(getattr(error, "orig", error)).strip().splitlines()
    if raw:
        print(f"\nСообщение сервера: {raw[0][:300]}", file=sys.stderr)


def _destructive(args, engine: Engine) -> int:
    """Очистка и снос. Оба необратимы, поэтому спрашивают подтверждение.

    Флаг --yes оставлен для скриптов, но по умолчанию требуется явное
    согласие: перепутать рабочую базу с тестовой проще, чем кажется.
    """
    what = "ОЧИСТИТЬ" if args.clear else "СНЕСТИ"
    if not args.yes:
        print(f"Это действие необратимо: {what} данные в {_safe_url(engine)}")
        if input("Введите yes для подтверждения: ").strip().lower() != "yes":
            print("Отменено.")
            return 1

    if args.clear:
        removed = clear_all(engine)
        total = sum(removed.values())
        print(f"Удалено строк: {total}")
        for name, n in sorted(removed.items()):
            if n:
                print(f"  {name}: {n}")
    else:
        dropped = drop_all(engine)
        print(f"Снесено таблиц: {len(dropped)}" + (f" — {', '.join(dropped)}" if dropped else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
