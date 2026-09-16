@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY=D:\Python3.12.1\python.exe"
if not exist "%PY%" set "PY=python"

echo ============================================================
echo   Stock Radar ^| Local Stock Market Radar
echo ------------------------------------------------------------
echo   Python : %PY%
echo   Project: %~dp0
echo   Portal : http://127.0.0.1:8848/
echo ============================================================
echo.

"%PY%" -c "import flask, requests, pandas" 2>nul
if errorlevel 1 (
  echo [INFO] Missing dependencies detected, installing from requirements.txt ...
  "%PY%" -m pip install -r requirements.txt
)

echo [INFO] Starting service, browser will open automatically ...
echo [INFO] Press Ctrl+C to stop the server.
echo.

"%PY%" "%~dp0app.py" %*

echo.
echo [INFO] Server stopped.
pause
endlocal
