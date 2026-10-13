"""程式節點 — your own Python code, asked by the platform while a robot runs.

A node with a 程式 (``Program(...)`` in a flow, or 程式 on the node in the web
editor) sends the session's variables to a program YOU run, the moment the
robot enters it. This module is that program's half::

    from socialrobot import Answer, SocialRobotClient

    def 挑下一題(call):                   # call.variables, call.node, call.session_id
        score = int(call.variables.get("分數", 0))
        if score >= 60:
            return Answer(exit="過關", say=f"{score} 分，過關了")
        return Answer(exit="再一題", variables={"題目": pick_question(score)})

    SocialRobotClient().serve({"挑下一題": 挑下一題})   # runs until Ctrl-C

``serve`` dials OUT to the platform (``WS /ws/programs`` with your API key), so
it runs on a laptop or a lab PC behind any router — no public URL. It answers
only sessions of robots on YOUR account. If it is not running, or takes longer
than the node's seconds, the node announces ``program_no_reply`` and the flow
takes that exit: write one.

A function may be ``async``. Whatever it returns becomes the answer: an
``Answer``, a dict with ``exit`` / ``say`` / ``variables``, a plain string (the
exit), or ``None`` (an empty answer). An exception becomes a no-reply whose
{{program_error}} is the exception's text — the robot never waits it out.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Union

import websockets

logger = logging.getLogger("socialrobot.program")


@dataclass
class ProgramCall:
    """What the platform asks: the node, the session, its variables."""
    step: str
    node: str
    session_id: str
    character_id: str
    variables: Dict[str, Any]
    id: str = ""


@dataclass
class Answer:
    """What to send back. Every field is optional.

    - ``exit``: written as {{program_exit}} — the node's 「程式回覆了」 rows
      compare it to pick where to go.
    - ``say``: written as {{program_say}} — a line can speak it.
    - ``variables``: written as {{name}} for the rest of the session (names
      are what a template reads: letters, digits, ``_``, one dot).
    """
    exit: Optional[str] = None
    say: Optional[str] = None
    variables: Dict[str, Any] = field(default_factory=dict)

    def frame(self) -> dict:
        out: Dict[str, Any] = {}
        if self.exit is not None:
            out["exit"] = str(self.exit)
        if self.say is not None:
            out["say"] = str(self.say)
        if self.variables:
            out["variables"] = {str(k): ("" if v is None else str(v)) for k, v in self.variables.items()}
        return out


Handler = Callable[[ProgramCall], Union[Any, Awaitable[Any]]]


def _as_answer(result: Any) -> Answer:
    if result is None:
        return Answer()
    if isinstance(result, Answer):
        return result
    if isinstance(result, str):
        return Answer(exit=result)
    if isinstance(result, Mapping):
        unknown = set(result) - {"exit", "say", "variables"}
        if unknown:
            raise TypeError(f"回傳的 dict 只能有 exit／say／variables，多了 {sorted(unknown)}")
        return Answer(exit=result.get("exit"), say=result.get("say"),
                      variables=dict(result.get("variables") or {}))
    raise TypeError(f"回傳 Answer、dict、字串或 None，收到 {type(result).__name__}")


async def answer_one(handlers: Mapping[str, Handler], frame: dict) -> dict:
    """One `call` frame → its `reply` frame. Never raises: a handler's error is
    the reply's `error`, so the robot hears about it instead of timing out."""
    rid = str(frame.get("id") or "")
    step = str(frame.get("step") or "")
    fn = handlers.get(step)
    if fn is None:
        return {"type": "reply", "id": rid, "error": f"這支程式沒有「{step}」"}
    call = ProgramCall(step=step, node=str(frame.get("node") or ""), session_id=str(frame.get("session_id") or ""),
                character_id=str(frame.get("character_id") or ""),
                variables=dict(frame.get("variables") or {}), id=rid)
    try:
        result = fn(call)
        if inspect.isawaitable(result):
            result = await result
        return {"type": "reply", "id": rid, **_as_answer(result).frame()}
    except Exception as e:
        logger.exception("program step %r failed", step)
        return {"type": "reply", "id": rid, "error": f"{type(e).__name__}: {e}"[:300]}


async def serve_async(client, handlers: Mapping[str, Handler], *, reconnect: bool = True,
                      ready: Optional[asyncio.Event] = None, retry_seconds: float = 3.0) -> None:
    """Connect and answer calls until cancelled. Reconnects after a drop
    (`reconnect=False` returns instead). A refused hello (a bad step name,
    a revoked key) raises — retrying would only be refused again."""
    from .runtime import _ws_base
    if not handlers:
        raise ValueError("serve() 要至少一個步驟：{\"名稱\": 函式}")
    steps = list(handlers)
    url = f"{_ws_base(client.base_url)}/programs?token={client.token}"
    while True:
        try:
            async with websockets.connect(url, open_timeout=10) as ws:
                await ws.send(json.dumps({"type": "hello", "steps": steps}))
                first = json.loads(await ws.recv())
                if first.get("type") != "ready":
                    raise RuntimeError(f"平台拒絕了這支程式：{first.get('error') or first}")
                logger.info("program ready: %s", ", ".join(steps))
                if ready is not None:
                    ready.set()
                tasks: set = set()
                async for raw in ws:
                    frame = json.loads(raw)
                    if frame.get("type") != "call":
                        if frame.get("type") == "error":
                            logger.warning("platform: %s", frame.get("error"))
                        continue

                    async def handle(f=frame):
                        await ws.send(json.dumps(await answer_one(handlers, f)))
                    t = asyncio.create_task(handle())
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
        except asyncio.CancelledError:
            raise
        except (OSError, websockets.ConnectionClosed, websockets.InvalidHandshake) as e:
            if isinstance(e, websockets.ConnectionClosed) and getattr(e, "code", None) in (4400, 4401):
                raise RuntimeError(f"平台拒絕了這支程式（{e.code}）：{getattr(e, 'reason', '')}") from e
            if not reconnect:
                return
            logger.warning("program connection lost (%s); retrying in %.0fs", e, retry_seconds)
        if ready is not None:
            ready.clear()
        if not reconnect:
            return
        await asyncio.sleep(retry_seconds)
