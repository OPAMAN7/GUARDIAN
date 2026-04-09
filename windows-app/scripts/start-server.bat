@echo off
:: ============================================================
:: 🛡️ Guardian Shield — Start Server (with Kill Protection)
:: Double-click this every time you want to run the dashboard.
:: The watchdog monitors the server — if anyone kills it via
:: Task Manager, it plays a LOUD alarm and opens the NUH UH
:: screen, then restarts the server automatically.
:: ============================================================

title Guardian Shield — Server Running [PROTECTED]
color 2F

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..\..") do set "ROOT_DIR=%%~fI"
set "SERVER_DIR=%ROOT_DIR%\parent-dashboard\server-ts"
set "WATCHDOG_PY=%ROOT_DIR%\windows-app\watchdog\watchdog_server.py"

echo.
echo  ====================================================
echo   🛡️  Guardian Shield Server — Starting...
echo   🔒  Kill Protection: ACTIVE
echo  ====================================================
echo.

:: Check Python is available
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Python not found! Please install Python 3.10+ from python.org
    pause
    exit /b 1
)

:: Check Node.js is available
node --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Node.js not found! Run install-server.bat first.
    pause
    exit /b 1
)

:: Get Windows IP
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /i "IPv4" ^| findstr /v "127.0.0.1"') do (
    for /f "tokens=1" %%b in ("%%a") do set WIN_IP=%%b
)

:: Install server deps if missing
cd /d "%SERVER_DIR%"
if not exist "node_modules" (
    echo [→] Installing server dependencies...
    call npm install
)

echo [✓] Starting Guardian Shield with kill-protection watchdog...
echo.
echo  ┌─────────────────────────────────────────────────────┐
echo  │                                                     │
echo  │  Dashboard URL : http://%WIN_IP%:3000              │
echo  │  Local URL     : http://localhost:3000             │
echo  │  Login         : admin / admin123                  │
echo  │                                                     │
echo  │  🔒 Kill Protection ON — closing this process      │
echo  │     will trigger the ALARM + NUH UH screen!        │
echo  │                                                     │
echo  └─────────────────────────────────────────────────────┘
echo.

:: Open browser after short delay
timeout /t 3 /nobreak >nul
start http://localhost:3000

:: Run through watchdog (not node directly)
cd /d "%ROOT_DIR%"
python "%WATCHDOG_PY%"

echo.
echo [!] Server watchdog stopped.
pause

