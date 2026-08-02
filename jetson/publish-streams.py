"""Stream publisher supervisor for synthetic toll-gate RTSP deployment.

Reads jetson/streams.yaml and manages one FFmpeg child per enabled stream.
Restart failed publishers with exponential backoff.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_STREAM_CONFIG = Path("/app/streams.yaml")
DEFAULT_DATA_DIR = Path("/data")
HEALTH_FILE = Path("/app/health.json")

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("publisher")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class StreamConfig:
    name: str
    source: str
    offset_seconds: int = 0
    enabled: bool = True


@dataclass
class PublisherState:
    stream: StreamConfig
    process: Optional[subprocess.Popen] = None
    restart_count: int = 0
    last_started: Optional[float] = None
    last_exit_code: Optional[int] = None
    pid: Optional[int] = None


# ---------------------------------------------------------------------------
# Stream loading
# ---------------------------------------------------------------------------


def load_streams(config_path: Path) -> list[StreamConfig]:
    """Load stream configurations from YAML file."""
    if not config_path.is_file():
        raise FileNotFoundError(f"Stream config not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    if not isinstance(data, dict) or "streams" not in data:
        raise ValueError(f"Invalid stream config format: missing 'streams' key")

    streams: list[StreamConfig] = []
    for stream_data in data["streams"]:
        if not isinstance(stream_data, dict):
            continue
        name = stream_data.get("name", "")
        source = stream_data.get("source", "")
        if not name or not source:
            logger.warning(f"Skipping stream with missing name/source: {stream_data}")
            continue
        streams.append(
            StreamConfig(
                name=name,
                source=source,
                offset_seconds=stream_data.get("offset_seconds", 0),
                enabled=stream_data.get("enabled", True),
            )
        )

    return streams


# ---------------------------------------------------------------------------
# FFmpeg command generation
# ---------------------------------------------------------------------------


def build_ffmpeg_cmd(stream: StreamConfig, data_dir: Path) -> list[str]:
    """Build FFmpeg command for a single stream."""
    source_path = Path(stream.source)
    if not source_path.is_absolute():
        source_path = data_dir / source_path

    cmd = [
        "ffmpeg",
        "-re",  # Real-time input pacing
        "-ss", str(stream.offset_seconds),  # Seek to offset
        "-i", str(source_path),  # Input file
        "-stream_loop", "-1",  # Infinite loop
        "-c:v", "copy",  # Stream copy (assume H.264 input)
        "-an",  # No audio
        "-f", "rtsp",  # RTSP output format
        "-rtsp_transport", "tcp",  # TCP transport
        "rtsp://127.0.0.1:8554/" + stream.name,
    ]

    return cmd


# ---------------------------------------------------------------------------
# Publisher management
# ---------------------------------------------------------------------------


class PublisherManager:
    """Manages FFmpeg publishers for all configured streams."""

    def __init__(self, streams: list[StreamConfig], data_dir: Path):
        self.streams = streams
        self.data_dir = data_dir
        self.states: dict[str, PublisherState] = {}
        self._shutdown = False
        self._backoff_base = 1.0  # seconds
        self._backoff_max = 30.0

        # Register signal handlers
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        # Initialize states for enabled streams
        for stream in streams:
            if stream.enabled:
                self.states[stream.name] = PublisherState(stream=stream)

    def _handle_signal(self, signum: int, frame: Any) -> None:
        """Handle shutdown signals."""
        logger.info(f"Received signal {signum}, shutting down...")
        self._shutdown = True

    def _start_publisher(self, state: PublisherState) -> bool:
        """Start an FFmpeg publisher process. Returns True on success."""
        stream = state.stream
        source_path = Path(stream.source)
        if not source_path.is_absolute():
            source_path = self.data_dir / source_path

        if not source_path.is_file():
            logger.error(f"Input file not found: {source_path}")
            return False

        cmd = build_ffmpeg_cmd(stream, self.data_dir)
        logger.info(f"Starting publisher: {stream.name} -> {cmd[-1]}")

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            state.process = process
            state.pid = process.pid
            state.last_started = time.time()
            state.restart_count += 1
            logger.info(
                f"Publisher started: {stream.name} (PID {process.pid}, "
                f"restart #{state.restart_count})"
            )
            return True
        except OSError as exc:
            logger.error(f"Failed to start {stream.name}: {exc}")
            return False

    def _stop_publisher(self, state: PublisherState) -> None:
        """Stop a publisher process cleanly."""
        if state.process and state.process.poll() is None:
            logger.info(f"Stopping publisher: {state.stream.name}")
            state.process.terminate()
            try:
                state.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning(f"Force killing {state.stream.name}")
                state.process.kill()
                state.process.wait()
            state.process = None
            state.pid = None

    def _check_publishers(self) -> None:
        """Check all publishers and restart failed ones."""
        for name, state in self.states.items():
            if state.process is None:
                # Process not running, try to start
                self._start_publisher(state)
                continue

            # Check if process is still running
            poll_result = state.process.poll()
            if poll_result is not None:
                state.last_exit_code = poll_result
                state.process = None
                state.pid = None

                if self._shutdown:
                    continue

                # Restart with backoff
                elapsed = time.time() - state.last_started if state.last_started else 0
                backoff = min(
                    self._backoff_base * (2 ** (state.restart_count - 1)),
                    self._backoff_max,
                )
                logger.info(
                    f"Publisher {name} exited with code {poll_result}. "
                    f"Restarting in {backoff:.1f}s..."
                )

                if elapsed < backoff:
                    time.sleep(backoff - elapsed)

                self._start_publisher(state)
            else:
                # Process still running, read some output for logging
                if state.process.stdout:
                    line = state.process.stdout.readline()
                    if line:
                        decoded = line.decode("utf-8", errors="replace").strip()
                        # Only log non-empty lines (suppress constant ffmpeg output)
                        if decoded and decoded != "At least one output file must be specified":
                            logger.debug(f"[{name}] {decoded[:100]}")

    def _write_health(self) -> None:
        """Write health information to health.json."""
        health: dict[str, Any] = {
            "timestamp": time.time(),
            "streams": {},
        }

        for name, state in self.states.items():
            health["streams"][name] = {
                "enabled": state.stream.enabled,
                "running": state.process is not None and state.process.poll() is None,
                "pid": state.pid,
                "restart_count": state.restart_count,
                "last_exit_code": state.last_exit_code,
                "last_started": state.last_started,
            }

        try:
            HEALTH_FILE.write_text(json.dumps(health, indent=2))
        except OSError as exc:
            logger.error(f"Failed to write health file: {exc}")

    def run(self) -> int:
        """Main loop. Returns exit code."""
        logger.info(f"Publisher manager starting with {len(self.states)} streams")

        # Start all publishers
        for state in self.states.values():
            self._start_publisher(state)

        check_interval = 2.0  # seconds
        health_interval = 10.0  # seconds
        last_health_check = 0.0

        while not self._shutdown:
            current_time = time.time()

            self._check_publishers()

            # Write health file periodically
            if current_time - last_health_check >= health_interval:
                self._write_health()
                last_health_check = current_time

            time.sleep(check_interval)

        # Clean shutdown
        logger.info("Shutting down publishers...")
        for state in self.states.values():
            self._stop_publisher(state)

        self._write_health()
        logger.info("Publisher manager stopped")

        return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    """Entry point."""
    config_path = Path(os.environ.get("STREAM_CONFIG", str(DEFAULT_STREAM_CONFIG)))
    data_dir = Path(os.environ.get("DATA_DIR", str(DEFAULT_DATA_DIR)))

    try:
        streams = load_streams(config_path)
    except (FileNotFoundError, ValueError) as exc:
        logger.error(f"Failed to load streams: {exc}")
        return 1

    enabled = [s for s in streams if s.enabled]
    if not enabled:
        logger.warning("No enabled streams found")
        return 0

    manager = PublisherManager(streams, data_dir)
    return manager.run()


if __name__ == "__main__":
    sys.exit(main())
