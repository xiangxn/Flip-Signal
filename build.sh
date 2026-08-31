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
# 可用命令由 cmd/ 目录自动检测: flip, collect, compact
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
echo "Run on Ubuntu (flip 引擎，纸面模式):"
echo "  nohup ./flip -output data/v3 -dashboard :8090 >> flip.log 2>&1 &"
