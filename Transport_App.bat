@echo off
setlocal EnableExtensions
title Transport Measurement App
cd /d "%~dp0"

echo Starting Transport UI...
echo ---------------------------------------

:: Keep the application dependencies separate from the system Python.
set "VENV_DIR=%~dp0.venv"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"
set "PYTHONW_EXE=%VENV_DIR%\Scripts\pythonw.exe"

if not exist "%PYTHON_EXE%" (
    where python.exe >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] Python was not found on PATH.
        echo Install Python 3.10 or newer, then run this launcher again.
        pause
        exit /b 1
    )

    echo [SETUP] Creating virtual environment in .venv...
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] Could not create the virtual environment.
        pause
        exit /b 1
    )
)

if not exist requirements.txt (
    echo [ERROR] requirements.txt not found. Cannot install application dependencies.
    pause
    exit /b 1
)

:: Install only when a requirement is absent from the project environment.
"%PYTHON_EXE%" -m pip show PyQt6 matplotlib numpy pyvisa nidaqmx pythonnet >nul 2>nul
if errorlevel 1 (
    echo [SETUP] Installing application dependencies into .venv...
    "%PYTHON_EXE%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed.
        pause
        exit /b 1
    )
)

:: Validate imports in a visible console before starting the windowed app.
"%PYTHON_EXE%" -c "from app.ui.main_window import MainWindow"
if errorlevel 1 (
    echo [ERROR] Application startup check failed.
    pause
    exit /b 1
)

:: Launch as a windowed Windows app. The app itself sets its Windows AppUserModelID.
if exist "%PYTHONW_EXE%" (
    start "Transport Measurement" "%PYTHONW_EXE%" "%~dp0transport_UI.py"
) else (
    echo [WARN] pythonw.exe not found; starting with python.exe.
    "%PYTHON_EXE%" "%~dp0transport_UI.py"
)

exit /b 0
