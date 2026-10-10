# Device protocol — build a client for any robot

This is the contract between a robot (or any client) and the platform backend.
The browser **simulator** and the Python **SDK** (`socialrobot`, this package) are
the two reference implementations; the Android app is a third. Implement this and
your hardware runs any flow authored on the platform.

**In Python you do not have to implement it**: `socialrobot.Robot` is this whole
document as a library — write a `RobotBody` (how your machine speaks, stops,
moves, lights up, listens, sees) and `Robot(body, base_url, device_id).run()`.

Three steps: get **credentials** (§0), **register** over HTTP (§1), then open one
**WebSocket** and exchange events (§2–3).

## Version

| version | date | what changed |
|---|---|---|
| 1 | 2026-10-10 | the first numbered version — everything in this file |

Send the version your client was written against as `protocol_version` when you
register (§1); the server answers with its own. A different number means one of
you is older than the other: the server logs it, and a client should say so.

---

## 0. Credentials and which character

**Credentials.** Either a developer API key (`srk_…`, made on the web: 個人設定 ›
機器人與金鑰 › 開發者 API 金鑰), or a **device token** the robot earns by pairing —
no account, no typing on the robot:

```
POST /api/v1/auth/pair/request            (no Authorization)
{"device_label": "pi-kitchen-01", "device_id": "pi-kitchen-01"}
→ {"code": "482915", "secret": "…", "expires_in": 300}
```

The robot **says the code**; a person types it on the web (個人資源 › 機器人 ›
連上機器人, which is `POST /auth/pair/claim {"code"}` with their login). The robot
polls every couple of seconds:

```
POST /api/v1/auth/pair/poll   {"secret": "…"}
→ {"status": "pending" | "approved" | "claimed" | "expired", …}
```

`approved` carries `token` and `user_name`, exactly once. Keep the token (it is
long-lived and, unlike a web login, works off the NTU network); from then on the
robot acts for that person. A `403` on `request` means the robot is **locked** to
an account on the web (個人資源 › 機器人 › 鎖定) — there is no code to give. A
robot that already has a token offers a new code when someone holds its head for
three seconds: that is how another person takes it over. Change accounts only
when the robot is not in the middle of talking to someone.

**Which character.** Ask the web, every ~10 s:

```
GET /api/v1/robot-devices/{device_id}?status={"version": "…", "kind": "…", "volume": 30}
→ {"bound": true, "character_id": "…", "character_name": "…", "actor": "B" | null, …}
```

This poll is also the robot's **heartbeat** — the web lists it as online — and
`status` is what the web's 機器人 card shows. Run `character_id`; when it is not
bound (or `character_name` is empty: the character is in the trash) register the
role **`__standby__`**, a session the live console can drive. When the answer's
`character_id` or `actor` changes, end the session and register again — that is
how changing a robot's character on the web reaches it without anyone touching
it. A robot pinned to one character still polls, for the heartbeat.

## 1. Register (HTTP)

```
POST /api/v1/robot/register
Authorization: Bearer <access-token-or-device-token>
Content-Type: application/json

{
  "device_id":  "kitchen-nao-01",     // stable per physical device
  "role":       "<character-id>",     // the character to run
  "app_version": "my-adapter/1.0",
  "protocol_version": 1,              // the version above
  "device_hardware": {                // OPTIONAL but important — see §4
    "robot_type": "nao",
    "motors":  true,
    "camera":  true,
    "mic_array": false,
    "chassis": false
  }
}
```

Response (`RegistrationResponse`):

| field | use |
|-------|-----|
| `session_id` | addresses this connection; goes in the WS URL and every event |
| `ws_ticket` | **pass as `?ticket=` on the WS** — proves you registered (see §2) |
| `websocket_url` | the robot WS to open |
| `role_config` | the compiled character: `assets.visuals` (name→URL), `assets.motor_keyframes`, prompts, stages… |
| `webrtc_config` | ICE servers, if you do WebRTC voice |
| `vision_gateway_url` | a full, ticketed URL to stream camera frames for face recognition (open verbatim; optional) |
| `download_assets` | media to pre-fetch (signed URLs) |
| `robot_id`, `jwt_token` | device identity + a token for authed calls |
| `protocol_version` | the device protocol version this server speaks (see Version) |

A `403` means the robot is locked to another account on the web.

## 2. Open the WebSocket

```
ws(s)://<host>[/prefix]/ws/<session_id>?ticket=<ws_ticket>
```

Build it from the base URL you already use (`https://host/prefix/api/v1` →
`wss://host/prefix/ws`), not from `websocket_url`, which is assembled from the
server's own setting and can name a host the robot cannot reach. The `ws_ticket`
authenticates the socket. (A backend with `ROBOT_WS_REQUIRE_TICKET` off still
accepts a ticketless connection with a warning, for older clients — but always
send it.)

Send **`client_ready`** on every connect, or the flow never starts — on a
reconnect it is what resumes the session.

**Reconnecting.** The ticket lasts 24 hours and a session survives a backend
restart, so after a dropped socket reconnect to the SAME session with the same
ticket; after three failures in a row, register again. A close code `4403` means
a web login was used off the NTU network — a device token or API key never gets it.

## 3. Events

Every message is JSON. **Client → server** (`{"type":"event","event":<name>,"session_id":…,"timestamp":…,"data":{…}}`):

| event | when |
|-------|------|
| `client_ready` | once, right after connect |
| `tts_finished` | **after every spoken chunk.** Without it the server waits out a liveness net (12 s + 0.45 s per character, at most 180 s — `robot_manager.speech_ack_deadline`) before moving on, and touch / face / speech events are deferred the whole time, so the robot looks slow and deaf. The #1 thing naive clients get wrong. Never for a line cut by `stop_now`. `data: "gateway_voice"` marks the ack of a v3 gateway-voice reply. |
| `asr_input` | recognized user speech (`data.text`) — drives AI nodes |
| `face_detected`, `UI_CLICK`, `UI_INPUT` | sensors / on-face UI |
| `voice_phase`, `agent_judgments` | v3 gateway-voice slices (optional) |
| any sensor name | e.g. a button/PIR — matched by the flow's hooks |

**Server → client** (`{"type":"state_update","state":<STATE>,"data":<StateData>}`), `state` ∈ `IDLE | SPEAKING | LISTENING | ERROR`:

| `data` field | meaning |
|--------------|---------|
| `text` | line to speak (TTS) — ack with `tts_finished` when done |
| `audio` / `audio_mode` / `utterance_id` / `audio_chunk_index`,`_total` | pre-rendered TTS chunks; `audio_mode` = `interrupt` \| `append` |
| `audio_clip_url` | a **non-TTS** clip; advance on its `ended` only when there's no `text` |
| `motion` | a built-in gesture **name** (the fallback) |
| `motor_keyframes` | `[{time, motors:{joint: angle}}]`, already retargeted server-side. **Prefer these; never apply both.** A client that declared `motors:false` receives only `motion`. |
| `visual` | an expression **name** resolved against `role_config.assets.visuals` (→ a video/image URL). Sentinel `__mirror__` = show the front camera. |
| `stage_name`,`stage_mode`,`stage_id`,`reminder_count`,`dataset_cursor`,`dataset_progress`,`citations`,`ui_config` | HUD / stage state (`dataset_progress` = `{structure: [n, total]}`, counted from 1) |
| `ui_config.image_url` | a picture to show above the prompt (QR code, menu, photo). Already rendered and signed server-side — fetch it as-is, prefixing only a bare `/uploads/…` path with your media base. Absent = the text-only screen. |
| `ui_config.input` | the answer slot's widget: `{kind: "time", start, end, step, default}` (hour+minute wheel, `HH:MM`), `{kind: "select", options: [{label, value}]}` (already expanded), `{kind: "number", min, max, step, default}`, or `{kind: "text"}`. 確認 sends `UI_INPUT` `{variable: ui_config.input_variable, value}` exactly like the text box. Absent = `show_input` decides (legacy). |
| `ui_config.layout` | `{image: "top"\|"left", buttons: "row"\|"column"\|"grid", size: "normal"\|"large"}` presets. |
| `ui_suggest` | `{variable, value}` — a spoken value the server parsed for the active screen's widget. MOVE the widget to it, do not submit; the person confirms. Arrives with the same `ui_config` again. |
| `needs_audio`,`needs_visual`,`hook_listening`,`voice_transport` | media gates — honor them or you hold the mic open every stage |
| `mic_open` | the server's verdict on whether the robot may hear right now; send microphone audio only while it is true |
| `led` | the node's lights: `{mode: "color"\|"off"\|"breath"\|"cycle", color, colors, period_ms, parts: {<part>: {same shape}}}`. The top level is the whole robot; `parts` override single lights. `breath` = brightness 0.15 + 0.85·(½ − ½·cos 2πt/T); `cycle` steps one T/n per color. `null` = the robot's own default |
| `location` | navigation target (chassis robots) |

**Other server → client messages:**

| `type` | meaning |
|---|---|
| `stop_now` | the editor's ■ / the console's 立刻停. Drop the SPEAKING lines still queued, cut the one being said, and **never ack it** — the server says it again on 繼續, or starts over |
| `device_command` | from the web's 機器人 card: `{"command": "volume", "value": 0–100}`, `"autonomy"` (bool), `"restart"`, `"update"`. Do what your machine can; ignore the rest |
| `device_log_request` | upload a log: `POST /api/v1/device-logs` multipart `file` (gzip) + `device_id`, `reason`, `app_build`, `device_model`, `os_release` |
| `reaction` | `{"kind": "hearing" \| "hearing_end"}` — the platform's VAD believes someone started / stopped speaking (a listening glow); `{"kind": "wait" \| "listen" \| "ack" \| "unclear" \| "far", "text", "audio"?}` — a short spoken back-channel |

A robot in a **多演員專案** (cast) is told which actor it plays by the binding
(`actor` in §0); the server routes each node to the robot that plays it.

### Binary messages — microphone and camera without WebRTC

A client that cannot run WebRTC (the on-robot NAO client, `sdk/nao27`, on NAOqi's Python 2.7) may send its microphone and camera as **binary** messages on this same WebSocket. The first byte says what follows:

| first byte | payload |
|---|---|
| `0x01` | little-endian signed 16-bit **mono PCM at 16 000 Hz**, any length (NAOqi's `processRemote` buffers are ~170 ms) |
| `0x02` | **one image** the server can decode: JPEG, PNG or binary PPM (`P6`) |

They join exactly the pipelines a WebRTC track does (server: `app/modules/session_media.py`): audio goes through the same frame gate, VAD and speech-to-text — so send it only while `mic_open` is true, though the server drops anything that arrives outside it — and images go through the same throttled relay to the vision-gateway. Send images only while `needs_visual` is true. Never send both WebRTC and binary audio for one session.

## 4. Capabilities — declare honestly

`device_hardware` says what **this machine** physically has. The server intersects
it with the character's design-time `robot_type` (default `kebbi`): the designer's
intent stands, but the device wins for anything producing physical commands, and
disagreement surfaces as a `machine.mismatch` rather than being silently resolved.

**`None` ≠ `False`.** A field you omit means "didn't say", and the platform keeps
the capability (so older clients don't silently lose features). Only an explicit
`false` denies it. So: declare `motors:true`/`robot_type` if you have a body, and
you'll get retargeted `motor_keyframes`; omit or `false` and you get gesture
`motion` names or nothing.

## Tiers — what a minimal client must handle

- **Screen-only** (no body): handle `text`/`audio`, `visual`, and send
  `client_ready` + `tts_finished`. Legal and common — the simulator is exactly this.
- **+ Body**: also handle `motor_keyframes` (or `motion`), declare `motors:true`.
- **+ Voice**: WebRTC via `webrtc_config`, or the WS `audio` chunks.
- **+ Vision**: stream frames to `vision_gateway_url`.

## Reference code

`socialrobot.Robot` (this SDK, `robot.py`) is all of the above — pairing, the
binding poll, registration, the socket and its reconnects, acks, `stop_now`,
device commands, binary mic and camera — around a `RobotBody` you write. The
on-robot NAO client (`nao27/meme_nao27.py`, Python 2.7) is the same logic for
NAOqi. `socialrobot.RobotSession` (`runtime.py`) is the lower-level piece: one
registration and one socket, for test drivers.
