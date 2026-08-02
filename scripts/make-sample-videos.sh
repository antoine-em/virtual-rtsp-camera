#!/usr/bin/env bash
# Generate sample clips for trying out `vcam` without real camera footage.
#
#   ./scripts/make-sample-videos.sh [output-dir]
#
# Each clip uses a different lavfi pattern so the feeds are easy to tell apart,
# and a 1-second GOP so `start_offset` works in copy mode (see README).
set -euo pipefail

out_dir="${1:-videos}"
duration="${DURATION:-20}"
size="${SIZE:-1280x720}"
fps="${FPS:-25}"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg not found. Install it with: brew install ffmpeg" >&2
  exit 1
fi

mkdir -p "$out_dir"

# name:pattern -- testsrc and testsrc2 draw a running seconds counter, which
# makes looping and per-camera offsets visible at a glance.
clips=(
  "cam1:testsrc"
  "cam2:testsrc2"
  "cam3:smptebars"
  "cam4:rgbtestsrc"
)

for clip in "${clips[@]}"; do
  name="${clip%%:*}"
  pattern="${clip##*:}"
  target="$out_dir/$name.mp4"

  ffmpeg -hide_banner -loglevel error -y \
    -f lavfi -i "${pattern}=size=${size}:rate=${fps}:duration=${duration}" \
    -c:v libx264 -preset veryfast -pix_fmt yuv420p \
    -g "$fps" -keyint_min "$fps" -sc_threshold 0 \
    "$target"

  echo "wrote $target  (${pattern}, ${size}@${fps}fps, ${duration}s)"
done

echo
echo "Next: uv run vcam run --source $out_dir/cam1.mp4"
