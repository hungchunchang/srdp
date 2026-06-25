"""Builder DSL — author characters in Python instead of the canvas.

Nodes are plain Python objects that reference each other directly (or by
node name for forward references). ``Character.deploy(client)`` compiles
them through the same REST API the visual editor uses, in two passes:
create every workflow to obtain ids, then patch in the cross-references
(jump targets, hook targets, judgment routes).

The ``transition_config`` shapes follow the backend's de-facto contract
(what registration_service compiles into runtime stages) — the API itself
stores them as unvalidated JSONB, so this module is where type-safety lives.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from .client import SocialRobotClient

#: Sentinel visual: render the user's webcam on the robot face.
Mirror = "__mirror__"

NodeRef = Union["Node", str, None]


# ── hooks ────────────────────────────────────────────────────────────────

@dataclass
class Hook:
    trigger: str
    goto: NodeRef = None
    sensor_name: Optional[str] = None
    write_to: Optional[str] = None
    timeout_seconds: Optional[int] = None
    api_config: Optional[dict] = None
    ws_config: Optional[dict] = None
    vision_config: Optional[dict] = None
    schedule_config: Optional[dict] = None

    def compile(self, resolve) -> dict:
        out: Dict[str, Any] = {"trigger": self.trigger}
        if self.sensor_name:
            out["sensor_name"] = self.sensor_name
        if self.goto is not None:
            out["target_workflow_id"] = resolve(self.goto)
        if self.write_to:
            out["write_to_variable"] = self.write_to
        if self.timeout_seconds is not None:
            out["timeout_seconds"] = self.timeout_seconds
        if self.api_config:
            out["api_config"] = self.api_config
        if self.ws_config:
            out["ws_config"] = self.ws_config
        if self.vision_config:
            out["vision_config"] = self.vision_config
        if self.schedule_config:
            out["schedule_config"] = self.schedule_config
        return out


def OnFace(goto: NodeRef = None, write_to: Optional[str] = None) -> Hook:
    return Hook("face_detected", goto=goto, write_to=write_to)


def OnSensor(name: Optional[str] = None, goto: NodeRef = None,
             write_to: Optional[str] = None) -> Hook:
    """Fires on any injected/sensor event (name=None) or that exact event
    name — including events pushed by external programs via
    POST /sessions/{id}/events."""
    return Hook("sensor", goto=goto, sensor_name=name, write_to=write_to)


def OnTimeout(seconds: int, goto: NodeRef = None) -> Hook:
    return Hook("timeout", goto=goto, timeout_seconds=seconds)


def OnApiPoll(url: str, *, path: str = "", equals: str = "", every: float = 2.0,
              method: str = "GET", json_body: Optional[dict] = None,
              headers: Optional[dict] = None, goto: NodeRef = None,
              write_to: Optional[str] = None) -> Hook:
    """Outbound interrupt: the backend polls `url` while the stage is
    active; the value at `path` (dot notation into the JSON body) is
    written to `write_to` every round, and the hook jumps to `goto` when
    it equals `equals`."""
    return Hook("api_poll", goto=goto, write_to=write_to, api_config={
        "url": url, "method": method, "json_body": json_body, "headers": headers,
        "poll_interval_seconds": every, "result_path": path, "fire_on_value": equals,
    })


def OnVision(model: str, fire_on_label: str, *, threshold: float = 0.7,
             every: float = 1.0, goto: NodeRef = None,
             write_to: Optional[str] = None) -> Hook:
    """Vision interrupt: the backend runs the custom vision model `model`
    (a VisionModel id from client.upload_vision_model(...)["id"]) on the
    session's live camera frame while the stage is active, writes the
    top label to `write_to` every round, and jumps to `goto` when it
    equals `fire_on_label`. The "watch continuously" counterpart to a
    Vision Switch."""
    return Hook("vision_detect", goto=goto, write_to=write_to, vision_config={
        "model_id": model, "fire_on_label": fire_on_label,
        "threshold": threshold, "poll_interval_seconds": every,
    })


def OnWebSocket(url: str, *, path: str = "", equals: str = "",
                subscribe: Optional[dict] = None, headers: Optional[dict] = None,
                goto: NodeRef = None, write_to: Optional[str] = None) -> Hook:
    """Streaming outbound interrupt: the backend keeps a websocket open to
    `url` (ws:// or wss://) while the stage is active, optionally sends
    `subscribe` once on connect, and on each JSON message writes the value
    at `path` to `write_to` — jumping to `goto` when it equals `equals`."""
    return Hook("ws_subscribe", goto=goto, write_to=write_to, ws_config={
        "url": url, "subscribe_message": subscribe, "headers": headers,
        "result_path": path, "fire_on_value": equals,
    })


def OnSchedule(goto: NodeRef = None, *, mode: str = "daily",
               at: Optional[str] = None, date: Optional[str] = None,
               every_seconds: Optional[int] = None,
               days_of_week: Optional[list] = None,
               timezone: str = "Asia/Taipei",
               write_to: Optional[str] = None) -> Hook:
    """Wall-clock scheduled interrupt: while the stage is active, fire at a
    time-of-day / interval and jump to `goto`.

      mode="daily",    at="15:00"                       — every day at 15:00
      mode="weekly",   at="09:00", days_of_week=[0,2,4] — Mon/Wed/Fri 09:00 (0=Mon)
      mode="interval", every_seconds=1800               — every 30 min
      mode="once",     at="14:00", date="2026-07-01"    — once at that instant

    `timezone` is an IANA name (default Asia/Taipei). Put it on a loop/idle WoZ
    stage; recurrence comes from the flow returning to that stage."""
    return Hook("schedule", goto=goto, write_to=write_to, schedule_config={
        "mode": mode, "at": at, "date": date, "every_seconds": every_seconds,
        "days_of_week": days_of_week or [], "timezone": timezone,
    })


# ── nodes ────────────────────────────────────────────────────────────────

@dataclass
class Node:
    name: str
    hooks: List[Hook] = field(default_factory=list)
    goto: NodeRef = None              # default next stage
    position: Optional[tuple] = None  # canvas (x, y); auto-grid if None
    #: escape hatch — extra transition_config keys merged verbatim
    extra_config: Dict[str, Any] = field(default_factory=dict)

    node_type = "woz"  # overridden

    def base_payload(self, resolve) -> dict:
        tc = self.transition_config(resolve)
        tc.update(self.extra_config)
        if self.goto is not None:
            tc["default_target_workflow_id"] = resolve(self.goto)
        return {
            "name": self.name,
            "node_type": self.node_type,
            "transition_config": tc,
            "peripheral_hooks": [h.compile(resolve) for h in self.hooks],
        }

    def transition_config(self, resolve) -> dict:
        return {}

    def post_deploy(self, client: SocialRobotClient, wf_id: str, resolve) -> None:
        """Per-node second-pass extras (scripts, agent-config)."""


@dataclass
class Say:
    text: str
    expression: Optional[str] = None      # expression UUID or Mirror
    motion: Optional[str] = None          # motion UUID
    built_in_motion: Optional[str] = None
    pause: float = 0.0
    audio: Optional[str] = None           # URL of an audio clip the robot plays on this line (≠ TTS)

    def compile(self, order: int) -> dict:
        line: Dict[str, Any] = {"text_content": self.text, "line_order": order,
                                "pause_duration": self.pause}
        if self.expression == Mirror:
            line["mirror_camera"] = True
        elif self.expression:
            line["expression_id"] = self.expression
        if self.motion:
            line["motion_id"] = self.motion
        if self.built_in_motion:
            line["built_in_motion"] = self.built_in_motion
        if self.audio:
            line["audio_clip_url"] = self.audio
        return line


@dataclass
class Woz(Node):
    lines: List[Say] = field(default_factory=list)
    loop: bool = False
    idle_expression: Optional[str] = None  # loop-mode idle visual (UUID or Mirror)
    idle_motion: Optional[str] = None
    # Min seconds per loop iteration before the lines repeat; the stage holds
    # its last visual silently for the remainder. Stops a loop WoZ from
    # re-speaking back-to-back (e.g. "比出 N 根手指" while watching the camera).
    loop_min_seconds: Optional[float] = None

    node_type = "woz"

    def transition_config(self, resolve) -> dict:
        if not self.loop:
            return {}
        tc: Dict[str, Any] = {
            "loop_mode": True,
            "expression_id": self.idle_expression,
            "motion_id": self.idle_motion,
        }
        if self.loop_min_seconds is not None:
            tc["loop_min_seconds"] = self.loop_min_seconds
        return tc

    def post_deploy(self, client, wf_id, resolve):
        if not self.lines:
            return
        compiled = [s.compile(i) for i, s in enumerate(self.lines)]
        client.bulk_scripts(wf_id, [
            {k: v for k, v in line.items() if k != "mirror_camera"} for line in compiled
        ])
        # Known backend gap: the bulk endpoint drops mirror_camera — patch
        # mirror lines individually after the bulk insert.
        mirror_orders = {l["line_order"] for l in compiled if l.get("mirror_camera")}
        if mirror_orders:
            for script in client.get_scripts(wf_id):
                if script["line_order"] in mirror_orders:
                    client.update_script(script["id"], {"mirror_camera": True})


@dataclass
class Judgment:
    variable: str
    description: str
    #: value → label  or  value → (label, target node/name)
    values: Dict[str, Any] = field(default_factory=dict)

    def compile(self, resolve) -> dict:
        out_values = []
        for value, spec in self.values.items():
            if isinstance(spec, tuple):
                label, target = spec
                out_values.append({"value": value, "label": label,
                                   "target_workflow_id": resolve(target)})
            else:
                out_values.append({"value": value, "label": spec})
        return {"variable": self.variable, "description": self.description,
                "values": out_values}


@dataclass
class DataSource:
    """One RAG knowledge source. Supply the text directly — pre-extract files /
    URLs however you like (this is the power-user path)."""
    name: str = ""
    text: str = ""
    kind: str = "text"        # text | file | url
    enabled: bool = True

    def compile(self) -> dict:
        import uuid as _uuid
        return {"id": str(_uuid.uuid4()), "name": self.name, "kind": self.kind,
                "enabled": self.enabled, "text": self.text, "source_ref": None,
                "fetched_at": None, "char_count": len(self.text or "")}


@dataclass
class Agent(Node):
    system_prompt: str = ""
    #: Free-text knowledge base (sugar for a single text data source). Retrieved
    #: at runtime (local RAG) and injected into the prompt.
    knowledge: str = ""
    #: How sources are queried: "keyword" (term overlap, no embedding service)
    #: or "embedding" (sentence-T5 cosine via the embed-gateway).
    knowledge_mode: str = "embedding"
    #: Multiple named knowledge sources. Takes precedence over `knowledge`.
    data_sources: List[DataSource] = field(default_factory=list)
    llm_model: str = "local_gateway"
    temperature: float = 0.7
    judgments: List[Judgment] = field(default_factory=list)
    #: phase → {"mode": "single|random", "expression_ids": [...], "motion_ids": [...]}
    expression_phases: Optional[Dict[str, dict]] = None
    #: #77 — let end-users set spoken reminders by voice on this AI stage.
    allow_runtime_authoring: bool = False

    node_type = "agent"

    def post_deploy(self, client, wf_id, resolve):
        end_conditions = None
        if self.expression_phases:
            end_conditions = {"expression_phases": self.expression_phases}
        sources = [d.compile() for d in self.data_sources]
        if not sources and self.knowledge:
            sources = [DataSource(name="知識", text=self.knowledge).compile()]
        client.put_agent_config(wf_id, {
            "system_prompt": self.system_prompt,
            "knowledge": None,
            "knowledge_mode": self.knowledge_mode,
            "data_sources": sources or None,
            "llm_model": self.llm_model,
            "temperature": self.temperature,
            "output_judgments": [j.compile(resolve) for j in self.judgments],
            "allow_runtime_authoring": self.allow_runtime_authoring,
            "end_conditions": end_conditions,
            # Hooks live at the workflow level (base_payload). Sending them
            # here too would double-register: the runtime merges
            # stage.peripheral_hooks + agent_config.peripheral_hooks.
            "peripheral_hooks": [],
        })


@dataclass
class Button:
    label: str
    goto: NodeRef = None
    id: Optional[str] = None  # stable id (clients reference it); auto btn-N

    def compile(self, idx: int, resolve) -> dict:
        return {"id": self.id or f"btn-{idx}", "label": self.label,
                "target_workflow_id": resolve(self.goto)}


@dataclass
class UI(Node):
    prompt: str = ""
    input_var: Optional[str] = None
    buttons: List[Button] = field(default_factory=list)

    node_type = "ui"

    def transition_config(self, resolve) -> dict:
        return {"ui_config": {
            "prompt_text": self.prompt,
            "show_input": bool(self.input_var),
            "input_variable": self.input_var,
            "buttons": [b.compile(i, resolve) for i, b in enumerate(self.buttons)],
        }}


@dataclass
class Identify(Node):
    known: NodeRef = None
    unknown: NodeRef = None
    default: NodeRef = None     # fallback; defaults to `unknown`
    timeout_seconds: Optional[int] = None
    login_method: str = "face_recognition"
    mode: str = "per_user"
    known_label: str = "已認識"
    unknown_label: str = "新使用者"

    node_type = "identify"

    def transition_config(self, resolve) -> dict:
        cfg: Dict[str, Any] = {
            "mode": self.mode,
            "login_method": self.login_method,
            "cases": [
                {"label": self.known_label, "match": "__known_user__",
                 "target_workflow_id": resolve(self.known)},
                {"label": self.unknown_label, "match": "__unknown_user__",
                 "target_workflow_id": resolve(self.unknown)},
            ],
            "default_target_workflow_id": resolve(self.default or self.unknown),
        }
        if self.timeout_seconds is not None:
            cfg["timeout_seconds"] = self.timeout_seconds
        return {"identify_config": cfg}


@dataclass
class Switch(Node):
    """Variable switch: branch on a session variable — typically primed by
    an external program via client.inject(variables={...}) or an OnApiPoll
    hook's write_to."""
    on: str = ""                                 # variable name
    #: match value → target, or → (label, target)
    cases: Dict[str, Any] = field(default_factory=dict)
    default: NodeRef = None

    node_type = "switch"

    def transition_config(self, resolve) -> dict:
        compiled = []
        for match, spec in self.cases.items():
            if isinstance(spec, tuple):
                label, target = spec
            else:
                label, target = match, spec
            compiled.append({"label": label, "match": match,
                             "target_workflow_id": resolve(target)})
        return {"switch_config": {
            "condition_type": "variable",
            "variable_name": self.on,
            "cases": compiled,
            "default_target_workflow_id": resolve(self.default),
        }}


@dataclass
class Vision(Node):
    """Custom-model vision switch: on stage entry the backend runs the
    uploaded model against the robot's current camera frame (gateway
    /v1/infer) and branches on the top label above `threshold`; everything
    below threshold takes `default`.

    `model` is a VisionModel id (from client.upload_vision_model(...)["id"]).
    `cases` keys are the model's labels.
    """
    model: str = ""                          # VisionModel id
    threshold: float = 0.7
    #: label → target, or → (display_label, target)
    cases: Dict[str, Any] = field(default_factory=dict)
    default: NodeRef = None

    node_type = "switch"

    def transition_config(self, resolve) -> dict:
        compiled = []
        for label, spec in self.cases.items():
            if isinstance(spec, tuple):
                disp, target = spec
            else:
                disp, target = label, spec
            compiled.append({"label": disp, "match": label,
                             "target_workflow_id": resolve(target)})
        return {"switch_config": {
            "condition_type": "vision",
            # registration_service reads metadata.model_url (= VisionModel id)
            # + metadata.threshold; mirrors what SwitchConfigEditor saves.
            "metadata": {"model_url": self.model, "model_type": "hosted",
                         "threshold": self.threshold},
            "cases": compiled,
            "default_target_workflow_id": resolve(self.default),
        }}


# ── character ────────────────────────────────────────────────────────────

@dataclass
class DeployedCharacter:
    id: str
    workflow_ids: Dict[str, str]   # node name → workflow id

    def workflow_id(self, node: Union[Node, str]) -> str:
        return self.workflow_ids[node.name if isinstance(node, Node) else node]


@dataclass
class Character:
    name: str
    nodes: List[Node]
    description: str = ""
    is_public: bool = False
    #: Let others view the flow read-only (beginner reference). None → defaults
    #: to is_public, so public demo characters are referenceable out of the box.
    blocks_public: Optional[bool] = None
    robot_type: str = "kebbi"

    def deploy(self, client: SocialRobotClient, replace: bool = True) -> DeployedCharacter:
        names = [n.name for n in self.nodes]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate node names: {names}")

        if replace:
            for c in client.list_characters():
                if c["name"] == self.name:
                    client.delete_character(c["id"])

        char = client.create_character(
            self.name, self.description, self.is_public, self.robot_type,
            blocks_public=(self.blocks_public if self.blocks_public is not None else self.is_public),
        )
        char_id = char["id"]

        # Pass 1 — create every workflow (no cross-references yet) to get ids.
        ids: Dict[str, str] = {}
        for i, node in enumerate(self.nodes):
            pos = node.position or (80 + (i % 4) * 280, 80 + (i // 4) * 200)
            wf = client.create_workflow(char_id, {
                "name": node.name,
                "node_type": node.node_type,
                "sequence_order": i,
                "canvas_position": {"x": pos[0], "y": pos[1]},
            })
            ids[node.name] = wf["id"]

        def resolve(ref: NodeRef) -> Optional[str]:
            if ref is None:
                return None
            key = ref.name if isinstance(ref, Node) else ref
            if key not in ids:
                raise ValueError(f"unknown node reference: {key!r} (nodes: {list(ids)})")
            return ids[key]

        # Pass 2 — patch in configs with resolved targets + per-node extras.
        for node in self.nodes:
            client.update_workflow(ids[node.name], node.base_payload(resolve))
            node.post_deploy(client, ids[node.name], resolve)

        return DeployedCharacter(id=char_id, workflow_ids=ids)
