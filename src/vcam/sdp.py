"""Minimal SDP parsing and rendering for replayed captures.

The captured session description is treated as data to be preserved, not
re-derived: ``a=rtpmap`` and especially ``a=fmtp`` carry the parameter sets that
decide whether a decoder reproduces the captured fault. Only the lines that
describe *where* the stream comes from — the connection line and the control
URLs — are rewritten to point at our own server.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .rtp import STATIC_CLOCK_RATES

DEFAULT_CLOCK_RATE = 90000

#: Lines rewritten by the replay server rather than copied from the capture.
_REWRITTEN_MEDIA_PREFIXES = ("a=control:", "c=", "a=range:")
_REWRITTEN_SESSION_PREFIXES = ("v=", "o=", "s=", "c=", "t=", "a=control:", "a=range:")


@dataclass
class MediaDescription:
    """One ``m=`` block with the lines that follow it."""

    media_line: str
    lines: list[str] = field(default_factory=list)

    @property
    def media_type(self) -> str:
        parts = self.media_line.split()
        return parts[0][2:] if parts else "video"

    @property
    def payload_types(self) -> list[int]:
        parts = self.media_line.split()
        return [int(token) for token in parts[3:] if token.isdigit()]

    @property
    def control(self) -> Optional[str]:
        for line in self.lines:
            if line.startswith("a=control:"):
                return line[len("a=control:") :].strip()
        return None

    def clock_rate(self, payload_type: int) -> int:
        """Clock rate from ``a=rtpmap``, falling back to the static table."""
        prefix = f"a=rtpmap:{payload_type} "
        for line in self.lines:
            if line.startswith(prefix):
                parts = line[len(prefix) :].split("/")
                if len(parts) >= 2 and parts[1].isdigit():
                    return int(parts[1])
        return STATIC_CLOCK_RATES.get(payload_type, DEFAULT_CLOCK_RATE)

    def preserved_lines(self) -> list[str]:
        """Every line worth copying verbatim into the served description."""
        return [
            line
            for line in self.lines
            if line and not line.startswith(_REWRITTEN_MEDIA_PREFIXES)
        ]


@dataclass
class SessionDescription:
    session_lines: list[str] = field(default_factory=list)
    media: list[MediaDescription] = field(default_factory=list)

    def preserved_session_lines(self) -> list[str]:
        return [
            line
            for line in self.session_lines
            if line and not line.startswith(_REWRITTEN_SESSION_PREFIXES)
        ]


def parse(text: str) -> SessionDescription:
    """Parse an SDP document into its session part and its media blocks."""
    description = SessionDescription()
    current: Optional[MediaDescription] = None
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("m="):
            current = MediaDescription(media_line=line)
            description.media.append(current)
            continue
        if current is None:
            description.session_lines.append(line)
        else:
            current.lines.append(line)
    return description


def synthetic_media(payload_type: int, media_type: str = "video") -> MediaDescription:
    """Build an ``m=`` block for a capture with no recorded handshake.

    This is a guess and is logged as such: a dynamic payload type carries no
    information about the codec, so H.264 is assumed as the overwhelmingly
    common case.
    """
    media = MediaDescription(media_line=f"m={media_type} 0 RTP/AVP {payload_type}")
    if payload_type >= 96:
        encoding = "H264/90000" if media_type == "video" else "MPEG4-GENERIC/48000"
        media.lines.append(f"a=rtpmap:{payload_type} {encoding}")
    media.lines.append("a=recvonly")
    return media


def render(
    description: SessionDescription,
    *,
    session_name: str = "vcam replay",
    connection_address: str = "0.0.0.0",
    duration: Optional[float] = None,
) -> str:
    """Render a description for serving, with our own origin and control URLs."""
    lines = [
        "v=0",
        f"o=- 0 0 IN IP4 {connection_address}",
        f"s={session_name}",
        f"c=IN IP4 {connection_address}",
        "t=0 0",
        "a=control:*",
    ]
    if duration is not None and duration > 0:
        lines.append(f"a=range:npt=0-{duration:.3f}")
    lines.extend(description.preserved_session_lines())

    for index, media in enumerate(description.media):
        lines.append(media.media_line)
        lines.append(f"a=control:trackID={index}")
        lines.extend(media.preserved_lines())

    return "\r\n".join(lines) + "\r\n"
