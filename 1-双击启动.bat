@echo off
chcp 65001 >nul
cd /d "%~dp0"
title OCS AI 答题服务

if not exist "runtime\python.exe" (
  echo [错误] 找不到 runtime\python.exe
  echo 说明压缩包没解压完整，请重新解压整包再试。
  pause
  exit /b 1
)

if not exist "server.py" (
  echo [错误] 找不到 server.py，请确认解压目录是 D:\OCS-AI
  pause
  exit /b 1
)

echo ============================================
echo   正在启动本地服务（端口 8321）
echo   这个黑窗口不要关，关掉服务就停了。
echo   看到 listening 字样后再去点安装链接。
echo ============================================
echo.

runtime\python.exe -u server.py

set EXITCODE=%errorlevel%
echo.
echo --- 服务已退出（退出码 %EXITCODE%）---
echo 如果上面有 Traceback 或「端口被占用」，把本窗口内容发给管理员。
echo.
pause
