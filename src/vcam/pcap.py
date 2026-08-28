"""PCAP/PCAPNG file I/O.

Captures reach us from wildly different places — ``tcpdump -i eth0`` writes
Ethernet frames, ``tcpdump -i any`` writes Linux cooked-mode (``SLL``/``SLL2``),
macOS ``lo0`` writes BSD loopback (``NULL``) — so the reader deliberately works
off scapy's decoded layers instead of assuming a link type.

scapy is imported lazily: it costs ~12 MB and a noticeable delay, and the vast
majority of ``vcam`` invocations never touch a capture file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator, Optional

#: Ports below this are almost never used for dynamically negotiated RTP.
_MIN_DYNAMIC_PORT = 1024

_scapy: Optional[SimpleNamespace] = None


class PcapError(RuntimeError):
    """Raised when a capture file cannot be read or written."""


def _layers() -> SimpleNamespace:
    """Import scapy on first use and cache the handful of names we need."""
    global _scapy
    if _scapy is not None:
        return _scapy
    try:
        from scapy.layers.inet import IP, TCP, UDP
        from scapy.layers.inet6 import IPv6
        from scapy.layers.l2 import Ether
        from scapy.packet import Padding, Raw
        from scapy.utils import PcapReader as _Reader
        from scapy.utils import PcapWriter as _Writer
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise PcapError(
            "reading capture files requires scapy; install the replay extra with "
            "`pip install 'vcam[replay]'` (or `uv sync --extra replay`)"
        ) from exc

    _scapy = SimpleNamespace(
        IP=IP,
        IPv6=IPv6,
        TCP=TCP,
        UDP=UDP,
        Ether=Ether,
        Padding=Padding,
        Raw=Raw,
        Reader=_Reader,
        Writer=_Writer,
    )
    return _scapy


def backend_version() -> str:
    """Return the scapy version backing capture I/O, for ``vcam doctor``."""
    _layers()
    import scapy  # noqa: PLC0415 - deliberately lazy

    return getattr(scapy, "__version__", "unknown")


@dataclass(frozen=True)
class Datagram:
    """One transport-layer payload recovered from a capture.

    ``tcp_seq`` is the TCP sequence number of the first payload byte, and is
    ``None`` for UDP. Reassembly needs it; nothing else does.
    """

    ts: float
    proto: str
    src: tuple[str, int]
    dst: tuple[str, int]
    payload: bytes
    tcp_seq: Optional[int] = None

    @property
    def flow(self) -> tuple[str, tuple[str, int], tuple[str, int]]:
        """Directional flow key: two opposite directions are two flows."""
        return (self.proto, self.src, self.dst)


def _strip_padding(payload_bytes: bytes, declared_length: Optional[int]) -> bytes:
    """Trim link-layer padding using the length declared by the IP/UDP header.

    Short UDP packets get padded to the 60-byte Ethernet minimum; without this
    the padding would be handed to the RTP parser as if it were payload.
    """
    if declared_length is None or declared_length < 0:
        return payload_bytes
    if declared_length > len(payload_bytes):
        return payload_bytes
    return payload_bytes[:declared_length]


def _endpoints(packet, layers: SimpleNamespace) -> Optional[tuple[str, str, int]]:
    """Return ``(src_ip, dst_ip, ip_payload_length)`` or ``None`` if not IP."""
    if packet.haslayer(layers.IP):
        ip = packet.getlayer(layers.IP)
        header_len = int(ip.ihl) * 4
        return str(ip.src), str(ip.dst), max(int(ip.len) - header_len, 0)
    if packet.haslayer(layers.IPv6):
        ip6 = packet.getlayer(layers.IPv6)
        return str(ip6.src), str(ip6.dst), int(ip6.plen)
    return None


def iter_datagrams(path: Path | str) -> Iterator[Datagram]:
    """Yield every UDP and TCP payload in *path*, in capture order.

    Non-IP packets, IP fragments beyond the first, and packets without a
    transport payload are skipped rather than raising: real captures are noisy
    and a single odd frame must not abort a replay.
    """
    layers = _layers()
    capture = Path(path).expanduser()
    if not capture.is_file():
        raise PcapError(f"capture file not found: {capture}")

    reader = None
    try:
        reader = layers.Reader(str(capture))
        for packet in reader:
            endpoints = _endpoints(packet, layers)
            if endpoints is None:
                continue
            src_ip, dst_ip, ip_payload_len = endpoints

            if packet.haslayer(layers.UDP):
                udp = packet.getlayer(layers.UDP)
                declared = int(udp.len) - 8 if udp.len else None
                payload = _strip_padding(bytes(udp.payload), declared)
                if not payload:
                    continue
                yield Datagram(
                    ts=float(packet.time),
                    proto="udp",
                    src=(src_ip, int(udp.sport)),
                    dst=(dst_ip, int(udp.dport)),
                    payload=payload,
                )
                continue

            if packet.haslayer(layers.TCP):
                tcp = packet.getlayer(layers.TCP)
                declared = ip_payload_len - int(tcp.dataofs) * 4 if ip_payload_len else None
                payload = _strip_padding(bytes(tcp.payload), declared)
                if not payload:
                    continue
                yield Datagram(
                    ts=float(packet.time),
                    proto="tcp",
                    src=(src_ip, int(tcp.sport)),
                    dst=(dst_ip, int(tcp.dport)),
                    payload=payload,
                    tcp_seq=int(tcp.seq),
                )
    except PcapError:
        raise
    except Exception as exc:  # scapy raises a wide range of parser errors
        raise PcapError(f"could not read capture file {capture}: {exc}") from exc
    finally:
        if reader is not None:
            reader.close()


class PcapWriter:
    """Write synthesised Ethernet frames to a ``.pcap`` file.

    The helpers build complete link-layer frames on purpose. Writing bare
    transport payloads (or, worse, arbitrary TCP stream chunks) produces a file
    that Wireshark and this module's own reader both reject.
    """

    def __init__(self, path: Path | str, *, flush_every_packets: int = 100) -> None:
        layers = _layers()
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._layers = layers
        self._writer = layers.Writer(str(self.path), append=False, sync=False)
        self._written = 0
        self._flush_every_packets = max(1, flush_every_packets)

    # -- low level -----------------------------------------------------------

    def write_frame(self, frame: bytes, ts: float) -> None:
        """Write a complete Ethernet frame verbatim."""
        packet = self._layers.Ether(frame)
        packet.time = ts
        self._writer.write(packet)
        self._written += 1
        if self._written % self._flush_every_packets == 0:
            self.flush()

    # -- convenience ---------------------------------------------------------

    def write_udp(
        self,
        payload: bytes,
        ts: float,
        *,
        src: tuple[str, int],
        dst: tuple[str, int],
    ) -> None:
        layers = self._layers
        frame = (
            layers.Ether()
            / layers.IP(src=src[0], dst=dst[0])
            / layers.UDP(sport=src[1], dport=dst[1])
            / layers.Raw(load=payload)
        )
        self.write_frame(bytes(frame), ts)

    def write_tcp(
        self,
        payload: bytes,
        ts: float,
        *,
        src: tuple[str, int],
        dst: tuple[str, int],
        seq: int,
        flags: str = "PA",
    ) -> None:
        layers = self._layers
        frame = (
            layers.Ether()
            / layers.IP(src=src[0], dst=dst[0])
            / layers.TCP(sport=src[1], dport=dst[1], seq=seq, flags=flags)
            / layers.Raw(load=payload)
        )
        self.write_frame(bytes(frame), ts)

    # -- lifecycle -----------------------------------------------------------

    def flush(self) -> None:
        self._writer.flush()

    def close(self) -> None:
        self._writer.close()

    def __enter__(self) -> "PcapWriter":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()


def is_dynamic_port(port: int) -> bool:
    return port >= _MIN_DYNAMIC_PORT
