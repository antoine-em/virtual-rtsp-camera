"""Tests for MediaMTX config rendering and server instance planning."""

from __future__ import annotations

from pathlib import Path

import yaml

from vcam.mediamtx import (
    UDP_BLOCK_SIZE,
    build_auth_users,
    plan_instances,
    render_server_config,
    render_server_config_yaml,
    write_server_config,
)
from vcam.models import AuthSpec, CameraSpec, CameraStack, ServerSpec


# ---------------------------------------------------------------------------
# instance planning
# ---------------------------------------------------------------------------


def test_cameras_share_one_instance_by_default(stack: CameraStack) -> None:
    instances = plan_instances(stack)

    assert len(instances) == 1
    assert instances[0].rtsp_port == 8554
    assert [camera.name for camera in instances[0].cameras] == ["cam1", "cam2"]


def test_port_override_spawns_a_second_instance(video_file: Path) -> None:
    stack = CameraStack(
        cameras=[
            CameraSpec(name="cam1", source=video_file),
            CameraSpec(name="cam2", source=video_file),
            CameraSpec(name="isolated", source=video_file, port=8555),
        ]
    )
    instances = plan_instances(stack)

    assert [instance.rtsp_port for instance in instances] == [8554, 8555]
    assert [camera.name for camera in instances[0].cameras] == ["cam1", "cam2"]
    assert [camera.name for camera in instances[1].cameras] == ["isolated"]


def test_instances_get_distinct_api_and_udp_ports(video_file: Path) -> None:
    stack = CameraStack(
        cameras=[
            CameraSpec(name="a", source=video_file),
            CameraSpec(name="b", source=video_file, port=8555),
            CameraSpec(name="c", source=video_file, port=8556),
        ]
    )
    instances = plan_instances(stack)

    api_ports = [instance.api_port for instance in instances]
    rtp_ports = [instance.rtp_port for instance in instances]
    assert len(set(api_ports)) == 3
    assert len(set(rtp_ports)) == 3
    # UDP blocks must not overlap: MediaMTX binds 8 consecutive ports.
    for first, second in zip(sorted(rtp_ports), sorted(rtp_ports)[1:]):
        assert second - first >= UDP_BLOCK_SIZE


def test_disabled_cameras_are_not_planned(video_file: Path) -> None:
    stack = CameraStack(
        cameras=[
            CameraSpec(name="on", source=video_file),
            CameraSpec(name="off", source=video_file, enabled=False),
        ]
    )
    instances = plan_instances(stack)
    assert [camera.name for camera in instances[0].cameras] == ["on"]


# ---------------------------------------------------------------------------
# config rendering
# ---------------------------------------------------------------------------


def test_rendered_config_listens_on_the_requested_ports(stack: CameraStack) -> None:
    instance = plan_instances(stack)[0]
    config = render_server_config(instance, stack.server)

    assert config["rtsp"] is True
    assert config["rtspAddress"] == ":8554"
    assert config["apiAddress"] == f"127.0.0.1:{instance.api_port}"
    assert config["rtpAddress"] == f":{instance.rtp_port}"
    assert config["rtcpAddress"] == f":{instance.rtp_port + 1}"


def test_explicit_bind_host_is_rendered(stack: CameraStack) -> None:
    stack.server.host = "10.0.0.5"
    config = render_server_config(plan_instances(stack)[0], stack.server)
    assert config["rtspAddress"] == "10.0.0.5:8554"


def test_only_rtsp_is_enabled(stack: CameraStack) -> None:
    """Every other listener must be off, otherwise instances collide on shared ports."""
    config = render_server_config(plan_instances(stack)[0], stack.server)

    for protocol in ("rtmp", "hls", "webrtc", "srt", "moq", "metrics", "pprof", "playback"):
        assert config[protocol] is False, f"{protocol} should be disabled"


def test_paths_are_declared_explicitly(stack: CameraStack) -> None:
    config = render_server_config(plan_instances(stack)[0], stack.server)
    assert set(config["paths"]) == {"cam1", "cam2"}


def test_rendered_config_is_valid_yaml(stack: CameraStack) -> None:
    text = render_server_config_yaml(plan_instances(stack)[0], stack.server)
    assert yaml.safe_load(text)["rtspAddress"] == ":8554"


def test_write_server_config_records_path(stack: CameraStack, tmp_path: Path) -> None:
    instance = plan_instances(stack)[0]
    path = write_server_config(instance, stack.server, tmp_path / "work")

    assert path.name == "mediamtx-8554.yml"
    assert instance.config_path == path
    assert yaml.safe_load(path.read_text())["rtsp"] is True


# ---------------------------------------------------------------------------
# authentication
# ---------------------------------------------------------------------------


def test_anonymous_access_by_default() -> None:
    users = build_auth_users(ServerSpec())
    public = users[0]

    assert public["user"] == "any"
    assert public["ips"] == []
    actions = {permission["action"] for permission in public["permissions"]}
    assert {"publish", "read"} <= actions


def test_auth_requires_credentials_from_readers() -> None:
    server = ServerSpec(auth=AuthSpec(username="reader", password="s3cr3t"))
    users = build_auth_users(server)

    anonymous = [user for user in users if user["user"] == "any"]
    named = [user for user in users if user["user"] == "reader"]

    assert len(named) == 1
    assert named[0]["pass"] == "s3cr3t"
    assert {p["action"] for p in named[0]["permissions"]} >= {"read", "playback"}

    # No anonymous user may read, from any IP.
    for user in anonymous:
        actions = {permission["action"] for permission in user["permissions"]}
        assert "read" not in actions


def test_local_publishers_stay_anonymous_under_auth() -> None:
    """The ffmpeg children publish over loopback, so they need no credentials."""
    server = ServerSpec(auth=AuthSpec(username="reader", password="s3cr3t"))
    users = build_auth_users(server)

    publishers = [
        user
        for user in users
        if any(permission["action"] == "publish" for permission in user["permissions"])
        and user["user"] == "any"
    ]
    assert len(publishers) == 1
    assert publishers[0]["ips"] == ["127.0.0.1", "::1"]


def test_api_is_restricted_to_loopback(stack: CameraStack) -> None:
    users = build_auth_users(stack.server)
    api_users = [
        user
        for user in users
        if any(permission["action"] == "api" for permission in user["permissions"])
    ]
    assert api_users
    for user in api_users:
        assert user["ips"] == ["127.0.0.1", "::1"]
