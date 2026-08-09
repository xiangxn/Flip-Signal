#!/usr/bin/env bash
# ============================================================
# Deploy — scp a built binary to the remote server.
#
# Usage:
#   ./deploy.sh          # deploy dist/lab (default)
#   ./deploy.sh flip     # deploy dist/flip
# ============================================================
set -euo pipefail

source .env

CMD="${1:-lab}"

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