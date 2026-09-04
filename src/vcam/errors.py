"""Unified exception hierarchy for vcam."""

from __future__ import annotations


class VcamError(RuntimeError):
    """Base exception for all vcam errors."""


class ConfigError(VcamError):
    """Configuration is invalid or missing."""


class BinaryError(VcamError):
    """A binary (MediaMTX, ffmpeg) cannot be resolved or executed."""


class ProbeError(VcamError):
    """Media probing failed (ffprobe)."""


class ServiceError(VcamError):
    """Service management (systemd/launchd) failed."""


class SupervisorError(VcamError):
    """Process supervision (start, restart, stop) failed."""


class PcapError(VcamError):
    """PCAP file parsing or handling failed."""


class ReplaySourceError(VcamError):
    """Replay source initialization or playback failed."""


class RtspFramingError(VcamError):
    """RTSP message parsing or framing error."""


class NTPError(VcamError):
    """NTP time synchronization failed."""
