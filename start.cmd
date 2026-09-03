@echo off
rem ---------------------------------------------------------------
rem  FructCity launcher (FastAPI / uvicorn).
rem  ASCII ONLY - do not put Cyrillic text in this file.
rem  cmd.exe parses .bat/.cmd using the OEM codepage (866 on Russian
rem  Windows) before chcp takes effect, so UTF-8 text here breaks into
rem  garbage lines that cmd then tries to execute as commands.
rem ---------------------------------------------------------------
chcp 65001 >nul 2>&1
title FructCity
cd /d "%~dp0"

rem Prefer the project virtualenv: it has fastapi, uvicorn and psycopg.
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" -c "import uvicorn" >nul 2>nul
if errorlevel 1 goto :nodeps

echo.
echo   FructCity: http://127.0.0.1:8000
echo   Admin:     http://127.0.0.1:8000/admin
echo   Health:    http://127.0.0.1:8000/healthz
echo.
echo   Press Ctrl+C to stop.
echo.

"%PY%" -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
goto :done

:nodeps
echo.
echo   Dependencies are missing / zavisimosti ne ustanovleny.
echo.
echo   Create the environment and install them:
echo     python -m venv .venv
echo     .venv\Scripts\python.exe -m pip install -r backend\requirements.txt
echo.

:done
echo.
pause
