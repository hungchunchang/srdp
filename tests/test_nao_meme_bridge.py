"""Mocked-NAOqi unit tests for the meme→NAO bridge (issue #55).

Exercises `sdk/examples/nao_meme_bridge.py` against a fake `qi` module — no
real NAOqi SDK or NAO hardware is available in this environment. Covers
keyframe playback (`NaoBody.play_keyframes`), the `DryRunBody` fallback, and
`drive_down`'s precedence between `motor_keyframes` and the legacy
motion-name gesture.

Real-NAO hardware validation (actual `qi.Session` against a live robot) is
deferred — see issue #55.
"""
from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import MagicMock

import pytest

import nao_meme_bridge as bridge


# ── fake qi module ───────────────────────────────────────────────────────────

class FakeQiSession:
    """Stands in for qi.Session: records connect() and hands back a
    MagicMock proxy per service name, like the real NAOqi ALProxy objects."""

    def __init__(self):
        self.connected_to = None
        self.services = {}

    def connect(self, url):
        self.connected_to = url

    def service(self, name):
        return self.services.setdefault(name, MagicMock(name=f"proxy:{name}"))


@pytest.fixture
def fake_qi_session(monkeypatch):
    """Install a fake `qi` module in sys.modules for the test, mirroring how
    NaoBody lazily `import qi` inside __init__."""
    session = FakeQiSession()
    fake_module = types.ModuleType("qi")
    fake_module.Session = MagicMock(return_value=session)
    monkeypatch.setitem(sys.modules, "qi", fake_module)
    return session


def make_body(fake_qi_session):
    body = bridge.NaoBody(ip="127.0.0.1")
    assert body.session is fake_qi_session
    assert fake_qi_session.connected_to == "tcp://127.0.0.1:9559"
    return body


# ── NaoBody.play_keyframes ───────────────────────────────────────────────────

def test_play_keyframes_dispatches_setangles_per_keyframe(fake_qi_session, monkeypatch):
    sleeps = []
    monkeypatch.setattr(bridge.time, "sleep", lambda s: sleeps.append(s))

    body = make_body(fake_qi_session)
    body.play_keyframes([
        {"time": 0.0, "motors": {"HeadYaw": 0.0, "HeadPitch": 0.1}},
        {"time": 0.5, "motors": {"HeadYaw": 0.3}},
    ])

    assert body.motion.setAngles.call_count == 2
    body.motion.setAngles.assert_any_call(
        ["HeadYaw", "HeadPitch"], [0.0, 0.1], bridge.NAO_DEFAULT_SPEED)
    body.motion.setAngles.assert_any_call(
        ["HeadYaw"], [0.3], bridge.NAO_DEFAULT_SPEED)
    # paced by the delta between consecutive keyframe times (0.0 -> 0.5s)
    assert sleeps == [0.5]


def test_play_keyframes_skips_keyframe_with_no_motors(fake_qi_session, monkeypatch):
    monkeypatch.setattr(bridge.time, "sleep", lambda s: None)
    body = make_body(fake_qi_session)

    body.play_keyframes([
        {"time": 0.0, "motors": {}},
        {"time": 0.2, "motors": {"HeadYaw": 0.1}},
    ])

    body.motion.setAngles.assert_called_once_with(
        ["HeadYaw"], [0.1], bridge.NAO_DEFAULT_SPEED)


def test_play_keyframes_continues_after_setangles_failure(fake_qi_session, monkeypatch, capsys):
    monkeypatch.setattr(bridge.time, "sleep", lambda s: None)
    body = make_body(fake_qi_session)
    body.motion.setAngles.side_effect = [RuntimeError("boom"), None]

    body.play_keyframes([
        {"time": 0.0, "motors": {"HeadYaw": 9.0}},
        {"time": 0.1, "motors": {"HeadPitch": 0.1}},
    ])

    assert body.motion.setAngles.call_count == 2
    assert "boom" in capsys.readouterr().out


def test_play_keyframes_empty_list_is_a_noop(fake_qi_session):
    body = make_body(fake_qi_session)
    body.play_keyframes([])
    body.motion.setAngles.assert_not_called()


# ── DryRunBody.play_keyframes ────────────────────────────────────────────────

def test_dry_run_play_keyframes_prints_and_does_not_raise(capsys):
    body = bridge.DryRunBody()
    body.play_keyframes([{"time": 0.0, "motors": {"HeadYaw": 0.1}}])
    assert "1 motor keyframe" in capsys.readouterr().out


# ── drive_down: motor_keyframes vs motion precedence ─────────────────────────

class FakeSession:
    """Duck-types the RobotSession surface drive_down actually uses."""

    def __init__(self, events):
        self._events = events
        self.sent = []

    async def updates(self):
        for ev in self._events:
            yield ev

    async def send_event(self, name, *args):
        self.sent.append((name,) + args)


def test_drive_down_prefers_motor_keyframes_over_motion_name():
    keyframes = [{"time": 0.0, "motors": {"HeadYaw": 0.1}}]
    session = FakeSession([
        {"state": "SPEAKING",
         "data": {"text": "hi", "motion": "wave", "motor_keyframes": keyframes}},
    ])
    nao = MagicMock()

    asyncio.run(bridge.drive_down(session, nao))

    nao.play_keyframes.assert_called_once_with(keyframes)
    nao.gesture.assert_not_called()
    assert ("tts_finished",) in session.sent


def test_drive_down_falls_back_to_motion_name_without_keyframes():
    session = FakeSession([
        {"state": "SPEAKING", "data": {"text": "hi", "motion": "wave"}},
    ])
    nao = MagicMock()

    asyncio.run(bridge.drive_down(session, nao))

    nao.gesture.assert_called_once_with("wave")
    nao.play_keyframes.assert_not_called()


def test_drive_down_treats_empty_keyframes_list_as_absent():
    session = FakeSession([
        {"state": "SPEAKING",
         "data": {"text": "hi", "motion": "wave", "motor_keyframes": []}},
    ])
    nao = MagicMock()

    asyncio.run(bridge.drive_down(session, nao))

    nao.gesture.assert_called_once_with("wave")
    nao.play_keyframes.assert_not_called()


def test_drive_down_ignores_non_speaking_states():
    session = FakeSession([{"state": "LISTENING", "data": {"motion": "wave"}}])
    nao = MagicMock()

    asyncio.run(bridge.drive_down(session, nao))

    nao.gesture.assert_not_called()
    nao.play_keyframes.assert_not_called()
    assert session.sent == []
