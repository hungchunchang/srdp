# srdp — Social Robot Platform Python SDK

用 Python 程式碼設計角色（取代/搭配視覺化 canvas），並驅動、監看正在執行的機器人。

## 在哪裡用？怎麼用？

**在你自己的電腦（或任何伺服器/CI）跑，不是在網頁裡跑。** 流程：

1. **安裝**（任何 Python ≥3.10 環境）：

   ```bash
   pip install srdp           # 發佈後（PyPI）— `socialrobot` 這個名稱已被佔用，改用 srdp
   pip install -e sdk/        # 或從 repo checkout 開發
   ```

   > **PyPI 套件名稱是 `srdp`，但 import 名稱維持 `socialrobot` 不變**：
   > `pip install srdp` 之後，程式裡還是 `import socialrobot`（下面所有範例都是）。
   > 只有發佈到 PyPI 用的名字換了，API 完全沒變。

2. **取得憑證**——擇一：
   - **API key（建議）**：登入網頁 → 右上角使用者設定 → API Key → 產生。
     `srk_` 開頭、只顯示一次，只能存取**你自己**帳號的資源。
   - 或直接用 email + password（SDK 會替你登入換 JWT）。

3. **指向平台**——`SocialRobotClient(base_url=…)` **必填指向你自己的部署**：
   - 部署環境：`https://sociallab.duckdns.org/ntu_social_robot/api/v1`
   - 本機開發：`http://localhost:6868/api/v1`
   - ⚠️ 不傳 `base_url` 時預設是 `http://localhost:6868/api/v1`（只適合本機）；
     用 `pip install` 在別台機器上跑時，務必傳入你部署的 URL。

4. **寫程式 → deploy → 到網頁看**：deploy 出來的角色就出現在你帳號的
   Dashboard，canvas 上完整可見、可繼續用圖形介面編輯、可在模擬器播放。
   程式碼和 canvas 編輯的是同一份資料（同一套 REST API）。

## Builder DSL — 用程式碼建角色

```python
from socialrobot import (
    SocialRobotClient, Character, Woz, Say, Agent, Judgment,
    UI, Button, Identify, Switch, Mirror, OnFace, OnSensor, OnApiPoll,
)

client = SocialRobotClient(
    base_url="https://sociallab.duckdns.org/ntu_social_robot/api/v1",
    api_key="srk_…",                      # 或 email=…, password=…
)

idle    = Woz("待機", loop=True, idle_expression=Mirror)
ident   = Identify("身份辨識")
ask     = UI("詢問姓名", prompt="請問怎麼稱呼您？", input_var="name")
welcome = Woz("歡迎", lines=[Say("{{name}}您好！", pause=1.0)])
chat    = Agent("對話", system_prompt="你是親切的導覽員…",
                judgments=[Judgment("is_done", "對話是否結束",
                                    values={"yes": ("結束", idle)})])

idle.hooks = [OnFace(goto=ident)]
ident.known, ident.unknown = welcome, ask
ask.goto, welcome.goto, chat.goto = welcome, chat, idle

deployed = Character("我的導覽員", nodes=[idle, ident, ask, welcome, chat],
                     is_public=False).deploy(client)
print(deployed.id)   # 開網頁就看得到這隻角色
```

`server/scripts/seed_demo_flows.py`（Museum Guide / Origami Master 的官方
seed）就是用這套 DSL 寫的，可當完整範例。

## Runtime client — 驅動正在跑的機器人

```python
import asyncio
from socialrobot import RobotSession

async def main():
    async with await RobotSession.start(client, "我的導覽員") as s:
        await s.send_face(embedding=[0.1] * 128)          # 模擬鏡頭
        ev = await s.expect(state="UI", timeout=10)        # 等 ask_name UI
        await s.send_event("UI_INPUT", {"variable": "name", "value": "小明"})
        await s.expect(state="SPEAKING", text="小明", timeout=20)

asyncio.run(main())
```

## 外部系統整合 — 把 API 結果變成中斷條件

**Inbound**（你的程式推進來）——不需要 robot WS，純 HTTP：

```python
for sess in client.list_sessions():                # 只看得到自己的機器人
    client.inject(sess["session_id"],
                  event="order_ready",             # 觸發 OnSensor("order_ready") hook
                  variables={"yolo_result": "1"})  # 餵 Switch / {{var}} 模板
```

**Outbound — 後端替你輪詢/訂閱**（掛在節點上的 hook）：

```python
waiting = Woz("等待製作", loop=True, hooks=[
    # HTTP 輪詢
    OnApiPoll("https://your-service/jobs/123",
              path="data.status", equals="done", every=2.0,
              write_to="job_status", goto="完成播報"),
    # 或 WebSocket 串流（後端持續訂閱外部 feed）
    OnWebSocket("wss://your-service/jobs/123/stream",
                path="status", equals="done",
                subscribe={"action": "subscribe"},   # 連上後送一次（可省）
                write_to="job_status", goto="完成播報"),
])
```

**Inbound 串流**——高頻推送時用持久 WS（取代一次一個 `inject`）：

```python
async with await client.open_stream(session_id) as stream:
    await stream.send(variables={"price": 42})     # → VARIABLE switch
    await stream.send(event="order_ready")         # → OnSensor hook
```

注意：會被 hook 中斷的等待節點請用**無台詞的 loop**（有台詞的 loop 會一直
處於 SPEAKING，sensor hook 會被延後而永遠不觸發）。

## 自訂視覺模型

上傳自己的分類/偵測模型（`.onnx` / `.pt` / `.tflite` …），用 `Vision`
switch 讓機器人「看當下畫面 → 依標籤分支」：

```python
model = client.upload_vision_model("origami_step.onnx", name="摺紙判斷",
                                   model_type="onnx", labels=["step1", "step2"])
judge = Vision("判斷這步", model=model["id"], threshold=0.7,
               cases={"step2": ("做對了", correct_node)}, default=retry_node)
```

進入該節點時後端對機器人鏡頭當下畫面跑模型,取信心值最高且過 threshold
的標籤跳對應分支,全部低於 threshold 走 default。Teachable Machine 匯出的
`.tflite` 可直接上傳。

## 已驗證的環境

從一般使用者環境（容器外的 venv/conda）對 localhost 與公開 prod URL 全程
驗證過：登入、DSL deploy、WS runtime、HTTP/WS inject。`make e2e-sdk` 是
完整迴歸（HTTP poll、WS subscribe、HTTP inject、WS stream、vision 全涵蓋）。

## MCP server (drive the platform from a terminal agent)

`socialrobot.mcp_server` exposes the SDK as [MCP](https://modelcontextprotocol.io)
tools, so an agent (Claude Code, etc.) can run the platform conversationally:

| tool | does |
|---|---|
| `list_characters` | your characters (id/name/public) |
| `list_sessions` | running robot sessions to inject into |
| `inject_event` | push an event/variables to a live session — `buzz` `{winner}`, `judged` `{result}`, `next_question` `{answer, clip_url}` |
| `upload_media` | upload an audio/video clip → `{url, signed_url}` |
| `get_scripts` | read a workflow's WoZ lines (incl. `audio_clip_url`) |

Install + register with Claude Code:

```bash
pip install "srdp[mcp]"     # or: pip install mcp
claude mcp add socialrobot \
  -e SOCIALROBOT_API_KEY=srk_… \
  -e SOCIALROBOT_BASE_URL=https://sociallab.duckdns.org/ntu_social_robot/api/v1 \
  -- socialrobot-mcp
```

Auth via `SOCIALROBOT_API_KEY` (an `srk_` developer key — mint in 個人設定 → API key)
or `SOCIALROBOT_EMAIL` + `SOCIALROBOT_PASSWORD`. The key lives only in the MCP
process's env, never in tool arguments.
