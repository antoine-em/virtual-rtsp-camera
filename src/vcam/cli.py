"""Typer command line interface for the virtual RTSP camera tool."""

from __future__ import annotations

import logging
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Annotated, Optional

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from . import __version__, binaries
from .ntp import NTPError, apply_offset, has_sys_time_cap, measure_offset, running_in_container
from .config import (
    ConfigError,
    dump_stack,
    example_stack,
    find_default_config,
    format_validation_error,
    load_stack,
    save_stack,
)
from .ffmpeg import build_publish_command, resolve_mode
from .mediamtx import plan_instances, render_server_config_yaml
from .models import (
    AuthSpec,
    CameraSpec,
    CameraStack,
    SimulationMode,
    SimulationSpec,
    StreamMode,
    Transport,
    VideoCodec,
    VideoSettings,
)
from .probe import ProbeError, probe as probe_source, try_probe
from .supervisor import CameraRuntime, Supervisor, SupervisorError
from . import service as _service
from .service import ServiceError

console = Console()
error_console = Console(stderr=True)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help=(
        "Serve local video files as looping virtual RTSP cameras.\n\n"
        "Cameras share one RTSP port and are addressed by path "
        "(rtsp://host:8554/cam1), unless a camera overrides `port` in its config."
    ),
)


# ---------------------------------------------------------------------------
# Shared option groups
# ---------------------------------------------------------------------------

ConfigOption = Annotated[
    Optional[Path],
    typer.Option(
        "--config",
        "-c",
        help="YAML config file (default: ./cameras.yaml, .yml, or vcam.yaml/.yml).",
    ),
]
HostOption = Annotated[
    Optional[str],
    typer.Option("--host", help="Bind address for the RTSP listener (default: 0.0.0.0)."),
]
DisplayHostOption = Annotated[
    Optional[str],
    typer.Option("--host", help="Hostname or IP to print in the URLs (default: from config)."),
]


def _fail(message: str, code: int = 1) -> "typer.Exit":
    error_console.print(f"[bold red]error:[/] {message}")
    return typer.Exit(code)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"vcam {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    pass


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def _video_overrides(
    resolution: Optional[str],
    fps: Optional[float],
    bitrate: Optional[str],
    codec: Optional[VideoCodec],
    encoder: Optional[str],
    gop: Optional[int],
    preset: Optional[str],
) -> dict[str, object]:
    overrides: dict[str, object] = {}
    for key, value in (
        ("resolution", resolution),
        ("fps", fps),
        ("bitrate", bitrate),
        ("codec", codec),
        ("encoder", encoder),
        ("gop", gop),
        ("preset", preset),
    ):
        if value is not None:
            overrides[key] = value
    return overrides


def _simulation_overrides(
    simulation: Optional[SimulationMode],
    noise_level: Optional[int],
    interval: Optional[float],
    duration: Optional[float],
    filters: Optional[str],
) -> dict[str, object]:
    overrides: dict[str, object] = {}
    for key, value in (
        ("mode", simulation),
        ("noise_level", noise_level),
        ("interval", interval),
        ("duration", duration),
        ("filters", filters),
    ):
        if value is not None:
            overrides[key] = value
    return overrides


def _parse_camera_argument(value: str) -> tuple[str, Path]:
    """Parse ``NAME=/path/to/file.mp4`` (or a bare path) into a camera tuple."""
    if "=" in value:
        name, _, source = value.partition("=")
        name = name.strip()
        source = source.strip()
        if not name or not source:
            raise typer.BadParameter(f"expected NAME=PATH, got {value!r}")
        return name, Path(source).expanduser()
    path = Path(value).expanduser()
    return _slug(path.stem), path


def _slug(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "_-." else "-" for char in value)
    return cleaned.strip("-.") or "cam"


def _load_stack_or_exit(config: Optional[Path]) -> CameraStack:
    path = config or find_default_config()
    if path is None:
        raise _fail(
            "no config file found. Pass --config, or create one with `vcam init`."
        )
    try:
        return load_stack(path)
    except ConfigError as exc:
        raise _fail(str(exc))


def _apply_overrides(
    stack: CameraStack,
    *,
    host: Optional[str],
    port: Optional[int],
    api_port: Optional[int],
    log_level: Optional[str],
    username: Optional[str],
    password: Optional[str],
    ntp_server: Optional[str],
    mode: Optional[StreamMode],
    loop: Optional[bool],
    realtime: Optional[bool],
    start_offset: Optional[float],
    transport: Optional[Transport],
    audio: Optional[bool],
    video: dict[str, object],
    simulation: dict[str, object],
) -> None:
    """Apply CLI overrides in place; every override applies to all cameras."""
    server = stack.server
    if host is not None:
        server.host = host
    if port is not None:
        server.rtsp_port = port
    if api_port is not None:
        server.api_port = api_port
    if log_level is not None:
        server.log_level = log_level
    if ntp_server is not None:
        server.ntp_server = ntp_server
    if username or password:
        if not (username and password):
            raise _fail("--username and --password must be provided together")
        server.auth = AuthSpec(username=username, password=password)

    for camera in stack.cameras:
        if mode is not None:
            camera.mode = mode
        if loop is not None:
            camera.loop = loop
        if realtime is not None:
            camera.realtime = realtime
        if start_offset is not None:
            camera.start_offset = start_offset
        if transport is not None:
            camera.transport = transport
        if audio is not None:
            camera.audio = audio
        if video:
            camera.video = camera.video.model_copy(update=video)
        if simulation:
            # Validated (unlike model_copy) so bad CLI values are reported
            # through the same ValidationError path as the rest.
            camera.simulation = SimulationSpec.model_validate(
                camera.simulation.model_dump() | simulation
            )


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@app.command()
def run(
    config: ConfigOption = None,
    source: Annotated[
        Optional[Path],
        typer.Option("--source", "-s", help="Video file to serve as a single camera."),
    ] = None,
    name: Annotated[
        Optional[str],
        typer.Option("--name", "-n", help="Camera name for --source (default: file stem)."),
    ] = None,
    camera: Annotated[
        Optional[list[str]],
        typer.Option(
            "--camera",
            help="Repeatable NAME=PATH pair, e.g. --camera cam1=videos/a.mp4.",
        ),
    ] = None,
    host: HostOption = None,
    port: Annotated[
        Optional[int], typer.Option("--port", "-p", help="Shared RTSP port (default: 8554).")
    ] = None,
    api_port: Annotated[
        Optional[int],
        typer.Option("--api-port", help="MediaMTX HTTP API port, loopback only (default: 9997)."),
    ] = None,
    mode: Annotated[
        Optional[StreamMode],
        typer.Option(
            "--mode",
            help="auto: copy when the source is H.264/HEVC, else transcode.",
        ),
    ] = None,
    loop: Annotated[
        Optional[bool], typer.Option("--loop/--no-loop", help="Loop the file forever (default: on).")
    ] = None,
    realtime: Annotated[
        Optional[bool],
        typer.Option(
            "--realtime/--no-realtime",
            help="Pace the file at native frame rate (default: on).",
        ),
    ] = None,
    start_offset: Annotated[
        Optional[float],
        typer.Option("--start-offset", help="Seek N seconds into the file (de-syncs feeds)."),
    ] = None,
    transport: Annotated[
        Optional[Transport],
        typer.Option("--transport", help="RTSP transport for publishing (default: tcp)."),
    ] = None,
    resolution: Annotated[
        Optional[str], typer.Option("--resolution", help="Transcode target, e.g. 1280x720.")
    ] = None,
    fps: Annotated[Optional[float], typer.Option("--fps", help="Transcode target frame rate.")] = None,
    bitrate: Annotated[
        Optional[str], typer.Option("--bitrate", help="Transcode target bitrate, e.g. 2M.")
    ] = None,
    codec: Annotated[Optional[VideoCodec], typer.Option("--codec", help="Transcode codec.")] = None,
    encoder: Annotated[
        Optional[str],
        typer.Option("--encoder", help="Explicit ffmpeg encoder, e.g. h264_nvenc (overrides --codec)."),
    ] = None,
    gop: Annotated[
        Optional[int], typer.Option("--gop", help="Keyframe interval in frames when transcoding.")
    ] = None,
    preset: Annotated[
        Optional[str],
        typer.Option("--preset", help="x264/x265 preset when transcoding (default: veryfast)."),
    ] = None,
    audio: Annotated[
        Optional[bool], typer.Option("--audio/--no-audio", help="Publish audio (off by default).")
    ] = None,
    simulation: Annotated[
        Optional[SimulationMode],
        typer.Option(
            "--simulation",
            help="Simulate a camera fault: noise, degraded, frozen, blackout, flaky, stutter.",
        ),
    ] = None,
    noise_level: Annotated[
        Optional[int],
        typer.Option(
            "--noise-level", min=1, max=100, help="Noise amplitude for --simulation noise (default 30)."
        ),
    ] = None,
    simulation_interval: Annotated[
        Optional[float],
        typer.Option(
            "--simulation-interval",
            min=0.1,
            help="Seconds between flaky/stutter events (default 30).",
        ),
    ] = None,
    simulation_duration: Annotated[
        Optional[float],
        typer.Option(
            "--simulation-duration",
            min=0.1,
            help="How long each flaky/stutter event lasts (default 5).",
        ),
    ] = None,
    simulation_filters: Annotated[
        Optional[str],
        typer.Option("--simulation-filters", help="Extra ffmpeg filters, e.g. 'gblur=sigma=2'."),
    ] = None,
    username: Annotated[
        Optional[str], typer.Option("--username", "-u", help="Require this user for readers.")
    ] = None,
    password: Annotated[
        Optional[str], typer.Option("--password", "-P", help="Password for --username.")
    ] = None,
    log_level: Annotated[
        Optional[str],
        typer.Option(
            "--server-log-level",
            help="MediaMTX log level: error, warn, info, debug (default: warn).",
        ),
    ] = None,
    ffmpeg_log_level: Annotated[
        str, typer.Option("--ffmpeg-log-level", help="ffmpeg -loglevel value.")
    ] = "warning",
    health_file: Annotated[
        Optional[Path],
        typer.Option("--health-file", help="Write a JSON health snapshot here every 5s."),
    ] = None,
    work_dir: Annotated[
        Optional[Path],
        typer.Option("--work-dir", help="Keep generated MediaMTX configs in this directory."),
    ] = None,
    mediamtx_binary: Annotated[
        Optional[Path], typer.Option("--mediamtx-binary", help="Use this MediaMTX binary.")
    ] = None,
    mediamtx_version: Annotated[
        str, typer.Option("--mediamtx-version", help="Release to download when not installed.")
    ] = binaries.DEFAULT_VERSION,
    download: Annotated[
        bool, typer.Option("--download/--no-download", help="Allow downloading MediaMTX.")
    ] = True,
    verify: Annotated[
        bool, typer.Option("--verify/--no-verify", help="Wait until every path is publishing.")
    ] = True,
    max_restarts: Annotated[
        Optional[int], typer.Option("--max-restarts", help="Give up after N restarts per process.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the plan and exit without starting anything.")
    ] = False,
    ntp_server: Annotated[
        Optional[str],
        typer.Option(
            "--ntp-server",
            help=(
                "Sync the container clock to this NTP server before starting "
                "(e.g. 192.168.198.151). Only valid inside a Docker container "
                "with cap_add: [SYS_TIME]. Rejected on bare CLI / service deployments."
            ),
        ),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    """Start the RTSP server and publish every camera. Runs until interrupted.

    With no --config/--source/--camera, the first of cameras.yaml, cameras.yml,
    vcam.yaml or vcam.yml in the current directory is used. Per-camera options
    below override the config for *every* camera in the stack.
    """
    stack = _build_stack(config=config, source=source, name=name, cameras=camera)

    try:
        _apply_overrides(
            stack,
            host=host,
            port=port,
            api_port=api_port,
            log_level=log_level,
            username=username,
            password=password,
            ntp_server=ntp_server,
            mode=mode,
            loop=loop,
            realtime=realtime,
            start_offset=start_offset,
            transport=transport,
            audio=audio,
            video=_video_overrides(resolution, fps, bitrate, codec, encoder, gop, preset),
            simulation=_simulation_overrides(
                simulation,
                noise_level,
                simulation_interval,
                simulation_duration,
                simulation_filters,
            ),
        )
    except ValidationError as exc:
        raise _fail(f"invalid options:\n{format_validation_error(exc)}")

    if not stack.enabled_cameras:
        raise _fail("no enabled cameras to serve")

    if dry_run:
        _print_dry_run(stack, ffmpeg_log_level)
        return

    _setup_logging(verbose)

    _run_ntp_sync(stack.server.ntp_server)

    try:
        binary = binaries.resolve_binary(
            mediamtx_binary,
            version=mediamtx_version,
            allow_download=download,
            on_event=lambda message: console.print(f"[dim]{message}[/]"),
        )
    except binaries.BinaryError as exc:
        raise _fail(str(exc))

    temp_dir: Optional[str] = None
    if work_dir is None:
        temp_dir = tempfile.mkdtemp(prefix="vcam-")
        resolved_work_dir = Path(temp_dir)
    else:
        resolved_work_dir = work_dir

    supervisor = Supervisor(
        stack,
        binary,
        work_dir=resolved_work_dir,
        ffmpeg_log_level=ffmpeg_log_level,
        health_file=health_file,
        verify=verify,
        max_restarts=max_restarts,
        on_ready=lambda runtimes: _print_ready(stack, runtimes),
    )

    try:
        raise typer.Exit(supervisor.run())
    except SupervisorError as exc:
        supervisor.shutdown()
        raise _fail(str(exc))
    except KeyboardInterrupt:
        supervisor.shutdown()
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


def _build_stack(
    *,
    config: Optional[Path],
    source: Optional[Path],
    name: Optional[str],
    cameras: Optional[list[str]],
) -> CameraStack:
    inline: list[CameraSpec] = []

    if source is not None:
        inline.append(
            CameraSpec(name=name or _slug(source.stem), source=Path(source).expanduser())
        )
    for entry in cameras or []:
        camera_name, camera_source = _parse_camera_argument(entry)
        inline.append(CameraSpec(name=camera_name, source=camera_source))

    if inline and config is not None:
        raise _fail("use either --config or --source/--camera, not both")

    if inline:
        try:
            return CameraStack(cameras=inline)
        except ValidationError as exc:
            raise _fail(f"invalid camera definition:\n{format_validation_error(exc)}")

    return _load_stack_or_exit(config)


def _run_ntp_sync(ntp_server: Optional[str]) -> None:
    """Query *ntp_server* and apply the measured offset. Container-only."""
    if ntp_server is None:
        return

    logger = logging.getLogger("vcam")

    if not running_in_container():
        raise _fail(
            f"--ntp-server ({ntp_server}) is only supported when running inside a Docker "
            "container.\nApplying it on a bare CLI or systemd service would skew the host "
            "system clock.\nRemove --ntp-server, or run vcam inside Docker with "
            "cap_add: [SYS_TIME]."
        )

    console.print(f"[dim]NTP: querying {ntp_server} …[/]")
    try:
        offset, rtt = measure_offset(ntp_server)
    except NTPError as exc:
        raise _fail(f"NTP query to {ntp_server} failed: {exc}")

    sign = "+" if offset >= 0 else ""
    if not has_sys_time_cap():
        console.print(
            f"[yellow]NTP:[/] measured offset {sign}{offset * 1000:.3f} ms "
            f"(RTT {rtt * 1000:.1f} ms) vs {ntp_server} — "
            "[yellow]not applied[/] (CAP_SYS_TIME unavailable; "
            "add [bold]cap_add: [SYS_TIME][/] to docker-compose.yml)"
        )
        return

    try:
        apply_offset(offset)
    except OSError as exc:
        raise _fail(f"Could not apply NTP offset: {exc}")

    action = "stepped" if abs(offset) > 0.128 else "slewed"
    logger.info(
        "NTP sync to %s: offset %s%+.3f ms applied via %s (RTT %.1f ms)",
        ntp_server,
        sign,
        offset * 1000,
        action,
        rtt * 1000,
    )
    console.print(
        f"[green]NTP:[/] {sign}{offset * 1000:.3f} ms {action} "
        f"(RTT {rtt * 1000:.1f} ms, server {ntp_server})"
    )


def _print_dry_run(stack: CameraStack, ffmpeg_log_level: str) -> None:
    instances = plan_instances(stack)
    for instance in instances:
        console.print(f"[bold]# mediamtx-{instance.rtsp_port}.yml[/]")
        console.print(render_server_config_yaml(instance, stack.server).rstrip())
        console.print()

    console.print("[bold]# publishers[/]")
    for camera in stack.enabled_cameras:
        info = try_probe(camera.source) if camera.source.is_file() else None
        command = build_publish_command(
            camera,
            stack.publish_url(camera),
            info=info,
            log_level=ffmpeg_log_level,
        )
        console.print(f"[dim]# {camera.name} -> {stack.read_url(camera)}[/]")
        console.print(" ".join(_quote(part) for part in command))
    console.print()
    _print_camera_table(stack)


def _quote(value: str) -> str:
    return f"'{value}'" if " " in value else value


def _print_ready(stack: CameraStack, runtimes: list[CameraRuntime]) -> None:
    table = Table(title="Virtual RTSP cameras", title_justify="left")
    table.add_column("camera", style="bold cyan")
    table.add_column("url")
    table.add_column("mode")
    table.add_column("sim")
    table.add_column("source", style="dim")
    for runtime in runtimes:
        table.add_row(
            runtime.camera.name,
            runtime.read_url_with_credentials,
            runtime.mode.value,
            _simulation_label(runtime.camera),
            str(runtime.camera.source),
        )
    console.print(table)
    if stack.server.auth is not None:
        console.print("[dim]readers must authenticate; credentials are embedded above[/]")
    console.print("[dim]press Ctrl-C to stop[/]")


def _simulation_label(camera: CameraSpec) -> str:
    """Short description of a camera's simulation for the CLI tables."""
    sim = camera.simulation
    if sim.mode is SimulationMode.NORMAL:
        return "-" if not sim.filters else "filters"
    if sim.is_temporal:
        return f"{sim.mode.value} {sim.duration:g}s/{sim.interval:g}s"
    return sim.mode.value


def _print_camera_table(stack: CameraStack, host: Optional[str] = None) -> None:
    table = Table(title="Cameras", title_justify="left")
    table.add_column("camera", style="bold cyan")
    table.add_column("url")
    table.add_column("mode")
    table.add_column("sim")
    table.add_column("loop")
    table.add_column("offset")
    table.add_column("source", style="dim")
    for camera in stack.cameras:
        table.add_row(
            camera.name if camera.enabled else f"[strike]{camera.name}[/]",
            stack.read_url(camera, host),
            camera.mode.value,
            _simulation_label(camera),
            "yes" if camera.loop else "no",
            f"{camera.start_offset:g}s",
            str(camera.source),
        )
    console.print(table)


# ---------------------------------------------------------------------------
# config management
# ---------------------------------------------------------------------------


@app.command()
def init(
    path: Annotated[Path, typer.Argument(help="Config file to create.")] = Path("cameras.yaml"),
    source: Annotated[
        Optional[list[Path]],
        typer.Option("--source", "-s", help="Repeatable video file to seed the config with."),
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Write a starter configuration file."""
    if path.exists() and not force:
        raise _fail(f"{path} already exists (use --force to overwrite)")
    stack = example_stack([Path(item).expanduser() for item in source] if source else None)
    save_stack(stack, path)
    console.print(f"wrote [bold]{path}[/]")


@app.command()
def add(
    source: Annotated[Path, typer.Argument(help="Video file to add as a camera.")],
    config: ConfigOption = None,
    name: Annotated[
        Optional[str], typer.Option("--name", "-n", help="Camera name (default: file stem).")
    ] = None,
    mode: Annotated[
        StreamMode,
        typer.Option(
            "--mode",
            help="auto: copy when the source is H.264/HEVC, else transcode.",
        ),
    ] = StreamMode.AUTO,
    simulation: Annotated[
        SimulationMode,
        typer.Option(
            "--simulation",
            help="Simulate a camera fault: noise, degraded, frozen, blackout, flaky, stutter.",
        ),
    ] = SimulationMode.NORMAL,
    start_offset: Annotated[
        float,
        typer.Option("--start-offset", help="Seek N seconds into the file (de-syncs feeds)."),
    ] = 0.0,
    port: Annotated[
        Optional[int],
        typer.Option(
            "--port",
            help="Dedicated RTSP port for this camera (spawns its own server instance).",
        ),
    ] = None,
    resolution: Annotated[
        Optional[str], typer.Option("--resolution", help="Transcode target, e.g. 1280x720.")
    ] = None,
    fps: Annotated[
        Optional[float], typer.Option("--fps", help="Transcode target frame rate.")
    ] = None,
    bitrate: Annotated[
        Optional[str], typer.Option("--bitrate", help="Transcode target bitrate, e.g. 2M.")
    ] = None,
    codec: Annotated[
        Optional[VideoCodec], typer.Option("--codec", help="Transcode codec (default: h264).")
    ] = None,
    encoder: Annotated[
        Optional[str],
        typer.Option(
            "--encoder", help="Explicit ffmpeg encoder, e.g. h264_nvenc (overrides --codec)."
        ),
    ] = None,
    gop: Annotated[
        Optional[int], typer.Option("--gop", help="Keyframe interval in frames when transcoding.")
    ] = None,
) -> None:
    """Append a camera to an existing configuration file.

    Only the most common per-camera settings are exposed here; edit the YAML
    directly for the rest (audio, transport, realtime, preset, simulation timing).
    """
    path = config or find_default_config()
    if path is None:
        raise _fail("no config file found. Create one with `vcam init`.")

    try:
        stack = load_stack(path)
    except ConfigError as exc:
        raise _fail(str(exc))

    overrides = _video_overrides(resolution, fps, bitrate, codec, encoder, gop, None)
    try:
        stack.cameras.append(
            CameraSpec(
                name=name or _slug(source.stem),
                # Stored absolute: relative paths in a config resolve against the
                # config directory, which is rarely the cwd `add` was run from.
                source=Path(source).expanduser().resolve(),
                mode=mode,
                start_offset=start_offset,
                port=port,
                video=VideoSettings(**overrides),  # type: ignore[arg-type]
                simulation=SimulationSpec(mode=simulation),
            )
        )
        CameraStack.model_validate(stack.model_dump())
    except ValidationError as exc:
        raise _fail(f"invalid camera definition:\n{format_validation_error(exc)}")

    save_stack(stack, path)
    console.print(f"added [bold]{stack.cameras[-1].name}[/] to {path}")


@app.command("list")
def list_cameras(config: ConfigOption = None, host: DisplayHostOption = None) -> None:
    """Show the cameras declared in a configuration file."""
    stack = _load_stack_or_exit(config)
    _print_camera_table(stack, host)


@app.command()
def urls(
    config: ConfigOption = None,
    host: DisplayHostOption = None,
    all_cameras: Annotated[
        bool, typer.Option("--all", help="Include disabled cameras.")
    ] = False,
) -> None:
    """Print one RTSP URL per line (handy for scripts and pipelines)."""
    stack = _load_stack_or_exit(config)
    cameras = stack.cameras if all_cameras else stack.enabled_cameras
    for camera in cameras:
        print(stack.read_url(camera, host))


@app.command()
def show(config: ConfigOption = None) -> None:
    """Print the fully resolved configuration as YAML."""
    stack = _load_stack_or_exit(config)
    console.print(dump_stack(stack).rstrip())


@app.command("clock-status")
def clock_status(
    ntp_server: Annotated[
        Optional[str],
        typer.Option(
            "--ntp-server",
            "-n",
            help="NTP server to measure offset against (e.g. 192.168.198.151).",
        ),
    ] = None,
) -> None:
    """Show clock status and measure offset against an NTP server (read-only)."""
    import time as _time

    in_container = running_in_container()
    has_cap = has_sys_time_cap()

    console.print(f"System time  : {_time.strftime('%Y-%m-%dT%H:%M:%S', _time.gmtime())} UTC")
    console.print(f"In container : {'[green]yes[/]' if in_container else '[yellow]no[/]'}")
    console.print(f"CAP_SYS_TIME : {'[green]yes[/]' if has_cap else '[yellow]no[/]'}")

    if ntp_server is None:
        if not in_container:
            console.print(
                "\n[dim]Tip: pass --ntp-server <host> to measure offset. "
                "NTP sync is only supported inside Docker containers.[/]"
            )
        return

    console.print(f"NTP server   : {ntp_server}")
    try:
        offset, rtt = measure_offset(ntp_server)
    except NTPError as exc:
        console.print(f"[red]NTP error    : {exc}[/]")
        raise typer.Exit(1)

    sign = "+" if offset >= 0 else ""
    console.print(f"Offset       : {sign}{offset * 1000:.3f} ms")
    console.print(f"RTT          : {rtt * 1000:.3f} ms")

    if not in_container:
        console.print(
            "\n[yellow]Note:[/] NTP sync ([bold]--ntp-server[/] on [bold]vcam run[/]) "
            "is only supported inside a Docker container to avoid touching the host clock."
        )
    elif not has_cap:
        console.print(
            "\n[yellow]Note:[/] CAP_SYS_TIME is not set — offset is measured but cannot "
            "be applied.\nAdd [bold]cap_add: [SYS_TIME][/] to docker-compose.yml."
        )


# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------


@app.command()
def probe(
    source: Annotated[Path, typer.Argument(help="Video file to inspect.")],
) -> None:
    """Show how a source file will be handled (codec, size, fps, chosen mode)."""
    try:
        info = probe_source(Path(source).expanduser())
    except ProbeError as exc:
        raise _fail(str(exc))

    camera = CameraSpec(name="probe", source=Path(source).expanduser())

    table = Table(title=str(info.path), title_justify="left")
    table.add_column("property", style="bold")
    table.add_column("value")
    table.add_row("codec", info.codec or "-")
    table.add_row("resolution", info.resolution or "-")
    table.add_row("fps", f"{info.fps:.3f}" if info.fps else "-")
    table.add_row("duration", f"{info.duration:.2f}s" if info.duration else "-")
    table.add_row("bitrate", f"{info.bitrate // 1000} kbit/s" if info.bitrate else "-")
    table.add_row("pixel format", info.pix_fmt or "-")
    table.add_row("audio track", "yes" if info.has_audio else "no")
    table.add_row("auto mode", resolve_mode(camera, info).value)
    console.print(table)


@app.command()
def doctor(
    mediamtx_binary: Annotated[
        Optional[Path], typer.Option("--mediamtx-binary", help="Check this MediaMTX binary.")
    ] = None,
    mediamtx_version: Annotated[
        str,
        typer.Option("--mediamtx-version", help="Release to look for in the local cache."),
    ] = binaries.DEFAULT_VERSION,
) -> None:
    """Check that ffmpeg, ffprobe and MediaMTX are available. Never downloads."""
    ok = True

    for tool in ("ffmpeg", "ffprobe"):
        location = shutil.which(tool)
        if location:
            console.print(f"[green]OK[/]   {tool}: {location}")
        else:
            ok = False
            console.print(f"[red]MISS[/] {tool}: not on PATH")

    try:
        binary = binaries.resolve_binary(
            mediamtx_binary, version=mediamtx_version, allow_download=False
        )
        console.print(f"[green]OK[/]   mediamtx: {binary}")
    except binaries.BinaryError:
        console.print(
            f"[yellow]WARN[/] mediamtx: not installed "
            f"(will download {mediamtx_version} on first run, or use `vcam install-server`)"
        )

    try:
        console.print(f"[dim]platform: {binaries.platform_slug()}[/]")
    except binaries.BinaryError as exc:
        ok = False
        console.print(f"[red]MISS[/] {exc}")

    if not ok:
        raise typer.Exit(1)


@app.command("install-server")
def install_server(
    version: Annotated[str, typer.Option("--version", help="MediaMTX release tag.")] = binaries.DEFAULT_VERSION,
    force: Annotated[bool, typer.Option("--force", help="Re-download even if cached.")] = False,
    verify: Annotated[
        bool, typer.Option("--verify/--no-verify", help="Verify the SHA-256 checksum.")
    ] = True,
) -> None:
    """Download the MediaMTX binary into the local cache."""
    target = binaries.install_path(version)
    if target.is_file() and not force:
        console.print(f"already installed: [bold]{target}[/]")
        return
    try:
        path = binaries.download(
            version=version,
            verify=verify,
            on_event=lambda message: console.print(f"[dim]{message}[/]"),
        )
    except binaries.BinaryError as exc:
        raise _fail(str(exc))
    console.print(f"installed [bold]{path}[/]")


# ---------------------------------------------------------------------------
# service management (systemd user unit / launchd agent)
# ---------------------------------------------------------------------------

service_app = typer.Typer(
    help=(
        "Run vcam as a persistent background service.\n\n"
        "Linux: a systemd user unit (no root needed).\n"
        "macOS: a launchd LaunchAgent."
    ),
    no_args_is_help=True,
)
app.add_typer(service_app, name="service")

_NameOption = Annotated[
    str,
    typer.Option("--name", "-n", help="Service / unit name (default: vcam)."),
]


@service_app.command()
def install(
    config: ConfigOption = None,
    name: _NameOption = "vcam",
) -> None:
    """Install vcam as a background service and start it immediately.

    Bakes the resolved config path into the unit so the service is
    self-contained.  Put all camera settings in the YAML file; edit the unit
    afterwards if you need extra CLI flags.
    """
    path = config or find_default_config()
    if path is None:
        raise _fail("no config file found; pass --config or create one with `vcam init`")
    try:
        load_stack(path)
    except ConfigError as exc:
        raise _fail(str(exc))
    try:
        summary = _service.install(name, Path(path).expanduser().resolve())
    except ServiceError as exc:
        raise _fail(str(exc))
    console.print(f"[green]installed[/] {summary}")


@service_app.command()
def start(name: _NameOption = "vcam") -> None:
    """Start a previously installed service."""
    try:
        summary = _service.start(name)
    except ServiceError as exc:
        raise _fail(str(exc))
    console.print(f"[green]started[/] {summary}")


@service_app.command()
def stop(name: _NameOption = "vcam") -> None:
    """Stop the running service (unit file is kept)."""
    try:
        summary = _service.stop(name)
    except ServiceError as exc:
        raise _fail(str(exc))
    console.print(f"[yellow]stopped[/] {summary}")


@service_app.command()
def status(name: _NameOption = "vcam") -> None:
    """Show whether the service is installed and running."""
    try:
        svc_status = _service.status(name)
    except ServiceError as exc:
        raise _fail(str(exc))
    style = "green" if svc_status.active else ("yellow" if svc_status.installed else "red")
    console.print(f"[{style}]{svc_status.line}[/]")
    if not svc_status.active:
        raise typer.Exit(3)


@service_app.command()
def uninstall(name: _NameOption = "vcam") -> None:
    """Stop and remove the unit / plist file."""
    try:
        summary = _service.uninstall(name)
    except ServiceError as exc:
        raise _fail(str(exc))
    console.print(f"[red]removed[/] {summary}")


@service_app.command()
def logs(name: _NameOption = "vcam") -> None:
    """Stream the service log (journalctl on Linux, tail on macOS)."""
    try:
        _service.tail_logs(name)
    except ServiceError as exc:
        raise _fail(str(exc))
    except KeyboardInterrupt:
        pass


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:  # pragma: no cover - interactive path
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
