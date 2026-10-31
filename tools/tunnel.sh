#!/usr/bin/env bash
# Lumen 公网隧道一键脚本
#
# 用法:
#   bash tools/tunnel.sh serveo     # 零注册，地址随机，断了重跑即可换新地址
#   bash tools/tunnel.sh pinggy     # 60 分钟有效
#
# 前置: 本机中继服务器(8772) 与 LumenGuard(8771) 必须在跑
#   cd Lumen && python tools/relay_server.py --host 0.0.0.0 --port 8772 --secret "<密钥>"
#   cd Lumen && python guard/lumen_guard.py --console
#
# 结果: 公网地址写入 data/tunnel.txt，手机 APP「跨网中继」模式填该地址 + 同一密钥
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROVIDER="${1:-serveo}"
PORT="${2:-8772}"
# 每个 provider+端口 单独一个文件，避免起下载隧道时覆盖掉中继地址
OUT="$ROOT/data/tunnel_${PROVIDER}_${PORT}.txt"

mkdir -p "$(dirname "$OUT")"

case "$PROVIDER" in
  serveo)
    CMD=(ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=30
         -o ExitOnForwardFailure=yes -R "80:localhost:${PORT}" serveo.net)
    PATTERN='https://[A-Za-z0-9.-]*serveousercontent\.com'
    ;;
  pinggy)
    CMD=(ssh -p 443 -o StrictHostKeyChecking=no -o ServerAliveInterval=30
         -R "0:localhost:${PORT}" a.pinggy.io)
    PATTERN='https://[A-Za-z0-9.-]*free\.pinggy\.net'
    ;;
  *)
    echo "用法: bash tools/tunnel.sh [serveo|pinggy] [端口]" >&2
    exit 2
    ;;
esac

echo "=== 建立 ${PROVIDER} 隧道 -> 本机 ${PORT} ==="
"${CMD[@]}" 2>&1 | tee /tmp/lumen_tunnel_raw.log &
SSH_PID=$!

URL=""
for _ in $(seq 1 30); do
  sleep 1
  URL="$(grep -oE "$PATTERN" /tmp/lumen_tunnel_raw.log 2>/dev/null | head -1)"
  [ -n "$URL" ] && break
done

if [ -z "$URL" ]; then
  echo "!! 没能拿到公网地址，看 /tmp/lumen_tunnel_raw.log" >&2
  tail -5 /tmp/lumen_tunnel_raw.log >&2
  kill $SSH_PID 2>/dev/null
  exit 1
fi

printf '%s\n' "$URL" > "$OUT"
echo ""
echo "公网地址: $URL"
echo "已写入:   $OUT"
echo "手机 APP「跨网中继」填: 中继地址=$URL  中继密钥=<config/settings.json 里的 relay.secret>"
echo ""
echo "隧道运行中 (Ctrl-C 结束)。"

wait $SSH_PID
