"""Tests for MediaMTX binary resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from vcam import binaries


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(binaries.ENV_CACHE, str(tmp_path / "cache"))
    monkeypatch.delenv(binaries.ENV_BINARY, raising=False)


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Darwin", "arm64", "darwin_arm64"),
        ("Darwin", "x86_64", "darwin_amd64"),
        ("Linux", "aarch64", "linux_arm64"),
        ("Linux", "x86_64", "linux_amd64"),
        ("Linux", "armv7l", "linux_armv7"),
    ],
)
def test_platform_slug(
    monkeypatch: pytest.MonkeyPatch, system: str, machine: str, expected: str
) -> None:
    monkeypatch.setattr(binaries.platform, "system", lambda: system)
    monkeypatch.setattr(binaries.platform, "machine", lambda: machine)
    monkeypatch.setattr(binaries, "_is_rosetta", lambda: False)
    assert binaries.platform_slug() == expected


def test_rosetta_resolves_to_arm64(monkeypatch: pytest.MonkeyPatch) -> None:
    """An Intel Python on Apple Silicon must still get the native build."""
    monkeypatch.setattr(binaries.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(binaries.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(binaries, "_is_rosetta", lambda: True)
    assert binaries.platform_slug() == "darwin_arm64"


def test_unsupported_platform_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(binaries.platform, "system", lambda: "Plan9")
    monkeypatch.setattr(binaries.platform, "machine", lambda: "sparc")
    with pytest.raises(binaries.BinaryError, match="unsupported platform"):
        binaries.platform_slug()


def test_asset_and_url_naming() -> None:
    assert (
        binaries.asset_name("v1.19.3", "linux_arm64")
        == "mediamtx_v1.19.3_linux_arm64.tar.gz"
    )
    assert binaries.asset_name("v1.19.3", "windows_amd64").endswith(".zip")
    assert binaries.asset_url("v1.19.3", "linux_arm64").startswith(
        "https://github.com/bluenviron/mediamtx/releases/download/v1.19.3/"
    )


def test_install_path_is_scoped_by_version_and_arch(monkeypatch: pytest.MonkeyPatch) -> None:
    a = binaries.install_path("v1.19.3", "linux_arm64")
    b = binaries.install_path("v1.19.3", "linux_amd64")
    c = binaries.install_path("v1.20.0", "linux_arm64")
    assert len({a, b, c}) == 3


def test_explicit_binary_must_exist(tmp_path: Path) -> None:
    with pytest.raises(binaries.BinaryError, match="not found"):
        binaries.resolve_binary(tmp_path / "missing")


def test_explicit_binary_is_used(tmp_path: Path) -> None:
    binary = tmp_path / "mediamtx"
    binary.write_text("#!/bin/sh\n")
    assert binaries.resolve_binary(binary) == binary


def test_env_override_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = tmp_path / "mediamtx"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setenv(binaries.ENV_BINARY, str(binary))
    monkeypatch.setattr(binaries.shutil, "which", lambda _name: None)
    assert binaries.resolve_binary() == binary


def test_cached_binary_is_preferred_over_download(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(binaries.shutil, "which", lambda _name: None)
    cached = binaries.install_path()
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text("#!/bin/sh\n")

    def fail(**_kwargs):  # pragma: no cover - must never run
        raise AssertionError("should not download when a cached binary exists")

    monkeypatch.setattr(binaries, "download", fail)
    assert binaries.resolve_binary() == cached


def test_download_disabled_raises_with_guidance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(binaries.shutil, "which", lambda _name: None)
    with pytest.raises(binaries.BinaryError, match="install-server"):
        binaries.resolve_binary(allow_download=False)
