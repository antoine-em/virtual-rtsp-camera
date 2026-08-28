"""Capture reading: link-layer handling, padding, and TCP metadata (TEST-001)."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from vcam.pcap import Datagram, PcapError, PcapWriter, backend_version, iter_datagrams

from captures import CLIENT, CLIENT_RTP, SERVER, SERVER_RTP, rtp_packet


def test_backend_version_is_reported() -> None:
    assert backend_version()


def test_udp_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "udp.pcap"
    payload = rtp_packet(sequence=7, timestamp=900)
    with PcapWriter(path) as writer:
        writer.write_udp(payload, 1000.5, src=SERVER_RTP, dst=CLIENT_RTP)

    (datagram,) = list(iter_datagrams(path))
    assert datagram.proto == "udp"
    assert datagram.payload == payload
    assert datagram.src == SERVER_RTP
    assert datagram.dst == CLIENT_RTP
    assert datagram.tcp_seq is None
    assert datagram.ts == pytest.approx(1000.5, abs=1e-6)


def test_short_udp_payload_is_not_padded(tmp_path: Path) -> None:
    """A 4-byte payload is padded to the 60-byte Ethernet minimum on the wire."""
    path = tmp_path / "short.pcap"
    with PcapWriter(path) as writer:
        writer.write_udp(b"\xde\xad\xbe\xef", 1.0, src=SERVER_RTP, dst=CLIENT_RTP)

    (datagram,) = list(iter_datagrams(path))
    assert datagram.payload == b"\xde\xad\xbe\xef"


def test_tcp_payload_carries_its_sequence_number(tmp_path: Path) -> None:
    path = tmp_path / "tcp.pcap"
    with PcapWriter(path) as writer:
        writer.write_tcp(b"OPTIONS * RTSP/1.0\r\n\r\n", 2.0, src=CLIENT, dst=SERVER, seq=4242)

    (datagram,) = list(iter_datagrams(path))
    assert datagram.proto == "tcp"
    assert datagram.tcp_seq == 4242
    assert datagram.payload.startswith(b"OPTIONS")


def test_empty_payloads_are_skipped(tmp_path: Path) -> None:
    """A bare ACK carries no payload and must not reach the RTP parser."""
    path = tmp_path / "ack.pcap"
    with PcapWriter(path) as writer:
        writer.write_tcp(b"", 1.0, src=CLIENT, dst=SERVER, seq=1, flags="A")
        writer.write_tcp(b"data", 2.0, src=CLIENT, dst=SERVER, seq=1)

    datagrams = list(iter_datagrams(path))
    assert [item.payload for item in datagrams] == [b"data"]


def test_non_ip_frames_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "arp.pcap"
    with PcapWriter(path) as writer:
        # An ARP frame: valid Ethernet, no IP layer, must not abort the read.
        writer.write_frame(
            b"\xff" * 6 + b"\x00\x11\x22\x33\x44\x55" + struct.pack("!H", 0x0806) + b"\x00" * 28,
            1.0,
        )
        writer.write_udp(b"payload", 2.0, src=SERVER_RTP, dst=CLIENT_RTP)

    assert [item.payload for item in iter_datagrams(path)] == [b"payload"]


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(PcapError, match="not found"):
        list(iter_datagrams(tmp_path / "nope.pcap"))


def test_unreadable_file_raises(tmp_path: Path) -> None:
    path = tmp_path / "garbage.pcap"
    path.write_bytes(b"not a capture at all")
    with pytest.raises(PcapError):
        list(iter_datagrams(path))


def test_flow_is_directional() -> None:
    forward = Datagram(ts=0.0, proto="udp", src=SERVER_RTP, dst=CLIENT_RTP, payload=b"")
    reverse = Datagram(ts=0.0, proto="udp", src=CLIENT_RTP, dst=SERVER_RTP, payload=b"")
    assert forward.flow != reverse.flow
