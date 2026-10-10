"""NAO as a FULL-DUPLEX robot on a meme session — the v3 gateway voice path.

`nao_meme_bridge.py` is half-duplex: record a window, send the text, wait, say the reply
with NAO's own synthesizer. This client speaks the platform's v3 voice protocol instead,
the one the Android app and the web simulator use:

    mic ──WebRTC audio──▶ api-gateway (VAD → ASR → LLM → TTS) ──WebRTC audio──▶ speaker
                              │ DataChannel: server.text / judgments / speech_done / interrupt_ack
                              ▼
    this client ──voice_phase / agent_judgments / tts_finished──▶ meme backend (the flow)

so the person can talk over the robot and be heard (barge-in), and the robot's voice is the
platform's. The mic is open ONLY on an AI node whose voice mode is a gateway one — the
active node decides whether the robot may hear — and it is muted, not disconnected, on
every other node. Scripted lines arrive over the robot WebSocket as before and are acked
with `tts_finished`.

Full duplex needs the robot not to hear itself. `NAO_ECHO` says how:
  aec   software echo cancellation (WebRTC's, via `pip install livekit`). The default when
        livekit is installed.
  none  the audio device cancels echo itself (a conference speakerphone, a headset).
  duck  no cancellation available: the mic closes while the robot is audible. This is NOT
        full duplex — nobody can interrupt — and it is the default only when livekit is
        missing, with a line at startup saying so.

Two voices, and which one says what matters:
  * an AI node's reply is the PLATFORM's voice, down the WebRTC track. That voice is
    Mandarin-only: a Latin-script word is spoken only if the gateway holds a recording
    of it (api-gateway `server/tts_clips_data/`, where this puzzle's names and clue
    words are) and is dropped otherwise — 「提示 11 也提到 Savickis」 was spoken as
    「提示 11 也提到」 before those recordings existed. Nothing on this side can put a
    word back; a new vocabulary is recorded over there.
  * a scripted line is said by `NAO_SCRIPT_VOICE`: `nao` = the robot's own synthesizer
    (reads Latin script; the default on a real NAO), `platform` = the audio the backend
    attached, when it attached any (the default without a robot).

    pip install socialrobot aiortc av numpy pyaudio livekit      # + qi on the NAO machine
    export SR_BASE_URL="https://<host>/ntu_social_robot/api/v1"
    export SR_GATEWAY_URL="https://<host>"        # where /v3/webrtc/connect lives
    export SR_API_KEY="srk_..."
    export NAO_CHARACTER="立陶宛語地圖解謎"
    export NAO_IP="192.168.111.181"               # omit to run without a robot (dry run)
    export NAO_SPEAKER=nao                        # nao = the robot's own loudspeaker | local
    python nao_duplex_client.py                   # --list-devices to pick NAO_MIC_INDEX

NOT verified on hardware: playing through NAO's loudspeaker (`ALAudioDevice.
sendRemoteBufferToOutput`) has only ever run against a mock. If it fails the client says
so and falls back to the local speaker. Everything else here is driven end to end by
`make e2e-nao-duplex` with a recorded utterance for a microphone.
"""
from __future__ import annotations

import asyncio
import base64
import fractions
import io
import json
import os
import sys
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import numpy as np

try:
    from socialrobot import RobotSession, SocialRobotClient
except ImportError:  # running straight from the repo
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from socialrobot import RobotSession, SocialRobotClient

RATE = 48000
FRAME = 960                      # 20 ms — the size aiortc's Opus encoder wants
FRAME_SECONDS = FRAME / RATE


# ── policy: which node may hear, and what the gateway is told ───────────────
# Pure, and deliberately the same decisions the Android app makes
# (GatewayVoicePolicy / GatewayMicPolicy / GatewayVoiceEventForwarder). A second
# implementation of a rule is a second place for it to drift, so the e2e lane runs this
# one against the live backend rather than trusting that the two agree.

MODE_WS_PIPELINE = "ws_pipeline"
MODE_NO_BARGE_IN = "no_barge_in"
DEFAULT_MAX_TOKENS = 512         # room for the JSON envelope, not just the spoken text
FALLBACK_SYSTEM_PROMPT = "你是友善的語音助手。"


def agent_config(role: dict, stage_id: Optional[str]) -> Optional[dict]:
    for stage in role.get("stages") or []:
        if stage.get("id") == stage_id:
            return stage.get("agent_config")
    return None


def uses_gateway_voice(agent: Optional[dict]) -> bool:
    """An AI node whose resolved voice mode is not the backend's own WS pipeline."""
    return agent is not None and (agent.get("voice_mode") or "full_duplex") != MODE_WS_PIPELINE


def flow_wants_gateway_voice(role: dict) -> bool:
    """Open a gateway voice channel at all? Only if the flow has a node that asked for one."""
    if (role.get("capabilities") or {}).get("audio") is False:
        return False
    stages = role.get("stages")
    if stages is None:
        return True              # an older backend that sends no stage list
    return any(uses_gateway_voice(s.get("agent_config")) for s in stages)


def stage_allows_mic(role: dict, stage_id: Optional[str]) -> bool:
    if role.get("stages") is None:
        return True
    return uses_gateway_voice(agent_config(role, stage_id))


def barge_in_for(agent: Optional[dict]) -> Optional[bool]:
    if agent is None:
        return None
    return (agent.get("voice_mode") or "full_duplex") != MODE_NO_BARGE_IN


def compose_system_prompt(role: dict, stage_id: Optional[str], agent_prompt: Optional[str]) -> str:
    """persona + the active node's task prompt. `agent_prompt` is the state_update's copy,
    already rendered against the session variables; the bundle's copy still has its {{…}}."""
    persona = ((role.get("prompts") or {}).get("system") or "").strip()
    node = (agent_prompt or (agent_config(role, stage_id) or {}).get("system_prompt") or "").strip()
    parts = [p for p in (persona, node) if p]
    return "\n\n".join(parts) if parts else FALLBACK_SYSTEM_PROMPT


def config_frame(system_prompt: Optional[str], judgments: Optional[list] = None,
                 barge_in: Optional[bool] = None, model: Optional[str] = None,
                 max_tokens: int = DEFAULT_MAX_TOKENS) -> dict:
    """The DataChannel frame that live-patches the gateway's side of the conversation."""
    frame: Dict[str, Any] = {"type": "config", "max_tokens": max_tokens}
    if system_prompt and system_prompt.strip():
        frame["system_prompt"] = system_prompt
    if model:
        frame["model"] = model
    if judgments:                # an empty list would still switch the gateway to JSON mode
        frame["output_judgments"] = judgments
    if barge_in is not None:
        frame["barge_in"] = barge_in
    return frame


def backend_events(msg: dict) -> List[Tuple[str, Optional[str]]]:
    """A gateway DataChannel message → the robot-WS events the backend needs to hear.

    The backend never sees a v3 conversation; this thin slice is how the flow learns that
    the robot is thinking, speaking, done, or has decided something.
    """
    kind = msg.get("type")
    if kind == "server.text":
        return [("voice_phase", "think" if msg.get("role") == "user" else "speak")]
    if kind in ("server.speech_done", "server.interrupt_ack"):
        # "gateway_voice" marks the ack as the gateway reply's end, not a scripted line's:
        # without it the backend would advance whatever script node happened to be active.
        return [("voice_phase", "listen"), ("tts_finished", "gateway_voice")]
    if kind == "server.judgments":
        return [("agent_judgments", json.dumps(
            {"judgments": msg.get("judgments") or {}, "should_end": bool(msg.get("should_end"))},
            ensure_ascii=False))]
    return []


class MicPolicy:
    """ONE verdict from three reasons to close the mic. Each can only subtract."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.stage_allows = False
        self.local_playback = 0      # scripted lines playing: outside the conversation
        self.reply_mute = False      # a no_barge_in reply in flight

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self.stage_allows and self.local_playback == 0 and not self.reply_mute

    def set_stage_allows(self, allows: bool) -> None:
        with self._lock:
            self.stage_allows = allows

    def begin_local_playback(self) -> None:
        with self._lock:
            self.local_playback += 1

    def end_local_playback(self) -> None:
        with self._lock:
            self.local_playback = max(0, self.local_playback - 1)

    def set_reply_mute(self, active: bool) -> None:
        with self._lock:
            self.reply_mute = active


# ── audio ───────────────────────────────────────────────────────────────────

def silence() -> np.ndarray:
    return np.zeros(FRAME, dtype=np.int16)


def decode_audio(data: bytes) -> List[np.ndarray]:
    """Any audio container → 20 ms mono frames at 48 kHz."""
    import av
    frames: List[np.ndarray] = []
    resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE, frame_size=FRAME)
    with av.open(io.BytesIO(data)) as container:
        for frame in container.decode(audio=0):
            frames.extend(f.to_ndarray().reshape(-1).copy() for f in resampler.resample(frame))
    tail = [f.to_ndarray().reshape(-1) for f in resampler.resample(None)]
    for f in tail:                                   # the last partial frame, padded
        frames.append(np.pad(f, (0, FRAME - len(f))) if len(f) < FRAME else f.copy())
    return frames


class EchoCanceller:
    """WebRTC's echo canceller (livekit's build of it). It is told what is about to come
    out of the speaker, and subtracts that from what the microphone hears."""

    def __init__(self, delay_ms: int = 60):
        from livekit import rtc
        self._rtc = rtc
        self._apm = rtc.AudioProcessingModule(echo_cancellation=True, noise_suppression=True,
                                              high_pass_filter=True, auto_gain_control=False)
        self._apm.set_stream_delay_ms(delay_ms)
        self._lock = threading.Lock()   # render and capture arrive on different threads

    def _process(self, frame: np.ndarray, reverse: bool) -> np.ndarray:
        out = np.empty_like(frame)
        half = FRAME // 2                # the module takes exactly 10 ms at a time
        for i in (0, half):
            chunk = self._rtc.AudioFrame(frame[i:i + half].tobytes(), RATE, 1, half)
            with self._lock:
                (self._apm.process_reverse_stream if reverse else self._apm.process_stream)(chunk)
            out[i:i + half] = np.frombuffer(bytes(chunk.data), dtype=np.int16)
        return out

    def render(self, frame: np.ndarray) -> None:
        self._process(frame, reverse=True)

    def capture(self, frame: np.ndarray) -> np.ndarray:
        return self._process(frame, reverse=False)


class NullOutput:
    """No loudspeaker: keeps what it was given. For a dry run and for the tests."""

    def __init__(self, keep: bool = True):
        self.frames: List[np.ndarray] = [] if keep else None   # type: ignore[assignment]

    def write(self, frame: np.ndarray) -> None:
        if self.frames is not None:
            self.frames.append(frame)

    def close(self) -> None:
        pass


class PyAudioOutput:
    def __init__(self, device_index: Optional[int] = None):
        import pyaudio
        self._pa = pyaudio.PyAudio()
        self._stream = self._pa.open(format=pyaudio.paInt16, channels=1, rate=RATE, output=True,
                                     frames_per_buffer=FRAME, output_device_index=device_index)

    def write(self, frame: np.ndarray) -> None:
        self._stream.write(frame.tobytes())          # blocks for about the frame's length

    def close(self) -> None:
        self._stream.stop_stream(); self._stream.close(); self._pa.terminate()


class NaoSpeakerOutput:
    """The robot's own loudspeaker: PCM handed to NAOqi, 16-bit stereo interleaved."""

    def __init__(self, qi_session):
        self._device = qi_session.service("ALAudioDevice")
        self._device.setParameter("outputSampleRate", RATE)

    def write(self, frame: np.ndarray) -> None:
        self._device.sendRemoteBufferToOutput(len(frame), np.repeat(frame, 2).tobytes())

    def close(self) -> None:
        pass


class PyAudioInput:
    def __init__(self, device_index: Optional[int] = None):
        import pyaudio
        self._pa = pyaudio.PyAudio()
        self._stream = self._pa.open(format=pyaudio.paInt16, channels=1, rate=RATE, input=True,
                                     frames_per_buffer=FRAME, input_device_index=device_index)

    def read(self) -> np.ndarray:
        return np.frombuffer(self._stream.read(FRAME, exception_on_overflow=False), dtype=np.int16)

    def close(self) -> None:
        self._stream.stop_stream(); self._stream.close(); self._pa.terminate()


class ScriptedInput:
    """A microphone that says what it is told to: silence, until `say()` queues an
    utterance. Paced at real time, like a device. For the dry run and the tests."""

    def __init__(self) -> None:
        self._pending: Deque[np.ndarray] = deque()
        self._next = time.monotonic()

    def say(self, frames: List[np.ndarray]) -> None:
        self._pending.extend(frames)

    @property
    def speaking(self) -> bool:
        return bool(self._pending)

    def read(self) -> np.ndarray:
        self._next = max(self._next + FRAME_SECONDS, time.monotonic() - 0.2)
        delay = self._next - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        return self._pending.popleft() if self._pending else silence()

    def close(self) -> None:
        pass


class Speaker:
    """Mixes the two things the robot can be saying — the gateway's reply and a scripted
    line — and feeds the device one frame every 20 ms from its own thread."""

    MAX_GATEWAY_BACKLOG = 25         # half a second: beyond it, old audio is dropped, not delayed
    AUDIBLE_PEAK = 300
    TALKING_HOLD_SECONDS = 0.35      # room tail: the echo outlasts the last loud frame

    def __init__(self, device, echo: Optional[EchoCanceller] = None,
                 on_device_error: Optional[Callable[[Exception], Any]] = None):
        self.device, self.echo, self._on_error = device, echo, on_device_error
        self._gateway: Deque[np.ndarray] = deque()
        self._local: Deque[Any] = deque()            # frames, or a callable fired when reached
        self._last_audible = 0.0
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="speaker")
        self._thread.start()

    # ── producers ──
    def push_gateway(self, frame: np.ndarray) -> None:
        self._gateway.append(frame)
        while len(self._gateway) > self.MAX_GATEWAY_BACKLOG:
            self._gateway.popleft()

    def clear_gateway(self) -> None:
        """Barge-in: the reply stops NOW, not when the buffered half-second has played."""
        self._gateway.clear()

    async def play_local(self, frames: List[np.ndarray]) -> None:
        """Play a scripted line and return when its last frame has left for the device."""
        loop = asyncio.get_running_loop()
        done = loop.create_future()
        self._local.extend(frames)
        self._local.append(lambda: loop.call_soon_threadsafe(lambda: done.done() or done.set_result(None)))
        try:
            await done
        except asyncio.CancelledError:
            self._local.clear()
            raise

    @property
    def talking(self) -> bool:
        return time.monotonic() - self._last_audible < self.TALKING_HOLD_SECONDS

    # ── the device thread ──
    def _next_local(self) -> Optional[np.ndarray]:
        while self._local:
            item = self._local.popleft()
            if callable(item):
                item()
                continue
            return item
        return None

    def _run(self) -> None:
        next_tick = time.monotonic()
        while self._running:
            gateway = self._gateway.popleft() if self._gateway else None
            local = self._next_local()
            if gateway is None and local is None:
                frame = silence()
            elif gateway is None or local is None:
                frame = gateway if local is None else local
            else:
                frame = np.clip(gateway.astype(np.int32) + local.astype(np.int32), -32768, 32767).astype(np.int16)
            if int(np.abs(frame).max()) > self.AUDIBLE_PEAK:
                self._last_audible = time.monotonic()
            if self.echo is not None:
                self.echo.render(frame)
            try:
                self.device.write(frame)
            except Exception as e:               # a device that stops working must not kill the robot
                if self._on_error:
                    replacement = self._on_error(e)
                    if replacement is not None:
                        self.device = replacement
            # A device that does not block (NAOqi queues and returns) still gets real-time pacing.
            next_tick = max(next_tick + FRAME_SECONDS, time.monotonic() - 0.1)
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)

    def close(self) -> None:
        self._running = False
        self._thread.join(timeout=1)
        self.device.close()


class Microphone:
    """Reads the input device on its own thread and keeps the last few frames."""

    def __init__(self, device, echo: Optional[EchoCanceller] = None):
        self.device, self.echo = device, echo
        self.frames: Deque[np.ndarray] = deque(maxlen=10)
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="microphone")
        self._thread.start()

    def _run(self) -> None:
        while self._running:
            try:
                frame = self.device.read()
            except Exception as e:
                print(f"[mic] read failed: {e}")
                time.sleep(0.2)
                continue
            self.frames.append(self.echo.capture(frame) if self.echo is not None else frame)

    def close(self) -> None:
        self._running = False
        self._thread.join(timeout=1)
        self.device.close()


def make_mic_track(microphone: Microphone, is_open: Callable[[], bool]):
    """The WebRTC track the gateway listens to. Closed means SILENCE on a live track — the
    connection stays up, so opening the mic again costs nothing."""
    import av
    from aiortc import MediaStreamTrack

    class MicTrack(MediaStreamTrack):
        kind = "audio"

        def __init__(self) -> None:
            super().__init__()
            self._pts = 0
            self._next: Optional[float] = None
            self.sent_open = 0           # frames that carried the microphone, for the tests

        async def recv(self):
            loop = asyncio.get_event_loop()
            if self._next is None:
                self._next = loop.time()
            delay = self._next - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next = max(self._next + FRAME_SECONDS, loop.time())

            mono = microphone.frames.popleft() if microphone.frames else silence()
            if is_open():
                self.sent_open += 1
            else:
                mono = silence()
            frame = av.AudioFrame.from_ndarray(np.repeat(mono, 2).reshape(1, -1), format="s16", layout="stereo")
            frame.sample_rate = RATE
            frame.pts = self._pts
            frame.time_base = fractions.Fraction(1, RATE)
            self._pts += FRAME
            return frame

    return MicTrack()


# ── the gateway voice channel ───────────────────────────────────────────────

class GatewayVoice:
    """One WebRTC peer to the api-gateway: our mic up, its reply down, a DataChannel beside."""

    def __init__(self, gateway_url: str, token: str, mic_track, on_audio: Callable[[np.ndarray], None],
                 on_message: Callable[[dict], None], ice_servers: Optional[list] = None,
                 on_lost: Optional[Callable[[], None]] = None):
        self.url, self.token = gateway_url.rstrip("/"), token
        self.mic_track, self.on_audio, self.on_message = mic_track, on_audio, on_message
        self.ice_servers = ice_servers or []
        self.on_lost = on_lost
        self.pc = None
        self.channel = None
        self.sent_frames: List[dict] = []    # every config frame, in order — what the gateway was told
        self._pending: Optional[dict] = None

    async def connect(self, frame: dict, timeout: float = 90.0) -> None:
        """One attempt. `timeout` is generous on purpose: a gateway that has just started
        fetches its VAD model on the first connect, which has been seen to take 35 s."""
        import av
        import httpx
        from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

        servers = [RTCIceServer(urls=s["urls"], username=s.get("username"), credential=s.get("credential"))
                   for s in self.ice_servers if s.get("urls")]
        pc = self.pc = RTCPeerConnection(RTCConfiguration(iceServers=servers) if servers else None)
        self.channel = self.pc.createDataChannel("chat")
        if self._pending is None:            # a node entered while we were down has the newer prompt
            self._pending = frame

        @pc.on("connectionstatechange")
        async def _state():
            # `pc is self.pc`: a connection we closed ourselves is not a lost one.
            if pc is self.pc and pc.connectionState in ("failed", "closed") and self.on_lost:
                self.on_lost()

        @self.channel.on("open")
        def _open():
            if self._pending is not None:
                self._send(self._pending)
                self._pending = None

        @self.channel.on("message")
        def _message(raw):
            try:
                self.on_message(json.loads(raw))
            except Exception as e:
                print(f"[gateway] bad DataChannel message: {e}")

        @self.pc.on("track")
        def _track(track):
            if track.kind != "audio":
                return
            resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE, frame_size=FRAME)

            async def pump():
                try:
                    while True:
                        for out in resampler.resample(await track.recv()):
                            self.on_audio(out.to_ndarray().reshape(-1).copy())
                except Exception:
                    pass                 # the track ended with the connection
            asyncio.ensure_future(pump())

        self.pc.addTrack(self.mic_track)
        await self.pc.setLocalDescription(await self.pc.createOffer())
        body = {"sdp": self.pc.localDescription.sdp, "system_prompt": frame.get("system_prompt")}
        if frame.get("model"):
            body["model"] = frame["model"]
        async with httpx.AsyncClient(timeout=timeout) as http:
            resp = await http.post(f"{self.url}/v3/webrtc/connect", json=body,
                                   headers={"Authorization": f"Bearer {self.token}"})
        if resp.status_code != 200:
            raise RuntimeError(f"gateway refused the voice session: HTTP {resp.status_code} {resp.text[:200]}")
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=resp.json()["sdp"], type="answer"))

    def _send(self, frame: dict) -> None:
        self.sent_frames.append(frame)
        self.channel.send(json.dumps(frame, ensure_ascii=False))

    def configure(self, frame: dict) -> None:
        """Live-patch the conversation (a new node, a re-rendered prompt). No reconnect."""
        if self.channel is not None and self.channel.readyState == "open":
            self._send(frame)
        else:
            self._pending = frame

    async def close(self) -> None:
        pc, self.pc, self.channel = self.pc, None, None
        if pc is not None:
            await pc.close()


# ── the robot ───────────────────────────────────────────────────────────────

class DuplexClient:
    """Ties the three conversations together: the flow (robot WS), the voice (gateway),
    and the body (NAO)."""

    def __init__(self, session: RobotSession, body, speaker: Speaker, microphone: Microphone,
                 gateway_url: str, token: str, *, echo_mode: str = "none", script_voice: str = "platform"):
        self.session, self.body, self.speaker, self.microphone = session, body, speaker, microphone
        self.role: dict = session.registration.get("role_config") or {}
        self.echo_mode, self.script_voice = echo_mode, script_voice
        self.policy = MicPolicy()
        self.stage_id: Optional[str] = None
        self.agent_prompt: Optional[str] = None
        self.transcript: List[Tuple[str, str]] = []
        self.gateway_events: Deque[str] = deque(maxlen=200)   # message types, newest last
        self.mic_track = make_mic_track(microphone, self.mic_open)
        self.voice: Optional[GatewayVoice] = None
        if flow_wants_gateway_voice(self.role):
            self.voice = GatewayVoice(gateway_url, token, self.mic_track, self._on_gateway_audio,
                                      self._on_gateway_message,
                                      session.registration.get("webrtc_config"), self._voice_lost)
        self._line: Optional[asyncio.Task] = None
        self._tasks: set = set()
        self._voice_task: Optional[asyncio.Task] = None
        self._closed = False

    def mic_open(self) -> bool:
        if not self.policy.enabled:
            return False
        # duck: with no echo cancellation the only safe mic is one that is shut while the
        # robot is audible. It is what makes this mode NOT full duplex.
        return not (self.echo_mode == "duck" and self.speaker.talking)

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_gateway_audio(self, frame: np.ndarray) -> None:
        # The gateway's voice belongs to the node that asked for it. A reply still draining
        # when the flow moves on (a verdict pushed mid-sentence) would talk over that
        # node's own line, so off an AI node it is dropped, not played.
        if self.policy.stage_allows:
            self.speaker.push_gateway(frame)

    # ── gateway → backend ──
    def _frame(self) -> dict:
        agent = agent_config(self.role, self.stage_id)
        barge_in = barge_in_for(agent)
        if self.echo_mode == "duck" and barge_in:
            barge_in = False                     # the gateway must not expect to be interrupted
        return config_frame(compose_system_prompt(self.role, self.stage_id, self.agent_prompt),
                            (agent or {}).get("output_judgments") or None, barge_in)

    def _on_gateway_message(self, msg: dict) -> None:
        kind = msg.get("type")
        self.gateway_events.append(kind or "")
        if kind == "server.text":
            self.transcript.append((msg.get("role") or "assistant", msg.get("text") or ""))
            print(f"{'🧑' if msg.get('role') == 'user' else '🤖'} {msg.get('text')}")
            if msg.get("role") == "user":
                self._spawn(asyncio.to_thread(think_gesture, self.body))
            elif barge_in_for(agent_config(self.role, self.stage_id)) is False:
                self.policy.set_reply_mute(True)      # a no_barge_in reply: nobody cuts it off
        elif kind == "server.interrupt_ack":
            self.speaker.clear_gateway()
            self.policy.set_reply_mute(False)
        elif kind == "server.speech_done":
            self.policy.set_reply_mute(False)
        elif kind == "server.error":
            print(f"[gateway] {msg.get('error')}")
        for event, data in backend_events(msg):
            self._spawn(self.session.send_event(event, data))

    # ── backend → robot ──
    def _on_stage(self, stage_id: Optional[str], agent_prompt: Optional[str]) -> None:
        if stage_id == self.stage_id and agent_prompt == self.agent_prompt:
            return
        self.stage_id, self.agent_prompt = stage_id, agent_prompt
        self.policy.set_reply_mute(False)             # the old node's reply no longer governs the mic
        allows = stage_allows_mic(self.role, stage_id)
        self.policy.set_stage_allows(allows)
        if not allows:
            self.speaker.clear_gateway()
        if self.voice is not None:
            self.voice.configure(self._frame())

    async def _speak_line(self, data: dict) -> None:
        """One scripted chunk: say it, then tell the flow it has been said."""
        text = data.get("text")
        text = "".join(text) if isinstance(text, list) else (text or "")
        self.policy.begin_local_playback()
        try:
            audio = data.get("audio") if self.script_voice == "platform" else None
            if audio:
                await self.speaker.play_local(decode_audio(base64.b64decode(audio)))
            else:
                await asyncio.to_thread(self.body.say, text)      # NAO's own synthesizer
        finally:
            self.policy.end_local_playback()
        await self.session.send_event("tts_finished")

    async def on_state_update(self, ev: dict) -> None:
        data = ev.get("data") or {}
        if "stage_id" in data:
            self._on_stage(data.get("stage_id"), data.get("agent_prompt"))
        # A SPEAKING with no words is the gateway's own phase, echoed back by the backend.
        if ev.get("state") == "SPEAKING" and data.get("text"):
            if self._line is not None and not self._line.done() and data.get("audio_mode") != "append":
                self._line.cancel()                   # a new utterance replaces the one in flight
                stop = getattr(getattr(self.body, "tts", None), "stopAll", None)
                if stop:
                    stop()
            previous = self._line

            async def after_previous():
                if previous is not None:
                    await asyncio.gather(previous, return_exceptions=True)
                await self._speak_line(data)
            self._line = asyncio.ensure_future(after_previous())
        keyframes = data.get("motor_keyframes")
        if keyframes:
            self._spawn(asyncio.to_thread(self.body.play_keyframes, keyframes))
        elif data.get("motion"):
            self._spawn(asyncio.to_thread(self.body.gesture, data["motion"]))

    # ── keeping the voice channel up ──
    VOICE_RETRY_SECONDS = 2.0
    VOICE_RETRY_MAX_SECONDS = 30.0

    async def _keep_voice(self) -> None:
        """Bring the voice channel up — and back up. A robot in a study runs for hours; a
        gateway that restarts under it must cost a sentence, not the session. Until the
        channel is there the scripted nodes work as usual and the AI node is deaf, which
        is said every time rather than once."""
        delay = self.VOICE_RETRY_SECONDS
        while not self._closed:
            try:
                await self.voice.connect(self._frame())
                print("Gateway voice connected (mic opens on AI nodes).")
                return
            except Exception as e:
                await self.voice.close()
                print(f"⚠ 語音閘道連不上（{e or type(e).__name__}）——{delay:.0f} 秒後再試。"
                      "這段時間腳本台詞照常，AI 節點聽不到人。")
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.VOICE_RETRY_MAX_SECONDS)

    def _start_voice(self) -> None:
        if self._closed or (self._voice_task is not None and not self._voice_task.done()):
            return
        self._voice_task = asyncio.ensure_future(self._keep_voice())

    def _voice_lost(self) -> None:
        if self._closed:
            return
        print("⚠ 語音閘道的連線斷了，重新連線中…")
        self.speaker.clear_gateway()
        self.policy.set_reply_mute(False)

        async def again():
            await self.voice.close()
            self._start_voice()
        self._spawn(again())

    async def run(self) -> None:
        if self.voice is not None:
            self._start_voice()      # beside the flow, not before it: lines are said meanwhile
        else:
            print("This flow has no gateway-voiced AI node: scripted lines only, the mic stays closed.")
        async for ev in self.session.updates():
            await self.on_state_update(ev)

    async def close(self) -> None:
        self._closed = True
        for task in list(self._tasks) + [t for t in (self._line, self._voice_task) if t]:
            task.cancel()
        if self.voice is not None:
            await self.voice.close()
        self.microphone.close()
        self.speaker.close()


def think_gesture(body) -> None:
    """Hand to chin while the model thinks — the gesture the original ECRobot client made."""
    motion = getattr(body, "motion", None)
    if motion is None:
        return
    try:
        motion.setAngles(["RShoulderPitch", "RElbowRoll", "RElbowYaw"], [0.5, 1.2, 1.5], 0.3)
        time.sleep(1.0)
        motion.setAngles(["RShoulderPitch", "RElbowRoll", "RElbowYaw"], [1.5, 0.0, 0.0], 0.3)
    except Exception as e:
        print(f"[nao] think gesture failed: {e}")


# ── wiring ──────────────────────────────────────────────────────────────────

def default_gateway_url(base_url: str) -> str:
    """The gateway is a sibling service on the same host: https://host/prefix/api/v1 → https://host"""
    parts = urlsplit(base_url)
    return f"{parts.scheme}://{parts.netloc}"


def choose_echo_mode(requested: Optional[str]) -> Tuple[str, Optional[EchoCanceller]]:
    delay = int(os.getenv("NAO_AEC_DELAY_MS", "60"))
    if requested in (None, "", "aec"):
        try:
            return "aec", EchoCanceller(delay)
        except ImportError:
            if requested == "aec":
                sys.exit("NAO_ECHO=aec needs `pip install livekit`.")
            print("⚠ livekit 沒有安裝，無法做回音消除：改用 duck（機器人出聲時關麥克風）。\n"
                  "  這樣就不是全雙工——對方不能打斷機器人。要全雙工：pip install livekit，\n"
                  "  或用本身有回音消除的喇叭麥克風並設 NAO_ECHO=none。")
            return "duck", None
    if requested not in ("none", "duck"):
        sys.exit(f"NAO_ECHO={requested!r}: expected aec, none or duck.")
    return requested, None


def list_devices() -> None:
    import pyaudio
    pa = pyaudio.PyAudio()
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        kinds = [k for k, n in (("輸入", info.get("maxInputChannels")), ("輸出", info.get("maxOutputChannels"))) if n]
        print(f"[{i}] {info.get('name')}  ({'/'.join(kinds)})")
    pa.terminate()


def _index(name: str) -> Optional[int]:
    value = os.getenv(name)
    return int(value) if value not in (None, "") else None


async def main() -> int:
    if "--list-devices" in sys.argv:
        list_devices()
        return 0
    base = os.getenv("SR_BASE_URL")
    key = os.getenv("SR_API_KEY")
    character = os.getenv("NAO_CHARACTER")
    if not base or not key or not character:
        sys.exit("Set SR_BASE_URL (…/api/v1), SR_API_KEY (srk_…) and NAO_CHARACTER (id or name).")
    gateway_url = os.getenv("SR_GATEWAY_URL") or default_gateway_url(base)

    import nao_meme_bridge as nao                     # the body: posture, gestures, keyframes
    body = nao.make_nao_body()
    echo_mode, echo = choose_echo_mode(os.getenv("NAO_ECHO"))

    def local_output():
        try:
            return PyAudioOutput(_index("NAO_SPEAKER_INDEX"))
        except Exception as e:
            print(f"⚠ 本機喇叭也開不起來（{e}）——這一場沒有聲音輸出。")
            return NullOutput(keep=False)

    def speaker_failed(e: Exception):
        print(f"⚠ 喇叭輸出失敗（{e}）——改用本機喇叭。")
        return local_output()

    qi_session = getattr(body, "session", None)
    want_nao_speaker = os.getenv("NAO_SPEAKER", "nao" if qi_session is not None else "local") == "nao"
    if want_nao_speaker and qi_session is not None:
        try:
            output = NaoSpeakerOutput(qi_session)
        except Exception as e:
            output = speaker_failed(e)
    else:
        output = local_output()

    script_voice = os.getenv("NAO_SCRIPT_VOICE") or ("nao" if body.kind == "nao" else "platform")
    if script_voice not in ("nao", "platform"):
        sys.exit(f"NAO_SCRIPT_VOICE={script_voice!r}: expected nao or platform.")

    client = SocialRobotClient(base_url=base, api_key=key)
    speaker = Speaker(output, echo, on_device_error=speaker_failed)
    microphone = Microphone(PyAudioInput(_index("NAO_MIC_INDEX")), echo)
    async with await RobotSession.start(client, character, device_id="nao-duplex", robot_type="nao") as session:
        robot = DuplexClient(session, body, speaker, microphone, gateway_url, client.token,
                             echo_mode=echo_mode, script_voice=script_voice)
        print(f"NAO duplex client live: session {session.session_id} · body={body.kind} · "
              f"echo={echo_mode} · scripted lines={script_voice} · gateway={gateway_url}. Ctrl+C to stop.")
        try:
            await robot.run()
        finally:
            await robot.close()
    client.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nStopped.")
