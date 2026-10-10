# -*- coding: utf-8 -*-
"""meme on the NAO itself — Python 2.7, no companion computer.

captain 2026-10-06: 「不然把nao的sdk改成2.7版，就不用另一台電腦控他」. This runs
ON the robot with NAOqi's own Python 2.7 and its built-in `naoqi` module; every
other dependency is vendored next to it (vendor/), so nothing is pip-installed
on the robot.

  down  state_update SPEAKING → say the line (the platform's own voice when the
        update carries audio, else NAO's ALTextToSpeech) → ack `tts_finished`
        (meme's script/AI nodes advance only on that ack); `motor_keyframes`
        (already retargeted server-side to NAO joints in radians) →
        ALMotion.setAngles per keyframe; else a named `motion` → a small
        gesture fallback. Never both.
  mic   CONTINUOUS: ALAudioDevice hands a subscriber the front head microphone
        as 16 kHz mono 16-bit PCM every ~170 ms (setClientPreferences(…, 16000,
        FRONTCHANNEL=3, 0) + processRemote). Those buffers go to the platform
        as binary messages on the session socket — byte 0x01 + PCM — while the
        server's `mic_open` says the robot may hear, and the platform's OWN
        voice pipeline (VAD → speech-to-text → hooks / AI) takes it from there,
        the same frame gate the Android app's microphone is held to.
        NAO_MIC_MODE=window keeps the old way (record a window, transcribe it
        via POST /asr/transcribe, send asr_input) for a server that predates
        binary audio.
  eyes  ALVideoDevice: one head camera (top by default), RGB, sent as one image
        per binary message — byte 0x02 + JPEG (or PPM when this NAO's Python
        has no JPEG encoder) — ONLY while the current node needs vision
        (`needs_visual` on state_update: faces, 認人, 拍照, vision models).
        Otherwise the camera is unsubscribed and its LED is off. The backend
        relays frames to the vision-gateway with its own ticket, so the robot
        never sends a credential anywhere but the platform.

FULL DUPLEX (talking OVER the robot) is not possible here: NAO has no echo
cancellation (its playback and capture buffers cannot be acquired in sync), so
the platform closes the robot's ears while it speaks — half-duplex. Barge-in
needs sdk/examples/nao_duplex_client.py on a companion computer with WebRTC's
software echo canceller.

    export SR_BASE_URL="https://sociallab.duckdns.org/ntu_social_robot/api/v1"
    export SR_API_KEY="srk_..."
    export NAO_CHARACTER="角色名稱或 id"
    python meme_nao27.py

See README.md for every setting.
"""
from __future__ import print_function

import json
import math
import os
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "vendor"))

import requests  # noqa: E402  (vendored)
import websocket  # noqa: E402  (vendored websocket-client 0.59)

try:
    import Queue as queue  # Python 2
except ImportError:  # pragma: no cover — lets the tests run under 3 too
    import queue

# NAO's own CA store is years old; Let's Encrypt (sociallab.duckdns.org) must
# verify against a bundle we ship, never the system's and never verify=False.
CA_BUNDLE = os.environ.get("SR_CA_BUNDLE") or os.path.join(HERE, "vendor", "certifi", "cacert.pem")

# Binary messages on the session socket (server: app/modules/session_media.py).
KIND_AUDIO = 0x01
KIND_IMAGE = 0x02

# ALAudioDevice channel ids for setClientPreferences: ALL=0 LEFT=1 RIGHT=2
# FRONT=3 REAR=4. One channel at 16 kHz is the only mono mode it offers.
FRONT_CHANNEL = 3
MIC_SERVICE = "MemeMic"
# ALVideoDevice ids: camera kTop=0 kBottom=1; resolution kQQVGA=0 kQVGA=1
# kVGA=2; colour space kRGBColorSpace=11.
CAMERA_NAME = "meme_cam"
RGB_COLORSPACE = 11

# fractionMaxSpeed for ALMotion.setAngles — topologies/nao.json's speed_default,
# the value the backend retargeter assumes for NAO (same as the bridge).
NAO_DEFAULT_SPEED = 0.2


# ── the client's own version (same hash as server/app/services/infra/nao_package) ──
EXCLUDE_DIRS = ("tests", "__pycache__")
EXCLUDE_FILES = ("meme.env", "device_token", "run.sh", "nao_settings.json")


def code_version(root=None):
    """sha256 over (path, bytes) of every shipped file, first 8 hex — what the
    機器人 card compares with GET /nao/version. Keep in step with the server."""
    import hashlib
    root = root or HERE
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS and not d.startswith("."))
        for name in filenames:
            if name in EXCLUDE_FILES or name.endswith((".pyc", ".pyo")) or name.startswith("."):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            entries.append((rel, full))
    h = hashlib.sha256()
    for rel, full in sorted(entries):
        with open(full, "rb") as f:
            data = f.read()
        rel_b = rel.encode("utf-8") if not isinstance(rel, bytes) else rel
        h.update(rel_b + b"\0" + data + b"\0")
    return h.hexdigest()[:8]


#: The launcher install.sh writes and an update refreshes: a loop, so a client
#: that will not start is retried — and after an update that will not start,
#: the previous copy is put back (`.update_pending` is cleared by a client that
#: reached the platform).
RUN_SH = """#!/bin/sh
# 由 install.sh／更新產生。開機時等 NAOqi 起來再啟動；已經在跑就不重複開。
ps -ef 2>/dev/null | grep "[m]eme_nao27.py" | grep -qv "$$" && exit 0
[ "$1" = "--now" ] || sleep 60
DIR="{dir}"
while true; do
  cd "$DIR" && [ -f meme.env ] && . ./meme.env
  python meme_nao27.py >> "{log}" 2>&1
  if [ -f "$DIR/.update_pending" ] && [ -d "$DIR.prev" ]; then
    mv "$DIR" "$DIR.bad.$(date +%s)" && mv "$DIR.prev" "$DIR"
    echo "update failed to start; previous version restored" >> "{log}"
  fi
  sleep 5
done
"""


def log(*parts):
    # Python 2 writing to a file or a pipe (run.sh's log) has no encoding: a
    # unicode part with Chinese in it would raise here and kill the thread
    # that only wanted to report something.
    out = []
    for p in parts:
        if isinstance(p, BaseException):
            try:
                p = str(p)
            except UnicodeError:
                p = repr(p)
        out.append(to_str(p))
    print(time.strftime("%H:%M:%S"), *out)
    sys.stdout.flush()


def to_str(text):
    """NAOqi wants UTF-8 `str` on Python 2, not `unicode`."""
    if sys.version_info[0] == 2 and isinstance(text, unicode):  # noqa: F821
        return text.encode("utf-8")
    return text


def ws_base(base_url):
    """http(s)://host[/prefix]/api/v1 → ws(s)://host[/prefix]/ws (as the SDK)."""
    return base_url.replace("http", "ws", 1).rsplit("/api/v1", 1)[0] + "/ws"


def binary(kind, payload):
    """One binary message: the kind byte, then the payload (str on Py2)."""
    return bytes(bytearray([kind])) + bytes(payload)


def pick_encoder():
    """The best JPEG encoder this Python has, else PPM (raw RGB with a header —
    the server decodes it the same; it is just ~15x bigger, so the camera runs
    slower). NAOqi images differ in what they ship, so it is found, not assumed."""
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401

        def enc_cv2(w, h, rgb):
            import cv2 as _cv2
            import numpy as _np
            arr = _np.frombuffer(bytes(rgb), dtype=_np.uint8).reshape(h, w, 3)
            ok, buf = _cv2.imencode(".jpg", _cv2.cvtColor(arr, _cv2.COLOR_RGB2BGR),
                                    [_cv2.IMWRITE_JPEG_QUALITY, 75])
            return buf.tostring() if ok else ppm(w, h, rgb)
        return "jpeg (cv2)", enc_cv2
    except Exception:
        pass
    try:
        from PIL import Image  # noqa: F401

        def enc_pil(w, h, rgb):
            import io
            from PIL import Image as _Image
            out = io.BytesIO()
            _Image.frombytes("RGB", (w, h), bytes(rgb)).save(out, "JPEG", quality=75)
            return out.getvalue()
        return "jpeg (PIL)", enc_pil
    except Exception:
        pass
    return "ppm", ppm


def ppm(w, h, rgb):
    return b"P6\n%d %d\n255\n" % (w, h) + bytes(rgb)


def audio_suffix(data):
    if data[:4] == b"RIFF":
        return ".wav"
    if data[:4] == b"OggS":
        return ".ogg"
    if data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return ".mp3"
    return None


# ── the platform ────────────────────────────────────────────────────────────

#: The character-less session a robot opens to be driven from the live console
#: (server: registration_service; the Kebbi app opens it from its picker).
STANDBY = "__standby__"


class Platform(object):
    def __init__(self, base_url, api_key=None, http=None):
        self.base = base_url.rstrip("/")
        self.http = http or requests.Session()
        if api_key:
            self.set_token(api_key)
        self.http.verify = CA_BUNDLE

    def set_token(self, token):
        self.http.headers["Authorization"] = "Bearer " + token

    # ── spoken-code pairing (POST /auth/pair/*): no account needed to ask ──
    def pair_request(self, device_label):
        # The label IS the device id on a NAO; sent as both so a robot locked
        # to an account (鎖定在這個帳號) is refused a code.
        return self._req("POST", "/auth/pair/request",
                         json={"device_label": device_label, "device_id": device_label})

    def pair_poll(self, secret):
        return self._req("POST", "/auth/pair/poll", json={"secret": secret})

    def _req(self, method, path, **kw):
        kw.setdefault("timeout", 20)
        r = self.http.request(method, self.base + path, **kw)
        if r.status_code >= 400:
            # ASCII and UTF-8 bytes only: on Python 2 a str with "→" in it
            # formatted with the unicode `r.text` raised a decode error, so a
            # wrong API key was reported as 'ascii' codec can't decode.
            raise RuntimeError("%s %s -> %s %s" % (method, path, r.status_code, to_str(r.text[:200])))
        return r.json()

    def character_id(self, wanted):
        if "-" in wanted and len(wanted) >= 32:
            return wanted
        for c in self._req("GET", "/characters/"):
            if c.get("name") == wanted:
                return c["id"]
        raise RuntimeError("character %r not found for this API key" % wanted)

    def binding(self, device_id, status=None):
        """Which character the web has given this NAO (個人資源 › 機器人).

        The same question the Android robot asks at boot, and asking it IS this
        machine's heartbeat: the web lists it as online — bound or not — so it
        can be given a character without anyone typing on the robot.
        Unbound answers `bound: false`, not an error."""
        params = {"status": json.dumps(status)} if status else None
        return self._req("GET", "/robot-devices/" + device_id, params=params)

    def nao_package(self):
        """(tar.gz bytes, version) of the client main holds (GET /nao/package)."""
        r = self.http.get(self.base + "/nao/package", timeout=120)
        if r.status_code >= 400:
            raise RuntimeError("GET /nao/package -> %s %s" % (r.status_code, to_str(r.text[:200])))
        return r.content, r.headers.get("X-Nao-Version") or ""

    def upload_log(self, device_id, gz_bytes, version):
        files = {"file": ("nao.log.gz", gz_bytes, "application/gzip")}
        data = {"device_id": device_id, "reason": "remote", "app_build": "nao27:" + version,
                "device_model": "NAO", "os_release": "NAOqi"}
        r = self.http.post(self.base + "/device-logs", files=files, data=data, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError("POST /device-logs -> %s %s" % (r.status_code, to_str(r.text[:200])))

    def device_hardware(self):
        """What this machine is — a NAO — from the server's profile catalog, so
        motions arrive retargeted to NAO joints in radians (see the SDK's
        device_hardware_for: declaring nothing makes the server treat us as the
        character's design type, e.g. Kebbi MOTOR_* in degrees)."""
        blank = {"robot_type": "nao", "mic_array": False, "chassis": False,
                 "motors": False, "camera": False}
        try:
            for p in self._req("GET", "/config/robot-profiles").get("profiles", []):
                if p.get("id") == "nao":
                    return {"robot_type": "nao", "mic_array": bool(p.get("mic_array")),
                            "chassis": bool(p.get("chassis")), "motors": bool(p.get("motors")),
                            "camera": bool(p.get("camera"))}
        except Exception as e:  # an older server: the name alone still helps
            log("[meme] robot profiles unavailable:", e)
        return blank

    def register(self, character_id, device_id):
        return self._req("POST", "/robot/register", json={
            "device_id": device_id, "role": character_id, "app_version": "nao27",
            "device_hardware": self.device_hardware(),
        })

    def transcribe(self, wav_bytes, language):
        files = {"audio": ("mic.wav", wav_bytes, "audio/wav")}
        r = self.http.post(self.base + "/asr/transcribe", files=files,
                           data={"language": language}, timeout=30)
        if r.status_code >= 400:
            log("[asr] %s %s" % (r.status_code, r.text[:120]))
            return ""
        return (r.json().get("text") or "").strip()


# ── the body ────────────────────────────────────────────────────────────────

def _hex_rgb(v):
    h = (v or "").strip().lstrip("#") if isinstance(v, (str, type(u""))) else ""
    if len(h) != 6:
        return None
    try:
        return int(h, 16)
    except ValueError:
        return None


def led_frame(part, t_ms):
    """The color (0xRRGGBB, 0 = off) one light shows at t_ms, or None when it
    is not a setting — the same frame robot-frontend lib/led.ledFrame and the
    Kebbi app compute: 呼吸 = brightness 0.15 + 0.85*(1/2 - 1/2*cos 2*pi*t/T),
    輪播 steps one T/n per color."""
    mode = part.get("mode") or "color"
    if mode == "off":
        return 0
    period = max(500, min(10000, int(part.get("period_ms") or 2000)))
    if mode == "cycle":
        colors = [c for c in (_hex_rgb(x) for x in (part.get("colors") or [])) if c is not None]
        if len(colors) < 2:
            return None
        step = float(period) / len(colors)
        return colors[int(t_ms / step) % len(colors)]
    rgb = _hex_rgb(part.get("color"))
    if rgb is None:
        return None
    if mode == "color":
        return rgb
    if mode != "breath":
        return None
    k = 0.15 + 0.85 * (0.5 - 0.5 * math.cos(2 * math.pi * (t_ms % period) / float(period)))
    return (int(round(((rgb >> 16) & 0xFF) * k)) << 16) | (int(round(((rgb >> 8) & 0xFF) * k)) << 8) | int(round((rgb & 0xFF) * k))


def led_plan(led, whole_groups, part_groups):
    """[(ALLeds group, its setting)]: the whole robot's on its groups, then
    each named part's on its own group (a part overrides the whole)."""
    from collections import OrderedDict
    out = OrderedDict()
    top = dict((k, v) for k, v in led.items() if k != "parts")
    if led_frame(top, 0) is not None:
        for g in whole_groups:
            out[g] = top
    for name, part in (led.get("parts") or {}).items():
        g = part_groups.get(name)
        if g and isinstance(part, dict) and led_frame(part, 0) is not None:
            out[g] = part
    return list(out.items())


def led_describe(led):
    mode = led.get("mode") or "color"
    s = mode if mode != "color" else (led.get("color") or "parts").upper()
    if mode == "off":
        s = "off"
    return s + (" +parts" if led.get("parts") else "")


class NaoBody(object):
    kind = "nao"

    def __init__(self, ip, port, proxy=None):
        if proxy is None:
            from naoqi import ALProxy  # built into NAOqi's Python 2.7
            proxy = ALProxy
        self._proxy = proxy
        self._ip, self._port = ip, port
        self._cam = None
        self.tts = proxy("ALTextToSpeech", ip, port)
        self.video = proxy("ALVideoDevice", ip, port)
        self.motion = proxy("ALMotion", ip, port)
        self.posture = proxy("ALRobotPosture", ip, port)
        self.player = proxy("ALAudioPlayer", ip, port)
        self.recorder = proxy("ALAudioRecorder", ip, port)
        self.leds = proxy("ALLeds", ip, port)
        self.memory = proxy("ALMemory", ip, port)
        self._life = None
        self._life_before = None
        self.anim = proxy("ALAnimationPlayer", ip, port)
        self._anim_id = None
        self.cut_seq = 0
        self._led_last = None
        lang = os.environ.get("NAO_TTS_LANGUAGE")
        if lang:
            try:
                self.tts.setLanguage(lang)
            except Exception as e:
                log("[nao] TTS language %r unavailable (%s); keeping %s" % (lang, e, self._lang()))
        try:
            self.motion.wakeUp()
            self.posture.goToPosture("Stand", 0.5)
        except Exception as e:
            log("[nao] wake/stand failed:", e)

    def _lang(self):
        try:
            return self.tts.getLanguage()
        except Exception:
            return "?"

    def say(self, text):
        self.tts.say(to_str(text))  # blocks until spoken

    def play_audio(self, data):
        """The platform's own voice (Taiwanese Mandarin), when the update
        carried it. False when it could not be played — the caller falls back
        to NAO's synthesizer rather than staying silent."""
        suffix = audio_suffix(data)
        if not suffix:
            return False
        path = "/tmp/meme_tts" + suffix
        try:
            with open(path, "wb") as f:
                f.write(data)
            self.player.playFile(path)  # blocks until played
            return True
        except Exception as e:
            log("[nao] platform audio failed (%s); using NAO's voice" % e)
            return False

    HEAD_KEY = "Device/SubDeviceList/Head/Touch/Middle/Sensor/Value"

    def head_held(self):
        """The middle of the head is being touched right now."""
        try:
            return float(self.memory.getData(self.HEAD_KEY) or 0) > 0.5
        except Exception:
            return False

    def take_over(self):
        """Switch NAO's own Autonomous Life off (its built-in dialog, looking
        around, reacting to sounds) and stand up: from now on only the flow
        moves it. Restored by give_back."""
        try:
            self._life = self._proxy("ALAutonomousLife", self._ip, self._port)
            self._life_before = self._life.getState()
            if self._life_before != "disabled":
                self._life.setState("disabled")
            self.motion.wakeUp()
            self.posture.goToPosture("Stand", 0.5)
            log("[nao] autonomous life %s → disabled; standing up" % self._life_before)
        except Exception as e:
            log("[nao] could not take over from autonomous life: %s" % e)

    def set_volume(self, volume):
        try:
            self._proxy("ALAudioDevice", self._ip, self._port).setOutputVolume(int(volume))
            log("[nao] volume → %d" % int(volume))
        except Exception as e:
            log("[nao] volume failed: %s" % e)

    def set_autonomy(self, on):
        """The card's 自主行為 switch: on = NAO's own Autonomous Life runs."""
        if on:
            try:
                life = self._life or self._proxy("ALAutonomousLife", self._ip, self._port)
                life.setState("solitary")
                log("[nao] autonomous life → solitary")
            except Exception as e:
                log("[nao] autonomous life on failed: %s" % e)
        else:
            self.take_over()

    def give_back(self):
        if self._life is not None and self._life_before and self._life_before != "disabled":
            try:
                self._life.setState(self._life_before)
                log("[nao] autonomous life restored to %s" % self._life_before)
            except Exception as e:
                log("[nao] could not restore autonomous life: %s" % e)

    def animation(self, path):
        """NAO's own gesture library (ALAnimationPlayer), by path — what the
        editor offers a NAO project (robot-frontend/src/data/naoMotions.ts).
        `post` so the line is said while NAO moves, not after."""
        for attempt in (1, 2):
            try:
                self._anim_id = self.anim.post.run(path)
                log("[nao] animation %s" % path)
                return
            except Exception as e:
                log("[nao] animation %r failed: %s" % (path, e))
                if attempt == 2:
                    return
                # The Stand library refuses a NAO that is sitting or crouched
                # (Autonomous Life or a fall put it there): stand, then once more.
                try:
                    self.motion.wakeUp()
                    self.posture.goToPosture("Stand", 0.5)
                except Exception as e2:
                    log("[nao] could not stand up: %s" % e2)
                    return

    def stop_all(self):
        """停止／立刻停: cut what NAO is doing NOW — its voice, the platform's
        audio, the gesture in flight, and keyframes still to come."""
        self.cut_seq += 1
        for name, call in (("tts", lambda: self.tts.stopAll()),
                           ("audio", lambda: self.player.stopAll()),
                           ("animation", lambda: self._anim_id and self.anim.stop(self._anim_id))):
            try:
                call()
            except Exception as e:
                log("[nao] stop %s failed: %s" % (name, e))
        self._anim_id = None
        log("[nao] stopped now")

    #: The LEDs a node's 燈光 sets: the eyes and the chest button.
    LED_GROUPS = ("FaceLeds", "ChestLeds")
    #: 分部位 (2026-10-09): the robot profile's part names → ALLeds groups.
    #: The feet light only when a node names them.
    PART_GROUPS = {"eyes": "FaceLeds", "chest": "ChestLeds", "feet": "FeetLeds"}

    def set_led(self, led):
        """燈光: {"mode": "color"|"off"|"breath"|"cycle", "color", "colors",
        "period_ms", "parts": {"eyes"|"chest"|"feet": the same}} / None = NAO's
        own (reset). Every state_update carries it; a repeat is a no-op. 呼吸
        and 輪播 run on a thread (led_frame — the frame every client computes)
        until the next setting."""
        # A repeat is the same setting whatever case its colors are written in.
        key = None if not led else json.dumps(led, sort_keys=True).upper()
        if key == self._led_last:
            return
        self._led_last = key
        self._stop_led_thread()
        try:
            if led is None:
                # The feet are handed back only if a node lit them.
                lit = getattr(self, "_led_feet", False)
                for g in self.LED_GROUPS + (("FeetLeds",) if lit else ()):
                    self.leds.reset(g)
                self._led_feet = False
                log("[nao] led → system")
                return
            plan = led_plan(led, self.LED_GROUPS, self.PART_GROUPS)
            if any(g == "FeetLeds" for g, _ in plan):
                self._led_feet = True
            animated = {}
            for group, part in plan:
                if part.get("mode") in ("breath", "cycle"):
                    animated[group] = part
                    continue
                rgb = led_frame(part, 0)
                if rgb is not None:
                    self.leds.fadeRGB(group, rgb, 0.2)
            if animated:
                self._start_led_thread(animated)
            log("[nao] led → %s" % led_describe(led))
        except Exception as e:
            log("[nao] led failed: %s" % e)

    def _start_led_thread(self, animated):
        stop = threading.Event()
        self._led_stop = stop
        leds = self.leds

        def run():
            t0 = time.time()
            last = {}
            while not stop.is_set():
                t_ms = int((time.time() - t0) * 1000)
                for group, part in animated.items():
                    rgb = led_frame(part, t_ms)
                    if rgb is not None and last.get(group) != rgb:
                        try:
                            leds.fadeRGB(group, rgb, 0.08)
                        except Exception as e:
                            log("[nao] led tick failed: %s" % e)
                            return
                        last[group] = rgb
                stop.wait(0.1)
        th = threading.Thread(target=run)
        th.daemon = True
        th.start()

    def _stop_led_thread(self):
        stop = getattr(self, "_led_stop", None)
        if stop is not None:
            stop.set()
        self._led_stop = None

    def gesture(self, name):
        if name and name.startswith("animations/"):
            return self.animation(name)
        try:
            if name in ("nod", "yes"):
                self.motion.setAngles("HeadPitch", 0.3, NAO_DEFAULT_SPEED)
                time.sleep(0.4)
                self.motion.setAngles("HeadPitch", 0.0, NAO_DEFAULT_SPEED)
            elif name in ("shake", "no"):
                for a in (0.4, -0.4, 0.0):
                    self.motion.setAngles("HeadYaw", a, NAO_DEFAULT_SPEED)
                    time.sleep(0.35)
        except Exception as e:
            log("[nao] gesture %r failed: %s" % (name, e))

    def play_keyframes(self, keyframes):
        """Server-retargeted keyframes: ALMotion joint names + radians already,
        each applied at its `time` offset; only the listed joints move."""
        t_prev = 0.0
        seq = self.cut_seq
        for kf in keyframes:
            if self.cut_seq != seq:
                return
            try:
                t = float(kf.get("time", 0.0))
                motors = kf.get("motors") or {}
                names = [to_str(n) for n in motors.keys()]
                angles = [float(v) for v in motors.values()]
            except (TypeError, ValueError, AttributeError) as e:
                log("[nao] malformed keyframe skipped:", e)
                continue
            if t > t_prev:
                time.sleep(t - t_prev)
            t_prev = t
            if names:
                try:
                    self.motion.setAngles(names, angles, NAO_DEFAULT_SPEED)
                except Exception as e:
                    log("[nao] keyframe @ %.2fs failed: %s" % (t, e))

    # ── continuous microphone ──
    def start_mic_stream(self, on_pcm, ip, port):
        """Subscribe to ALAudioDevice; `on_pcm(bytes)` gets 16 kHz mono PCM
        every ~170 ms. NAOqi 2.5+ (NAO6 is 2.8): a qi service. Older NAOqi: an
        ALModule on an ALBroker, which must be a module-level global named
        like the module and whose methods need docstrings to be bound."""
        try:
            import qi

            class MicService(object):
                def processRemote(self, nbOfChannels, nbOfSamplesByChannel, timeStamp, inputBuffer):
                    on_pcm(bytes(inputBuffer))

            app = qi.Application(["meme_nao27", "--qi-url=tcp://%s:%d" % (ip, port)])
            app.start()
            self._qi_app = app
            self._mic_service = MicService()
            self._mic_service_id = app.session.registerService(MIC_SERVICE, self._mic_service)
            audio = app.session.service("ALAudioDevice")
            audio.setClientPreferences(MIC_SERVICE, 16000, FRONT_CHANNEL, 0)
            audio.subscribe(MIC_SERVICE)
            self._audio = audio
            return "qi"
        except ImportError:
            pass
        from naoqi import ALBroker, ALModule
        global MemeMic
        self._broker = ALBroker("memeMicBroker", "0.0.0.0", 0, ip, port)

        class MemeMicModule(ALModule):
            def processRemote(self, nbOfChannels, nbOfSamplesByChannel, timeStamp, inputBuffer):
                """Microphone buffers from ALAudioDevice."""
                on_pcm(bytes(inputBuffer))

        MemeMic = MemeMicModule(MIC_SERVICE)
        self._audio = self._proxy("ALAudioDevice", ip, port)
        self._audio.setClientPreferences(MIC_SERVICE, 16000, FRONT_CHANNEL, 0)
        self._audio.subscribe(MIC_SERVICE)
        return "ALBroker"

    def stop_mic_stream(self):
        try:
            self._audio.unsubscribe(MIC_SERVICE)
        except Exception:
            pass

    # ── camera ──
    def camera_open(self, camera, resolution, fps):
        """Subscribe one camera. A NAO keeps at most six subscriptions per
        name, and a client that died without unsubscribing leaves its own
        behind — clear ours first, or the seventh start fails."""
        try:
            for sub in self.video.getSubscribers() or []:
                if str(sub).startswith(CAMERA_NAME):
                    self.video.unsubscribe(sub)
        except Exception:
            pass
        self._cam = self.video.subscribeCamera(CAMERA_NAME, camera, resolution, RGB_COLORSPACE, fps)
        return self._cam

    def camera_frame(self):
        """(width, height, RGB bytes) or None. getImageRemote's array:
        [0] width, [1] height, [2] layers, [3] colour space, [4] seconds,
        [5] microseconds, [6] the pixels."""
        img = self.video.getImageRemote(self._cam)
        if not img or len(img) < 7 or not img[6]:
            return None
        return int(img[0]), int(img[1]), img[6]

    def camera_close(self):
        cam, self._cam = getattr(self, "_cam", None), None
        if cam:
            try:
                self.video.unsubscribe(cam)
            except Exception:
                pass

    def record(self, seconds, path="/tmp/meme_mic.wav"):
        """One window from the FRONT head microphone, 16 kHz mono WAV. (The
        channel tuple is left, right, front, rear; one channel at 16 kHz is the
        only mono mode ALAudioRecorder offers in WAV.)"""
        try:
            self.recorder.stopMicrophonesRecording()
        except Exception:
            pass
        self.recorder.startMicrophonesRecording(path, "wav", 16000, (0, 0, 1, 0))
        time.sleep(seconds)
        self.recorder.stopMicrophonesRecording()
        with open(path, "rb") as f:
            return f.read()


class DryRunBody(object):
    """No NAOqi (a dev machine): print what NAO would do; nothing is heard."""
    kind = "dry-run"

    def say(self, text):
        log("NAO would say:", to_str(text))

    def play_audio(self, data):
        return False

    cut_seq = 0

    def gesture(self, name):
        log("NAO would gesture:", name)

    def stop_all(self):
        self.cut_seq += 1
        log("NAO would stop")

    def set_led(self, led):
        log("NAO would light:", led)

    def head_held(self):
        return False

    def take_over(self):
        pass

    def set_volume(self, volume):
        log("NAO would set volume:", volume)

    def set_autonomy(self, on):
        log("NAO would set autonomy:", on)

    def give_back(self):
        pass

    def play_keyframes(self, keyframes):
        log("NAO would play %d keyframe(s)" % len(keyframes))

    def record(self, seconds):
        time.sleep(seconds)
        return b""

    def start_mic_stream(self, on_pcm, ip, port):
        return None

    def stop_mic_stream(self):
        pass

    def camera_open(self, camera, resolution, fps):
        return None

    def camera_frame(self):
        return None

    def camera_close(self):
        pass


def wav_rms(wav):
    """Loudness of a 16-bit WAV, to skip silent windows instead of sending
    every four seconds of a quiet room to the recognizer."""
    import audioop
    pcm = wav[44:] if wav[:4] == b"RIFF" else wav
    if len(pcm) < 2:
        return 0
    return audioop.rms(pcm[: len(pcm) - (len(pcm) % 2)], 2)


# ── the session ─────────────────────────────────────────────────────────────

class Client(object):
    def __init__(self, platform, body, character, device_id,
                 voice="auto", listen=True, listen_seconds=4.0, silence_rms=300,
                 asr_language="zh", mic_mode="stream", camera=True, camera_index=0,
                 camera_resolution=1, camera_fps=None, encoder=None):
        self.platform = platform
        self.body = body
        self.character = character
        self.device_id = device_id
        self.voice = voice
        self.listen = listen
        self.listen_seconds = listen_seconds
        self.silence_rms = silence_rms
        self.asr_language = asr_language
        self.session_id = None
        self.ticket = None
        self.ws = None
        self.connected = threading.Event()
        self.stopping = threading.Event()
        self.updates = queue.Queue()
        self.send_lock = threading.Lock()
        # The server's verdict on whether the robot may hear right now
        # (voice_policy.mic_should_be_open). Absent on an older server ⇒ True.
        self.mic_open = True
        self.mic_closed_seq = 0   # bumps on every update that says the mic is SHUT
        self.mic_mode = mic_mode
        # Whether the node the robot is on needs vision (`needs_visual`). Off
        # until a state_update says otherwise: the camera never runs unasked.
        self.camera = camera
        self.camera_wanted = False
        self.camera_index = camera_index
        self.camera_resolution = camera_resolution
        self.encoder_name, self.encode = encoder or pick_encoder()
        self.camera_fps = camera_fps or (5.0 if self.encoder_name.startswith("jpeg") else 2.0)
        self.audio_bytes_sent = 0
        self.frames_sent = 0
        self.sent = []     # (event, data) — diagnostics and the tests
        # No NAO_CHARACTER: run whatever the web gives this machine, and follow
        # it when the web changes it (captain 2026-10-08: 換角色不要 SSH).
        self.follow_binding = not character
        # Spoken-code pairing: who this NAO is acting for (a device token).
        http = getattr(platform, "http", None)
        self.has_token = http is None or bool(http.headers.get("Authorization"))
        self.token_file = None          # main() sets where an earned token is kept
        self.last_state = None
        self.last_state_at = 0.0
        self.pair_poll_s = 2.0
        self.head_hold_s = 3.0
        self.locked_retry_s = 60.0
        # The 機器人 card: what this NAO reports, and the settings it keeps.
        self.home = HERE
        self.log_file = os.path.expanduser("~/meme_nao27.log")
        self.settings = {}
        self.version = code_version(self.home) if os.path.isdir(self.home) else "dev"
        self.character_id = None     # the character the current session runs
        self.actor = None            # 多演員專案: the actor it plays there (None = A)
        self.bound_actor = None
        self.waiting_logged = False

    # ── wire ──
    def status(self):
        st = {"version": self.version, "kind": "nao"}
        if "volume" in self.settings:
            st["volume"] = self.settings["volume"]
        if "autonomy" in self.settings:
            st["autonomy"] = self.settings["autonomy"]
        return st

    def settings_path(self):
        return os.path.join(self.home, "nao_settings.json")

    def load_settings(self):
        try:
            with open(self.settings_path()) as f:
                self.settings = json.load(f) or {}
        except (IOError, OSError, ValueError):
            self.settings = {}
        return self.settings

    def save_settings(self):
        try:
            with open(self.settings_path(), "w") as f:
                json.dump(self.settings, f)
        except (IOError, OSError) as e:
            log("[nao] could not save settings:", e)

    def handle_command(self, msg):
        """The 機器人 card (POST /robot-devices/{id}/command)."""
        cmd = msg.get("command")
        if msg.get("type") == "device_log_request":
            return self.upload_log()
        if cmd == "volume":
            self.settings["volume"] = int(msg.get("value"))
            self.save_settings()
            self.body.set_volume(self.settings["volume"])
        elif cmd == "autonomy":
            self.settings["autonomy"] = bool(msg.get("value"))
            self.save_settings()
            self.body.set_autonomy(self.settings["autonomy"])
        elif cmd == "restart":
            self.restart()
        elif cmd == "update":
            self.update()

    def upload_log(self):
        import gzip, io
        try:
            with open(self.log_file, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 512 * 1024))
                tail = f.read()
        except (IOError, OSError):
            tail = ("(no log file at %s)\n" % to_str(self.log_file)).encode("utf-8") if not isinstance(self.log_file, bytes) else b"(no log file)\n"
        buf = io.BytesIO()
        gz = gzip.GzipFile(fileobj=buf, mode="wb")
        gz.write(tail)
        gz.close()
        try:
            self.platform.upload_log(self.device_id, buf.getvalue(), self.version)
            log("[nao] log uploaded (%d bytes)" % len(tail))
        except Exception as e:
            log("[nao] log upload failed:", e)

    def restart(self):
        log("[nao] restarting the client")
        self.stopping.set()
        try:
            if self.ws is not None:
                self.ws.close()
        except Exception:
            pass
        self.exec_self()

    def exec_self(self):  # pragma: no cover - replaces the process
        os.execv(sys.executable, [sys.executable, os.path.join(self.home, "meme_nao27.py")])

    def update(self):
        """Fetch main's client, check it compiles, swap it in keeping the old
        copy (`<dir>.prev`) and the per-NAO files, then restart into it. If the
        new client does not reach the platform, it (or run.sh) puts the old
        one back — `.update_pending` marks that window."""
        import tarfile, io, shutil, py_compile
        try:
            data, version = self.platform.nao_package()
        except Exception as e:
            log("[nao] update download failed:", e)
            return False
        if version and version == self.version:
            log("[nao] already on %s" % version)
            return False
        home = self.home.rstrip("/")
        new, prev = home + ".new", home + ".prev"
        shutil.rmtree(new, ignore_errors=True)
        try:
            os.makedirs(new)
            tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
            try:
                for member in tar.getmembers():
                    name = member.name
                    if not name.startswith("nao27/") or ".." in name.split("/") or member.issym() or member.islnk():
                        continue
                    member.name = name[len("nao27/"):]
                    if member.name:
                        tar.extract(member, new)
            finally:
                tar.close()
            py_compile.compile(os.path.join(new, "meme_nao27.py"), doraise=True)
        except Exception as e:
            log("[nao] update rejected (%s); staying on %s" % (e, self.version))
            shutil.rmtree(new, ignore_errors=True)
            return False
        for keep in EXCLUDE_FILES:
            src = os.path.join(home, keep)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(new, keep))
        shutil.rmtree(prev, ignore_errors=True)
        os.rename(home, prev)
        os.rename(new, home)
        with open(os.path.join(home, ".update_pending"), "w") as f:
            f.write("%s %s %d" % (self.version, version, int(time.time())))
        try:
            with open(os.path.join(home, "run.sh"), "w") as f:
                f.write(RUN_SH.format(dir=home, log=self.log_file))
            os.chmod(os.path.join(home, "run.sh"), 0o755)
        except Exception as e:
            log("[nao] could not refresh run.sh:", e)
        log("[nao] updated %s → %s; restarting" % (self.version, version))
        self.restart()
        return True

    def watch_update(self, grace_s=90.0):
        """Started by a client that came up after an update: reach the platform
        within `grace_s`, or put the previous copy back."""
        marker = os.path.join(self.home, ".update_pending")
        if not os.path.exists(marker):
            return
        def guard():
            if self.connected.wait(grace_s):
                try:
                    os.remove(marker)
                except OSError:
                    pass
                log("[nao] update confirmed: now on %s" % self.version)
                return
            home = self.home.rstrip("/")
            if os.path.isdir(home + ".prev"):
                log("[nao] the new version never reached the platform; restoring the previous one")
                os.rename(home, home + ".bad.%d" % int(time.time()))
                os.rename(home + ".prev", home)
                self.restart()
        t = threading.Thread(target=guard)
        t.daemon = True
        t.start()

    def bound_character(self):
        """The character the web gives this NAO, or STANDBY when none (unbound,
        or bound to a character in the trash, which the server names as nothing).
        Also remembers which actor it plays there (多演員專案: "B"…, None = A) —
        the server decides that at registration, so a change re-registers."""
        b = self.platform.binding(self.device_id, self.status()) or {}
        cid = b.get("character_id") if b.get("bound") and b.get("character_name") else None
        self.bound_actor = b.get("actor") if cid else None
        return cid or STANDBY

    def wanted_character(self):
        """The character to run now. With no character from the web NAO stands
        by under the live console's control (實時中控, captain 2026-10-08) — the
        same `__standby__` session a Kebbi opens from its picker — so it can be
        driven the moment it is paired."""
        if not self.follow_binding:
            return self.platform.character_id(self.character)
        cid = self.bound_character()
        if cid == STANDBY:
            if not self.waiting_logged:
                log("[meme] no character: standing by for the live console (bind one in 個人資源 › 機器人 › %s)"
                    % self.device_id)
                self.waiting_logged = True
        else:
            self.waiting_logged = False
        return cid

    def register(self):
        cid = self.wanted_character()
        if cid is None:
            return False
        reg = self.platform.register(cid, self.device_id)
        self.character_id = cid
        self.actor = self.bound_actor
        self.session_id = reg["session_id"]
        self.ticket = reg.get("ws_ticket")
        # The socket address comes from SR_BASE_URL, like the SDK — never from
        # the registration's `websocket_url`, which is built from the server's
        # WS_PATH setting and can name a host this robot cannot (or must not)
        # reach: on a dev stack it is ws://localhost:6868, PROD's own port.
        self.ws_url = (ws_base(self.platform.base) + "/" + self.session_id
                       + ("?ticket=" + self.ticket if self.ticket else ""))
        log("[meme] session %s" % self.session_id)
        return True

    def send(self, event, data=None):
        msg = {"type": "event", "event": event, "session_id": self.session_id,
               "timestamp": int(time.time() * 1000)}
        if data is not None:
            msg["data"] = data
        with self.send_lock:
            if self.ws is None or not self.connected.is_set():
                log("[meme] not connected; %s dropped" % event)
                return False
            self.ws.send(json.dumps(msg))
        self.sent.append((event, data))
        return True

    def send_binary(self, kind, payload):
        with self.send_lock:
            if self.ws is None or not self.connected.is_set():
                return False
            self.ws.send(binary(kind, payload), opcode=websocket.ABNF.OPCODE_BINARY)
        return True

    def on_open(self, ws):
        self.connected.set()
        self.send("client_ready")   # every connect: it is also what resumes a session after a deploy
        log("[meme] connected")

    def on_message(self, ws, raw):
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if msg.get("type") in ("device_command", "device_log_request"):
            t = threading.Thread(target=self.handle_command, args=(msg,))
            t.daemon = True
            t.start()
            return
        if msg.get("type") == "stop_now":
            self.stop_now()
            return
        if msg.get("type") != "state_update":
            return
        data = msg.get("data") or {}
        self.last_state = msg.get("state")
        self.last_state_at = time.time()
        if "mic_open" in data:
            self.mic_open = bool(data["mic_open"])
            if not self.mic_open:
                self.mic_closed_seq += 1
        if data.get("needs_visual") is not None:
            self.camera_wanted = bool(data["needs_visual"])
        if "led" in data:
            setter = getattr(self.body, "set_led", None)
            if setter:
                setter(data.get("led"))
        self.updates.put(msg)

    def stop_now(self):
        """The editor's 停止 and the console's 立刻停 (server: {"type":
        "stop_now"}, right after an IDLE). Speech blocks the playback thread,
        so the cut happens here: drop the lines still queued, then stop NAO's
        voice, audio and gesture. The cut line is never acknowledged — the
        server says it again on 繼續, or starts over."""
        dropped = 0
        kept = []
        while True:
            try:
                m = self.updates.get_nowait()
            except queue.Empty:
                break
            if m.get("state") == "SPEAKING":
                dropped += 1
            else:
                kept.append(m)
        for m in kept:
            self.updates.put(m)
        stopper = getattr(self.body, "stop_all", None)
        if stopper:
            stopper()
        log("[meme] stop_now: %d queued line(s) dropped" % dropped)

    def on_close(self, ws, *args):
        self.connected.clear()
        self.camera_wanted = False
        log("[meme] socket closed", args[:1])

    def on_error(self, ws, err):
        log("[meme] socket error:", err)

    # ── spoken-code pairing ──
    def say_code(self, code):
        # The code arrives as unicode (JSON); on Python 2 joining it with a
        # byte-string "、" raises before NAO says anything.
        code = to_str(code)
        spoken = "、".join(code)
        log("[pair] code %s — type it on the web: 個人資源 › 機器人 › 連上機器人" % code)
        self.body.say("我的配對碼是 %s。請在網頁輸入。" % spoken)

    def pair_once(self):
        """Ask for a code, say it, wait for someone to type it. A touch on the
        head while it waits says the code again — it used to do nothing until
        the code ran out, five minutes later (2026-10-08). Returns
        (token, user_name), or None when nobody typed it in time."""
        try:
            req = self.platform.pair_request(self.device_id)
        except RuntimeError as e:
            if "-> 403" not in str(e):
                raise
            # Locked to an account on the web: there is no code to give.
            log("[pair] refused:", e)
            self.body.say("這台機器人鎖定在別人的帳號，不能換人。")
            if not self.has_token:
                # Nothing else to do but ask again; not five times a second.
                self.stopping.wait(self.locked_retry_s)
            return None
        self.say_code(req["code"])
        deadline = time.time() + float(req.get("expires_in") or 300)
        next_poll = time.time() + self.pair_poll_s
        touched = self.body.head_held()   # the hold that asked for this code
        while time.time() < deadline and not self.stopping.is_set():
            held = self.body.head_held()
            if held and not touched:
                self.say_code(req["code"])
            touched = held
            if time.time() < next_poll:
                time.sleep(0.1)
                continue
            next_poll = time.time() + self.pair_poll_s
            try:
                got = self.platform.pair_poll(req["secret"])
            except Exception as e:
                log("[pair] poll failed:", e)
                continue
            if got.get("status") == "approved":
                return got["token"], got.get("user_name") or ""
            if got.get("status") == "claimed":
                return None
            if got.get("status") == "expired":
                break
        if self.has_token and not self.stopping.is_set():
            # Without a key the next code follows at once; with one, the head
            # is what asks again — say so, or the silence reads as broken.
            self.body.say("配對碼過期了。要換人，請再摸我的頭三秒。")
        return None

    def busy(self):
        """In the middle of an interaction: someone is being talked to."""
        return (self.session_id is not None
                and self.last_state in ("SPEAKING", "LISTENING", "THINKING")
                and time.time() - self.last_state_at < 120)

    def apply_token(self, token, user_name):
        """Act for the person who typed the code — after the current
        interaction, never in the middle of it."""
        warned = False
        while self.busy() and not self.stopping.is_set():
            if not warned:
                self.body.say("等一下，我還在跟人說話。說完就換。")
                warned = True
            time.sleep(1.0)
        if self.token_file:
            try:
                old = os.umask(0o077)
                with open(self.token_file, "w") as f:
                    f.write(token)
                os.umask(old)
            except Exception as e:
                log("[pair] could not save the token:", e)
        self.platform.set_token(token)
        self.has_token = True
        self.body.say("好，現在聽%s的。" % to_str(user_name) if user_name else "好，連上了。")
        log("[pair] now acting for %s" % to_str(user_name))
        self.session_id = None
        self.character_id = None
        self.camera_wanted = False
        ws = self.ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def run_pairing(self):
        """No token yet: keep offering a code. Otherwise: a head held for
        head_hold_s seconds offers one — that is how a lab member takes over."""
        held_since = None
        while not self.stopping.is_set():
            try:
                if not self.has_token:
                    got = self.pair_once()
                    if got:
                        self.apply_token(*got)
                    continue
                if self.body.head_held():
                    held_since = held_since or time.time()
                    if time.time() - held_since >= self.head_hold_s:
                        held_since = None
                        got = self.pair_once()
                        if got:
                            self.apply_token(*got)
                else:
                    held_since = None
            except Exception as e:
                log("[pair] failed:", e)
                time.sleep(5)
            time.sleep(0.2)

    def run_socket(self):
        """Reconnect to the SAME session (the ticket lasts 24 h; a backend
        restart keeps the session for a returning robot); after repeated
        failures, register a fresh one."""
        failures = 0
        while not self.stopping.is_set():
            if not self.has_token:
                time.sleep(1.0)
                continue
            if self.session_id is None or failures >= 3:
                try:
                    if not self.register():
                        time.sleep(self.binding_poll_s)
                        continue
                    failures = 0
                except Exception as e:
                    log("[meme] register failed:", e)
                    time.sleep(min(30, 2 ** min(failures, 4)))
                    failures += 1
                    continue
            self.ws = websocket.WebSocketApp(
                self.ws_url, on_open=self.on_open, on_message=self.on_message,
                on_close=self.on_close, on_error=self.on_error)
            opened_at = time.time()
            self.ws.run_forever(sslopt={"ca_certs": CA_BUNDLE}, ping_interval=20, ping_timeout=10)
            self.connected.clear()
            if self.stopping.is_set():
                break
            failures = 0 if time.time() - opened_at > 60 else failures + 1
            time.sleep(min(15, 1 + failures * 2))

    binding_poll_s = 10.0

    def binding_changed(self):
        """True when the web now gives this NAO a different character (or
        none — standby): the current session is over and the next one runs that."""
        cid = self.bound_character()
        return cid != self.character_id or self.bound_actor != self.actor

    def run_binding(self):
        """Follow the web: a changed binding closes this session; run_socket
        then registers whatever the web gives now (or waits for one)."""
        while not self.stopping.is_set():
            time.sleep(self.binding_poll_s)
            self.check_binding_once()

    def check_binding_once(self):
        if not self.follow_binding:
            # A fixed character: the poll is only this NAO's hello + status.
            if self.has_token:
                try:
                    self.platform.binding(self.device_id, self.status())
                except Exception as e:
                    log("[meme] hello failed:", e)
            return False
        if self.session_id is None:
            return False
        try:
            changed = self.binding_changed()
        except Exception as e:
            log("[meme] binding check failed:", e)
            return False
        if changed:
            log("[meme] the web changed this robot's character; switching")
            self.session_id = None
            self.camera_wanted = False
            ws = self.ws
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
        return changed

    # ── down ──
    def handle(self, msg):
        if msg.get("state") != "SPEAKING":
            return
        data = msg.get("data") or {}
        text = data.get("text")
        lines = text if isinstance(text, list) else ([text] if text else [])
        seq = getattr(self.body, "cut_seq", 0)
        cut = lambda: getattr(self.body, "cut_seq", 0) != seq
        keyframes = data.get("motor_keyframes")
        # A library gesture starts with the line (ALAnimationPlayer.post), so
        # NAO moves while it speaks rather than after.
        if not keyframes and data.get("motion"):
            self.body.gesture(data["motion"])
        spoken = False
        audio_b64 = data.get("audio")
        if audio_b64 and self.voice in ("auto", "platform"):
            import base64
            try:
                spoken = self.body.play_audio(base64.b64decode(audio_b64))
            except (TypeError, ValueError):
                spoken = False
        if not spoken:
            for line in lines:
                if cut():
                    break
                if line:
                    self.body.say(line)
        if keyframes and not cut():
            self.body.play_keyframes(keyframes)
        if cut():
            return
        if lines or spoken:
            self.send("tts_finished")

    def run_down(self):
        while not self.stopping.is_set():
            try:
                msg = self.updates.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.handle(msg)
            except Exception as e:
                log("[nao] update failed:", e)

    # ── up ──
    def listen_once(self):
        """One window. Returns the transcript sent, or None."""
        if not (self.connected.is_set() and self.mic_open):
            time.sleep(0.2)
            return None
        closed_before = self.mic_closed_seq
        wav = self.body.record(self.listen_seconds)
        # Open at both ends AND never shut in between: a line the robot began
        # and finished inside one window would look clean to either end alone.
        if not self.mic_open or self.mic_closed_seq != closed_before:
            return None
        if not wav or wav_rms(wav) < self.silence_rms:
            return None
        text = self.platform.transcribe(wav, self.asr_language)
        if not text:
            return None
        log("[mic]", to_str(text))
        self.send("asr_input", text)
        return text

    def on_pcm(self, pcm):
        """ALAudioDevice's buffer → the platform, only while it may hear. The
        platform's gate drops it anyway outside LISTENING; not sending it saves
        the robot's Wi-Fi and makes "it never heard itself" true at both ends."""
        if not (self.connected.is_set() and self.mic_open):
            return
        if self.send_binary(KIND_AUDIO, pcm):
            self.audio_bytes_sent += len(pcm)

    def camera_step(self):
        """One tick of the camera loop. Returns the seconds to wait."""
        want = self.camera and self.connected.is_set() and self.camera_wanted
        if not want:
            if self.body_camera_open:
                self.body.camera_close()
                self.body_camera_open = False
                log("[cam] off")
            return 0.2
        if not self.body_camera_open:
            self.body.camera_open(self.camera_index, self.camera_resolution, int(round(self.camera_fps)))
            self.body_camera_open = True
            log("[cam] on (%s, %.0f fps)" % (self.encoder_name, self.camera_fps))
        frame = self.body.camera_frame()
        if frame:
            w, h, rgb = frame
            if self.send_binary(KIND_IMAGE, self.encode(w, h, rgb)):
                self.frames_sent += 1
        return 1.0 / self.camera_fps

    body_camera_open = False

    def run_camera(self):
        while not self.stopping.is_set():
            try:
                time.sleep(self.camera_step())
            except Exception as e:
                log("[cam] failed:", e)
                self.body.camera_close()
                self.body_camera_open = False
                time.sleep(2)
        self.body.camera_close()

    def run_up(self):
        while not self.stopping.is_set():
            try:
                self.listen_once()
            except Exception as e:
                log("[mic] failed:", e)
                time.sleep(1)

    def start(self, ip=None, port=None):
        threads = [threading.Thread(target=self.run_socket), threading.Thread(target=self.run_down),
                   threading.Thread(target=self.run_pairing), threading.Thread(target=self.run_binding)]
        if self.listen and self.mic_mode == "stream":
            how = self.body.start_mic_stream(self.on_pcm, ip, port)
            log("[mic] streaming to the platform (%s)" % how)
        elif self.listen:
            threads.append(threading.Thread(target=self.run_up))
        if self.camera:
            threads.append(threading.Thread(target=self.run_camera))
        for t in threads:
            t.daemon = True
            t.start()
        return threads


def env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def main():
    base = os.environ.get("SR_BASE_URL", "https://sociallab.duckdns.org/ntu_social_robot/api/v1")
    # Who this NAO acts for: SR_API_KEY if set (the old way), else the device
    # token a person earned by typing its spoken code (kept in TOKEN_FILE).
    # Neither → it says a pairing code and waits.
    token_file = os.environ.get("NAO_TOKEN_FILE") or os.path.join(HERE, "device_token")
    key = os.environ.get("SR_API_KEY")
    if not key and os.path.exists(token_file):
        try:
            with open(token_file) as f:
                key = f.read().strip() or None
        except Exception:
            key = None
    character = os.environ.get("NAO_CHARACTER")
    ip = os.environ.get("NAO_IP", "127.0.0.1")
    port = int(os.environ.get("NAO_PORT", "9559"))
    try:
        body = NaoBody(ip, port)
    except Exception as e:
        log("[nao] NAOqi unavailable (%s); dry run" % e)
        body = DryRunBody()
    client = Client(
        Platform(base, key), body, character,
        device_id=os.environ.get("NAO_DEVICE_ID") or ("nao-" + socket.gethostname()),
        voice=os.environ.get("NAO_VOICE", "auto"),
        listen=os.environ.get("NAO_LISTEN", "1") != "0" and body.kind == "nao",
        listen_seconds=env_float("NAO_LISTEN_SECONDS", 4.0),
        silence_rms=env_float("NAO_SILENCE_RMS", 300),
        asr_language=os.environ.get("SR_ASR_LANGUAGE", "zh"),
        mic_mode=os.environ.get("NAO_MIC_MODE", "stream"),
        camera=os.environ.get("NAO_CAMERA", "1") != "0" and body.kind == "nao",
        camera_index=int(os.environ.get("NAO_CAMERA_INDEX", "0")),
        camera_resolution=int(os.environ.get("NAO_CAMERA_RESOLUTION", "1")),
        camera_fps=env_float("NAO_CAMERA_FPS", 0.0) or None,
    )
    log("[meme] NAO client (%s) → %s; camera frames as %s" % (body.kind, base, client.encoder_name))
    client.token_file = token_file
    # Only the flow moves NAO: its own Autonomous Life (built-in dialog, looking
    # around) is switched off while this runs. NAO_TAKE_OVER=0 keeps it.
    client.log_file = os.environ.get("NAO_LOG_FILE") or client.log_file
    settings = client.load_settings()
    # The card's 自主行為 switch wins over the env default once it was used.
    take_over = (not settings["autonomy"]) if "autonomy" in settings else os.environ.get("NAO_TAKE_OVER", "1") != "0"
    if take_over:
        body.take_over()
    if "volume" in settings:
        body.set_volume(settings["volume"])
    client.start(ip, port)
    client.watch_update()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        client.stopping.set()
        if take_over:
            body.give_back()
        client.body.stop_mic_stream()
        if client.ws is not None:
            client.ws.close()
        log("stopped")


if __name__ == "__main__":
    main()
