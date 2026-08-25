---
goal: RTSP traffic capture (proxy mode) and PCAP replay for virtual camera
version: 1.0
date_created: 2026-08-26
owner: antoine-em
status: 'Planned'
tags: [feature, architecture, networking, debugging]
---

# Introduction

![Status: Planned](https://img.shields.io/badge/status-Planned-blue)

This plan adds two complementary capabilities to `vcam`:

1. **Proxy mode** — vcam sits between a real IP camera and the EdgeAI Station. It transparently forwards RTSP traffic while capturing the raw RTP/RTSP packets into a PCAP file (equivalent to a Wireshark capture). Useful to record real-world traffic from production cameras for later reproduction.

2. **PCAP replay mode** — vcam reads a previously recorded PCAP file (`.pcap` / `.pcapng`) and replays the original RTP packets over a fresh RTSP session toward any consumer (e.g., DeepStream on the EdgeAI Station). The goal is **exact wire-level reproduction**: same codec, same bitrate bursts, same packet timing — enabling reproduction of subtle pipeline errors that a re-encoded video file would never trigger.

---

## 1. Requirements & Constraints

- **REQ-001**: `vcam proxy` sub-command must accept an upstream RTSP URL (real camera) and expose a local RTSP path; all readers receive a live tee of the upstream stream.
- **REQ-002**: While proxying, every RTP/RTCP/RTSP packet (including RTSP handshake messages) must be saved verbatim into a PCAP file with original timestamps.
- **REQ-003**: `vcam replay` sub-command must accept a PCAP file path and serve it as an RTSP stream; packet inter-arrival timing must be preserved (±1 ms tolerance at replay).
- **REQ-004**: Both sub-commands must integrate with the existing `cameras.yaml` configuration format; camera entries must support `mode: proxy` and `mode: replay` source types.
- **REQ-005**: The proxy PCAP capture must survive the upstream camera going offline and resuming (capture file is flushed and kept valid at all times).
- **REQ-006**: Replay must support looping the PCAP (same flag semantics as the existing video loop behaviour).
- **REQ-007**: The existing `run` sub-command and all current behaviour must be fully preserved; this plan introduces additive functionality only.
- **SEC-001**: PCAP files may contain credentials embedded in RTSP `DESCRIBE` / `ANNOUNCE` headers; the tool must log a warning when writing such a capture and must document redaction procedures.
- **CON-001**: Must run on `linux/amd64` and `linux/arm64` (Jetson Orin target). macOS support for development is required but performance is best-effort.
- **CON-002**: No new mandatory system-level dependencies may be added to the Docker image beyond what ships in the base image (`python:3.12-slim`). Any C-level packet capture library (`libpcap`) must be handled as an optional install with a graceful error message when absent.
- **CON-003**: PCAP replay must not require a running `ffmpeg` process — packets are injected directly into MediaMTX via its TCP interleaved RTSP channel or via a thin Python RTP sender.
- **GUD-001**: Follow the existing module layout (`src/vcam/`); new source files must each have a single clear responsibility.
- **GUD-002**: All new CLI parameters must follow the existing Click conventions in `cli.py`; long-form options only, with short aliases where existing patterns do so.
- **GUD-003**: New code must achieve ≥ 90 % line coverage via `pytest`; mocks must substitute real network I/O and `libpcap` in unit tests.
- **PAT-001**: Use the existing `Supervisor` / `Service` pattern for managing the new proxy and replay background processes.

---

## 2. Implementation Steps

### Phase 1 — Research & Feasibility Spike

- GOAL-001: Confirm technical approach for each sub-feature; document findings; decide on Python libraries.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | **Evaluate packet-capture libraries.** Compare `pyshark` (wraps TShark), `scapy`, and `python-libpcap` for PCAP write performance on aarch64. Identify which supports writing live-captured packets to a `.pcapng` file with nanosecond timestamps. Document choice in a `docs/adr/` file. | | |
| TASK-002 | **Evaluate RTP injection approaches.** Compare: (a) feeding extracted RTP payload back through `ffmpeg -f rtp_mpegts` re-mux, (b) using Python `socket` to send raw UDP RTP packets, (c) using MediaMTX's SRT/RTSP push API. Determine which preserves timing and avoids re-encoding. Document choice in `docs/adr/`. | | |
| TASK-003 | **Prototype proxy interception.** Confirm that MediaMTX `source: rtsp://…` (pull mode) can be wrapped so Python can observe the RTP stream before MediaMTX re-serves it, OR confirm that a Python RTSP reverse-proxy must be written. Identify the minimal viable approach. | | |
| TASK-004 | **Confirm PCAP replay with real DeepStream pipeline.** On the DEX5W000001 test station, capture 30 s of real camera traffic via `tcpdump`, then attempt replay with `tcpreplay`; verify DeepStream receives and decodes frames. This confirms wire-level replay is feasible before writing Python code. | | |

### Phase 2 — Core Infrastructure

- GOAL-002: Lay the shared building blocks required by both proxy and replay modes.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-005 | **Add `pcap.py` module** at `src/vcam/pcap.py`. Implement `PcapWriter` class: open a `.pcapng` file for writing, expose `write_packet(data: bytes, ts: float)` method, call `flush()` periodically (every 1 s or every 100 packets). Implement `PcapReader` class: iterate over packets yielding `(timestamp: float, data: bytes)` tuples. Use the library chosen in TASK-001. | | |
| TASK-006 | **Add `rtp_sender.py` module** at `src/vcam/rtp_sender.py`. Implement `RtpSender` class: open a UDP socket (or TCP interleaved socket toward MediaMTX), accept `(rtp_packet: bytes, send_at: float)`, block until `send_at` wall-clock time, then send. Support cancel/shutdown. | | |
| TASK-007 | **Extend `models.py`** — add `ProxyCamera` and `ReplayCamera` dataclasses alongside the existing `Camera` model. Both must be serialisable to/from the `cameras.yaml` schema; add Pydantic validators. | | |
| TASK-008 | **Extend `config.py`** — update the YAML loader to parse `mode: proxy` (requires `upstream_url`, optional `capture_path`) and `mode: replay` (requires `source` pointing to a `.pcap`/`.pcapng` file, optional `loop: true`). Maintain backward compatibility with existing `mode: file` (default). | | |

### Phase 3 — Proxy Mode

- GOAL-003: Implement `vcam proxy` — transparent RTSP reverse-proxy with simultaneous PCAP capture.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-009 | **Add `proxy.py` module** at `src/vcam/proxy.py`. Implement `RtspProxy` class: (a) connect to upstream RTSP URL using `asyncio` + raw TCP socket; (b) relay all bytes bidirectionally to downstream MediaMTX ANNOUNCE path; (c) fork each inbound RTP packet to `PcapWriter.write_packet()`. Handle RTSP `TEARDOWN` and upstream disconnects gracefully — attempt reconnection up to configurable retries. | | |
| TASK-010 | **Implement `ProxyService`** (extends `Service`) in `proxy.py`. It starts `RtspProxy` inside an `asyncio` event loop thread, mirrors the `start()` / `stop()` interface used by `FfmpegService` today. | | |
| TASK-011 | **Register `ProxyService` in `supervisor.py`** — when a `ProxyCamera` is present in config, instantiate `ProxyService` instead of `FfmpegService`. No changes to the supervisor's generic start/stop/restart logic. | | |
| TASK-012 | **Add `vcam proxy` CLI command** in `cli.py`. Options: `--upstream URL` (required), `--path TEXT` (local RTSP path, default derived from upstream path), `--capture FILE` (output PCAP path, default `./captures/<date>_<path>.pcapng`), `--port INT` (default 8554). Reuse existing `--username` / `--password` options. | | |
| TASK-013 | **SEC-001 credential warning** — scan RTSP `DESCRIBE` / `ANNOUNCE` request lines in `RtspProxy` for `Authorization:` headers; if found, emit a `logging.WARNING` with instructions for redacting PCAP files (`editcap -E`). | | |

### Phase 4 — PCAP Replay Mode

- GOAL-004: Implement `vcam replay` — serve a captured PCAP as a live RTSP stream.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-014 | **Add `replay.py` module** at `src/vcam/replay.py`. Implement `PcapReplayService` (extends `Service`): (a) parse the PCAP using `PcapReader`; (b) extract the RTP stream(s) from the capture (filter by UDP packets on port 5004 or identify port from the recorded RTSP SDP); (c) publish via `RtpSender` to a MediaMTX RTSP path using SDP derived from the capture; (d) loop if `loop=True` by seeking back to first packet. | | |
| TASK-015 | **SDP reconstruction** — implement `sdp_from_pcap(pcap_path) -> str` in `replay.py`. Parse the recorded RTSP `DESCRIBE` response inside the PCAP to extract the original SDP. Fall back to heuristic detection (payload type 96 = H.264, etc.) if RTSP handshake was not captured. | | |
| TASK-016 | **Register `PcapReplayService` in `supervisor.py`** — analogous to TASK-011. | | |
| TASK-017 | **Add `vcam replay` CLI command** in `cli.py`. Options: `--source FILE` (required, `.pcap` or `.pcapng`), `--path TEXT` (local RTSP path), `--port INT` (default 8554), `--loop / --no-loop` (default `--loop`), `--speed FLOAT` (playback speed multiplier, default 1.0), existing `--username` / `--password`. | | |

### Phase 5 — Integration & Hardening

- GOAL-005: End-to-end validation, documentation, and Docker packaging.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-018 | **`vcam doctor` checks** — extend `probe.py` to verify `libpcap` is present and writable; print a clear error if absent with install instructions (`apt install libpcap-dev` / `brew install libpcap`). | | |
| TASK-019 | **Update `cameras.yaml` examples** — add a `proxy` camera entry and a `replay` camera entry to the sample config. Update README with the two new modes, usage examples, and a troubleshooting section for PCAP credential redaction. | | |
| TASK-020 | **Dockerfile update** — add `RUN apt-get install -y libpcap-dev` in `Dockerfile` (already running Debian-based). Pin to the package version present in the base image's apt cache to ensure reproducibility. | | |
| TASK-021 | **End-to-end test on DEX5W000001** — (a) run `vcam proxy` pointing at a real camera, capture 60 s, verify PCAP is valid with `capinfos`; (b) run `vcam replay` from the PCAP, connect DeepStream pipeline, verify frame delivery and no pipeline errors over a 5-minute window. | | |

---

## 3. Alternatives

- **ALT-001**: Use `tcpdump` / `tcpreplay` as external system tools instead of Python-native PCAP I/O. Rejected because it adds mandatory system-level binaries, is harder to integrate into the Python service lifecycle, and prevents portable Docker packaging on arm64 without cross-compilation.
- **ALT-002**: Re-encode PCAP video to an MP4 and replay via the existing ffmpeg path. Rejected because re-encoding destroys the original packet timing and codec parameters that are needed to reproduce bitstream-level pipeline errors.
- **ALT-003**: Extend ffmpeg with `-f rtsp` and `-use_wallclock_as_timestamps` as a relay. Rejected because ffmpeg re-muxes RTP packets and cannot faithfully reproduce original packet inter-arrival times or fragmentation patterns.
- **ALT-004**: Build a standalone proxy binary in Go (closer to MediaMTX ecosystem). Rejected for scope and maintainability reasons; Python is the project language; a Go sub-binary would fragment the distribution story.

---

## 4. Dependencies

- **DEP-001**: `libpcap` / `libpcap-dev` — system library for low-level packet capture and PCAP file I/O. Required for proxy capture and replay parsing. Optional on the dev machine; mandatory in Docker image.
- **DEP-002**: Python PCAP binding — one of `python-libpcap`, `scapy`, or `pyshark` (chosen in TASK-001). Added to `pyproject.toml` as an optional dependency group `[pcap]`.
- **DEP-003**: `asyncio` (stdlib) — used in `proxy.py` for concurrent upstream ↔ downstream TCP relay.
- **DEP-004**: `MediaMTX` — existing dependency; proxy mode requires MediaMTX ≥ 1.6 which supports RTSP ANNOUNCE (publish) from external sources. Verify version constraint in `binaries.py`.

---

## 5. Files

- **FILE-001**: `src/vcam/pcap.py` — new module; `PcapWriter` and `PcapReader` classes.
- **FILE-002**: `src/vcam/rtp_sender.py` — new module; `RtpSender` class for timing-accurate UDP delivery.
- **FILE-003**: `src/vcam/proxy.py` — new module; `RtspProxy` and `ProxyService`.
- **FILE-004**: `src/vcam/replay.py` — new module; `PcapReplayService` and `sdp_from_pcap`.
- **FILE-005**: `src/vcam/models.py` — extend with `ProxyCamera` and `ReplayCamera` dataclasses.
- **FILE-006**: `src/vcam/config.py` — extend YAML loader for `proxy` and `replay` camera modes.
- **FILE-007**: `src/vcam/supervisor.py` — register new service types.
- **FILE-008**: `src/vcam/cli.py` — add `proxy` and `replay` sub-commands.
- **FILE-009**: `src/vcam/probe.py` — extend `doctor` checks for `libpcap`.
- **FILE-010**: `Dockerfile` — add `libpcap-dev` install step.
- **FILE-011**: `cameras.yaml` — add example proxy and replay entries.
- **FILE-012**: `README.md` — document proxy and replay modes.
- **FILE-013**: `docs/adr/001-pcap-library.md` — new ADR for library choice (TASK-001).
- **FILE-014**: `docs/adr/002-rtp-injection.md` — new ADR for RTP injection approach (TASK-002).
- **FILE-015**: `tests/test_pcap.py` — unit tests for `pcap.py`.
- **FILE-016**: `tests/test_rtp_sender.py` — unit tests for `rtp_sender.py`.
- **FILE-017**: `tests/test_proxy.py` — unit tests for `proxy.py`.
- **FILE-018**: `tests/test_replay.py` — unit tests for `replay.py`.
- **FILE-019**: `tests/test_config_proxy_replay.py` — config parsing tests for new camera modes.

---

## 6. Testing

- **TEST-001**: `PcapWriter` writes a valid `.pcapng` file readable by `PcapReader`; round-trip of 1000 synthetic packets preserves byte content and timestamps within 1 µs. (FILE-015)
- **TEST-002**: `PcapReader` raises `ValueError` on a corrupt or truncated PCAP; does not hang. (FILE-015)
- **TEST-003**: `RtpSender` delivers packets in the correct order and respects inter-packet gap to within 2 ms for a 30-packet synthetic sequence (mocked `time.monotonic`). (FILE-016)
- **TEST-004**: `RtspProxy.relay()` calls `PcapWriter.write_packet()` for every byte chunk received from the upstream socket (verified with mock socket). (FILE-017)
- **TEST-005**: `RtspProxy` reconnects when the upstream socket raises `ConnectionResetError`; PCAP file is not corrupted. (FILE-017)
- **TEST-006**: `sdp_from_pcap` correctly extracts SDP from a synthetic PCAP containing a recorded RTSP `DESCRIBE` response. (FILE-018)
- **TEST-007**: `sdp_from_pcap` returns a heuristic H.264 SDP when no RTSP handshake is found in the PCAP. (FILE-018)
- **TEST-008**: Config loader parses a `mode: proxy` camera entry and produces a `ProxyCamera` with correct `upstream_url` and `capture_path`. (FILE-019)
- **TEST-009**: Config loader parses a `mode: replay` camera entry and produces a `ReplayCamera` with correct `source` path and `loop` flag. (FILE-019)
- **TEST-010**: `vcam doctor` exits with a non-zero code and prints an actionable message when `libpcap` is not installed (mock `ctypes.find_library`). (existing `tests/test_probe.py`)
- **TEST-011**: End-to-end integration on DEX5W000001 — proxy captures real camera for 60 s; replay delivers frames to DeepStream without pipeline errors for 5 minutes. (TASK-021, manual)

---

## 7. Risks & Assumptions

- **RISK-001**: **libpcap availability on arm64 Docker base image** — `python:3.12-slim` is Debian-based; `libpcap-dev` should be available via `apt`, but build-time package installs can break on air-gapped or restricted registries used on the EdgeAI Station. Mitigation: add a fallback that uses `scapy`'s pure-Python PCAP writer if the C library is unavailable.
- **RISK-002**: **RTSP interleaving complexity** — real cameras commonly use TCP-interleaved RTSP (RTP over TCP with `$` framing). The proxy must correctly re-frame interleaved packets; bugs here cause silent data corruption in the PCAP. Mitigation: add a framing unit test with a known-good interleaved capture.
- **RISK-003**: **Replay timing drift at high bitrates** — for cameras streaming at ≥ 10 Mbps, Python's `time.sleep` jitter (~1–5 ms) may introduce noticeable timing drift over long replays. Mitigation: use `time.perf_counter` busy-wait for the final 2 ms of each inter-packet gap.
- **RISK-004**: **SDP mismatch on replay** — if the PCAP does not contain the RTSP handshake (capture started after `SETUP`), SDP reconstruction may guess the wrong payload type, causing DeepStream to reject the stream. Mitigation: document the requirement to start capture before the RTSP session and provide a `--sdp-override FILE` option as an escape hatch.
- **RISK-005**: **Credential leakage in PCAP files** — RTSP `DESCRIBE` responses with `WWW-Authenticate` and client `Authorization` headers will be recorded verbatim. Mitigation: SEC-001 warning + README redaction guide using `editcap`.
- **ASSUMPTION-001**: MediaMTX supports RTSP ANNOUNCE (external publish) in the version auto-downloaded by the existing `binaries.py` logic. Must be verified in TASK-003.
- **ASSUMPTION-002**: The target real IP cameras use standard RTP over TCP or UDP; proprietary RTSP extensions (Hikvision-specific commands, etc.) may not be transparently relayed without additional parsing.
- **ASSUMPTION-003**: A single PCAP capture contains one RTSP session (one audio/video pair). Multi-session PCAPs are out of scope for v1.

---

## 8. Related Specifications / Further Reading

- [MediaMTX RTSP publish documentation](https://github.com/bluenviron/mediamtx#rtsp)
- [RFC 2326 — Real Time Streaming Protocol (RTSP)](https://www.rfc-editor.org/rfc/rfc2326)
- [RFC 3550 — RTP: A Transport Protocol for Real-Time Applications](https://www.rfc-editor.org/rfc/rfc3550)
- [pcapng file format specification](https://pcapng.com/)
- [Wireshark / editcap — credential redaction](https://www.wireshark.org/docs/man-pages/editcap.html)
- [scapy PCAP read/write documentation](https://scapy.readthedocs.io/en/latest/usage.html#reading-and-writing-pcap-files)
- [python-libpcap on PyPI](https://pypi.org/project/python-libpcap/)
- Existing vcam modules: `src/vcam/service.py`, `src/vcam/supervisor.py`, `src/vcam/models.py`
