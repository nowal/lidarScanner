@echo off
rem Double-click to run the Home Guide demo: local backend on 8010, the
rem demo pages on 8020, and the phone view in the default browser.
setlocal
set HERE=%~dp0
set BACKEND=%HERE%..\..
set PY=%BACKEND%\.venv\Scripts\python.exe
if not exist "%PY%" (
  echo No virtualenv at %PY%. From backend\: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
  pause
  exit /b 1
)
start "Home Guide backend (8010)" "%PY%" "%HERE%run_local_backend.py" --port 8010
start "Home Guide demo pages (8020)" "%PY%" "%HERE%serve.py" --port 8020
timeout /t 6 /nobreak >nul
start "" "http://127.0.0.1:8020/phone.html?home=quintin-house"
echo Backend and demo pages are running in their own windows. Close them to stop.
endlocal
