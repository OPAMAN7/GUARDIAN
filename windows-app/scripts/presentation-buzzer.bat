@echo off
:: One-shot presentation buzzer trigger for dashboard demo mode.

title Guardian Shield - Presentation Buzzer

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..\..") do set "ROOT_DIR=%%~fI"
set "SERVER_DIR=%ROOT_DIR%\parent-dashboard\server-ts"

cd /d "%SERVER_DIR%"
if not exist "node_modules" (
    call npm install
)
if not exist "node_modules\socket.io-client" (
    call npm install
)

call npm run emu:buzzer
