"""Builder DSL — INTERNAL provisioning/test infrastructure, NOT public API.

Retired from the SDK's public surface (2026-08-07, captain decision): a
Builder script still has to `deploy()` its result up to the backend, so it
buys a designer nothing the web editor doesn't already do, and the canvas is
the surface that actually gets maintained. Authoring is the web editor's job.

This module stays in the repo — and is deliberately still importable from
`socialrobot` — because it is how the platform provisions its OWN fixture
characters: every `server/scripts/seed_*.py` (Museum, Origami, 搶答主持人,
聽力認知搶答主持人) and ~15 `server/scripts/e2e_*.py` drivers build their
characters with it. Deleting it would break `make e2e-demos`, `e2e-buzzer`,
`e2e-listening`, `e2e-loop-playback`, `e2e-trace` and most of the runtime
suite. Treat it as test infrastructure with no external compatibility
promise: it is absent from `__all__`, from the README, and from the package
description. Don't build anything new on it.

Original design notes follow.

Nodes are plain Python objects that reference each other directly (or by
node name for forward references). ``Character.deploy(client)`` compiles
them through the same REST API the visual editor uses, in two passes:
create every workflow to obtain ids, then patch in the cross-references
(jump targets, hook targets, judgment routes).

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
from typing import Any, Dict, List, Optional, Union, Sequence

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
    variables: Optional[Dict[str, str]] = None
    #: Named per-end-user counters to increment when this fires, e.g.
    #: {"exercise_done": 1}. Durable across sessions, read back as
    #: {{count_<name>}} once the user is recognised. Totals only — a streak's
    #: rules belong to your study, not to the platform.
    counters: Optional[Dict[str, int]] = None
    #: 記住 — {"breakfast_time": "{{breakfast_time}}"}: rendered on fire and
    #: written verbatim onto the recognised person's profile.attributes; read
    #: back as {{profile_breakfast_time}}. Deterministic where a memory slot is
    #: an LLM guess. Unresolved templates are not written.
    remember: Optional[Dict[str, str]] = None
    #: 通知 — {"line": "text", "line_image": "{{last_capture_url}}"}: pushed to
    #: the person's LINE and the character's line_config.push_to on fire.
    notify: Optional[Dict[str, str]] = None
    #: 紀錄 — {"kind": "meal", "label": "{{slot}}", "note": "…"}: one row on the
    #: person's event ledger, carrying the session's unfiled photos.
    #: (Every On* helper forwards `remember=` / `notify=` / `log_event=` here.)
    log_event: Optional[Dict[str, str]] = None
    #: 認識你 — {"name": "{{full_name}}", "call_name": "{{call_name}}"}: create
    #: the person, keep the face this session saw (only with the researcher's
    #: consent at 叫醒), and bind them as the character's primary user. Once
    #: per character, ever; {{enroll_result}} says what happened.
    #: Add ``"mode": "visitor"`` for 記住這個人 instead: the visitor seen this
    #: visit (face + this visit's face consent) becomes a person, never bound
    #: (for a screen's typed name, use ``Screen(remember_person=True)``).
    enroll: Optional[Dict[str, str]] = None
    timeout_seconds: Optional[int] = None
    #: Per-row 秒數 for trigger='timeout': a `{{col}}` template rendered at
    #: stage entry, so a data-driven loop waits its CURRENT row's own number
    #: (OnTimeout(8, timeout_var="{{rest_sec}}")). Falls back to
    #: timeout_seconds when the cell is missing or not a positive number, so
    #: keep timeout_seconds set as the default.
    timeout_var: Optional[str] = None
    api_config: Optional[dict] = None
    ws_config: Optional[dict] = None
    vision_config: Optional[dict] = None
    schedule_config: Optional[dict] = None
    match_variable: Optional[str] = None   # value condition: fire only when…
    match_value: Optional[str] = None      # …session var `match_variable` == this
    #: How the two are compared: None/"eq" is string equality (what every flow
    #: had before), "gt"/"gte"/"lt"/"lte" are NUMERIC and do not fall back to
    #: text — on text `"9" > "10"` is true, so a threshold that degraded to a
    #: string compare would fire at 9 for 「超過 10」 and look fine.
    match_op: Optional[str] = None
    # Wake-word / keyword rule for trigger='asr_input': fire only when the
    # normalized transcript CONTAINS this phrase (substring containment,
    # NFKC + casefold + whitespace/punctuation stripped server-side). None =
    # any utterance (legacy semantics). See OnUtterance().
    utterance_match: Optional[str] = None
    #: ↻ 下一列 — the NAME of the data structure (a Data node's structure) this
    #: transition moves to its next row right before it jumps, so the target
    #: node's {{columns}} render the new row. Put it on the edge that returns
    #: to the block start (e.g. OnWs("next_question", goto=play,
    #: advance_dataset="題庫")). Replaces the old dataset_binding.advance_on
    #: event name; a node that SPEAKS its bound structure still self-advances
    #: on its own completion without this.
    advance_dataset: Optional[str] = None

    def compile(self, resolve) -> dict:
        out: Dict[str, Any] = {"trigger": self.trigger}
        if self.sensor_name:
            out["sensor_name"] = self.sensor_name
        if self.utterance_match:
            out["utterance_match"] = self.utterance_match
        if self.match_variable:
            out["match_variable"] = self.match_variable
            out["match_value"] = self.match_value
            if self.match_op:
                out["match_op"] = self.match_op
        if self.goto is not None:
            out["target_workflow_id"] = resolve(self.goto)
        if self.write_to:
            out["write_to_variable"] = self.write_to
        if self.variables:
            out["variables"] = self.variables
        if self.counters:
            out["counters"] = self.counters
        if self.remember:
            out["remember"] = self.remember
        if self.notify:
            out["notify"] = self.notify
        if self.log_event:
            out["log_event"] = self.log_event
        if self.enroll:
            out["enroll"] = self.enroll
        if self.advance_dataset:
            out["advance_dataset"] = self.advance_dataset
        if self.timeout_seconds is not None:
            out["timeout_seconds"] = self.timeout_seconds
        if self.timeout_var:
            out["timeout_var"] = self.timeout_var
        if self.api_config:
            out["api_config"] = self.api_config
        if self.ws_config:
            out["ws_config"] = self.ws_config
        if self.vision_config:
            out["vision_config"] = self.vision_config
        if self.schedule_config:
            out["schedule_config"] = self.schedule_config
        return out


def OnFace(goto: NodeRef = None, write_to: Optional[str] = None, **hook_fields) -> Hook:
    """A face is seen. What it produces is written by the runtime under fixed
    names — `{{identity_kind}}` (`__known_user__` / `__unknown_user__`),
    `{{name}}`, `{{identity}}`, `{{identity_source}}`, `{{profile_*}}`,
    `{{count_*}}`. `write_to` is still accepted (older scripts) but the runtime
    IGNORES it on a face: it could only ever store the string "face_detected"."""
    return Hook("face_detected", goto=goto, write_to=write_to, **hook_fields)


def OnSensor(name: Optional[str] = None, goto: NodeRef = None,
             write_to: Optional[str] = None, when_var: Optional[str] = None,
             equals: Optional[str] = None, *,
             variables: Optional[Dict[str, str]] = None,
             counters: Optional[Dict[str, int]] = None,
             advance_dataset: Optional[str] = None, **hook_fields) -> Hook:
    """Fires on any injected/sensor event (name=None) or that exact event
    name — including events pushed by external programs via
    POST /sessions/{id}/events. With `when_var`/`equals` it also gates on a
    value: fires only when session variable `when_var` == `equals` — the way
    a branch on a variable is written (e.g. judged where result==correct → 答對).
    `match_op="gte"` (or gt/lt/lte) makes that comparison NUMERIC instead, for a
    threshold on a measurement: OnSensor("scale_weight", when_var="scale_weight",
    equals="200", match_op="gte")."""
    return Hook("sensor", goto=goto, sensor_name=name, write_to=write_to,
                match_variable=when_var, match_value=equals,
                variables=variables, counters=counters,
                advance_dataset=advance_dataset, **hook_fields)


def OnWs(name: Optional[str] = None, goto: NodeRef = None,
         write_to: Optional[str] = None, when_var: Optional[str] = None,
         equals: Optional[str] = None, *,
         advance_dataset: Optional[str] = None, **hook_fields) -> Hook:
    """Fires when a named signal is PUSHED in over the session websocket — a
    control panel / external program sending {event, variables} on
    WS /ws/sessions/{id}/stream (or POST /sessions/{id}/events). NOT a physical
    sensor. Same matching as OnSensor (signal name + optional `when_var`/`equals`
    value gate); use this for a controller/message source (e.g. the buzzer panel:
    OnWs("buzz"), OnWs("judged", when_var="result", equals="correct")).
    `advance_dataset="<structure>"` makes this the ↻ 下一列 edge of a data loop:
    OnWs("next_question", goto=play, advance_dataset="題庫")."""
    return Hook("ws", goto=goto, sensor_name=name, write_to=write_to,
                match_variable=when_var, match_value=equals,
                advance_dataset=advance_dataset, **hook_fields)


def OnUtterance(phrase: Optional[str] = None, goto: NodeRef = None,
                write_to: Optional[str] = None, *,
                variables: Optional[Dict[str, str]] = None,
                counters: Optional[Dict[str, int]] = None, **hook_fields) -> Hook:
    """An `asr_input` 中斷條件 — speech heard on this node fires the hook.

    `phrase` is the wake-word/keyword rule: the hook fires only when the
    normalized transcript CONTAINS the normalized phrase (zh-safe substring
    containment). None = any utterance (the legacy asr_input semantics).
    Keep phrases >= 2 characters — a single character over-fires — and add a
    second OnUtterance with an alternate spelling for homophone-prone words
    (「小飛」 vs 「小非」): several rule hooks on one node are keyword routing,
    evaluated specific-before-generic ahead of any rule-less catch-all. On a
    scripted (non-AI) node whose only listeners are rule hooks, a
    NON-matching utterance is dropped entirely — never recorded, never the
    LLM (bystander speech stays private)."""
    return Hook("asr_input", goto=goto, write_to=write_to, utterance_match=phrase,
                variables=variables, counters=counters, **hook_fields)


def OnTimeout(seconds: int, goto: NodeRef = None, *,
              variables: Optional[Dict[str, str]] = None,
              counters: Optional[Dict[str, int]] = None,
              timeout_var: Optional[str] = None,
              advance_dataset: Optional[str] = None, **hook_fields) -> Hook:
    """Fires N seconds after entering the node. `variables`/`counters` ride the
    transition exactly as on any other hook — a timeout branch is usually the
    "they didn't respond" arm, which is a measurement worth recording, not just
    an escape hatch.

    `timeout_var` makes the wait per-row: a `{{col}}` template rendered against
    the session variables at stage entry, so a data loop can hold each row for
    its own number of seconds (a rest that varies by exercise, a question whose
    thinking time varies by difficulty). `seconds` stays the default it falls
    back to, so the node always has an exit even when a cell is blank::

        OnTimeout(8, goto=lead, timeout_var="{{rest_sec}}",
                  advance_dataset="健康操動作表")
    """
    return Hook("timeout", goto=goto, timeout_seconds=seconds,
                variables=variables, counters=counters,
                timeout_var=timeout_var, advance_dataset=advance_dataset, **hook_fields)


def OnEnter(goto: NodeRef = None, *, when_var: Optional[str] = None,
            equals: Optional[str] = None, op: Optional[str] = None,
            variables: Optional[Dict[str, str]] = None,
            counters: Optional[Dict[str, int]] = None, **hook_fields) -> Hook:
    """進入時 — route by VALUE the moment the node is entered.

    A node's OnEnter hooks are read in the order they are listed against the
    session variables as they stand at entry; the first whose condition holds
    leaves the node at once (no line, no screen, no clock). One with no
    `when_var` is the 「其他」 row: put it last. A node none of whose rows hold
    simply runs as usual::

        route = Script("認識-分流", lines=[])
        route.hooks = [OnEnter(named, when_var="enroll_result", equals="enrolled"),
                       OnEnter(taken, when_var="enroll_result", equals="already_bound"),
                       OnEnter(retry)]

    `op` is the comparison (``eq`` default; ``gt``/``gte``/``lt``/``lte`` are
    numeric), and `equals` may name another variable as ``{{date}}`` — the same
    condition every other trigger takes. This replaced staggered value-gated
    timeouts (0.2 s, 0.4 s, …), whose seconds were only an order but read as
    timing on the canvas.
    """
    return Hook("enter", goto=goto, variables=variables, counters=counters,
                match_variable=when_var, match_value=equals, match_op=op, **hook_fields)


def OnPlaybackDone(goto: NodeRef = None, write_to: Optional[str] = None,
                    when_var: Optional[str] = None, equals: Optional[str] = None,
                    variables: Optional[Dict[str, str]] = None,
                    counters: Optional[Dict[str, int]] = None,
                    advance_dataset: Optional[str] = None, **hook_fields) -> Hook:
    """正常結束 (normal-end): fire when a node finishes normally — a loop-mode
    WoZ node has played ALL its lines to completion ("這段播完了 → 跳轉"), a
    non-loop WoZ node reaches the end of its script, or an AI/compose-llm node's
    reply ends with no judgment-jump / end_interaction override. The editor
    pins this as an always-present, non-removable exit condition (2026-07);
    the SDK still authors it as an ordinary Hook. `variables` rides the
    transition exactly like an inject's payload (e.g. the buzzer's
    next_question) — each value may be a `{{var}}` template, rendered against
    the session's current variables and written before the jump.

    On a loop-mode WoZ node this is the ONLY way to advance on plain playback
    completion — a loop otherwise cycles back to line 0 forever, and does so
    again here with no `goto` (or the hook omitted entirely): the loop's core
    semantic is unchanged, only wiring a target opts a specific iteration
    exit in. On a non-loop WoZ / AI / compose-llm node, omitting `goto` simply
    falls through to that node's normal default-next/sequential advance.

    Put it on a `Script(loop=True, ...)` node for the loop case. For the "播放完畢
    後，等待特定秒數 N，然後切換" variant, also set that node's `loop_min_seconds=N`
    — the exit then holds silently until the iteration has lasted N seconds,
    then jumps.
    `when_var`/`equals` refine it into a value-conditional exit, which is what
    the editor's 結束 tab authors: several 正常結束 rows, each gated on a
    judgment variable, resolved in authoring order with the UNCONDITIONAL row
    as the catch-all. This is how a branch on a variable is written now that
    the switch node is gone — put the specific rows first.

    `advance_dataset="<structure>"` is ↻ 下一列 on the normal end — "spoke this
    row, move to the next": the structure advances right before the jump, and
    on a loop-mode node with no `goto` the wrap honors it too (speak a row,
    wrap, speak the next).
    """
    return Hook("playback_done", goto=goto, write_to=write_to,
                match_variable=when_var, match_value=equals,
                variables=variables, counters=counters,
                advance_dataset=advance_dataset, **hook_fields)


def OnApiPoll(url: str, *, path: str = "", equals: str = "", every: float = 2.0,
              method: str = "GET", json_body: Optional[dict] = None,
              headers: Optional[dict] = None, goto: NodeRef = None,
              write_to: Optional[str] = None, **hook_fields) -> Hook:
    """Outbound interrupt: the backend polls `url` while the stage is
    active; the value at `path` (dot notation into the JSON body) is
    written to `write_to` every round, and the hook jumps to `goto` when
    it equals `equals`."""
    return Hook("api_poll", goto=goto, write_to=write_to, api_config={
        "url": url, "method": method, "json_body": json_body, "headers": headers,
        "poll_interval_seconds": every, "result_path": path, "fire_on_value": equals,
    }, **hook_fields)


def OnVision(model: str, fire_on_label: str, *, threshold: float = 0.7,
             every: float = 1.0, goto: NodeRef = None,
             write_to: Optional[str] = None,
             allowed_labels: Optional[Sequence[str]] = None, **hook_fields) -> Hook:
    """Vision interrupt: the backend runs the custom vision model `model`
    (a VisionModel id from client.upload_vision_model(...)["id"]) on the
    session's live camera frame while the stage is active, writes the
    top label to `write_to` every round, and jumps to `goto` when it
    equals `fire_on_label`. The "watch continuously" counterpart to a
    vision node, which is gone with the rest of the switch surface.

    `allowed_labels` situates the model in this step: while the stage is
    active the model chooses among THESE labels only (scores renormalised
    over them), so a step that waits for "step4" can name
    `["step3", "step4"]` and stop a look-alike "step2" from stealing the
    argmax. It must include at least one label that does NOT fire — a set
    of only firing labels would fire on the first frames regardless of what
    the camera sees, and the backend refuses it (runs unrestricted, WARNs).
    `fire_on_label` is always included. None/empty = every label."""
    cfg = {
        "model_id": model, "fire_on_label": fire_on_label,
        "threshold": threshold, "poll_interval_seconds": every,
    }
    if allowed_labels:
        cfg["allowed_labels"] = list(allowed_labels)
    return Hook("vision_detect", goto=goto, write_to=write_to, vision_config=cfg, **hook_fields)


def OnWebSocket(url: str, *, path: str = "", equals: str = "",
                subscribe: Optional[dict] = None, headers: Optional[dict] = None,
                goto: NodeRef = None, write_to: Optional[str] = None, **hook_fields) -> Hook:
    """Streaming outbound interrupt: the backend keeps a websocket open to
    `url` (ws:// or wss://) while the stage is active, optionally sends
    `subscribe` once on connect, and on each JSON message writes the value
    at `path` to `write_to` — jumping to `goto` when it equals `equals`."""
    return Hook("ws_subscribe", goto=goto, write_to=write_to, ws_config={
        "url": url, "subscribe_message": subscribe, "headers": headers,
        "result_path": path, "fire_on_value": equals,
    }, **hook_fields)


def OnSchedule(goto: NodeRef = None, *, mode: str = "daily",
               at: Optional[str] = None, date: Optional[str] = None,
               every_seconds: Optional[int] = None,
               days_of_week: Optional[list] = None,
               timezone: str = "Asia/Taipei",
               write_to: Optional[str] = None,
               at_var: Optional[str] = None,
               grace_minutes: int = 0, **hook_fields) -> Hook:
    """Wall-clock scheduled interrupt: while the stage is active, fire at a
    time-of-day / interval and jump to `goto`.

      mode="daily",    at="15:00"                       — every day at 15:00
      mode="weekly",   at="09:00", days_of_week=[0,2,4] — Mon/Wed/Fri 09:00 (0=Mon)
      mode="interval", every_seconds=1800               — every 30 min
      mode="once",     at="14:00", date="2026-07-01"    — once at that instant

    `timezone` is an IANA name (default Asia/Taipei). Put it on a loop/idle WoZ
    stage; recurrence comes from the flow returning to that stage.

    `at_var="{{profile_breakfast_time}}"` reads THIS person's time from the
    session variables at stage entry and beats `at` when it is a clock time
    (keep `at` as the fallback). `grace_minutes=30` fires a daily/weekly time
    that went by while the flow was away from this stage, once per day.
    Extra keyword fields (`notify=`, `variables=`, …) go onto the hook."""
    return Hook("schedule", goto=goto, write_to=write_to, schedule_config={
        "mode": mode, "at": at, "at_var": at_var, "date": date, "every_seconds": every_seconds,
        "days_of_week": days_of_week or [], "timezone": timezone,
        "grace_minutes": grace_minutes,
    }, **hook_fields)


# ── nodes ────────────────────────────────────────────────────────────────

@dataclass
class Node:
    name: str
    hooks: List[Hook] = field(default_factory=list)
    goto: NodeRef = None              # default next stage
    position: Optional[tuple] = None  # canvas (x, y); auto-grid if None
    #: escape hatch — extra transition_config keys merged verbatim
    extra_config: Dict[str, Any] = field(default_factory=dict)
    #: 子流程 membership — set by `subflow()` / `Return()`, not by hand: the
    #: definition's ENTRY node (the entry names itself), its name, and for a
    #: 回傳 node the name of the exit it stands for.
    member_of: NodeRef = None
    subflow_name: Optional[str] = None
    return_name: Optional[str] = None
    #: 燈光 (Kebbi's four LEDs) while the robot is on this node:
    #: ``{"mode": "color", "color": "#34C759"}`` or ``{"mode": "off"}``; None =
    #: the robot's own lights. An Agent's phases take their own the same way,
    #: as ``expression_phases={"think": {"led": {...}}}``, over this one.
    led: Optional[Dict[str, str]] = None

    node_type = "script"  # overridden

    def base_payload(self, resolve) -> dict:
        tc = self.transition_config(resolve)
        tc.update(self.extra_config)
        if self.goto is not None:
            tc["default_target_workflow_id"] = resolve(self.goto)
        if self.led:
            tc["led"] = dict(self.led)
        if self.member_of is not None:
            sf: Dict[str, Any] = {"of": resolve(self.member_of), "name": self.subflow_name or self.name}
            if self.return_name:
                sf["return"] = self.return_name
            tc["subflow"] = sf
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
class Data(Node):
    """A non-executable data node holding one or more NAMED structures (list /
    table / dict) referenced by loops and templates — not a stage the robot
    enters. Row 0's columns are injected as {{column}} variables at interaction
    start; the row moves when a hook carrying advance_dataset='<structure
    name>' fires (↻ 下一列) — put it on the transition that consumes the row
    last: an event/timeout hook on a wait node, or OnPlaybackDone on the node
    that speaks the row. No per-node binding is needed (or possible) any more.

    Single-structure sugar — the node's own name names the structure::

        Data("題庫", kind="table", value=rows)        # one structure named 題庫

    Multiple structures in one node::

        Data("題庫", structures=[
            {"name": "簡單題", "kind": "table", "value": easy},
            {"name": "困難題", "kind": "table", "value": hard},
        ])
    """
    kind: str = "table"          # 'list' | 'table' | 'dict'
    value: Any = None
    structures: Optional[List[dict]] = None  # [{name, kind, value[, order]}]; overrides kind/value
    # 抽取順序: 'random' = shuffled draws (every row once per round, reshuffle
    # never repeats the row it just left, the FIRST row is random too);
    # anything else = sequential row 0 → ↻. See services/infra/dataset.py.
    order: Optional[str] = None
    #: Declared project variables (the editor's 變數 panel): [{name, value,
    #: description?, persist?}]. `persist: True` keeps the value across
    #: sessions on the character (跨場次保留) — no recognised person needed.
    #: A node carrying only `variables` declares no structure.
    variables: Optional[List[dict]] = None

    node_type = "data"

    def transition_config(self, resolve) -> dict:
        def _st(name, kind, value, order):
            st = {"name": name, "kind": kind, "value": value}
            if order == "random":
                st["order"] = "random"
            return st
        tc: Dict[str, Any] = {}
        if self.structures:
            tc["structures"] = [
                _st(s.get("name") or self.name, s.get("kind", "table"), s.get("value"), s.get("order"))
                for s in self.structures
            ]
        elif self.value is not None or not self.variables:
            tc["structures"] = [_st(self.name, self.kind, self.value, self.order)]
        if self.variables:
            tc["variables"] = [dict(v) for v in self.variables]
        return tc


@dataclass
class Call(Node):
    """One use of a 子流程: at registration this node is replaced by a copy of
    the definition whose entry is `subflow`. `exits` maps each 回傳 name to
    where that copy goes next; an unmapped one takes this node's `goto`. The
    node's own `hooks` are interrupt conditions on EVERY step of the copy
    (an OnPlaybackDone is not — it would cut the subflow into single steps).
    """
    subflow: NodeRef = None
    exits: Dict[str, NodeRef] = field(default_factory=dict)

    def transition_config(self, resolve) -> dict:
        return {"call": {"subflow": resolve(self.subflow), "exits": [
            {"name": k, "target_workflow_id": resolve(v)} for k, v in self.exits.items()]}}


def Return(label: str, of: "Node", position: Optional[tuple] = None) -> "Script":
    """A 回傳 of the definition entered at `of`: an edge into it leaves the
    copy through the calling node's exit named `label`."""
    return Script(f"回傳：{label}", member_of=of, subflow_name=of.subflow_name,
                  return_name=label, position=position)


def subflow(name: str, entry: "Node", members: List["Node"]) -> "Node":
    """Make `entry` + `members` one 子流程 definition named `name`; returns
    the entry, which is what a `Call(subflow=...)` names. Create `Return`s
    AFTER this, so they inherit the name. The members never run on their own —
    only as a call's copy (spec: devlog/notes/subflow-2026-10.md).
    """
    for n in [entry, *members]:
        n.member_of, n.subflow_name = entry, name
    return entry


@dataclass
class Say:
    text: str
    expression: Optional[str] = None      # expression UUID or Mirror
    motion: Optional[str] = None          # motion UUID
    built_in_motion: Optional[str] = None
    pause: float = 0.0
    audio: Optional[str] = None           # asset:<id> / {{col}} / url of a clip the robot plays (≠ TTS)
    expression_var: Optional[str] = None  # RobotPerform per-row: a {{col}} resolving to an expression NAME
    motion_var: Optional[str] = None      # RobotPerform per-row: a {{col}} resolving to a motion NAME
    #: 等影片播完 — end this step at max(speech, expression video) instead of
    #: when the speech ends. Needs a FIXED `expression`: a per-row
    #: `expression_var` is a different clip each round, with no one length.
    hold_for_visual: bool = False

    def compile(self, order: int) -> dict:
        line: Dict[str, Any] = {"text_content": self.text, "line_order": order,
                                "pause_duration": self.pause}
        if self.hold_for_visual:
            line["hold_for_visual"] = True
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
        if self.expression_var:
            line["expression_var"] = self.expression_var
        if self.motion_var:
            line["motion_var"] = self.motion_var
        return line


@dataclass
class Script(Node):
    lines: List[Say] = field(default_factory=list)
    loop: bool = False
    idle_expression: Optional[str] = None  # loop-mode idle visual (UUID or Mirror)
    idle_motion: Optional[str] = None
    # Min seconds per loop iteration before the lines repeat; the stage holds
    # its last visual silently for the remainder. Stops a loop WoZ from
    # re-speaking back-to-back (e.g. "比出 N 根手指" while watching the camera).
    loop_min_seconds: Optional[float] = None
    #: 重複 N 輪: with `loop=True`, play the lines N times, then leave like a
    #: non-loop node (None = until a hook interrupts it).
    loop_max_rounds: Optional[int] = None
    #: 拍照 — {"count": 3, "interval_seconds": 2, "delay_seconds": 0}: take
    #: stills from the camera stream while this node is active; announces
    #: sensor `capture_done` / `capture_failed`, writes {{capture_count}},
    #: {{capture_urls}}, {{last_capture_url}}. Best as a silent loop node with
    #: OnSensor("capture_done") and OnTimeout exits.
    capture: Optional[Dict[str, Any]] = None

    node_type = "script"

    def transition_config(self, resolve) -> dict:
        tc: Dict[str, Any] = {}
        if self.loop:
            tc.update({
                "loop_mode": True,
                "expression_id": self.idle_expression,
                "motion_id": self.idle_motion,
            })
            if self.loop_min_seconds is not None:
                tc["loop_min_seconds"] = self.loop_min_seconds
            if self.loop_max_rounds is not None:
                tc["loop_max_rounds"] = self.loop_max_rounds
        if self.capture:
            tc["capture"] = dict(self.capture)
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
        """The judgment itself: WHAT the node decides. A value's jump target is
        NOT part of this — it compiles into a 正常結束 hook instead (see
        `routes`), so every exit is authored in one list. `resolve` is unused
        here and kept for signature symmetry with the other compile()s."""
        out_values = []
        for value, spec in self.values.items():
            label = spec[0] if isinstance(spec, tuple) else spec
            out_values.append({"value": value, "label": label})
        return {"variable": self.variable, "description": self.description,
                "values": out_values}

    def routes(self, resolve) -> list:
        """The exits this judgment implies, as value-conditional 正常結束 hooks
        (server: services/infra/judgment_routing.py). Declaration order is
        preserved and the caller prepends them, because the first matching row
        wins and the unconditional pinned row must stay the fallback."""
        out = []
        for value, spec in self.values.items():
            if not isinstance(spec, tuple):
                continue
            _label, target = spec
            if target is None:
                continue
            out.append({"trigger": "playback_done", "match_variable": self.variable,
                        "match_value": value, "target_workflow_id": resolve(target),
                        "write_to_variable": None})
        return out


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
    #: or "embedding" (semantic cosine via the embed-gateway).
    knowledge_mode: str = "embedding"
    #: Multiple named knowledge sources. Takes precedence over `knowledge`.
    data_sources: List[DataSource] = field(default_factory=list)
    llm_model: str = "local_gateway"
    #: voice-design-v2 — this node's voice mode: None = inherit the character
    #: default (→ platform default "full_duplex"), or one of "full_duplex"
    #: (gateway WebRTC, barge-in on), "no_barge_in" (gateway WebRTC, reply
    #: uninterruptible), "ws_pipeline" (backend robot-WS pipeline).
    voice_mode: Optional[str] = None
    #: Cross-session recall for THIS node: None = inherit the platform default
    #: ('summary'), or 'off' / 'summary' / 'transcript' / 'same_node'. The two
    #: verbatim modes are gated at runtime on the character's transcript
    #: consent and degrade to 'summary' without it.
    memory_source: Optional[str] = None
    temperature: float = 0.7
    judgments: List[Judgment] = field(default_factory=list)
    #: phase → {"mode": "single|random", "expression_ids": [...], "motion_ids": [...],
    #: "led": {"mode": "color", "color": "#FFCC00"}} — listen / think / speak.
    expression_phases: Optional[Dict[str, dict]] = None
    #: 時間預算 — how long this person may spend in THIS node per day, e.g.
    #: ``{"name": "chat", "limit_seconds": 1800,
    #: "limit_var": "{{profile_chat_budget_sec}}"}``. Running out announces the
    #: ordinary inbound event ``budget_exhausted``, which a hook catches — the
    #: runtime never decides what the robot says about it. Per-VISIT limits are
    #: still a node `timeout`; this is the per-day half.
    budget: Optional[Dict[str, Any]] = None
    #: #77 — let end-users set spoken reminders by voice on this AI stage.
    allow_runtime_authoring: bool = False
    #: Hybrid round (P2b): speak FIRST on entry instead of waiting for the
    #: person — `opening_instruction` steers that opening turn and may use
    #: {{variables}} (e.g. the utterance that woke the flow, carried in by an
    #: asr_input hook's write_to).
    opens_conversation: bool = False
    opening_instruction: Optional[str] = None
    #: A `{{variable}}` naming ONE stored picture (e.g. "{{last_capture_url}}")
    #: shown to the model with every turn of this node — a meal photo from
    #: LINE or the robot's camera. Unresolved = no picture.
    image_var: Optional[str] = None
    #: The designer's 停損點/結束語: when True this stage's `should_end` ends the
    #: whole ROUND rather than just advancing — and ending the round is what
    #: actually triggers memory persistence + memory-slot classification. Set it
    #: on the node that concludes an interaction, or nothing is remembered.
    end_interaction: bool = False
    #: Leave after this many of the node's own replies have finished playing,
    #: through the same exit `should_end` takes. None = no limit. For a node
    #: that says one thing and is done (看照片) — a timeout is no substitute,
    #: it counts from entering the node and fires mid-sentence.
    max_replies: Optional[int] = None

    node_type = "ai"

    def base_payload(self, resolve) -> dict:
        """A judgment's jump targets are exits, so they ride with the node's
        other 中斷條件 rather than inside the judgment spec. Prepended: the
        runtime takes the first matching row, and the unconditional 正常結束
        row (if the author added one) must stay the fallback."""
        payload = super().base_payload(resolve)
        routes = [r for j in self.judgments for r in j.routes(resolve)]
        payload["peripheral_hooks"] = routes + payload["peripheral_hooks"]
        return payload

    def post_deploy(self, client, wf_id, resolve):
        end_conditions = None
        if self.expression_phases or self.end_interaction or self.budget or self.max_replies:
            end_conditions = {}
            if self.expression_phases:
                end_conditions["expression_phases"] = self.expression_phases
            if self.end_interaction:
                end_conditions["end_interaction"] = True
            if self.max_replies:
                end_conditions["max_replies"] = int(self.max_replies)
            if self.budget:
                # Inside end_conditions because that is what it is: 一天三十分鐘
                # ends this node for today, next to the time_limit_seconds that
                # ends this visit.
                end_conditions["budget"] = self.budget
        sources = [d.compile() for d in self.data_sources]
        if not sources and self.knowledge:
            sources = [DataSource(name="知識", text=self.knowledge).compile()]
        client.put_agent_config(wf_id, {
            "system_prompt": self.system_prompt,
            "knowledge": None,
            "knowledge_mode": self.knowledge_mode,
            "data_sources": sources or None,
            "llm_model": self.llm_model,
            "voice_mode": self.voice_mode,
            "memory_source": self.memory_source,
            "temperature": self.temperature,
            "output_judgments": [j.compile(resolve) for j in self.judgments],
            "allow_runtime_authoring": self.allow_runtime_authoring,
            "opens_conversation": self.opens_conversation,
            "opening_instruction": self.opening_instruction,
            "image_var": self.image_var,
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
    #: What pressing this records into the Screen node's `input_var`. None = record
    #: nothing (route only) — recording is opt-in per button so an existing node
    #: that mixes a text box with a 「略過」 button keeps its old behavior instead
    #: of overwriting the typed answer. Set it for options you want to analyse
    #: (a scale showing 「不太好」 but recording "1").
    value: Optional[str] = None
    #: Free short text drawn before the label, a step larger than it — a
    #: picture someone recognises without reading (🍚 / 💊 / 「藥」 / 「①」).
    #: Cut to four graphemes by the clients (lib/uiSize.iconOf).
    icon: Optional[str] = None
    #: An uploaded picture for the same slot (``POST /uploads/image``); wins
    #: over ``icon``. Stored bare, signed at stage entry like the screen's own.
    icon_url: Optional[str] = None
    #: A NAMED colour role — "accent" / "green" / "red" / "blue" / "amber" /
    #: "neutral" — never a hex, so the robot, the simulator and the editor
    #: cannot render three different buttons from one choice.
    color: Optional[str] = None
    #: Free placement ``{x, y, size, color}`` in percent of the screen (centre
    #: anchor), like the editor's layout editor writes. None = auto layout.
    place: Optional[Dict[str, Any]] = None

    def compile(self, idx: int, resolve) -> dict:
        out = {"id": self.id or f"btn-{idx}", "label": self.label,
               "target_workflow_id": resolve(self.goto), "value": self.value,
               "icon": self.icon, "icon_url": self.icon_url, "color": self.color}
        if self.place:
            out["place"] = dict(self.place)
        return out


@dataclass
class Screen(Node):
    prompt: str = ""
    #: Where this node's answer is stored. A typed answer and a button press
    #: both land here (a button also writes `<input_var>_label`), so one Screen
    #: node has one answer slot regardless of how it was answered. None =
    #: record nothing.
    input_var: Optional[str] = None
    buttons: List[Button] = field(default_factory=list)
    #: Render a text box? None = infer (a node with buttons is a choice node;
    #: one with only `input_var` is a text-entry node). Set True for both.
    show_input: Optional[bool] = None
    #: A picture above the prompt: a ``/uploads/…`` path (from
    #: ``POST /uploads/image``), an absolute https URL, or a ``{{col}}``
    #: template. None = text only.
    image_url: Optional[str] = None
    #: The answer slot's widget: ``{"kind": "time", "start": "06:00", "end":
    #: "10:00", "step": 30, "default": "{{profile_breakfast_time}}"}``,
    #: ``{"kind": "select", "options_from": "{{意圖.關鍵字}}"}`` (or
    #: ``options=[{"label", "value"}]``), ``{"kind": "number", "min", "max",
    #: "step"}``. A spoken value moves the widget; 確認 writes `input_var`.
    input: Optional[dict] = None
    #: Layout presets: ``{"image": "top"|"left", "buttons": "row"|"column"|
    #: "grid", "size": "small"|"normal"|"large"|"xl"}``.
    layout: Optional[dict] = None
    #: ``size`` is the ONE knob scaling the whole screen: ``small`` /
    #: ``normal`` / ``large`` / ``xl`` (prompt, buttons, wheel and icons
    #: together — see robot-frontend/src/lib/uiSize.ts).
    #: 'self' | 'family': the robot issues a LINE binding code at stage entry
    #: and exposes it as ``{{line_code}}``; the webhook announces
    #: ``line_linked`` (catch it with ``OnSensor("line_linked")``).
    line_code: Optional[str] = None
    #: Lines spoken WHILE the screen is up. Leave empty for the classic screen
    #: (it holds and waits for a button / 確認); give it lines and it says them
    #: over the picture and then takes its normal exit — a loop would say them
    #: again, and again.
    lines: List["Say"] = field(default_factory=list)
    #: 拍照 while the screen is up — same shape as :class:`Script`'s. A node is
    #: what it HAS, not what it is (migration 080), so "a screen that also
    #: takes a picture" is an ordinary combination rather than a special case:
    #: the elder flow's three ESM rounds show the live weight on the screen and
    #: photograph the plate from the same node.
    capture: Optional[Dict[str, Any]] = None
    #: 記住這個人 on 填完: when the typed answer is submitted, the visitor whose
    #: face was seen this visit — and who gave face consent this visit — is
    #: remembered under that name, so the next visit recognises them
    #: ({{enroll_result}}: created / no_consent / no_face / unresolved).
    #: ``True`` names them by ``{{<input_var>}}``; a string is the name
    #: template itself. Only a free-text answer can carry it. Typing a name
    #: remembers NOBODY without this.
    remember_person: Union[bool, str, None] = None

    # 畫面 is not a node type (migration 080). A screen is the third value of a
    # script line's ONE visual slot (鏡像／影片／畫面), so this builds a script
    # node whose line says `show_ui`. That is what lets the same node speak,
    # listen for a keyword and offer buttons at once — a node could only ever
    # be ONE type, which is what made those three mutually exclusive.
    node_type = "script"

    def transition_config(self, resolve) -> dict:
        # `input_var` used to imply a text box, because it was the only thing
        # that wrote it. Buttons write it now too, so a node with buttons is a
        # choice node unless the author explicitly asks for the box as well.
        show = self.show_input
        if show is None:
            show = bool(self.input_var) and not self.buttons
        tc: Dict[str, Any] = {"ui_config": {
            "prompt_text": self.prompt,
            "show_input": bool(show),
            "input_variable": self.input_var,
            "buttons": [b.compile(i, resolve) for i, b in enumerate(self.buttons)],
            "image_url": self.image_url,
            "input": self.input,
            "layout": self.layout,
            "line_code": self.line_code,
        }}
        if self.remember_person:
            name = (self.remember_person if isinstance(self.remember_person, str)
                    else "{{" + (self.input_var or "user_input") + "}}")
            tc["ui_config"]["enroll"] = {"name": name, "mode": "visitor"}
        if not self.lines:
            # Nothing plays, so nothing finishes: without the loop the silent
            # line below counts as played and the node walks straight past its
            # own screen.
            tc["loop_mode"] = True
        if self.capture:
            tc["capture"] = dict(self.capture)
        return tc

    def post_deploy(self, client, wf_id, resolve):
        """One line per spoken sentence, every one of them showing the screen —
        or a single silent one when there is nothing to say. A line that omitted
        `show_ui` would take the screen down mid-sentence: both clients clear
        `ui_config` on a state update that carries none."""
        if self.lines:
            compiled = [s.compile(i) for i, s in enumerate(self.lines)]
            for line in compiled:
                line["show_ui"] = True
        else:
            compiled = [{"line_order": 0, "text_content": "", "pause_duration": 0.0,
                         "expression_id": None, "motion_id": None,
                         "built_in_motion": None, "mirror_camera": False,
                         "show_ui": True, "audio_clip_url": None}]
        client.bulk_scripts(wf_id, [
            {k: v for k, v in line.items() if k != "mirror_camera"} for line in compiled
        ])


@dataclass
class Identify(Node):
    known: NodeRef = None
    unknown: NodeRef = None
    default: NodeRef = None     # fallback; defaults to `unknown`
    timeout_seconds: Optional[int] = None

    # 身份 was never a kind of node at runtime — it compiled to a waiting script
    # node whose exits are value-gated `face_detected` conditions on
    # `identity_kind` (migration 081). This builds that node directly, so what
    # the SDK writes is what an author would write in the editor.
    node_type = "script"

    def transition_config(self, resolve) -> dict:
        # Nothing to play, so nothing finishes: the loop is what holds the node
        # while the camera looks.
        return {"loop_mode": True}

    def base_payload(self, resolve) -> dict:
        payload = super().base_payload(resolve)
        # The branches go FIRST: `_match_hook` returns the first match, so a
        # node that also listens for something else must try "who is this"
        # before the general ear.
        payload["peripheral_hooks"] = (
            self._identity_hooks(resolve) + (payload.get("peripheral_hooks") or []))
        return payload

    def _identity_hooks(self, resolve) -> List[Dict[str, Any]]:
        hooks: List[Dict[str, Any]] = []
        for ref, match in ((self.known, "__known_user__"),
                           (self.unknown, "__unknown_user__")):
            target = resolve(ref) if ref is not None else None
            if not target:
                continue
            hooks.append({"trigger": "face_detected", "match_variable": "identity_kind",
                          "match_value": match, "target_workflow_id": target})
        # The bounded wait the node used to invent for itself at compile time.
        # Without it "waits, then falls through" quietly becomes "waits forever".
        fallback = resolve(self.default or self.unknown) if (self.default or self.unknown) else None
        if fallback:
            hooks.append({"trigger": "timeout",
                          "timeout_seconds": self.timeout_seconds or 25,
                          "target_workflow_id": fallback})
        return hooks




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
    #: How external control events reach this character. None/{"mode":"push"} =
    #: a panel POSTs /sessions/{id}/events (default). {"mode":"subscribe","url":…}
    #: = the backend holds one session-level ws to the feed and dispatches each
    #: message as an inbound event (the robot stays passive). url may contain
    #: {{session_id}}, filled per session at runtime.
    signal_source: Optional[dict] = None
    #: LEGACY (pre-2026-07) loop table {"rows":[...], "advance_on": ...}. Ignored
    #: by the runtime since 2026-08-19 — use a Data node + dataset_binding +
    #: a hook's advance_dataset instead. Kept so old scripts still parse.
    dataset: Optional[dict] = None
    #: Character-level settings that are not node wiring but decide how the
    #: robot behaves across the whole flow. They are applied in a follow-up
    #: PUT after the nodes exist — without them a scripted character has no
    #: voice of its own, remembers nothing between sessions, and records
    #: nothing at all (recording is opt-in and defaults to off).
    persona: Optional[str] = None
    #: [{key, label, description}] — what to remember about each end-user.
    #: Only meaningful alongside an Identify node that recognises them.
    memory_slots: Optional[list] = None
    #: {transcript, webcam, tts_audio} — all default OFF when unset, so a
    #: character that never sets this leaves no ConversationLog behind.
    recording_config: Optional[dict] = None
    #: {asr_provider, tts_provider} — character-level BYO voice-provider picks
    #: (None = platform default). The always-on {camera, sensor, asr} detectors
    #: this used to carry were removed 2026-08-19; the server ignores those keys.
    background_config: Optional[dict] = None
    #: {audio_capture, default_voice_mode} — character-level voice policy.
    voice_config: Optional[dict] = None
    #: The ONE person this deployment serves (an EndUser id of the same owner):
    #: every session starts as them before any face is seen. Home robots.
    primary_end_user_id: Optional[str] = None
    #: {push_to: [LINE userIds]} — recipients of a hook's notify.line besides
    #: the recognised person's own linked id.
    line_config: Optional[dict] = None
    #: {subtitles: "on"|"off"} — show spoken text on screen; "off" = voice only.
    display_config: Optional[dict] = None

    def deploy(self, client: SocialRobotClient, replace: bool = True,
               in_place: bool = False) -> DeployedCharacter:
        """Compile these nodes into a real character on the server.

        ``replace=True`` first deletes every existing character matching this
        one's ``name`` — matching is by NAME, not id, so a character renamed in
        the web dashboard (Character names are mutable and deliberately not
        unique) no longer matches a script still deploying under the old name:
        you get a second character alongside the renamed one rather than a
        replacement. Nothing is lost, but re-point the script's ``name`` at the
        new one to keep the idempotent-redeploy behavior.

        ``in_place=True`` KEEPS the existing character row and replaces only
        its nodes. Use it for anything deployed to real people.

        The distinction is not tidiness. A character id is what the rest of the
        platform points at: ``robot_devices`` binds a robot to one, ``line_config
        .push_to`` holds the family's LINE binding, ``variable_state`` holds
        跨場次保留 values, ``primary_end_user_id`` names a specific person, and
        every deep link and saved composition carries it. Delete-and-recreate
        gives back a DIFFERENT character that looks identical and is connected
        to none of it — the robot in someone's kitchen is now bound to a
        character that no longer exists. So re-running a seed to ship a flow
        change would silently unpair the deployment it was meant to update.

        What in-place still replaces is the NODES, which are authored content:
        every workflow under the character is deleted and rebuilt from this
        script. Hand edits made in the editor do not survive — a seed is the
        source of truth for the graph, and pretending otherwise would need a
        merge nobody asked for. Live sessions are unaffected until they
        re-register (stages are rebuilt from the DB at registration).
        """
        names = [n.name for n in self.nodes]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate node names: {names}")

        keep: Optional[str] = None
        if replace:
            for c in client.list_characters():
                if c["name"] != self.name:
                    continue
                # In-place keeps the FIRST match and still removes any others:
                # duplicates under one name are what a previous replace-mode
                # run left behind, and leaving them would make "which one is
                # the robot bound to" unanswerable.
                if in_place and keep is None:
                    keep = c["id"]
                else:
                    client.delete_character(c["id"])

        if keep is not None:
            char_id = keep
            # The nodes are authored content and are rebuilt wholesale; the
            # character row — and everything pointing at its id — stays.
            for wf in client.list_workflows(char_id):
                client.delete_workflow(wf["id"])
        else:
            char = client.create_character(
                self.name, self.description, self.is_public, self.robot_type,
                blocks_public=(self.blocks_public if self.blocks_public is not None else self.is_public),
                **({"signal_source": self.signal_source} if self.signal_source else {}),
                **({"dataset": self.dataset} if self.dataset else {}),
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

        # Pass 3 — character-level settings. A partial PUT (the endpoint uses
        # exclude_unset), so unset fields are left exactly as they were.
        char_fields = {
            k: v for k, v in (
                ("persona", self.persona),
                ("memory_slots", self.memory_slots),
                ("recording_config", self.recording_config),
                ("background_config", self.background_config),
                ("voice_config", self.voice_config),
                ("primary_end_user_id", self.primary_end_user_id),
                ("line_config", self.line_config),
                ("display_config", self.display_config),
            ) if v is not None
        }
        if char_fields:
            client.update_character(char_id, **char_fields)

        return DeployedCharacter(id=char_id, workflow_ids=ids)
