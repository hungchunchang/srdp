# Changelog

All notable changes to the `socialrobot` SDK. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions are [SemVer](https://semver.org/).

## [Unreleased]

## [0.6.0] — 2026-10-10

### Added
- **The `socialrobot` command** — `characters`, `whoami`, `export <character> [-o file]`, `import <file> [--into <character>] [--force]`; the API URL and key come from `SR_BASE_URL` / `SR_API_KEY` (or `--base-url` / `--api-key`). The file is the platform's own lossless export written as stable text (sorted keys, indented, real characters): an unchanged character re-exports identically except for `exported_at`.
- **`client.replace_character(id, bundle, force=False)`** (`PUT /characters/{id}/bundle`) — put a bundle back INTO an existing character, keeping its id (bound robots, collaborators, people and logs stay) and the ids of the nodes the bundle keeps; what it replaces goes to the trash. It never changes who may see the character, how long it keeps people's data, or which robot plays which actor. Refused with `SocialRobotError` 409 when someone else changed a node after the bundle's `exported_at` (`edited_elsewhere`, naming them) or the bundle has none (`no_export_time`); `force=True` overwrites.
- **`client.export_character(id)`** / **`client.import_character(bundle)`** — the export (now carrying `exported_at`) and the import as a new private character.

## [0.5.0] — 2026-10-10

### Added
- **`Robot` + `RobotBody`** — run your own hardware (a Pi, a laptop, a home-made robot) as a platform robot. You write the body (`say`, `stop`, and whatever else it has: `play_audio`, `gesture`, `play_keyframes`, `set_led`, `show`, `set_volume`, `head_held`, `start_microphone`, `camera_frame`); `Robot` does the rest: pairing by a spoken six-digit code (the token kept owner-only in `~/.config/socialrobot/<device_id>.token`; holding the head offers a new code), following the character the web binds to the device and standing by when there is none, the heartbeat/status for the web's 機器人 card, acknowledging every line, 立刻停 (a cut line is never acked), the card's volume and log commands, binary microphone PCM while the platform lets it hear and camera frames while a node needs vision, reconnecting to the same session. `robot.emit()` sends events in from any thread.
- **Device protocol version.** `DEVICE_PROTOCOL.md` is now version 1 and documents what it was missing (pairing, the binding poll and standby, reconnecting, `stop_now`, `device_command`, `device_log_request`, `reaction`, `mic_open`, `led`, actors). `Robot` sends `protocol_version` when it registers and the server answers with its own; a difference is logged.

## [0.4.0] — 2026-10-10

### Added
- **`client.transcribe(audio, language="zh", provider="platform")`** — speech → text without a session (`POST /asr/transcribe`): a path or bytes, at most 120 s; returns `{text, seconds, provider}`.
- **`client.tts(text, provider="platform", voice=None)`** — text → mp3 bytes without a session (`POST /tts/synthesize`), at most 1000 characters.
- **`client.chat(messages, model=None, temperature=None, max_tokens=None, json_mode=False)`** — an LLM without a session (`POST /llm/chat/completions`, OpenAI-compatible, so the `openai` package works too with `base_url=<platform>/api/v1/llm` and the `srk_` key). No model = the deployment's self-hosted LLM (never overflowed to a paid node); an OpenAI/Gemini model from the curated list uses the caller's own key only.
- `provider="platform"` is the deployment's self-hosted speech — transcription waits behind every live robot session and answers 503 + `Retry-After` when busy; `provider="openai"` uses the caller's own OpenAI key (never the platform's) and lets `tts` pick a `voice`. Each API key has its own per-minute limit; every call is in the account's usage.

## [0.3.0] — 2026-10-10

### Changed — breaking
- **`base_url` is required.** `SocialRobotClient()` without it, with a URL that is not `http(s)://`, or one that does not point at `/api/…`, raises `ValueError` saying what to pass. The old default, `http://localhost:6868/api/v1`, is the production site on the lab box, so a script that forgot the argument wrote into real users' data. The MCP server refuses to start without `SOCIALROBOT_BASE_URL`; the examples refuse without `SR_BASE_URL`.

### Added
- **`SocialRobotError`** — what every refused request now raises, with the server's reason in the message (`detail`, `code`) and, for the common ones, what to do: off campus with a password login (`ntu_network_only`) → use an API key; 401 with an API key → it is wrong or revoked; 429 → wait `Retry-After`; every call 404 → `base_url` is wrong. It subclasses `httpx.HTTPStatusError`, so existing `except httpx.HTTPStatusError` code keeps working.
- **`client.whoami()`** — `GET /auth/me`. A password login calls it once, so an off-campus refusal is raised by the line that built the client instead of by the first call after it.

## [0.2.1] — 2026-10-10
Documentation only; no code changes.

### Changed
- **README rewritten as the SDK guide** (it is also the PyPI project page): what the SDK does, connecting and credentials (API key vs. NTU-only password login), `say`, `inject` / `open_stream` (the `matched` field, reserved and special event names, error codes), `subscribe`, `RobotSession` (ack every line with `tts_finished`; name `robot_type` when driving hardware), uploads, the low-level character/workflow calls, the MCP server, error handling. Every example was run against a dev stack. Builder examples are gone — `socialrobot.builder` is not public API.
- **`DEVICE_PROTOCOL.md`**: a missing `tts_finished` does not block forever; the server moves on after a liveness net of 12 s + 0.45 s per character (at most 180 s), deferring touch / face / speech meanwhile.

## [0.2.0] — 2026-10-10
The first release that reaches PyPI: `0.1.0` was tagged (`sdk-v0.1.0`) but its publish run failed before uploading, so nothing was ever on PyPI under any name.

### Added
- **`Screen(input=…, layout=…, line_code=…)`** — the answer slot's widget
  (`time` wheel, `select` list, `number` wheel, or the classic `text` box),
  three layout presets, and a robot-issued LINE binding code exposed as
  `{{line_code}}` (`line_linked` fires on the session when someone types it).
  `OnUtterance("{{意圖.關鍵字}}")` matches any cell of a data-structure column
  and seeds the matched row's columns; `{{改時間詞}}` matches a list.
- **`Screen(image_url=…)`** — a picture above a screen node's prompt: a
  `/uploads/…` path from `POST /uploads/image`, an absolute https URL, or a
  `{{column}}` template resolved per row. The device receives it on
  `ui_config.image_url`, already signed; omitted = the text-only screen.
- **`OnPlaybackDone(goto=…, write_to=…)`** — opt-in loop-exit hook. Put it on a
  `Woz(loop=True, …)` node so the loop advances when it has played all its lines
  to completion (otherwise a loop cycles back to line 0 forever and can leave
  only via a peripheral hook). For the "wait N seconds after playback, then
  switch" variant, also set that node's `loop_min_seconds=N`. A loop with no
  `OnPlaybackDone` hook keeps cycling exactly as before.
- **Silent token refresh** — an `email` + `password` session now gets a 60-minute
  access token plus an httpOnly refresh cookie (backend F6, issue #117).
  `SocialRobotClient` transparently calls `POST /auth/refresh` once on a 401 and
  retries the request, so a long-running `RobotSession` (`api_poll` /
  `ws_subscribe` loops) keeps working past the access token's expiry with no
  caller change. An `api_key` session never expires, has no refresh cookie, and
  skips this entirely.

### Changed
- The `mcp` extra is pinned to `mcp>=1.0,<2`: mcp 2.x renamed `FastMCP` to `MCPServer`, so `socialrobot-mcp` failed to import on a fresh install.
- License metadata is the SPDX expression `MIT` (`license-files = ["LICENSE"]`); the license classifier and the deprecated TOML-table form are gone (builds need setuptools ≥77).
- PyPI distribution name is `socialrobot`, the same as the import package: `pip install socialrobot`, then `import socialrobot`. (An earlier rename to `srdp` never shipped; `srdp` on PyPI is an unrelated project — do not install it.) The package metadata points `Repository`/`Issues` at the public source mirror [github.com/hungchunchang/srdp](https://github.com/hungchunchang/srdp) (one-way mirror of `sdk/`; development stays in the monorepo, no direct PRs).
- `upload_expression` / `upload_media` / `upload_vision_model` now read the file
  into memory before posting, so the refresh-and-retry above can re-send the same
  body (a consumed file handle could not be replayed).

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
