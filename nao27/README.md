# NAO 直接跑 meme（Python 2.7，不用另一台電腦）

`meme_nao27.py` 跑在 NAO 本體上，用 NAOqi 自帶的 Python 2.7 和 `naoqi` 模組；其他需要的套件都已經放在 `vendor/`，機器人上什麼都不用 pip 安裝。

## 它做什麼

- **講話**：流程的每一句，有平台聲音（台灣腔）就用 NAO 的喇叭播平台聲音，沒有就用 NAO 自己的 `ALTextToSpeech`；講完回報，流程才往下走。
- **動作**：伺服器已經換算成 NAO 關節（弧度）的動作，用 `ALMotion.setAngles` 照時間播；只有動作名稱時，點頭／搖頭兩個做得出來，其他名稱不動。
- **聽（連續）**：訂閱 `ALAudioDevice`，頭頂前方麥克風每約 170 毫秒給一段 16 kHz 單聲道聲音，直接送到平台，由平台自己的語音偵測與語音轉文字處理——跟 Android 機器人走同一條路、同一道「現在可不可以聽」的閘門。只在伺服器說可以聽的時候送；機器人自己講話時不送，所以不會聽到自己。舊的「錄一段 → 轉文字」做法還在：`NAO_MIC_MODE=window`。
- **看**：只有在目前的節點需要畫面時（認臉、認人、拍照、手勢模型）才打開頭上的相機，畫面送到平台、由平台轉給影像辨識；其他時候相機關著、燈不亮。用得到 JPEG 編碼（`cv2` 或 `PIL`）就送 JPEG（每秒 5 張），沒有就送原始 RGB（PPM，每秒 2 張，比較吃頻寬）。
- **斷線**：自己重連同一個 session（部署重啟後會接回去）；一直連不上才重新註冊。

## 它不做什麼

- **不能插話（全雙工）**：NAO 本身沒有回音消除（它的播放與收音緩衝無法同步取得），所以平台在機器人講話時會關掉它的耳朵——半雙工。要插話，還是用另一台電腦跑 `sdk/examples/nao_duplex_client.py`（用 WebRTC 的軟體回音消除）。
- 不顯示畫面（NAO 沒有螢幕）。

## 放到 NAO 上（裝一次就好）

1. 把整個資料夾複製上去（NAO 的帳號是 `nao`），進去跑安裝：

       scp -r nao27 nao@<NAO 的 IP>:/home/nao/
       ssh nao@<NAO 的 IP>
       cd /home/nao/nao27
       sh install.sh

   它會問三件事：平台網址（直接 Enter 用正式站）、API 金鑰（**留空**）、要不要固定跑某個角色（**留空**，讓網頁決定）。然後設成開機自動執行，並馬上啟動。這一步只有管理 NAO 的人做一次。

2. 連上：NAO 一啟動會**念出六位數的配對碼**（也寫在 log 裡）。在網頁「個人資源 › 機器人 › 連上機器人」輸入，這台 NAO 就由你使用。**換人用**：摸 NAO 頭頂 3 秒，它念新的配對碼，另一個人輸入就換過去；沒聽清楚就再碰一下頭，它會再念一次；配對碼五分鐘內有效，過期它會說，要再摸頭 3 秒拿新的；NAO 正在跟人說話時，會說「等一下」，說完才換。

3. 到網頁指定角色：**個人資源 › 機器人**，找 `nao-<主機名稱>`（安裝最後會印出來），選一個角色；或在編輯器按「在實機上跑」選這台 NAO。之後要換角色也一樣在網頁換，NAO 大約十秒內自己切過去，不用再 SSH。

   看它在做什麼：`tail -f /home/nao/meme_nao27.log`。看到 `[meme] connected` 就是連上了；還沒指定角色時會印 `waiting for a character`。

   重裝（換金鑰）就再跑一次 `sh install.sh`；拿掉開機自動執行：`sh install.sh --remove`。

不想裝成開機自動執行，也可以手動跑：

    cd /home/nao/nao27
    export SR_BASE_URL="https://sociallab.duckdns.org/ntu_social_robot/api/v1"
    python meme_nao27.py                   # 念配對碼；角色由網頁指定
    NAO_CHARACTER="角色名稱或 id" python meme_nao27.py   # 或固定一個，不跟網頁

## 之後都在網頁上（個人資源 › 機器人）

NAO 在線上時，它那一列下面有：

- **程式版本**：NAO 現在跑的版本，和平台上的最新版比——「最新」或「有新版」。按「**更新到最新版**」：NAO 自己下載、檢查能不能執行、換上新版（舊版留在 `nao27.prev`）、重新啟動；新版 90 秒內沒連回平台，就自己換回舊版。
- **重新啟動程式**、**自主行為**（開＝NAO 自己的 Autonomous Life；關＝只照流程動）、**音量**：設定存在 NAO 上（`nao_settings.json`），重開機照舊。
- 「**現在打包**」：NAO 上傳最近的 log，展開那一列就看得到。

程式只有 repo 裡 `sdk/nao27/` 這一份：合併到 main、部署之後，平台就提供新版，NAO 卡片上會顯示「有新版」。

## 其他設定（都可以不設）

| 變數 | 預設 | 說明 |
|---|---|---|
| `NAO_TAKE_OVER` | `1` | `1`＝啟動時關掉 NAO 自己的 Autonomous Life（內建對話、東張西望）、站起來，結束時還原；`0`＝不動它 |
| `NAO_TOKEN_FILE` | `nao27/device_token` | 配對拿到的授權存在這裡（只有 nao 帳號讀得到） |
| `NAO_CHARACTER` | 不設 | 不設＝跑網頁指定的角色、網頁換它就換；設了就固定跑這一個 |
| `NAO_VOICE` | `auto` | `auto`＝有平台聲音就用、沒有就用 NAO 的；`nao`＝一律 NAO 自己念；`platform`＝只用平台聲音 |
| `NAO_TTS_LANGUAGE` | 不改 | NAO 自己念時的語言，例如 `Chinese`（NAO 要裝了中文語音包） |
| `NAO_LISTEN` | `1` | `0`＝不聽，只講 |
| `NAO_MIC_MODE` | `stream` | `stream`＝連續送麥克風給平台；`window`＝錄一段、轉文字再送（舊做法） |
| `NAO_LISTEN_SECONDS` | `4` | `window` 模式每次錄多久 |
| `NAO_SILENCE_RMS` | `300` | `window` 模式：比這個安靜的錄音不送 |
| `NAO_CAMERA` | `1` | `0`＝永遠不開相機 |
| `NAO_CAMERA_INDEX` | `0` | `0`＝額頭上方的相機、`1`＝嘴巴下方的相機 |
| `NAO_CAMERA_RESOLUTION` | `1` | `0`＝160×120、`1`＝320×240、`2`＝640×480 |
| `NAO_CAMERA_FPS` | JPEG 5／PPM 2 | 每秒送幾張 |
| `SR_ASR_LANGUAGE` | `zh` | 語音轉文字的語言 |
| `NAO_DEVICE_ID` | `nao-<主機名稱>` | 綁定機器人時看到的名字 |
| `NAO_IP` / `NAO_PORT` | `127.0.0.1` / `9559` | 跑在 NAO 上不用改 |
| `SR_CA_BUNDLE` | `vendor/certifi/cacert.pem` | HTTPS 憑證；NAO 內建的太舊，認不得 Let's Encrypt |

## 第一次在真的 NAO 上要確認

- HTTPS 連得上：NAO 的 OpenSSL 若太舊（不支援 TLS 1.2）會連不上 `https://`；錯誤訊息會印在畫面上。
- 麥克風：開頭要印出 `[mic] streaming to the platform (qi)`（NAO6／NAOqi 2.5 以上）或 `(ALBroker)`（更舊的 NAOqi）；對機器人說話時，網頁即時控制台的時序面板要出現聽到的話。
- 相機：走到需要畫面的節點時要印出 `[cam] on (jpeg … 或 ppm …)`，離開時 `[cam] off`；印的是 `ppm` 代表這台 NAO 的 Python 沒有 JPEG 編碼，畫面會慢一點。
- 用 Wi-Fi 時的頻寬：麥克風約 256 kbit/s；相機 JPEG 每秒 5 張約 0.6 Mbit/s，PPM 每秒 2 張約 3.7 Mbit/s。
- 平台聲音：`ALAudioPlayer` 播得出 mp3/wav；播不出來會自動改用 NAO 自己念，並印出原因。
- 中文：NAO 自己念中文需要中文語音包；`NAO_VOICE=auto` 時有平台聲音就不需要。

## 測試

    make e2e-nao27     # python:2.7 單元測試（假的 NAOqi）＋對 dev stack 的實際連線
