"""Runtime client — register a robot session and drive it over the same
WebSocket the simulator/app use.

    client = SocialRobotClient(api_key="srk_…")
    async with await RobotSession.start(client, "Museum Guide") as s:
        await s.send_face(embedding=[0.1] * 128)
        ev = await s.expect(state="UI", timeout=10)
        await s.inject(variables={"yolo_result": "correct"})   # HTTP path
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional, Union

import websockets

from .client import SocialRobotClient


def _ws_base(base_url: str) -> str:
    """http(s)://host[/prefix]/api/v1 → ws(s)://host[/prefix]/ws"""
    return base_url.replace("http", "ws", 1).rsplit("/api/v1", 1)[0] + "/ws"


class ExternalStream:
    """Inbound streaming producer: a persistent authenticated WS to
    /ws/sessions/{id}/stream for pushing many events/variables into a live
    session (the streaming version of client.inject). For external systems,
    NOT the robot client.

        async with await client.open_stream(session_id) as stream:
            await stream.send(variables={"price": 42})
            await stream.send(event="order_ready")
    """

    def __init__(self, ws):
        self.ws = ws

    @classmethod
    async def open(cls, client, session_id: str) -> "ExternalStream":
        url = f"{_ws_base(client.base_url)}/sessions/{session_id}/stream?token={client.token}"
        ws = await websockets.connect(url, open_timeout=10)
        return cls(ws)

    async def send(self, event: Optional[str] = None,
                   variables: Optional[Dict[str, Any]] = None) -> dict:
        await self.ws.send(json.dumps({"event": event, "variables": variables}))
        return json.loads(await self.ws.recv())   # per-frame ack

    async def close(self):
        try:
            await self.ws.close()
        except Exception:
            pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()


class SubscribeStream:
    """Outbound robot→external event stream: a persistent authenticated WS to
    /ws/sessions/{id}/subscribe. Receive-only — `async for event in stream`
    yields the robot's events as dicts: ``{type:"event", event}`` and
    ``{type:"state", state, stage_name, stage_mode}``.

        async with await client.subscribe(session_id) as events:
            async for ev in events:
                print(ev)
    """

    def __init__(self, ws):
        self.ws = ws

    @classmethod
    async def open(cls, client, session_id: str) -> "SubscribeStream":
        url = f"{_ws_base(client.base_url)}/sessions/{session_id}/subscribe?token={client.token}"
        ws = await websockets.connect(url, open_timeout=10)
        return cls(ws)

    async def close(self):
        try:
            await self.ws.close()
        except Exception:
            pass

    def __aiter__(self):
        return self

    async def __anext__(self) -> dict:
        try:
            msg = await self.ws.recv()
        except Exception:
            raise StopAsyncIteration
        return json.loads(msg)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()


def _data(ev: dict) -> dict:
    return ev.get("data") or {}


def _prompt(ev: dict) -> str:
    return (_data(ev).get("ui_config") or {}).get("prompt_text") or ""


class RobotSession:
    """One live robot session. state_update messages stream into an internal
    queue; ``expect()`` consumes them in arrival order until a match."""

    def __init__(self, client: SocialRobotClient, registration: dict):
        self.client = client
        self.registration = registration
        self.session_id = registration["session_id"]
        self.ws: Optional[Any] = None
        self.events: List[dict] = []          # full state_update history
        self._queue: asyncio.Queue = asyncio.Queue()
        self._reader_task: Optional[asyncio.Task] = None

    # ---------- lifecycle ----------

    @classmethod
    async def start(cls, client: SocialRobotClient,
                    character: Union[str, dict], device_id: str = "sdk") -> "RobotSession":
        """`character` is a character id, a character dict, or a name
        (resolved against the caller's character list + public ones)."""
        char_id = None
        if isinstance(character, dict):
            char_id = character["id"]
        elif "-" in character and len(character) >= 32:
            char_id = character
        else:
            match = next((c for c in await asyncio.to_thread(client.list_characters)
                          if c["name"] == character), None)
            if not match:
                raise ValueError(f"character {character!r} not found")
            char_id = match["id"]

        reg = await asyncio.to_thread(client.register_robot, char_id, device_id)
        ws_base = _ws_base(client.base_url)
        session = cls(client, reg)
        session.ws = await websockets.connect(f"{ws_base}/{reg['session_id']}", open_timeout=10)
        await session._send_raw("client_ready")
        session._reader_task = asyncio.create_task(session._reader())
        return session

    async def close(self):
        if self._reader_task:
            self._reader_task.cancel()
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def _reader(self):
        try:
            async for m in self.ws:
                o = json.loads(m)
                if o.get("type") == "state_update":
                    self.events.append(o)
                    self._queue.put_nowait(o)
        except Exception:
            pass

    # ---------- sending ----------

    async def _send_raw(self, event: str, data: Optional[str] = None):
        msg: Dict[str, Any] = {"type": "event", "event": event,
                               "session_id": self.session_id, "timestamp": 0}
        if data is not None:
            msg["data"] = data
        await self.ws.send(json.dumps(msg))

    async def send_event(self, event: str, data: Any = None):
        """Send a client event over the robot WS (dict/list data is
        JSON-encoded the way the simulator does)."""
        if isinstance(data, (dict, list)):
            data = json.dumps(data)
        await self._send_raw(event, data)

    async def send_face(self, *, user_id: Optional[str] = None,
                        embedding: Optional[list] = None, confidence: float = 0.9,
                        times: int = 3, gap: float = 0.35):
        """Deliver face_detected like a real camera (repeated events — the
        Loop hook consumes the first; Identify resolves on a later one)."""
        payload = {"user_id": user_id, "embedding": embedding, "confidence": confidence}
        for i in range(times):
            await self.send_event("face_detected", payload)
            if i < times - 1:
                await asyncio.sleep(gap)

    async def inject(self, event: Optional[str] = None,
                     variables: Optional[Dict[str, Any]] = None) -> dict:
        """HTTP injection path (POST /sessions/{id}/events) — what an
        external program without this WS would use."""
        return await asyncio.to_thread(self.client.inject, self.session_id,
                                       event, variables)

    # ---------- awaiting ----------

    async def expect(self, *, state: Optional[str] = None,
                     stage_name: Optional[str] = None, stage_mode: Optional[str] = None,
                     text: Optional[str] = None, prompt: Optional[str] = None,
                     timeout: float = 10.0) -> Optional[dict]:
        """Consume state_updates until one matches every given criterion
        (substring match for text-ish fields). None on timeout."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                ev = await asyncio.wait_for(self._queue.get(), remaining)
            except asyncio.TimeoutError:
                return None
            d = _data(ev)
            if state is not None and ev.get("state") != state:
                continue
            if stage_name is not None and stage_name not in (d.get("stage_name") or ""):
                continue
            if stage_mode is not None and d.get("stage_mode") != stage_mode:
                continue
            if text is not None and text not in (d.get("text") or ""):
                continue
            if prompt is not None and prompt not in _prompt(ev):
                continue
            return ev

    # ---------- diagnostics ----------

    def tail(self, n: int = 8) -> list:
        return [(e.get("state"), _data(e).get("stage_name"),
                 (_data(e).get("text") or _prompt(e))[:24]) for e in self.events[-n:]]
