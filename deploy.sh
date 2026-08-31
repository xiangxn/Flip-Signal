#!/usr/bin/env bash
# ============================================================
# Deploy — scp a built binary to the remote server.
#
# Usage:
#   ./deploy.sh          # deploy dist/flip (default)
#   ./deploy.sh collect  # deploy dist/collect
#
# 依赖: 根目录 .env 中的 SERVER_IP（.env 不入 git）
# 网络: 经本地 SOCKS5 代理 127.0.0.1:1080 连接服务器
# ============================================================
set -euo pipefail

source .env

CMD="${1:-flip}"

SRC="./dist/$CMD"
DST="root@$SERVER_IP:/root/lastrading/$CMD"

if [[ ! -f "$SRC" ]]; then
    echo "Error: binary not found: $SRC"
    echo "Run: ./build.sh $CMD"
    exit 1
fi

echo "Deploying $CMD ..."
scp -o ProxyCommand="nc -X 5 -x 127.0.0.1:1080 %h %p" "$SRC" "$DST"
echo "Done: $DST"
