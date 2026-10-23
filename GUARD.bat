@echo off
setlocal
cd /d "%~dp0"

REM ============================================================
REM  GUARD launcher
REM   GUARD.bat              -> interactive menu
REM   GUARD.bat deploy       -> upload pi-agent\agent\* to the Pi, run it, verify
REM   GUARD.bat verify       -> check Pi service + Pi->dashboard reachability
REM   GUARD.bat recalibrate  -> ask for Pi IP; "auto" checks only the cached IP,
REM                             then re-point server_url and verify end-to-end
REM   GUARD.bat diag         -> Pi install/service state + agent log tail
REM   GUARD.bat progress     -> open the Academic Progress page (app/web/YouTube
REM                             grading; part of the standard install)
REM   GUARD.bat telegram     -> connect the Telegram bot that pushes device alerts
REM                             to your phone (add "test" to re-send a test message)
REM   GUARD.bat datapull     -> copy the Pi's progress spool, logs and service state
REM                             into data-exports\<date_time>\ over SSH
REM                             (TRANSFER-DATA.bat is the double-click version)
REM   GUARD.bat ip <addr>    -> set the server IP all agents connect to
REM                             (use "auto" to match the Pi's subnet)
REM   GUARD.bat pi-ip <addr> -> update GUARD's cached Pi connection target
REM
REM  The single source of truth for the IP lives in
REM  windows-app\config\guard_runtime.json and is pushed to the Pi
REM  and the local agent configs on every deploy/recalibrate.
REM ============================================================

where python >nul 2>&1
if errorlevel 1 (
    echo [X] Python was not found on PATH. Install Python 3 and retry.
    exit /b 1
)

python GUARD.py %*
set RC=%ERRORLEVEL%
endlocal & exit /b %RC%
