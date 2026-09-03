#!/usr/bin/env bash
# Точка входа контейнера приложения.
#
# Три шага по порядку: дождаться, что Postgres принимает соединения —
# при первом `docker compose up` контейнер приложения обычно стартует
# раньше, чем база готова; поднять схему; запустить сервер.
set -euo pipefail
cd /app

echo "[entrypoint] жду Postgres (${FC_DB_HOST:-127.0.0.1}:${FC_DB_PORT:-5432})..."
python - <<'PY'
import os
import socket
import sys
import time

host = os.environ.get("FC_DB_HOST", "127.0.0.1")
port = int(os.environ.get("FC_DB_PORT", "5432"))

for attempt in range(60):
    try:
        with socket.create_connection((host, port), timeout=2):
            sys.exit(0)
    except OSError:
        time.sleep(1)
print(f"Postgres на {host}:{port} не ответил за 60 секунд", file=sys.stderr)
sys.exit(1)
PY

# --create идемпотентен («существующее не трогает» — models.py) —
# безопасно гонять при каждом старте контейнера, а не только на
# первом развёртывании.
#
# Не alembic upgrade head: CI (.github/workflows/tests.yml) сам пока
# поднимает схему через --create, а не через ревизии — Docker не
# должен разойтись с тем, что реально проверяется. Как только CI
# переключится на alembic, поменять и здесь (ROADMAP.md, этап 3).
echo "[entrypoint] проверяю схему базы..."
python -m backend.app.db.models --create

echo "[entrypoint] запускаю uvicorn..."
exec python -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
