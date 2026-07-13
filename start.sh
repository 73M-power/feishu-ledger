#!/bin/bash
set -e

export TZ="Asia/Shanghai"
export LEDGER_DATA_DIR="${LEDGER_DATA_DIR:-/app/data}"
mkdir -p "$LEDGER_DATA_DIR"

CF_TOKEN="${CLOUDFLARE_TUNNEL_TOKEN:-${CF_TUNNEL_TOKEN:-${TUNNEL_TOKEN:-}}}"
if [ -n "$CF_TOKEN" ]; then
    echo "Starting Cloudflare Tunnel..."
    cloudflared tunnel --no-autoupdate run --token "$CF_TOKEN" &
    sleep 1
    echo "cloudflared PID: $!"
fi

PORT="${PORT:-8787}"
HOST="${HOST:-0.0.0.0}"
echo "Starting Feishu Ledger on ${HOST}:${PORT}"
echo "Data dir: ${LEDGER_DATA_DIR}"
exec python3 -u /app/server.py --host "$HOST" --port "$PORT"