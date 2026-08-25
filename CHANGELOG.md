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

### Fixed

- CI release pipeline: fixed duplicate workflow content causing YAML parse error.
- CI release pipeline: release body now written via Python to avoid shell
  interpolation of backtick-quoted text in CHANGELOG entries.
- CI: Docker Hub push jobs now reference the `dockerhub` environment so
  `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN` secrets are correctly injected.

### Added

- Publish wheel to GitHub Packages (PyPI registry) on every release.
- Push `konekuto/vcam:main` to Docker Hub on every merge to `main`.

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
