@echo off
chcp 65001 >nul
title Lumen
cd /d "%~dp0"

if exist "Lumen.exe" (
  start "" "Lumen.exe"
  exit /b 0
)

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 没有找到 Python，也没有 Lumen.exe。
  pause
  exit /b 1
)

echo 正在点亮 Lumen ...
python main.py %*
if errorlevel 1 pause
