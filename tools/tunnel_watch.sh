#!/usr/bin/env bash
# Lumen 公网验收隧道守护：serveo 断了自动重连，并把当前公网地址写到 puburl.txt
#
# 注意：路径必须从脚本自身位置推导，不能写死某台机器的绝对路径 ——
# 这个文件是要进仓库的公版脚本，写死路径等于把使用者用户名暴露出去。
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
URL_FILE="$ROOT/tools/puburl.txt"
LOG="${TMPDIR:-/tmp}/tunnel_watch.log"
: > "$URL_FILE"
echo "$(date) watch start" >> "$LOG"

while true; do
  # 清掉可能残留的旧连接
  pkill -f "serveo.net" 2>/dev/null
  sleep 1
  echo "$(date) starting tunnel..." >> "$LOG"
  ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=4 \
      -o LogLevel=ERROR -R 80:localhost:8770 serveo.net > /tmp/tunnel_live.log 2>&1 &
  SSH_PID=$!
  # 等隧道拿到地址（serveo 会把地址打到 stdout）
  for i in $(seq 1 15); do
    if grep -q "serveousercontent.com" /tmp/tunnel_live.log 2>/dev/null; then
      NEW_URL=$(grep -oE "https://[a-z0-9-]+\.serveousercontent\.com" /tmp/tunnel_live.log | head -1)
      if [ -n "$NEW_URL" ]; then
        echo "$NEW_URL" > "$URL_FILE"
        echo "$(date) URL=$NEW_URL" >> "$LOG"
        break
      fi
    fi
    sleep 1
  done
  # 等这个 ssh 进程结束（断了就重来）
  wait $SSH_PID
  echo "$(date) tunnel exited, restarting in 3s" >> "$LOG"
  sleep 3
done
