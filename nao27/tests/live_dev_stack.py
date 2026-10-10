# -*- coding: utf-8 -*-
"""meme_nao27 against a REAL backend (the isolated dev stack), on Python 2.7.

A fresh account → an API key → a NAO character:
  開場 says two lines → 等你說 waits (loop, an asr_input hook on 「恐龍」) → 恐龍
  says one → 拍照 takes a photo (capture) → 拍到了 / 沒拍到.
The client runs exactly as on the robot — registration, the ticketed socket,
client_ready, the down/up threads, the camera loop — with only the body faked
(no NAOqi here: a recorded utterance stands in for the microphone, a PPM image
for the camera) and the window recognizer faked (a dev stack cannot reach the
ASR worker). It proves: each line is said and acked; the CONTINUOUS microphone
reaches the platform's own voice pipeline (its VAD hears the recorded
utterance and the server answers with the `hearing` cue — 聆聽綠燈 is on for
this character); a transcript moves the flow; the camera switches on by itself
on the node that needs vision, its frames reach the vision-gateway through the
backend (the 拍照 node can only end in 拍到了 if the gateway had a frame), and
it switches off again after.

    SR_BASE_URL=http://localhost:<dev backend port>/api/v1 python tests/live_dev_stack.py
"""
from __future__ import print_function

import os
import sys
import time
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import meme_nao27 as m  # noqa: E402
import requests  # noqa: E402  (vendored, via meme_nao27's sys.path)

BASE = os.environ["SR_BASE_URL"].rstrip("/")
PASSED, FAILED = [], []


def check(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    detail = m.to_str(detail) if not isinstance(detail, str) else detail
    print(("  ✓ " if ok else "  ✗ ") + m.to_str(name) + ("" if ok else "  — " + detail))


class Body(m.DryRunBody):
    kind = "fake"

    def __init__(self):
        self.said = []
        self.loud = False
        self.on_pcm = None
        self.camera = []          # "open" / "close"
        self.head = False         # the middle head sensor, held

    def start_mic_stream(self, on_pcm, ip, port):
        self.on_pcm = on_pcm
        return "fake"

    def camera_open(self, camera, resolution, fps):
        self.camera.append("open")

    def camera_frame(self):
        w, h = 64, 48
        return w, h, b"\x30\x80\xc0" * (w * h)

    def camera_close(self):
        if self.camera and self.camera[-1] == "open":
            self.camera.append("close")

    def say(self, text):
        # Kept as unicode, whatever the client passed (Python 2 str is bytes).
        self.said.append(text if isinstance(text, type(u"")) else text.decode("utf-8"))

    def head_held(self):
        return self.head

    def set_volume(self, v):
        self.volume = v

    def set_autonomy(self, on):
        self.autonomy = on

    def record(self, seconds):
        time.sleep(seconds)
        if not self.loud:
            return b""
        import struct
        n = 1600
        return b"RIFF" + b"\x00" * 40 + struct.pack("<%dh" % n, *([4000, -4000] * (n // 2)))


class Platform(m.Platform):
    def transcribe(self, wav, language):
        return u"我想看恐龍"


class Client(m.Client):
    """Records every message type the server sends (the `hearing` cue is a
    one-shot `reaction`, which the client itself has no use for)."""

    def __init__(self, *a, **kw):
        m.Client.__init__(self, *a, **kw)
        self.received = []

    def on_message(self, ws, raw):
        try:
            import json
            msg = json.loads(raw)
            self.received.append((msg.get("type"), msg.get("kind")))
        except ValueError:
            pass
        m.Client.on_message(self, ws, raw)


def stream_utterance(body, path):
    """The recorded utterance through the mic callback, 170 ms at a time and
    in real time — what ALAudioDevice.processRemote hands the client."""
    with open(path, "rb") as f:
        pcm = f.read()[44:]
    step = 2720 * 2
    for i in range(0, len(pcm), step):
        body.on_pcm(pcm[i:i + step])
        time.sleep(0.17)
    for _ in range(8):                       # trailing silence ends the utterance
        body.on_pcm(b"\x00\x00" * 2720)
        time.sleep(0.17)


def wait(pred, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.2)
    return False


def main():
    s = requests.Session()
    email = "nao27_%d@example.com" % int(time.time())
    r = s.post(BASE + "/auth/register", json={"email": email, "password": "Nao27!2026", "display_name": "nao27"})
    assert r.status_code == 201, r.text
    tok = s.post(BASE + "/auth/login", json={"email": email, "password": "Nao27!2026"}).json()["access_token"]
    h = {"Authorization": "Bearer " + tok}
    key = s.post(BASE + "/users/me/api-keys", headers=h, json={"name": "nao27"}).json()["api_key"]

    char = s.post(BASE + "/characters/", headers=h, json={"name": u"NAO 2.7 測試", "robot_type": "nao"}).json()
    cid = char["id"]
    r = s.put(BASE + "/characters/%s" % cid, headers=h, json={"voice_config": {"hearing_glow": "on"}})
    assert r.status_code == 200, r.text

    def node(name, order, **tc):
        return s.post(BASE + "/characters/%s/workflows" % cid, headers=h, json={
            "name": name, "node_type": "script", "sequence_order": order,
            "transition_config": tc}).json()["id"]
    a = node(u"開場", 0)
    w = node(u"等你說", 1, loop_mode=True)
    b = node(u"恐龍", 2)
    cam = node(u"拍照", 3, loop_mode=True,
               capture={"count": 1, "delay_seconds": 3, "interval_seconds": 0.5})
    got = node(u"拍到了", 4)
    missed = node(u"沒拍到", 5)

    def lines(wf, texts):
        r = s.post(BASE + "/workflows/%s/scripts/bulk" % wf, headers=h, json={"lines": [
            {"text_content": t, "line_order": i} for i, t in enumerate(texts)]})
        assert r.status_code == 200, r.text
    lines(a, [u"第一句", u"第二句"])
    lines(b, [u"我們去看恐龍"])
    lines(got, [u"拍到了"])
    lines(missed, [u"沒拍到"])
    r = s.put(BASE + "/workflows/%s" % b, headers=h, json={"transition_config": {"default_target_workflow_id": cam}})
    assert r.status_code == 200, r.text
    r = s.put(BASE + "/workflows/%s" % cam, headers=h, json={
        "transition_config": {"loop_mode": True,
                              "capture": {"count": 1, "delay_seconds": 3, "interval_seconds": 0.5}},
        "peripheral_hooks": [
            {"trigger": "sensor", "sensor_name": "capture_done", "target_workflow_id": got},
            {"trigger": "sensor", "sensor_name": "capture_failed", "target_workflow_id": missed}]})
    assert r.status_code == 200, r.text
    r = s.put(BASE + "/workflows/%s" % a, headers=h, json={"transition_config": {"default_target_workflow_id": w}})
    assert r.status_code == 200, r.text
    r = s.put(BASE + "/workflows/%s" % w, headers=h, json={"transition_config": {"loop_mode": True},
              "peripheral_hooks": [{"trigger": "asr_input", "utterance_match": u"恐龍", "target_workflow_id": b}]})
    assert r.status_code == 200, r.text

    body = Body()
    c = Client(Platform(BASE, key), body, cid, "nao27-live", listen=True,
               listen_seconds=0.3, silence_rms=100, mic_mode="stream",
               camera=True, camera_fps=4.0, encoder=("ppm", m.ppm))
    c.start()
    try:
        check("registered and connected over the ticketed socket", c.connected.wait(15))
        check("each line is said in order — the second only after the first was acked",
              wait(lambda: body.said[:2] == [u"第一句", u"第二句"]), repr(body.said))
        acks = [e for (e, _) in c.sent if e == "tts_finished"]
        check("one tts_finished per spoken line", wait(lambda: len([e for (e, _) in c.sent if e == "tts_finished"]) >= 2),
              repr(c.sent))
        check("nothing is heard while the robot spoke (mic_open honoured)",
              not [e for (e, _) in c.sent if e == "asr_input"], repr(c.sent))
        check("the waiting node opens the mic", wait(lambda: c.mic_open), "mic_open stayed False")
        check("the camera stays off on nodes that do not need vision", body.camera == [], repr(body.camera))
        check("the continuous microphone is subscribed at start", body.on_pcm is not None)
        stream_utterance(body, os.path.join(HERE, "utterance_16k.wav"))
        check("streamed microphone audio reaches the platform's own VAD (the server answers `hearing`)",
              wait(lambda: ("reaction", "hearing") in c.received, 10), repr(c.received[-8:]))
        check("audio went up as binary PCM on the session socket", c.audio_bytes_sent > 16000 * 2, str(c.audio_bytes_sent))
        body.loud = True
        c.listen_once()                      # the window path (NAO_MIC_MODE=window) still works
        check("a transcript sent up as asr_input moves the flow to the next node",
              wait(lambda: u"我們去看恐龍" in body.said), repr(body.said))
        check("the 拍照 node switches the camera on by itself",
              wait(lambda: "open" in body.camera, 15), repr(body.camera))
        check("frames reach the vision-gateway through the backend: the photo is taken (capture_done → 拍到了)",
              wait(lambda: u"拍到了" in body.said or u"沒拍到" in body.said, 25) and u"拍到了" in body.said,
              repr(body.said))
        check("camera frames went up as binary images", c.frames_sent >= 2, str(c.frames_sent))
        check("the camera switches off once the node no longer needs vision",
              wait(lambda: body.camera[-1:] == ["close"], 10), repr(body.camera))
        check("asr_input carried the text", (u"asr_input", u"我想看恐龍") in [(e, d) for (e, d) in c.sent],
              repr(c.sent))
        sessions = s.get(BASE + "/operator/sessions", headers=h).json()
        check("the session is listed for this account", any(x.get("session_id") == c.session_id for x in sessions),
              repr(sessions)[:200])
        del acks
    finally:
        c.stopping.set()
        if c.ws is not None:
            c.ws.close()
        s.delete(BASE + "/characters/%s" % cid, headers=h)

    web_binding(s, h, key)
    spoken_pairing(s, h)
    nao_card(s, h, key)
    stop_and_gesture(s, h, key)

    print("%d passed, %d failed" % (len(PASSED), len(FAILED)))
    return 1 if FAILED else 0


def web_binding(s, h, key):
    """captain 2026-10-08: 換角色不要 SSH. A NAO started WITHOUT NAO_CHARACTER
    shows up on the web as online and unbound, STANDS BY under the live
    console until the web gives it a character (實時中控 by default), runs what
    the web gives it, switches when the web changes it, and stands by again
    when the web releases it."""
    def one_line_character(name, line):
        cid = s.post(BASE + "/characters/", headers=h, json={"name": name, "robot_type": "nao"}).json()["id"]
        wf = s.post(BASE + "/characters/%s/workflows" % cid, headers=h, json={
            "name": u"待著", "node_type": "script", "sequence_order": 0,
            "transition_config": {"loop_mode": True}}).json()["id"]
        r = s.post(BASE + "/workflows/%s/scripts/bulk" % wf, headers=h, json={"lines": [
            {"text_content": line, "line_order": 0}]})
        assert r.status_code == 200, r.text
        return cid
    first = one_line_character(u"NAO 甲", u"我是甲")
    second = one_line_character(u"NAO 乙", u"我是乙")
    device = "nao27-bind-%d" % int(time.time())
    body = Body()
    body.gestures = []
    body.gesture = lambda name: body.gestures.append(name)
    c = Client(Platform(BASE, key), body, None, device, listen=False, camera=False)
    c.binding_poll_s = 1.0
    c.start()
    try:
        def listed():
            for d in s.get(BASE + "/robot-devices", headers=h).json():
                if d.get("device_id") == device:
                    return d
            return {}
        check("web: a NAO with no NAO_CHARACTER is listed online and unbound",
              wait(lambda: listed().get("online") and not listed().get("bound"), 15), repr(listed()))
        check("web: unbound, it stands by for the live console (a __standby__ session)",
              wait(lambda: c.connected.is_set() and c.character_id == m.STANDBY, 15), repr((c.session_id, c.character_id)))
        standby_sid = c.session_id
        listed_s = [x for x in s.get(BASE + "/operator/sessions", headers=h).json() if x.get("session_id") == standby_sid]
        check("web: the console lists it as a standby session",
              bool(listed_s) and listed_s[0].get("role_id") == m.STANDBY, repr(listed_s)[:200])
        r = s.post(BASE + "/operator/woz/fire", headers=h, json={
            "session_ids": [standby_sid], "text": u"控制台說的話", "motion": "animations/Stand/Gestures/Hey_1"})
        check("web: the console's line is said, with its NAO gesture",
              r.status_code == 200 and wait(lambda: u"控制台說的話" in body.said, 10)
              and "animations/Stand/Gestures/Hey_1" in body.gestures, repr((r.status_code, body.said, body.gestures)))
        r = s.put(BASE + "/robot-devices/%s" % device, headers=h, json={"character_id": first})
        check("web: binding it answers 200", r.status_code == 200, r.text[:200])
        check("web: bound, it starts the character the web gave it",
              wait(lambda: u"我是甲" in body.said, 20) and c.character_id == first, repr(body.said))
        s.put(BASE + "/robot-devices/%s" % device, headers=h, json={"character_id": second})
        check("web: rebinding switches it to the new character without touching the robot",
              wait(lambda: u"我是乙" in body.said, 25) and c.character_id == second, repr(body.said))
        s.delete(BASE + "/robot-devices/%s" % device, headers=h)
        check("web: releasing it ends the character and it stands by again",
              wait(lambda: c.character_id == m.STANDBY and c.connected.is_set(), 20), repr((c.session_id, c.character_id)))
    finally:
        c.stopping.set()
        if c.ws is not None:
            c.ws.close()
        s.delete(BASE + "/robot-devices/%s" % device, headers=h)
        for cid in (first, second):
            s.delete(BASE + "/characters/%s" % cid, headers=h)



class SlowBody(Body):
    """Speaks for up to `long_s` seconds unless 停止 cuts it, like NAO's
    blocking ALTextToSpeech.say; records gestures and stops."""

    def __init__(self, long_s=8.0):
        Body.__init__(self)
        self.long_s = long_s
        self.gestures = []
        self.stops = 0
        self.cut = threading.Event()

    def gesture(self, name):
        self.gestures.append((name, len(self.said)))

    def say(self, text):
        Body.say(self, text)
        self.cut.wait(self.long_s)
        self.cut.clear()

    def stop_all(self):
        self.cut_seq += 1
        self.stops += 1
        self.cut.set()


def stop_and_gesture(s, h, key):
    """The editor's ■ on a real NAO: POST /operator/sessions/{id}/stop sends
    `stop_now`; NAO cuts the line it is saying, never acks it, and the next ▶
    (繼續) starts the flow over. A NAO motion goes out WITH its line."""
    print("— 停止 and NAO gestures")
    char = s.post(BASE + "/characters/", headers=h, json={"name": u"NAO 停止測試", "robot_type": "nao"}).json()
    cid = char["id"]
    wf = s.post(BASE + "/characters/%s/workflows" % cid, headers=h, json={
        "name": u"打招呼", "node_type": "script", "sequence_order": 0}).json()["id"]
    r = s.post(BASE + "/workflows/%s/scripts/bulk" % wf, headers=h, json={"lines": [
        {"text_content": u"大家好，我是 NAO", "line_order": 0, "built_in_motion": "animations/Stand/Gestures/Hey_1"},
        {"text_content": u"第二句不該被說", "line_order": 1}]})
    assert r.status_code == 200, r.text
    body = SlowBody()
    c = m.Client(m.Platform(BASE, key), body, cid, "nao27-stop", listen=False, camera=False)
    c.start()
    try:
        check("stop: the line with a NAO motion is being said", wait(lambda: u"大家好，我是 NAO" in body.said, 15),
              repr(body.said))
        check("stop: the NAO motion went out with its line, before the line was said",
              ("animations/Stand/Gestures/Hey_1", 0) in body.gestures, repr(body.gestures))
        r = s.post(BASE + "/operator/sessions/%s/stop" % c.session_id, headers=h)
        check("stop: the editor's ■ is accepted", r.status_code == 200, r.text[:200])
        check("stop: NAO cuts what it is saying", wait(lambda: body.stops >= 1, 5), str(body.stops))
        time.sleep(1.5)
        check("stop: the cut line is never acked, and the next line is not said",
              not [e for (e, _) in c.sent if e == "tts_finished"] and u"第二句不該被說" not in body.said,
              repr((c.sent, body.said)))
        r = s.post(BASE + "/operator/sessions/%s/pause" % c.session_id, headers=h, json={"paused": False})
        check("stop: ▶ is accepted", r.status_code == 200, r.text[:200])
        check("stop: ▶ starts the flow over — the first line again",
              wait(lambda: body.said.count(u"大家好，我是 NAO") >= 2, 15), repr(body.said))
    finally:
        c.stopping.set()
        body.cut.set()
        if c.ws is not None:
            c.ws.close()
        s.delete(BASE + "/characters/%s" % cid, headers=h)


def _new_account(s, tag):
    email = "nao27_%s_%d@example.com" % (tag, int(time.time() * 1000))
    r = s.post(BASE + "/auth/register", json={"email": email, "password": "Nao27!2026", "display_name": tag})
    assert r.status_code == 201, r.text
    tok = s.post(BASE + "/auth/login", json={"email": email, "password": "Nao27!2026"}).json()["access_token"]
    return {"Authorization": "Bearer " + tok}


def spoken_pairing(s, h_a):
    """captain 2026-10-08: 一般使用者不碰終端機. A NAO with no key says a code;
    A types it and runs A's character; B holds its head, types the NEW code,
    and the NAO switches to B (it was in standby, so at once)."""
    import re
    h_b = _new_account(s, "B")

    def line_char(h, name, line):
        cid = s.post(BASE + "/characters/", headers=h, json={"name": name, "robot_type": "nao"}).json()["id"]
        wf = s.post(BASE + "/characters/%s/workflows" % cid, headers=h, json={
            "name": u"待著", "node_type": "script", "sequence_order": 0,
            "transition_config": {"loop_mode": True, "loop_min_seconds": 600}}).json()["id"]
        s.post(BASE + "/workflows/%s/scripts/bulk" % wf, headers=h, json={"lines": [{"text_content": line, "line_order": 0}]})
        return cid

    def latest_code(after):
        for t in reversed(body.said[after:]):
            m_ = re.search(u"配對碼是 ([0-9、]+)", t if isinstance(t, type(u"")) else t.decode("utf-8"))
            if m_:
                return m_.group(1).replace(u"、", u"")
        return None

    device = "nao27-pair-%d" % int(time.time())
    body = Body()
    c = Client(Platform(BASE), body, None, device, listen=False, camera=False)
    c.pair_poll_s = 0.5
    c.binding_poll_s = 1.0
    c.head_hold_s = 1.0
    c.start()
    try:
        check("pair: with no key it says a six-digit code and registers nothing",
              wait(lambda: latest_code(0), 15) and c.session_id is None, repr(body.said[-2:]))
        code = latest_code(0)
        r = s.post(BASE + "/auth/pair/claim", headers=h_a, json={"code": code})
        check("pair: A types the code on the web", r.status_code == 200, r.text[:200])
        check("pair: the NAO says it now works for A", wait(lambda: any(u"現在聽" in x for x in body.said), 15), repr(body.said[-2:]))
        a_char = line_char(h_a, u"A 的角色", u"我是 A 的機器人")
        s.put(BASE + "/robot-devices/%s" % device, headers=h_a, json={"character_id": a_char})
        check("pair: and runs A's character", wait(lambda: u"我是 A 的機器人" in body.said, 20), repr(body.said[-3:]))

        n = len(body.said)
        body.head = True
        ok = wait(lambda: latest_code(n), 15)
        body.head = False
        check("pair: B holds its head → a NEW code", ok and latest_code(n) != code, repr(body.said[n:]))
        r = s.post(BASE + "/auth/pair/claim", headers=h_b, json={"code": latest_code(n)})
        check("pair: B types it", r.status_code == 200, r.text[:200])
        b_char = line_char(h_b, u"B 的角色", u"我是 B 的機器人")
        check("pair: in standby it switches to B at once (the old session ends)",
              wait(lambda: c.session_id is None or u"現在聽" in "".join(body.said[n:]), 15), repr(body.said[n:]))
        s.put(BASE + "/robot-devices/%s" % device, headers=h_b, json={"character_id": b_char})
        check("pair: and runs B's character, as B", wait(lambda: u"我是 B 的機器人" in body.said, 25), repr(body.said[-3:]))
        listed_b = [d for d in s.get(BASE + "/robot-devices", headers=h_b).json() if d.get("device_id") == device]
        check("pair: the NAO is now on B's list", bool(listed_b), repr(listed_b))
    finally:
        c.stopping.set()
        if c.ws is not None:
            c.ws.close()



def nao_card(s, h, key):
    """captain 2026-10-08: 更新、重新啟動、自主行為、音量、看 log，都在網頁上."""
    import shutil, tempfile
    device = "nao27-card-%d" % int(time.time())
    tmp = tempfile.mkdtemp()
    home = os.path.join(tmp, "nao27")
    shutil.copytree(os.path.dirname(HERE), home, ignore=shutil.ignore_patterns("tests", "*.pyc"))
    with open(os.path.join(home, "README.md"), "a") as f:
        f.write("\nan older copy\n")          # so the platform's copy is newer
    body = Body()
    c = Client(Platform(BASE, key), body, None, device, listen=False, camera=False)
    c.binding_poll_s = 1.0
    c.home = home
    c.version = m.code_version(home)
    c.restarts = []
    c.exec_self = lambda: c.restarts.append(time.time())
    c.log_file = os.path.join(tmp, "nao.log")
    with open(c.log_file, "w") as f:
        f.write("[meme] connected\n[nao] led -> #3B82F6\n")
    c.start()
    try:
        def row():
            for d in s.get(BASE + "/robot-devices", headers=h).json():
                if d.get("device_id") == device:
                    return d
            return {}
        check("card: the NAO reports its version and kind on its poll",
              wait(lambda: (row().get("status") or {}).get("kind") == "nao", 15)
              and row()["status"].get("version") == c.version, repr(row()))
        latest = s.get(BASE + "/nao/version", headers=h).json().get("version")
        check("card: the platform's copy is a different (newer) version", latest and latest != c.version,
              repr((latest, c.version)))

        # Commands need a live session: with no character it stands by, which is one.
        check("card: standing by (a live session to send commands on)",
              wait(lambda: c.connected.is_set() and c.character_id == m.STANDBY, 20), repr(c.character_id))

        def cmd(command, value=None):
            body_ = {"command": command}
            if value is not None:
                body_["value"] = value
            return s.post(BASE + "/robot-devices/%s/command" % device, headers=h, json=body_)
        r = cmd("volume", 30)
        check("card: volume 30 reaches the NAO and is kept", r.status_code == 200
              and wait(lambda: getattr(body, "volume", None) == 30, 10) and c.settings.get("volume") == 30, r.text[:200])
        r = cmd("autonomy", True)
        check("card: 自主行為 on reaches the NAO and is kept", r.status_code == 200
              and wait(lambda: getattr(body, "autonomy", None) is True, 10) and c.settings.get("autonomy") is True)
        check("card: and the card sees both on the next poll",
              wait(lambda: (row().get("status") or {}).get("volume") == 30 and row()["status"].get("autonomy") is True, 15),
              repr(row().get("status")))
        r = cmd("log")
        check("card: 看 log uploads a bundle the panel lists",
              r.status_code == 200 and wait(lambda: any(
                  x.get("device_id") == device for x in s.get(BASE + "/device-logs", headers=h,
                                                             params={"device_id": device}).json()), 15), r.text[:200])
        r = cmd("update")
        check("card: 更新 swaps in the platform's copy and restarts into it",
              r.status_code == 200 and wait(lambda: c.restarts, 30)
              and m.code_version(home) == latest and os.path.isdir(home + ".prev")
              and os.path.exists(os.path.join(home, ".update_pending")),
              repr((m.code_version(home), latest)))
        check("card: a bad command is refused before it reaches the NAO", cmd("format-disk").status_code == 422)
    finally:
        c.stopping.set()
        if c.ws is not None:
            c.ws.close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
