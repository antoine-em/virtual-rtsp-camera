"""Tests for vcam.ntp — container detection, NTP measurement, clock adjustment."""

from __future__ import annotations

import struct
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vcam.ntp import (
    _NTP_DELTA,
    _STEP_THRESHOLD,
    NTPError,
    apply_offset,
    has_sys_time_cap,
    measure_offset,
    running_in_container,
)

# ---------------------------------------------------------------------------
# running_in_container
# ---------------------------------------------------------------------------


def test_running_in_container_dockerenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = tmp_path / ".dockerenv"
    sentinel.touch()
    with patch("vcam.ntp.Path") as mock_path:
        mock_path.return_value.exists.return_value = True
        assert running_in_container() is True


def test_not_in_container_no_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    """When /.dockerenv is absent and /proc/1/cgroup has no docker marker."""
    with (
        patch("vcam.ntp.Path") as mock_path_cls,
    ):
        # /.dockerenv does not exist
        sentinel = MagicMock()
        sentinel.exists.return_value = False
        # /proc/1/cgroup contains nothing docker-related
        cgroup = MagicMock()
        cgroup.read_text.return_value = "11:memory:/system.slice\n0::/\n"
        mock_path_cls.side_effect = lambda p: cgroup if "cgroup" in str(p) else sentinel
        result = running_in_container()
    # sentinel.exists() = False → falls through to cgroup check → False
    assert result is False


def test_running_in_container_via_cgroup(monkeypatch: pytest.MonkeyPatch) -> None:
    with (
        patch("vcam.ntp.Path") as mock_path_cls,
    ):
        sentinel = MagicMock()
        sentinel.exists.return_value = False
        cgroup = MagicMock()
        cgroup.read_text.return_value = "0::/system.slice/docker-abc123.scope\n"
        mock_path_cls.side_effect = lambda p: cgroup if "cgroup" in str(p) else sentinel
        assert running_in_container() is True


# ---------------------------------------------------------------------------
# has_sys_time_cap
# ---------------------------------------------------------------------------


def test_has_sys_time_cap_present(monkeypatch: pytest.MonkeyPatch) -> None:
    # CAP_SYS_TIME = bit 25 → 0x2000000
    cap_line = "CapEff:\t0000000002000000\n"  # only bit 25 set
    with patch("vcam.ntp.Path") as mock_path_cls:
        status = MagicMock()
        status.read_text.return_value = cap_line
        mock_path_cls.return_value = status
        assert has_sys_time_cap() is True


def test_has_sys_time_cap_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    cap_line = "CapEff:\t0000000000000000\n"  # no capabilities
    with patch("vcam.ntp.Path") as mock_path_cls:
        status = MagicMock()
        status.read_text.return_value = cap_line
        mock_path_cls.return_value = status
        assert has_sys_time_cap() is False


def test_has_sys_time_cap_ioerror(monkeypatch: pytest.MonkeyPatch) -> None:
    with patch("vcam.ntp.Path") as mock_path_cls:
        status = MagicMock()
        status.read_text.side_effect = OSError("no /proc")
        mock_path_cls.return_value = status
        assert has_sys_time_cap() is False


# ---------------------------------------------------------------------------
# measure_offset — socket mocking
# ---------------------------------------------------------------------------


def _make_ntp_response(ntp_unix_time: float) -> bytes:
    """Build a minimal NTP server reply with the given transmit timestamp."""
    ntp_secs = int(ntp_unix_time) + _NTP_DELTA
    ntp_frac = int((ntp_unix_time % 1) * 2**32)
    header = b"\x1c" + b"\x00" * 39  # 40 bytes before transmit timestamp
    return header + struct.pack("!II", ntp_secs, ntp_frac)


def test_measure_offset_basic() -> None:
    """Offset should be close to zero when the mock server matches real time."""
    fake_now = time.time()
    response = _make_ntp_response(fake_now)

    mock_sock = MagicMock()
    mock_sock.__enter__ = lambda s: s
    mock_sock.__exit__ = MagicMock(return_value=False)
    mock_sock.recvfrom.return_value = (response, ("127.0.0.1", 123))

    with (
        patch("vcam.ntp.socket.socket", return_value=mock_sock),
        patch("vcam.ntp.time.time", side_effect=[fake_now, fake_now + 0.002] * 5),
    ):
        offset, rtt = measure_offset("127.0.0.1", samples=1)

    assert abs(offset) < 0.01  # within 10 ms
    assert 0 <= rtt < 0.1


def test_measure_offset_positive() -> None:
    """Simulate server 1 s ahead of client clock."""
    fake_now = 1_700_000_000.0
    server_time = fake_now + 1.0  # server is 1 second ahead
    response = _make_ntp_response(server_time)

    mock_sock = MagicMock()
    mock_sock.__enter__ = lambda s: s
    mock_sock.__exit__ = MagicMock(return_value=False)
    mock_sock.recvfrom.return_value = (response, ("127.0.0.1", 123))

    with (
        patch("vcam.ntp.socket.socket", return_value=mock_sock),
        patch("vcam.ntp.time.time", side_effect=[fake_now, fake_now + 0.001] * 5),
    ):
        offset, _rtt = measure_offset("127.0.0.1", samples=1)

    assert abs(offset - 1.0) < 0.01


def test_measure_offset_all_fail() -> None:
    mock_sock = MagicMock()
    mock_sock.__enter__ = lambda s: s
    mock_sock.__exit__ = MagicMock(return_value=False)
    mock_sock.sendto.side_effect = OSError("network unreachable")

    with (
        patch("vcam.ntp.socket.socket", return_value=mock_sock),
        pytest.raises(NTPError, match="NTP query"),
    ):
        measure_offset("10.0.0.1", samples=2)


def test_measure_offset_picks_lowest_rtt() -> None:
    """Of two samples, the one with lower RTT should be returned."""
    fake_now = 1_700_000_000.0
    response = _make_ntp_response(fake_now + 0.5)

    mock_sock = MagicMock()
    mock_sock.__enter__ = lambda s: s
    mock_sock.__exit__ = MagicMock(return_value=False)
    mock_sock.recvfrom.return_value = (response, ("127.0.0.1", 123))

    # First sample: RTT = 0.1 s; second sample: RTT = 0.01 s
    time_seq = [fake_now, fake_now + 0.1, fake_now + 0.1, fake_now + 0.11]
    with (
        patch("vcam.ntp.socket.socket", return_value=mock_sock),
        patch("vcam.ntp.time.time", side_effect=time_seq),
    ):
        _, rtt = measure_offset("127.0.0.1", samples=2)

    assert rtt < 0.05  # should have picked the second (lower RTT) sample


# ---------------------------------------------------------------------------
# apply_offset — branching logic
# ---------------------------------------------------------------------------


def test_apply_offset_calls_step_for_large_offset() -> None:
    with (
        patch("vcam.ntp._clock_settime_step") as mock_step,
        patch("vcam.ntp._adjtimex_slew") as mock_slew,
    ):
        apply_offset(_STEP_THRESHOLD + 0.001)
        mock_step.assert_called_once()
        mock_slew.assert_not_called()


def test_apply_offset_calls_slew_for_small_offset() -> None:
    with (
        patch("vcam.ntp._clock_settime_step") as mock_step,
        patch("vcam.ntp._adjtimex_slew") as mock_slew,
    ):
        apply_offset(_STEP_THRESHOLD - 0.001)
        mock_slew.assert_called_once()
        mock_step.assert_not_called()


def test_apply_offset_calls_step_for_large_negative_offset() -> None:
    with (
        patch("vcam.ntp._clock_settime_step") as mock_step,
        patch("vcam.ntp._adjtimex_slew") as mock_slew,
    ):
        apply_offset(-(_STEP_THRESHOLD + 0.001))
        mock_step.assert_called_once()
        mock_slew.assert_not_called()
