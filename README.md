# socialrobot — 社會機器人平台的 Python SDK

`socialrobot` 讓你的 Python 程式連上一個已經部署好的社會機器人平台：把外部資料送進正在跑的機器人、看它現在在做什麼、叫它說一句話，或讓你自己的硬體（NAO、樹莓派、任何跑得了 Python 的東西）當機器人的身體。

SDK 本身不跑流程、不做語音、不做 AI。這些都在平台上，SDK 是平台的客戶端：每個功能都是對你指定的平台發一個 HTTP 或 WebSocket 請求。流程可以在網頁編輯器畫，也可以用 `Flow` 寫成程式再部署上去（[用程式寫流程](#用程式寫流程flow)）。

> 這個目錄同步鏡像到公開 repo [github.com/hungchunchang/srdp](https://github.com/hungchunchang/srdp)（`main` 分支），供 PyPI 套件頁與外部使用者參考；開發仍在 monorepo 的 `sdk/` 進行，鏡像不接受直接 PR。

## 能做什麼

| 你想做的事 | 用這個 |
|---|---|
| 叫正在跑的機器人馬上說一句 | `client.say()` |
| 把按鈕、感測器、API 結果送進場次，讓流程換節點或填進台詞 | `client.inject()`、`client.open_stream()` |
| 即時看一個場次走到哪個節點、收到哪些事件 | `client.subscribe()` |
| 讓自己的硬體（樹莓派、筆電、自製機器人）當機器人 | `Robot` ＋ `RobotBody` |
| 聲音轉文字、文字轉聲音、跟 LLM 對話（不用開場次） | `client.transcribe()`、`client.tts()`、`client.chat()` |
| 上傳音檔、表情影片、視覺模型 | `client.upload_*()` |
| 把角色的流程放進 git：匯出、改、匯回同一個角色 | `socialrobot export`／`socialrobot import --into` |
| 讓 Claude Code 這類 AI 代理操作平台 | MCP server |

「場次」是一台機器人（或模擬器）正在跑某個角色的那一段連線。網頁的模擬器、凱比、NAO、`Robot`、`RobotSession` 開出來的都算。

## 安裝

需要 Python 3.10 以上。

```bash
pip install socialrobot              # SDK
pip install "socialrobot[mcp]"       # 加上 MCP server
```

從 repo 安裝：`pip install -e sdk/`。安裝名和 import 名都是 `socialrobot`。

> ⚠️ 不要 `pip install srdp`。PyPI 上的 `srdp` 是一個無關的專案（Single Repo Data Platform）。

## 連上平台

```python
import os
from socialrobot import SocialRobotClient

client = SocialRobotClient(
    base_url=os.environ["SR_BASE_URL"],   # 例如 https://sociallab.duckdns.org/ntu_social_robot/api/v1
    api_key=os.environ["SR_API_KEY"],     # srk_ 開頭
)
print([c["name"] for c in client.list_characters()])
```

`SocialRobotClient` 也能當 context manager 用（`with SocialRobotClient(...) as client:`），離開時關掉連線。

### `base_url`：指向你的部署

一律明確傳入，結尾是 `/api/v1`。

- 實驗室的部署：`https://sociallab.duckdns.org/ntu_social_robot/api/v1`
- 自己用 `make up` 起的 dev stack：`http://localhost:<backend 埠>/api/v1`（`make up` 結束時會印出埠號）

沒有預設值：不傳、不是 `http(s)://` 開頭、或沒有指到 `/api/…`，都會馬上丟 `ValueError`。（0.2.1 以前預設是 `http://localhost:6868/api/v1`，而在實驗室那台主機上，6868 就是正式站。）

### 憑證：用 API key

**API key（建議）**：網頁登入 → 個人設定 → 開發者 API 金鑰 → 產生。

- `srk_` 開頭，只在產生時顯示一次，請存進環境變數或密碼管理工具，不要寫進程式碼。
- 可以產生好幾把（每支程式一把），各自撤銷，互不影響。
- 只能存取你自己帳號的東西：你的角色、你的場次。
- 不會過期，在校外也能用。

**Email＋密碼**：`SocialRobotClient(base_url=..., email=..., password=...)`。

- 拿到的是網頁登入的 token，**只在台大網段（或台大 VPN）有效**。登入時 SDK 會先問一次 `/auth/me`，所以在校外會在建立 client 的那一行就丟 `SocialRobotError`（`code` 是 `ntu_network_only`），不會等到第一個呼叫。
- token 60 分鐘過期；SDK 收到 401 時會自動換發一次再重送，長時間跑的程式不用自己處理。
- 帳號開了兩步驟驗證時，沒帶驗證碼會丟 `MfaRequiredError`；要帶 `mfa_code="123456"`（30 秒內有效）。

寫成會一直跑的程式、或要在校外跑，請用 API key。

什麼都不傳時（`SocialRobotClient()`），讀環境變數 `SR_BASE_URL` 和 `SR_API_KEY`，跟 `socialrobot` 指令一樣：金鑰 export 一次，每支程式都不用再帶。有傳的參數優先；用 email＋密碼登入時不會拿環境變數的金鑰。

## 叫正在跑的機器人說一句

先讓一台機器人跑起你的角色（網頁模擬器也可以），然後：

```python
sessions = client.list_sessions()          # 只看得到你自己帳號的場次
sid = sessions[0]["session_id"]

results = client.say(sid, "大家好，歡迎光臨")
print(results[0]["success"])               # True
```

`list_sessions()` 每一列有 `session_id`、`character_id`、`character_name`、`device_id`、`state`（`IDLE`、`SPEAKING`、`LISTENING`…）、`connected_at`。

`say(session_id, text, visual=None, motion=None)` 就是 Live 操作台的「即時指令」：馬上說，不管流程走到哪。`visual` 是表情名稱，`motion` 是動作名稱。回傳每台機器人一筆 `{session_id, robot_id, success, error, visual_shown}`。

## 把外部資料送進場次

流程裡有兩個地方接得到你送進來的東西：

- **變數**：寫進場次變數，台詞裡的 `{{price}}`、數值條件都讀得到。
- **事件**：一個名字。在編輯器把節點的中斷條件設成「別人送進來 › 收到事件」，事件名填同一個名字，收到時就換節點。

如果你的服務本來就有 HTTP API 或 WebSocket，可以不寫程式：在編輯器用「機器人主動去問 › 輪詢 HTTP API／訂閱 WebSocket feed」，由平台自己去問，值對了就換節點。

### 一次一筆：`inject`

```python
client.inject(sid, variables={"price": 42})
# {'ok': True, 'event': None, 'variables_written': ['price']}

client.inject(sid, event="order_ready")
# {'ok': True, 'event': 'order_ready', 'variables_written': [], 'matched': True}
```

同一次呼叫可以同時帶 `event` 和 `variables`；變數先寫，事件後送。

「收到事件」的中斷條件在機器人說話中也會立刻觸發，不用等這句說完。

### 很多筆：`open_stream`

要一直送（例如每秒一筆感測值），開一條 WebSocket，不要一直 `inject`：

```python
import asyncio

async def main():
    async with await client.open_stream(sid) as stream:
        await stream.send(variables={"price": 99})
        ack = await stream.send(event="order_ready")
        print(ack)   # 每一筆都回一個確認，格式跟 inject 的回傳一樣

asyncio.run(main())
```

送了不允許的事件名稱時，確認裡會是 `{'ok': False, 'error': ..., 'code': 422}`，連線不會斷。

### 怎麼知道事件有沒有接到：`matched`

| `matched` | 意思 |
|---|---|
| `True` | 現在的節點有在等這個事件，已經觸發 |
| `False` | 現在的節點沒有在等這個名字。事件**不會排隊**，就這樣丟了 |
| 沒有這個欄位 | 不知道。場次在另一個 worker 上，沒有及時回覆 |

中斷條件只看「現在這個節點」。事件早送了一步、或流程已經走過頭，都會是 `False`。

### 不能送的事件名稱

這些名字有專用的管道，送了回 422：`client_ready`、`tts_finished`、`face_detected`、`asr_input`、`agent_judgments`、`voice_phase`、`UI_CLICK`、`UI_INPUT`、`button_click`、`error`、`info`、`ARRIVED`。

### 有特別意思的事件名稱

| 事件 | 做什麼 |
|---|---|
| `restart_flow` | 跳回流程的第一個節點 |
| `create_reminder` | 在這個場次排一句提醒：`variables={"say": "記得吃藥", "when": "十分鐘後"}`。`when` 可以是「十分鐘後」「in 10 min」「at 14:05」「每天早上8點」（只排下一次）。回傳 `reminder_armed`：`False` 表示時間看不懂、已經過了，或這個場次的提醒已經滿了 |
| `cancel_reminder` | 取消這個場次所有的提醒，回傳 `reminders_cancelled`（取消了幾個） |

### 錯誤

| 狀態碼 | 原因 |
|---|---|
| 404 `session not found` | 場次不存在、已經結束，或不是你帳號的 |
| 409 `session has no active interaction yet` | 送變數時流程還沒開始，稍等再送 |
| 422 | 沒帶 `event` 也沒帶 `variables`，或用了不能送的事件名稱 |

## 看場次在做什麼

`subscribe` 開一條只收不送的 WebSocket，場次每換一次狀態、每收到一個事件就收到一筆：

```python
async def watch(sid):
    async with await client.subscribe(sid) as events:
        async for ev in events:
            print(ev)

# {'type': 'state', 'state': 'IDLE', 'stage_name': '等待', 'stage_mode': 'woz'}
# {'type': 'event', 'event': 'order_ready'}
# {'type': 'state', 'state': 'SPEAKING', 'stage_name': '播報', 'stage_mode': 'woz'}
```

`stage_name` 是節點名稱；`stage_mode` 是 `woz`（腳本節點）或 `conversation`（AI 節點）。

## 讓你的硬體當機器人

樹莓派、筆電、自己做的機器人都可以：你只寫「這台機器怎麼說話、怎麼停、怎麼動、怎麼亮燈」，其餘交給 `Robot`。

```python
import os
import pyttsx3
from socialrobot import Robot, RobotBody

engine = pyttsx3.init()

class Speaker(RobotBody):
    def say(self, text):          # 說完才回來：回來時 Robot 會跟平台說「念完了」
        engine.say(text)
        engine.runAndWait()

    def stop(self):               # 編輯器的 ■、中控台的立刻停
        engine.stop()

Robot(Speaker(), base_url=os.environ["SR_BASE_URL"], device_id="pi-kitchen-01").run()
```

第一次跑、沒有給金鑰時，它會**說出一組六位數配對碼**；在網頁「個人資源 › 機器人 › 連上機器人」輸入，它就替你的帳號工作，金鑰自己存起來（`~/.config/socialrobot/<device_id>.token`，只有你讀得到），下次開機不用再配對。之後在網頁同一頁把它綁一個角色，它就跑那個角色；換角色、解除綁定，它都會自己跟著換，不用碰機器。沒有角色時它待命，可以從中控台直接操作。

`Robot` 替你做的事：

- 配對（摸頭 `head_held()` 三秒會換一組新的配對碼，讓別人接手）、跟著網頁的綁定換角色、回報狀態給網頁的機器人卡片
- 每說完一句就回報（`tts_finished`）——沒回報的話平台會等十幾秒才往下，機器人看起來又慢又聾
- ■／立刻停：正在說的那句切掉、不回報，佇列裡的台詞丟掉
- 機器人卡片的「音量」「看 log」（給 `log_file=` 就上傳那個檔案的最後一段）
- 斷線後接回同一個場次，一直接不上才重新註冊
- 麥克風和鏡頭：只在平台說可以聽、這個節點需要看的時候才送

`RobotBody` 裡可以覆寫的（都有預設，沒有的硬體就不用寫）：

| 方法／屬性 | 做什麼 |
|---|---|
| `say(text)` | 說一句，說完才回來 |
| `stop()` | 立刻停下正在說的、正在動的；會在 `say` 還沒回來時從另一個執行緒被呼叫 |
| `play_audio(audio) -> bool` | 用平台的聲音說（mp3/wav 的 bytes），回 `True`；回 `False` 就改用 `say` |
| `gesture(name)`／`play_keyframes(keyframes)` | 動作名稱，或換算好的馬達角度（有角度時不會再給名稱） |
| `set_led(led)` | 節點的燈光（顏色、呼吸、輪播、分部位） |
| `show(visual, url)` | 有螢幕時顯示表情 |
| `set_volume(volume)` | 網頁卡片調的音量（0–100） |
| `head_held() -> bool` | 有人按著頭（或你的「重新配對」按鈕） |
| `start_microphone(on_pcm) -> bool` | 開始收音：從你的錄音執行緒呼叫 `on_pcm(16 kHz 單聲道 16-bit PCM)`；平台的語音辨識會處理 |
| `camera_frame() -> bytes` | 一張 JPEG／PNG 照片；只有需要看的節點才會來要 |
| `robot_type` | 平台認得的機型（例如 `"nao"`），動作就會換算成那個機型的關節角度 |
| `kind` | 網頁卡片上顯示的機器種類 |

`Robot(body, base_url, device_id, api_key=None, character=None, token_file=None, log_file=None)`：給 `api_key` 就不用配對；給 `character`（id 或名稱）就固定跑那個角色、不跟網頁的綁定。`robot.emit(事件, data)` 可以從任何執行緒送事件進流程，例如按鈕、感測器、你自己的語音辨識（`emit("asr_input", 文字)`）。`run()` 會一直跑到 Ctrl-C；用 asyncio 的程式改 `await robot.run_async()`。

平台與機器人之間的協定寫在 [`DEVICE_PROTOCOL.md`](https://github.com/hungchunchang/srdp/blob/main/DEVICE_PROTOCOL.md)，有版本號：`Robot` 註冊時會送出自己的版本，平台不一樣時會在 log 裡說。

### 低階：`RobotSession`

`RobotSession` 只開一個場次、一條連線，其餘（配對、跟著綁定、斷線重連、回報）都要自己寫；適合測試程式。它用你的帳號開一個場次，就像一台機器人選了這個角色：平台照流程送指令給你的程式，你的程式負責說出來、動起來，再回報。

```python
import asyncio
from socialrobot import RobotSession

async def main():
    async with await RobotSession.start(client, "我的導覽員") as robot:
        print("場次：", robot.session_id)
        async for ev in robot.updates():
            d = ev.get("data") or {}
            if ev["state"] == "SPEAKING" and d.get("text"):
                print("機器人說：", d["text"])          # 換成你的喇叭
                await robot.send_event("tts_finished")  # 說完了，一定要回

asyncio.run(main())
```

**每說完一段都要回 `tts_finished`。** 平台收到才會送下一句、才會處理觸碰、看到人、聽到的話（說話中這些都會延後）。沒回的話，平台會等一個保底時間（12 秒加每字 0.45 秒，最多 180 秒）才自己往下走，機器人會顯得又慢又聾。

`RobotSession.start(client, character, device_id="sdk", robot_type=None)`：

- `character`：角色 id、`list_characters()` 回來的那一筆，或角色名稱。
- `device_id`：這台裝置的固定名字，同一個帳號好幾台時用來分辨。
- `robot_type`：你的程式真的在驅動某個機型（例如 `"nao"`）時一定要填，平台才會照這個機型送換算好的馬達角度。不填就當成沒有身體的程式：台詞、表情、動作名稱照送，馬達角度不送。

其他方法：

| 方法 | 做什麼 |
|---|---|
| `updates()` | 一筆一筆給你平台送來的 `state_update`，給主迴圈用 |
| `expect(state=, stage_name=, stage_mode=, text=, prompt=, timeout=10)` | 等到符合條件的那一筆，逾時回 `None`。跟 `updates()` 吃同一個佇列，同一個場次只用其中一個 |
| `send_event(name, data=None)` | 回報事件給平台，例如 `tts_finished`、觸碰、`UI_CLICK` |
| `send_face(user_id=None, embedding=None, confidence=0.9)` | 模擬鏡頭看到人臉 |
| `inject(event=None, variables=None)` | 同 `client.inject`，自動帶這個場次 |
| `tail(n=8)` | 最近幾筆的（狀態、節點、文字），除錯用 |

`state_update` 裡有哪些欄位（台詞、音檔、表情、馬達角度、畫面設定）、要回哪些事件、怎麼送麥克風和鏡頭，寫在 [`DEVICE_PROTOCOL.md`](https://github.com/hungchunchang/srdp/blob/main/DEVICE_PROTOCOL.md)。完整的硬體範例是 [`examples/nao_meme_bridge.py`](https://github.com/hungchunchang/srdp/blob/main/examples/nao_meme_bridge.py)。

`RobotSession` 不會自己播音、動馬達或開麥克風，這些都要你的程式做。

## 上傳素材

```python
up = client.upload_media("beep.wav")             # mp3 / m4a / wav / ogg / webm / mp4
# {'url': '/uploads/conv_….wav?sig=…', 'signed_url': '/uploads/conv_….wav?sig=…'}

clip = client.create_audio_asset("嗶一聲", up["url"], duration_seconds=0.1)
# 編輯器的音檔素材庫裡就有「嗶一聲」；資料結構的格子裡寫 asset:<clip["id"]>
```

| 方法 | 做什麼 |
|---|---|
| `upload_media(path)` | 上傳音檔或影片，回傳 `{url, signed_url}`。網址是相對於平台主機的路徑 |
| `create_audio_asset(name, url, duration_seconds=None)`／`list_audio_assets()` | 把上傳的音檔登記進音檔素材庫 |
| `upload_expression(path, name)` | 上傳一段 mp4 當表情 |
| `upload_vision_model(path, name, model_type="onnx", labels=[...])` | 上傳分類模型（`.onnx`／`.pt`／`.tflite`…，Teachable Machine 匯出的 `.tflite` 可以直接用）。`labels` 要照模型輸出的順序。上傳後在編輯器的中斷條件選「機器人自己感知到 › 視覺模型看到」 |
| `list_vision_models()`／`delete_vision_model(id)` | 列出、刪除視覺模型 |

## 語音辨識與語音合成

不用開場次、不用機器人，直接請平台把聲音變文字、文字變聲音：

```python
out = client.transcribe("訪談片段.wav")        # 或傳檔案的 bytes
print(out["text"], out["seconds"])               # {'text': '…', 'seconds': 12.4, 'provider': 'platform'}

mp3 = client.tts("歡迎來到博物館。")             # bytes
open("welcome.mp3", "wb").write(mp3)
```

| 方法 | 做什麼 | 限制 |
|---|---|---|
| `transcribe(audio, language="zh", provider="platform")` | 聲音 → 文字。`audio` 是檔案路徑或 bytes（wav、mp3、m4a、webm、ogg） | 一段最長 120 秒；每把 API key 每分鐘 20 次 |
| `tts(text, provider="platform", voice=None)` | 文字 → mp3 的 bytes | 一次最多 1000 字；每把 API key 每分鐘 30 次 |

`provider` 決定誰來做、記在誰的帳上：

- **`"platform"`（預設）**：部署自己架的語音服務，機器人講話用的就是這個聲音（只有一種，所以不能選 `voice`）。轉文字排在所有正在進行的場次後面；忙的時候丟 `SocialRobotError` 503，照 `Retry-After` 等一下再送。
- **`"openai"`**：用**你自己的** OpenAI 金鑰（網頁 › 個人設定 › 機器人與金鑰 › AI 金鑰），費用記在你的 OpenAI 帳上。`tts` 可以選 `voice`：alloy、ash、ballad、coral、echo、fable、nova、onyx、sage、shimmer、verse。沒填金鑰會回 400；平台不會拿自己的金鑰幫你付。

每一次呼叫都記進你帳號的用量（網頁 › 個人設定 › 機器人與金鑰，AI 金鑰下面）。限制是每把金鑰分開算：兩支程式各用一把，不會互相擋到。

## 跟 LLM 對話

```python
out = client.chat("用一句話介紹台北 101")
print(out["choices"][0]["message"]["content"])

out = client.chat([
    {"role": "system", "content": "你是博物館導覽員，回答不超過三句。"},
    {"role": "user", "content": "這件青花瓷是哪個朝代的？"},
], temperature=0.3, max_tokens=200)
```

`chat(messages, model=None, temperature=None, max_tokens=None, json_mode=False)`：`messages` 是 OpenAI 的格式，或直接一個字串。回傳 OpenAI 的 `chat.completion` 格式。

- **不給 `model`**：部署自己架的模型，費用不記在任何廠商帳上。
- **`model="gpt-4.1-mini"` 這類**：用**你自己的**金鑰（網頁 › 個人設定 › 機器人與金鑰 › AI 金鑰）。可選的：`gpt-4o`、`gpt-4o-mini`、`gpt-4.1`、`gpt-4.1-mini`、`gemini-2.5-flash`、`gemini-2.5-pro`、`gemini-2.0-flash`。
- `json_mode=True` 要模型回一個 JSON 物件。
- 每把 API key 每分鐘 20 次；`max_tokens` 最多 2048（預設 1024）；不支援串流。

這個端點跟 OpenAI 的相容，所以 `openai` 套件也能直接用，一把 API key、一個網址：

```python
from openai import OpenAI

llm = OpenAI(base_url=os.environ["SR_BASE_URL"] + "/llm", api_key=os.environ["SR_API_KEY"])
r = llm.chat.completions.create(model="local_gateway", messages=[{"role": "user", "content": "嗨"}])
print(r.choices[0].message.content)
```

## 用程式寫流程：`Flow`

你的 `for` 和 `if` 只在部署那一刻跑一次：它可以產生 20 個題目節點，但機器人跑起來以後，Python 程式早就結束了，機器人跑的是平台上那張流程圖。所以「答錯就再問、最多三次」「依分數走不同的路」這種**要等機器人跑到那裡才知道**的判斷，要用 `Flow` 的區塊寫。它們會編成一般的節點和「進入時」條件，在網頁編輯器裡看得到、改得了：

```python
from socialrobot import NEXT, Button, Character, Flow, Screen, Script, Say, SocialRobotClient, var

def 問吃藥():
    return Screen("問吃藥", prompt="第 {{第幾次}} 次：吃藥了嗎？", input_var="吃了", buttons=[
        Button("吃了", value="yes", goto=NEXT), Button("還沒", value="no", goto=NEXT)])

f = Flow()
f.then(Script("問候", lines=[Say("早安")]))
with f.Repeat(3, counter="第幾次", until=var("吃了") == "yes"):
    f.then(問吃藥())
with f.If(var("吃了") == "yes"):
    f.then(Script("好", lines=[Say("太好了")]))
with f.Else():
    f.then(Script("提醒家人", lines=[Say("我請家人來看看")]))

with SocialRobotClient() as client:            # 讀 SR_BASE_URL、SR_API_KEY
    Character(name="吃藥", nodes=f.nodes).deploy(client, in_place=True)
```

- `f.then(節點)`：接在前一個後面（前一個的「正常結束」走到它）。按鈕或中斷條件的 `goto=NEXT` 也一樣，指到下一個接上的節點。
- `with f.If(條件):`、`f.Elif(條件)`、`f.Else()`：一個不說話的節點，「進入時」依序比對條件，第一個成立的走；都不成立走 `Else`，沒有 `Else` 就接到區塊後面。
- `with f.Repeat(n, counter="次數", until=條件):`：跑 n 次，`{{次數}}` 是第幾次（從 1 開始），可以念出來；有 `until` 的話，每一輪跑完先看它，成立就提早離開。同一個場次再走到這裡，會從 1 重新數。
- `with f.While(條件):`：條件成立就跑一輪，不成立就往下。
- `with f.ForEach("題庫"):`：資料節點裡的結構，一列跑一輪，`{{欄位}}`、`{{題庫.序號}}`、`{{題庫.總數}}` 都讀得到；跑完最後一列往下，並回到第一列，下次從頭。
- 條件：`var("x") == "yes"` 比文字；`>`、`>=`、`<`、`<=` 比數字，另一邊可以是 `var("門檻")`；`!=` 只能用在沒有 `Elif` 的 `If` 和 `While`。沒有 `and`／`or`：兩個條件就巢狀兩個 `If`。
- 用 Python 自己的 `if`、`and`、`not` 去判斷 `var(...)` 會直接報錯，因為那時候還沒有值。
- `f.nodes` 是全部的節點；還沒接上的出口會接到最後一個「結束」節點。把它們放在角色節點清單的最後面。
- `Repeat` 最多 50 次（檢查節點上一次一列）；更多請用 `ForEach` 走資料表。

這些區塊只能組合流程圖本來就有的功能。要在執行時跑你自己的程式，用下面的程式節點。

## 程式節點：機器人跑到這裡時問你的程式

自己的計分公式、從自己的 API 挑題、呼叫自己的模型——流程圖做不到的判斷，交給你自己跑的程式。機器人走到程式節點時，平台把目前的變數送給它；它回傳的東西寫成變數，再依它選的出口往下走。

你的程式（在筆電、實驗室電腦都可以，不用公開網址）：

```python
from socialrobot import Answer, SocialRobotClient

def 挑下一題(call):                       # call.variables、call.node、call.session_id
    score = int(call.variables.get("分數", 0))
    if score >= 60:
        return Answer(exit="過關", say=f"{score} 分，過關了")
    return Answer(exit="再一題", variables={"題目": pick_question(score)})

SocialRobotClient().serve({"挑下一題": 挑下一題})   # 一直跑，Ctrl-C 結束；斷線會自己重連
```

流程（用 `Flow`，或在網頁編輯器：腳本分頁最上面的「程式」填程式名稱）：

```python
from socialrobot import Program

done = Script("過關", lines=[Say("{{program_say}}")])
f.then(Program("挑題", step="挑下一題", exits={"過關": done}, seconds=10))
f.then(Script("出題", lines=[Say("{{題目}}")]))      # 其他出口（再一題）走這裡
```

- 回傳 `Answer(exit=, say=, variables=)`，或 dict、只有出口的字串、`None`；函式可以是 `async`。
- 平台先寫好變數：你的 `variables`、`{{program_exit}}`、`{{program_say}}`，再走出口。`exits` 對到的走那裡，其他走 `otherwise`。
- **程式沒開、超過 `seconds`、或丟出例外**：走 `no_reply`，原因在 `{{program_error}}`（`offline`／`timeout`／例外的文字）。一定要接，不然那天沒人開程式，機器人就停在那裡（流程檢查會提醒）。
- 只會問到**你帳號**的機器人；平台送出的是 `{{}}` 讀得到的變數，包括語音轉出來的文字。
- `client.programs()`：現在連著的程式名稱。


## 把流程放進 git：命令列工具

裝了 SDK 就有 `socialrobot` 這個指令。匯出的就是平台自己的匯出檔，不掉資料（節點、台詞、AI 設定、便利貼、畫布排列）。

```bash
export SR_BASE_URL=https://sociallab.duckdns.org/ntu_social_robot/api/v1
export SR_API_KEY=srk_…

socialrobot characters                            # 你的角色：id 和名稱
socialrobot export 博物館導覽 -o guide.json        # 用名稱或 id
git add guide.json && git commit -m "導覽：改開場白"
socialrobot import guide.json --into 博物館導覽    # 蓋回同一個角色
socialrobot import guide.json                     # 或匯入成一個新的私人角色
```

- 匯出的檔案是固定格式（鍵排序、縮排、中文照原樣），同一個角色沒改過就只差 `exported_at` 那一行，git 的差異看得懂。
- `--into` 會**保留角色的 id**：綁著它的機器人、協作者、受試者與紀錄都不受影響；檔案裡還在的節點也保留原本的 id。被換掉或拿掉的節點會進網頁的垃圾桶，可以還原。誰能看到這個角色、資料保留多久、多演員專案哪台機器人演哪個角色，不會被檔案改掉。
- 匯出之後，如果**別人**在網頁上改過這個角色的節點，`--into` 會停下來、說是誰改了哪幾個；確定要蓋掉再加 `--force`。沒有 `exported_at` 的舊檔案也要加 `--force`。
- 程式裡用：`client.export_character(id)`、`client.import_character(bundle)`、`client.replace_character(id, bundle, force=False)`。

## 直接讀寫角色和流程

這些方法直接對應平台的 REST API，payload 就是編輯器存的那個格式。適合讀資料、改名稱和描述這類簡單欄位；設計流程請用網頁編輯器。

| 方法 | 對應 |
|---|---|
| `list_characters()`、`create_character(name, ...)`、`update_character(id, **fields)`、`delete_character(id)` | 角色 |
| `list_workflows(character_id)`、`create_workflow(...)`、`update_workflow(...)`、`delete_workflow(id)`、`put_agent_config(...)` | 節點 |
| `get_scripts(workflow_id)`、`bulk_scripts(...)`、`update_script(...)` | 腳本節點的台詞 |
| `robot_profiles()` | 平台支援的機型和各自的硬體 |

節點類別（`Character`、`Script`、`Say`、`Screen`、`Button`、`Agent`、`OnEnter`、`OnTimeout`…）從 0.7.0 起是公開 API，跟 `Flow` 一起用；寫法見[用程式寫流程](#用程式寫流程flow)。

## MCP server

把平台變成 [MCP](https://modelcontextprotocol.io) 工具，讓 Claude Code 這類代理用對話操作。

| 工具 | 做什麼 |
|---|---|
| `list_characters` | 你的角色（id、名稱、是否公開） |
| `list_sessions` | 正在跑的場次 |
| `inject_event` | 把事件或變數送進場次 |
| `upload_media` | 上傳音檔或影片 |
| `get_scripts` | 讀一個腳本節點的台詞 |

```bash
pip install "socialrobot[mcp]"
claude mcp add socialrobot \
  -e SOCIALROBOT_API_KEY=srk_… \
  -e SOCIALROBOT_BASE_URL=https://sociallab.duckdns.org/ntu_social_robot/api/v1 \
  -- socialrobot-mcp
```

憑證只放在 MCP 程序的環境變數（`SOCIALROBOT_API_KEY`，或 `SOCIALROBOT_EMAIL`＋`SOCIALROBOT_PASSWORD`，後者只在台大網段有效），不會出現在工具參數裡。

## 錯誤處理

平台拒絕一個請求時丟 `SocialRobotError`。訊息裡就有平台給的原因，常見的還會說該怎麼做：

```
socialrobot.client.SocialRobotError: GET /api/v1/auth/me → 403：僅限台大網域使用，校外請先連台大 VPN（ntu_network_only）。用 email＋密碼登入拿到的憑證只在台大網段（或台大 VPN）有效；在校外跑的程式請改用 API key（網頁 › 個人設定 › 機器人與金鑰 › 開發者 API 金鑰），傳 api_key="srk_…"
```

要在程式裡分辨，讀它的欄位：

```python
from socialrobot import SocialRobotError

try:
    client.inject(sid, event="order_ready")
except SocialRobotError as e:
    print(e.status_code, e.code, e.detail, e.hint)
```

`SocialRobotError` 是 `httpx.HTTPStatusError` 的子類別，原本寫 `except httpx.HTTPStatusError`、讀 `e.response` 的程式照樣能用。

| 狀態碼 | 常見原因 |
|---|---|
| 401 | API key 錯了或被撤銷 |
| 403 `ntu_network_only` | 用密碼登入，而且人在校外；改用 API key |
| 404 | 東西不存在，或不是你帳號的；每個呼叫都 404 就是 `base_url` 指錯了 |
| 429 | 太頻繁；照 `Retry-After` 等一下 |

想在長時間執行前先確認金鑰還有效：`client.whoami()`。

## 目前沒有的

- 不能單獨呼叫視覺推論；它只在場次裡由流程使用。
- SDK 是同步的 HTTP 客戶端；串流（`open_stream`、`subscribe`、`RobotSession`）用 `asyncio`。

## 範例

[`examples/`](https://github.com/hungchunchang/srdp/tree/main/examples) 裡有完整程式：

- `repeat_line.py`：用 `say` 念一句、停幾秒、再念
- `tailscale_camera_driver.py`：自己的鏡頭偵測到人時送事件進場次
- `nao_meme_bridge.py`：NAO 當機器人身體（說話、動作、麥克風）
- `nao_duplex_client.py`：NAO 走全雙工語音
- `ecrobot_puzzle_bridge.py`：手寫格子謎題當感測器

版本紀錄在 [`CHANGELOG.md`](https://github.com/hungchunchang/srdp/blob/main/CHANGELOG.md)。
