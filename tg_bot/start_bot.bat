@echo off
cd /d "%~dp0.."
if exist ".venv_bot\Scripts\python.exe" (
  ".venv_bot\Scripts\python.exe" -m tg_bot.main
) else (
  py -3.12 -m tg_bot.main
)
pause
