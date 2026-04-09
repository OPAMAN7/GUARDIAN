@echo off
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "WINAPP_DIR=%%~fI"
set "SRC=%WINAPP_DIR%\launcher\GuardianOneClick.cs"
set "OUT=%WINAPP_DIR%\launcher\Guardian-OneClick.exe"

if not exist "%SRC%" (
  echo [!] Source file not found: %SRC%
  exit /b 1
)

set "CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not exist "%CSC%" set "CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe"

if not exist "%CSC%" (
  echo [!] C# compiler not found.
  echo [i] Install .NET Framework build tools or compile with Visual Studio.
  exit /b 1
)

echo [i] Compiling one-click launcher...
"%CSC%" /nologo /target:winexe /out:"%OUT%" /reference:System.Windows.Forms.dll /reference:System.Drawing.dll "%SRC%"
if errorlevel 1 (
  echo [!] Build failed.
  exit /b 1
)

echo [OK] Built: %OUT%
exit /b 0
