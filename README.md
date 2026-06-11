# socialrobot — Social Robot Platform Python SDK

用 Python 程式碼設計角色（取代/搭配視覺化 canvas），並驅動、監看正在執行的機器人。

## 在哪裡用？怎麼用？

**在你自己的電腦（或任何伺服器/CI）跑，不是在網頁裡跑。** 流程：

1. **安裝**（任何 Python ≥3.10 環境）：

   ```bash
   pip install -e sdk/        # 從 repo checkout；之後會發佈到 PyPI
   ```

2. **取得憑證**——擇一：
   - **API key（建議）**：登入網頁 → 右上角使用者設定 → API Key → 產生。
     `srk_` 開頭、只顯示一次，只能存取**你自己**帳號的資源。
   - 或直接用 email + password（SDK 會替你登入換 JWT）。

3. **指向平台**：
   - 部署環境：`https://sociallab.duckdns.org/ntu_social_robot/api/v1`
   - 本機開發：`http://localhost:6868/api/v1`

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

**Outbound**（後端替你輪詢）——掛在節點上的 `OnApiPoll` hook：

```python
waiting = Woz("等待製作", loop=True, hooks=[
    OnApiPoll("https://your-service/jobs/123",
              path="data.status", equals="done", every=2.0,
              write_to="job_status", goto="完成播報"),
])
```

注意：會被 hook 中斷的等待節點請用**無台詞的 loop**（有台詞的 loop 會一直
處於 SPEAKING，sensor hook 會被延後而永遠不觸發）。

## 已驗證的環境

從一般使用者環境（容器外的 venv/conda）對 localhost 與公開 prod URL 全程
驗證過：登入、DSL deploy、WS runtime、HTTP inject。`make e2e-sdk` 是它的
迴歸測試。
