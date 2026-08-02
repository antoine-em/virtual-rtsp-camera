"""Tests for configuration models, loading and URL construction."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from vcam.config import ConfigError, dump_stack, find_default_config, load_stack, save_stack
from vcam.models import AuthSpec, CameraSpec, CameraStack, ServerSpec, StreamMode


def write_config(tmp_path: Path, payload: dict, name: str = "cameras.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["cam 1", "cam/1", "-cam", "", "cam?1"])
def test_invalid_camera_names_rejected(video_file: Path, name: str) -> None:
    with pytest.raises(ValidationError):
        CameraSpec(name=name, source=video_file)


@pytest.mark.parametrize("name", ["cam1", "gate-north", "gate_north", "cam.1", "A1"])
def test_valid_camera_names_accepted(video_file: Path, name: str) -> None:
    assert CameraSpec(name=name, source=video_file).name == name


def test_duplicate_camera_names_on_same_port_rejected(video_file: Path) -> None:
    with pytest.raises(ValidationError, match="duplicate camera path"):
        CameraStack(
            cameras=[
                CameraSpec(name="cam1", source=video_file),
                CameraSpec(name="cam1", source=video_file),
            ]
        )


def test_same_name_on_different_ports_allowed(video_file: Path) -> None:
    stack = CameraStack(
        cameras=[
            CameraSpec(name="cam1", source=video_file),
            CameraSpec(name="cam1", source=video_file, port=8555),
        ]
    )
    assert len(stack.cameras) == 2


@pytest.mark.parametrize("resolution", ["1280", "1280*720", "hd", "1280x"])
def test_invalid_resolution_rejected(resolution: str) -> None:
    with pytest.raises(ValidationError):
        CameraSpec(name="cam1", source=Path("a.mp4"), video={"resolution": resolution})


@pytest.mark.parametrize("bitrate", ["2Mb", "fast", "2 M"])
def test_invalid_bitrate_rejected(bitrate: str) -> None:
    with pytest.raises(ValidationError):
        CameraSpec(name="cam1", source=Path("a.mp4"), video={"bitrate": bitrate})


def test_negative_offset_rejected(video_file: Path) -> None:
    with pytest.raises(ValidationError):
        CameraSpec(name="cam1", source=video_file, start_offset=-1)


def test_unknown_key_rejected(video_file: Path) -> None:
    with pytest.raises(ValidationError):
        CameraSpec(name="cam1", source=video_file, framerate=30)  # type: ignore[call-arg]


@pytest.mark.parametrize("password", ["with space", "with:colon", "with/slash", "tab\there"])
def test_credentials_outside_mediamtx_charset_rejected(password: str) -> None:
    with pytest.raises(ValidationError, match="MediaMTX"):
        AuthSpec(username="edgeai", password=password)


@pytest.mark.parametrize("password", ["s3cr3t!", "A-b_c.d", "p@ssw0rd#1", "{braces}"])
def test_valid_credentials_accepted(password: str) -> None:
    assert AuthSpec(username="edgeai", password=password).password == password


def test_invalid_log_level_rejected() -> None:
    with pytest.raises(ValidationError):
        ServerSpec(log_level="verbose")


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------


def test_cameras_share_one_port_with_distinct_paths(stack: CameraStack) -> None:
    urls = [stack.read_url(camera) for camera in stack.cameras]
    assert urls == [
        "rtsp://127.0.0.1:8554/cam1",
        "rtsp://127.0.0.1:8554/cam2",
    ]


def test_camera_port_override_changes_url(video_file: Path) -> None:
    stack = CameraStack(cameras=[CameraSpec(name="cam1", source=video_file, port=8600)])
    assert stack.read_url(stack.cameras[0]) == "rtsp://127.0.0.1:8600/cam1"


def test_wildcard_bind_address_displays_as_loopback(stack: CameraStack) -> None:
    stack.server.host = "0.0.0.0"
    assert stack.read_url(stack.cameras[0]).startswith("rtsp://127.0.0.1:")


def test_explicit_host_is_used(stack: CameraStack) -> None:
    assert stack.read_url(stack.cameras[0], "10.0.0.5") == "rtsp://10.0.0.5:8554/cam1"


def test_publish_url_is_always_loopback_and_anonymous(stack: CameraStack) -> None:
    stack.server.host = "10.0.0.5"
    stack.server.auth = AuthSpec(username="edgeai", password="s3cr3t")
    assert stack.publish_url(stack.cameras[0]) == "rtsp://127.0.0.1:8554/cam1"


def test_read_url_embeds_credentials(stack: CameraStack) -> None:
    stack.server.auth = AuthSpec(username="edgeai", password="p@ss")
    assert stack.read_url(stack.cameras[0]) == "rtsp://edgeai:p%40ss@127.0.0.1:8554/cam1"


def test_read_url_can_omit_credentials(stack: CameraStack) -> None:
    stack.server.auth = AuthSpec(username="edgeai", password="p@ss")
    url = stack.read_url(stack.cameras[0], with_credentials=False)
    assert url == "rtsp://127.0.0.1:8554/cam1"


def test_enabled_cameras_filter(video_file: Path) -> None:
    stack = CameraStack(
        cameras=[
            CameraSpec(name="on", source=video_file),
            CameraSpec(name="off", source=video_file, enabled=False),
        ]
    )
    assert [camera.name for camera in stack.enabled_cameras] == ["on"]


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------


def test_load_minimal_config(tmp_path: Path, video_file: Path) -> None:
    path = write_config(tmp_path, {"cameras": [{"name": "cam1", "source": str(video_file)}]})
    stack = load_stack(path)

    assert stack.server.rtsp_port == 8554
    assert stack.cameras[0].mode is StreamMode.AUTO
    assert stack.cameras[0].loop is True


def test_relative_sources_resolve_next_to_the_config(tmp_path: Path) -> None:
    media = tmp_path / "videos"
    media.mkdir()
    (media / "clip.mp4").write_bytes(b"0")
    path = write_config(tmp_path, {"cameras": [{"name": "cam1", "source": "videos/clip.mp4"}]})

    stack = load_stack(path)
    assert stack.cameras[0].source == (media / "clip.mp4").resolve()


def test_legacy_streams_manifest_is_accepted(tmp_path: Path, video_file: Path) -> None:
    path = write_config(
        tmp_path,
        {
            "streams": [
                {"name": "toll-day", "source": str(video_file), "offset_seconds": 12},
                {"name": "toll-night", "source": str(video_file), "enabled": False},
            ]
        },
        name="streams.yaml",
    )
    stack = load_stack(path)

    assert [camera.name for camera in stack.cameras] == ["toll-day", "toll-night"]
    assert stack.cameras[0].start_offset == 12
    assert stack.cameras[1].enabled is False


def test_repository_legacy_manifest_loads() -> None:
    path = Path(__file__).resolve().parent.parent / "jetson" / "streams.yaml"
    if not path.is_file():
        pytest.skip("jetson/streams.yaml not present")
    stack = load_stack(path)
    assert len(stack.cameras) >= 4
    assert len({camera.name for camera in stack.cameras}) == len(stack.cameras)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_stack(tmp_path / "nope.yaml")


def test_empty_config_raises(tmp_path: Path) -> None:
    path = tmp_path / "cameras.yaml"
    path.write_text("cameras: []\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="no cameras"):
        load_stack(path)


def test_invalid_config_reports_field_path(tmp_path: Path, video_file: Path) -> None:
    path = write_config(
        tmp_path,
        {"cameras": [{"name": "bad name", "source": str(video_file)}]},
    )
    with pytest.raises(ConfigError, match="cameras.0.name"):
        load_stack(path)


def test_broken_yaml_raises(tmp_path: Path) -> None:
    path = tmp_path / "cameras.yaml"
    path.write_text("cameras: [\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_stack(path)


def test_find_default_config(tmp_path: Path) -> None:
    assert find_default_config(tmp_path) is None
    path = tmp_path / "cameras.yaml"
    path.write_text("cameras: []\n", encoding="utf-8")
    assert find_default_config(tmp_path) == path


# ---------------------------------------------------------------------------
# round trip
# ---------------------------------------------------------------------------


def test_save_and_reload_preserves_settings(tmp_path: Path, video_file: Path) -> None:
    original = CameraStack(
        server=ServerSpec(rtsp_port=9000, auth=AuthSpec(username="edgeai", password="s3cr3t")),
        cameras=[
            CameraSpec(
                name="cam1",
                source=video_file,
                mode=StreamMode.TRANSCODE,
                start_offset=4,
                port=9001,
                video={"resolution": "640x360", "fps": 10, "bitrate": "600k"},
            )
        ],
    )
    path = tmp_path / "out.yaml"
    save_stack(original, path)
    reloaded = load_stack(path)

    assert reloaded.server.rtsp_port == 9000
    assert reloaded.server.auth is not None
    assert reloaded.server.auth.password == "s3cr3t"
    camera = reloaded.cameras[0]
    assert camera.mode is StreamMode.TRANSCODE
    assert camera.start_offset == 4
    assert camera.port == 9001
    assert camera.video.resolution == "640x360"
    assert camera.video.fps == 10


def test_dump_is_valid_yaml(stack: CameraStack) -> None:
    payload = yaml.safe_load(dump_stack(stack))
    assert [camera["name"] for camera in payload["cameras"]] == ["cam1", "cam2"]
