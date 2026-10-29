@echo off
setlocal EnableExtensions
title Transport UI Preview - Offline
for %%I in ("%~dp0..") do set "PREVIEW_ROOT=%%~fI"
cd /d "%PREVIEW_ROOT%"
set "PREVIEW_PYTHON=%PREVIEW_ROOT%\.venv\Scripts\python.exe"
if not exist "%PREVIEW_PYTHON%" (
    echo [ERROR] The project .venv is missing. Set up Transport_App.bat first.
    pause
    exit /b 1
)
echo Offline preview: hardware and network access are blocked.
echo Ctrl+Shift+R rebuilds this Qt Quick preview only.
"%PREVIEW_PYTHON%" -B "%PREVIEW_ROOT%\transport_quick_preview.py" %*
if errorlevel 1 (
    echo [ERROR] Preview stopped. Fix the error above, then run this launcher again.
    pause
    exit /b 1
)
exit /b 0
