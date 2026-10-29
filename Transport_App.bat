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
        echo Install Python 3.12 to 3.14, then run this launcher again.
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

"%PYTHON_EXE%" -c "import sys; sys.exit(0 if (3,12) <= sys.version_info[:2] < (3,15) else 1)"
if errorlevel 1 (
    echo [ERROR] This release needs Python 3.12 to 3.14. The existing .venv was preserved.
    echo See docs\qt-quick-migration.md for environment migration and rollback.
    pause
    exit /b 1
)

if not exist requirements.txt (
    echo [ERROR] requirements.txt not found. Cannot install application dependencies.
    pause
    exit /b 1
)

:: Read installed metadata first; avoid pip startup/network checks on every launch.
echo [SETUP] Checking application dependencies in .venv...
"%PYTHON_EXE%" -B "%~dp0tools\check_transport_startup.py" --dependencies-only
if errorlevel 1 (
    "%PYTHON_EXE%" -m pip install --disable-pip-version-check -r "%~dp0requirements.txt"
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed.
        pause
        exit /b 1
    )
)

:: Recheck after source/environment changes. Reuse only a successful matching check.
:: Set TRANSPORT_VALIDATE_STARTUP=1 to force a fresh hardware-blocked check.
if /I "%~1"=="--widgets" (
    "%PYTHON_EXE%" -B "%~dp0tools\check_transport_startup.py" --frontend widgets
) else (
    "%PYTHON_EXE%" -B "%~dp0tools\check_transport_startup.py" --frontend quick
)
if errorlevel 1 (
    echo [ERROR] Application startup check failed.
    pause
    exit /b 1
)

:: Keep the local shortcut's icon and taskbar identity in sync with this checkout.
:: Allow this local helper in its own process without changing system policy.
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File "%~dp0tools\Update-TransportShortcut.ps1"
if errorlevel 1 (
    echo [WARN] Could not update the Transport shortcut. Application startup will continue.
)

:: Launch as a windowed Windows app. The app itself sets its Windows AppUserModelID.
if exist "%PYTHONW_EXE%" (
    start "Transport Measurement" "%PYTHONW_EXE%" "%~dp0transport_UI.py" %*
) else (
    echo [WARN] pythonw.exe not found; starting with python.exe.
    "%PYTHON_EXE%" "%~dp0transport_UI.py" %*
)

exit /b 0
