"""Validation of the Jetson deployment manifest (jetson/streams.yaml).

These tests guard the deployment artifact itself. Behaviour of the publisher
command builder is covered by tests/test_ffmpeg_builder.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from vcam.config import load_stack
from vcam.ffmpeg import build_publish_command
from vcam.models import StreamMode

STREAMS_PATH = Path(__file__).resolve().parent.parent / "jetson" / "streams.yaml"


@pytest.fixture(scope="module")
def manifest() -> dict:
    if not STREAMS_PATH.is_file():
        pytest.skip(f"streams manifest not found: {STREAMS_PATH}")
    return yaml.safe_load(STREAMS_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# manifest structure
# ---------------------------------------------------------------------------


def test_manifest_has_streams(manifest: dict) -> None:
    assert isinstance(manifest.get("streams"), list)
    assert len(manifest["streams"]) >= 4


def test_streams_have_required_fields(manifest: dict) -> None:
    for stream in manifest["streams"]:
        assert stream.get("name"), f"stream missing 'name': {stream}"
        assert stream.get("source"), f"stream missing 'source': {stream}"
        assert isinstance(stream.get("enabled", True), bool)


def test_stream_names_are_unique(manifest: dict) -> None:
    names = [stream["name"] for stream in manifest["streams"]]
    assert len(names) == len(set(names)), f"duplicate stream names: {names}"


def test_stream_offsets_are_non_negative(manifest: dict) -> None:
    for stream in manifest["streams"]:
        offset = stream.get("offset_seconds", 0)
        assert offset >= 0, f"{stream['name']} has a negative offset: {offset}"


def test_stream_sources_are_absolute(manifest: dict) -> None:
    for stream in manifest["streams"]:
        assert stream["source"].startswith(
            "/"
        ), f"{stream['name']} has a non-absolute source: {stream['source']}"


# ---------------------------------------------------------------------------
# the manifest must be usable by the CLI
# ---------------------------------------------------------------------------


def test_manifest_loads_as_a_camera_stack() -> None:
    if not STREAMS_PATH.is_file():
        pytest.skip(f"streams manifest not found: {STREAMS_PATH}")

    stack = load_stack(STREAMS_PATH)
    assert len(stack.cameras) >= 4
    # offset_seconds is mapped onto start_offset by the legacy shim.
    assert any(camera.start_offset > 0 for camera in stack.cameras)


def test_manifest_produces_looping_realtime_publishers() -> None:
    if not STREAMS_PATH.is_file():
        pytest.skip(f"streams manifest not found: {STREAMS_PATH}")

    stack = load_stack(STREAMS_PATH)
    for camera in stack.enabled_cameras:
        camera.mode = StreamMode.COPY  # sources are not present on this machine
        cmd = build_publish_command(camera, stack.publish_url(camera))

        assert cmd[cmd.index("-stream_loop") + 1] == "-1"
        assert "-re" in cmd
        assert "-an" in cmd
        assert cmd[cmd.index("-c:v") + 1] == "copy"
        assert cmd[cmd.index("-rtsp_transport") + 1] == "tcp"
        assert cmd[-1] == f"rtsp://127.0.0.1:8554/{camera.name}"
