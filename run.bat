@echo off
cd /d "%~dp0"
.venv\Scripts\python -m budget_sync run %*
pause
