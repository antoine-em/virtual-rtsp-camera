# Changelog

All notable changes to **vcam** are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

<!-- ─────────────────────────────────────────────────────────────────────────
  HOW TO ADD A RELEASE ENTRY
  ──────────────────────────
  1. Copy the [Unreleased] block, replace "Unreleased" with the new version and
     today's date: ## [1.2.3] - 2025-06-15
  2. Clear the copied [Unreleased] block, ready for the next cycle.
  3. Add a link at the bottom of the file.
  4. Tag the commit: git tag v1.2.3 && git push --tags
     → the Release workflow fires, extracts this block, and creates the GitHub
       Release with the wheel attached.

  Only include headings that have entries.  Standard headings:
    ### Added        — new features visible to users
    ### Changed      — changes to existing behaviour
    ### Deprecated   — features to be removed in a future version
    ### Removed      — features removed in this version
    ### Fixed        — bug fixes
    ### Security     — vulnerability fixes
──────────────────────────────────────────────────────────────────────────── -->

## [Unreleased]

### Added

-

---

## [0.1.1] - 2026-08-25

### Security

- Bump the bundled MediaMTX from `v1.19.3` to `v1.20.1`. The v1.19.3 binary was
  built with Go 1.26.5 and carried 9 fixable HIGH advisories in the Go standard
  library (CVE-2026-39821, -33818, -46600, -56853, -56858, -56859, -56860,
  -56862) plus CVE-2026-71556 in `go-git`; v1.20.1 is built with Go 1.26.6.
- Runtime image: `apt-get upgrade` during build so `trixie-security` updates
  published after the base image was cut are picked up.
- Runtime image: remove `perl-base`. Nothing in the image needs it (ffmpeg and
  CPython both verified working without it) and it shipped CVE-2026-12087,
  -13221 and -48959, none of which have a fixed package in Debian trixie.
- Runtime image: uninstall `pip` after the wheel is installed. It is unused at
  runtime and carried 7 known advisories.
- CI: Docker builds now run with `pull: true` so a cached stale base image
  cannot silently reintroduce patched vulnerabilities.

  Net effect: fixable CRITICAL/HIGH findings in the published image drop from
  9 to 0.

  Known accepted risk: `libcjson1` CVE-2026-67215 / -29036 / -67216 remain.
  The package is a hard dependency of `librist4`, which Debian's (and Alpine's)
  `ffmpeg` is linked against, so it cannot be removed without a custom ffmpeg
  build. No fixed version is available upstream.

### Added

- `.github/dependabot.yml` — weekly update checks for uv, Docker and GitHub
  Actions dependencies.
- CI now fails on *fixable* CRITICAL/HIGH image vulnerabilities via Trivy.
  Unfixable findings are ignored so the pipeline is not permanently blocked.
- Publish wheel to GitHub Packages (PyPI registry) on every release.
- Push `konekuto/vcam:main` to Docker Hub on every merge to `main`.

### Fixed

- CI release pipeline: fixed duplicate workflow content causing YAML parse error.
- CI release pipeline: release body now written via Python to avoid shell
  interpolation of backtick-quoted text in CHANGELOG entries.
- CI: Docker Hub push jobs now reference the `dockerhub` environment so
  `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN` secrets are correctly injected.


---

## [0.1.0] - 2025-08-25

### Added

- `vcam run` — start one or more looping RTSP cameras from local video files,
  multiplexed over a single port via MediaMTX (auto-downloaded, SHA-256 verified).
- `vcam init / add / list / urls / show` — configuration file management
  (`cameras.yaml`).
- `vcam probe` — inspect a video file and show which stream mode `auto` would pick.
- `vcam doctor` — check that ffmpeg, ffprobe and MediaMTX are present.
- `vcam install-server` — pre-fetch the MediaMTX binary into the local cache.
- `vcam service install / start / stop / status / uninstall / logs` — register
  the stack as a **systemd user unit** (Linux) or **launchd LaunchAgent** (macOS),
  no root required.
- `python -m vcam` entry-point so service units work even when `vcam` is not on
  the system `PATH`.
- Fault-simulation modes: `noise`, `degraded`, `frozen`, `blackout`, `flaky`,
  `stutter` (useful for testing downstream analytics pipelines).
- Linux `aarch64` support (downloads the `linux_arm64` MediaMTX build).
- macOS Apple Silicon support with Rosetta detection.

---

<!-- version diff links ────────────────────────────────────────────────────── -->
[Unreleased]: https://github.com/antoine-em/virtual-rtsp-camera/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/antoine-em/virtual-rtsp-camera/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/antoine-em/virtual-rtsp-camera/releases/tag/v0.1.0
