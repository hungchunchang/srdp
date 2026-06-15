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
