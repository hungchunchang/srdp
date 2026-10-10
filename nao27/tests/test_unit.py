# -*- coding: utf-8 -*-
"""meme_nao27 on Python 2.7 with a fake NAOqi (ALProxy records every call).
Run: docker run --rm -v $PWD/sdk/nao27:/c -w /c python:2.7-slim python -m unittest discover -s tests -p 'test_*.py'
"""
from __future__ import print_function

import base64
import json
import os
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import meme_nao27 as m  # noqa: E402


class Proxy(object):
    calls = []

    def __init__(self, name, ip, port):
        self.name = name

    def __getattr__(self, method):
        if method == "post":   # ALProxy.post.<method>(...) — the async form
            outer = self
            class _Post(object):
                def __getattr__(self, m):
                    def call(*args):
                        Proxy.calls.append((outer.name, "post." + m, args))
                    return call
            return _Post()
        def call(*args):
            Proxy.calls.append((self.name, method, args))
            if (self.name, method) == ("ALAudioRecorder", "stopMicrophonesRecording") and Proxy.wav is not None:
                with open(Proxy.wav_path, "wb") as f:
                    f.write(Proxy.wav)
            return None
        return call

    wav = None
    wav_path = "/tmp/meme_mic.wav"


class FakeWS(object):
    def __init__(self):
        self.out = []
        self.binary = []
        self.closed = False

    def close(self):
        self.closed = True

    def send(self, raw, opcode=None):
        if opcode == m.websocket.ABNF.OPCODE_BINARY:
            self.binary.append(raw)
        else:
            self.out.append(json.loads(raw))


class FakeVideo(object):
    """ALVideoDevice: getImageRemote's array is [w, h, layers, colourspace,
    sec, usec, pixels]."""

    def __init__(self, w=4, h=2):
        self.w, self.h = w, h
        self.subscribed = []
        self.unsubscribed = []
        self.stale = ["meme_cam_3", "someone_else_0"]

    def getSubscribers(self):
        return list(self.stale)

    def subscribeCamera(self, name, camera, resolution, colorspace, fps):
        self.subscribed.append((name, camera, resolution, colorspace, fps))
        return name + "_0"

    def getImageRemote(self, handle):
        return [self.w, self.h, 3, 11, 0, 0, b"\x10\x20\x30" * (self.w * self.h)]

    def unsubscribe(self, handle):
        self.unsubscribed.append(handle)


class FakePlatform(object):
    base = "https://robot.example/x/api/v1"

    def __init__(self, transcript=u"你好"):
        self.transcript = transcript
        self.heard = []

    def transcribe(self, wav, language):
        self.heard.append((len(wav), language))
        return self.transcript


def wav_of(level, seconds=0.2):
    n = int(16000 * seconds)
    pcm = struct.pack("<%dh" % n, *([level, -level] * (n // 2)))
    hdr = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack(
        "<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16) + b"data" + struct.pack("<I", len(pcm))
    return hdr + pcm


def client(platform=None, voice="auto"):
    Proxy.calls = []
    body = m.NaoBody("127.0.0.1", 9559, proxy=Proxy)
    c = m.Client(platform or FakePlatform(), body, "x", "nao-test", voice=voice,
                 listen_seconds=0.0, silence_rms=300)
    c.session_id = "s1"
    c.ws = FakeWS()
    c.connected.set()
    return c


def calls(name, method):
    return [a for (n, mt, a) in Proxy.calls if n == name and mt == method]


class Down(unittest.TestCase):
    def test_a_line_is_said_by_nao_and_acked_once(self):
        c = client()
        c.handle({"type": "state_update", "state": "SPEAKING", "data": {"text": u"歡迎光臨"}})
        self.assertEqual(calls("ALTextToSpeech", "say"), [(u"歡迎光臨".encode("utf-8"),)])
        self.assertEqual([e["event"] for e in c.ws.out], ["tts_finished"])

    def test_platform_audio_plays_instead_of_nao_voice_and_is_acked(self):
        c = client()
        audio = base64.b64encode(b"ID3" + b"\x00" * 64).decode()
        c.handle({"state": "SPEAKING", "data": {"text": u"你好", "audio": audio}})
        self.assertEqual(calls("ALAudioPlayer", "playFile"), [("/tmp/meme_tts.mp3",)])
        self.assertEqual(calls("ALTextToSpeech", "say"), [])
        self.assertEqual([e["event"] for e in c.ws.out], ["tts_finished"])

    def test_voice_nao_ignores_platform_audio(self):
        c = client(voice="nao")
        c.handle({"state": "SPEAKING", "data": {"text": u"你好", "audio": base64.b64encode(b"RIFF....").decode()}})
        self.assertEqual(calls("ALAudioPlayer", "playFile"), [])
        self.assertEqual(len(calls("ALTextToSpeech", "say")), 1)

    def test_unrecognised_audio_falls_back_to_nao_voice(self):
        c = client()
        c.handle({"state": "SPEAKING", "data": {"text": u"你好", "audio": base64.b64encode(b"????").decode()}})
        self.assertEqual(len(calls("ALTextToSpeech", "say")), 1)

    def test_keyframes_drive_setangles_and_motion_name_is_not_also_played(self):
        c = client()
        kf = [{"time": 0.0, "motors": {"HeadYaw": 0.5}},
              {"time": 0.01, "motors": {"LShoulderPitch": -1.0, "RShoulderPitch": -1.0}}]
        c.handle({"state": "SPEAKING", "data": {"text": u"看這邊", "motor_keyframes": kf, "motion": "nod"}})
        sa = calls("ALMotion", "setAngles")
        self.assertEqual(sa[0], (["HeadYaw"], [0.5], m.NAO_DEFAULT_SPEED))
        self.assertEqual(sorted(sa[1][0]), ["LShoulderPitch", "RShoulderPitch"])
        self.assertEqual(len(sa), 2)   # no gesture on top

    def test_a_motion_name_alone_plays_the_fallback_gesture(self):
        c = client()
        c.handle({"state": "SPEAKING", "data": {"motion": "nod"}})
        self.assertTrue(calls("ALMotion", "setAngles"))
        self.assertEqual(c.ws.out, [])   # nothing spoken, nothing to ack

    def test_non_speaking_updates_do_nothing(self):
        c = client()
        c.handle({"state": "IDLE", "data": {"text": u"x"}})
        self.assertEqual(calls("ALTextToSpeech", "say"), [])
        self.assertEqual(c.ws.out, [])


class Up(unittest.TestCase):
    def setUp(self):
        Proxy.wav = wav_of(3000)

    def tearDown(self):
        Proxy.wav = None

    def test_speech_while_the_mic_is_open_is_sent_as_asr_input(self):
        p = FakePlatform(u"我想看恐龍")
        c = client(p)
        self.assertEqual(c.listen_once(), u"我想看恐龍")
        self.assertEqual(c.ws.out[-1]["event"], "asr_input")
        self.assertEqual(c.ws.out[-1]["data"], u"我想看恐龍")
        self.assertEqual(p.heard[0][1], "zh")
        rec = calls("ALAudioRecorder", "startMicrophonesRecording")[0]
        self.assertEqual(rec[1:], ("wav", 16000, (0, 0, 1, 0)))

    def test_nothing_is_recorded_while_the_server_says_the_mic_is_shut(self):
        c = client()
        c.on_message(None, json.dumps({"type": "state_update", "state": "SPEAKING", "data": {"mic_open": False}}))
        self.assertIsNone(c.listen_once())
        self.assertEqual(calls("ALAudioRecorder", "startMicrophonesRecording"), [])

    def test_a_window_the_mic_was_shut_during_is_discarded(self):
        c = client()
        orig = c.body.record

        def record(seconds):
            wav = orig(seconds)
            # The robot spoke a line that started and ended inside the window.
            c.on_message(None, json.dumps({"type": "state_update", "data": {"mic_open": False}}))
            c.on_message(None, json.dumps({"type": "state_update", "data": {"mic_open": True}}))
            return wav
        c.body.record = record
        self.assertIsNone(c.listen_once())
        self.assertEqual(c.platform.heard, [])

    def test_a_silent_window_is_not_sent_to_the_recognizer(self):
        Proxy.wav = wav_of(20)
        c = client()
        self.assertIsNone(c.listen_once())
        self.assertEqual(c.platform.heard, [])


class Wire(unittest.TestCase):
    def test_ws_base_keeps_the_prefix(self):
        self.assertEqual(m.ws_base("https://h/ntu_social_robot/api/v1"), "wss://h/ntu_social_robot/ws")

    def test_the_socket_address_comes_from_the_base_url_not_the_registration(self):
        class P(FakePlatform):
            def character_id(self, wanted):
                return "c1"

            def register(self, cid, device_id):
                return {"session_id": "s9", "ws_ticket": "1.sig",
                        "websocket_url": "ws://localhost:6868/ws/s9"}
        c = client(P())
        c.register()
        self.assertEqual(c.ws_url, "wss://robot.example/x/ws/s9?ticket=1.sig")

    def test_events_follow_the_device_protocol(self):
        c = client()
        c.send("client_ready")
        e = c.ws.out[0]
        self.assertEqual((e["type"], e["event"], e["session_id"]), ("event", "client_ready", "s1"))

    def test_audio_formats_are_told_apart(self):
        self.assertEqual(m.audio_suffix(b"RIFFxxxx"), ".wav")
        self.assertEqual(m.audio_suffix(b"OggSxxxx"), ".ogg")
        self.assertEqual(m.audio_suffix(b"\xff\xfbxx"), ".mp3")
        self.assertIsNone(m.audio_suffix(b"????"))

    def test_the_shipped_ca_bundle_trusts_lets_encrypt(self):
        with open(m.CA_BUNDLE) as f:
            self.assertIn("ISRG Root X1", f.read())

class BindingPlatform(FakePlatform):
    """The server's /robot-devices/{id} and /robot/register, scripted."""
    def __init__(self, bindings):
        FakePlatform.__init__(self)
        self.bindings = list(bindings)
        self.registered = []

    def binding(self, device_id, status=None):
        return self.bindings.pop(0) if len(self.bindings) > 1 else self.bindings[0]

    def register(self, cid, device_id):
        self.registered.append(cid)
        return {"session_id": "s-" + cid, "ws_ticket": "t"}


UNBOUND = {"bound": False, "character_id": None, "character_name": None}


def bound(cid):
    return {"bound": True, "character_id": cid, "character_name": "role " + cid}


class WebBindingTest(unittest.TestCase):
    """captain 2026-10-08: 換 NAO 的角色不要 SSH——網頁的 個人資源 › 機器人
    指定，NAO 自己跟著換。"""

    def follower(self, platform):
        body = m.NaoBody("127.0.0.1", 9559, proxy=Proxy)
        return m.Client(platform, body, None, "nao-test", listen_seconds=0.0)

    def test_without_a_character_it_stands_by_for_the_live_console(self):
        p = BindingPlatform([UNBOUND])
        c = self.follower(p)
        self.assertTrue(c.follow_binding)
        self.assertTrue(c.register())
        self.assertEqual((p.registered, c.character_id), ([m.STANDBY], m.STANDBY))

    def test_a_character_in_the_trash_is_never_registered_it_stands_by(self):
        p = BindingPlatform([{"bound": True, "character_id": "c1", "character_name": None}])
        self.follower(p).register()
        self.assertEqual(p.registered, [m.STANDBY])

    def test_standing_by_then_bound_switches_to_the_character(self):
        p = BindingPlatform([UNBOUND, bound("c1")])
        c = self.follower(p)
        c.register()
        c.ws = FakeWS()
        self.assertTrue(c.check_binding_once())
        self.assertTrue(c.ws.closed)
        c.register()
        self.assertEqual(p.registered, [m.STANDBY, "c1"])

    def test_once_the_web_gives_one_it_registers_that_character(self):
        p = BindingPlatform([bound("c1")])
        c = self.follower(p)
        self.assertTrue(c.register())
        self.assertEqual((p.registered, c.character_id, c.session_id), (["c1"], "c1", "s-c1"))

    def test_a_changed_binding_ends_the_session_and_the_next_one_runs_the_new_character(self):
        p = BindingPlatform([bound("c1"), bound("c2")])
        c = self.follower(p)
        c.register()
        c.ws = FakeWS()
        self.assertTrue(c.check_binding_once())
        self.assertIsNone(c.session_id)
        self.assertTrue(c.ws.closed)
        self.assertTrue(c.register())
        self.assertEqual(p.registered, ["c1", "c2"])

    def test_the_same_binding_leaves_the_session_alone(self):
        p = BindingPlatform([bound("c1")])
        c = self.follower(p)
        c.register()
        c.ws = FakeWS()
        self.assertFalse(c.check_binding_once())
        self.assertEqual(c.session_id, "s-c1")
        self.assertFalse(c.ws.closed)

    def test_unbinding_on_the_web_ends_the_character_and_stands_by(self):
        p = BindingPlatform([bound("c1"), UNBOUND])
        c = self.follower(p)
        c.register()
        c.ws = FakeWS()
        self.assertTrue(c.check_binding_once())
        c.register()
        self.assertEqual(p.registered, ["c1", m.STANDBY])

    def test_playing_another_actor_in_the_same_character_re_registers(self):
        # 多演員專案: the server decides the actor at registration, so B ↔ A in
        # one project is a new session even though the character is the same.
        p = BindingPlatform([bound("c1"), dict(bound("c1"), actor="B")])
        c = self.follower(p)
        c.register()
        c.ws = FakeWS()
        self.assertTrue(c.check_binding_once())
        c.register()
        self.assertEqual((p.registered, c.actor), (["c1", "c1"], "B"))
        self.assertFalse(c.check_binding_once(), "the same actor again leaves the session alone")

    def test_a_named_nao_character_does_not_follow_the_web(self):
        c = m.Client(FakePlatform(), m.NaoBody("127.0.0.1", 9559, proxy=Proxy), "x", "nao-test")
        self.assertFalse(c.follow_binding)


class ErrorMessageTest(unittest.TestCase):
    def test_a_refused_request_says_its_status_not_a_codec_error(self):
        class R(object):
            status_code = 401
            text = u"API 金鑰無效"
        class H(object):
            headers = {}
            def request(self, *a, **kw):
                return R()
        p = m.Platform("https://x/api/v1", "srk_bad", http=H())
        with self.assertRaises(RuntimeError) as cm:
            p._req("GET", "/robot-devices/nao-1")
        self.assertIn("401", str(cm.exception))

    def test_logging_unicode_to_a_pipe_does_not_raise(self):
        m.log(u"[meme] 等待角色", RuntimeError("x"))


class NaoOwnMotionsAndLightsTest(unittest.TestCase):
    """captain 2026-10-08: NAO plays NAO's own gestures and lights its own
    LEDs (eyes, chest); it has no screen, so nothing about expressions."""

    def test_a_library_path_runs_on_alanimationplayer_not_the_head_fallback(self):
        c = client()
        c.handle({"state": "SPEAKING", "data": {"text": "你好", "motion": "animations/Stand/Gestures/Hey_1"}})
        names = [(n, meth) for (n, meth, _) in Proxy.calls]
        self.assertIn(("ALAnimationPlayer", "post.run"), names)
        self.assertIn(("ALAnimationPlayer", "post.run", ("animations/Stand/Gestures/Hey_1",)), Proxy.calls)
        self.assertNotIn(("ALMotion", "setAngles"), names)

    def test_the_gesture_starts_with_the_line_not_after_it(self):
        c = client(voice="nao")
        c.handle({"state": "SPEAKING", "data": {"text": "你好", "motion": "animations/Stand/Gestures/Hey_1"}})
        order = [(n, meth) for (n, meth, _) in Proxy.calls if (n, meth) in
                 (("ALAnimationPlayer", "post.run"), ("ALTextToSpeech", "say"))]
        self.assertEqual(order, [("ALAnimationPlayer", "post.run"), ("ALTextToSpeech", "say")])

    def test_a_refused_gesture_stands_nao_up_and_tries_once_more(self):
        c = client()
        tries = []
        class Anim(object):
            class post(object):
                @staticmethod
                def run(path):
                    tries.append(path)
                    if len(tries) == 1:
                        raise RuntimeError("posture family mismatch")
                    return 7
        c.body.anim = Anim()
        c.body.gesture("animations/Stand/Gestures/Hey_1")
        self.assertEqual(len(tries), 2)
        self.assertIn(("ALRobotPosture", "goToPosture", ("Stand", 0.5)), Proxy.calls)
        self.assertEqual(c.body._anim_id, 7)

    def test_stop_now_cuts_the_voice_drops_queued_lines_and_never_acks_the_cut_line(self):
        c = client(voice="nao")
        c.updates.put({"state": "SPEAKING", "data": {"text": "第二句"}})
        c.updates.put({"state": "IDLE", "data": {}})
        cut_during_say = []
        real_say = c.body.say
        def say(text):
            real_say(text)
            if not cut_during_say:
                cut_during_say.append(True)
                c.on_message(None, json.dumps({"type": "stop_now"}))   # arrives while NAO speaks
        c.body.say = say
        c.handle({"state": "SPEAKING", "data": {"text": ["第一句", "沒說到"]}})
        said_ = [m.to_str(a[0]) for a in calls("ALTextToSpeech", "say")]
        self.assertEqual(said_, ["第一句"], "the rest of a cut line is not said")
        self.assertTrue(calls("ALTextToSpeech", "stopAll") and calls("ALAudioPlayer", "stopAll"))
        self.assertNotIn("tts_finished", [json.loads(x).get("event") for x in c.ws.out])
        left = []
        while not c.updates.empty():
            left.append(c.updates.get().get("state"))
        self.assertEqual(left, ["IDLE"], "queued lines are dropped; the IDLE that follows stays")

    def test_a_color_lights_eyes_and_chest_once(self):
        c = client()
        c.on_message(None, json.dumps({"type": "state_update", "state": "IDLE",
                                       "data": {"led": {"mode": "color", "color": "#FF8800"}}}))
        c.on_message(None, json.dumps({"type": "state_update", "state": "SPEAKING",
                                       "data": {"led": {"mode": "color", "color": "#ff8800"}}}))
        fades = [args for (n, meth, args) in Proxy.calls if (n, meth) == ("ALLeds", "fadeRGB")]
        self.assertEqual(fades, [("FaceLeds", 0xFF8800, 0.2), ("ChestLeds", 0xFF8800, 0.2)])

    def test_off_is_black_and_none_hands_them_back(self):
        c = client()
        c.on_message(None, json.dumps({"type": "state_update", "state": "IDLE", "data": {"led": {"mode": "off"}}}))
        c.on_message(None, json.dumps({"type": "state_update", "state": "IDLE", "data": {"led": None}}))
        calls = [(meth, args[0]) for (n, meth, args) in Proxy.calls if n == "ALLeds"]
        self.assertIn(("fadeRGB", "FaceLeds"), calls)
        self.assertEqual([a for (m, a) in calls if m == "reset"], ["FaceLeds", "ChestLeds"])
        self.assertEqual([args[1] for (n, meth, args) in Proxy.calls if (n, meth) == ("ALLeds", "fadeRGB")], [0, 0])


class PairPlatform(FakePlatform):
    """POST /auth/pair/* scripted: approve after `after` polls."""
    def __init__(self, after=1, status="approved"):
        FakePlatform.__init__(self)
        self.after, self.status, self.polls = after, status, 0
        self.http = type("H", (), {"headers": {}})()
        self.token = None

    def pair_request(self, label):
        return {"code": u"481352", "secret": u"s3cret", "expires_in": 5}   # unicode, as JSON gives it

    def pair_poll(self, secret):
        self.polls += 1
        if self.polls >= self.after:
            return {"status": self.status, "token": "tok-" + secret, "user_name": u"林同學"}
        return {"status": "pending"}

    def set_token(self, t):
        self.token = t
        self.http.headers["Authorization"] = "Bearer " + t


def said(name="ALTextToSpeech"):
    return [to for (n, meth, args) in Proxy.calls if n == name and meth == "say" for to in args]


class SpokenCodePairingTest(unittest.TestCase):
    """captain 2026-10-08: 一般使用者不碰終端機——摸頭，NAO 念配對碼，網頁輸入就連上；
    換人也一樣，但只在待機時換。"""

    def fresh(self, platform):
        Proxy.calls = []
        c = m.Client(platform, m.NaoBody("127.0.0.1", 9559, proxy=Proxy), None, "nao-test", listen_seconds=0.0)
        c.pair_poll_s = 0.0
        return c

    def test_with_no_token_it_does_not_register_and_waits_to_be_paired(self):
        c = self.fresh(PairPlatform())
        self.assertFalse(c.has_token)

    def test_it_says_the_code_digit_by_digit_and_takes_the_token_of_whoever_typed_it(self):
        p = PairPlatform(after=2)
        c = self.fresh(p)
        got = c.pair_once()
        self.assertEqual(got, ("tok-s3cret", u"林同學"))
        self.assertTrue(any("4、8、1、3、5、2" in m.to_str(x) for x in said()), said())
        c.apply_token(*got)
        self.assertTrue(c.has_token and p.token == "tok-s3cret")
        self.assertTrue(any("現在聽" in m.to_str(x) for x in said()))

    def test_a_robot_locked_to_an_account_says_so_and_waits_before_asking_again(self):
        p = PairPlatform()
        def refused(label):
            raise RuntimeError("POST /auth/pair/request -> 403 locked")
        p.pair_request = refused
        c = self.fresh(p)
        waited = []
        c.stopping.wait = lambda s: waited.append(s)
        self.assertIsNone(c.pair_once())
        self.assertTrue(any("鎖定" in m.to_str(x) for x in said()), said())
        self.assertEqual(waited, [60.0], "no token: ask again a minute later, not five times a second")

    def test_an_expired_code_gives_nothing(self):
        self.assertIsNone(self.fresh(PairPlatform(status="expired")).pair_once())

    def test_a_touch_while_the_code_waits_says_it_again(self):
        p = PairPlatform(after=6)
        c = self.fresh(p)
        c.has_token = True
        touches = iter([True, True, False, True, False])   # still holding, let go, touch again
        c.body.head_held = lambda: next(touches, False)
        self.assertIsNotNone(c.pair_once())
        codes = [x for x in said() if "4、8、1、3、5、2" in m.to_str(x)]
        self.assertEqual(len(codes), 2, said())

    def test_an_expired_code_is_announced_when_the_head_is_what_asks_again(self):
        c = self.fresh(PairPlatform(status="expired"))
        c.has_token = True
        self.assertIsNone(c.pair_once())
        self.assertTrue(any("過期" in m.to_str(x) and "摸我的頭" in m.to_str(x) for x in said()), said())

    def test_without_a_key_an_expired_code_is_not_announced_the_next_one_follows(self):
        c = self.fresh(PairPlatform(status="expired"))
        self.assertIsNone(c.pair_once())
        self.assertFalse(any("過期" in m.to_str(x) for x in said()), said())

    def test_mid_interaction_it_says_so_and_switches_only_after(self):
        p = PairPlatform()
        c = self.fresh(p)
        c.session_id, c.last_state, c.last_state_at = "s1", "SPEAKING", m.time.time()
        def end_later():
            m.time.sleep(0.3)
            c.last_state = "IDLE"
        t = m.threading.Thread(target=end_later); t.start()
        c.apply_token("tok-2", u"陳同學")
        t.join()
        texts = [m.to_str(x) for x in said()]
        self.assertTrue(any("還在跟人說話" in x for x in texts), texts)
        self.assertIsNone(c.session_id, "the old session ends; the next one is the new person's")
        self.assertEqual(p.token, "tok-2")

    def test_the_head_is_read_from_almemory(self):
        b = m.NaoBody("127.0.0.1", 9559, proxy=Proxy)
        Proxy.calls = []
        b.head_held()
        self.assertIn(("ALMemory", "getData", (m.NaoBody.HEAD_KEY,)), Proxy.calls)

    def test_take_over_switches_autonomous_life_off_and_stands_up(self):
        b = m.NaoBody("127.0.0.1", 9559, proxy=Proxy)
        Proxy.calls = []
        b.take_over()
        names = [(n, meth) for (n, meth, _) in Proxy.calls]
        self.assertIn(("ALAutonomousLife", "setState"), names)
        self.assertIn(("ALMotion", "wakeUp"), names)
        self.assertIn(("ALRobotPosture", "goToPosture"), names)


class NaoCardTest(unittest.TestCase):
    """The 機器人 card: volume / autonomy kept across restarts, a status the
    card reads, and an update that never leaves a NAO without a working copy."""

    def setUp(self):
        import tempfile, shutil
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "nao27")
        os.makedirs(self.home)
        with open(os.path.join(self.home, "meme_nao27.py"), "w") as f:
            f.write("print('old')\n")
        with open(os.path.join(self.home, "meme.env"), "w") as f:
            f.write("export SR_BASE_URL=x\n")
        with open(os.path.join(self.home, "device_token"), "w") as f:
            f.write("tok")
        self.addCleanup(shutil.rmtree, self.tmp)

    def card(self, platform=None):
        c = client(platform)
        c.home = self.home
        c.version = m.code_version(self.home)
        c.restarted = []
        c.exec_self = lambda: c.restarted.append(True)
        return c

    def test_volume_and_autonomy_are_applied_kept_and_reported(self):
        c = self.card()
        c.handle_command({"type": "device_command", "command": "volume", "value": 35})
        c.handle_command({"type": "device_command", "command": "autonomy", "value": False})
        self.assertIn(("ALAudioDevice", "setOutputVolume", (35,)), Proxy.calls)
        st = c.status()
        self.assertEqual((st["volume"], st["autonomy"], st["kind"]), (35, False, "nao"))
        fresh = self.card()
        self.assertEqual(fresh.load_settings(), {"volume": 35, "autonomy": False})

    def tarball(self, source):
        import tarfile, io
        buf = io.BytesIO()
        tar = tarfile.open(fileobj=buf, mode="w:gz")
        for name, text in (("nao27/meme_nao27.py", source), ("nao27/README.md", "new")):
            data = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        tar.close()
        return buf.getvalue()

    def test_an_update_swaps_in_the_new_copy_keeps_the_old_one_and_this_nao_s_files(self):
        p = FakePlatform()
        p.nao_package = lambda: (self.tarball("print('new')\n"), "abcd1234")
        c = self.card(p)
        self.assertTrue(c.update())
        with open(os.path.join(self.home, "meme_nao27.py")) as f:
            self.assertIn("new", f.read())
        with open(os.path.join(self.home + ".prev", "meme_nao27.py")) as f:
            self.assertIn("old", f.read())
        for keep in ("meme.env", "device_token"):
            self.assertTrue(os.path.exists(os.path.join(self.home, keep)), keep)
        self.assertTrue(os.path.exists(os.path.join(self.home, ".update_pending")))
        self.assertTrue(os.path.exists(os.path.join(self.home, "run.sh")))
        self.assertEqual(c.restarted, [True])

    def test_a_package_that_does_not_compile_is_refused_and_nothing_moves(self):
        p = FakePlatform()
        p.nao_package = lambda: (self.tarball("def broken(:\n"), "bad00000")
        c = self.card(p)
        self.assertFalse(c.update())
        with open(os.path.join(self.home, "meme_nao27.py")) as f:
            self.assertIn("old", f.read())
        self.assertFalse(os.path.exists(self.home + ".prev"))
        self.assertEqual(c.restarted, [])

    def test_a_new_version_that_never_connects_puts_the_previous_one_back(self):
        import shutil
        shutil.copytree(self.home, self.home + ".prev")
        with open(os.path.join(self.home, ".update_pending"), "w") as f:
            f.write("x")
        c = self.card()
        c.connected.clear()        # it never reaches the platform
        c.watch_update(grace_s=0.1)
        m.time.sleep(0.5)
        self.assertEqual(c.restarted, [True])
        self.assertFalse(os.path.exists(self.home + ".prev"), "the previous copy is back in place")

    def test_a_new_version_that_connects_clears_the_marker(self):
        with open(os.path.join(self.home, ".update_pending"), "w") as f:
            f.write("x")
        c = self.card()
        c.connected.set()
        c.watch_update(grace_s=1.0)
        m.time.sleep(0.3)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".update_pending")))
        self.assertEqual(c.restarted, [])

    def test_the_version_ignores_this_nao_s_own_files(self):
        before = m.code_version(self.home)
        with open(os.path.join(self.home, "nao_settings.json"), "w") as f:
            f.write("{}")
        with open(os.path.join(self.home, "device_token"), "w") as f:
            f.write("another")
        self.assertEqual(m.code_version(self.home), before)


if __name__ == "__main__":
    unittest.main()


class StreamingTests(unittest.TestCase):
    """The continuous microphone (ALAudioDevice → binary PCM) and the camera
    (ALVideoDevice → binary image), both on the session socket."""

    def test_mic_buffers_go_up_as_binary_pcm_only_while_the_mic_is_open(self):
        c = client()
        pcm = struct.pack("<4h", 1, -1, 2, -2)
        c.on_pcm(pcm)
        self.assertEqual(c.ws.binary, [b"\x01" + pcm])
        self.assertEqual(c.audio_bytes_sent, len(pcm))
        c.on_message(None, json.dumps({"type": "state_update", "state": "SPEAKING", "data": {"mic_open": False}}))
        c.on_pcm(pcm)
        self.assertEqual(len(c.ws.binary), 1, "the robot never streams while it may not hear")
        c.connected.clear()
        c.mic_open = True
        c.on_pcm(pcm)
        self.assertEqual(len(c.ws.binary), 1, "nothing is sent while disconnected")

    def test_the_mic_subscription_asks_for_16k_mono_from_the_front_microphone(self):
        """NAOqi offers one channel only at 16 kHz; FRONT is 3."""
        Proxy.calls = []
        body = m.NaoBody("127.0.0.1", 9559, proxy=Proxy)
        import types
        naoqi = types.ModuleType("naoqi")

        class ALModule(object):
            def __init__(self, name):
                self.name = name
        naoqi.ALModule = ALModule
        naoqi.ALBroker = lambda *a: ("broker",) + a
        sys.modules["naoqi"] = naoqi
        try:
            how = body.start_mic_stream(lambda pcm: None, "127.0.0.1", 9559)
        finally:
            del sys.modules["naoqi"]
        self.assertEqual(how, "ALBroker")       # no `qi` in this container → the older path
        self.assertEqual(calls("ALAudioDevice", "setClientPreferences"), [("MemeMic", 16000, 3, 0)])
        self.assertEqual(calls("ALAudioDevice", "subscribe"), [("MemeMic",)])
        self.assertEqual(m.MemeMic.name, "MemeMic", "the module must be a global named like itself")
        self.assertTrue(type(m.MemeMic).processRemote.__doc__, "NAOqi binds only documented methods")

    def test_the_camera_runs_only_while_the_node_needs_vision_and_frames_go_up_as_images(self):
        c = client()
        video = FakeVideo()
        c.body.video = video
        c.encoder_name, c.encode = "ppm", m.ppm
        c.camera_fps = 2.0
        self.assertEqual(c.camera_step(), 0.2)
        self.assertEqual(video.subscribed, [], "no node has asked for vision yet")
        c.on_message(None, json.dumps({"type": "state_update", "state": "IDLE", "data": {"needs_visual": True}}))
        wait = c.camera_step()
        self.assertEqual(wait, 0.5)
        self.assertEqual(video.subscribed, [("meme_cam", 0, 1, 11, 2)])
        self.assertEqual(video.unsubscribed, ["meme_cam_3"], "our own stale subscription is cleared first")
        frame = c.ws.binary[-1]
        self.assertEqual(frame[:1], b"\x02")
        self.assertTrue(frame[1:].startswith(b"P6\n4 2\n255\n"))
        self.assertEqual(len(frame), 1 + len(b"P6\n4 2\n255\n") + 4 * 2 * 3)
        c.on_message(None, json.dumps({"type": "state_update", "state": "IDLE", "data": {"needs_visual": False}}))
        c.camera_step()
        self.assertEqual(video.unsubscribed[-1], "meme_cam_0", "the camera is released when the node no longer needs it")
        self.assertEqual(c.frames_sent, 1)

    def test_an_update_that_does_not_mention_vision_leaves_the_camera_as_it_was(self):
        c = client()
        c.on_message(None, json.dumps({"type": "state_update", "state": "IDLE", "data": {"needs_visual": True}}))
        c.on_message(None, json.dumps({"type": "state_update", "state": "SPEAKING", "data": {"text": "x"}}))
        self.assertTrue(c.camera_wanted)
        c.on_close(None)
        self.assertFalse(c.camera_wanted, "a dropped socket turns the camera off")

    def test_jpeg_is_preferred_when_this_python_can_encode_it(self):
        import types
        pil = types.ModuleType("PIL")
        image_mod = types.ModuleType("PIL.Image")
        pil.Image = image_mod
        sys.modules["PIL"], sys.modules["PIL.Image"] = pil, image_mod
        try:
            name, _ = m.pick_encoder()
        finally:
            del sys.modules["PIL"], sys.modules["PIL.Image"]
        self.assertEqual(name, "jpeg (PIL)")
        self.assertEqual(m.pick_encoder()[0], "ppm")


class NaoLedModesTest(unittest.TestCase):
    """呼吸、輪播、分部位 (2026-10-09): the same frame as the web and the Kebbi
    app; parts on their own ALLeds group; breathing on a thread until the next."""

    def test_the_frame_matches_the_web(self):
        br = {"mode": "breath", "color": "#FFFFFF", "period_ms": 2000}
        self.assertEqual(m.led_frame(br, 0), 0x262626)
        self.assertEqual(m.led_frame(br, 1000), 0xFFFFFF)
        cy = {"mode": "cycle", "colors": ["#FF0000", "#00FF00"], "period_ms": 2000}
        self.assertEqual([m.led_frame(cy, t) for t in (0, 999, 1000, 2500)], [0xFF0000, 0xFF0000, 0x00FF00, 0xFF0000])
        self.assertIsNone(m.led_frame({"mode": "cycle", "colors": ["#FF0000"]}, 0))
        self.assertEqual(m.led_frame({"mode": "off"}, 0), 0)

    def test_parts_light_their_own_group_and_the_feet_only_when_named(self):
        plan = dict(m.led_plan({"mode": "color", "color": "#0000FF",
                                "parts": {"chest": {"mode": "off"}, "feet": {"mode": "color", "color": "#FF0000"}, "tail": {"mode": "off"}}},
                               m.NaoBody.LED_GROUPS, m.NaoBody.PART_GROUPS))
        self.assertEqual(plan["FaceLeds"]["color"], "#0000FF")
        self.assertEqual(plan["ChestLeds"], {"mode": "off"})
        self.assertEqual(plan["FeetLeds"]["color"], "#FF0000")
        self.assertEqual(len(plan), 3)

    def test_breathing_runs_on_a_thread_and_stops_at_the_next_setting(self):
        b = m.NaoBody("127.0.0.1", 9559, proxy=Proxy)
        Proxy.calls = []
        b.set_led({"mode": "breath", "color": "#FF0000", "period_ms": 500})
        m.time.sleep(0.45)
        reds = set(args[1] for (n, meth, args) in Proxy.calls if (n, meth) == ("ALLeds", "fadeRGB"))
        self.assertGreater(len(reds), 2, "the color changes over time while it breathes")
        b.set_led({"mode": "off"})
        m.time.sleep(0.25)
        n = len(Proxy.calls)
        m.time.sleep(0.3)
        self.assertEqual(len(Proxy.calls), n, "nothing is sent after the next setting stopped it")
