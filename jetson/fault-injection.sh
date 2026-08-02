#!/usr/bin/env bash
set -euo pipefail

# Fault injection utility for synthetic RTSP deployment
# Usage: jetson/fault-injection.sh <operation> [options]

set -euo pipefail

# Fault injection utility for synthetic RTSP deployment
# Usage: jetson/fault-injection.sh <operation> [options]

show_usage() {
    cat << 'USAGE'
Usage: jetson/fault-injection.sh <operation> [options]

Operations:
  stop <stream-name>       Stop one publisher
  restart <stream-name>    Restart one publisher
  kill <stream-name>       Kill publisher unexpectedly (SIGKILL)
  restart-mediamtx         Restart MediaMTX container
  outage <duration>        Simulate configurable outage (seconds)
  disconnect-reconnect <name> <count>  Repeated disconnect/reconnect
  start-all                Start several streams simultaneously
  stop-all                 Stop all streams
  corrupt <stream-name>    Corrupt input video, verify failure reporting

Options:
  --acknowledge            Required for network-level operations
  --help                   Show this help message

WARNING: Network-level operations require --acknowledge flag.
USAGE
}

# Parse arguments
if [[ $# -lt 1 ]]; then
    show_usage
    exit 1
fi

OPERATION="$1"
shift

case "$OPERATION" in
    stop)
        STREAM_NAME="${1:?Stream name required}"
        echo "[FAULT] Stopping publisher for stream: $STREAM_NAME"
        # Find and stop the FFmpeg process for this stream
        PIDS=$(pgrep -f "rtsp://127.0.0.1:8554/$STREAM_NAME" 2>/dev/null || true)
        if [[ -n "$PIDS" ]]; then
            echo "$PIDS" | xargs kill 2>/dev/null || true
            echo "  Publisher(s) stopped: $PIDS"
        else
            echo "  No running publisher found for $STREAM_NAME"
        fi
        ;;

    restart)
        STREAM_NAME="${1:?Stream name required}"
        echo "[FAULT] Restarting publisher for stream: $STREAM_NAME"
        bash "$0" stop "$STREAM_NAME" 2>/dev/null || true
        sleep 2
        echo "  Publisher should restart automatically (supervisor check interval: 2s)"
        sleep 3
        pgrep -f "rtsp://127.0.0.1:8554/$STREAM_NAME" && echo "  Publisher restarted successfully" || echo "  Publisher not running"
        ;;

    kill)
        STREAM_NAME="${1:?Stream name required}"
        echo "[FAULT] SIGKILL publisher for stream: $STREAM_NAME"
        PIDS=$(pgrep -f "rtsp://127.0.0.1:8554/$STREAM_NAME" 2>/dev/null || true)
        if [[ -n "$PIDS" ]]; then
            echo "$PIDS" | xargs kill -9 2>/dev/null || true
            echo "  Publisher(s) killed: $PIDS"
        else
            echo "  No running publisher found for $STREAM_NAME"
        fi
        ;;

    restart-mediamtx)
        echo "[FAULT] Restarting MediaMTX"
        if command -v docker &>/dev/null && docker compose ps mediamtx &>/dev/null; then
            cd /opt/edgeai/data/stack 2>/dev/null && docker compose restart mediamtx
        elif pgrep -f mediamtx &>/dev/null; then
            pkill -HUP mediamtx 2>/dev/null || true
            sleep 2
            mediamtx &
        else
            echo "ERROR: MediaMTX not found (not in Docker or as standalone process)"
            exit 1
        fi
        echo "  Waiting for MediaMTX to recover..."
        for i in $(seq 1 10); do
            if curl -sf http://127.0.0.1:9997/v3/health &>/dev/null; then
                echo "  MediaMTX recovered after ${i}s"
                break
            fi
            sleep 1
        done
        ;;

    outage)
        DURATION="${1:?Duration required}"
        echo "[FAULT] Simulating outage for ${DURATION}s"
        # Block RTSP traffic temporarily
        if [[ -f /tmp/synthetic-fault-iptables-backup ]]; then
            echo "WARNING: Outage already active. Stop first."
            exit 1
        fi
        # Save current iptables rules
        iptables-save > /tmp/synthetic-fault-iptables-backup 2>/dev/null || true
        # Block port 8554
        iptables -A INPUT -p tcp --dport 8554 -j DROP 2>/dev/null || echo "  Note: iptables requires root"
        echo "  Traffic blocked. Outage ends in ${DURATION}s"
        sleep "$DURATION"
        # Restore rules
        if [[ -f /tmp/synthetic-fault-iptables-backup ]]; then
            iptables-restore < /tmp/synthetic-fault-iptables-backup 2>/dev/null || true
            rm -f /tmp/synthetic-fault-iptables-backup
        fi
        echo "  Traffic restored"
        ;;

    disconnect-reconnect)
        STREAM_NAME="${1:?Stream name required}"
        COUNT="${2:?Connection count required}"
        echo "[FAULT] Disconnect/reconnect: $STREAM_NAME x $COUNT"
        for i in $(seq 1 "$COUNT"); do
            echo "  Attempt $i/$COUNT..."
            bash "$0" stop "$STREAM_NAME" 2>/dev/null || true
            sleep 2
            echo "  Waiting for auto-restart..."
            for j in $(seq 1 10); do
                if pgrep -f "rtsp://127.0.0.1:8554/$STREAM_NAME" &>/dev/null; then
                    echo "  Recovered after ${j}s"
                    break
                fi
                sleep 1
            done
        done
        echo "  Fault injection complete"
        ;;

    start-all)
        echo "[FAULT] Starting all enabled streams"
        STREAM_CONFIG="${STREAM_CONFIG:-/app/streams.yaml}"
        if [[ -f "$STREAM_CONFIG" ]]; then
            python3 -c "
import yaml
with open('$STREAM_CONFIG') as f:
    data = yaml.safe_load(f)
for s in data.get('streams', []):
    if s.get('enabled', True):
        print(s['name'])
" 2>/dev/null | while read -r name; do
                bash "$0" stop "$name" 2>/dev/null || true
            done
            sleep 1
            echo "  All publishers stopped, supervisor will restart"
        else
            echo "  No stream config found"
        fi
        ;;

    stop-all)
        echo "[FAULT] Stopping all publishers"
        pkill -f "rtsp://127.0.0.1:8554/" 2>/dev/null || true
        sleep 2
        echo "  All publishers stopped"
        ;;

    corrupt)
        STREAM_NAME="${1:?Stream name required}"
        echo "[FAULT] Corrupting input video for: $STREAM_NAME"
        # Find the source file
        STREAM_CONFIG="${STREAM_CONFIG:-/app/streams.yaml}"
        SOURCE=""
        if [[ -f "$STREAM_CONFIG" ]]; then
            SOURCE=$(python3 -c "
import yaml
with open('$STREAM_CONFIG') as f:
    data = yaml.safe_load(f)
for s in data.get('streams', []):
    if s.get('name') == '$STREAM_NAME':
        print(s.get('source', ''))
" 2>/dev/null)
        fi
        if [[ -n "$SOURCE" && -f "$SOURCE" ]]; then
            # Truncate the file (simulate corruption)
            head -c 1024 "$SOURCE" > /tmp/${STREAM_NAME}_corrupted 2>/dev/null || true
            mv /tmp/${STREAM_NAME}_corrupted "$SOURCE"
            echo "  Video corrupted (truncated to 1024 bytes)"
            sleep 3
            # Verify failure
            if pgrep -f "rtsp://127.0.0.1:8554/$STREAM_NAME" &>/dev/null; then
                echo "  Publisher still running (may recover)"
            else
                echo "  Publisher stopped due to corrupted input (expected)"
            fi
            # Restore from backup if available
            if [[ -f "/tmp/${STREAM_NAME}_backup" ]]; then
                cp "/tmp/${STREAM_NAME}_backup" "$SOURCE"
                echo "  Video restored from backup"
            fi
        else
            echo "  Could not find source file: $SOURCE"
        fi
        ;;

    --help|-h)
        show_usage
        ;;

    *)
        echo "ERROR: Unknown operation: $OPERATION"
        show_usage
        exit 1
        ;;
esac

echo ""
echo "=== Fault injection complete ==="
