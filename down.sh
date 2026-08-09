#!/bin/bash
set -euo pipefail

source .env

# 用法: ./down.sh <btc|eth>
#   btc — 下载 /root/lastrading/data 到 ./data
#   eth — 下载 /root/lastrading/eth/data 到 ./data

SYMBOL="${1:-}"

if [ -z "$SYMBOL" ]; then
    echo "用法: $0 <btc|eth>"
    exit 1
fi

case "$SYMBOL" in
    btc)
        REMOTE_DIR="/root/lastrading/data"
        ;;
    eth)
        REMOTE_DIR="/root/lastrading/eth/data"
        ;;
    *)
        echo "错误: 无效参数 '$SYMBOL'，只支持 btc 或 eth"
        exit 1
        ;;
esac

echo "正在从 $REMOTE_DIR 下载到 ./data ..."
scp -r -o ProxyCommand="nc -X 5 -x 127.0.0.1:1080 %h %p" root@"$SERVER_IP":"$REMOTE_DIR" ./data
echo "下载完成。"
