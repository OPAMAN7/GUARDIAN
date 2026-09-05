@echo off
setlocal
cd /d "%~dp0"

REM ============================================================
REM  TRANSFER-DATA.bat  --  pull the monitored device's records
REM                        off the Raspberry Pi over SSH
REM
REM  What it copies into GUARDIAN\data-exports\<date_time>\:
REM    progress\      app + web + YouTube records the agent has
REM                   spooled but not yet delivered to the dashboard
REM    logs\          the agent's own log files
REM    agent\         config.json, warning counts, kill attempts
REM    service\       systemd state + the last 2000 journal lines
REM    MANIFEST.txt   every file with its size
REM
REM  Read-only on the Pi. The agent is never stopped, restarted or
REM  killed by this script, so it is safe to run while the child is
REM  using the device. The only thing it writes there is one
REM  temporary archive under /tmp, which it deletes afterwards.
REM
REM  Each run makes its own timestamped folder, so nothing is ever
REM  overwritten and two pulls can be compared.
REM
REM  Same command from the launcher: GUARD.bat datapull
REM  Or the interactive menu: GUARD.bat -> option P
REM
REM  Exit codes:  0 = data landed    1 = nothing pulled
REM ============================================================

where python >nul 2>&1
if errorlevel 1 (
    echo [X] Python was not found on PATH. Install Python 3 and retry.
    exit /b 1
)

echo.
echo  GUARDIAN :: SSH DATA TRANSFER
echo  -----------------------------------------------------------
echo  Pulling progress records, logs and service state from the Pi.
echo.

python GUARD.py datapull %*
set RC=%ERRORLEVEL%

echo.
if "%RC%"=="0" (
    echo  Done. The folder above holds this run's copy.
) else (
    echo  [X] Nothing was pulled. Check that the Pi is on the same
    echo      Wi-Fi, then run: GUARD.bat verify
)
echo.

REM Only pause when double-clicked from Explorer; a scheduled task
REM or a parent script must not hang waiting for a keypress.
if "%~1"=="" (
    if not defined GUARDIAN_NO_PAUSE pause
)

endlocal & exit /b %RC%
