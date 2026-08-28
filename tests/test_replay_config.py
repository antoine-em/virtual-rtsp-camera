"""Configuration, validation and supervision of replay entries (TEST-011)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from vcam.config import ConfigError, dump_stack, load_stack
from vcam.models import AuthSpec, CameraSpec, CameraStack, ReplaySpec, ServerSpec
from vcam.supervisor import (
    REPLAY_PASSWORD_ENV,
    Supervisor,
    SupervisorError,
    build_replay_command,
    build_replay_env,
)


@pytest.fixture
def capture_file(tmp_path: Path) -> Path:
    path = tmp_path / "camera.pcap"
    path.write_bytes(b"\x00" * 32)
    return path


def _stack(capture: Path, **replay_kwargs: object) -> CameraStack:
    return CameraStack(
        server=ServerSpec(rtsp_port=8554),
        replays=[ReplaySpec(name="fault", source=capture, port=8555, **replay_kwargs)],  # type: ignore[arg-type]
    )


# -- model validation -------------------------------------------------------


def test_replay_defaults(capture_file: Path) -> None:
    replay = ReplaySpec(name="fault", source=capture_file, port=8555)
    assert replay.enabled and replay.loop and replay.rewrite_on_loop
    assert replay.speed == 1.0
    assert replay.sdp is None


def test_replay_name_must_be_a_valid_path_segment(capture_file: Path) -> None:
    with pytest.raises(ValidationError, match="valid RTSP path segment"):
        ReplaySpec(name="bad name", source=capture_file, port=8555)


def test_replay_speed_is_bounded(capture_file: Path) -> None:
    with pytest.raises(ValidationError):
        ReplaySpec(name="fault", source=capture_file, port=8555, speed=0)


def test_replay_rejects_unknown_fields(capture_file: Path) -> None:
    with pytest.raises(ValidationError):
        ReplaySpec(name="fault", source=capture_file, port=8555, transport="tcp")  # type: ignore[call-arg]


def test_a_replay_may_not_share_the_mediamtx_port(capture_file: Path, tmp_path: Path) -> None:
    """Replay runs its own listener, so the port cannot be MediaMTX's."""
    with pytest.raises(ValidationError, match="already served by MediaMTX"):
        CameraStack(
            server=ServerSpec(rtsp_port=8554),
            cameras=[CameraSpec(name="cam1", source=tmp_path / "clip.mp4")],
            replays=[ReplaySpec(name="fault", source=capture_file, port=8554)],
        )


def test_two_replays_may_not_share_a_port(capture_file: Path) -> None:
    with pytest.raises(ValidationError, match="claimed by two replays"):
        CameraStack(
            replays=[
                ReplaySpec(name="one", source=capture_file, port=8555),
                ReplaySpec(name="two", source=capture_file, port=8555),
            ]
        )


def test_replay_url_includes_its_own_port(capture_file: Path) -> None:
    stack = _stack(capture_file)
    assert stack.replay_url(stack.replays[0], host="cam.local") == "rtsp://cam.local:8555/fault"


def test_replay_url_carries_credentials_when_auth_is_on(capture_file: Path) -> None:
    stack = CameraStack(
        server=ServerSpec(auth=AuthSpec(username="admin", password="s3cret")),
        replays=[ReplaySpec(name="fault", source=capture_file, port=8555)],
    )
    url = stack.replay_url(stack.replays[0], host="cam.local")
    assert url == "rtsp://admin:s3cret@cam.local:8555/fault"
    assert "s3cret" not in stack.replay_url(stack.replays[0], "cam.local", with_credentials=False)


def test_enabled_replays_filters_disabled_entries(capture_file: Path) -> None:
    stack = CameraStack(
        replays=[
            ReplaySpec(name="on", source=capture_file, port=8555),
            ReplaySpec(name="off", source=capture_file, port=8556, enabled=False),
        ]
    )
    assert [replay.name for replay in stack.enabled_replays] == ["on"]


# -- config file ------------------------------------------------------------


def test_a_config_with_only_replays_is_valid(tmp_path: Path) -> None:
    (tmp_path / "camera.pcap").write_bytes(b"\x00")
    config = tmp_path / "cameras.yaml"
    config.write_text(
        "replays:\n  - name: fault\n    source: camera.pcap\n    port: 8555\n", encoding="utf-8"
    )
    stack = load_stack(config)
    assert not stack.cameras
    assert stack.replays[0].source == (tmp_path / "camera.pcap").resolve()


def test_relative_replay_paths_resolve_against_the_config(tmp_path: Path) -> None:
    (tmp_path / "captures").mkdir()
    (tmp_path / "captures" / "camera.pcap").write_bytes(b"\x00")
    (tmp_path / "session.sdp").write_text("v=0\r\n", encoding="utf-8")
    config = tmp_path / "cameras.yaml"
    config.write_text(
        "replays:\n"
        "  - name: fault\n"
        "    source: captures/camera.pcap\n"
        "    sdp: session.sdp\n"
        "    port: 8555\n",
        encoding="utf-8",
    )
    replay = load_stack(config).replays[0]
    assert replay.source == (tmp_path / "captures" / "camera.pcap").resolve()
    assert replay.sdp == (tmp_path / "session.sdp").resolve()


def test_an_empty_config_is_still_rejected(tmp_path: Path) -> None:
    config = tmp_path / "cameras.yaml"
    config.write_text("server:\n  rtsp_port: 8554\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="no cameras or replays"):
        load_stack(config)


def test_dump_stack_round_trips_replays(capture_file: Path, tmp_path: Path) -> None:
    stack = _stack(capture_file, speed=2.0, loop=False)
    config = tmp_path / "out.yaml"
    config.write_text(dump_stack(stack), encoding="utf-8")

    reloaded = load_stack(config).replays[0]
    assert (reloaded.name, reloaded.port, reloaded.speed, reloaded.loop) == (
        "fault",
        8555,
        2.0,
        False,
    )


def test_dump_stack_omits_an_empty_replay_list(tmp_path: Path) -> None:
    stack = CameraStack(cameras=[CameraSpec(name="cam1", source=tmp_path / "clip.mp4")])
    assert "replays" not in dump_stack(stack)


# -- supervisor -------------------------------------------------------------


def test_replay_command_carries_every_setting(capture_file: Path) -> None:
    stack = _stack(capture_file, speed=2.5, loop=False, rewrite_on_loop=False)
    command = build_replay_command(stack.replays[0], stack, host="0.0.0.0")

    assert "replay" in command
    assert str(capture_file) in command
    assert command[command.index("--port") + 1] == "8555"
    assert command[command.index("--path") + 1] == "fault"
    assert command[command.index("--speed") + 1] == "2.5"
    assert "--no-loop" in command
    assert "--no-rewrite-on-loop" in command
    assert "--username" not in command


def test_replay_command_passes_credentials_and_sdp(capture_file: Path, tmp_path: Path) -> None:
    sdp_file = tmp_path / "session.sdp"
    sdp_file.write_text("v=0\r\n", encoding="utf-8")
    stack = CameraStack(
        server=ServerSpec(auth=AuthSpec(username="admin", password="s3cret")),
        replays=[ReplaySpec(name="fault", source=capture_file, port=8555, sdp=sdp_file)],
    )
    command = build_replay_command(stack.replays[0], stack, host="0.0.0.0")
    env = build_replay_env(stack)

    assert command[command.index("--username") + 1] == "admin"
    assert command[command.index("--sdp") + 1] == str(sdp_file)
    assert "--loop" in command
    # The password must never reach argv: /proc/<pid>/cmdline is world-readable.
    assert "--password" not in command
    assert "s3cret" not in command
    assert env[REPLAY_PASSWORD_ENV] == "s3cret"


def test_replay_env_is_empty_without_auth(capture_file: Path) -> None:
    assert build_replay_env(_stack(capture_file)) == {}


def test_prepare_creates_a_managed_process_per_replay(
    capture_file: Path, tmp_path: Path
) -> None:
    supervisor = Supervisor(
        _stack(capture_file), mediamtx_binary=tmp_path / "mediamtx", work_dir=tmp_path / "work"
    )
    supervisor.prepare()

    assert not supervisor.servers  # no cameras, so no MediaMTX at all
    assert [runtime.replay.name for runtime in supervisor.replays] == ["fault"]
    assert supervisor.replays[0].process.kind == "replay"
    assert supervisor.replays[0].read_url.endswith(":8555/fault")


def test_prepare_reports_a_missing_capture(tmp_path: Path) -> None:
    stack = CameraStack(
        replays=[ReplaySpec(name="fault", source=tmp_path / "absent.pcap", port=8555)]
    )
    supervisor = Supervisor(
        stack, mediamtx_binary=tmp_path / "mediamtx", work_dir=tmp_path / "work"
    )
    with pytest.raises(SupervisorError, match="absent.pcap"):
        supervisor.prepare()


def test_prepare_rejects_a_stack_with_nothing_enabled(capture_file: Path, tmp_path: Path) -> None:
    stack = CameraStack(
        replays=[ReplaySpec(name="fault", source=capture_file, port=8555, enabled=False)]
    )
    supervisor = Supervisor(
        stack, mediamtx_binary=tmp_path / "mediamtx", work_dir=tmp_path / "work"
    )
    with pytest.raises(SupervisorError, match="no enabled cameras or replays"):
        supervisor.prepare()


def test_health_snapshot_includes_replays(capture_file: Path, tmp_path: Path) -> None:
    supervisor = Supervisor(
        _stack(capture_file), mediamtx_binary=tmp_path / "mediamtx", work_dir=tmp_path / "work"
    )
    supervisor.prepare()
    snapshot = supervisor.health_snapshot()

    assert snapshot["replays"][0]["name"] == "fault"
    assert snapshot["replays"][0]["running"] is False
    assert snapshot["replays"][0]["url"].endswith(":8555/fault")


def test_replays_are_monitored_for_restarts(capture_file: Path, tmp_path: Path) -> None:
    supervisor = Supervisor(
        _stack(capture_file), mediamtx_binary=tmp_path / "mediamtx", work_dir=tmp_path / "work"
    )
    supervisor.prepare()
    assert supervisor.replays[0].process in supervisor._all_processes()
