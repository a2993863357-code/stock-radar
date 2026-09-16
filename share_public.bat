@echo off
setlocal enabledelayedexpansion
title Stock Radar - 公网分享
cd /d "%~dp0"

set "PY=D:\Python3.12.1\python.exe"
if not exist "%PY%" set "PY=python"
set "CF=D:\cloudflared\cloudflared.exe"
if defined CLOUDFLARED set "CF=%CLOUDFLARED%"
set "PORT=8848"
set "LOG=%~dp0tunnel.log"
set "URLFILE=%~dp0public_url.txt"
set "MODE=%~1"

echo ============================================================
echo   Stock Radar ^| 公网分享（Cloudflare 快速隧道）
echo ------------------------------------------------------------
echo   项目目录 : %~dp0
echo   本机入口 : http://127.0.0.1:%PORT%/
echo   隧道程序 : %CF%
echo ============================================================
echo.

if not exist "%CF%" (
  echo [错误] 未找到 cloudflared：%CF%
  echo        请下载后放到该路径：
  echo        https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe
  echo.
  pause
  exit /b 1
)

rem ---------------- 1. 本地服务 ----------------
netstat -ano | findstr /c:"LISTENING" | findstr /c:":%PORT% " >nul 2>&1
if not errorlevel 1 (
  echo [1/3] 本地服务已在运行 ^(127.0.0.1:%PORT%^)
  goto :svc_ready
)

echo [1/3] 启动本地服务 ...
start "stock-radar-server" /min "%PY%" "%~dp0app.py" --no-browser

set "READY="
for /l %%i in (1,1,30) do (
  if not defined READY (
    timeout /t 1 /nobreak >nul
    netstat -ano | findstr /c:"LISTENING" | findstr /c:":%PORT% " >nul 2>&1 && set "READY=1"
  )
)
if not defined READY (
  echo [错误] 本地服务 30 秒内未就绪。
  echo        请在项目目录手动执行 app.py 查看报错信息。
  pause
  exit /b 1
)
echo       本地服务已就绪。

:svc_ready

rem ---------------- 2. 公网隧道 ----------------
echo [2/3] 建立公网隧道 ...
taskkill /f /im cloudflared.exe >nul 2>&1
if exist "%LOG%" del "%LOG%" >nul 2>&1
start "cloudflared-tunnel" /min "%CF%" tunnel --url http://127.0.0.1:%PORT% --no-autoupdate --logfile "%LOG%" --loglevel info

rem ---------------- 3. 提取公网地址 ----------------
echo [3/3] 等待公网地址 ...
set "PUBURL="
for /l %%i in (1,1,45) do (
  if not defined PUBURL (
    timeout /t 1 /nobreak >nul
    for /f "usebackq delims=" %%u in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Test-Path -LiteralPath '%LOG%') { $m = Select-String -LiteralPath '%LOG%' -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -AllMatches | Select-Object -Last 1; if ($m) { $m.Matches.Value } }" 2^>nul`) do set "PUBURL=%%u"
  )
)

if not defined PUBURL (
  echo [错误] 45 秒内未获取到公网地址，请查看日志：%LOG%
  pause
  exit /b 1
)

>"%URLFILE%" echo %PUBURL%
echo %PUBURL%| clip

echo.
echo ============================================================
echo   公网地址（已复制到剪贴板）：
echo.
echo   %PUBURL%
echo.
echo   同时保存到：%URLFILE%
echo ============================================================
echo.
echo   提醒：
echo   - 保持本机开机、联网；关闭隧道后该链接立即失效
echo   - 链接是公开的，拿到的人都能访问，用完请及时关闭
echo.

if /i "%MODE%"=="keep" (
  echo [keep 模式] 隧道已在后台常驻，本窗口可关闭。
  echo             需要停止时执行：taskkill /f /im cloudflared.exe
  endlocal
  exit /b 0
)

echo   按任意键结束分享并关闭隧道 ...
pause >nul
taskkill /f /im cloudflared.exe >nul 2>&1
echo   已关闭隧道，公网地址已失效。
timeout /t 2 /nobreak >nul
endlocal
exit /b 0
