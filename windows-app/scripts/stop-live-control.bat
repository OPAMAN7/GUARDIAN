@echo off
:: Stops windows spawned by start-live-control.bat

title Guardian Shield - Stop Live Controlled Mode

echo Stopping live controlled mode windows...

taskkill /FI "WINDOWTITLE eq Guardian Live Agent*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Guardian Shield Server*" /T /F >nul 2>&1
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*pi-agent\\agent\\main.py*' -or $_.CommandLine -like '*windows-app\\watchdog\\watchdog_server.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | Where-Object { $_.CommandLine -like '*parent-dashboard\\server-ts\\index.js*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1

echo Done. If any process is still open, close its window manually.
pause
