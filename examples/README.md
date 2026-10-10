# SDK examples

Runnable recipes built on the existing SDK — no platform changes.

The "author once" snippets below use `socialrobot.builder` to create a matching character quickly. Builder is the platform's own provisioning tool, not public API — building the same character in the web editor works just as well.

## `tailscale_camera_driver.py` — local camera → live robot session

A computer on your tailnet runs face detection locally and pushes events into a
running robot session via `client.inject(...)`. The robot reacts through its
sensor hooks / variable switches. The robot backend never receives the camera —
only your injected events. **Tailscale is just networking**: point
`SR_BASE_URL` at the backend's tailnet address.

```bash
pip install opencv-python httpx
export SR_BASE_URL="http://<tailnet-ip-or-host>:<backend port>/api/v1"
export SR_API_KEY="srk_..."          # web: 個人設定 → API Key (same key the app's API-Key login uses)
# export SR_SESSION_ID="..."         # optional; else the first live session is used
python tailscale_camera_driver.py
```

It injects `person_arrived` / `person_left` sensor events (debounced) and a
`people_count` variable.

### Robot side (author once, via the Builder DSL)

Give the character a stage whose hook reacts to those injected events:

```python
from socialrobot import Character, Script, Say, OnSensor, Mirror, SocialRobotClient

client = SocialRobotClient(base_url="https://<host>/ntu_social_robot/api/v1", api_key="srk_...")

idle = Script("待命", loop=True, idle_expression=Mirror,
           hooks=[OnSensor("person_arrived", goto="打招呼")])
greet = Script("打招呼", lines=[Say("你好！歡迎光臨。")],
            hooks=[OnSensor("person_left", goto="待命")])

Character(name="迎賓員", nodes=[idle, greet]).deploy(client)
```

`OnSensor(name)` fires on the exact injected event name (or any event when
`name` is omitted). To branch on the headcount instead, read the `people_count`
variable from a `Switch` (VARIABLE mode) or a `{{people_count}}` template.

> The same pattern works for **any** local sensor source, not just a camera —
> swap the detection loop for whatever the tailnet machine can measure.

## `nao_meme_bridge.py` — meme as the brain for an ecrobot NAO robot

Make meme's web platform drive the NAO robot (ecrobot project): author a NAO
character in meme's editor, and this bridge registers a meme `RobotSession` and
renders each `state_update` on the NAO via the qi SDK. Full-duplex — NAO speech
comes from meme; visitor speech goes back into meme's AI node.

- **down**: SPEAKING `text` → `ALTextToSpeech.say`; `motion` → a NAO gesture.
  After each line the bridge sends `tts_finished` so meme's WoZ/AI advances
  (same ack the app/simulator do).
- **up**: console input (default, testable) or mic→ASR (`SR_INPUT=mic`) →
  `session.send_event("asr_input", text)` → meme AI replies → NAO speaks it.
  Only an AI node (or a Compose node in LLM voice mode) replies — while the
  flow sits on a scripted node the transcript is still recorded and still
  drives variables / `asr_input` hooks, but by design produces no reply.

The **ecrobot server is bypassed** — meme is the sole brain; the NAO is a pure
qi body. (ecrobot's handwriting OCR / its own ASR can later feed meme as inputs
via `inject`, if ecrobot exposes them.)

```bash
# On the NAO machine (the `nao` env has qi):
pip install socialrobot pyaudio      # or from a checkout: pip install -e sdk/
export SR_BASE_URL="https://<host>/ntu_social_robot/api/v1"
export SR_API_KEY="srk_..."
export NAO_CHARACTER="NAO 教學助理"     # id or name authored in meme
export NAO_IP="<nao-ip>"
# export SR_INPUT=mic SR_ASR_URL=<your ASR endpoint>   # else console input
python nao_meme_bridge.py
```

Off the NAO (no qi) it runs a **dry-run body** that prints "NAO would say: …",
so you can test the meme side anywhere.

### Robot side — author a NAO character (no screen → text + motion only)

```python
from socialrobot import Character, Script, Say, Agent, OnSensor, SocialRobotClient

client = SocialRobotClient(base_url="https://<host>/ntu_social_robot/api/v1", api_key="srk_...")

welcome = Script("歡迎", lines=[Say("你好，我是 NAO，今天我們一起練習。")])
chat = Agent("對話", system_prompt="你是親切的 NAO 教學助理，用簡短中文回答。")
Character(name="NAO 教學助理", nodes=[welcome, chat]).deploy(client)
```

NAO has no screen, so use **WoZ (text) / AI (text) / motion** nodes — not
expression / UI / mirror. `OnSensor(...)` hooks still fire from anything the
bridge injects (ecrobot handwriting, external sensors, …).

> Part C's `client.subscribe(session_id)` isn't needed here — the bridge **is**
> the robot client, so it already receives full `state_update`s. Use `subscribe`
> only if a separate dashboard wants to observe this NAO session.

## `ecrobot_puzzle_bridge.py` — a handwritten grid puzzle as a sensor

The other half of the ecrobot project: the iPad worksheet. The bridge takes the
tablet's handwriting, decides what each answer is (right house, wrong house, a
name copied in its clue form, a resident who was already on the map) and pushes
ONE event per answer into the live session — `answer_wrong`, `answer_correct`,
… — with the variables the flow speaks from. A wrong answer carries its
diagnosis: which misreading of the clue puts that name in that house, looked up
in a table fitted to 46 real participants rather than guessed by a model.

- **The puzzle lives in the character, not in this file.** Residents, the
  answer → misconception table, the affect lines and the teaching stages are
  the character's 資料結構; the bridge reads them back at start. Edit a hint
  in the web editor and the robot says the new one.
- **The iPad app needs no change**: `/register` and `/handwriting` take the
  payloads the original ecrobot server took, on the same port (1468). A request
  may carry `text` instead of strokes for a client that recognises on its side.
- **It can be the robot too.** `EC_ROBOT=nao` (or `dry`) runs
  `nao_meme_bridge`'s body and ears in the same process; `EC_ROBOT=attach`
  follows the live session of whatever robot is already on the character (the
  full-duplex NAO client below, a Kebbi, the web simulator) and only pushes
  into it — when that robot restarts, the bridge moves to its new session.
- **Any tablet.** The picture handed to the recognizer is framed on the ink's
  own bounding box, so the iPad's points and the Android app's 480-unit grid
  produce the same image; a cell reported empty when it already was is dropped
  (the 2025-12 iPad build re-sent those on every stroke).

```bash
export SR_BASE_URL="https://<host>/ntu_social_robot/api/v1"
export SR_API_KEY="srk_..."
export EC_CHARACTER="立陶宛語地圖解謎"          # authored by server/scripts/seed_ecrobot_puzzle.py
export EC_ROBOT=nao NAO_IP="<nao-ip>"          # or EC_ROBOT=dry / attach
export GOOGLE_APPLICATION_CREDENTIALS=key.json # OCR; EC_OCR=none to require "text"
pip install socialrobot fastapi uvicorn pillow google-cloud-vision   # or pip install -e sdk/ from a checkout
python ecrobot_puzzle_bridge.py                # serves the tablet on :1468
```

Every snapshot and every verdict is appended to `EC_LOG` (JSONL) — the record
of what the participant wrote, which the platform itself does not keep.

## `nao_duplex_client.py` — NAO as a full-duplex robot (v3 gateway voice)

`nao_meme_bridge.py` is half-duplex: record a window, send the text, say the reply. This client speaks the platform's v3 voice protocol instead — the one the Android app and the web simulator use — so the person can talk over the robot and be heard:

```
mic ──WebRTC──▶ api-gateway (VAD → ASR → LLM → TTS) ──WebRTC──▶ speaker
                     └─ DataChannel: text / judgments / speech_done / interrupt_ack
this client ──voice_phase / agent_judgments / tts_finished──▶ backend (the flow)
```

- **The active node decides whether the robot may hear.** The mic is open only on an AI node whose voice mode is a gateway one; on every other node the track carries silence (muted, never disconnected). The gateway is handed persona + the node prompt as the backend rendered it (`state_update.data.agent_prompt`), again whenever it changes.
- **Full duplex needs the robot not to hear itself.** `NAO_ECHO=aec` (WebRTC's echo canceller via `pip install livekit`; the default when it is installed), `none` (the audio device cancels echo), or `duck` (mic shut while the robot is audible — not full duplex, and the default only when livekit is missing, said at startup).
- **Two voices.** An AI reply is the platform's voice, which is Mandarin-only: a Latin-script word is spoken only if the gateway has a recording of it (api-gateway `server/tts_clips_data/` — the Lithuanian puzzle's names and clue words are there) and is dropped otherwise. Scripted lines are said by `NAO_SCRIPT_VOICE` — `nao` (the robot's own synthesizer, which reads any spelling; the default on a real NAO) or `platform`.
- **The voice channel is kept up, beside the flow.** It connects while the robot is already saying its scripted lines, retries a gateway that is not there yet (one that has just started fetches its VAD model on the first connect — 35 s measured), and reconnects when the connection is lost. Until it is up the AI node is deaf, and the client says so each time.
- **The gateway's voice belongs to the node that asked for it.** A reply still draining when the flow moves to a scripted node is dropped, not played over that node's line.

```bash
pip install socialrobot aiortc av numpy pyaudio livekit   # + qi on the NAO machine; or pip install -e sdk/
export SR_BASE_URL="https://<host>/ntu_social_robot/api/v1"
export SR_GATEWAY_URL="https://<host>"             # where /v3/webrtc/connect lives
export SR_API_KEY="srk_..." NAO_CHARACTER="立陶宛語地圖解謎" NAO_IP="<nao-ip>"
python nao_duplex_client.py                        # --list-devices to pick NAO_MIC_INDEX
```

`make e2e-nao-duplex` drives it against a live backend and a live gateway (mock speech workers) with a recorded utterance for a microphone: deaf on a script node, a spoken turn, a tablet verdict re-prompting the gateway, a barge-in. **Not verified on hardware**: output through NAO's own loudspeaker (`ALAudioDevice.sendRemoteBufferToOutput`; falls back to the local speaker if it fails), and how much of the robot's voice the echo canceller removes in a real room.
