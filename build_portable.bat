@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

set VENV_PYTHON=%~dp0.venv\Scripts\python.exe
set PLAYWRIGHT_CACHE=C:\Users\%USERNAME%\AppData\Local\ms-playwright
set DIST=%~dp0portable

echo ========================================
echo   Build Portable yngp Searcher
echo ========================================

if not exist "%VENV_PYTHON%" (
    echo ERROR: Python not found at %VENV_PYTHON%
    pause & exit /b 1
)

echo [1/3] Cleaning old build...
if exist "%DIST%" rmdir /s /q "%DIST%"
if exist "build" rmdir /s /q "build"

echo [2/3] Running PyInstaller...
"%VENV_PYTHON%" -m PyInstaller ^
    --onefile ^
    --name "yngp_searcher" ^
    --distpath "%DIST%" ^
    --workpath "build" ^
    --add-data "static;static" ^
    --hidden-import "playwright.async_api" ^
    --hidden-import "uvicorn.loops.auto" ^
    --hidden-import "uvicorn.protocols.http.auto" ^
    --hidden-import "uvicorn.protocols.websockets.auto" ^
    --hidden-import "uvicorn.lifespan.on" ^
    --hidden-import "websockets" ^
    --hidden-import "soupsieve" ^
    --hidden-import "webview" ^
    --hidden-import "webview.platforms.winforms" ^
    --hidden-import "pythonnet" ^
    --hidden-import "clr_loader" ^
    --hidden-import "discovery" ^
    --hidden-import "discovery.adapters" ^
    --hidden-import "discovery.engine" ^
    --hidden-import "discovery.fetcher" ^
    --hidden-import "discovery.models" ^
    --hidden-import "discovery.parsers" ^
    --hidden-import "discovery.providers" ^
    --hidden-import "discovery.ratelimit" ^
    --hidden-import "discovery.urltools" ^
    --collect-all "playwright" ^
    server.py

if %ERRORLEVEL% neq 0 (
    echo PyInstaller failed!
    pause & exit /b 1
)

echo [3/3] Copying Playwright Chromium...
set PW_DIR=%DIST%\playwright
mkdir "%PW_DIR%" 2>nul
xcopy /e /i /q "%PLAYWRIGHT_CACHE%\chromium-1228" "%PW_DIR%\chromium-1228\"
xcopy /e /i /q "%PLAYWRIGHT_CACHE%\chromium_headless_shell-1228" "%PW_DIR%\chromium_headless_shell-1228\"
xcopy /e /i /q "%PLAYWRIGHT_CACHE%\ffmpeg-1011" "%PW_DIR%\ffmpeg-1011\"
xcopy /e /i /q "%PLAYWRIGHT_CACHE%\winldd-1007" "%PW_DIR%\winldd-1007\"

(
echo @echo off
echo cd /d "%%~dp0"
echo set PLAYWRIGHT_BROWSERS_PATH=%%~dp0playwright
echo yngp_searcher.exe
echo pause
) > "%DIST%\start.bat"

del /q yngp_searcher.spec 2>nul
rmdir /s /q build 2>nul

echo.
echo ========================================
echo   Build complete!
echo   Output: portable\
echo   Double-click start.bat to launch
echo ========================================
pause
