"""Tests for the camera fault simulation: ffmpeg profiles and event scheduling."""

from __future__ import annotations

from pathlib import Path

import pytest

from vcam.ffmpeg import (
    DEGRADED_BITRATE,
    DEGRADED_GOP,
    NOISE_BITRATE,
    build_publish_command,
    effective_mode,
    simulation_forces_transcode,
)
from vcam.mediamtx import ServerInstance
from vcam.models import (
    CameraSpec,
    SimulationMode,
    SimulationSpec,
    StreamMode,
    VideoSettings,
)
from vcam.probe import MediaInfo
from vcam.supervisor import CameraRuntime, ManagedProcess, SimulationScheduler

URL = "rtsp://127.0.0.1:8554/cam1"


def index_of(cmd: list[str], value: str) -> int:
    return cmd.index(value)


def filters_of(cmd: list[str]) -> str:
    return cmd[index_of(cmd, "-vf") + 1]


def camera_with(video_file: Path, **simulation: object) -> CameraSpec:
    return CameraSpec(name="cam1", source=video_file, simulation=simulation)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# defaults
# ---------------------------------------------------------------------------


def test_simulation_defaults_to_normal(video_file: Path) -> None:
    camera = CameraSpec(name="cam1", source=video_file)
    assert camera.simulation.mode is SimulationMode.NORMAL
    assert camera.simulation.is_temporal is False


def test_normal_leaves_a_passthrough_feed_untouched(
    video_file: Path, h264_info: MediaInfo
) -> None:
    camera = CameraSpec(name="cam1", source=video_file)
    cmd = build_publish_command(camera, URL, info=h264_info)

    assert simulation_forces_transcode(camera) is False
    assert effective_mode(camera, h264_info) is StreamMode.COPY
    assert cmd[index_of(cmd, "-c:v") + 1] == "copy"
    assert "-vf" not in cmd


# ---------------------------------------------------------------------------
# steady-state modes
# ---------------------------------------------------------------------------


def test_noise_forces_transcode_of_a_copyable_source(
    video_file: Path, h264_info: MediaInfo
) -> None:
    """Filters rewrite pixels, so passthrough cannot stand."""
    camera = camera_with(video_file, mode="noise")
    cmd = build_publish_command(camera, URL, info=h264_info)

    assert simulation_forces_transcode(camera) is True
    assert effective_mode(camera, h264_info) is StreamMode.TRANSCODE
    assert cmd[index_of(cmd, "-c:v") + 1] == "libx264"
    assert filters_of(cmd) == "noise=alls=30:allf=t"


def test_noise_is_bitrate_capped(video_file: Path, h264_info: MediaInfo) -> None:
    """Uncapped, noise pushes tens of Mbit/s per camera."""
    camera = camera_with(video_file, mode="noise")
    cmd = build_publish_command(camera, URL, info=h264_info)

    assert cmd[index_of(cmd, "-b:v") + 1] == NOISE_BITRATE
    assert cmd[index_of(cmd, "-maxrate") + 1] == NOISE_BITRATE


def test_noise_cap_yields_to_an_explicit_bitrate(
    video_file: Path, h264_info: MediaInfo
) -> None:
    camera = CameraSpec(
        name="cam1",
        source=video_file,
        video=VideoSettings(bitrate="8M"),
        simulation=SimulationSpec(mode=SimulationMode.NOISE),
    )
    cmd = build_publish_command(camera, URL, info=h264_info)
    assert cmd[index_of(cmd, "-b:v") + 1] == "8M"


def test_noise_level_is_applied(video_file: Path, h264_info: MediaInfo) -> None:
    camera = camera_with(video_file, mode="noise", noise_level=75)
    assert filters_of(build_publish_command(camera, URL, info=h264_info)) == (
        "noise=alls=75:allf=t"
    )


def test_blackout_pins_luma_and_neutral_chroma(
    video_file: Path, h264_info: MediaInfo
) -> None:
    """lutyuv, so an RGB-decoding source is blanked rather than tinted."""
    camera = camera_with(video_file, mode="blackout")
    assert filters_of(build_publish_command(camera, URL, info=h264_info)) == (
        "lutyuv=y=0:u=128:v=128"
    )


def test_frozen_holds_the_picture(video_file: Path, h264_info: MediaInfo) -> None:
    camera = camera_with(video_file, mode="frozen")
    assert filters_of(build_publish_command(camera, URL, info=h264_info)) == "fps=1"


def test_degraded_starves_the_bitstream(video_file: Path, h264_info: MediaInfo) -> None:
    camera = camera_with(video_file, mode="degraded")
    cmd = build_publish_command(camera, URL, info=h264_info)

    assert cmd[index_of(cmd, "-c:v") + 1] == "libx264"
    assert cmd[index_of(cmd, "-b:v") + 1] == DEGRADED_BITRATE
    assert cmd[index_of(cmd, "-maxrate") + 1] == DEGRADED_BITRATE
    assert cmd[index_of(cmd, "-g") + 1] == str(DEGRADED_GOP)
    # It degrades the encoding, not the picture.
    assert "-vf" not in cmd


def test_degraded_keeps_explicit_video_settings(
    video_file: Path, h264_info: MediaInfo
) -> None:
    """The preset only fills gaps; a bitrate the user asked for still wins."""
    camera = CameraSpec(
        name="cam1",
        source=video_file,
        video=VideoSettings(bitrate="900k", gop=25),
        simulation=SimulationSpec(mode=SimulationMode.DEGRADED),
    )
    cmd = build_publish_command(camera, URL, info=h264_info)

    assert cmd[index_of(cmd, "-b:v") + 1] == "900k"
    assert cmd[index_of(cmd, "-g") + 1] == "25"


def test_simulation_filters_follow_the_transcode_filters(
    video_file: Path, mpeg4_info: MediaInfo
) -> None:
    camera = CameraSpec(
        name="cam1",
        source=video_file,
        mode=StreamMode.TRANSCODE,
        video=VideoSettings(resolution="640x360", fps=10),
        simulation=SimulationSpec(mode=SimulationMode.NOISE, noise_level=20),
    )
    cmd = build_publish_command(camera, URL, info=mpeg4_info)
    assert filters_of(cmd) == "scale=640:360,fps=10,noise=alls=20:allf=t"


def test_extra_filters_are_appended(video_file: Path, h264_info: MediaInfo) -> None:
    camera = camera_with(video_file, mode="noise", filters="gblur=sigma=2")
    assert filters_of(build_publish_command(camera, URL, info=h264_info)) == (
        "noise=alls=30:allf=t,gblur=sigma=2"
    )


def test_extra_filters_alone_force_transcode(
    video_file: Path, h264_info: MediaInfo
) -> None:
    camera = camera_with(video_file, filters="hue=s=0")
    cmd = build_publish_command(camera, URL, info=h264_info)

    assert cmd[index_of(cmd, "-c:v") + 1] == "libx264"
    assert filters_of(cmd) == "hue=s=0"


# ---------------------------------------------------------------------------
# temporal modes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["flaky", "stutter"])
def test_temporal_modes_carry_interval_and_duration(
    video_file: Path, mode: str
) -> None:
    camera = camera_with(video_file, mode=mode)
    assert camera.simulation.is_temporal is True


def test_flaky_publishes_normally(video_file: Path, h264_info: MediaInfo) -> None:
    """Nothing is degraded on the wire; the supervisor drops the stream."""
    camera = camera_with(video_file, mode="flaky")
    cmd = build_publish_command(camera, URL, info=h264_info)

    assert simulation_forces_transcode(camera, h264_info) is False
    assert cmd[index_of(cmd, "-c:v") + 1] == "copy"
    assert "-vf" not in cmd


def test_stutter_freezes_inside_one_filter_graph(
    video_file: Path, h264_info: MediaInfo
) -> None:
    """One publisher, no swap: swapping would stall attached readers."""
    camera = camera_with(video_file, mode="stutter", interval=12, duration=6)
    cmd = build_publish_command(camera, URL, info=h264_info)

    # Motion for 12s of every 18s, then the last frame is held for 6s.
    assert filters_of(cmd) == "select='lt(mod(t,18),12)',fps=30"


def test_stutter_refill_rate_follows_the_camera(
    video_file: Path, h264_info: MediaInfo
) -> None:
    camera = CameraSpec(
        name="cam1",
        source=video_file,
        video=VideoSettings(fps=10),
        simulation=SimulationSpec(mode=SimulationMode.STUTTER, interval=5, duration=5),
    )
    cmd = build_publish_command(camera, URL, info=h264_info)
    assert filters_of(cmd) == "fps=10,select='lt(mod(t,10),5)',fps=10"


def test_stutter_falls_back_to_a_default_rate(video_file: Path) -> None:
    """Unprobed source: still needs a rate to refill the frozen window."""
    camera = camera_with(video_file, mode="stutter", interval=5, duration=5)
    cmd = build_publish_command(camera, URL, info=None)
    assert filters_of(cmd) == "select='lt(mod(t,10),5)',fps=25"


def test_frozen_joins_quickly(video_file: Path, h264_info: MediaInfo) -> None:
    """At 1 fps a default GOP would put minutes between keyframes."""
    camera = camera_with(video_file, mode="frozen")
    cmd = build_publish_command(camera, URL, info=h264_info)
    assert cmd[index_of(cmd, "-g") + 1] == "1"


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "nonsense"},
        {"noise_level": 0},
        {"noise_level": 101},
        {"interval": 0},
        {"duration": -1},
        {"unknown": 1},
    ],
)
def test_invalid_simulation_is_rejected(video_file: Path, payload: dict) -> None:
    with pytest.raises(Exception):
        CameraSpec(name="cam1", source=video_file, simulation=payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# scheduler
# ---------------------------------------------------------------------------


class FakeProcess:
    """Stands in for ManagedProcess: records the stop/start cycle."""

    def __init__(self, name: str, suspended: bool = False) -> None:
        self.name = name
        self.suspended = suspended
        self.running = False
        self.events: list[str] = []

    def start(self) -> bool:
        self.running = True
        self.events.append("start")
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self.running = False
        self.events.append("stop")


def make_runtime(video_file: Path, spec: SimulationSpec) -> CameraRuntime:
    camera = CameraSpec(name="cam1", source=video_file, simulation=spec)
    instance = ServerInstance(rtsp_port=8554, api_port=9997, rtp_port=8000, cameras=[camera])
    runtime = CameraRuntime(
        camera=camera,
        instance=instance,
        mode=StreamMode.COPY,
        info=None,
        read_url=URL,
        read_url_with_credentials=URL,
        process=FakeProcess("cam1"),  # type: ignore[arg-type]
    )
    runtime.process.start()
    runtime.scheduler = SimulationScheduler(runtime, spec, now=0.0)
    return runtime


def test_flaky_drops_and_restores_the_stream(video_file: Path) -> None:
    spec = SimulationSpec(mode=SimulationMode.FLAKY, interval=10, duration=3)
    runtime = make_runtime(video_file, spec)
    scheduler = runtime.scheduler
    assert scheduler is not None

    scheduler.tick(5.0)  # before the first event
    assert runtime.process.running is True
    assert scheduler.state_label == "up"

    scheduler.tick(10.0)  # event starts
    assert runtime.process.running is False
    assert runtime.process.suspended is True
    assert scheduler.state_label == "down"

    scheduler.tick(12.0)  # still inside the event
    assert runtime.process.running is False

    scheduler.tick(13.0)  # event ends
    assert runtime.process.running is True
    assert runtime.process.suspended is False
    assert scheduler.state_label == "up"


def test_flaky_repeats_on_the_interval(video_file: Path) -> None:
    spec = SimulationSpec(mode=SimulationMode.FLAKY, interval=10, duration=3)
    runtime = make_runtime(video_file, spec)
    scheduler = runtime.scheduler
    assert scheduler is not None

    for now in (10.0, 13.0, 23.0, 26.0):
        scheduler.tick(now)

    # start, then (stop, start) per cycle.
    assert runtime.process.events == ["start", "stop", "start", "stop", "start"]


# ---------------------------------------------------------------------------
# planned stops vs failures
# ---------------------------------------------------------------------------


def test_planned_exit_is_not_a_failure() -> None:
    process = ManagedProcess(name="cam1", kind="publisher", command=["true"])
    process.consecutive_failures = 3

    process.note_exit(0, planned=True)

    assert process.consecutive_failures == 0


def test_unplanned_exit_still_backs_off() -> None:
    process = ManagedProcess(name="cam1", kind="publisher", command=["true"])

    process.note_exit(1)

    assert process.consecutive_failures == 1
    assert process.retry_at > 0
