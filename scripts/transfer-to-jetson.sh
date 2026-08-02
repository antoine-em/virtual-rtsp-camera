#!/usr/bin/env bash
set -euo pipefail

# Transfer data to Jetson AGX Orin
# Usage: scripts/transfer-to-jetson.sh <source-dir> [jetson-user] [jetson-host]

SOURCE_DIR="${1:?Usage: $0 <source-dir> [jetson-user] [jetson-host]}"
JETSON_USER="${2:-}"
JETSON_HOST="${3:-${JETSON_HOST:-}}"

echo "=== Transfer to Jetson ==="
echo "Source:  $SOURCE_DIR"
echo "Jetson:  ${JETSON_HOST:-?}"
echo ""

# Check dependencies
if ! command -v rsync &>/dev/null; then
    echo "ERROR: rsync not found"
    exit 1
fi

if [[ ! -d "$SOURCE_DIR" ]]; then
    echo "ERROR: Source directory not found: $SOURCE_DIR"
    exit 1
fi

# Detect jetson host
if [[ -z "$JETSON_HOST" ]]; then
    echo "ERROR: Jetson host not specified"
    echo "Usage: $0 <source-dir> [jetson-user] [jetson-host]"
    echo "Or set JETSON_HOST environment variable"
    exit 1
fi

# Detect jetson user
if [[ -z "$JETSON_USER" ]]; then
    echo "WARNING: Jetson user not specified. Attempting to detect..."
    # Try common defaults
    for user in edgeai nvidia admin; do
        if ssh -o BatchMode=yes -o ConnectTimeout=5 "$user@$JETSON_HOST" "echo OK" 2>/dev/null | grep -q "OK"; then
            JETSON_USER="$user"
            echo "  Detected user: $JETSON_USER"
            break
        fi
    done
    if [[ -z "$JETSON_USER" ]]; then
        echo "ERROR: Could not detect Jetson user. Please specify it explicitly."
        echo "Usage: $0 <source-dir> <jetson-user> <jetson-host>"
        exit 1
    fi
fi

# Destination on Jetson
DEST_DIR="${DEST_DIR:-/home/${JETSON_USER}/synthetic-data}"

echo ""
echo "--- Transfer plan ---"
echo "  Local:  $SOURCE_DIR/"
echo "  Remote: ${JETSON_USER}@${JETSON_HOST}:${DEST_DIR}/"
echo ""

# Show rsync command
RSYNC_CMD="rsync -avz --progress --partial \
    -e 'ssh -o StrictHostKeyChecking=accept-new' \
    $SOURCE_DIR/ ${JETSON_USER}@${JETSON_HOST}:${DEST_DIR}/"

echo "Command:"
echo "  $RSYNC_CMD"
echo ""

# Execute rsync
eval "$RSYNC_CMD" || {
    echo "ERROR: Transfer failed"
    exit 1
}

echo ""
echo "=== Transfer complete ==="
echo "Verify on Jetson:"
echo "  ssh ${JETSON_USER}@${JETSON_HOST} \"ls -lh ${DEST_DIR}\""
