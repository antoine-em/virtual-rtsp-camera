"""A minimal RTSP server that replays captured RTP packets verbatim.

Everything a general-purpose media server does — depacketising, transcoding,
re-packetising — is exactly what would destroy the value of a capture, so this
server does none of it. It negotiates a session, then hands the reader the
recorded packets unchanged, at the intervals they were recorded with.
"""

from __future__ import annotations

import base64
import logging
import secrets
import socket
import socketserver
import threading
import time
from typing import Optional

from . import rtp
from .replay_source import ReplaySource, ReplayTrack, TimelineEntry
from .rtp_sender import Pacer, RtpSender, bind_rtp_pair
from .rtsp_messages import (
    RtspFramingError,
    RtspMessage,
    RtspStreamParser,
    build_interleaved,
    build_response,
    parse_port_pair,
    parse_transport,
)

logger = logging.getLogger("vcam")

SERVER_NAME = "vcam-replay"
SESSION_TIMEOUT = 60
#: Give the reader a moment to finish PLAY bookkeeping before the first packet.
START_DELAY = 0.05

PUBLIC_METHODS = (
    "OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER, SET_PARAMETER"
)

_REASONS = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    454: "Session Not Found",
    455: "Method Not Valid In This State",
    461: "Unsupported Transport",
    500: "Internal Server Error",
    501: "Not Implemented",
}


class _Transport:
    """Where one track's packets go for one reader."""

    def send_rtp(self, data: bytes) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:
        pass


class _InterleavedTransport(_Transport):
    def __init__(self, connection: "_Connection", channel: int) -> None:
        self._connection = connection
        self.channel = channel

    def send_rtp(self, data: bytes) -> None:
        self._connection.send_raw(build_interleaved(self.channel, data))


class _UdpTransport(_Transport):
    def __init__(self, sender: RtpSender, rtcp: RtpSender, address: tuple[str, int]) -> None:
        self._sender = sender
        self._rtcp = rtcp
        self.address = address

    @property
    def server_port(self) -> int:
        return self._sender.port

    def send_rtp(self, data: bytes) -> None:
        self._sender.send_to(data, self.address)

    def close(self) -> None:
        self._sender.close()
        self._rtcp.close()


class _Player(threading.Thread):
    """Streams the capture timeline to one reader's transports."""

    def __init__(self, connection: "_Connection") -> None:
        super().__init__(name="vcam-replay-player", daemon=True)
        self._connection = connection
        self._server = connection.server
        self._pacer = Pacer()

    def stop(self) -> None:
        self._pacer.stop()

    def run(self) -> None:
        server = self._server
        source = server.source
        timeline = source.timeline
        if not timeline:
            return

        steps = server.loop_steps
        loop_period = source.loop_period() or 0.0
        speed = server.speed
        iteration = 0
        base = time.perf_counter() + START_DELAY

        while not self._pacer.stopped:
            for position, entry in enumerate(timeline):
                if not self._pacer.wait_until(base + entry.offset / speed):
                    return
                transport = self._connection.transport_for(entry.track_index)
                if transport is None:
                    continue
                transport.send_rtp(self._packet(entry, position, iteration, steps))

            if not server.loop or loop_period <= 0:
                break
            iteration += 1
            base += loop_period / speed

        logger.debug("replay: playback finished for %s", self._connection.peer)

    @staticmethod
    def _packet(
        entry: TimelineEntry,
        position: int,
        iteration: int,
        steps: list[tuple[int, int]],
    ) -> bytes:
        """Return the packet to send, rewritten only when looping."""
        if iteration == 0 or not steps:
            return entry.data
        sequence_step, timestamp_step = steps[position]
        header = rtp.parse(entry.data)
        if header is None:
            return entry.data
        return rtp.rewrite(
            entry.data,
            sequence=header.sequence + iteration * sequence_step,
            timestamp=header.timestamp + iteration * timestamp_step,
        )


class _Connection:
    """Per-reader session state for one accepted TCP connection."""

    def __init__(self, sock: socket.socket, server: "ReplayServer", peer: str) -> None:
        self.socket = sock
        self.server = server
        self.peer = peer
        self.session_id: Optional[str] = None
        self.transports: dict[int, _Transport] = {}
        self.player: Optional[_Player] = None
        self._write_lock = threading.Lock()

    def send_raw(self, data: bytes) -> None:
        with self._write_lock:
            try:
                self.socket.sendall(data)
            except OSError:
                self.stop_player()

    def transport_for(self, track_index: int) -> Optional[_Transport]:
        return self.transports.get(track_index)

    def start_player(self) -> None:
        self.stop_player()
        self.player = _Player(self)
        self.player.start()

    def stop_player(self) -> None:
        if self.player is not None:
            self.player.stop()
            self.player = None

    def close(self) -> None:
        self.stop_player()
        for transport in self.transports.values():
            transport.close()
        self.transports.clear()


class _RtspHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server: ReplayServer = self.server.replay  # type: ignore[attr-defined]
        peer = f"{self.client_address[0]}:{self.client_address[1]}"
        connection = _Connection(self.request, server, peer)
        parser = RtspStreamParser()
        logger.info("replay: reader connected from %s", peer)

        try:
            while not server.stopping:
                try:
                    chunk = self.request.recv(4096)
                except (TimeoutError, socket.timeout):
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                try:
                    items = list(parser.feed(chunk))
                except RtspFramingError as exc:
                    logger.warning("replay: dropping reader %s: %s", peer, exc)
                    return
                for item in items:
                    if not isinstance(item, RtspMessage):
                        continue  # readers do not send us interleaved data
                    response, keep_open = server.handle_message(connection, item)
                    if response:
                        connection.send_raw(response)
                    if not keep_open:
                        return
        finally:
            connection.close()
            logger.info("replay: reader disconnected from %s", peer)


class _ThreadedRtspServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    timeout = 0.5


class ReplayServer:
    """Serve a :class:`ReplaySource` over RTSP."""

    def __init__(
        self,
        source: ReplaySource,
        *,
        host: str = "0.0.0.0",
        port: int = 8554,
        path: str = "replay",
        username: Optional[str] = None,
        password: Optional[str] = None,
        loop: bool = True,
        speed: float = 1.0,
        rewrite_on_loop: bool = True,
    ) -> None:
        if speed <= 0:
            raise ValueError("speed must be greater than zero")
        if (username is None) != (password is None):
            raise ValueError("username and password must be given together")
        if username is not None and not password:
            # An empty password would disable authentication entirely, which is
            # the opposite of what asking for it means.
            raise ValueError("password must not be empty when a username is set")

        self.source = source
        self.host = host
        self.path = path.strip("/")
        self.loop = loop
        self.speed = speed
        self.rewrite_on_loop = rewrite_on_loop
        self.stopping = False

        self._credentials = (
            base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
            if username is not None
            else None
        )
        self.loop_steps = self._plan_loop_steps()
        self._server = _ThreadedRtspServer((host, port), _RtspHandler)
        self._server.replay = self  # type: ignore[attr-defined]
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle -----------------------------------------------------------

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def rtsp_url(self, host: Optional[str] = None) -> str:
        display = host or ("127.0.0.1" if self.host in ("0.0.0.0", "::", "") else self.host)
        return f"rtsp://{display}:{self.port}/{self.path}"

    def start(self) -> None:
        """Serve in a background thread."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True
        )
        self._thread.start()

    def serve_forever(self) -> None:
        self._server.serve_forever(poll_interval=0.2)

    def shutdown(self) -> None:
        self.stopping = True
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "ReplayServer":
        self.start()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.shutdown()

    # -- loop planning -------------------------------------------------------

    def _plan_loop_steps(self) -> list[tuple[int, int]]:
        """Per-packet sequence and timestamp increments for each extra loop.

        Precomputed because it depends only on the capture: at playback time a
        packet's Nth-loop header is ``original + N * step``, which keeps both
        counters monotonic without touching the payload.

        Empty when no rewriting will happen, so a one-shot or deliberately raw
        replay does not walk the whole timeline at startup.
        """
        if not self.loop or not self.rewrite_on_loop:
            return []

        source = self.source
        tracks = {track.index: track for track in source.tracks}
        loop_period = source.loop_period()
        steps: list[tuple[int, int]] = []

        for entry in source.timeline:
            track = tracks.get(entry.track_index)
            header = rtp.parse(entry.data)
            if track is None or header is None:
                steps.append((0, 0))
                continue
            span = track.ssrc_spans.get(header.ssrc)
            sequence_step = span.span if span is not None else 0
            # Wall clock and RTP clock rarely agree exactly; take whichever
            # advances further so the next loop can never start behind this one.
            timestamp_step = max(
                round(loop_period * track.clock_rate),
                span.timestamp_step() if span is not None else 0,
            )
            steps.append((sequence_step, timestamp_step))

        return steps

    # -- request handling ----------------------------------------------------

    def handle_message(
        self, connection: _Connection, message: RtspMessage
    ) -> tuple[bytes, bool]:
        """Return ``(response_bytes, keep_connection_open)``."""
        if not message.is_request:
            return b"", True

        cseq = message.headers.get("cseq", "0")
        method = message.method or ""

        if method != "OPTIONS" and not self._authorised(message):
            return (
                build_response(
                    401,
                    _REASONS[401],
                    {
                        "CSeq": cseq,
                        "Server": SERVER_NAME,
                        "WWW-Authenticate": 'Basic realm="vcam"',
                    },
                ),
                True,
            )

        handlers = {
            "OPTIONS": self._options,
            "DESCRIBE": self._describe,
            "SETUP": self._setup,
            "PLAY": self._play,
            # Answered explicitly rather than by falling through to the unknown
            # method branch, so the refusal is a decision and not an omission.
            "PAUSE": self._pause,
            "TEARDOWN": self._teardown,
            "GET_PARAMETER": self._keepalive,
            "SET_PARAMETER": self._keepalive,
        }
        handler = handlers.get(method)
        if handler is None:
            return self._error(501, cseq), True
        return handler(connection, message, cseq)

    def _authorised(self, message: RtspMessage) -> bool:
        if self._credentials is None:
            return True
        header = message.headers.get("authorization", "")
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "basic":
            return False
        return secrets.compare_digest(value.strip(), self._credentials)

    def _error(self, status: int, cseq: str) -> bytes:
        return build_response(
            status, _REASONS.get(status, "Error"), {"CSeq": cseq, "Server": SERVER_NAME}
        )

    def _base_headers(self, connection: _Connection, cseq: str) -> dict[str, str]:
        headers = {"CSeq": cseq, "Server": SERVER_NAME}
        if connection.session_id:
            headers["Session"] = f"{connection.session_id};timeout={SESSION_TIMEOUT}"
        return headers

    def _matches_path(self, uri: Optional[str]) -> bool:
        if not uri or not self.path:
            return True
        target = uri.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        return target.endswith(f"/{self.path}")

    # -- methods -------------------------------------------------------------

    def _options(
        self, connection: _Connection, message: RtspMessage, cseq: str
    ) -> tuple[bytes, bool]:
        del message
        headers = self._base_headers(connection, cseq) | {"Public": PUBLIC_METHODS}
        return build_response(200, _REASONS[200], headers), True

    def _describe(
        self, connection: _Connection, message: RtspMessage, cseq: str
    ) -> tuple[bytes, bool]:
        if not self._matches_path(message.uri):
            return self._error(404, cseq), True
        body = self.source.sdp_text().encode("utf-8")
        headers = self._base_headers(connection, cseq) | {
            "Content-Type": "application/sdp",
            "Content-Base": f"{self.rtsp_url()}/",
        }
        return build_response(200, _REASONS[200], headers, body), True

    def _track_for(self, uri: Optional[str]) -> Optional[ReplayTrack]:
        if not uri:
            return None
        for track in self.source.tracks:
            if uri.rstrip("/").endswith(track.control):
                return track
        # A reader that sets up the aggregate URL gets the first track.
        return self.source.tracks[0] if self.source.tracks else None

    def _setup(
        self, connection: _Connection, message: RtspMessage, cseq: str
    ) -> tuple[bytes, bool]:
        if connection.player is not None:
            return self._error(455, cseq), True

        track = self._track_for(message.uri)
        if track is None:
            return self._error(404, cseq), True

        transport = parse_transport(message.headers.get("transport", ""))
        specifier = transport.get("spec", "RTP/AVP")
        interleaved = parse_port_pair(transport.get("interleaved", ""))

        if "TCP" in specifier or interleaved is not None or "interleaved" in transport:
            channels = interleaved or (track.index * 2, track.index * 2 + 1)
            connection.transports[track.index] = _InterleavedTransport(
                connection, channels[0]
            )
            reply = f"RTP/AVP/TCP;unicast;interleaved={channels[0]}-{channels[1]}"
        else:
            client_ports = parse_port_pair(transport.get("client_port", ""))
            if client_ports is None:
                return self._error(461, cseq), True
            try:
                rtp_sender, rtcp_sender = bind_rtp_pair(self.host)
            except OSError:
                return self._error(500, cseq), True
            udp = _UdpTransport(
                rtp_sender, rtcp_sender, (connection.peer.rsplit(":", 1)[0], client_ports[0])
            )
            connection.transports[track.index] = udp
            reply = (
                f"RTP/AVP;unicast;client_port={client_ports[0]}-{client_ports[1]};"
                f"server_port={udp.server_port}-{udp.server_port + 1}"
            )

        if connection.session_id is None:
            connection.session_id = secrets.token_hex(4).upper()

        headers = self._base_headers(connection, cseq) | {"Transport": reply}
        return build_response(200, _REASONS[200], headers), True

    def _play(
        self, connection: _Connection, message: RtspMessage, cseq: str
    ) -> tuple[bytes, bool]:
        del message
        if not connection.transports:
            return self._error(455, cseq), True

        headers = self._base_headers(connection, cseq) | {
            "Range": "npt=0.000-",
            "RTP-Info": self._rtp_info(connection),
        }
        connection.start_player()
        return build_response(200, _REASONS[200], headers), True

    def _rtp_info(self, connection: _Connection) -> str:
        """Tell the reader which sequence number and timestamp to expect first."""
        parts = []
        for track in self.source.tracks:
            if track.index not in connection.transports or not track.packets:
                continue
            header = rtp.parse(track.packets[0].data)
            if header is None:
                continue
            parts.append(
                f"url={self.rtsp_url()}/{track.control};"
                f"seq={header.sequence};rtptime={header.timestamp}"
            )
        return ",".join(parts)

    def _pause(
        self, connection: _Connection, message: RtspMessage, cseq: str
    ) -> tuple[bytes, bool]:
        """PAUSE is deliberately unimplemented.

        Resuming would have to restart the player mid-timeline with continuous
        RTP counters. Anything less rewinds the sequence numbers and timestamps
        the loop machinery works to keep monotonic, so a reader that "paused"
        would get a stream its decoder stalls on. A 501 is the honest answer.
        """
        del message, connection
        return self._error(501, cseq), True

    def _teardown(
        self, connection: _Connection, message: RtspMessage, cseq: str
    ) -> tuple[bytes, bool]:
        del message
        headers = self._base_headers(connection, cseq)
        connection.close()
        return build_response(200, _REASONS[200], headers), False

    def _keepalive(
        self, connection: _Connection, message: RtspMessage, cseq: str
    ) -> tuple[bytes, bool]:
        del message
        return build_response(200, _REASONS[200], self._base_headers(connection, cseq)), True


__all__ = ["ReplayServer", "Pacer"]
