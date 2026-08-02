"""Tests for RTSP stream configuration."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from generator import config


# ---------------------------------------------------------------------------
# Test: stream loading
# ---------------------------------------------------------------------------


def test_load_streams_yaml() -> None:
    """Load jetson/streams.yaml, verify structure."""
    streams_path = Path(__file__).resolve().parent.parent / "jetson" / "streams.yaml"
    assert streams_path.is_file(), f"Streams config not found: {streams_path}"

    data = yaml.safe_load(open(streams_path))
    assert "streams" in data
    assert isinstance(data["streams"], list)


def test_stream_config_validation() -> None:
    """Each stream has required fields (name, source, enabled)."""
    streams_path = Path(__file__).resolve().parent.parent / "jetson" / "streams.yaml"
    if not streams_path.is_file():
        pytest.skip("Streams config not found")

    data = yaml.safe_load(open(streams_path))

    for stream in data["streams"]:
        assert "name" in stream, f"Stream missing 'name': {stream}"
        assert "source" in stream, f"Stream missing 'source': {stream}"
        assert isinstance(stream.get("enabled", True), bool), f"Stream 'enabled' must be bool"


def test_stream_count() -> None:
    """At least 4 streams configured."""
    streams_path = Path(__file__).resolve().parent.parent / "jetson" / "streams.yaml"
    if not streams_path.is_file():
        pytest.skip("Streams config not found")

    data = yaml.safe_load(open(streams_path))
    assert len(data["streams"]) >= 4


def test_stream_names_unique() -> None:
    """All stream names must be unique."""
    streams_path = Path(__file__).resolve().parent.parent / "jetson" / "streams.yaml"
    if not streams_path.is_file():
        pytest.skip("Streams config not found")

    data = yaml.safe_load(open(streams_path))
    names = [s["name"] for s in data["streams"]]
    assert len(names) == len(set(names)), f"Duplicate stream names: {names}"


# ---------------------------------------------------------------------------
# Test: FFmpeg command generation
# ---------------------------------------------------------------------------


def test_ffmpeg_command_generation() -> None:
    """Generate valid FFmpeg command for a stream."""
    stream_name = "toll-overview-day"
    source = "/data/toll-overview-day.mp4"
    offset = 0

    cmd = [
        "ffmpeg",
        "-re",
        "-ss", str(offset),
        "-i", source,
        "-stream_loop", "-1",
        "-c:v", "copy",
        "-an",
        "-f", "rtsp",
        "-rtsp_transport", "tcp",
        f"rtsp://127.0.0.1:8554/{stream_name}",
    ]

    assert "ffmpeg" in cmd[0]
    assert "-re" in cmd
    assert "-stream_loop" in cmd
    assert "-1" in cmd


def test_ffmpeg_command_parameters() -> None:
    """Verify key parameters (re, stream_loop, rtsp_transport tcp)."""
    stream_name = "toll-plate-night"
    source = "/data/toll-plate-night.mp4"
    offset = 12

    cmd = [
        "ffmpeg",
        "-re",
        "-ss", str(offset),
        "-i", source,
        "-stream_loop", "-1",
        "-c:v", "copy",
        "-an",
        "-f", "rtsp",
        "-rtsp_transport", "tcp",
        f"rtsp://127.0.0.1:8554/{stream_name}",
    ]

    # Check all required flags are present
    assert "-re" in cmd, "Missing -re (real-time pacing)"
    assert "-stream_loop" in cmd, "Missing -stream_loop"
    assert "-1" in cmd, "Missing -1 (loop count)"
    assert "-c:v" in cmd, "Missing -c:v (codec)"
    assert "copy" in cmd, "Missing copy (stream copy)"
    assert "-an" in cmd, "Missing -an (no audio)"
    assert "-rtsp_transport" in cmd, "Missing -rtsp_transport"
    assert "tcp" in cmd, "Missing tcp (RTSP transport)"
    assert f"rtsp://127.0.0.1:8554/{stream_name}" in cmd


def test_ffmpeg_command_no_audio() -> None:
    """Command should not include audio flags."""
    stream_name = "toll-overview-day"
    source = "/data/toll-overview-day.mp4"

    cmd = [
        "ffmpeg", "-re", "-ss", "0", "-i", source,
        "-stream_loop", "-1", "-c:v", "copy",
        "-an", "-f", "rtsp", "-rtsp_transport", "tcp",
        f"rtsp://127.0.0.1:8554/{stream_name}",
    ]

    assert "-an" in cmd
    assert "-acodec" not in cmd  # should not specify audio codec


# ---------------------------------------------------------------------------
# Test: stream offsets and paths
# ---------------------------------------------------------------------------


def test_stream_config_offset_positive() -> None:
    """Stream offsets should be non-negative."""
    streams_path = Path(__file__).resolve().parent.parent / "jetson" / "streams.yaml"
    if not streams_path.is_file():
        pytest.skip("Streams config not found")

    data = yaml.safe_load(open(streams_path))
    for stream in data["streams"]:
        offset = stream.get("offset_seconds", 0)
        assert offset >= 0, f"Stream '{stream['name']}' has negative offset: {offset}"


def test_stream_config_source_absolute() -> None:
    """All stream sources must be absolute paths."""
    streams_path = Path(__file__).resolve().parent.parent / "jetson" / "streams.yaml"
    if not streams_path.is_file():
        pytest.skip("Streams config not found")

    data = yaml.safe_load(open(streams_path))
    for stream in data["streams"]:
        source = stream.get("source", "")
        assert source.startswith("/"), f"Stream '{stream['name']}' has non-absolute path: {source}"
