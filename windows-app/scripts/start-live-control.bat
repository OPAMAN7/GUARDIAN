@echo off
:: ============================================================
:: Guardian Shield - Live Controlled Mode (No Raspberry Pi)
:: Streams this PC's real screen to dashboard and applies
:: blocklist updates from web app on this machine.
:: ============================================================

title Guardian Shield - Live Controlled Mode
color 1A
setlocal EnableExtensions EnableDelayedExpansion

if /I "%~1"=="--elevated" shift

set "SCRIPT_FILE=%~f0"
for %%I in ("%~dp0..") do set "WINAPP_DIR=%%~fI"
for %%I in ("%WINAPP_DIR%\..") do set "ROOT_DIR=%%~fI"

set "SERVER_DIR=%ROOT_DIR%\parent-dashboard\server-ts"
set "DASHBOARD_DIR=%ROOT_DIR%\parent-dashboard\web-ts"
set "AGENT_DIR=%ROOT_DIR%\pi-agent\agent"
set "VENV_DIR=%WINAPP_DIR%\.live-agent-venv"
set "CONFIG_FILE=%WINAPP_DIR%\config\local-agent-config.json"

if not exist "%SERVER_DIR%\package.json" (
    echo [!] Could not resolve launcher directory.
    echo [i] Expected server package at: %SERVER_DIR%\package.json
    echo [i] Current directory: %CD%
    echo [i] Script file seen by batch: %SCRIPT_FILE%
    pause
    exit /b 1
)
set "APP_PORT=3000"
set "BLOCK_PAGE_PORT=80"
set "EDGE_EXE="
set "NO_ADMIN_MODE=0"
set "CLEAR_HOSTS_ON_START=true"

echo.
echo  ====================================================
echo   Guardian Shield - Live Controlled Mode
echo  ====================================================
echo.

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [i] Requesting Administrator privileges...
    powershell -NoProfile -Command "$ErrorActionPreference='Stop'; try { Start-Process -FilePath 'cmd.exe' -WorkingDirectory '%WINAPP_DIR%\scripts' -ArgumentList '/k','call ""%SCRIPT_FILE%"" --elevated' -Verb RunAs; exit 0 } catch { exit 1 }"
    if %errorlevel% neq 0 (
        echo [!] Elevation was cancelled or failed.
        echo [i] No UAC prompt appeared or it was denied.
        echo [i] Continuing in non-admin fallback mode.
        echo [i] Hosts-level blocking may be limited in this run.
        set "NO_ADMIN_MODE=1"
        set "BLOCK_PAGE_PORT=8080"
        set "CLEAR_HOSTS_ON_START=false"
    ) else (
        echo [i] Elevated launcher started in a new window.
        echo [i] You can close this window.
        timeout /t 2 /nobreak >nul
        exit /b 0
    )
)

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

if "%NO_ADMIN_MODE%"=="1" (
    echo [i] Running in non-admin mode (fallback).
) else (
    echo [i] Running as Administrator.
)

echo [i] Cleaning previous live-agent windows (if any)...
taskkill /FI "WINDOWTITLE eq Guardian Live Agent*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Guardian Shield Server*" /T /F >nul 2>&1
echo [i] Fast cleanup complete.
echo [i] If old hidden processes exist, they will be handled by port checks below.

echo [1/6] Ensuring Node dependencies...
cd /d "%SERVER_DIR%"
if %errorlevel% neq 0 (
    echo [!] Server directory not found: %SERVER_DIR%
    echo [i] Current directory: %CD%
    pause
    exit /b 1
)
if not exist "node_modules" (
    call npm install
)
if not exist "node_modules\socket.io-client" (
    call npm install
)
if %errorlevel% neq 0 (
    echo [!] Failed to install server dependencies.
    pause
    exit /b 1
)

echo [2/6] Preparing dashboard assets (Vite build is one-shot and exits)...
cd /d "%DASHBOARD_DIR%"
if %errorlevel% neq 0 (
    echo [!] Dashboard directory not found: %DASHBOARD_DIR%
    echo [i] Current directory: %CD%
    pause
    exit /b 1
)
if not exist "node_modules" (
    call npm install
)
if not exist "dist\index.html" (
    echo [i] No dashboard dist found. Running Vite build now...
    call npm run build
    if %errorlevel% neq 0 (
        echo [!] Dashboard build failed. Fix npm errors and retry.
        pause
        exit /b 1
    )
    echo [i] Dashboard build complete.
) else (
    echo [i] Using existing dashboard build at %DASHBOARD_DIR%\dist\index.html
    echo [i] (Set FORCE_DASHBOARD_BUILD=1 before launch if you need a fresh rebuild.)
    if /I "%FORCE_DASHBOARD_BUILD%"=="1" (
        echo [i] FORCE_DASHBOARD_BUILD=1 detected. Rebuilding dashboard now...
        call npm run build
        if %errorlevel% neq 0 (
            echo [!] Forced dashboard build failed. Fix npm errors and retry.
            pause
            exit /b 1
        )
    )
)

echo [3/6] Starting server...
set "APP_PORT_PID="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:"[\.:]%APP_PORT% .*LISTENING"') do (
    set "APP_PORT_PID=%%P"
    goto :app_port_checked
)
:app_port_checked
if defined APP_PORT_PID (
    if "!APP_PORT_PID!"=="4" (
        if "%APP_PORT%"=="3000" (
            echo [!] Port 3000 is reserved by Windows System (PID 4).
            echo [i] Switching dashboard/server port to 3100.
            set "APP_PORT=3100"
            set "APP_PORT_PID="
        )
    )
)

if not "%APP_PORT%"=="3000" (
    for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:"[\.:]%APP_PORT% .*LISTENING"') do (
        set "APP_PORT_PID=%%P"
        goto :app_port_rechecked
    )
)
:app_port_rechecked
if defined APP_PORT_PID (
    if "!APP_PORT_PID!"=="4" (
        echo [!] Port %APP_PORT% is also reserved by Windows System (PID 4).
        echo [!] Free port 3000/3100 and run again.
        pause
        exit /b 1
    ) else (
        echo [i] Releasing port %APP_PORT% from PID !APP_PORT_PID!...
        taskkill /PID !APP_PORT_PID! /F >nul 2>&1
        timeout /t 2 /nobreak >nul
    )
)

start "Guardian Shield Server" cmd /k "cd /d %SERVER_DIR% && set PORT=%APP_PORT% && node index.js"
timeout /t 5 /nobreak >nul

powershell -NoProfile -Command "try { $null = Invoke-RestMethod -Uri 'http://localhost:%APP_PORT%/api/health' -TimeoutSec 4; exit 0 } catch { exit 1 }"
if %errorlevel% neq 0 (
    echo [!] Server failed health check on port %APP_PORT%.
    echo [!] Opening diagnostic server window now.
    start "Guardian Server Diagnostic" cmd /k "cd /d %SERVER_DIR% && set PORT=%APP_PORT% && node index.js"
    echo [!] Startup stopped. Fix the server error shown in diagnostic window, then rerun.
    pause
    exit /b 1
)

echo [i] Checking port 80 availability for block warning page...
if "%NO_ADMIN_MODE%"=="1" (
    echo [i] Skipping privileged port 80 checks in non-admin mode.
    echo [i] Using fallback block page port 8080 + active-tab enforcement.
    goto :after_port80_checks
)
set "PORT80_PID="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:"[\.:]80 .*LISTENING"') do (
    set "PORT80_PID=%%P"
    goto :port80_checked
)
:port80_checked
if defined PORT80_PID (
    if "!PORT80_PID!"=="4" (
        echo [!] Port 80 is owned by Windows System (PID 4) and cannot be killed.
        echo [i] Switching block page server to fallback port 8080.
        set "BLOCK_PAGE_PORT=8080"
    ) else (
        echo [i] Port 80 is in use by PID !PORT80_PID!, attempting to free it...
        taskkill /PID !PORT80_PID! /F >nul 2>&1
        timeout /t 2 /nobreak >nul
        set "PORT80_PID="
        for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:"[\.:]80 .*LISTENING"') do (
            set "PORT80_PID=%%P"
            goto :port80_recheck
        )
    )
)

:port80_recheck
if defined PORT80_PID (
    if "!PORT80_PID!"=="4" (
        echo [i] Port 80 still reserved by Windows System (PID 4).
        echo [i] Keeping fallback port 8080 + active-tab blocking.
        set "BLOCK_PAGE_PORT=8080"
    ) else (
        echo [!] Port 80 is still busy (PID !PORT80_PID!).
        for /f "tokens=1,*" %%A in ('tasklist /FI "PID eq !PORT80_PID!" ^| findstr /R /V "^$"') do (
            if /I not "%%A"=="INFO:" echo [i] Port 80 owner: %%A
        )
        echo [!] Port 80 not available. Hosts-based redirect may be limited.
        echo [i] Agent will use active-tab fallback blocking in Edge.
        set "BLOCK_PAGE_PORT=8080"
    )
)
if not defined PORT80_PID (
    echo [i] Port 80 is available for the local NUH UH warning page.
)
echo [i] Verifying bind permissions on port !BLOCK_PAGE_PORT!...
powershell -NoProfile -Command "$ok=$true; try { $l=[System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Any,%BLOCK_PAGE_PORT%); $l.Start(); $l.Stop() } catch { $ok=$false }; if(-not $ok){ exit 1 }"
if %errorlevel% neq 0 (
    echo [!] Cannot bind to port !BLOCK_PAGE_PORT! due permission/ACL restrictions.
    echo [i] Continuing with active-tab fallback blocking.
)

:after_port80_checks

echo [i] Checking port 443 availability for HTTPS NUH UH page...
if "%NO_ADMIN_MODE%"=="1" (
    echo [i] Skipping privileged port 443 checks in non-admin mode.
    goto :after_port443_checks
)
set "PORT443_PID="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:"[\.:]443 .*LISTENING"') do (
    set "PORT443_PID=%%P"
    goto :port443_checked
)
:port443_checked
if defined PORT443_PID (
    if "!PORT443_PID!"=="4" (
        echo [i] Port 443 is owned by Windows System (PID 4), skipping kill attempt.
    ) else (
        echo [i] Port 443 is in use by PID !PORT443_PID!, attempting to free it...
        taskkill /PID !PORT443_PID! /F >nul 2>&1
        timeout /t 2 /nobreak >nul
        set "PORT443_PID="
        for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:"[\.:]443 .*LISTENING"') do (
            set "PORT443_PID=%%P"
            goto :port443_recheck
        )
    )
)

:port443_recheck
if defined PORT443_PID (
    echo [!] Port 443 is still busy (PID !PORT443_PID!).
    for /f "tokens=1,*" %%A in ('tasklist /FI "PID eq !PORT443_PID!" ^| findstr /R /V "^$"') do (
        if /I not "%%A"=="INFO:" echo [i] Port 443 owner: %%A
    )
    echo [!] Port 443 not available. HTTPS local interception may be limited.
    echo [i] Continuing with HTTP/local active-tab fallback.
)
if not defined PORT443_PID (
    echo [i] Port 443 is available for HTTPS NUH UH interception.
)
echo [i] Verifying bind permissions on port 443...
powershell -NoProfile -Command "$ok=$true; try { $l=[System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Any,443); $l.Start(); $l.Stop() } catch { $ok=$false }; if(-not $ok){ exit 1 }"
if %errorlevel% neq 0 (
    echo [!] Cannot bind to port 443 due permission/ACL restrictions.
    echo [i] Continuing with fallback enforcement.
)

:after_port443_checks

echo [4/6] Preparing Python environment for local agent...
if not exist "%VENV_DIR%\Scripts\python.exe" (
    python -m venv "%VENV_DIR%"
    "%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip
    "%VENV_DIR%\Scripts\python.exe" -m pip install -r "%AGENT_DIR%\requirements.txt"
)
if %errorlevel% neq 0 (
    echo [!] Python environment setup failed.
    pause
    exit /b 1
)

echo [5/6] Building local agent config...
if exist "%CONFIG_FILE%" del /q "%CONFIG_FILE%" >nul 2>&1
set "AGENT_TOKEN="
if exist "%SERVER_DIR%\.env" (
    for /f "usebackq tokens=1,* delims==" %%A in ("%SERVER_DIR%\.env") do (
        if /i "%%A"=="AGENT_SHARED_SECRET" set "AGENT_TOKEN=%%B"
    )
)

set "DOWNLOAD_PATH=%USERPROFILE%\Downloads"
set "QUARANTINE_PATH=%USERPROFILE%\.guardian-shield\quarantine"
set "DOWNLOAD_JSON=!DOWNLOAD_PATH:\=\\!"
set "QUARANTINE_JSON=!QUARANTINE_PATH:\=\\!"

(
echo {
echo   "server_url": "http://localhost:%APP_PORT%",
echo   "device_name": "EDGE-LIVE-CONTROLLED",
echo   "screenshot_interval": 0.05,
echo   "target_fps": 20,
echo   "screenshot_quality": 50,
echo   "max_width": 1280,
echo   "capture_mode": "edge_window",
echo   "window_title_contains": "Microsoft Edge",
echo   "window_title_excludes": ["Guardian Shield"],
echo   "auto_launch_edge": true,
echo   "edge_dashboard_url": "http://localhost:%APP_PORT%",
echo   "edge_start_url": "https://www.youtube.com",
echo   "safe_browse_url": "http://localhost:%APP_PORT%",
echo   "low_latency_mode": true,
echo   "enable_local_buzzer": true,
echo   "enable_block_page_server": true,
echo   "block_page_port": !BLOCK_PAGE_PORT!,
echo   "use_online_blocklist": false,
echo   "clear_guardian_hosts_on_start": !CLEAR_HOSTS_ON_START!,
echo   "enable_download_guard": false,
echo   "auth_token": "!AGENT_TOKEN!",
echo   "blocklist_update_interval": 3600,
echo   "download_watch_paths": ["!DOWNLOAD_JSON!"],
echo   "quarantine_path": "!QUARANTINE_JSON!",
echo   "log_level": "INFO",
echo   "stealth_mode": false,
echo   "platform": "windows-live",
echo   "use_scrot_fallback": false
echo }
) > "%CONFIG_FILE%"

if not exist "%CONFIG_FILE%" (
    echo [!] Failed to create local agent config at %CONFIG_FILE%
    pause
    exit /b 1
)

echo [6/6] Starting local monitored agent...
start "Guardian Live Agent" cmd /k ""%VENV_DIR%\Scripts\python.exe" "%AGENT_DIR%\main.py" --no-watchdog --config "%CONFIG_FILE%""
timeout /t 3 /nobreak >nul

tasklist /FI "WINDOWTITLE eq Guardian Live Agent*" | find /I "cmd.exe" >nul
if %errorlevel% neq 0 (
    echo [!] Local agent did not start correctly.
    echo [!] Opening diagnostic agent window now.
    start "Guardian Agent Diagnostic" cmd /k ""%VENV_DIR%\Scripts\python.exe" "%AGENT_DIR%\main.py" --no-watchdog --config "%CONFIG_FILE%""
    echo [!] Startup stopped. Fix the agent error shown in diagnostic window, then rerun.
    pause
    exit /b 1
)

echo Opening Microsoft Edge dashboard + tracked browsing window...
for /f "tokens=2,*" %%A in ('reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe" /ve 2^>nul ^| findstr /R /C:"REG_SZ"') do (
    set "EDGE_EXE=%%B"
)
if not defined EDGE_EXE (
    where msedge >nul 2>&1
    if %errorlevel%==0 set "EDGE_EXE=msedge"
)

if defined EDGE_EXE (
    start "" "%EDGE_EXE%" --new-window --ignore-certificate-errors --allow-running-insecure-content "http://localhost:%APP_PORT%"
    timeout /t 1 /nobreak >nul
    start "" "%EDGE_EXE%" --new-window --ignore-certificate-errors --allow-running-insecure-content "https://www.youtube.com"
) else (
    start "" "microsoft-edge:http://localhost:%APP_PORT%"
    timeout /t 1 /nobreak >nul
    start "" "microsoft-edge:https://www.youtube.com"
)

echo.
echo Live controlled mode is active.
echo - Dashboard opens in one Edge window.
echo - A second Edge window opens for browsing and is the tracked target.
echo - Open dashboard Live View to watch that Edge window in real time.
echo - Browse any blocked domain in the browsing window to trigger buzzer + NUH UH page.
echo - Add a domain in Blocklist page to block it on this machine.
echo.
echo To stop: close "Guardian Live Agent" and "Guardian Shield Server" windows.
echo.
pause
