@echo off
title Transport Measurement App
cd /d "%~dp0"

echo Starting Transport UI...
echo ---------------------------------------

:: Check that the GUI dependency is available before launching without a console.
python -c "import PyQt6" >nul 2>nul
if %errorlevel% neq 0 (
    echo.
    echo [SETUP] Missing Python libraries.
    if exist requirements.txt (
        echo Installing dependencies from requirements.txt...
        python -m pip install -r requirements.txt
    ) else (
        echo [ERROR] requirements.txt not found. Cannot auto-fix.
        pause
        exit /b 1
    )
)

:: Launch as a windowed Windows app. The app itself sets its Windows AppUserModelID.
where pythonw.exe >nul 2>nul
if %errorlevel% equ 0 (
    start "Transport Measurement" pythonw.exe "%~dp0transport_UI.py"
) else (
    echo [WARN] pythonw.exe not found; falling back to python.exe.
    start "Transport Measurement" python.exe "%~dp0transport_UI.py"
)

exit /b 0
