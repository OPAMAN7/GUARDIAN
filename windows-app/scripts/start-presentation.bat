@echo off
:: ============================================================
:: Guardian Shield - Presentation Mode Launcher
:: Starts Windows server watchdog + Raspberry Pi emulator,
:: then opens dashboard with presentation skin enabled.
:: ============================================================

title Guardian Shield - Presentation Mode
color 1E

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..\..") do set "ROOT_DIR=%%~fI"
set "SERVER_DIR=%ROOT_DIR%\parent-dashboard\server-ts"
set "DASHBOARD_DIR=%ROOT_DIR%\parent-dashboard\web-ts"
set "WATCHDOG_PY=%ROOT_DIR%\windows-app\watchdog\watchdog_server.py"

echo.
echo  ====================================================
echo   Guardian Shield - Presentation Mode
echo  ====================================================
echo.

node --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Node.js not found. Install Node.js LTS first.
    pause
    exit /b 1
)

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Python not found. Install Python 3.10+ first.
    pause
    exit /b 1
)

echo [1/4] Installing server dependencies if needed...
cd /d "%SERVER_DIR%"
if not exist "node_modules" (
    call npm install
)
if not exist "node_modules\socket.io-client" (
    call npm install
)

echo [2/5] Building dashboard assets...
cd /d "%DASHBOARD_DIR%"
if not exist "node_modules" (
    call npm install
)
call npm run build

echo [3/5] Starting protected server in a new window...
start "Guardian Shield Server" cmd /c "cd /d %ROOT_DIR% && python %WATCHDOG_PY%"

echo [4/5] Waiting for server startup...
timeout /t 5 /nobreak >nul

echo [5/5] Starting Raspberry Pi emulator stream...
start "Guardian Pi Emulator" cmd /c "cd /d %SERVER_DIR% && npm run emu:pi"

echo.
echo Opening Microsoft Edge dashboard...
start "" msedge "http://localhost:3000/?presentation=1"
if %errorlevel% neq 0 (
    start "" "http://localhost:3000/?presentation=1"
)

echo.
echo Presentation mode is running.
echo - Server window: Guardian Shield Server
echo - Emulator window: Guardian Pi Emulator
echo.
echo To trigger buzzer manually, run: windows-app\scripts\presentation-buzzer.bat
echo To stop presentation mode, close both spawned windows.
echo.
pause
