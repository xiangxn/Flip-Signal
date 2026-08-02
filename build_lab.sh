#!/usr/bin/env bash
# ============================================================
# Build Lab — cross-compile cmd/lab for Ubuntu (Linux).
#
# Go's built-in cross-compiler means no Docker, no extra deps.
#
# Usage:
#   ./build_lab.sh              # build for linux/amd64 (default)
#   ./build_lab.sh arm64        # build for linux/arm64
#   ./build_lab.sh --clean      # remove dist/
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DIST_DIR="$SCRIPT_DIR/dist"

ARCH="${1:-amd64}"
case "$ARCH" in
    amd64|x86_64)  ARCH="amd64" ;;
    arm64|aarch64) ARCH="arm64" ;;
    --clean)
        echo "Cleaning $DIST_DIR ..."
        rm -rf "$DIST_DIR"
        echo "Done."
        exit 0
        ;;
    *)
        echo "Usage: $0 [amd64|arm64|--clean]"
        exit 1
        ;;
esac

echo "=== Building cmd/lab for linux/$ARCH ==="
echo ""

cd "$SCRIPT_DIR"

mkdir -p "$DIST_DIR"

GOOS=linux GOARCH="$ARCH" CGO_ENABLED=0 \
    go build -trimpath -ldflags="-s -w" \
    -o "$DIST_DIR/lab" \
    ./cmd/lab

echo ""
echo "=== Build complete ==="
echo "Binary: $DIST_DIR/lab"
ls -lh "$DIST_DIR/lab"
file "$DIST_DIR/lab"
echo ""
echo "Transfer to Ubuntu:"
echo "  scp $DIST_DIR/lab user@host:/usr/local/bin/"
echo ""
echo "Run on Ubuntu:"
echo "  ./lab -output data/lab -symbol BTCUSDT"
