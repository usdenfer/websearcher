@echo off
setlocal
cd /d "%~dp0"
set "POWERSHELL_EXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"

if not exist "%POWERSHELL_EXE%" (
    echo Windows PowerShell was not found at "%POWERSHELL_EXE%".
    exit /b 1
)

if "%~1"=="" (
    "%POWERSHELL_EXE%" -NoProfile -ExecutionPolicy Bypass -File "scripts\start-debug.ps1"
) else (
    "%POWERSHELL_EXE%" -NoProfile -ExecutionPolicy Bypass -File "scripts\start-debug.ps1" -Port "%~1"
)

set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo Debug launcher failed with exit code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%
