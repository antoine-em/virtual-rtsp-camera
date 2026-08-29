"""Tests for the ffmpeg publisher command builder."""

from __future__ import annotations

from pathlib import Path

import pytest

from vcam.ffmpeg import build_publish_command, resolve_mode
from vcam.models import CameraSpec, StreamMode, Transport, VideoCodec, VideoSettings
from vcam.probe import MediaInfo

URL = "rtsp://127.0.0.1:8554/cam1"


def index_of(cmd: list[str], value: str) -> int:
    return cmd.index(value)


# ---------------------------------------------------------------------------
# mode resolution
# ---------------------------------------------------------------------------


def test_auto_copies_h264_source(video_file: Path, h264_info: MediaInfo) -> None:
    camera = CameraSpec(name="cam1", source=video_file)
    assert resolve_mode(camera, h264_info) is StreamMode.COPY


def test_auto_transcodes_non_rtsp_codec(video_file: Path, mpeg4_info: MediaInfo) -> None:
    camera = CameraSpec(name="cam1", source=video_file)
    assert resolve_mode(camera, mpeg4_info) is StreamMode.TRANSCODE


def test_auto_copies_hevc_source(video_file: Path) -> None:
    camera = CameraSpec(name="cam1", source=video_file)
    info = MediaInfo(path=video_file, codec="hevc")
    assert resolve_mode(camera, info) is StreamMode.COPY


def test_auto_falls_back_to_copy_without_probe(video_file: Path) -> None:
    camera = CameraSpec(name="cam1", source=video_file)
    assert resolve_mode(camera, None) is StreamMode.COPY


def test_explicit_mode_wins_over_probe(video_file: Path, h264_info: MediaInfo) -> None:
    camera = CameraSpec(name="cam1", source=video_file, mode=StreamMode.TRANSCODE)
    assert resolve_mode(camera, h264_info) is StreamMode.TRANSCODE


# ---------------------------------------------------------------------------
# copy mode
# ---------------------------------------------------------------------------


def test_copy_mode_command(video_file: Path, h264_info: MediaInfo) -> None:
    camera = CameraSpec(name="cam1", source=video_file)
    cmd = build_publish_command(camera, URL, info=h264_info)

    assert cmd[0] == "ffmpeg"
    assert cmd[-1] == URL
    assert "-c:v" in cmd and cmd[index_of(cmd, "-c:v") + 1] == "copy"
    assert "-an" in cmd
    assert cmd[index_of(cmd, "-f") + 1] == "rtsp"
    assert cmd[index_of(cmd, "-rtsp_transport") + 1] == "tcp"
    # No encoder flags should leak into a passthrough command.
    assert "-preset" not in cmd
    assert "-b:v" not in cmd
    assert "-vf" not in cmd


def test_loop_and_realtime_flags_precede_input(video_file: Path, h264_info: MediaInfo) -> None:
    """-stream_loop and -re are input options; after -i ffmpeg would ignore them."""
    camera = CameraSpec(name="cam1", source=video_file)
    cmd = build_publish_command(camera, URL, info=h264_info)

    assert cmd[index_of(cmd, "-stream_loop") + 1] == "-1"
    assert index_of(cmd, "-stream_loop") < index_of(cmd, "-i")
    assert index_of(cmd, "-re") < index_of(cmd, "-i")


def test_loop_can_be_disabled(video_file: Path, h264_info: MediaInfo) -> None:
    camera = CameraSpec(name="cam1", source=video_file, loop=False)
    cmd = build_publish_command(camera, URL, info=h264_info)
    assert "-stream_loop" not in cmd


def test_realtime_can_be_disabled(video_file: Path, h264_info: MediaInfo) -> None:
    camera = CameraSpec(name="cam1", source=video_file, realtime=False)
    cmd = build_publish_command(camera, URL, info=h264_info)
    assert "-re" not in cmd


def test_start_offset_seeks_after_input(video_file: Path, h264_info: MediaInfo) -> None:
    """Output-side seek: an input -ss restarts timestamps on every loop."""
    camera = CameraSpec(name="cam1", source=video_file, start_offset=12)
    cmd = build_publish_command(camera, URL, info=h264_info)

    assert cmd[index_of(cmd, "-ss") + 1] == "12"
    assert index_of(cmd, "-ss") > index_of(cmd, "-i")


def test_start_offset_bursts_past_the_skipped_head(video_file: Path, h264_info: MediaInfo) -> None:
    """Without the burst, -re would pace the discarded head in real time."""
    camera = CameraSpec(name="cam1", source=video_file, start_offset=12)
    cmd = build_publish_command(camera, URL, info=h264_info)

    assert cmd[index_of(cmd, "-readrate_initial_burst") + 1] == "13"
    assert index_of(cmd, "-readrate_initial_burst") < index_of(cmd, "-i")


def test_burst_is_omitted_without_realtime(video_file: Path, h264_info: MediaInfo) -> None:
    camera = CameraSpec(name="cam1", source=video_file, start_offset=12, realtime=False)
    assert "-readrate_initial_burst" not in build_publish_command(camera, URL, info=h264_info)


def test_burst_is_omitted_without_offset(video_file: Path, h264_info: MediaInfo) -> None:
    camera = CameraSpec(name="cam1", source=video_file)
    assert "-readrate_initial_burst" not in build_publish_command(camera, URL, info=h264_info)


def test_zero_offset_omits_seek(video_file: Path, h264_info: MediaInfo) -> None:
    camera = CameraSpec(name="cam1", source=video_file)
    assert "-ss" not in build_publish_command(camera, URL, info=h264_info)


def test_fractional_offset_is_formatted(video_file: Path, h264_info: MediaInfo) -> None:
    camera = CameraSpec(name="cam1", source=video_file, start_offset=2.5)
    cmd = build_publish_command(camera, URL, info=h264_info)
    assert cmd[index_of(cmd, "-ss") + 1] == "2.5"


def test_udp_transport(video_file: Path, h264_info: MediaInfo) -> None:
    camera = CameraSpec(name="cam1", source=video_file, transport=Transport.UDP)
    cmd = build_publish_command(camera, URL, info=h264_info)
    assert cmd[index_of(cmd, "-rtsp_transport") + 1] == "udp"


# ---------------------------------------------------------------------------
# transcode mode
# ---------------------------------------------------------------------------


def test_transcode_applies_video_settings(video_file: Path, mpeg4_info: MediaInfo) -> None:
    camera = CameraSpec(
        name="cam1",
        source=video_file,
        mode=StreamMode.TRANSCODE,
        video=VideoSettings(resolution="1280x720", fps=15, bitrate="2M", gop=30),
    )
    cmd = build_publish_command(camera, URL, info=mpeg4_info)

    assert cmd[index_of(cmd, "-vf") + 1] == "scale=1280:720,fps=15"
    assert cmd[index_of(cmd, "-c:v") + 1] == "libx264"
    assert cmd[index_of(cmd, "-b:v") + 1] == "2M"
    assert cmd[index_of(cmd, "-maxrate") + 1] == "2M"
    assert cmd[index_of(cmd, "-bufsize") + 1] == "4M"
    assert cmd[index_of(cmd, "-g") + 1] == "30"
    assert cmd[index_of(cmd, "-keyint_min") + 1] == "30"
    assert cmd[index_of(cmd, "-pix_fmt") + 1] == "yuv420p"


def test_transcode_h265(video_file: Path, mpeg4_info: MediaInfo) -> None:
    camera = CameraSpec(
        name="cam1",
        source=video_file,
        mode=StreamMode.TRANSCODE,
        video=VideoSettings(codec=VideoCodec.H265),
    )
    cmd = build_publish_command(camera, URL, info=mpeg4_info)
    assert cmd[index_of(cmd, "-c:v") + 1] == "libx265"


def test_explicit_encoder_overrides_codec_and_skips_x264_flags(
    video_file: Path, mpeg4_info: MediaInfo
) -> None:
    camera = CameraSpec(
        name="cam1",
        source=video_file,
        mode=StreamMode.TRANSCODE,
        video=VideoSettings(codec=VideoCodec.H264, encoder="h264_nvenc"),
    )
    cmd = build_publish_command(camera, URL, info=mpeg4_info)

    assert cmd[index_of(cmd, "-c:v") + 1] == "h264_nvenc"
    assert "-preset" not in cmd
    assert "-tune" not in cmd


def test_transcode_without_filters_omits_vf(video_file: Path, mpeg4_info: MediaInfo) -> None:
    camera = CameraSpec(name="cam1", source=video_file, mode=StreamMode.TRANSCODE)
    assert "-vf" not in build_publish_command(camera, URL, info=mpeg4_info)


def test_scale_only_filter(video_file: Path, mpeg4_info: MediaInfo) -> None:
    camera = CameraSpec(
        name="cam1",
        source=video_file,
        mode=StreamMode.TRANSCODE,
        video=VideoSettings(resolution="640x360"),
    )
    cmd = build_publish_command(camera, URL, info=mpeg4_info)
    assert cmd[index_of(cmd, "-vf") + 1] == "scale=640:360"


@pytest.mark.parametrize(
    ("bitrate", "bufsize"),
    [("2M", "4M"), ("800k", "1600k"), ("1.5M", "3M"), ("500", "1000")],
)
def test_bufsize_is_double_the_bitrate(
    video_file: Path, mpeg4_info: MediaInfo, bitrate: str, bufsize: str
) -> None:
    camera = CameraSpec(
        name="cam1",
        source=video_file,
        mode=StreamMode.TRANSCODE,
        video=VideoSettings(bitrate=bitrate),
    )
    cmd = build_publish_command(camera, URL, info=mpeg4_info)
    assert cmd[index_of(cmd, "-bufsize") + 1] == bufsize


# ---------------------------------------------------------------------------
# audio
# ---------------------------------------------------------------------------


def test_audio_disabled_by_default(video_file: Path, h264_info: MediaInfo) -> None:
    camera = CameraSpec(name="cam1", source=video_file)
    cmd = build_publish_command(camera, URL, info=h264_info)
    assert "-an" in cmd
    assert "-c:a" not in cmd


def test_audio_enabled_encodes_aac(video_file: Path, h264_info: MediaInfo) -> None:
    camera = CameraSpec(name="cam1", source=video_file, audio=True)
    cmd = build_publish_command(camera, URL, info=h264_info)
    assert "-an" not in cmd
    assert cmd[index_of(cmd, "-c:a") + 1] == "aac"
