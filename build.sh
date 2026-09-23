#!/usr/bin/env bash
# ============================================================
# Build — cross-compile any cmd/<name> for Ubuntu (Linux).
#
# Go's built-in cross-compiler means no Docker, no extra deps.
#
# Usage:
#   ./build.sh                    # build cmd/flip for linux/amd64
#   ./build.sh <name>             # build cmd/<name> for linux/amd64
#   ./build.sh <name> <arch>      # build cmd/<name> for linux/<arch>
#   ./build.sh --clean            # remove dist/
#
# 可用命令由 cmd/ 目录自动检测（v4 分支: flip = dog@0.2 引擎 / tail = 扫尾盘 ⑤ 引擎 /
# btreplay = 逐笔对账重放）
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DIST_DIR="$SCRIPT_DIR/dist"

# ------------------------------------------------------------------
# Parse arguments
# ------------------------------------------------------------------
CMD=""          # which cmd/<name> to build
ARCH=""         # target architecture

# List of known cmd entry points (auto-detected from cmd/ directory)
KNOWN_CMDS=($(ls "$SCRIPT_DIR/cmd/" 2>/dev/null))

is_known_cmd() {
    local name="$1"
    for c in "${KNOWN_CMDS[@]}"; do
        [[ "$c" == "$name" ]] && return 0
    done
    return 1
}

if [[ $# -eq 0 ]]; then
    # No args: build flip for amd64
    CMD="flip"
    ARCH="amd64"
elif [[ $# -eq 1 ]]; then
    case "$1" in
        --clean)
            echo "Cleaning $DIST_DIR ..."
            rm -rf "$DIST_DIR"
            echo "Done."
            exit 0
            ;;
        amd64|x86_64)
            # Legacy: bare arch → build flip
            CMD="flip"
            ARCH="amd64"
            ;;
        arm64|aarch64)
            CMD="flip"
            ARCH="arm64"
            ;;
        *)
            if is_known_cmd "$1"; then
                CMD="$1"
                ARCH="amd64"
            else
                echo "Error: unknown command '$1'"
                echo "Available: ${KNOWN_CMDS[*]}"
                exit 1
            fi
            ;;
    esac
elif [[ $# -eq 2 ]]; then
    if is_known_cmd "$1"; then
        CMD="$1"
    else
        echo "Error: unknown command '$1'"
        echo "Available: ${KNOWN_CMDS[*]}"
        exit 1
    fi
    case "$2" in
        amd64|x86_64)  ARCH="amd64" ;;
        arm64|aarch64) ARCH="arm64" ;;
        *)
            echo "Error: unknown architecture '$2'. Use amd64 or arm64."
            exit 1
            ;;
    esac
else
    echo "Usage: $0 [<cmd>] [<arch>]"
    echo "       $0 --clean"
    echo ""
    echo "Commands: ${KNOWN_CMDS[*]}"
    echo "Archs:    amd64, arm64"
    exit 1
fi

echo "=== Building cmd/$CMD for linux/$ARCH ==="
echo ""

cd "$SCRIPT_DIR"
mkdir -p "$DIST_DIR"

GOOS=linux GOARCH="$ARCH" CGO_ENABLED=0 \
    go build -trimpath -ldflags="-s -w" \
    -o "$DIST_DIR/$CMD" \
    "./cmd/$CMD"

echo ""
echo "=== Build complete ==="
echo "Binary: $DIST_DIR/$CMD"
ls -lh "$DIST_DIR/$CMD"
file "$DIST_DIR/$CMD"
echo ""
echo "Transfer to Ubuntu:"
echo "  ./deploy.sh $CMD"
echo ""
case "$CMD" in
    flip)
        echo "Run on Ubuntu (dog@0.2 引擎，纸面模式):"
        echo "  nohup ./flip -config v4.config.yaml -dashboard :8090 >> flip.log 2>&1 &"
        ;;
    tail)
        echo "Run on Ubuntu (扫尾盘 ⑤ 引擎，纸面模式):"
        echo "  nohup ./tail -config v4.config.yaml >> tail.log 2>&1 &"
        ;;
    *)
        echo "Run on Ubuntu:"
        echo "  nohup ./$CMD -config v4.config.yaml >> $CMD.log 2>&1 &"
        ;;
esac
echo ""
echo "注: 参数（output_dir/stake/熔断线/阈值…）都在配置文件里，二进制不再接受这些 flag。"
echo "    服务器上需自备一份 v4.config.yaml（含密文凭证时由你手动放置，deploy.sh 不传配置文件）。"
