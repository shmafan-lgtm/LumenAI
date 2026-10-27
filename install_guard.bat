@echo off
chcp 65001 >nul
title LumenGuard 安装
cd /d "%~dp0"

echo ============================================
echo  LumenGuard 即将安装
echo ============================================
echo  它会在你登录 Windows 时自动启动，常驻后台
echo  只做三件事：启动 Lumen、关闭 Lumen、Lumen 异常退出后拉起
echo.
echo  它不会隐藏自己，不会对抗任务管理器，
echo  你可以随时在「任务计划程序」里禁用它，或运行 uninstall_guard.bat 卸载。
echo.
echo  常驻内存约 26MB，空闲时几乎不占 CPU。
echo ============================================
choice /c YN /m "同意并继续安装"
if errorlevel 2 exit /b 0

if exist "LumenGuard\LumenGuard.exe" (
  "LumenGuard\LumenGuard.exe" --install
) else if exist "LumenGuard.exe" (
  "LumenGuard.exe" --install
) else (
  echo [提示] 没找到 LumenGuard 可执行文件，改用源码方式启动（需要本机有 Python）。
  python guard\lumen_guard.py --install
)
echo.
pause
