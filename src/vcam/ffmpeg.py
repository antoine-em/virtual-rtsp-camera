"""Build the ffmpeg command that publishes a camera into MediaMTX."""

from __future__ import annotations

from typing import Optional

from .models import RTSP_PASSTHROUGH_CODECS, CameraSpec, StreamMode
from .probe import MediaInfo

SOFTWARE_ENCODERS = frozenset({"libx264", "libx265"})


def resolve_mode(camera: CameraSpec, info: Optional[MediaInfo]) -> StreamMode:
    """Turn ``auto`` into a concrete mode using the probed source codec.

    Falls back to ``copy`` when the source could not be probed: passthrough is the
    cheapest option and any incompatibility surfaces immediately as a publisher error.
    """
    if camera.mode is not StreamMode.AUTO:
        return camera.mode
    if info is None or info.codec is None:
        return StreamMode.COPY
    if info.codec.lower() in RTSP_PASSTHROUGH_CODECS:
        return StreamMode.COPY
    return StreamMode.TRANSCODE


def _needs_filters(camera: CameraSpec) -> bool:
    return camera.video.resolution is not None or camera.video.fps is not None


def build_publish_command(
    camera: CameraSpec,
    target_url: str,
    *,
    info: Optional[MediaInfo] = None,
    ffmpeg: str = "ffmpeg",
    log_level: str = "warning",
) -> list[str]:
    """Return the full ffmpeg argv publishing *camera* to *target_url*."""
    mode = resolve_mode(camera, info)

    cmd: list[str] = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", log_level]

    # --- input options (order matters: these must precede -i) -----------------
    if camera.loop:
        cmd += ["-stream_loop", "-1"]
    if camera.realtime:
        cmd += ["-re"]
        if camera.start_offset:
            # The seek below is an *output* option, so ffmpeg has to read and
            # discard the skipped head. Burst through it instead of letting -re
            # pace it in real time, which would delay the stream by start_offset.
            cmd += ["-readrate_initial_burst", _format_seconds(camera.start_offset + 1)]
    cmd += ["-fflags", "+genpts"]
    cmd += ["-i", str(camera.source)]

    # Seeking on the output rather than the input. An input -ss combined with
    # -stream_loop restarts timestamps at the seek point on every loop, which
    # makes DTS run backwards and floods the log with "Non-monotonic DTS".
    # Seeking on the output skips the head exactly once, so later loops replay
    # the whole file and the feed simply stays phase-shifted from its peers.
    if camera.start_offset:
        cmd += ["-ss", _format_seconds(camera.start_offset)]

    # --- video ----------------------------------------------------------------
    if mode is StreamMode.COPY:
        cmd += ["-c:v", "copy"]
    else:
        if _needs_filters(camera):
            cmd += ["-vf", _build_filters(camera)]
        encoder = camera.video.ffmpeg_encoder
        cmd += ["-c:v", encoder]
        if encoder in SOFTWARE_ENCODERS:
            cmd += ["-preset", camera.video.preset, "-tune", "zerolatency"]
        if camera.video.bitrate:
            bitrate = camera.video.bitrate
            cmd += ["-b:v", bitrate, "-maxrate", bitrate, "-bufsize", _double_bitrate(bitrate)]
        if camera.video.gop:
            gop = str(camera.video.gop)
            cmd += ["-g", gop, "-keyint_min", gop, "-sc_threshold", "0"]
        cmd += ["-pix_fmt", "yuv420p"]

    # --- audio ----------------------------------------------------------------
    if camera.audio:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "44100"]
    else:
        cmd += ["-an"]

    # --- output ---------------------------------------------------------------
    cmd += ["-f", "rtsp", "-rtsp_transport", camera.transport.value, target_url]
    return cmd


def _build_filters(camera: CameraSpec) -> str:
    filters: list[str] = []
    size = camera.video.scale_size()
    if size is not None:
        filters.append(f"scale={size[0]}:{size[1]}")
    if camera.video.fps is not None:
        filters.append(f"fps={_format_number(camera.video.fps)}")
    return ",".join(filters)


def _double_bitrate(bitrate: str) -> str:
    """Buffer size = 2x target bitrate, preserving the k/M suffix."""
    suffix = ""
    value = bitrate
    if bitrate and bitrate[-1] in "kKmM":
        suffix = bitrate[-1]
        value = bitrate[:-1]
    try:
        doubled = float(value) * 2
    except ValueError:
        return bitrate
    return f"{_format_number(doubled)}{suffix}"


def _format_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def _format_seconds(value: float) -> str:
    return _format_number(value)
