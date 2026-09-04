"""RTSP framing: messages and ``$``-framed interleaved data on one stream.

RFC 2326 §10.12 lets an RTSP connection carry binary RTP/RTCP frames between
protocol messages, prefixed with ``$``, a channel byte and a 16-bit length. The
capture extractor and the replay server both have to walk that mixture, so the
framing lives here and is tested once.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from .errors import RtspFramingError

INTERLEAVED_MAGIC = 0x24  # b"$"
INTERLEAVED_HEADER_LENGTH = 4
_HEADER_TERMINATOR = b"\r\n\r\n"

#: Guard against a stream that never produces a terminator (truncated capture,
#: or binary data misread as the start of a message).
MAX_HEADER_LENGTH = 64 * 1024


@dataclass(frozen=True)
class InterleavedFrame:
    channel: int
    payload: bytes
    offset: int = 0
    """Byte offset of the frame within the stream, used to date it."""


@dataclass(frozen=True)
class RtspMessage:
    start_line: str
    headers: dict[str, str] = field(default_factory=dict)
    """Header names lower-cased; repeated headers keep the last value."""
    body: bytes = b""
    offset: int = 0

    @property
    def is_request(self) -> bool:
        return not self.start_line.startswith("RTSP/")

    @property
    def method(self) -> str | None:
        if not self.is_request:
            return None
        return self.start_line.split(" ", 1)[0].upper()

    @property
    def uri(self) -> str | None:
        if not self.is_request:
            return None
        parts = self.start_line.split(" ")
        return parts[1] if len(parts) > 1 else None

    @property
    def status(self) -> int | None:
        if self.is_request:
            return None
        parts = self.start_line.split(" ")
        if len(parts) < 2 or not parts[1].isdigit():
            return None
        return int(parts[1])

    @property
    def cseq(self) -> int | None:
        raw = self.headers.get("cseq", "").strip()
        return int(raw) if raw.isdigit() else None


StreamItem = RtspMessage | InterleavedFrame


def parse_headers(block: str) -> tuple[str, dict[str, str]]:
    """Split a header block into its start line and a lower-cased header map."""
    lines = block.split("\r\n")
    start_line = lines[0].strip() if lines else ""
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line.strip() or ":" not in line:
            continue
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return start_line, headers


class RtspStreamParser:
    """Incremental parser for one direction of an RTSP TCP connection."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._consumed = 0
        """Absolute offset of ``self._buffer[0]`` within the stream."""

    def feed(self, data: bytes) -> Iterator[StreamItem]:
        """Add bytes and yield every complete item they made available."""
        self._buffer.extend(data)
        while True:
            item, size = self._next_item()
            if item is None:
                return
            del self._buffer[:size]
            self._consumed += size
            yield item

    @property
    def pending(self) -> int:
        """Bytes buffered but not yet forming a complete item."""
        return len(self._buffer)

    def _next_item(self) -> tuple[StreamItem | None, int]:
        buffer = self._buffer
        if not buffer:
            return None, 0

        if buffer[0] == INTERLEAVED_MAGIC:
            if len(buffer) < INTERLEAVED_HEADER_LENGTH:
                return None, 0
            length = int.from_bytes(buffer[2:4], "big")
            total = INTERLEAVED_HEADER_LENGTH + length
            if len(buffer) < total:
                return None, 0
            frame = InterleavedFrame(
                channel=buffer[1],
                payload=bytes(buffer[INTERLEAVED_HEADER_LENGTH:total]),
                offset=self._consumed,
            )
            return frame, total

        terminator = buffer.find(_HEADER_TERMINATOR)
        if terminator == -1:
            if len(buffer) > MAX_HEADER_LENGTH:
                # No message can still be forming. Refuse the stream rather
                # than buffering it forever.
                raise RtspFramingError(
                    f"no RTSP header terminator within {MAX_HEADER_LENGTH} bytes"
                )
            return None, 0

        header_block = bytes(buffer[:terminator]).decode("utf-8", errors="replace")
        start_line, headers = parse_headers(header_block)
        body_start = terminator + len(_HEADER_TERMINATOR)
        content_length = _content_length(headers)
        total = body_start + content_length
        if len(buffer) < total:
            return None, 0

        message = RtspMessage(
            start_line=start_line,
            headers=headers,
            body=bytes(buffer[body_start:total]),
            offset=self._consumed,
        )
        return message, total


def _content_length(headers: dict[str, str]) -> int:
    raw = headers.get("content-length", "0").strip()
    try:
        return max(int(raw), 0)
    except ValueError:
        return 0


def build_interleaved(channel: int, payload: bytes) -> bytes:
    """Frame *payload* for transmission on an RTSP interleaved channel."""
    if len(payload) > 0xFFFF:
        raise ValueError("interleaved payload exceeds 65535 bytes")
    return bytes((INTERLEAVED_MAGIC, channel & 0xFF)) + len(payload).to_bytes(2, "big") + payload


def build_response(
    status: int,
    reason: str,
    headers: dict[str, str],
    body: bytes = b"",
) -> bytes:
    """Render an RTSP response, adding ``Content-Length`` when there is a body."""
    lines = [f"RTSP/1.0 {status} {reason}"]
    rendered = dict(headers)
    if body:
        rendered["Content-Length"] = str(len(body))
    lines.extend(f"{name}: {value}" for name, value in rendered.items())
    head = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")
    return head + body


def parse_transport(value: str) -> dict[str, str]:
    """Parse an RTSP ``Transport`` header into its parameters.

    Valueless parameters (``unicast``, ``multicast``) map to an empty string;
    the leading specifier (``RTP/AVP/TCP``) is stored under ``"spec"``.
    """
    parameters: dict[str, str] = {}
    first = value.split(",")[0]
    for index, part in enumerate(first.split(";")):
        token = part.strip()
        if not token:
            continue
        if index == 0 and "=" not in token:
            parameters["spec"] = token.upper()
            continue
        name, separator, parameter = token.partition("=")
        parameters[name.strip().lower()] = parameter.strip() if separator else ""
    return parameters


def parse_port_pair(value: str) -> tuple[int, int] | None:
    """Parse ``a-b`` (or a bare ``a``) as used by ``client_port``/``interleaved``."""
    text = value.strip()
    if not text:
        return None
    low, _, high = text.partition("-")
    if not low.isdigit():
        return None
    first = int(low)
    second = int(high) if high.isdigit() else first + 1
    return first, second
