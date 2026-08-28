"""Run and supervise the MediaMTX servers and their ffmpeg publishers."""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.error import URLError
from urllib.request import urlopen

from .ffmpeg import build_publish_command, effective_mode
from .mediamtx import ServerInstance, plan_instances, write_server_config
from .models import (
    CameraSpec,
    CameraStack,
    ReplaySpec,
    SimulationMode,
    SimulationSpec,
    StreamMode,
)
from .probe import MediaInfo, try_probe
from .service import vcam_command

logger = logging.getLogger("vcam")

#: Environment variable carrying the reader password to a spawned `vcam replay`.
REPLAY_PASSWORD_ENV = "VCAM_REPLAY_PASSWORD"

BACKOFF_BASE = 1.0
BACKOFF_MAX = 30.0
STABLE_RUNTIME = 20.0  # a publisher alive this long resets its backoff


@dataclass
class ManagedProcess:
    """A supervised child process with restart backoff."""

    name: str
    command: list[str]
    kind: str  # "server" or "publisher"
    env: Optional[dict[str, str]] = None
    """Extra environment for the child, merged over the supervisor's own.

    Used for secrets: argv is world-readable through /proc/<pid>/cmdline.
    """
    process: Optional[subprocess.Popen] = None
    restarts: int = 0
    consecutive_failures: int = 0
    started_at: Optional[float] = None
    last_exit_code: Optional[int] = None
    retry_at: float = 0.0
    gave_up: bool = False
    suspended: bool = False
    """When set, the monitor leaves this process alone: a simulation scheduler
    owns its stop/start cycle, so stops are planned rather than failures."""
    _reader: Optional[threading.Thread] = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @property
    def pid(self) -> Optional[int]:
        return self.process.pid if self.process is not None else None

    def start(self) -> bool:
        if self.running:
            return True  # already up (e.g. the monitor restarted it first)
        logger.debug("%s: %s", self.name, " ".join(self.command))
        try:
            self.process = subprocess.Popen(
                self.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env={**os.environ, **self.env} if self.env else None,
            )
        except OSError as exc:
            logger.error("%s: failed to start: %s", self.name, exc)
            self.process = None
            self.consecutive_failures += 1
            self.retry_at = time.monotonic() + self.backoff()
            return False

        self.started_at = time.monotonic()
        self._reader = threading.Thread(
            target=self._pump_output, args=(self.process,), daemon=True
        )
        self._reader.start()
        logger.info("%s: started (pid %s)", self.name, self.process.pid)
        return True

    def _pump_output(self, process: subprocess.Popen) -> None:
        stream = process.stdout
        if stream is None:
            return
        for raw in stream:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                logger.info("[%s] %s", self.name, line)
        stream.close()

    def backoff(self) -> float:
        return min(BACKOFF_BASE * (2 ** max(self.consecutive_failures - 1, 0)), BACKOFF_MAX)

    def note_exit(self, code: int, *, planned: bool = False) -> None:
        uptime = time.monotonic() - self.started_at if self.started_at else 0.0
        self.last_exit_code = code
        self.process = None
        if planned:
            # A scheduled simulation stop is not a failure: it must not feed the
            # restart backoff, nor burn part of the --max-restarts budget.
            self.consecutive_failures = 0
            return
        if uptime >= STABLE_RUNTIME:
            self.consecutive_failures = 1
        else:
            self.consecutive_failures += 1
        self.retry_at = time.monotonic() + self.backoff()

    def stop(self, timeout: float = 5.0) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            logger.info("%s: stopping", self.name)
            try:
                self.process.terminate()
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                logger.warning("%s: did not exit, killing", self.name)
                self.process.kill()
                self.process.wait()
            except OSError:
                pass
        self.process = None


@dataclass
class CameraRuntime:
    camera: CameraSpec
    instance: ServerInstance
    mode: StreamMode
    info: Optional[MediaInfo]
    read_url: str
    """Credential-free URL, safe for logs and the health file."""
    read_url_with_credentials: str
    """URL a reader can use directly; carries credentials when auth is enabled."""
    process: ManagedProcess
    scheduler: Optional["SimulationScheduler"] = None
    """Drives the dropout cycle of a flaky camera, None otherwise."""


@dataclass
class ReplayRuntime:
    replay: ReplaySpec
    read_url: str
    """Credential-free URL, safe for logs and the health file."""
    read_url_with_credentials: str
    process: ManagedProcess


def build_replay_command(
    replay: ReplaySpec,
    stack: CameraStack,
    *,
    host: str,
    verbose: bool = False,
) -> list[str]:
    """The `vcam replay` invocation the supervisor spawns for one capture."""
    command = [
        *vcam_command(),
        "replay",
        str(replay.source),
        "--path",
        replay.name,
        "--host",
        host,
        "--port",
        str(replay.port),
        "--speed",
        str(replay.speed),
    ]
    command.append("--loop" if replay.loop else "--no-loop")
    if not replay.rewrite_on_loop:
        command.append("--no-rewrite-on-loop")
    if replay.sdp is not None:
        command += ["--sdp", str(replay.sdp)]
    if stack.server.auth is not None:
        # The password goes through the environment instead: see
        # build_replay_env.
        command += ["--username", stack.server.auth.username]
    if verbose:
        command.append("--verbose")
    return command


def build_replay_env(stack: CameraStack) -> dict[str, str]:
    """Extra environment for a spawned `vcam replay`.

    The reader password is passed here rather than on the command line because
    argv is readable by every user on the box via /proc/<pid>/cmdline — the
    same reason the camera credentials go into a MediaMTX config file.
    """
    if stack.server.auth is None:
        return {}
    return {REPLAY_PASSWORD_ENV: stack.server.auth.password}


class SupervisorError(RuntimeError):
    """Raised when the supervisor cannot start."""


class SimulationScheduler:
    """Takes a `flaky` camera's publisher down and back up on a cycle.

    The publisher is flagged ``suspended`` for the duration, which keeps the
    monitor's crash-restart logic out of the scheduled cycle: a planned stop
    counts neither against the restart backoff nor the restart budget.

    Only ``flaky`` needs this. The other modes, ``stutter`` included, are
    expressed inside a single ffmpeg filter graph — swapping publishers mid-run
    tears the path's stream down and leaves attached readers stalled for good.
    """

    def __init__(self, runtime: "CameraRuntime", spec: "SimulationSpec", now: float) -> None:
        self.runtime = runtime
        self.spec = spec
        self.state = "up"  # "up" | "event"
        self._next_event = now + spec.interval

    @property
    def state_label(self) -> str:
        return "up" if self.state == "up" else "down"

    def tick(self, now: float) -> None:
        if now < self._next_event:
            return
        if self.state == "up":
            self._begin(now)
        else:
            self._end(now)

    def _begin(self, now: float) -> None:
        logger.info(
            "%s: [simulation] dropping the stream for %gs",
            self.runtime.camera.name,
            self.spec.duration,
        )
        self.runtime.process.suspended = True
        self.runtime.process.stop()
        self.state = "event"
        self._next_event = now + self.spec.duration

    def _end(self, now: float) -> None:
        logger.info("%s: [simulation] stream restored", self.runtime.camera.name)
        self.runtime.process.suspended = False
        self.runtime.process.start()
        self.state = "up"
        self._next_event = now + self.spec.interval


class Supervisor:
    """Owns the whole lifecycle: servers, publishers, health and shutdown."""

    def __init__(
        self,
        stack: CameraStack,
        mediamtx_binary: Path,
        *,
        work_dir: Path,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        ffmpeg_log_level: str = "warning",
        health_file: Optional[Path] = None,
        verify: bool = True,
        max_restarts: Optional[int] = None,
        on_ready: Optional[Callable[[list[CameraRuntime]], None]] = None,
    ) -> None:
        self.stack = stack
        self.mediamtx_binary = mediamtx_binary
        self.work_dir = work_dir
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.ffmpeg_log_level = ffmpeg_log_level
        self.health_file = health_file
        self.verify = verify
        self.max_restarts = max_restarts
        self.on_ready = on_ready

        self.instances: list[ServerInstance] = []
        self.servers: list[ManagedProcess] = []
        self.runtimes: list[CameraRuntime] = []
        self.replays: list[ReplayRuntime] = []
        self._stop = threading.Event()

    # -- lifecycle -----------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        def handler(signum: int, _frame: Any) -> None:
            logger.info("received signal %s, shutting down", signal.Signals(signum).name)
            self._stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except ValueError:
                # Not on the main thread (e.g. under a test runner); skip.
                pass

    def prepare(self) -> None:
        """Validate sources, plan instances and render server configs."""
        cameras = self.stack.enabled_cameras
        replays = self.stack.enabled_replays
        if not cameras and not replays:
            raise SupervisorError("no enabled cameras or replays to serve")

        missing = [camera for camera in cameras if not camera.source.is_file()]
        missing_captures = [replay for replay in replays if not replay.source.is_file()]
        if missing or missing_captures:
            details = "\n".join(
                f"  - {item.name}: {item.source}" for item in [*missing, *missing_captures]
            )
            raise SupervisorError(f"source file(s) not found:\n{details}")

        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.instances = plan_instances(self.stack)

        for instance in self.instances:
            write_server_config(instance, self.stack.server, self.work_dir)
            assert instance.config_path is not None
            self.servers.append(
                ManagedProcess(
                    name=instance.label,
                    kind="server",
                    command=[
                        str(self.mediamtx_binary),
                        str(instance.config_path),
                    ],
                )
            )

        for instance in self.instances:
            for camera in instance.cameras:
                info = try_probe(camera.source, ffprobe=self.ffprobe)
                mode = effective_mode(camera, info)
                command = build_publish_command(
                    camera,
                    self.stack.publish_url(camera),
                    info=info,
                    ffmpeg=self.ffmpeg,
                    log_level=self.ffmpeg_log_level,
                )
                runtime = CameraRuntime(
                    camera=camera,
                    instance=instance,
                    mode=mode,
                    info=info,
                    read_url=self.stack.read_url(camera, with_credentials=False),
                    read_url_with_credentials=self.stack.read_url(camera),
                    process=ManagedProcess(
                        name=camera.name, kind="publisher", command=command
                    ),
                )
                if camera.simulation.mode is SimulationMode.FLAKY:
                    runtime.scheduler = SimulationScheduler(
                        runtime, camera.simulation, time.monotonic()
                    )
                self.runtimes.append(runtime)

        verbose = logger.isEnabledFor(logging.DEBUG)
        for replay in replays:
            self.replays.append(
                ReplayRuntime(
                    replay=replay,
                    read_url=self.stack.replay_url(replay, with_credentials=False),
                    read_url_with_credentials=self.stack.replay_url(replay),
                    process=ManagedProcess(
                        name=replay.name,
                        kind="replay",
                        command=build_replay_command(
                            replay,
                            self.stack,
                            host=self.stack.server.host,
                            verbose=verbose,
                        ),
                        env=build_replay_env(self.stack) or None,
                    ),
                )
            )

    def run(self) -> int:
        """Start everything and block until interrupted. Returns an exit code."""
        self._install_signal_handlers()
        self.prepare()

        for server in self.servers:
            if not server.start():
                self.shutdown()
                raise SupervisorError(f"could not start {server.name}")

        for instance in self.instances:
            if not _wait_for_port("127.0.0.1", instance.rtsp_port, timeout=15.0):
                self.shutdown()
                raise SupervisorError(
                    f"MediaMTX did not open RTSP port {instance.rtsp_port} in time"
                )

        for runtime in self.runtimes:
            runtime.process.start()

        for replay in self.replays:
            replay.process.start()

        if self.verify:
            self._verify_streams()
            self._verify_replays()

        if self.on_ready is not None:
            self.on_ready(self.runtimes)

        return self._monitor()

    def _monitor(self) -> int:
        health_interval = 5.0
        last_health = 0.0

        while not self._stop.is_set():
            now = time.monotonic()

            for process in self._all_processes():
                if process.running:
                    continue
                if process.process is not None:
                    code = process.process.poll()
                    if process.suspended:
                        logger.info("%s: stopped (simulation)", process.name)
                        process.note_exit(code, planned=True)
                    else:
                        level = logger.info if self._stop.is_set() else logger.warning
                        level("%s: exited with code %s", process.name, code)
                        process.note_exit(code)
                    continue
                if process.suspended:
                    # The simulation scheduler owns this stop/start cycle.
                    continue
                if now < process.retry_at:
                    continue
                if (
                    self.max_restarts is not None
                    and process.restarts >= self.max_restarts
                ):
                    if not process.gave_up:
                        process.gave_up = True
                        logger.error(
                            "%s: giving up after %s restarts (--max-restarts)",
                            process.name,
                            process.restarts,
                        )
                    continue
                process.restarts += 1
                logger.info("%s: restart #%s", process.name, process.restarts)
                process.start()

            for runtime in self.runtimes:
                if runtime.scheduler is not None:
                    runtime.scheduler.tick(now)

            if self.health_file is not None and now - last_health >= health_interval:
                self._write_health()
                last_health = now

            self._stop.wait(1.0)

        self.shutdown()
        return 0

    def _all_processes(self) -> list[ManagedProcess]:
        return [
            *self.servers,
            *(runtime.process for runtime in self.runtimes),
            *(replay.process for replay in self.replays),
        ]

    def shutdown(self) -> None:
        self._stop.set()
        for replay in self.replays:
            replay.process.stop()
        for runtime in self.runtimes:
            runtime.process.stop()
        for server in self.servers:
            server.stop()
        if self.health_file is not None:
            self._write_health()

    # -- verification & health ----------------------------------------------

    def _verify_streams(self, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        pending = {runtime.camera.name: runtime for runtime in self.runtimes}

        while pending and time.monotonic() < deadline:
            for instance in self.instances:
                ready = _ready_paths(instance.api_url)
                for name in list(pending):
                    if pending[name].instance is instance and name in ready:
                        logger.info("%s: ready at %s", name, pending[name].read_url)
                        del pending[name]
            if pending:
                time.sleep(0.5)

        for name in pending:
            logger.warning("%s: not publishing yet (check the ffmpeg log above)", name)

    def _verify_replays(self, timeout: float = 15.0) -> None:
        for replay in self.replays:
            if _wait_for_port("127.0.0.1", replay.replay.port, timeout=timeout):
                logger.info("%s: ready at %s", replay.replay.name, replay.read_url)
            else:
                logger.warning(
                    "%s: replay did not open RTSP port %s", replay.replay.name, replay.replay.port
                )

    def health_snapshot(self) -> dict[str, Any]:
        return {
            "timestamp": time.time(),
            "servers": [
                {
                    "name": server.name,
                    "running": server.running,
                    "pid": server.pid,
                    "restarts": server.restarts,
                    "last_exit_code": server.last_exit_code,
                }
                for server in self.servers
            ],
            "cameras": [
                {
                    "name": runtime.camera.name,
                    "url": runtime.read_url,
                    "mode": runtime.mode.value,
                    "source": str(runtime.camera.source),
                    "running": runtime.process.running,
                    "pid": runtime.process.pid,
                    "restarts": runtime.process.restarts,
                    "last_exit_code": runtime.process.last_exit_code,
                    "simulation": runtime.camera.simulation.mode.value,
                    **(
                        {"simulation_state": runtime.scheduler.state_label}
                        if runtime.scheduler is not None
                        else {}
                    ),
                }
                for runtime in self.runtimes
            ],
            "replays": [
                {
                    "name": replay.replay.name,
                    "url": replay.read_url,
                    "source": str(replay.replay.source),
                    "running": replay.process.running,
                    "pid": replay.process.pid,
                    "restarts": replay.process.restarts,
                    "last_exit_code": replay.process.last_exit_code,
                }
                for replay in self.replays
            ],
        }

    def _write_health(self) -> None:
        if self.health_file is None:
            return
        try:
            self.health_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.health_file.with_suffix(self.health_file.suffix + ".tmp")
            tmp.write_text(json.dumps(self.health_snapshot(), indent=2), encoding="utf-8")
            os.replace(tmp, self.health_file)
        except OSError as exc:
            logger.error("could not write health file: %s", exc)


def _wait_for_port(host: str, port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _ready_paths(api_url: str) -> set[str]:
    """Names of paths currently receiving data, via the MediaMTX HTTP API."""
    try:
        with urlopen(f"{api_url}/v3/paths/list", timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (URLError, TimeoutError, json.JSONDecodeError, OSError):
        return set()
    return {
        item.get("name", "")
        for item in payload.get("items", [])
        if item.get("ready") and item.get("name")
    }
