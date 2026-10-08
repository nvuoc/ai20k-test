@echo off
setlocal
pushd "%~dp0"
uv sync --project src/backend --extra dev --extra voice --locked --cache-dir .cache/uv
if errorlevel 1 goto :failed
src\backend\.venv\Scripts\python.exe src/scripts/init_voice_env.py
if errorlevel 1 goto :failed
pushd src\backend
.venv\Scripts\python.exe -m livekit.agents download-files
if errorlevel 1 goto :failed_backend
.venv\Scripts\python.exe -m app.voice.agent dev
if errorlevel 1 goto :failed_backend
popd
popd
endlocal
exit /b 0
:failed_backend
popd
:failed
echo ERROR: Voice agent failed. Check LiveKit, Azure Speech and VOICE_API_URL configuration.
popd
endlocal
exit /b 1
