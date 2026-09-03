# Образ приложения FructCity — FastAPI-бэкенд + статика витрины/админки.
#
# Версия Python — 3.13, не 3.14: requirements.txt целится в 3.14, но
# единственная версия, которую реально прогоняет CI
# (.github/workflows/tests.yml), — 3.13. Берём то, что доказанно
# работает целиком, а не то, что стоит на машине разработчика.
FROM python:3.13-slim

# PYTHONUNBUFFERED — иначе лог uvicorn оседает в буфере stdout и не
# доходит до `docker logs` до перезапуска контейнера.
# PIP_NO_CACHE_DIR — кэш pip внутри слоя образа только раздувает его,
# повторной установки в этом же контейнере не будет.
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Отдельным слоем — зависимости меняются реже кода, повторная сборка
# после правки backend/app не будет качать пакеты заново.
#
# requirements.lock.txt, а не requirements.txt: в нём зафиксированы
# версии, requirements.txt — только нижние границы (см. комментарий в
# самом файле).
COPY backend/requirements.lock.txt backend/requirements.lock.txt
RUN pip install --no-cache-dir -r backend/requirements.lock.txt

# Код приложения и то, что оно отдаёт браузеру напрямую.
COPY backend/app backend/app
COPY lib lib
COPY public public
COPY alembic.ini alembic.ini
COPY docker/entrypoint.sh docker/entrypoint.sh

# Непривилегированный пользователь: процесс приложения не должен уметь
# писать за пределы того, что ему нужно, даже если найдётся дыра в коде.
RUN useradd --create-home --uid 1000 fructcity \
    && chmod +x docker/entrypoint.sh \
    && chown -R fructcity:fructcity /app
USER fructcity

EXPOSE 8000

# /healthz не требует базы для самого ответа (см. main.py) — годится
# и для докеровского HEALTHCHECK, и для healthcheck в docker-compose.
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=2)" || exit 1

ENTRYPOINT ["docker/entrypoint.sh"]
