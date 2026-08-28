"""SDP preservation and rendering (TEST-004)."""

from __future__ import annotations

from vcam import sdp

from captures import SDP_TEXT

MULTI_TRACK = (
    "v=0\r\n"
    "o=- 1 1 IN IP4 10.0.0.1\r\n"
    "s=Live\r\n"
    "c=IN IP4 10.0.0.1\r\n"
    "t=0 0\r\n"
    "a=tool:camera-fw-4.2\r\n"
    "a=control:rtsp://10.0.0.1/stream\r\n"
    "m=video 0 RTP/AVP 96\r\n"
    "a=rtpmap:96 H264/90000\r\n"
    "a=fmtp:96 packetization-mode=1;sprop-parameter-sets=Z0LgHtoCgPRA,aM48gA==\r\n"
    "a=control:rtsp://10.0.0.1/stream/trackID=1\r\n"
    "m=audio 0 RTP/AVP 8\r\n"
    "a=control:rtsp://10.0.0.1/stream/trackID=2\r\n"
)


def test_parse_splits_session_and_media() -> None:
    description = sdp.parse(MULTI_TRACK)
    assert len(description.media) == 2
    assert description.media[0].media_type == "video"
    assert description.media[1].media_type == "audio"
    assert "a=tool:camera-fw-4.2" in description.session_lines


def test_media_exposes_payload_types_and_control() -> None:
    video = sdp.parse(MULTI_TRACK).media[0]
    assert video.payload_types == [96]
    assert video.control == "rtsp://10.0.0.1/stream/trackID=1"


def test_clock_rate_comes_from_rtpmap_then_the_static_table() -> None:
    description = sdp.parse(MULTI_TRACK)
    assert description.media[0].clock_rate(96) == 90000
    assert description.media[1].clock_rate(8) == 8000  # PCMA, no rtpmap line
    assert description.media[1].clock_rate(96) == sdp.DEFAULT_CLOCK_RATE


def test_render_preserves_fmtp_verbatim() -> None:
    """fmtp carries the parameter sets; losing it changes what the decoder sees."""
    rendered = sdp.render(sdp.parse(MULTI_TRACK))
    assert (
        "a=fmtp:96 packetization-mode=1;sprop-parameter-sets=Z0LgHtoCgPRA,aM48gA==" in rendered
    )
    assert "a=rtpmap:96 H264/90000" in rendered


def test_render_rewrites_origin_connection_and_control() -> None:
    rendered = sdp.render(sdp.parse(MULTI_TRACK), connection_address="192.0.2.7", duration=12.5)
    assert "c=IN IP4 192.0.2.7" in rendered
    assert "o=- 0 0 IN IP4 192.0.2.7" in rendered
    assert "a=range:npt=0-12.500" in rendered
    assert "rtsp://10.0.0.1" not in rendered
    assert "a=control:trackID=0" in rendered
    assert "a=control:trackID=1" in rendered


def test_render_keeps_unknown_session_attributes() -> None:
    assert "a=tool:camera-fw-4.2" in sdp.render(sdp.parse(MULTI_TRACK))


def test_render_ends_every_line_with_crlf() -> None:
    rendered = sdp.render(sdp.parse(SDP_TEXT))
    assert rendered.endswith("\r\n")
    assert "\n" not in rendered.replace("\r\n", "")


def test_synthetic_media_guesses_h264_for_a_dynamic_type() -> None:
    media = sdp.synthetic_media(96)
    assert media.media_line == "m=video 0 RTP/AVP 96"
    assert "a=rtpmap:96 H264/90000" in media.lines


def test_synthetic_media_leaves_static_types_alone() -> None:
    """A static payload type is self-describing; inventing an rtpmap would lie."""
    media = sdp.synthetic_media(26)
    assert not any(line.startswith("a=rtpmap:") for line in media.lines)
    assert media.clock_rate(26) == 90000
