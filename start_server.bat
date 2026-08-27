@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

echo ===================================================
echo   SLK Smart Lunch Tracker Server v3.0
echo   URL: http://localhost:8000
echo ===================================================
echo.

python server.py
pause
