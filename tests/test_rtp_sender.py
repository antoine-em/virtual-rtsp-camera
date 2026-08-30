"""Pacing and UDP delivery (TEST-009)."""

from __future__ import annotations

import socket
import threading
import time

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


def _worst_single_overshoot() -> float:
    """How far this machine overshoots one short wait, worst of five tries.

    Used to size the drift budget below. A shared CI runner oversleeps far
    worse than a laptop, and that is the scheduler's doing rather than the
    pacer's, so the tolerance has to be measured rather than guessed.
    """
    worst = 0.0
    for _ in range(5):
        pacer = Pacer()
        base = time.perf_counter()
        pacer.wait_until(base + 0.005)
        worst = max(worst, time.perf_counter() - base - 0.005)
    return worst


def test_pacing_does_not_accumulate_drift() -> None:
    """Absolute deadlines: the total tracks the last deadline, not the gap count.

    Sleeping each gap in turn pays the scheduler's overshoot once per wait, so
    the error grows with the number of packets. Waiting on absolute deadlines
    pays it once overall: after an overshoot the next deadlines are already in
    the past and return immediately, which is the catching-up that
    ``late_packets`` counts.

    So the test spans the same 50 ms twice, once with 10 deadlines and once
    with 50. Accumulation would make the second run five times worse; absolute
    deadlines put both within a single overshoot of 50 ms. The budget is
    calibrated from this machine, because the previous ``0.05 +/- 0.02`` failed
    on a busy runner that was merely slow, not drifting.
    """
    budget = 0.05 + max(0.01, 3 * _worst_single_overshoot())

    for deadlines, gap in ((10, 0.005), (50, 0.001)):
        pacer = Pacer()
        base = time.perf_counter()
        for index in range(1, deadlines + 1):
            pacer.wait_until(base + index * gap)
        elapsed = time.perf_counter() - base

        # Exact and unflakeable: an absolute deadline is never met early.
        assert elapsed >= 0.05
        assert elapsed < budget, f"{deadlines} deadlines drifted to {elapsed:.4f}s"


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
    thread = threading.Thread(
        target=lambda: result.append(pacer.wait_until(time.perf_counter() + 30))
    )
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
