# Changelog

All notable changes to the `socialrobot` SDK. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions are [SemVer](https://semver.org/).

## [Unreleased]

### Changed
- PyPI distribution renamed `socialrobot` → `srdp` (the `socialrobot` name was
  already taken on PyPI). The import package is unchanged — `pip install srdp`,
  then `import socialrobot` as before.

## [0.1.0] — 2026-06-15
First packaged release (PyPI-ready).

### Added
- **Builder DSL** (`Character`, `Woz`, `Say`, `Agent`, `Judgment`, `UI`, `Button`,
  `Identify`, `Switch`, `Vision`, `Mirror`, hooks `OnFace`/`OnSensor`/`OnTimeout`/
  `OnApiPoll`/`OnVision`/`OnWebSocket`) — author characters in code; compiles to the
  same REST API the web canvas uses.
- **Runtime client** — `SocialRobotClient` (REST; `srk_` API key or email/password),
  `RobotSession` (drive a live robot over WS), `ExternalStream` / `SubscribeStream`
  for external-API interrupts (inbound push + outbound poll/subscribe).
- `upload_vision_model` + `Vision` switch.
- Packaging: license (MIT), `py.typed`, typed public API, metadata + classifiers.
