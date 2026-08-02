#!/usr/bin/env bash
set -euo pipefail

# Jetson installation script
# Run ON the Jetson AGX Orin

echo "=== Synthetic Toll-Gate RTSP: Jetson Install ==="

# Check Docker
if ! command -v docker &>/dev/null; then
    echo "ERROR: Docker not installed"
    exit 1
fi

# Check Docker Compose
if ! docker compose version &>/dev/null; then
    echo "ERROR: Docker Compose not available"
    exit 1
fi

DEPLOY_DIR="${DEPLOY_DIR:-/opt/edgeai/data/stack}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Deploy directory: $DEPLOY_DIR"

# Create directories
mkdir -p "$DEPLOY_DIR"

# Copy config files
echo "Copying configuration files..."
cp "$SCRIPT_DIR/compose.yaml" "$DEPLOY_DIR/docker-compose.yml"
cp "$SCRIPT_DIR/mediamtx.yml" "$DEPLOY_DIR/mediamtx.yml"

# Create data directory
DATA_DIR="${DATA_DIR:-/home/edgeai/synthetic-data}"
mkdir -p "$DATA_DIR"

# Start services
echo ""
echo "Starting services..."
cd "$DEPLOY_DIR"
docker compose up -d

# Wait for health
echo ""
echo "Waiting for services to become healthy..."
sleep 10

# Verify
echo ""
echo "=== Deployment Verification ==="
docker compose ps

# Check MediaMTX health
if curl -sf http://127.0.0.1:9997/v3/health &>/dev/null; then
    echo "[OK] MediaMTX health check passed"
else
    echo "[WARN] MediaMTX health check failed"
fi

echo ""
echo "=== Installation complete ==="
echo "RTSP server: rtsp://<JETSON_IP>:8554/<stream-name>"
echo "Manage with: cd $DEPLOY_DIR && docker compose [up|down|logs]"
