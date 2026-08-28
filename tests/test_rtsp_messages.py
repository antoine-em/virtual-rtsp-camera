"""RTSP framing and header parsing (TEST-003)."""

from __future__ import annotations

import pytest

from vcam.rtsp_messages import (
    MAX_HEADER_LENGTH,
    InterleavedFrame,
    RtspFramingError,
    RtspMessage,
    RtspStreamParser,
    build_interleaved,
    build_response,
    parse_headers,
    parse_port_pair,
    parse_transport,
)


def test_a_stream_without_a_terminator_is_refused_instead_of_buffered() -> None:
    """The buffer must not grow without bound on a truncated or noisy stream."""
    parser = RtspStreamParser()
    assert list(parser.feed(b"OPTIONS * RTSP/1.0\r\n")) == []

    with pytest.raises(RtspFramingError, match="terminator"):
        list(parser.feed(b"X" * (MAX_HEADER_LENGTH + 1)))


def test_parses_a_request_with_a_body() -> None:
    data = (
        b"ANNOUNCE rtsp://cam/stream RTSP/1.0\r\n"
        b"CSeq: 3\r\n"
        b"Content-Type: application/sdp\r\n"
        b"Content-Length: 5\r\n\r\n"
        b"v=0\r\n"
    )
    (message,) = list(RtspStreamParser().feed(data))
    assert isinstance(message, RtspMessage)
    assert message.is_request
    assert message.method == "ANNOUNCE"
    assert message.uri == "rtsp://cam/stream"
    assert message.cseq == 3
    assert message.body == b"v=0\r\n"
    assert message.status is None


def test_parses_a_response() -> None:
    (message,) = list(RtspStreamParser().feed(b"RTSP/1.0 401 Unauthorized\r\nCSeq: 9\r\n\r\n"))
    assert isinstance(message, RtspMessage)
    assert not message.is_request
    assert message.status == 401
    assert message.method is None
    assert message.cseq == 9


def test_interleaved_frames_are_split_from_messages() -> None:
    """RFC 2326 §10.12: binary frames and messages share the same stream."""
    stream = (
        b"RTSP/1.0 200 OK\r\nCSeq: 1\r\n\r\n"
        + build_interleaved(0, b"\x80\x60rtp-one")
        + build_interleaved(1, b"rtcp")
        + b"RTSP/1.0 200 OK\r\nCSeq: 2\r\n\r\n"
    )
    items = list(RtspStreamParser().feed(stream))
    kinds = [type(item).__name__ for item in items]
    assert kinds == ["RtspMessage", "InterleavedFrame", "InterleavedFrame", "RtspMessage"]
    assert items[1].channel == 0
    assert items[1].payload == b"\x80\x60rtp-one"
    assert items[2].channel == 1


def test_items_are_yielded_only_once_complete() -> None:
    """Segment boundaries are arbitrary; a half-arrived frame must wait."""
    parser = RtspStreamParser()
    frame = build_interleaved(2, b"0123456789")
    assert list(parser.feed(frame[:6])) == []
    assert parser.pending == 6
    (item,) = list(parser.feed(frame[6:]))
    assert isinstance(item, InterleavedFrame)
    assert item.payload == b"0123456789"


def test_byte_at_a_time_feeding_produces_the_same_items() -> None:
    stream = b"OPTIONS * RTSP/1.0\r\nCSeq: 1\r\n\r\n" + build_interleaved(0, b"abc")
    parser = RtspStreamParser()
    items = [item for byte in stream for item in parser.feed(bytes([byte]))]
    assert [type(item).__name__ for item in items] == ["RtspMessage", "InterleavedFrame"]


def test_offsets_track_position_in_the_stream() -> None:
    head = b"RTSP/1.0 200 OK\r\nCSeq: 1\r\n\r\n"
    stream = head + build_interleaved(0, b"xyz")
    items = list(RtspStreamParser().feed(stream))
    assert items[0].offset == 0
    assert items[1].offset == len(head)


def test_repeated_headers_keep_the_last_value() -> None:
    start_line, headers = parse_headers("SETUP rtsp://cam RTSP/1.0\r\nX: one\r\nX: two")
    assert start_line == "SETUP rtsp://cam RTSP/1.0"
    assert headers["x"] == "two"


def test_build_response_adds_content_length() -> None:
    out = build_response(200, "OK", {"CSeq": "1"}, b"v=0\r\n")
    assert out.startswith(b"RTSP/1.0 200 OK\r\n")
    assert b"Content-Length: 5\r\n" in out
    assert out.endswith(b"\r\n\r\nv=0\r\n")


def test_build_interleaved_rejects_an_oversized_payload() -> None:
    with pytest.raises(ValueError):
        build_interleaved(0, b"x" * 70000)


def test_parse_transport_tcp() -> None:
    parsed = parse_transport("RTP/AVP/TCP;unicast;interleaved=0-1")
    assert parsed["spec"] == "RTP/AVP/TCP"
    assert parsed["unicast"] == ""
    assert parsed["interleaved"] == "0-1"


def test_parse_transport_uses_the_first_alternative() -> None:
    parsed = parse_transport("RTP/AVP;unicast;client_port=5000-5001,RTP/AVP/TCP;unicast")
    assert parsed["client_port"] == "5000-5001"
    assert "interleaved" not in parsed


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0-1", (0, 1)),
        ("5000-5001", (5000, 5001)),
        ("5000", (5000, 5001)),
        ("", None),
        ("bogus", None),
    ],
)
def test_parse_port_pair(value: str, expected: object) -> None:
    assert parse_port_pair(value) == expected
