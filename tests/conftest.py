"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from vcam.models import CameraSpec, CameraStack
from vcam.probe import MediaInfo


@pytest.fixture
def video_file(tmp_path: Path) -> Path:
    """A placeholder file that exists on disk (content is never decoded in unit tests)."""
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00" * 64)
    return path


@pytest.fixture
def h264_info(video_file: Path) -> MediaInfo:
    return MediaInfo(
        path=video_file,
        codec="h264",
        width=1920,
        height=1080,
        fps=30.0,
        duration=10.0,
        pix_fmt="yuv420p",
    )


@pytest.fixture
def mpeg4_info(video_file: Path) -> MediaInfo:
    return MediaInfo(
        path=video_file,
        codec="mpeg4",
        width=640,
        height=480,
        fps=15.0,
        duration=10.0,
        pix_fmt="yuv420p",
    )


@pytest.fixture
def stack(video_file: Path) -> CameraStack:
    return CameraStack(
        cameras=[
            CameraSpec(name="cam1", source=video_file),
            CameraSpec(name="cam2", source=video_file, start_offset=5),
        ]
    )
