"""RTP header parsing and loop rewriting (TEST-002)."""

from __future__ import annotations

import struct

import pytest

from vcam import rtp

from captures import rtp_packet


def test_parse_reads_the_fixed_header() -> None:
    packet = rtp_packet(sequence=1234, timestamp=98765, ssrc=0xCAFEBABE, marker=True)
    header = rtp.parse(packet)
    assert header is not None
    assert (header.sequence, header.timestamp, header.ssrc) == (1234, 98765, 0xCAFEBABE)
    assert header.marker is True
    assert header.payload_type == 96
    assert header.header_length == rtp.RTP_HEADER_LENGTH


def test_parse_accounts_for_csrc_and_extension() -> None:
    # version 2, extension bit, 2 CSRC entries.
    first = 0x80 | 0x10 | 2
    body = struct.pack("!BBHII", first, 96, 1, 2, 3) + b"\x00" * 8
    body += struct.pack("!HH", 0xBEDE, 3) + b"\x00" * 12
    header = rtp.parse(body + b"payload")
    assert header is not None
    assert header.header_length == 12 + 8 + 4 + 12


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x80" * 4,
        b"\x40" + b"\x00" * 20,  # version 1
        struct.pack("!BBHII", 0x80, 200, 0, 0, 0),  # RTCP sender report
    ],
)
def test_parse_rejects_non_rtp(data: bytes) -> None:
    assert rtp.parse(data) is None


def test_is_rtcp_spots_a_sender_report() -> None:
    assert rtp.is_rtcp(struct.pack("!BBHII", 0x80, 200, 0, 0, 0))
    assert not rtp.is_rtcp(rtp_packet(sequence=1, timestamp=1))


def test_looks_like_rtp_ignores_unlikely_payload_types() -> None:
    assert rtp.looks_like_rtp(rtp_packet(sequence=1, timestamp=1, payload_type=96))
    assert rtp.looks_like_rtp(rtp_packet(sequence=1, timestamp=1, payload_type=26))
    assert not rtp.looks_like_rtp(rtp_packet(sequence=1, timestamp=1, payload_type=42))


def test_rewrite_touches_only_sequence_and_timestamp() -> None:
    packet = rtp_packet(sequence=10, timestamp=100, payload=b"\x01\x02\x03\x04")
    out = rtp.rewrite(packet, sequence=20, timestamp=200)
    header = rtp.parse(out)
    assert header is not None
    assert (header.sequence, header.timestamp) == (20, 200)
    assert out[0:2] == packet[0:2]  # version, marker, payload type
    assert out[8:12] == packet[8:12]  # SSRC
    assert out[12:] == packet[12:]  # payload byte for byte


def test_rewrite_wraps_at_the_field_width() -> None:
    packet = rtp_packet(sequence=1, timestamp=1)
    out = rtp.rewrite(packet, sequence=70000, timestamp=(1 << 32) + 5)
    header = rtp.parse(out)
    assert header is not None
    assert header.sequence == 70000 % 65536
    assert header.timestamp == 5


def test_rewrite_rejects_a_short_packet() -> None:
    with pytest.raises(ValueError):
        rtp.rewrite(b"\x80\x60", sequence=1, timestamp=1)


def test_sequence_span_counts_the_extent_not_the_packets() -> None:
    # A capture that lost packets still advances by its full extent, so the
    # next loop cannot collide with the sequence numbers of this one.
    assert rtp.sequence_span(100, 109) == 10
    assert rtp.sequence_span(100, 100) == 1


def test_sequence_span_handles_wraparound() -> None:
    assert rtp.sequence_span(65530, 4) == 11
