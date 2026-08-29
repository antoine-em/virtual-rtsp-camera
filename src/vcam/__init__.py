"""Virtual RTSP camera CLI."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    # Read the version off the installed distribution rather than repeating it
    # here. Hardcoding it meant pyproject.toml and this file drifted apart, and
    # `vcam --version` under-reported by two releases before anyone noticed.
    __version__ = version("vcam")
except PackageNotFoundError:  # imported straight from a source tree
    __version__ = "0+unknown"

__all__ = ["__version__"]
