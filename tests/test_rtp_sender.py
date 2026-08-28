"""Pacing and UDP delivery (TEST-009)."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from vcam.rtp_sender import Pacer, RtpSender, bind_rtp_pair


def test_wait_until_blocks_until_the_deadline() -> None:
    pacer = Pacer()
    start = time.perf_counter()
    assert pacer.wait_until(start + 0.05)
    assert time.perf_counter() - start >= 0.045


def test_a_past_deadline_returns_at_once_and_is_counted() -> None:
    """Replay catches up instead of shifting every later packet."""
    pacer = Pacer()
    start = time.perf_counter()
    assert pacer.wait_until(start - 1.0)
    assert time.perf_counter() - start < 0.02
    assert pacer.late_packets == 1


def test_pacing_does_not_accumulate_drift() -> None:
    """Absolute deadlines: ten 5 ms gaps must still total about 50 ms."""
    pacer = Pacer()
    base = time.perf_counter()
    for index in range(1, 11):
        pacer.wait_until(base + index * 0.005)
    elapsed = time.perf_counter() - base
    assert elapsed == pytest.approx(0.05, abs=0.02)


def test_pacing_does_not_burn_cpu_between_packets() -> None:
    """Sub-millisecond gaps must not turn pacing into a busy spin."""
    pacer = Pacer()
    base = time.perf_counter()
    start_cpu = time.process_time()
    for index in range(1, 101):
        pacer.wait_until(base + index * 0.002)
    elapsed = time.perf_counter() - base
    cpu = time.process_time() - start_cpu

    assert elapsed >= 0.15
    assert cpu < elapsed * 0.5


def test_stop_interrupts_a_long_wait_immediately() -> None:
    pacer = Pacer()
    result: list[bool] = []
    thread = threading.Thread(target=lambda: result.append(pacer.wait_until(time.perf_counter() + 30)))
    thread.start()
    time.sleep(0.05)
    pacer.stop()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert result == [False]
    assert pacer.stopped


def test_reset_moves_the_base() -> None:
    pacer = Pacer()
    original = pacer.base
    time.sleep(0.01)
    pacer.reset()
    assert pacer.base > original
    pacer.reset(123.0)
    assert pacer.base == 123.0


def test_sender_delivers_to_the_given_address() -> None:
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.settimeout(2.0)
    try:
        with RtpSender("127.0.0.1") as sender:
            sender.send_to(b"packet", receiver.getsockname())
            assert receiver.recv(1024) == b"packet"
    finally:
        receiver.close()


def test_sending_after_close_is_a_no_op() -> None:
    """A reader that vanishes mid-replay must not take the server down."""
    sender = RtpSender("127.0.0.1")
    sender.close()
    sender.close()  # idempotent
    sender.send_to(b"packet", ("127.0.0.1", 9))


def test_bind_rtp_pair_is_even_then_odd() -> None:
    rtp_sender, rtcp_sender = bind_rtp_pair("127.0.0.1")
    try:
        assert rtp_sender.port % 2 == 0
        assert rtcp_sender.port == rtp_sender.port + 1
    finally:
        rtp_sender.close()
        rtcp_sender.close()
