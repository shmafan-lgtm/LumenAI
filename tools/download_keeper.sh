#!/usr/bin/env bash
# 下载隧道看门狗
#
# 为什么需要：pinggy 免费隧道硬性 60 分钟到期（服务端直接 "Time exceeded" 断连）。
# 发公版需要一个能长期挂着的下载入口，不能靠人肉每 60 分钟重连一次。
#
# 行为：隧道断了就 5 秒后重连，地址每次重写到
#       data/tunnel_pinggy_<端口>.txt，并追加到 data/download_tunnel.log。
#       桌面端「手机」面板可以读这个文件展示当前下载链接。
#
# 用法：bash tools/download_keeper.sh [端口，默认 8790]
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PORT="${1:-8790}"
LOG="$ROOT/data/download_tunnel.log"
mkdir -p "$ROOT/data"

while true; do
  echo "[$(date '+%F %T')] 建立下载隧道 -> 127.0.0.1:$PORT" >> "$LOG"
  bash tools/tunnel.sh pinggy "$PORT" >> "$LOG" 2>&1
  echo "[$(date '+%F %T')] 隧道断开（pinggy 免费上限 60 分钟），5 秒后重连" >> "$LOG"
  sleep 5
done
