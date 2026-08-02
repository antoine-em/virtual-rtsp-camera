#!/usr/bin/env bash
set -euo pipefail

# Health check script for synthetic RTSP deployment
# Run ON the Jetson AGX Orin

echo "=== Synthetic RTSP Health Check ==="
PASS=0
FAIL=0

check() {
    local desc="$1"
    local result="$2"
    if [[ "$result" == "pass" ]]; then
        echo "  [PASS] $desc"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL] $desc: $result"
        FAIL=$((FAIL + 1))
    fi
}

# Check MediaMTX is running
echo ""
echo "--- MediaMTX Service ---"
if pgrep -f mediamtx &>/dev/null; then
    check "MediaMTX process is running" "pass"
else
    check "MediaMTX process is running" "fail (not found)"
fi

# Check RTSP port 8554 is listening
if command -v ss &>/dev/null; then
    if ss -tlnp 2>/dev/null | grep -q ':8554 '; then
        check "RTSP port 8554 listening" "pass"
    else
        check "RTSP port 8554 listening" "fail"
    fi
elif command -v netstat &>/dev/null; then
    if netstat -tlnp 2>/dev/null | grep -q ':8554 '; then
        check "RTSP port 8554 listening" "pass"
    else
        check "RTSP port 8554 listening" "fail"
    fi
else
    check "RTSP port 8554 listening" "fail (no ss/netstat)"
fi

# Load stream list
STREAM_CONFIG="${STREAM_CONFIG:-/app/streams.yaml}"
if [[ ! -f "$STREAM_CONFIG" ]]; then
    echo "[WARN] Stream config not found: $STREAM_CONFIG"
else
    echo ""
    echo "--- Stream Health ---"

    # Get stream names
    STREAM_NAMES=$(python3 -c "
import yaml, sys
data = yaml.safe_load(open('$STREAM_CONFIG'))
for s in data.get('streams', []):
    if s.get('enabled', True):
        print(s['name'])
" 2>/dev/null)

    if [[ -z "$STREAM_NAMES" ]]; then
        check "Streams configured" "fail (no enabled streams)"
    else
        while IFS= read -r stream_name; do
            # Check if path is available via MediaMTX API
            if curl -sf "http://127.0.0.1:9997/v3/paths/$stream_name" &>/dev/null; then
                check "Stream '$stream_name' available via API" "pass"
            else
                check "Stream '$stream_name' available via API" "fail"
            fi

            # Check with ffprobe if available
            if command -v ffprobe &>/dev/null; then
                PROBE_OUTPUT=$(ffprobe -v quiet -show_streams "rtsp://127.0.0.1:8554/$stream_name" 2>&1)
                if echo "$PROBE_OUTPUT" | grep -q "codec_type=video"; then
                    check "Stream '$stream_name' readable by ffprobe" "pass"
                else
                    check "Stream '$stream_name' readable by ffprobe" "fail"
                fi
            else
                check "Stream '$stream_name' readable by ffprobe" "fail (ffprobe not found)"
            fi
        done <<< "$STREAM_NAMES"
    fi
fi

echo ""
TOTAL=$((PASS + FAIL))
echo "Results: $PASS/$TOTAL passed, $FAIL failed"

if [[ $FAIL -gt 0 ]]; then
    echo "=== Health check FAILED ==="
    exit 1
fi
echo "=== Health check PASSED ==="
