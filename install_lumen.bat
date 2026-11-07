@echo off
chcp 65001 >nul
title Lumen 安装
cd /d "%~dp0"

echo ============================================
echo  Lumen 本地化 AI 桌面端 · 安装
echo ============================================
echo.
echo  这是绿色版：不需要安装程序，把整个目录放到任意目录都能跑。
echo  首次启动会在程序旁边自动创建：
echo    models\        放 .gguf 权重（主基座 gpt-oss-20b，界面「模型档位 → 获取权重」可下载）
echo    workspace\     AI 的默认工作目录
echo    data\          账号、会话库、IPC 令牌（删掉即重置）
echo    logs\          运行日志
echo    config\        设置、智能搜索与同步远控配置
echo.
echo  提示：模型权重不入安装包（10.8GB 太大），请在安装向导里勾选下载，
echo        或装好后打开 Lumen ->「模型档位 -> 获取权重」下载 gpt-oss-20b。
echo        未下载前 Lumen 以离线规则引擎运行（会明确标注，不假装是模型输出）。
echo.
echo  想换目录？直接把这个文件夹整个剪走即可，权重、会话、设置都跟着走。
echo.

if exist "Lumen.exe" (
  echo [就绪] Lumen.exe 已存在
) else (
  echo [提示] 未找到 Lumen.exe —— 若这是源码包，请先运行：
  echo        python -m PyInstaller --noconfirm --clean build.spec
)

if exist "_internal\runtime\llamacpp\llama-server.exe" (
  echo [就绪] 内置 llama.cpp 运行时已就位（Vulkan GPU 加速，无需另装 Ollama）
) else (
  echo [提示] 没找到内置运行时，推理会回落到 CPU / 离线规则引擎。
)

if exist "_internal\models\Qwen2.5-3B-Instruct-Q4_K_M.gguf" (
  echo [就绪] 内置权重已就位（Qwen2.5-3B-Instruct Q4_K_M，约 2.0GB）
) else (
  echo [提示] 没有内置权重，请自行下载 .gguf 放进 models\ 目录。
)

echo.
echo 接下来：
echo   1. 双击 Lumen.exe 启动（首次启动会自动拉起内置运行时）
echo   2. 想要「关窗口也常驻、异常退出自动拉起」→ 运行 install_guard.bat
echo   3. 想用手机同步/远控 → 在「设置 → 手机同步」里开启「允许局域网访问」
echo.
echo 遇到 Windows Defender 防火墙弹窗？
echo    Lumen 启动时会自动按「端口」补放行规则（8770 只允许本地子网），
echo    不再依赖程序路径，所以整个目录搬到哪都有效。若仍被拦，
echo    在「手机同步」面板点「一键放行局域网」，按提示同意管理员权限即可。
echo    注意：禁用 Defender 实时保护与防火墙放行是两件事，别混为一谈。
echo.
echo  想用 H1 Turbo / mini se？
echo    在「模型档位」弹窗里点「获取权重」，可直接下载对应档位的 .gguf
echo    权重（带断点续传），下完切档位即刻生效。
echo.
echo  完整说明见 README.md，第三方许可声明在 licenses\ 目录。
echo.
pause
