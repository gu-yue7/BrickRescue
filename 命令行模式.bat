@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=%~dp0runtime\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0brick_rescue.py" --cli
echo.
pause
