#!/usr/bin/env bash
# Lumen 权重下载器（断点续传 + 大小校验）
# 目标目录：F:\LumenAI\models
set -u
MODELS_DIR="F:/LumenAI/models"
LOG="F:/LumenAI/models/download.log"
mkdir -p "$MODELS_DIR"
exec > >(tee -a "$LOG") 2>&1

ts(){ date "+%Y-%m-%d %H:%M:%S"; }

# 模型清单：本地文件名 | 镜像URL | 期望字节数
# 顺序：先把较小的 Core 3B 拉完（便于尽快做引擎自检），再拉 H1 Turbo。
MODELS=(
"Qwen2.5-3B-Instruct-Q4_K_M.gguf|https://hf-mirror.com/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf|2104932768"
"H1-Turbo-Q4_K_M.gguf|https://hf-mirror.com/unsloth/gpt-oss-20b-GGUF/resolve/main/gpt-oss-20b-Q4_K_M.gguf|11624759488"
)

for entry in "${MODELS[@]}"; do
  IFS='|' read -r fname url expect <<< "$entry"
  dest="$MODELS_DIR/$fname"
  echo "[$(ts)] ===== 开始: $fname (期望 $((expect/1024/1024)) MB) ====="
  attempt=0
  while :; do
    attempt=$((attempt+1))
    # 用 -C - 续传；-L 跟随重定向；--retry 网络抖动重试
    # --speed-limit/--speed-time：连续 30s 平均速度低于 100KB/s 视为卡死，
    # curl 以 28 退出 → 外层 while 用 -C - 续传重试，避免无限挂起。
    curl -L -C - --retry 8 --retry-delay 5 --retry-all-errors \
      --speed-limit 100000 --speed-time 30 --max-time 0 -o "$dest" "$url"
    code=$?
    if [ $code -ne 0 ]; then
      echo "[$(ts)] curl 退出码 $code，$fname 中断，10s 后重试(第 $attempt 次)"
      sleep 10
      continue
    fi
    size=$(stat -c%s "$dest" 2>/dev/null || echo 0)
    if [ "$size" -ge "$expect" ]; then
      echo "[$(ts)] 完成: $fname 大小=$size 字节 (期望 $expect) ✅"
      break
    else
      echo "[$(ts)] 大小不足 $size < $expect，继续续传(第 $attempt 次)"
      sleep 3
    fi
  done
done
echo "[$(ts)] ===== 全部权重下载完成 ====="
