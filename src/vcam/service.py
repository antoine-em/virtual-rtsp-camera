"""Manage vcam as a background service (systemd user unit or launchd agent).

The ``vcam run`` supervisor is already daemon-ready: it blocks until signalled and
installs SIGTERM/SIGINT handlers that stop publishers and then servers cleanly.
A service only needs to start that process and keep it alive.

Resolution:
- **Linux** → ``systemctl --user`` (user-session unit, no root required)
- **macOS** → ``launchctl`` (LaunchAgent in ``~/Library/LaunchAgents``)
"""

from __future__ import annotations

import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .errors import ServiceError

SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class ServiceStatus:
    line: str
    active: bool
    installed: bool


# ---------------------------------------------------------------------------
# backend detection
# ---------------------------------------------------------------------------


def detect_backend() -> str | None:
    """Return the service backend for this OS: ``'systemd'`` or ``'launchd'``, else ``None``."""
    system = platform.system()
    if system == "Linux":
        return "systemd"
    if system == "Darwin":
        return "launchd"
    return None


def _require_backend() -> str:
    backend = detect_backend()
    if backend is None:
        raise ServiceError(
            f"service management is not supported on {platform.system()}; "
            "only Linux (systemd) and macOS (launchd) are supported"
        )
    return backend


# ---------------------------------------------------------------------------
# subprocess helper — isolated so tests can monkeypatch it
# ---------------------------------------------------------------------------


def _run(
    argv: list[str],
    *,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(list(argv), check=check, text=True, capture_output=capture)
    except FileNotFoundError as exc:
        raise ServiceError(f"command not found on PATH: {exc.filename}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        msg = f"`{' '.join(argv)}` exited {exc.returncode}"
        raise ServiceError(f"{msg}: {detail}" if detail else msg) from exc


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


def vcam_command() -> list[str]:
    """Return the command list to launch vcam.

    Prefer a ``vcam`` executable on ``$PATH``; fall back to
    ``sys.executable -m vcam`` so the service unit still works when vcam is
    installed in a venv but not symlinked onto the system PATH.
    """
    on_path = shutil.which("vcam")
    if on_path:
        return [on_path]
    return [sys.executable, "-m", "vcam"]


def systemd_user_dir() -> Path:
    """``~/.config/systemd/user`` (or ``$XDG_CONFIG_HOME/systemd/user``)."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "systemd" / "user"


def systemd_unit_path(name: str) -> Path:
    return systemd_user_dir() / f"{name}.service"


def launchd_plist_path(name: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{name}.plist"


def launchd_log_path(name: str) -> Path:
    return Path.home() / "Library" / "Logs" / f"vcam-{name}.log"


# ---------------------------------------------------------------------------
# unit / plist rendering
# ---------------------------------------------------------------------------


def _shell_quote(part: str) -> str:
    """Minimal quoting for a systemd ExecStart value."""
    return f'"{part}"' if any(ch.isspace() for ch in part) else part


def render_systemd_unit(name: str, command: list[str], config_path: Path) -> str:
    """Render a systemd user unit file for ``vcam run -c <config_path>``."""
    exe = " ".join(_shell_quote(p) for p in [*command, "run", "-c", str(config_path)])
    lines = [
        "[Unit]",
        f"Description=vcam virtual RTSP camera service ({name})",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={config_path.parent}",
        f"ExecStart={exe}",
        "Restart=always",
        "RestartSec=5",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def render_launchd_plist(name: str, command: list[str], config_path: Path) -> bytes:
    """Render a launchd agent plist for ``vcam run -c <config_path>``."""
    payload: dict = {
        "Label": name,
        "ProgramArguments": [*command, "run", "-c", str(config_path)],
        "WorkingDirectory": str(config_path.parent),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(launchd_log_path(name)),
        "StandardErrorPath": str(launchd_log_path(name)),
    }
    return plistlib.dumps(payload)


# ---------------------------------------------------------------------------
# name validation
# ---------------------------------------------------------------------------


def check_name(name: str) -> None:
    if not SERVICE_NAME_RE.match(name):
        raise ServiceError(
            f"invalid service name {name!r}; use letters, digits, '_', '-' or '.' "
            "starting with a letter or digit"
        )


# ---------------------------------------------------------------------------
# launchd helpers
# ---------------------------------------------------------------------------


def _launchd_domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl_unload(name: str, path: Path) -> None:
    """Attempt to stop and unload a launchd agent; ignore failure (it may not be loaded)."""
    domain = _launchd_domain()
    result = _run(["launchctl", "bootout", domain, name], check=False)
    if result.returncode != 0:
        _run(["launchctl", "unload", str(path)], check=False)


def _launchctl_load(path: Path) -> None:
    """Load a launchd agent, trying the modern bootstrap verb first."""
    domain = _launchd_domain()
    result = _run(["launchctl", "bootstrap", domain, str(path)], check=False)
    if result.returncode != 0:
        _run(["launchctl", "load", "-w", str(path)])


# ---------------------------------------------------------------------------
# install / start / stop / status / uninstall / tail_logs
# ---------------------------------------------------------------------------


def install(name: str, config_path: Path) -> str:
    """Write the unit/agent file and start the service. Returns a summary message."""
    check_name(name)
    backend = _require_backend()
    config_path = Path(config_path).expanduser().resolve()
    if not config_path.is_file():
        raise ServiceError(f"config file not found: {config_path}")

    command = vcam_command()

    if backend == "systemd":
        path = systemd_unit_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_systemd_unit(name, command, config_path), encoding="utf-8")
        _run(["systemctl", "--user", "daemon-reload"])
        _run(["systemctl", "--user", "enable", "--now", f"{name}.service"])
        return (
            f"installed systemd user unit {path}\n"
            f"  logs:  journalctl --user -u {name}.service -f\n"
            f"  hint:  headless boxes need `sudo loginctl enable-linger $USER`"
        )

    # launchd
    path = launchd_plist_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    log = launchd_log_path(name)
    log.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_launchd_plist(name, command, config_path))
    _launchctl_unload(name, path)
    _launchctl_load(path)
    return f"installed launchd agent {path}\n  logs:  tail -f {log}"


def start(name: str) -> str:
    """Start a previously installed service."""
    check_name(name)
    backend = _require_backend()
    if backend == "systemd":
        unit = f"{name}.service"
        if not systemd_unit_path(name).is_file():
            raise ServiceError(f"{unit} is not installed; run `vcam service install` first")
        _run(["systemctl", "--user", "start", unit])
        return f"started {unit}"
    path = launchd_plist_path(name)
    if not path.is_file():
        raise ServiceError(f"{name} is not installed; run `vcam service install` first")
    _launchctl_load(path)
    return f"loaded {path}"


def stop(name: str) -> str:
    """Stop a running service (the unit/plist file is kept)."""
    check_name(name)
    backend = _require_backend()
    if backend == "systemd":
        unit = f"{name}.service"
        result = _run(["systemctl", "--user", "stop", unit], check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            if not systemd_unit_path(name).is_file():
                raise ServiceError(f"{unit} is not installed")
            raise ServiceError(f"could not stop {unit}: {detail}")
        return f"stopped {unit}"
    path = launchd_plist_path(name)
    if not path.is_file():
        raise ServiceError(f"{name} is not installed")
    _launchctl_unload(name, path)
    return f"stopped {name}"


def status(name: str) -> ServiceStatus:
    """Return the current service status."""
    check_name(name)
    backend = _require_backend()

    if backend == "systemd":
        unit = f"{name}.service"
        if not systemd_unit_path(name).is_file():
            return ServiceStatus(f"{unit}: not installed", active=False, installed=False)
        active = _run(["systemctl", "--user", "is-active", unit], check=False).stdout.strip()
        enabled = _run(["systemctl", "--user", "is-enabled", unit], check=False).stdout.strip()
        label = f"{unit}: {active or 'unknown'}"
        if enabled and enabled not in ("unknown", ""):
            label += f", {enabled}"
        return ServiceStatus(label, active=(active == "active"), installed=True)

    # launchd
    path = launchd_plist_path(name)
    if not path.is_file():
        return ServiceStatus(f"{name}: not installed", active=False, installed=False)
    result = _run(["launchctl", "list", name], check=False)
    if result.returncode != 0:
        return ServiceStatus(
            f"{name}: installed but not loaded (run `vcam service start`)",
            active=False,
            installed=True,
        )
    first_line = (result.stdout.strip().splitlines() or [""])[0]
    parts = first_line.split("\t")
    pid = parts[0].strip() if parts else "-"
    if pid and pid != "-":
        return ServiceStatus(f"{name}: running (pid {pid})", active=True, installed=True)
    return ServiceStatus(f"{name}: loaded but not running", active=False, installed=True)


def uninstall(name: str) -> str:
    """Disable and remove the unit/plist file."""
    check_name(name)
    backend = _require_backend()
    if backend == "systemd":
        unit = f"{name}.service"
        path = systemd_unit_path(name)
        if not path.is_file():
            raise ServiceError(f"{unit} is not installed")
        _run(["systemctl", "--user", "disable", "--now", unit], check=False)
        path.unlink()
        _run(["systemctl", "--user", "daemon-reload"], check=False)
        return f"removed {path}"
    path = launchd_plist_path(name)
    if not path.is_file():
        raise ServiceError(f"{name} is not installed")
    _launchctl_unload(name, path)
    path.unlink()
    return f"removed {path}"


def tail_logs(name: str) -> None:
    """Stream the service log until interrupted."""
    check_name(name)
    backend = _require_backend()
    if backend == "systemd":
        argv = ["journalctl", "--user", "-f", "-u", f"{name}.service"]
    else:
        argv = ["tail", "-n", "100", "-f", str(launchd_log_path(name))]
    _run(argv, check=False, capture=False)
