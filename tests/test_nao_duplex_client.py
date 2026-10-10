"""The full-duplex NAO client, without a robot, a microphone or a network.

The decisions in it — which node may hear, what the gateway is told, what the backend is
told back — are the same ones the Android app makes in Kotlin. They are restated here in
Python, so each is pinned on its own: a drifted copy fails quietly (a mic open on a node
that asked for silence, a scripted node advanced by the gateway's reply).

The live half — a real gateway, a real backend, a recorded voice — is
`server/scripts/e2e_nao_duplex.py`.
"""
from __future__ import annotations

import asyncio
import json
import time

import numpy as np
import pytest

import nao_duplex_client as nd

ROLE = {
    "capabilities": {"audio": True},
    "prompts": {"system": "你是 NAO。"},
    "stages": [
        {"id": "script", "name": "開場"},
        {"id": "duplex", "name": "家教", "agent_config": {
            "system_prompt": "盤面：{{board}}", "voice_mode": "full_duplex",
            "output_judgments": [{"variable": "done", "description": "解完了嗎"}]}},
        {"id": "polite", "name": "不可打斷", "agent_config": {"system_prompt": "慢慢講", "voice_mode": "no_barge_in"}},
        {"id": "typed", "name": "文字管線", "agent_config": {"system_prompt": "打字", "voice_mode": "ws_pipeline"}},
    ],
}


# ── policy ──────────────────────────────────────────────────────────────────

def test_the_mic_may_open_only_on_a_gateway_voiced_ai_node():
    assert [nd.stage_allows_mic(ROLE, s) for s in ("script", "duplex", "polite", "typed", None, "gone")] == \
        [False, True, True, False, False, False]


def test_a_flow_with_no_such_node_never_opens_a_voice_channel():
    scripted = {"capabilities": {"audio": True}, "stages": [{"id": "a"}, ROLE["stages"][3]]}
    assert nd.flow_wants_gateway_voice(ROLE) is True
    assert nd.flow_wants_gateway_voice(scripted) is False
    assert nd.flow_wants_gateway_voice({**ROLE, "capabilities": {"audio": False}}) is False   # privacy hard-off


def test_the_prompt_is_persona_plus_the_rendered_node_prompt():
    # The bundle's copy still has its placeholder; the state_update's copy is the one to send.
    assert nd.compose_system_prompt(ROLE, "duplex", None) == "你是 NAO。\n\n盤面：{{board}}"
    assert nd.compose_system_prompt(ROLE, "duplex", "盤面：A4 Bačkis✓") == "你是 NAO。\n\n盤面：A4 Bačkis✓"
    assert nd.compose_system_prompt({}, None, None) == nd.FALLBACK_SYSTEM_PROMPT


def test_the_config_frame_says_only_what_it_knows():
    frame = nd.config_frame("p", [{"variable": "done"}], False)
    assert frame == {"type": "config", "max_tokens": 512, "system_prompt": "p",
                     "output_judgments": [{"variable": "done"}], "barge_in": False}
    # An empty judgments list would still flip the gateway into JSON mode; blank is omitted.
    assert nd.config_frame("", [], None) == {"type": "config", "max_tokens": 512}
    assert [nd.barge_in_for(nd.agent_config(ROLE, s)) for s in ("duplex", "polite", "script")] == [True, False, None]


def test_gateway_messages_become_the_backend_events_the_flow_needs():
    assert nd.backend_events({"type": "server.text", "role": "user", "text": "嗨"}) == [("voice_phase", "think")]
    assert nd.backend_events({"type": "server.text", "role": "assistant", "text": "你好"}) == [("voice_phase", "speak")]
    done = [("voice_phase", "listen"), ("tts_finished", "gateway_voice")]
    # The tag is load-bearing: an untagged tts_finished would advance a scripted node.
    assert nd.backend_events({"type": "server.speech_done"}) == done
    assert nd.backend_events({"type": "server.interrupt_ack"}) == done
    (event, data), = nd.backend_events({"type": "server.judgments", "judgments": {"done": "yes"}, "should_end": True})
    assert event == "agent_judgments" and json.loads(data) == {"judgments": {"done": "yes"}, "should_end": True}
    assert nd.backend_events({"type": "server.error", "error": "x"}) == []


def test_every_reason_to_close_the_mic_can_only_subtract():
    p = nd.MicPolicy()
    assert not p.enabled
    p.set_stage_allows(True); assert p.enabled
    p.begin_local_playback(); assert not p.enabled
    p.begin_local_playback(); p.end_local_playback(); assert not p.enabled   # refcounted
    p.end_local_playback(); assert p.enabled
    p.set_reply_mute(True); assert not p.enabled
    p.set_reply_mute(False); p.set_stage_allows(False); assert not p.enabled


# ── audio ───────────────────────────────────────────────────────────────────

def tone(frames: int, amplitude: int = 8000, hz: float = 440.0) -> list:
    t = np.arange(frames * nd.FRAME) / nd.RATE
    wave = (amplitude * np.sin(2 * np.pi * hz * t)).astype(np.int16)
    return [wave[i * nd.FRAME:(i + 1) * nd.FRAME] for i in range(frames)]


def test_the_speaker_plays_a_scripted_line_to_its_end_and_mixes_the_gateway_under_it():
    async def scenario():
        out = nd.NullOutput()
        speaker = nd.Speaker(out)
        try:
            for f in tone(5, 3000):
                speaker.push_gateway(f)
            started = time.monotonic()
            await speaker.play_local(tone(10, 8000))
            elapsed = time.monotonic() - started
            return out.frames, elapsed, speaker.talking
        finally:
            speaker.close()
    frames, elapsed, talking = asyncio.run(scenario())
    loud = [f for f in frames if np.abs(f).max() > 300]
    assert len(loud) == 10                              # the line, whole
    assert 0.15 <= elapsed <= 0.6                       # paced like a device: ten frames ≈ 200 ms
    assert np.abs(loud[0]).max() > 8500                 # the gateway's audio was summed in, not dropped
    assert talking


def test_a_barge_in_empties_the_reply_buffer():
    speaker = nd.Speaker(nd.NullOutput())
    try:
        for f in tone(20):
            speaker.push_gateway(f)
        speaker.clear_gateway()
        time.sleep(0.15)
        played = [f for f in speaker.device.frames if np.abs(f).max() > 300]
        assert len(played) <= 2                          # at most what was already on its way out
    finally:
        speaker.close()


def test_echo_cancellation_removes_the_robots_own_voice():
    """The claim full duplex rests on: what the speaker plays must not come back up the mic."""
    pytest.importorskip("livekit", reason="software echo cancellation needs `pip install livekit`")
    rng = np.random.default_rng(1)
    far = (rng.standard_normal(200 * nd.FRAME) * 5000).astype(np.int16)          # 4 s of "reply"
    delay = int(0.04 * nd.RATE)
    echo = np.concatenate([np.zeros(delay, np.int16), (far[:-delay] * 0.5).astype(np.int16)])
    aec = nd.EchoCanceller(delay_ms=40)
    residual = []
    for i in range(200):
        aec.render(far[i * nd.FRAME:(i + 1) * nd.FRAME])
        residual.append(aec.capture(echo[i * nd.FRAME:(i + 1) * nd.FRAME]))
    rms = lambda x: float(np.sqrt(np.mean(np.concatenate(x).astype(np.float64) ** 2)))   # noqa: E731
    before = rms([echo[100 * nd.FRAME:]])
    after = rms(residual[100:])                          # once it has converged
    assert after < before / 10, f"echo {before:.0f} → {after:.0f}: less than 20 dB removed"


# ── the client ──────────────────────────────────────────────────────────────

class FakeSession:
    def __init__(self, role):
        self.registration = {"role_config": role, "webrtc_config": []}
        self.sent = []

    async def send_event(self, event, data=None):
        self.sent.append((event, data))


class RecordingBody:
    kind = "recording"

    def __init__(self):
        self.said, self.moves = [], []

    def say(self, text):
        self.said.append(text)

    def gesture(self, name):
        self.moves.append(name)

    def play_keyframes(self, keyframes):
        self.moves.append(("keyframes", len(keyframes)))


class FakeVoice:
    def __init__(self, refusals=0):
        self.frames, self.refusals, self.attempts, self.closed = [], refusals, 0, 0

    async def connect(self, frame):
        self.attempts += 1
        if self.attempts <= self.refusals:
            raise RuntimeError("gateway refused the voice session: HTTP 503")
        self.frames.append(frame)

    def configure(self, frame):
        self.frames.append(frame)

    async def close(self):
        self.closed += 1


def make_client(role=ROLE):
    session, body = FakeSession(role), RecordingBody()
    speaker = nd.Speaker(nd.NullOutput())
    microphone = nd.Microphone(nd.ScriptedInput())
    client = nd.DuplexClient(session, body, speaker, microphone, "http://gateway", "token")
    client.voice = FakeVoice()
    return client, session, body


def test_a_scripted_line_is_said_and_acked_and_the_mic_is_shut_meanwhile():
    async def scenario():
        client, session, body = make_client()
        try:
            await client.on_state_update({"state": "LISTENING", "data": {"stage_id": "duplex", "agent_prompt": "盤面：空"}})
            open_on_ai_node = client.mic_open()
            await client.on_state_update({"state": "SPEAKING", "data": {"stage_id": "script", "text": "錯了。"}})
            await client._line
            return open_on_ai_node, client.mic_open(), body.said, session.sent, client.voice.frames
        finally:
            await client.close()
    open_on_ai, open_on_script, said, sent, frames = asyncio.run(scenario())
    assert open_on_ai is True and open_on_script is False
    assert said == ["錯了。"] and sent == [("tts_finished", None)]
    # Entering each node told the gateway who it is now — with the RENDERED prompt.
    assert frames[0]["system_prompt"] == "你是 NAO。\n\n盤面：空" and frames[0]["barge_in"] is True
    assert "barge_in" not in frames[1]


def test_the_gateways_own_speaking_phase_is_not_a_line_to_say():
    async def scenario():
        client, session, body = make_client()
        try:
            await client.on_state_update({"state": "SPEAKING", "data": {"stage_id": "duplex"}})
            await asyncio.sleep(0.05)
            return body.said, session.sent
        finally:
            await client.close()
    assert asyncio.run(scenario()) == ([], [])


def test_a_new_prompt_on_the_same_node_reaches_the_gateway():
    """A pushed answer rewrites the variables the tutor's prompt reads, without a node change."""
    async def scenario():
        client, _, _ = make_client()
        try:
            for prompt in ("診斷：", "診斷：", "診斷：方位＝隔壁"):
                await client.on_state_update({"state": "LISTENING", "data": {"stage_id": "duplex", "agent_prompt": prompt}})
            return [f["system_prompt"] for f in client.voice.frames]
        finally:
            await client.close()
    assert asyncio.run(scenario()) == ["你是 NAO。\n\n診斷：", "你是 NAO。\n\n診斷：方位＝隔壁"]


def test_gateway_messages_are_forwarded_and_a_barge_in_stops_the_reply():
    async def scenario():
        client, session, _ = make_client()
        try:
            await client.on_state_update({"state": "LISTENING", "data": {"stage_id": "polite"}})
            client._on_gateway_message({"type": "server.text", "role": "assistant", "text": "讓我講完"})
            muted = client.mic_open()
            for f in tone(20):
                client.speaker.push_gateway(f)
            client._on_gateway_message({"type": "server.interrupt_ack"})
            await asyncio.sleep(0.05)
            return muted, client.mic_open(), len(client.speaker._gateway), session.sent
        finally:
            await client.close()
    muted, reopened, backlog, sent = asyncio.run(scenario())
    assert muted is False and reopened is True           # no_barge_in: shut for the reply, then open
    assert backlog == 0
    assert sent == [("voice_phase", "speak"), ("voice_phase", "listen"), ("tts_finished", "gateway_voice")]


def test_a_reply_still_draining_does_not_talk_over_the_next_nodes_line():
    async def scenario():
        client, _, _ = make_client()
        try:
            await client.on_state_update({"state": "LISTENING", "data": {"stage_id": "duplex"}})
            for f in tone(10):
                client._on_gateway_audio(f)
            owned = len(client.speaker._gateway)
            await client.on_state_update({"state": "IDLE", "data": {"stage_id": "script"}})
            left = len(client.speaker._gateway)
            for f in tone(10):                               # the gateway has not stopped sending
                client._on_gateway_audio(f)
            return owned, left, len(client.speaker._gateway)
        finally:
            await client.close()
    owned, left, late = asyncio.run(scenario())
    assert owned > 0 and left == 0 and late == 0


def test_a_gateway_that_is_not_there_yet_is_retried_and_a_lost_one_is_reconnected():
    async def scenario():
        client, _, _ = make_client()
        client.voice = FakeVoice(refusals=2)
        client.VOICE_RETRY_SECONDS = 0.01
        try:
            client._start_voice()
            await asyncio.wait_for(client._voice_task, 2)
            up = (client.voice.attempts, len(client.voice.frames))
            client._voice_lost()                              # the gateway restarted under us
            await asyncio.sleep(0.05)
            await asyncio.wait_for(client._voice_task, 2)
            return up, client.voice.attempts
        finally:
            await client.close()
    up, after = asyncio.run(scenario())
    assert up == (3, 1)                                       # two refusals, then connected
    assert after == 4                                         # …and once more after the drop


def test_without_echo_cancellation_the_mic_ducks_while_the_robot_is_audible():
    async def scenario():
        client, _, _ = make_client()
        client.echo_mode = "duck"
        try:
            await client.on_state_update({"state": "LISTENING", "data": {"stage_id": "duplex"}})
            quiet = client.mic_open()
            for f in tone(10):
                client.speaker.push_gateway(f)
            await asyncio.sleep(0.1)
            during = client.mic_open()
            await asyncio.sleep(0.2 + nd.Speaker.TALKING_HOLD_SECONDS)
            return quiet, during, client.mic_open(), client.voice.frames[-1]["barge_in"]
        finally:
            await client.close()
    assert asyncio.run(scenario()) == (True, False, True, False)
