@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" olx_app.py
) else (
    py olx_app.py
)
if errorlevel 1 pause

