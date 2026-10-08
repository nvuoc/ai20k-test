@echo off
setlocal
title Start project on Windows
pushd "%~dp0"
if errorlevel 1 exit /b 1

where uv >nul 2>&1
if errorlevel 1 (
    echo ERROR: uv was not found. Install uv and add it to PATH.
    goto :failed
)
where npm.cmd >nul 2>&1
if errorlevel 1 (
    echo ERROR: npm was not found. Install Node.js 22.12+ and add it to PATH.
    goto :failed
)

echo [1/6] Installing backend dependencies...
uv sync --project src/backend --extra dev --extra voice --locked --cache-dir .cache/uv
if errorlevel 1 goto :failed

echo [2/6] Initializing environment...
src\backend\.venv\Scripts\python.exe src/scripts/init_env.py
if errorlevel 1 goto :failed
src\backend\.venv\Scripts\python.exe src/scripts/init_voice_env.py
if errorlevel 1 goto :failed

echo [3/6] Installing frontend dependencies...
call npm.cmd --prefix src/frontend ci --cache .cache/npm
if errorlevel 1 goto :failed

echo [4/6] Building frontend...
call npm.cmd --prefix src/frontend run build
if errorlevel 1 goto :failed

echo [5/6] Seeding demo data...
src\backend\.venv\Scripts\python.exe src/scripts/seed_demo.py
if errorlevel 1 goto :failed

echo [6/6] Starting server at http://127.0.0.1:8000
echo Press Ctrl+C to stop the server.
src\backend\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir src/backend --host 127.0.0.1 --port 8000 --workers 1
if errorlevel 1 goto :failed

popd
endlocal
exit /b 0

:failed
echo.
echo ERROR: Setup or server startup failed. See the error above.
pause
popd
endlocal
exit /b 1
