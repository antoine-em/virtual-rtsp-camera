"""RTP and RTCP header handling.

Only the fixed header is ever touched. The payload — including the H.264 FU-A
fragmentation, the marker bit and any in-band parameter sets — is what makes a
captured fault reproducible, so it is copied through byte for byte.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

RTP_VERSION = 2
RTP_HEADER_LENGTH = 12

SEQUENCE_MODULO = 1 << 16
TIMESTAMP_MODULO = 1 << 32

#: RTCP packet types 200–204 (SR, RR, SDES, BYE, APP) map onto these values in
#: the byte that RTP uses for marker + payload type, which is how RFC 5761
#: multiplexing is disambiguated.
_RTCP_PAYLOAD_TYPES = frozenset(range(72, 77))

#: Default clock rates for the static payload types we may need to guess.
STATIC_CLOCK_RATES = {
    0: 8000,  # PCMU
    8: 8000,  # PCMA
    26: 90000,  # JPEG
    31: 90000,  # H261
    32: 90000,  # MPV
    33: 90000,  # MP2T
    34: 90000,  # H263
}


@dataclass(frozen=True)
class RtpHeader:
    payload_type: int
    marker: bool
    sequence: int
    timestamp: int
    ssrc: int
    header_length: int


def is_rtcp(data: bytes) -> bool:
    """True when *data* is RTCP rather than RTP (RFC 5761 disambiguation)."""
    if len(data) < 2:
        return False
    if data[0] >> 6 != RTP_VERSION:
        return False
    return (data[1] & 0x7F) in _RTCP_PAYLOAD_TYPES


def parse(data: bytes) -> Optional[RtpHeader]:
    """Parse the RTP fixed header, or return ``None`` if *data* is not RTP.

    Returning ``None`` instead of raising keeps the heuristic flow detector
    simple: it can throw arbitrary UDP payloads at this function.
    """
    if len(data) < RTP_HEADER_LENGTH:
        return None
    if data[0] >> 6 != RTP_VERSION:
        return None
    if is_rtcp(data):
        return None

    csrc_count = data[0] & 0x0F
    has_extension = bool(data[0] & 0x10)
    header_length = RTP_HEADER_LENGTH + csrc_count * 4

    if has_extension:
        if len(data) < header_length + 4:
            return None
        extension_words = int.from_bytes(data[header_length + 2 : header_length + 4], "big")
        header_length += 4 + extension_words * 4

    if len(data) < header_length:
        return None

    return RtpHeader(
        payload_type=data[1] & 0x7F,
        marker=bool(data[1] & 0x80),
        sequence=int.from_bytes(data[2:4], "big"),
        timestamp=int.from_bytes(data[4:8], "big"),
        ssrc=int.from_bytes(data[8:12], "big"),
        header_length=header_length,
    )


def looks_like_rtp(data: bytes) -> bool:
    """Cheap plausibility test used when a capture has no RTSP handshake."""
    header = parse(data)
    if header is None:
        return False
    # Dynamic types (96–127) cover every camera we care about; static types are
    # accepted too, but 1–34 alone would match far too much random traffic.
    return header.payload_type >= 96 or header.payload_type in STATIC_CLOCK_RATES


def rewrite(data: bytes, *, sequence: int, timestamp: int) -> bytes:
    """Return *data* with a new sequence number and timestamp, payload intact."""
    if len(data) < RTP_HEADER_LENGTH:
        raise ValueError("packet is too short to be RTP")
    out = bytearray(data)
    out[2:4] = (sequence % SEQUENCE_MODULO).to_bytes(2, "big")
    out[4:8] = (timestamp % TIMESTAMP_MODULO).to_bytes(4, "big")
    return bytes(out)


def sequence_span(first: int, last: int) -> int:
    """Number of sequence numbers covered by a capture, honouring 16-bit wrap.

    Gaps caused by real packet loss are preserved: the span is the distance
    between the first and last sequence number, not the packet count.
    """
    return ((last - first) % SEQUENCE_MODULO) + 1
