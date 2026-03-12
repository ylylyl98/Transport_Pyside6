@echo off
title Transport Measurement App
cd /d "%~dp0"

echo Starting Transport UI...
echo ---------------------------------------

:: 1. Try to launch the Python Application
python transport_UI.py

:: 2. Crash Handler & Auto-Installer
if %errorlevel% neq 0 (
    echo.
    echo [CRASH DETECTED]
    echo The application closed with an error.
    echo.
    echo ---------------------------------------
    echo Attempting to fix missing libraries...
    echo ---------------------------------------
    
    :: Check if pip is available and requirements.txt exists
    if exist requirements.txt (
        echo Found requirements.txt. Installing dependencies...
        pip install -r requirements.txt
        
        echo.
        echo ---------------------------------------
        echo Installation complete. Retrying App...
        echo ---------------------------------------
        python transport_UI.py
        
        :: If it crashes again, pause
        if %errorlevel% neq 0 (
            echo.
            echo [FATAL ERROR] Still crashing. Please check the error message above.
            pause
        )
    ) else (
        echo [ERROR] requirements.txt not found! Cannot auto-fix.
        pause
    )
)