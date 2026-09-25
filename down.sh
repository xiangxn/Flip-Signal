#!/bin/bash
set -euo pipefail

source .env

# 用法: ./down.sh <v4|v3|btc|eth>
#   v4  — 下载 /root/lastrading/data/v4（dog@0.2 触底观测 JSONL）到 ./data
#   v3  — 下载 /root/lastrading/data/v3（旧 flip 信号 JSONL，兼容保留）
#   btc — 下载 /root/lastrading/data 到 ./data（旧采集数据，兼容保留）
#   eth — 下载 /root/lastrading/data/eth 到 ./data（走默认分支，同采集器默认输出目录）

SYMBOL="${1:-}"

if [ -z "$SYMBOL" ]; then
    echo "用法: $0 <v4|v3|btc|eth|v4live>"
    exit 1
fi

case "$SYMBOL" in
    v4)
        REMOTE_DIR="/root/lastrading/data/v4"
        ;;
    v3)
        REMOTE_DIR="/root/lastrading/data/v3"
        ;;
    btc)
        REMOTE_DIR="/root/lastrading/data"
        ;;
    v4live)
        REMOTE_DIR="/root/lastrading/data/v4live"
        ;;
    *)
        REMOTE_DIR="/root/lastrading/data/$SYMBOL"
        ;;
esac

echo "正在从 $REMOTE_DIR 下载到 ./data ..."
scp -r -o ProxyCommand="nc -X 5 -x 127.0.0.1:1080 %h %p" root@"$SERVER_IP":"$REMOTE_DIR" ./data
echo "下载完成。"
