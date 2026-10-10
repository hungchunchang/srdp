"""Synchronous REST client for the Social Robot Platform.

Auth: either a developer API key (``srk_…``, generated in UserSettings —
sent as a Bearer token; the backend's get_current_user accepts it on every
endpoint including /robot/register) or email+password (logs in for a JWT).

F6: email+password login now gets a short-lived (default 60min) access token
plus an httpOnly refresh-token cookie, same as the web app. `self._http` is a
single persistent `httpx.Client` for the client's whole lifetime, so it
already carries that cookie via httpx's built-in cookie jar — `_req`/`_req_url`
use it to silently refresh once on a 401 and retry, so a long-running `RobotSession`
(``sdk/socialrobot/runtime.py``, e.g. its ``api_poll``/``ws_subscribe`` loops)
started with email+password keeps working past the access token's expiry
without the caller doing anything. An ``api_key`` session never expires and
has no refresh cookie, so it skips this entirely.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx


class MfaRequiredError(RuntimeError):
    """Raised by `SocialRobotClient(email=..., password=...)` when the account
    has two-factor login on and no `mfa_code` was given."""


_KEY_PAGE = "網頁 › 個人設定 › 機器人與金鑰 › 開發者 API 金鑰"

_BASE_URL_HELP = (
    "結尾是 /api/v1，例如 https://sociallab.duckdns.org/ntu_social_robot/api/v1"
    "（實驗室的部署）或 http://localhost:<埠>/api/v1（你自己用 make up 起的 dev stack）。"
    "沒有預設值：在實驗室那台主機上，localhost:6868 就是正式站"
)


def _check_base_url(base_url: Optional[str]) -> str:
    """No default and no guessing: the old default, localhost:6868, IS the
    production site on the lab box, so a script that forgot the argument
    wrote into real users' data without a word."""
    if not base_url:
        raise ValueError("base_url 必填，" + _BASE_URL_HELP)
    if not base_url.startswith(("http://", "https://")):
        raise ValueError(f"base_url={base_url!r} 要以 http:// 或 https:// 開頭，" + _BASE_URL_HELP)
    if "/api/" not in base_url:
        raise ValueError(f"base_url={base_url!r} 要指到平台的 API，" + _BASE_URL_HELP)
    return base_url.rstrip("/")


def _reason(response: httpx.Response) -> "tuple[str, Optional[str]]":
    """(detail, code) out of a FastAPI error body, whatever its shape: a
    string `detail`, a `{code, message}` dict, a 422 list, or a top-level
    `code` (the NTU network gate answers `{"detail", "code"}`)."""
    try:
        body = response.json()
    except ValueError:
        return (response.text or "").strip()[:300], None
    if not isinstance(body, dict):
        return str(body)[:300], None
    code = body.get("code")
    detail = body.get("detail")
    if isinstance(detail, dict):
        code = code or detail.get("code")
        detail = detail.get("message") or detail.get("detail") or detail.get("code")
    elif isinstance(detail, list):
        detail = "；".join(
            f"{'.'.join(str(x) for x in (d.get('loc') or [])[1:])}: {d.get('msg')}"
            if isinstance(d, dict) else str(d)
            for d in detail
        )
    return ("" if detail is None else str(detail)), (str(code) if code else None)


def _hint(response: httpx.Response, detail: str, code: Optional[str],
          using_api_key: bool) -> Optional[str]:
    status = response.status_code
    if code == "ntu_network_only":
        return ("用 email＋密碼登入拿到的憑證只在台大網段（或台大 VPN）有效；"
                f"在校外跑的程式請改用 API key（{_KEY_PAGE}），傳 api_key=\"srk_…\"")
    if status == 401 and using_api_key:
        return f"這把 API key 無效或已被撤銷，到{_KEY_PAGE}看它還在不在"
    if status == 429:
        wait = response.headers.get("retry-after")
        return f"請求太頻繁，{wait} 秒後再試" if wait else "請求太頻繁，稍後再試"
    if status == 404 and detail == "Not Found":
        # FastAPI's answer for a route that does not exist — every call
        # getting it means base_url points at the wrong place.
        return "如果每個呼叫都是這樣，檢查 base_url：" + _BASE_URL_HELP
    return None


class SocialRobotError(httpx.HTTPStatusError):
    """The platform refused a request — with the server's own reason.

    `str(e)` says what was refused, why (the server's `detail`), and, for the
    refusals a script hits most, what to do. It subclasses
    `httpx.HTTPStatusError`, so `except httpx.HTTPStatusError` and
    `e.response` keep working.

    Attributes: `status_code`; `detail`, the server's words; `code`, its
    machine-readable reason (e.g. ``ntu_network_only``), or None; `hint`,
    what to do, or None.
    """

    def __init__(self, response: httpx.Response, *, using_api_key: bool = False):
        self.status_code = response.status_code
        self.detail, self.code = _reason(response)
        self.hint = _hint(response, self.detail, self.code, using_api_key)
        req = response.request
        msg = f"{req.method} {req.url.path} → {response.status_code}"
        if self.detail or self.code:
            msg += "：" + (self.detail or "") + (f"（{self.code}）" if self.code else "")
        if self.hint:
            msg += "。" + self.hint
        super().__init__(msg, request=req, response=response)


class SocialRobotClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        email: Optional[str] = None,
        password: Optional[str] = None,
        timeout: float = 30.0,
        mfa_code: Optional[str] = None,
    ):
        self.base_url = _check_base_url(base_url)
        self._http = httpx.Client(timeout=timeout)
        if api_key:
            self._token = api_key
            self._can_refresh = False
        elif email and password:
            self._can_refresh = True
            r = self._http.post(f"{self.base_url}/auth/login",
                                json={"email": email, "password": password})
            self._raise_for(r)
            body = r.json()
            if body.get("mfa_required"):
                # The account has two-factor login on: the password alone
                # issues no token. A script should use an API key (個人設定 →
                # API Key) — a six-digit code expires in 30 s, a key does not.
                if not mfa_code:
                    raise MfaRequiredError(
                        "這個帳號開了兩步驟驗證：寫程式請改用 api_key=\"srk_…\""
                        f"（{_KEY_PAGE}），或帶 mfa_code=\"123456\"（30 秒內有效）"
                    )
                r = self._http.post(f"{self.base_url}/auth/login/mfa",
                                    json={"mfa_ticket": body["mfa_ticket"],
                                          "code": mfa_code})
                self._raise_for(r)
                body = r.json()
            self._token = body["access_token"]
            # Off campus the login itself succeeds and every call after it is
            # refused (the token is a web session's, good only on the NTU
            # network) — so ask once now, and the refusal points at this line
            # instead of at whatever the script happened to call first.
            self.whoami()
        else:
            raise ValueError("要給 api_key，或 email＋password")

    def _raise_for(self, r: httpx.Response) -> None:
        if r.is_error:
            raise SocialRobotError(r, using_api_key=not self._can_refresh)

    # ---------- plumbing ----------

    @property
    def token(self) -> str:
        """The resolved bearer (API key or minted JWT) — used to auth the
        inbound stream websocket via its ?token= query param."""
        return self._token

    @property
    def _auth(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _refresh(self) -> bool:
        """Silent refresh (F6) — see module docstring. Returns False (caller
        falls through to the normal 401 raise) if there's no valid refresh
        cookie, e.g. it expired/was revoked/was never issued (api_key)."""
        try:
            r = self._http.post(f"{self.base_url}/auth/refresh")
            r.raise_for_status()
        except httpx.HTTPError:
            return False
        self._token = r.json()["access_token"]
        return True

    def _req_url(self, method: str, url: str, **kw) -> Any:
        """Same silent-refresh-on-401 retry as `_req`, for the few endpoints
        that live outside `base_url` (e.g. /uploads/*, mounted at the host
        root). Any `files=` payload must be bytes, not an open handle — the
        retry re-sends the same body."""
        r = self._send(method, url, **kw)
        return r.json() if r.content else None

    def _send(self, method: str, url: str, **kw) -> httpx.Response:
        r = self._http.request(method, url, headers=self._auth, **kw)
        if r.status_code == 401 and self._can_refresh and self._refresh():
            r = self._http.request(method, url, headers=self._auth, **kw)
        self._raise_for(r)
        return r

    def _req(self, method: str, path: str, **kw) -> Any:
        return self._req_url(method, f"{self.base_url}{path}", **kw)

    # ---------- account ----------

    def whoami(self) -> dict:
        """The account these credentials act as (`GET /auth/me`). A cheap way
        to check an API key before a long run."""
        return self._req("GET", "/auth/me")

    # ---------- speech (語音), outside any session ----------

    def transcribe(self, audio, language: str = "zh", provider: str = "platform") -> dict:
        """Speech → text through the platform (`POST /asr/transcribe`).

        `audio` is a file path or the file's bytes — wav, mp3, m4a, webm, ogg,
        at most 120 s. Returns ``{"text", "seconds", "provider"}``.

        `provider="platform"` uses the deployment's own recognizer, behind
        every live robot session (busy → `SocialRobotError` 503 with
        Retry-After); `provider="openai"` uses YOUR OpenAI key, set on the
        web under 個人設定 › AI 金鑰 — never the platform's.
        """
        if isinstance(audio, (bytes, bytearray)):
            name, data = "audio", bytes(audio)
        else:
            p = Path(audio)
            name, data = p.name, p.read_bytes()
        return self._req("POST", "/asr/transcribe",
                         data={"language": language, "provider": provider},
                         files={"audio": (name, data, "application/octet-stream")})

    def tts(self, text: str, provider: str = "platform", voice: Optional[str] = None) -> bytes:
        """Text → speech through the platform (`POST /tts/synthesize`); returns
        the mp3's bytes. At most 1000 characters.

        `provider="platform"` is the voice the robots speak with (one voice,
        so no `voice`); `provider="openai"` uses YOUR OpenAI key and takes a
        `voice` (alloy, ash, ballad, coral, echo, fable, nova, onyx, sage,
        shimmer, verse).
        """
        body: Dict[str, Any] = {"text": text, "provider": provider}
        if voice is not None:
            body["voice"] = voice
        return self._send("POST", f"{self.base_url}/tts/synthesize", json=body).content

    def chat(self, messages, model: Optional[str] = None, temperature: Optional[float] = None,
             max_tokens: Optional[int] = None, json_mode: bool = False) -> dict:
        """Chat with an LLM through the platform (`POST /llm/chat/completions`).

        `messages` is OpenAI-style (``[{"role": "user", "content": "…"}]``) or
        just a string (one user message). Returns the OpenAI-shaped
        ``chat.completion``; the reply is
        ``out["choices"][0]["message"]["content"]``.

        No `model` (or ``"local_gateway"``) is the deployment's self-hosted
        model. An OpenAI/Gemini model (``gpt-4.1-mini``, ``gemini-2.5-flash``…)
        uses YOUR key from 個人設定 › 機器人與金鑰 › AI 金鑰 — never the
        platform's. `json_mode=True` asks for a JSON object.
        """
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        body: Dict[str, Any] = {"messages": list(messages)}
        if model is not None:
            body["model"] = model
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return self._req("POST", "/llm/chat/completions", json=body)

    # ---------- audio assets (素材庫) ----------

    def list_audio_assets(self) -> List[dict]:
        return self._req("GET", "/audio-assets")

    def create_audio_asset(self, name: str, url: str, duration_seconds: float = None) -> dict:
        """Register an existing /uploads url as a named audio asset; reference it
        from a WoZ line / data cell as ``asset:<returned id>``."""
        return self._req("POST", "/audio-assets",
                         json={"name": name, "url": url, "duration_seconds": duration_seconds})

    # ---------- characters ----------

    def list_characters(self) -> List[dict]:
        return self._req("GET", "/characters/")

    def create_character(self, name: str, description: str = "", is_public: bool = False,
                         robot_type: str = "kebbi", **extra) -> dict:
        return self._req("POST", "/characters/", json={
            "name": name, "description": description,
            "is_public": is_public, "robot_type": robot_type, **extra,
        })

    def export_character(self, character_id: str) -> dict:
        """The whole character as the platform's own bundle (`GET
        /characters/{id}/export`) — nodes, lines, AI settings, notes, canvas
        layout — nothing lost; `exported_at` says when it was taken."""
        return self._req("GET", f"/characters/{character_id}/export")

    def import_character(self, bundle: dict) -> dict:
        """A bundle → a NEW private character (`POST /characters/import`)."""
        return self._req("POST", "/characters/import", json=bundle)

    def replace_character(self, character_id: str, bundle: dict, force: bool = False) -> dict:
        """Put a bundle back INTO an existing character (`PUT
        /characters/{id}/bundle`), keeping its id — the robots bound to it,
        its collaborators, people and logs stay; nodes keep their ids too.
        Returns ``{character_id, name, kept, added, removed}``; what it
        replaces goes to the web's trash.

        Raises `SocialRobotError` 409 (`code` ``edited_elsewhere``) when
        someone else changed a node after the bundle's `exported_at`, or
        (``no_export_time``) when the bundle has none; `force=True` overwrites.
        """
        return self._req("PUT", f"/characters/{character_id}/bundle",
                         params={"force": "true"} if force else None, json=bundle)

    def update_character(self, character_id: str, **fields) -> dict:
        return self._req("PUT", f"/characters/{character_id}", json=fields)

    def delete_character(self, character_id: str) -> None:
        self._req("DELETE", f"/characters/{character_id}")

    # ---------- workflows ----------

    def list_workflows(self, character_id: str) -> List[dict]:
        return self._req("GET", f"/characters/{character_id}/workflows")

    def delete_workflow(self, workflow_id: str) -> None:
        self._req("DELETE", f"/workflows/{workflow_id}")

    def create_workflow(self, character_id: str, payload: dict) -> dict:
        return self._req("POST", f"/characters/{character_id}/workflows", json=payload)

    def update_workflow(self, workflow_id: str, payload: dict) -> dict:
        return self._req("PUT", f"/workflows/{workflow_id}", json=payload)

    def put_agent_config(self, workflow_id: str, payload: dict) -> dict:
        return self._req("PUT", f"/workflows/{workflow_id}/agent-config", json=payload)

    def bulk_scripts(self, workflow_id: str, lines: List[dict]) -> Any:
        return self._req("POST", f"/workflows/{workflow_id}/scripts/bulk", json={"lines": lines})

    def get_scripts(self, workflow_id: str) -> List[dict]:
        return self._req("GET", f"/workflows/{workflow_id}/scripts")

    def update_script(self, script_id: str, payload: dict) -> dict:
        return self._req("PUT", f"/scripts/{script_id}", json=payload)

    # ---------- assets ----------

    def upload_expression(self, path: str, name: str) -> dict:
        p = Path(path)
        return self._req(
            "POST", "/expressions/upload",
            data={"name": name},
            files={"file": (p.name, p.read_bytes(), "video/mp4")},
        )

    def upload_media(self, path: str) -> dict:
        """Upload an audio/video media file (mp3/m4a/wav/ogg/webm/mp4) to the
        platform's media store. Returns ``{"url", "signed_url"}`` — use
        ``signed_url`` for a client to fetch/play it (e.g. a WoZ line's
        ``Say(audio=...)`` / an injected ``{{clip_url}}``)."""
        import mimetypes
        p = Path(path)
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        # /uploads/conversation-media is mounted at the HOST root, not under
        # /api/v1 — so strip the /api/... suffix from base_url.
        root = self.base_url.rsplit("/api/", 1)[0]
        return self._req_url(
            "POST", f"{root}/uploads/conversation-media",
            files={"file": (p.name, p.read_bytes(), ctype)},
        )

    def upload_vision_model(self, path: str, name: str, model_type: str = "onnx",
                            labels: Optional[List[str]] = None) -> dict:
        """Upload a custom classifier (.onnx/.pt/.tflite/.h5/.weights) for
        on-demand inference. Returns the VisionModel dict (its ``id`` is what
        a Vision switch references). `labels` must match the model's output
        order — they're the values a Vision switch's cases compare against."""
        import json as _json
        p = Path(path)
        return self._req(
            "POST", "/vision-models/upload",
            data={"name": name, "model_type": model_type,
                  "labels": _json.dumps(labels or [])},
            files={"file": (p.name, p.read_bytes(), "application/octet-stream")},
        )

    def list_vision_models(self) -> List[dict]:
        return self._req("GET", "/vision-models/")

    def delete_vision_model(self, model_id: str) -> None:
        self._req("DELETE", f"/vision-models/{model_id}")

    # ---------- runtime / sessions ----------

    def robot_profiles(self) -> dict:
        """The platform's robot-model catalog: `{default, profiles: [...]}`.

        Each profile is the STATIC hardware description of a robot model
        (`id`, `dof`, `gui`, `motors`, `chassis`, `mic_array`, `camera`,
        `sensors`, `motion_catalog`, `expression_support`, `leds`). This is the single
        source of truth `characters.robot_type` keys into — the SDK never keeps
        its own copy, so a robot added server-side needs no SDK release.
        """
        return self._req("GET", "/config/robot-profiles")

    def device_hardware_for(self, robot_type: Optional[str]) -> Dict[str, Any]:
        """Build the `device_hardware` declaration for a robot model.

        With `robot_type=None` this describes a bare `socialrobot` script: a
        session driver with no body. With a robot_type it looks the model up in
        the server's profile catalog and declares what that model has — which
        is how a bridge (see `examples/nao_meme_bridge.py`) tells the server it
        really is a NAO and gets NAO-retargeted `motor_keyframes` in radians
        instead of Kebbi `MOTOR_*` names in degrees.

        An unknown robot_type is passed through with everything else False:
        the name still reaches the server (so `machine.mismatch` can surface
        the disagreement) but nothing is claimed on its behalf.
        """
        blank = {"robot_type": robot_type or None, "mic_array": False,
                 "chassis": False, "motors": False, "camera": False}
        if not robot_type:
            return blank
        try:
            catalog = self.robot_profiles()
        except Exception:
            # An older server has no catalog endpoint. Declaring the name alone
            # is still strictly better than declaring nothing (which the server
            # reads as "unknown" and treats as the character's design type).
            return blank
        for p in catalog.get("profiles", []):
            if p.get("id") == robot_type:
                return {
                    "robot_type": robot_type,
                    "mic_array": bool(p.get("mic_array")),
                    "chassis": bool(p.get("chassis")),
                    "motors": bool(p.get("motors")),
                    "camera": bool(p.get("camera")),
                }
        return blank

    def register_robot(self, character_id: str, device_id: str = "sdk",
                       robot_type: Optional[str] = None,
                       device_hardware: Optional[Dict[str, Any]] = None) -> dict:
        """Open a session against a character.

        `robot_type` names the robot model this process is actually driving; it
        is resolved through the server's profile catalog into the
        `device_hardware` block the registration API expects. Pass
        `device_hardware` directly to bypass the catalog entirely (a body the
        platform has no profile for).

        Declaring SOMETHING is the point: before this the SDK sent no hardware
        block at all, so the server fell back to the character's design-time
        `robot_type` and every SDK adapter was silently treated as a Kebbi —
        which is how the NAO bridge ended up being handed Kebbi `MOTOR_*` names
        in degrees for a `setAngles` API that wants NAO joints in radians.
        """
        return self._req("POST", "/robot/register", json={
            "device_id": device_id, "role": character_id, "app_version": "sdk",
            "device_hardware": device_hardware if device_hardware is not None
            else self.device_hardware_for(robot_type),
        })

    def list_sessions(self) -> List[dict]:
        return self._req("GET", "/sessions/")

    def inject(self, session_id: str, event: Optional[str] = None,
               variables: Optional[Dict[str, Any]] = None) -> dict:
        """One-shot push into a live session: variables feed VARIABLE
        switches / templates; the event fires sensor hooks and SENSOR
        switches. For high-frequency streaming use open_stream()."""
        return self._req("POST", f"/sessions/{session_id}/events",
                         json={"event": event, "variables": variables})

    def say(self, session_id: str, text: str, visual: Optional[str] = None,
            motion: Optional[str] = None) -> List[dict]:
        """Make a live robot say one line now — the live console's 即時指令
        (`POST /operator/woz/fire`). Returns one result per robot."""
        return self._req("POST", "/operator/woz/fire", json={
            "session_ids": [session_id], "text": text, "visual": visual, "motion": motion,
        })

    async def open_stream(self, session_id: str):
        """Open a persistent inbound stream (await it). Returns an
        ExternalStream async context manager for pushing many
        events/variables. See runtime.ExternalStream."""
        from .runtime import ExternalStream
        return await ExternalStream.open(self, session_id)

    async def subscribe(self, session_id: str):
        """Open a persistent OUTBOUND stream (await it). Returns a
        SubscribeStream — `async for ev in stream` yields the robot's events
        (sensor/client events + state/stage transitions) as dicts. The inverse
        of open_stream/inject. See runtime.SubscribeStream."""
        from .runtime import SubscribeStream
        return await SubscribeStream.open(self, session_id)

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
