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
#
# ⚠️ 本脚本**只传二进制，不传配置文件**（2026-09-16 config 重构后二进制不再自带
#    output/slug/stake 等参数，全部来自服务器上的配置文件）。首次部署需手动放一份
#    v4.config.yaml 到 /root/lastrading/；这样做是有意的——服务器上那份含**密文凭证**
#    （sdk.polymarket.owner_key / clob_creds），自动覆盖会把它冲掉。
#    之后改参数: 改本地副本 → scp 手动上传 → 重启进程（不要无脑全量覆盖）。
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
