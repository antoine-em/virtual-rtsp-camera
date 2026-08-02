#!/usr/bin/env bash
set -euo pipefail

# GStreamer headless test for synthetic RTSP streams
# Usage: jetson/test-gstreamer.sh [stream-name] [duration]

STREAM_NAME="${1:-toll-overview-day}"
DURATION="${2:-10}"
RTSP_URL="rtsp://127.0.0.1:8554/$STREAM_NAME"

echo "=== GStreamer RTSP Test ==="
echo "Stream: $STREAM_NAME"
echo "URL:    $RTSP_URL"
echo "Duration: ${DURATION}s"
echo ""

# Check gstreamer
if ! command -v gst-launch-1.0 &>/dev/null; then
    echo "ERROR: gst-launch-1.0 not found"
    exit 1
fi

echo "--- Launching pipeline ---"
# Try Jetson hardware decoder first
if command -v nvv4l2decoder &>/dev/null && command -v nv3dsink &>/dev/null; then
    echo "  Using NVIDIA hardware decoder (nvv4l2decoder + nv3dsink)"
    PIPELINE="rtspsrc location=$RTSP_URL ! rtph264depay ! h264parse ! nvv4l2decoder ! nv3dsink sync=false"
else
    echo "  Using software decoder (avdec_h264 + fakesink)"
    PIPELINE="rtspsrc location=$RTSP_URL ! rtph264depay ! h264parse ! avdec_h264 ! fakesink"
fi

echo "  Pipeline: $PIPELINE"
echo ""

# Run pipeline
ERROR_OUTPUT=$($PIPELINE 2>&1 || true)
EXIT_CODE=$?

echo "--- Results ---"
echo "Exit code: $EXIT_CODE"

# Count frames received
if echo "$ERROR_OUTPUT" | grep -qi "elapsed.*seconds"; then
    echo "  Pipeline ran successfully"
    echo ""
    echo "$ERROR_OUTPUT" | tail -5
else
    echo "  WARNING: Pipeline may have failed"
    echo "$ERROR_OUTPUT" | tail -10
fi

if [[ $EXIT_CODE -eq 0 ]]; then
    echo ""
    echo "=== GStreamer test PASSED ==="
else
    echo ""
    echo "=== GStreamer test FAILED ==="
    exit 1
fi
