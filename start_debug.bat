@echo off
setlocal
cd /d "%~dp0"

if "%~1"=="" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\start-debug.ps1"
) else (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\start-debug.ps1" -Port "%~1"
)

set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo Debug launcher failed with exit code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%
