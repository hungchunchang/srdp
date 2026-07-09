"""Bridge: meme is the brain, the NAO robot is the body (full-duplex).

You author a NAO character in meme's web editor; this bridge registers a meme
`RobotSession` and renders each `state_update` on the NAO via the qi SDK:
  - down: SPEAKING `text` → `ALTextToSpeech.say`; `motor_keyframes` (already
          retargeted server-side to NAO's native joints/radians, see
          topologies/nao.json) → per-joint `ALMotion.setAngles`, else
          `motion` → a named NAO gesture fallback.
  - up:   mic/console input → ASR → `session.send_event("asr_input", text)`,
          which drives meme's AI node; its reply comes back as SPEAKING.

The ecrobot server is bypassed — meme is the sole brain. Runs on the NAO
machine (the `nao` env: qi + `pip install srdp pyaudio`). Off-NAO it
falls back to a dry-run body that just prints, so you can test the meme side
anywhere.

    export SR_BASE_URL="http://<host>:6868/api/v1"
    export SR_API_KEY="srk_..."             # web: 個人設定 → API Key
    export NAO_CHARACTER="NAO 教學助理"      # character id or name (authored in meme)
    export NAO_IP="127.0.0.1"               # used only when qi is available
    # input: default reads lines from the console (type to simulate speech).
    #   on the NAO, set SR_INPUT=mic + SR_ASR_URL=<your ASR endpoint> + pip install pyaudio
    python nao_meme_bridge.py

NOTE on flow control: meme's WoZ advances only when the client acks each TTS
chunk, so the bridge sends `tts_finished` after the NAO finishes each line —
exactly like the app/simulator. Without it the script would speak the first
line and stall.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

try:
    from socialrobot import RobotSession, SocialRobotClient
except ImportError:  # running straight from the repo
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from socialrobot import RobotSession, SocialRobotClient


# fractionMaxSpeed for ALMotion.setAngles; matches topologies/nao.json's
# speed_default (the same value the backend retargeter assumes for NAO).
NAO_DEFAULT_SPEED = 0.2


# ── NAO body (qi) ───────────────────────────────────────────────────────────

class NaoBody:
    """Real NAO via the qi SDK — mirrors ecrobot nao/nao_client.connect_nao /
    _speak_single. `say` blocks until the utterance finishes (so the caller can
    then ack tts_finished)."""
    kind = "nao"

    def __init__(self, ip: str):
        import qi
        self.session = qi.Session()
        self.session.connect(f"tcp://{ip}:9559")
        self.tts = self.session.service("ALTextToSpeech")
        self.motion = self.session.service("ALMotion")
        self.posture = self.session.service("ALRobotPosture")
        self.motion.wakeUp()
        self.posture.goToPosture("Stand", 0.5)

    def say(self, text: str) -> None:
        self.tts.say(text)  # blocking

    def gesture(self, name: str) -> None:
        # Map known meme motion names to NAO gestures here; unknown → no-op.
        # (Kept minimal; extend per your character's motion vocabulary.)
        try:
            if name in ("nod", "yes"):
                self.motion.setAngles("HeadPitch", 0.3, 0.2)
                self.motion.setAngles("HeadPitch", 0.0, 0.2)
        except Exception as e:
            print(f"[nao] gesture {name!r} failed: {e}")

    def play_keyframes(self, keyframes: list[dict]) -> None:
        """Play server-retargeted NAO-native motor keyframes — already
        converted to ALMotion joint names + radians by
        motion_mapping/retarget_stage3.py against topologies/nao.json, so no
        unit conversion happens here (unlike Kebbi's degrees). Each keyframe
        is applied via `setAngles` at its `time` offset from motion start;
        only the motors it lists move (partial keyframes), mirroring the
        app's KebbiMotionPlayer. Blocks for the motion's total duration, like
        `say`, so the caller can ack tts_finished once playback is done."""
        t_prev = 0.0
        for kf in keyframes:
            try:
                t = float(kf.get("time", 0.0))
                motors = kf.get("motors") or {}
                names = list(motors.keys())
                angles = [float(v) for v in motors.values()]
            except (TypeError, ValueError) as e:
                print(f"[nao] keyframe malformed, skipping: {e}")
                continue
            wait = t - t_prev
            if wait > 0:
                time.sleep(wait)
            t_prev = t
            if not motors:
                continue
            try:
                self.motion.setAngles(names, angles, NAO_DEFAULT_SPEED)
            except Exception as e:
                print(f"[nao] keyframe @ {t}s failed: {e}")


class DryRunBody:
    """Used when qi isn't available (any dev machine): print what NAO would do.
    Lets you test the whole meme side without a robot."""
    kind = "dry-run"

    def say(self, text: str) -> None:
        print(f"🤖 NAO would say: {text}")

    def gesture(self, name: str) -> None:
        print(f"🤖 NAO would gesture: {name}")

    def play_keyframes(self, keyframes: list[dict]) -> None:
        print(f"🤖 NAO would play {len(keyframes)} motor keyframe(s)")


def make_nao_body():
    ip = os.getenv("NAO_IP")
    try:
        import qi  # noqa: F401
        if ip:
            return NaoBody(ip)
    except Exception as e:
        print(f"[bridge] qi/NAO unavailable ({e}); using dry-run body.")
    return DryRunBody()


# ── down: meme state_update → NAO ───────────────────────────────────────────

async def drive_down(session: RobotSession, nao) -> None:
    async for ev in session.updates():
        if ev.get("state") != "SPEAKING":
            continue
        data = ev.get("data") or {}
        text = data.get("text")
        lines = text if isinstance(text, list) else ([text] if text else [])
        for line in lines:
            if line:
                nao.say(line)
        # Prefer precise per-joint keyframes when the backend retargeted this
        # motion to NAO's native motors; fall back to the named gesture.
        # Never both (mirrors the app's RobotViewModel.kt).
        keyframes = data.get("motor_keyframes")
        if keyframes:
            nao.play_keyframes(keyframes)
        elif data.get("motion"):
            nao.gesture(data["motion"])
        # Ack the chunk so meme's WoZ/AI advances (mirrors app/simulator).
        if lines:
            await session.send_event("tts_finished")


# ── up: input → ASR → meme asr_input ────────────────────────────────────────

async def drive_up_console(session: RobotSession) -> None:
    """Testable input path: type a line → asr_input. Replace with mic+ASR on NAO."""
    loop = asyncio.get_event_loop()
    print("Type what the visitor says (drives meme's AI node), Ctrl+D to stop:")
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        text = line.strip()
        if text:
            await session.send_event("asr_input", text)


async def drive_up_mic(session: RobotSession) -> None:
    """NAO/computer mic → ASR → asr_input. ASR is pluggable: posts captured
    audio to SR_ASR_URL and expects {"text": "..."}. Adjust to your ASR's
    contract. Needs `pip install pyaudio` and SR_ASR_URL set."""
    import io
    import wave

    import httpx
    import pyaudio

    asr_url = os.environ["SR_ASR_URL"]
    rate, chunk, seconds = 16000, 1024, 4  # capture in fixed windows; simplest VAD-free loop
    pa = pyaudio.PyAudio()
    stream = pa.open(format=pyaudio.paInt16, channels=1, rate=rate, input=True,
                     frames_per_buffer=chunk)
    print(f"Mic → {asr_url} → asr_input. Ctrl+C to stop.")
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            while True:
                frames = [await asyncio.to_thread(stream.read, chunk, exception_on_overflow=False)
                          for _ in range(int(rate / chunk * seconds))]
                buf = io.BytesIO()
                with wave.open(buf, "wb") as wf:
                    wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(rate)
                    wf.writeframes(b"".join(frames))
                resp = await http.post(asr_url, files={"file": ("a.wav", buf.getvalue(), "audio/wav")})
                text = (resp.json().get("text") or "").strip() if resp.is_success else ""
                if text:
                    print(f"🎤 {text}")
                    await session.send_event("asr_input", text)
    finally:
        stream.stop_stream(); stream.close(); pa.terminate()


async def main() -> int:
    base = os.getenv("SR_BASE_URL", "http://localhost:6868/api/v1")
    key = os.getenv("SR_API_KEY")
    character = os.getenv("NAO_CHARACTER")
    if not key or not character:
        sys.exit("Set SR_API_KEY (srk_…) and NAO_CHARACTER (id or name).")

    client = SocialRobotClient(base_url=base, api_key=key)
    nao = make_nao_body()
    up = drive_up_mic if os.getenv("SR_INPUT") == "mic" else drive_up_console

    async with await RobotSession.start(client, character, device_id="nao-bridge") as session:
        print(f"Bridge live: meme session {session.session_id} → NAO ({nao.kind}). Ctrl+C to stop.")
        try:
            await asyncio.gather(drive_down(session, nao), up(session))
        except KeyboardInterrupt:
            pass
    client.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nStopped.")
