@echo off
chcp 65001 >nul
title LumenGuard 卸载
cd /d "%~dp0"

echo 正在移除 LumenGuard 开机自启项 ...
if exist "LumenGuard\LumenGuard.exe" (
  "LumenGuard\LumenGuard.exe" --uninstall
) else if exist "LumenGuard.exe" (
  "LumenGuard.exe" --uninstall
) else (
  python guard\lumen_guard.py --uninstall
)
schtasks /Delete /F /TN LumenGuard >nul 2>&1
echo.
echo 完成。Lumen 本体、你的会话与模型文件都没有被动过。
echo 如果 LumenGuard 还在后台运行，可在「任务管理器」里结束它。
pause
