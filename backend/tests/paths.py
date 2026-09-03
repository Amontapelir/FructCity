"""Общие пути для тестов.

Отдельный модуль без зависимостей: из корня проекта читаются
`data/store.json`, `public/` и `lib/calc.js`, и когда-то `ROOT` жил в
одном из тестовых файлов — его удаление задело половину набора.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["ROOT", "STORE", "PUBLIC", "LIB"]

# backend/tests/paths.py → backend/tests → backend → корень
ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "data" / "store.json"
PUBLIC = ROOT / "public"
LIB = ROOT / "lib"
