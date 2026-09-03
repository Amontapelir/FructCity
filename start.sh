#!/usr/bin/env bash
# Запуск FructCity на macOS и Linux.  Права: chmod +x start.sh
set -e
cd "$(dirname "$0")"

echo
echo "  FructCity — запуск магазина"
echo "  ============================"
echo

# Виртуальное окружение проекта предпочтительнее системного Python:
# в нём стоят fastapi, uvicorn и psycopg.
PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"

if ! "$PY" -c "import uvicorn" >/dev/null 2>&1; then
  echo "  [!] Не установлены зависимости."
  echo "      python3 -m venv .venv && .venv/bin/python -m pip install -r backend/requirements.txt"
  exit 1
fi

PORT="${PORT:-8000}"

echo "  Витрина:  http://127.0.0.1:${PORT}"
echo "  Админка:  http://127.0.0.1:${PORT}/admin"
echo "  Здоровье: http://127.0.0.1:${PORT}/healthz"
echo
echo "  Остановить: Ctrl+C"
echo

# открываем браузер, когда сервер поднимется
( sleep 1.5
  if command -v open >/dev/null 2>&1; then open "http://127.0.0.1:${PORT}"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "http://127.0.0.1:${PORT}"
  fi ) >/dev/null 2>&1 &

exec "$PY" -m uvicorn backend.app.main:app --host 127.0.0.1 --port "${PORT}"
