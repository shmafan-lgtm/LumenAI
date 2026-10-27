@echo off
chcp 65001 >nul
title Lumen 推送到 GitHub
cd /d "%~dp0"

echo ============================================
echo  Lumen 一键推送到 GitHub
echo ============================================
echo.
echo  推之前需要先在浏览器里做两件事：
echo.
echo  [1] 建一个空仓库
echo      https://github.com/new
echo      仓库名填 Lumen
echo      **不要**勾选 Add a README / .gitignore / license
echo      （本地已经有 README、LICENSE、.gitignore 了）
echo.
echo  [2] 把这台机器的公钥加到账号
echo      https://github.com/settings/ssh/new
echo      Title 随便填，Key 粘贴下面这一整行：
echo.
echo  ------------------------------------------------------------
echo  ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJrDADS5udhPAxXwC0v+9UOxrVW5/9puHwSIC+nKdF1K lumen-deploy@PC-20260828CGAS
echo  ------------------------------------------------------------
echo.
echo  （这台机器上 github.com 主站直连不通，所以走 SSH over 443，
echo    ssh.github.com:443 已验证可连。）
echo.

set /p REPO=请输入仓库地址（例 https://github.com/你的账号/Lumen）: 
if "%REPO%"=="" (
  echo.
  echo [取消] 没填仓库地址。
  pause
  exit /b 1
)

echo.
echo [1/3] 测试 SSH 连接...
ssh -T -o BatchMode=yes -o StrictHostKeyChecking=accept-new git@github.com 2>&1 | findstr /C:"successfully authenticated" >nul
if errorlevel 1 (
  echo   没通过。请确认公钥已经加到 GitHub 账号里。
  echo.
  pause
  exit /b 1
)
echo   OK，公钥已生效。

echo.
echo [2/3] 设置远端并推送代码...
git remote remove origin 2>nul
git remote add origin "%REPO%"
git push -u origin HEAD
if errorlevel 1 (
  echo   推送失败，请检查仓库地址是否正确。
  pause
  exit /b 1
)
echo   代码已推送。

echo.
echo [3/3] 打标签触发自动构建（Windows EXE + 手机 APK → Releases）...
set /p TAG=版本号（直接回车用 v1.0.0）: 
if "%TAG%"=="" set TAG=v1.0.0
git tag -f "%TAG%"
git push origin "%TAG%"
if errorlevel 1 (
  echo   标签推送失败，可稍后重试：git push origin %TAG%
  pause
  exit /b 1
)

echo.
echo ============================================
echo  搞定！
echo.
echo  等 5-10 分钟，CI 会自动构建并发布到：
echo    %REPO%/releases
echo.
echo  产物：
echo    Lumen-Windows-x64.zip    桌面端
echo    LumenGuard-Windows-x64.zip  守护服务
echo    Lumen-release-v1.0.0.apk     手机 APP
echo.
echo  下载走 objects.githubusercontent.com，这个域名国内直连是通的。
echo ============================================
pause
