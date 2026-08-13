@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM Clear session env overrides
set APP_PASSWORD=
set SECRET_KEY=
set MAIL_PASSWORD=
set MAIL_USER=
set AI_API_KEY=

REM Kill previous zakupki servers on 3001 (hung polls leave zombies)
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":3001" ^| findstr LISTENING') do (
  echo Stopping old process %%p on port 3001...
  taskkill /F /PID %%p >nul 2>&1
)
timeout /t 1 /nobreak >nul

if not exist ".venv\Scripts\python.exe" (
  echo Creating .venv...
  python -m venv .venv
  if errorlevel 1 (
    echo Python not found.
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -c "import urllib.request; urllib.request.getproxies=lambda: {}; from pip._internal.cli.main import main; raise SystemExit(main(['install','-r','requirements.txt']))"
)
if not exist ".env" (
  copy /Y ".env.example" ".env" >nul
  echo Created .env — fill MAIL_* and AI_API_KEY.
)

set DATA_DIR=./data
set PORT=3001
echo.
echo Starting http://127.0.0.1:%PORT%
echo Stop: Ctrl+C
echo.
".venv\Scripts\uvicorn.exe" app.main:app --host 127.0.0.1 --port %PORT%
pause
