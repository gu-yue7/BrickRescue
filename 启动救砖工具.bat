@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=%~dp0runtime\pythonw.exe"
if not exist "%PY%" set "PY=pythonw"
start "" "%PY%" "%~dp0brick_rescue.py"
