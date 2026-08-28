"""Timing primitives for replay.

Pacing is done against absolute deadlines derived from a single monotonic base
rather than by sleeping each inter-packet gap in turn: sleeping accumulates the
scheduler's jitter, so a 10 Mbps stream would drift by seconds over a few
minutes of replay.
"""

from __future__ import annotations

import socket
import threading
import time

#: Below this, `time.sleep` overshoots more than it is worth waiting on the
#: stop event. Kept well under a typical inter-packet gap: at 1.5 ms a 4 Mbps
#: H.264 stream — let alone the FU-A fragments of one I-frame — would take this
#: branch for every packet and pin a core per reader.
BUSY_WAIT_WINDOW = 0.0002


class Pacer:
    """Stoppable clock that waits until an absolute monotonic deadline."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self.base = time.perf_counter()
        self.late_packets = 0
        """Packets whose deadline had already passed when they came up."""

    def reset(self, base: float | None = None) -> None:
        self.base = time.perf_counter() if base is None else base

    def wait_until(self, deadline: float) -> bool:
        """Block until *deadline*; return ``False`` if stopped while waiting.

        A deadline in the past returns immediately: replay catches up rather
        than shifting every later packet by the amount it ran late.
        """
        while not self._stop.is_set():
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                if remaining < -BUSY_WAIT_WINDOW:
                    self.late_packets += 1
                return True
            if remaining > BUSY_WAIT_WINDOW:
                # Wait on the event so stop() interrupts long gaps at once.
                self._stop.wait(remaining - BUSY_WAIT_WINDOW)
            else:
                # Yield rather than spin: deadlines are absolute, so the small
                # overshoot of a short sleep never accumulates.
                time.sleep(remaining / 2)
        return False

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()


class RtpSender:
    """UDP socket used to deliver RTP and RTCP to one reader."""

    def __init__(self, host: str = "0.0.0.0", port: int = 0) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind((host, port))
        self._closed = False

    @property
    def port(self) -> int:
        return int(self._socket.getsockname()[1])

    def send_to(self, data: bytes, address: tuple[str, int]) -> None:
        if self._closed:
            return
        try:
            self._socket.sendto(data, address)
        except OSError:
            # A reader that vanished mid-replay must not take the server down.
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._socket.close()

    def __enter__(self) -> "RtpSender":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()


def bind_rtp_pair(host: str = "0.0.0.0", *, attempts: int = 50) -> tuple[RtpSender, RtpSender]:
    """Bind an even RTP port with its odd RTCP neighbour, as RFC 3550 expects."""
    for _ in range(attempts):
        rtp_sender = RtpSender(host)
        if rtp_sender.port % 2 != 0:
            rtp_sender.close()
            continue
        try:
            rtcp_sender = RtpSender(host, rtp_sender.port + 1)
        except OSError:
            rtp_sender.close()
            continue
        return rtp_sender, rtcp_sender
    raise OSError("could not bind a consecutive RTP/RTCP port pair")
