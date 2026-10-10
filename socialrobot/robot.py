"""Turn your own hardware into a platform robot.

You write a :class:`RobotBody` — how YOUR machine speaks, stops, moves, lights
up, and (if it has them) listens and sees. :class:`Robot` does everything else,
the part every robot client otherwise rewrites (the Kebbi app and the NAO
client each did):

- **pairing by a spoken code** when it has no credentials: it says a six-digit
  code, someone types it on the web (個人資源 › 機器人 › 連上機器人), and from
  then on it acts for that person. Holding the head (`head_held`) for three
  seconds asks for a new code — that is how someone else takes it over;
- **following the web**: it runs whatever character the web gives this
  machine (個人資源 › 機器人), switches when that changes, and stands by for the
  live console when there is none. Its poll is also its heartbeat, so the web
  lists it as online;
- **acknowledging every line** (`tts_finished`) — without it the flow waits
  out a timeout after each line and the robot looks slow and deaf;
- **立刻停** (the editor's ■, the console's stop): the line being said is cut
  and never acknowledged;
- **the 機器人 card's commands** (volume, a log upload) and its status;
- **reconnecting** to the same session after a dropped socket, and
  registering again when that keeps failing.

    import os
    from socialrobot import Robot, RobotBody

    class Speaker(RobotBody):
        def say(self, text):
            engine.say(text); engine.runAndWait()   # block until it is said
        def stop(self):
            engine.stop()

    Robot(Speaker(), base_url=os.environ["SR_BASE_URL"], device_id="pi-01").run()

The wire protocol is `DEVICE_PROTOCOL.md`; this module is its reference
client. No APK, no vendor SDK, no kiosk setup.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
import websockets

from .client import SocialRobotClient, _check_base_url
from .runtime import _ws_base

logger = logging.getLogger("socialrobot.robot")

#: The version of `DEVICE_PROTOCOL.md` this client speaks. The server returns
#: its own on registration; a difference is logged, never fatal.
PROTOCOL_VERSION = 1

#: The character-less session a robot opens to be driven from the live console.
STANDBY = "__standby__"

KIND_AUDIO = 0x01   # PCM16 mono 16 kHz
KIND_IMAGE = 0x02   # one JPEG / PNG / PPM

_BUSY = ("SPEAKING", "LISTENING", "THINKING")


class RobotBody:
    """Your hardware. Override what your machine has; the rest are no-ops.

    Every method is called from a worker thread, so blocking is fine — and
    `say` SHOULD block until the line has been said: the line is acknowledged
    when it returns. `stop` is called from another thread while `say` may be
    running and must make it return promptly.
    """

    #: A robot model from the platform's catalog (`client.robot_profiles()`),
    #: e.g. ``"nao"`` — motions then arrive retargeted for it. None = a body
    #: the platform has no profile for (motions arrive as gesture names).
    robot_type: Optional[str] = None
    #: What the web's 機器人 card shows as this machine's kind.
    kind: str = "python"

    def say(self, text: str) -> None:
        """Speak `text`; return when it has been said (or cut by `stop`)."""
        print(f"🤖 {text}")

    def play_audio(self, audio: bytes) -> bool:
        """Play the platform's own voice for a line (mp3/wav bytes) and return
        True; return False to have the line spoken with `say` instead."""
        return False

    def stop(self) -> None:
        """Cut whatever is being said, played or moved — now."""

    def gesture(self, name: str) -> None:
        """A named gesture (the fallback when no keyframes came)."""

    def play_keyframes(self, keyframes: List[dict]) -> None:
        """``[{time, motors: {joint: angle}}]``, retargeted for `robot_type`."""

    def set_led(self, led: Optional[dict]) -> None:
        """The node's lights: ``{mode, color, colors, period_ms, parts}`` or None."""

    def show(self, visual: Optional[str], url: Optional[str]) -> None:
        """A screen's expression: its name and media URL (None = none)."""

    def set_volume(self, volume: int) -> None:
        """0–100, from the web's 機器人 card."""

    def head_held(self) -> bool:
        """True while someone holds the head (or your "pair again" button)."""
        return False

    def start_microphone(self, on_pcm: Callable[[bytes], None]) -> bool:
        """Start streaming the microphone: call ``on_pcm(chunk)`` from your own
        audio thread with PCM16 mono 16 kHz chunks (~100–200 ms each). Return
        True when started. The robot forwards chunks only while the platform
        says it may hear."""
        return False

    def camera_frame(self) -> Optional[bytes]:
        """One camera frame as JPEG/PNG/PPM bytes, or None. Asked only while
        the active node needs vision."""
        return None

    def announce_pairing_code(self, code: str) -> None:
        """Tell the person in front of the robot the code to type on the web."""
        self.say(f"我的配對碼是 {'、'.join(code)}。請在網頁輸入。")


def _overrides(body: RobotBody, name: str) -> bool:
    return getattr(type(body), name, None) is not getattr(RobotBody, name)


def default_token_file(device_id: str) -> Path:
    return Path(os.path.expanduser("~/.config/socialrobot")) / f"{device_id}.token"


class Robot:
    """Runs a :class:`RobotBody` against a platform.

    Credentials, in order: `api_key` (a developer `srk_` key), the device token
    this machine earned by pairing (kept in `token_file`), or none — then it
    says a pairing code and waits. `character` (an id or a name) pins one
    character; without it the robot runs whatever the web gives `device_id`.
    """

    def __init__(self, body: RobotBody, base_url: Optional[str] = None, device_id: Optional[str] = None,
                 *, api_key: Optional[str] = None, character: Optional[str] = None,
                 token_file: Optional[os.PathLike] = None, log_file: Optional[os.PathLike] = None,
                 binding_poll_s: float = 10.0, pair_poll_s: float = 2.0, head_hold_s: float = 3.0,
                 camera_fps: float = 2.0, locked_retry_s: float = 60.0):
        if not device_id:
            raise ValueError("device_id 必填：這台機器的固定名字，例如 \"pi-kitchen-01\"；網頁用它認這台機器")
        self.body = body
        self.base_url = _check_base_url(base_url)
        self.device_id = device_id
        self.character = character
        self.follow_binding = character is None
        self.token_file = Path(token_file) if token_file else default_token_file(device_id)
        self.log_file = Path(log_file) if log_file else None
        self.binding_poll_s = binding_poll_s
        self.pair_poll_s = pair_poll_s
        self.head_hold_s = head_hold_s
        self.camera_fps = camera_fps
        self.locked_retry_s = locked_retry_s

        self.token: Optional[str] = api_key or self._load_token()
        self.client: Optional[SocialRobotClient] = self._client_for(self.token)
        self.session_id: Optional[str] = None
        self.character_id: Optional[str] = None
        self.actor: Optional[str] = None
        self.server_protocol: Optional[int] = None
        self.volume: Optional[int] = None
        self.mic_open = True
        self.camera_wanted = False
        self.last_state: Optional[str] = None
        self.last_state_at = 0.0
        self.said_codes: List[str] = []

        self._ticket: Optional[str] = None
        self._role_config: Dict[str, Any] = {}
        self._ws = None
        self._cut = 0
        self._bound_actor: Optional[str] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stopping: Optional[asyncio.Event] = None
        self._connected: Optional[asyncio.Event] = None
        self._updates: Optional[asyncio.Queue] = None
        self._http = httpx.AsyncClient(timeout=20.0)

    # ── running ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Run until interrupted (Ctrl-C) or `stop()`."""
        try:
            asyncio.run(self.run_async())
        except KeyboardInterrupt:
            pass

    async def run_async(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stopping = asyncio.Event()
        self._connected = asyncio.Event()
        self._updates = asyncio.Queue()
        tasks = [self._pairing_task(), self._session_task(), self._binding_task(), self._speaker_task()]
        if _overrides(self.body, "camera_frame"):
            tasks.append(self._camera_task())
        if _overrides(self.body, "start_microphone"):
            if await asyncio.to_thread(self.body.start_microphone, self._on_pcm):
                logger.info("microphone streaming to the platform while it may hear")
        try:
            await asyncio.gather(*(asyncio.create_task(t) for t in tasks))
        finally:
            await self._http.aclose()
            if self.client is not None:
                self.client.close()

    def stop(self) -> None:
        """End `run()` from any thread."""
        if self._loop is not None and self._stopping is not None:
            self._loop.call_soon_threadsafe(self._stop_now_from_loop)

    def _stop_now_from_loop(self) -> None:
        self._stopping.set()
        ws = self._ws
        if ws is not None:
            asyncio.ensure_future(ws.close())

    @property
    def connected(self) -> bool:
        return self._connected is not None and self._connected.is_set()

    def emit(self, event: str, data: Any = None) -> None:
        """Send an event into the running flow from any thread — a button, a
        sensor, your own speech recognizer (`emit("asr_input", text)`)."""
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._send_event(event, data), self._loop)

    # ── credentials ─────────────────────────────────────────────────────────

    def _client_for(self, token: Optional[str]) -> Optional[SocialRobotClient]:
        return SocialRobotClient(self.base_url, api_key=token) if token else None

    def _load_token(self) -> Optional[str]:
        try:
            token = self.token_file.read_text().strip()
            return token or None
        except OSError:
            return None

    def _save_token(self, token: str) -> None:
        try:
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(token)
        except OSError as e:
            logger.warning(f"could not save the device token to {self.token_file}: {e}")

    async def _say(self, text: str) -> None:
        await asyncio.to_thread(self.body.say, text)

    async def _pair_once(self) -> Optional[Tuple[str, str]]:
        """Ask for a code, say it, wait for someone to type it. Returns
        (token, user_name), or None when nobody did in time."""
        r = await self._http.post(f"{self.base_url}/auth/pair/request",
                                  json={"device_label": self.device_id, "device_id": self.device_id})
        if r.status_code == 403:
            logger.warning(f"pairing refused: {r.text[:200]}")
            await self._say("這台機器人鎖定在別人的帳號，不能換人。")
            if self.token is None:
                await self._wait(self.locked_retry_s)
            return None
        r.raise_for_status()
        req = r.json()
        code = str(req["code"])
        self.said_codes.append(code)
        logger.info(f"pairing code {code} — type it on the web: 個人資源 › 機器人 › 連上機器人")
        await asyncio.to_thread(self.body.announce_pairing_code, code)
        deadline = time.monotonic() + float(req.get("expires_in") or 300)
        next_poll = time.monotonic() + self.pair_poll_s
        touched = await asyncio.to_thread(self.body.head_held)
        while time.monotonic() < deadline and not self._stopping.is_set():
            held = await asyncio.to_thread(self.body.head_held)
            if held and not touched:
                await asyncio.to_thread(self.body.announce_pairing_code, code)
            touched = held
            if time.monotonic() < next_poll:
                await asyncio.sleep(0.1)
                continue
            next_poll = time.monotonic() + self.pair_poll_s
            try:
                p = await self._http.post(f"{self.base_url}/auth/pair/poll", json={"secret": req["secret"]})
                got = p.json() if p.status_code == 200 else {}
            except (httpx.HTTPError, ValueError) as e:
                logger.warning(f"pairing poll failed: {e}")
                continue
            if got.get("status") == "approved":
                return got["token"], got.get("user_name") or ""
            if got.get("status") == "claimed":
                return None
            if got.get("status") == "expired":
                break
        if self.token is not None and not self._stopping.is_set():
            await self._say("配對碼過期了。要換人，請再摸我的頭三秒。")
        return None

    def _busy(self) -> bool:
        return (self.session_id is not None and self.last_state in _BUSY
                and time.monotonic() - self.last_state_at < 120)

    async def _apply_token(self, token: str, user_name: str) -> None:
        """Act for the person who typed the code — after the current
        interaction, never in the middle of it."""
        warned = False
        while self._busy() and not self._stopping.is_set():
            if not warned:
                await self._say("等一下，我還在跟人說話。說完就換。")
                warned = True
            await asyncio.sleep(1.0)
        self._save_token(token)
        old = self.client
        self.token, self.client = token, self._client_for(token)
        if old is not None:
            old.close()
        logger.info(f"now acting for {user_name or 'the account that typed the code'}")
        await self._say(f"好，現在聽{user_name}的。" if user_name else "好，連上了。")
        await self._end_session()

    async def _pairing_task(self) -> None:
        """No credentials: keep offering a code. Otherwise a head held for
        `head_hold_s` seconds offers one — that is how someone takes over."""
        held_since = None
        while not self._stopping.is_set():
            try:
                if self.token is None:
                    got = await self._pair_once()
                    if got:
                        await self._apply_token(*got)
                    continue
                if await asyncio.to_thread(self.body.head_held):
                    held_since = held_since or time.monotonic()
                    if time.monotonic() - held_since >= self.head_hold_s:
                        held_since = None
                        got = await self._pair_once()
                        if got:
                            await self._apply_token(*got)
                else:
                    held_since = None
            except Exception as e:  # noqa: BLE001 — keep offering
                logger.warning(f"pairing failed: {e}")
                await self._wait(5)
            await asyncio.sleep(0.2)

    # ── which character ─────────────────────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        """What the web's 機器人 card shows for this machine."""
        from . import __version__
        st: Dict[str, Any] = {"version": f"socialrobot {__version__}", "kind": self.body.kind}
        if self.volume is not None:
            st["volume"] = self.volume
        return st

    def _binding(self) -> Tuple[str, Optional[str]]:
        """(character to run, actor) per the web — STANDBY when none. The
        request is also this machine's heartbeat and status report."""
        b = self.client._req("GET", f"/robot-devices/{self.device_id}",
                             params={"status": json.dumps(self.status())}) or {}
        cid = b.get("character_id") if b.get("bound") and b.get("character_name") else None
        return (cid or STANDBY), (b.get("actor") if cid else None)

    def _fixed_character(self) -> str:
        wanted = self.character
        if "-" in wanted and len(wanted) >= 32:
            return wanted
        for c in self.client.list_characters():
            if c.get("name") == wanted:
                return c["id"]
        raise ValueError(f"找不到角色 {wanted!r}（這組憑證的帳號底下沒有）")

    def _hardware(self) -> Dict[str, Any]:
        hw = self.client.device_hardware_for(self.body.robot_type)
        if _overrides(self.body, "camera_frame"):
            hw["camera"] = True
        return hw

    def _register(self) -> bool:
        if self.follow_binding:
            cid, actor = self._binding()
        else:
            cid, actor = self._fixed_character(), None
        from . import __version__
        reg = self.client._req("POST", "/robot/register", json={
            "device_id": self.device_id, "role": cid,
            "app_version": f"socialrobot-robot/{__version__}",
            "device_hardware": self._hardware(),
            "protocol_version": PROTOCOL_VERSION,
        })
        self.character_id, self.actor = cid, actor
        self.session_id = reg["session_id"]
        self._ticket = reg.get("ws_ticket")
        self._role_config = reg.get("role_config") or {}
        self.server_protocol = reg.get("protocol_version")
        if self.server_protocol is None:
            logger.warning("this platform predates protocol versions; expect gaps")
        elif self.server_protocol != PROTOCOL_VERSION:
            logger.warning(f"the platform speaks device protocol {self.server_protocol}, this SDK "
                           f"{PROTOCOL_VERSION}: pip install -U socialrobot")
        logger.info(f"session {self.session_id} ({'standby' if cid == STANDBY else cid})")
        return True

    async def _end_session(self) -> None:
        """Drop the current session; the session task registers the next one."""
        self.session_id = None
        self.camera_wanted = False
        ws = self._ws
        if ws is not None:
            await ws.close()

    async def _binding_task(self) -> None:
        while not self._stopping.is_set():
            await self._wait(self.binding_poll_s)
            if self.client is None or self._stopping.is_set():
                continue
            try:
                cid, actor = await asyncio.to_thread(self._binding)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"binding check failed: {e}")
                continue
            if self.follow_binding and self.session_id is not None \
                    and (cid != self.character_id or actor != self.actor):
                logger.info("the web changed this robot's character; switching")
                await self._end_session()

    # ── the socket ──────────────────────────────────────────────────────────

    async def _session_task(self) -> None:
        """Reconnect to the SAME session (its ticket outlives a backend
        restart); after repeated failures, register a fresh one."""
        failures = 0
        while not self._stopping.is_set():
            if self.client is None:
                await asyncio.sleep(0.5)
                continue
            if self.session_id is None or failures >= 3:
                try:
                    await asyncio.to_thread(self._register)
                    failures = 0
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"register failed: {e}")
                    failures += 1
                    await self._wait(min(30, 2 ** min(failures, 4)))
                    continue
            url = f"{_ws_base(self.base_url)}/{self.session_id}" + (f"?ticket={self._ticket}" if self._ticket else "")
            opened = time.monotonic()
            try:
                async with websockets.connect(url, open_timeout=10, ping_interval=20, ping_timeout=10,
                                              max_size=None) as ws:
                    self._ws = ws
                    self._connected.set()
                    # Every connect: it is also what resumes a session after a deploy.
                    await self._send_event("client_ready")
                    logger.info("connected")
                    async for raw in ws:
                        if isinstance(raw, str):
                            await self._on_message(raw)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"socket: {e}")
            finally:
                self._ws = None
                self._connected.clear()
                self.camera_wanted = False
            if self._stopping.is_set():
                break
            if self.session_id is None:
                continue        # a switch: register the next one now
            failures = 0 if time.monotonic() - opened > 60 else failures + 1
            await self._wait(min(15, 1 + failures * 2))

    async def _send_event(self, event: str, data: Any = None) -> bool:
        ws = self._ws
        if ws is None or self.session_id is None:
            logger.info(f"not connected; {event} dropped")
            return False
        msg: Dict[str, Any] = {"type": "event", "event": event, "session_id": self.session_id,
                               "timestamp": int(time.time() * 1000)}
        if data is not None:
            msg["data"] = data
        try:
            await ws.send(json.dumps(msg, ensure_ascii=False))
            return True
        except Exception as e:  # noqa: BLE001
            logger.info(f"{event} not sent: {e}")
            return False

    async def _send_binary(self, kind: int, payload: bytes) -> None:
        ws = self._ws
        if ws is not None:
            try:
                await ws.send(bytes([kind]) + payload)
            except Exception:  # noqa: BLE001 — the socket task reconnects
                pass

    def _on_pcm(self, pcm: bytes) -> None:
        """From the body's audio thread: forward only while it may hear."""
        if self._loop is not None and self.connected and self.mic_open and pcm:
            asyncio.run_coroutine_threadsafe(self._send_binary(KIND_AUDIO, pcm), self._loop)

    async def _on_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        kind = msg.get("type")
        if kind in ("device_command", "device_log_request"):
            asyncio.create_task(self._command(msg))
            return
        if kind == "stop_now":
            await self._stop_now()
            return
        if kind != "state_update":
            return
        data = msg.get("data") or {}
        self.last_state = msg.get("state")
        self.last_state_at = time.monotonic()
        if "mic_open" in data:
            self.mic_open = bool(data["mic_open"])
        if data.get("needs_visual") is not None:
            self.camera_wanted = bool(data["needs_visual"])
        if "led" in data:
            asyncio.create_task(asyncio.to_thread(self.body.set_led, data.get("led")))
        if "visual" in data:
            asyncio.create_task(asyncio.to_thread(self.body.show, data.get("visual"),
                                                  self._visual_url(data.get("visual"))))
        self._updates.put_nowait(msg)

    def _visual_url(self, name: Optional[str]) -> Optional[str]:
        if not name:
            return None
        url = ((self._role_config.get("assets") or {}).get("visuals") or {}).get(name)
        if isinstance(url, str) and url.startswith("/"):
            return self.base_url.rsplit("/api/", 1)[0] + url
        return url if isinstance(url, str) else None

    async def _stop_now(self) -> None:
        """The editor's ■ and the console's 立刻停: drop the lines still
        queued, cut the one being said. A cut line is never acknowledged —
        the platform says it again on 繼續, or starts over."""
        kept, dropped = [], 0
        while not self._updates.empty():
            m = self._updates.get_nowait()
            if m.get("state") == "SPEAKING":
                dropped += 1
            else:
                kept.append(m)
        for m in kept:
            self._updates.put_nowait(m)
        self._cut += 1
        await asyncio.to_thread(self.body.stop)
        logger.info(f"stop_now: cut; {dropped} queued line(s) dropped")

    async def _command(self, msg: dict) -> None:
        """The web's 機器人 card (POST /robot-devices/{id}/command)."""
        cmd = msg.get("command")
        try:
            if msg.get("type") == "device_log_request":
                await self._upload_log()
            elif cmd == "volume":
                self.volume = int(msg.get("value"))
                await asyncio.to_thread(self.body.set_volume, self.volume)
            else:
                logger.info(f"command {cmd!r} is not something a socialrobot body does")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"command {cmd or msg.get('type')} failed: {e}")

    async def _upload_log(self) -> None:
        import gzip
        if self.log_file is None:
            tail = b"(this robot was started without log_file=)\n"
        else:
            try:
                data = self.log_file.read_bytes()
                tail = data[-512 * 1024:]
            except OSError:
                tail = f"(no log file at {self.log_file})\n".encode()
        from . import __version__
        await asyncio.to_thread(
            self.client._req, "POST", "/device-logs",
            files={"file": ("robot.log.gz", gzip.compress(tail), "application/gzip")},
            data={"device_id": self.device_id, "reason": "remote",
                  "app_build": f"socialrobot:{__version__}", "device_model": self.body.kind,
                  "os_release": "python"})
        logger.info(f"log uploaded ({len(tail)} bytes)")

    # ── speaking ────────────────────────────────────────────────────────────

    async def _speaker_task(self) -> None:
        while not self._stopping.is_set():
            try:
                msg = await asyncio.wait_for(self._updates.get(), 0.5)
            except asyncio.TimeoutError:
                continue
            if msg.get("state") != "SPEAKING":
                continue
            seq = self._cut
            try:
                spoke = await asyncio.to_thread(self._perform, msg.get("data") or {}, seq)
            except Exception as e:  # noqa: BLE001 — one bad line must not stop the robot
                logger.warning(f"line failed: {e}")
                spoke = True
            if self._cut != seq:
                continue            # cut by 立刻停: never acknowledged
            if spoke:
                await self._send_event("tts_finished")

    def _perform(self, data: dict, seq: int) -> bool:
        """One SPEAKING update, on a worker thread. True when something was
        said or played — the line to acknowledge."""
        text = data.get("text")
        lines = text if isinstance(text, list) else ([text] if text else [])
        keyframes = data.get("motor_keyframes")
        # Never both: keyframes when they came, the gesture name otherwise.
        # The gesture starts WITH the line, so the robot moves while speaking.
        if not keyframes and data.get("motion"):
            self.body.gesture(data["motion"])
        spoken = False
        if data.get("audio") and _overrides(self.body, "play_audio"):
            try:
                spoken = bool(self.body.play_audio(base64.b64decode(data["audio"])))
            except (TypeError, ValueError):
                spoken = False
        if not spoken:
            for line in lines:
                if self._cut != seq:
                    break
                if line:
                    self.body.say(line)
        if keyframes and self._cut == seq:
            self.body.play_keyframes(keyframes)
        return bool(lines) or spoken

    # ── seeing ──────────────────────────────────────────────────────────────

    async def _camera_task(self) -> None:
        while not self._stopping.is_set():
            if not (self.connected and self.camera_wanted):
                await asyncio.sleep(0.2)
                continue
            try:
                frame = await asyncio.to_thread(self.body.camera_frame)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"camera: {e}")
                frame = None
            if frame:
                await self._send_binary(KIND_IMAGE, frame)
            await asyncio.sleep(1.0 / max(0.2, self.camera_fps))

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stopping.wait(), seconds)
        except asyncio.TimeoutError:
            pass
