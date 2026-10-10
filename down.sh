#!/bin/bash
set -euo pipefail

source .env

# 用法: ./down.sh <v4|v3|btc|eth|...>
#   v4  — 下载 /root/lastrading/data/v4（dog@0.2 触底观测 JSONL）到 ./data
#   v3  — 下载 /root/lastrading/data/v3（旧 flip 信号 JSONL，兼容保留）
#   btc — 下载 /root/lastrading/data 到 ./data（旧采集数据，兼容保留）
#   eth — 下载 /root/lastrading/data/eth 到 ./data（走默认分支，同采集器默认输出目录）
#
# 下载语义（rsync 取代原 scp -r 全量拉取）:
#   ① 本地已经有的文件默认跳过，不再重复下载；
#   ② 例外 = 每个「前缀」里日期最新那份：每次都重新核对——它很可能还在写，
#      远端体积变大只传新增部分（--size-only 判体积：没变 = 没增量，直接跳过）；
#   ③ 远端有、本地没有的文件照常下载。
# 干跑（只列会动哪些文件，不写入）: DRY_RUN=1 ./down.sh events

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

# 落点语义与原来的 scp -r 一致: ./data 已存在时文件落在 ./data/<远端目录名>，否则就是 ./data 本身
if [ -d ./data ]; then
    LOCAL_DIR="./data/$(basename "$REMOTE_DIR")"
else
    LOCAL_DIR="./data"
fi

TMPD="$(mktemp -d)"
trap 'rm -rf "$TMPD"' EXIT
: > "$TMPD/patterns.txt"   # rsync 排除模式（锚定传输根）
: > "$TMPD/excl.txt"       # 本地已有、按名跳过的文件
: > "$TMPD/latest.txt"     # 每族日期最新的文件（允许核对增量）

if [ -d "$LOCAL_DIR" ]; then
    # 本地已有文件清单（相对 LOCAL_DIR 的路径）
    ( cd "$LOCAL_DIR" && find . -type f ! -name '.DS_Store' | sed 's|^\./||' ) > "$TMPD/local.txt"

    # 每个「前缀+后缀」族里日期最大的那条 = 允许核对增量；名字里没有日期的不参与排除（宁可多查不漏）
    awk '
        match($0, /[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]/) {
            d = substr($0, RSTART, RLENGTH)
            print substr($0, 1, RSTART-1) substr($0, RSTART+RLENGTH) "|" d "|" $0
        }
    ' "$TMPD/local.txt" | sort -t '|' -k1,1 -k2,2 \
        | awk -F'|' '{ pick[$1] = $3 } END { for (k in pick) print pick[k] }' > "$TMPD/latest.txt"

    if [ -s "$TMPD/latest.txt" ]; then
        grep -Fxv -f "$TMPD/latest.txt" "$TMPD/local.txt" > "$TMPD/excl.txt" || true
    else
        cp "$TMPD/local.txt" "$TMPD/excl.txt"
    fi
    # 模式以 / 开头 = 锚定到传输根，子目录里的同名文件不会被误伤
    sed 's|^|/|' "$TMPD/excl.txt" > "$TMPD/patterns.txt"
fi

RSYNC_OPTS=(-rltvz --size-only --partial-dir=.rsync-partial --progress
            --exclude-from="$TMPD/patterns.txt")
if [ -n "${DRY_RUN:-}" ]; then
    RSYNC_OPTS+=(-n)
    echo "（干跑模式：只列出会下载/更新的文件，不写入）"
fi

echo "正在从 $REMOTE_DIR 下载到 ./data ..."
SKIP_N=$(wc -l < "$TMPD/excl.txt" | tr -d ' ')
KEEP_N=$(wc -l < "$TMPD/latest.txt" | tr -d ' ')
if [ "$SKIP_N" -gt 0 ]; then
    echo "  跳过本地已有文件 ${SKIP_N} 个"
fi
if [ "$KEEP_N" -gt 0 ]; then
    echo "  核对增量（每族最新日期，共 ${KEEP_N} 个）:"
    sed 's|^|    |' "$TMPD/latest.txt"
fi

rsync "${RSYNC_OPTS[@]}" \
    -e "ssh -o ProxyCommand='nc -X 5 -x 127.0.0.1:1080 %h %p'" \
    root@"$SERVER_IP":"$REMOTE_DIR" ./data

echo "下载完成。"
