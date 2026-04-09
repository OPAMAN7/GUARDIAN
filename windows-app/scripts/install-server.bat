@echo off
:: ============================================================
:: 🛡️ Guardian Shield — Windows Parent Server Installer
:: Run this ONCE to set up the server on your Windows PC
:: Double-click to run — no need for admin (unless firewall prompt)
:: ============================================================

title Guardian Shield — Server Setup
color 1F

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..\..") do set "ROOT_DIR=%%~fI"
set "SERVER_DIR=%ROOT_DIR%\parent-dashboard\server-ts"
set "DASHBOARD_DIR=%ROOT_DIR%\parent-dashboard\web-ts"

echo.
echo  ====================================================
echo   🛡️  Guardian Shield — Windows Parent Server Setup
echo  ====================================================
echo.

:: ── Check Node.js ────────────────────────────────────────────
echo [→] Checking Node.js...
node --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Node.js not found!
    echo     Please download and install Node.js from:
    echo     https://nodejs.org/  (LTS version)
    echo.
    echo     Then re-run this installer.
    pause
    exit /b 1
)
for /f "tokens=*" %%i in ('node --version') do set NODE_VER=%%i
echo [✓] Node.js %NODE_VER% found

:: ── Install server dependencies ──────────────────────────────
echo.
echo [→] Installing server dependencies (npm install)...
cd /d "%SERVER_DIR%"
call npm install
if %errorlevel% neq 0 (
    echo [✗] npm install failed! Check your internet connection.
    pause
    exit /b 1
)
echo [✓] Server dependencies installed

:: ── Install dashboard dependencies ───────────────────────────
echo.
echo [→] Installing dashboard dependencies...
cd /d "%DASHBOARD_DIR%"
call npm install
if %errorlevel% neq 0 (
    echo [✗] Dashboard npm install failed!
    pause
    exit /b 1
)
echo [✓] Dashboard dependencies installed

:: ── Get Windows IP ───────────────────────────────────────────
echo.
echo [→] Your Windows PC IP address (share this with the Pi):
ipconfig | findstr /i "IPv4"

:: ── Generate secure secrets ───────────────────────────────────
for /f %%i in ('powershell -NoProfile -Command "[guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')"') do set JWT_SECRET=%%i
for /f %%i in ('powershell -NoProfile -Command "[guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')"') do set AGENT_SHARED_SECRET=%%i

:: ── Create .env file ─────────────────────────────────────────
cd /d "%SERVER_DIR%"
if not exist ".env" (
    echo PORT=3000> .env
    echo NODE_ENV=production>> .env
    echo JWT_SECRET=%JWT_SECRET%>> .env
    echo ADMIN_USERNAME=admin>> .env
    echo ADMIN_PASSWORD=admin123>> .env
    echo AGENT_SHARED_SECRET=%AGENT_SHARED_SECRET%>> .env
    echo [✓] Server config (.env) created
)

findstr /b /c:"AGENT_SHARED_SECRET=" ".env" >nul
if %errorlevel% neq 0 (
    echo AGENT_SHARED_SECRET=%AGENT_SHARED_SECRET%>> .env
    echo [✓] Added AGENT_SHARED_SECRET to existing .env
)

:: ── Add firewall rule ─────────────────────────────────────────
echo.
echo [→] Adding firewall rule for port 3000...
netsh advfirewall firewall add rule name="Guardian Shield Server" dir=in action=allow protocol=TCP localport=3000 >nul 2>&1
echo [✓] Firewall rule added (port 3000 open on LAN)

echo.
echo  ====================================================
echo   ✅  Setup complete!
echo  ====================================================
echo.
echo   Run "start-server.bat" to launch the dashboard.
echo.
echo   IMPORTANT: Note your Windows IP above.
echo   You'll need it when setting up the Pi agent.
echo.
pause
