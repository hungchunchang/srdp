# SDK examples

Runnable recipes built on the existing SDK — no platform changes.

## `tailscale_camera_driver.py` — local camera → live robot session

A computer on your tailnet runs face detection locally and pushes events into a
running robot session via `client.inject(...)`. The robot reacts through its
sensor hooks / variable switches. The robot backend never receives the camera —
only your injected events. **Tailscale is just networking**: point
`SR_BASE_URL` at the backend's tailnet address.

```bash
pip install opencv-python httpx
export SR_BASE_URL="http://<tailnet-ip-or-host>:6868/api/v1"
export SR_API_KEY="srk_..."          # web: 個人設定 → API Key (same key the app's API-Key login uses)
# export SR_SESSION_ID="..."         # optional; else the first live session is used
python tailscale_camera_driver.py
```

It injects `person_arrived` / `person_left` sensor events (debounced) and a
`people_count` variable.

### Robot side (author once, via the Builder DSL)

Give the character a stage whose hook reacts to those injected events:

```python
from socialrobot import Character, Woz, Say, OnSensor, Mirror, SocialRobotClient

client = SocialRobotClient(base_url="http://<host>:6868/api/v1", api_key="srk_...")

idle = Woz("待命", loop=True, idle_expression=Mirror,
           hooks=[OnSensor("person_arrived", goto="打招呼")])
greet = Woz("打招呼", lines=[Say("你好！歡迎光臨。")],
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

The **ecrobot server is bypassed** — meme is the sole brain; the NAO is a pure
qi body. (ecrobot's handwriting OCR / its own ASR can later feed meme as inputs
via `inject`, if ecrobot exposes them.)

```bash
# On the NAO machine (the `nao` env has qi):
pip install socialrobot pyaudio
export SR_BASE_URL="http://<host>:6868/api/v1"
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
from socialrobot import Character, Woz, Say, Agent, OnSensor, SocialRobotClient

client = SocialRobotClient(base_url="http://<host>:6868/api/v1", api_key="srk_...")

welcome = Woz("歡迎", lines=[Say("你好，我是 NAO，今天我們一起練習。")])
chat = Agent("對話", system_prompt="你是親切的 NAO 教學助理，用簡短中文回答。")
Character(name="NAO 教學助理", nodes=[welcome, chat]).deploy(client)
```

NAO has no screen, so use **WoZ (text) / AI (text) / motion** nodes — not
expression / UI / mirror. `OnSensor(...)` hooks still fire from anything the
bridge injects (ecrobot handwriting, external sensors, …).

> Part C's `client.subscribe(session_id)` isn't needed here — the bridge **is**
> the robot client, so it already receives full `state_update`s. Use `subscribe`
> only if a separate dashboard wants to observe this NAO session.
