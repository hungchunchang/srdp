"""Bridge: a handwritten grid puzzle as a SENSOR for a meme session.

This is the half of the ECRobot tutor that meme's flow engine cannot hold: the
board, the answer check, and the running confidence score. A flow has no
arithmetic and no table lookup, so those stay here, and what crosses into the
session is the VERDICT — one `answer_<verdict>` event (`answer_wrong`,
`answer_correct`, …) carrying the variables the flow speaks from:

    iPad (PencilKit) ──POST /handwriting──▶ this bridge ──{event, variables}──▶ meme session
                                              │ OCR → which resident, which house
                                              │ right / wrong / already on the map / spelled as in the clue
                                              │ wrong → which misreading of the clue (looked up, not guessed)
                                              └ confidence → which line of the affect table

Everything a designer might want to change lives in the CHARACTER, not here:
the residents, the answer → misconception table, the affect lines and the
teaching stages are the character's 資料結構, read once at start (`PuzzleSpec.
from_character`). Editing a hint in the web editor changes what the robot says
at the next start of this bridge; a different puzzle is a different character.
Author the character with `server/scripts/seed_ecrobot_puzzle.py`.

The iPad app needs no change: `/register` and `/handwriting` are the two routes
the original ECRobot server exposed, same payloads, same port.

    export SR_BASE_URL="https://<host>/ntu_social_robot/api/v1"
    export SR_API_KEY="srk_..."
    export EC_CHARACTER="立陶宛語地圖解謎"     # id or name
    export EC_ROBOT=nao                       # nao | dry | attach   (default: dry)
    #   nao/dry — this process is ALSO the robot (nao_meme_bridge's body + mic)
    #   attach  — the robot is something else (Kebbi, the web simulator); find
    #             its live session on this character and only push into it
    export GOOGLE_APPLICATION_CREDENTIALS=…   # OCR; or EC_OCR=none and send "text"
    python ecrobot_puzzle_bridge.py           # serves :1468

NOTE on timing: a hook only matches the ACTIVE node and nothing queues, so an
answer pushed while the robot is on a node without a condition for it is
dropped without a trace. The seeded flow puts the conditions on every node, and
`SessionLink.push` says so loudly when a hand-edited flow no longer does.
That check is why the verdict is in the event's NAME rather than in a value
condition on one `answer` event: when this was written the inject endpoint
computed `matched` without the session's variables, so a node whose conditions
were ALL value-gated read `false` even when one fired (fixed on its own in
#616). Six named conditions per node also read better in the editor than one
event with six values, so the names stay. Answers also wait for the robot to stop
talking: a student writes the next letter every couple of seconds, and cutting
the hint off each time would mean no hint is ever heard.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from socialrobot import RobotSession, SocialRobotClient
except ImportError:  # running straight from the repo
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from socialrobot import RobotSession, SocialRobotClient

Cell = Tuple[int, int]

# The character's 資料結構 this bridge reads, and the columns it needs from each.
# These names are the contract with the seed (and with whoever edits the tables
# in the web editor): rename one there and `PuzzleSpec` refuses to start rather
# than diagnose against half a table.
T_RESIDENTS, T_ANSWERS, T_FAMILIES, T_AFFECT, T_STAGES = "住戶", "迷思對應", "迷思類型", "情緒回饋", "教學階段"
REQUIRED_COLUMNS = {
    T_RESIDENTS: {"名字", "提示形式", "格子", "題目已給", "還原提示"},
    T_ANSWERS: {"格子", "名字", "迷思", "迷思名稱", "提示句"},
    T_FAMILIES: {"迷思", "迷思名稱", "學生的想法", "策略"},
    T_AFFECT: {"情緒", "同理", "中性"},
    T_STAGES: {"階段", "目標", "完成條件"},
}
GROUP_COLUMN = {"empathetic": "同理", "neutral": "中性"}
STYLE = {
    "empathetic": "語氣要有同理心：先回應學生的努力或情緒，再給一句引導。",
    "neutral": "語氣保持中性、簡短，只談題目，不評論學生的情緒或表現。",
}


def cell_id(cell: Cell) -> str:
    return "ABCDEFGH"[cell[0]] + str(cell[1] + 1)


def parse_cell(text: str) -> Cell:
    return "ABCDEFGH".index(text[0].upper()), int(text[1:]) - 1


# ── OCR text → resident ─────────────────────────────────────────────────────

# Glyphs a handwriting recognizer returns for Latin letters it took for another
# script. Measured on the 2025-12 logs: "Yčo" comes back as "Усо", "Čepulio" as
# "Черино" — without this table a third of one resident's answers are noise.
_CONFUSABLE = str.maketrans({
    "у": "y", "У": "y", "с": "c", "С": "c", "ч": "c", "Ч": "c", "о": "o", "О": "o",
    "а": "a", "А": "a", "е": "e", "Е": "e", "к": "k", "К": "k", "р": "p", "Р": "p",
    "и": "u", "в": "b", "В": "b", "н": "h", "м": "m", "М": "m", "т": "t", "Т": "t",
    "х": "x", "л": "l", "Л": "l", "п": "n", "д": "d", "і": "i", "ј": "j",
    "ε": "c", "α": "a", "ς": "s", "ο": "o", "ν": "v", "κ": "k", "ι": "i", "υ": "u",
    "ρ": "p", "τ": "t", "ı": "i", "ł": "l", "ø": "o", "đ": "d", "ß": "b",
})
FUZZY_MIN = 0.74


def norm(text: str) -> str:
    """Lowercase ASCII skeleton: diacritics dropped, look-alike glyphs folded."""
    t = unicodedata.normalize("NFKD", text.translate(_CONFUSABLE))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z]", "", t.lower())


@dataclass(frozen=True)
class Resident:
    name: str
    clue_form: str          # how the name looks after "nuo" ("" when the clues never inflect it)
    cell: Cell
    printed: bool           # already on the worksheet
    restore_hint: str       # how to get from the clue form back to the name, by analogy


class SpecError(ValueError):
    """The character's 資料結構 cannot drive this bridge."""


class PuzzleSpec:
    """The puzzle, as the character's data structures describe it."""

    def __init__(self, tables: Dict[str, List[dict]]):
        for table, cols in REQUIRED_COLUMNS.items():
            rows = tables.get(table)
            if not rows:
                raise SpecError(f"資料結構「{table}」不存在或是空的")
            missing = cols - set(rows[0])
            if missing:
                raise SpecError(f"資料結構「{table}」少了欄位：{'、'.join(sorted(missing))}")
        self.residents: Dict[str, Resident] = {}
        for r in tables[T_RESIDENTS]:
            self.residents[r["名字"]] = Resident(
                r["名字"], r.get("提示形式") or "", parse_cell(r["格子"]),
                bool(str(r.get("題目已給") or "").strip()), r.get("還原提示") or "")
        self.home: Dict[Cell, str] = {r.cell: r.name for r in self.residents.values()}
        self.to_place = [r.name for r in self.residents.values() if not r.printed]
        self.rows = 1 + max(c[0] for c in self.home)
        self.cols = 1 + max(c[1] for c in self.home)
        self._answers = {(parse_cell(r["格子"]), r["名字"]): r for r in tables[T_ANSWERS]}
        self._families = {r["迷思"]: r for r in tables[T_FAMILIES]}
        self._affect = {int(r["情緒"]): r for r in tables[T_AFFECT]}
        self.stages = sorted(tables[T_STAGES], key=lambda r: int(r["階段"]))
        # Every written form we can recognise: the name, and its clue form.
        self._forms: Dict[str, Tuple[str, str]] = {}
        for r in self.residents.values():
            self._forms[norm(r.name)] = (r.name, "name")
            if r.clue_form:
                self._forms[norm(r.clue_form)] = (r.name, "clue_form")
        initials: Dict[str, List[str]] = {}
        for r in self.residents.values():
            initials.setdefault(norm(r.name)[0], []).append(r.name)
        # An initial identifies a resident only when no two share it.
        self._initial = {k: v[0] for k, v in initials.items() if len(v) == 1}

    # ── loading ──
    @classmethod
    def from_file(cls, path: str) -> "PuzzleSpec":
        return cls(tables_from_fixture(json.load(open(path, encoding="utf-8"))))

    @classmethod
    def from_character(cls, client: SocialRobotClient, character_id: str) -> "PuzzleSpec":
        tables: Dict[str, List[dict]] = {}
        for wf in client.list_workflows(character_id):
            if wf.get("node_type") != "data":
                continue
            for st in (wf.get("transition_config") or {}).get("structures") or []:
                if st.get("kind", "table") == "table" and isinstance(st.get("value"), list):
                    tables[st["name"]] = st["value"]
        return cls(tables)

    # ── lookups ──
    def answer_row(self, cell: Cell, name: str) -> Optional[dict]:
        return self._answers.get((cell, name))

    def family(self, code: str) -> dict:
        return self._families.get(code) or {}

    def affect(self, emotion: float, group: str) -> str:
        """The affect line for this emotion score. Clamped, not looked up blind:
        a score outside the table is a rounding edge, never a reason to say nothing."""
        key = max(min(self._affect), min(max(self._affect), round(emotion)))
        return (self._affect[key].get(GROUP_COLUMN.get(group, "中性")) or "").strip()

    def resolve(self, text: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        """What was written → (resident, kind), or (None, None).

        kind: 'name' / 'clue_form' for an exact form, 'partial' while the word is still
        being written or the recognizer slipped, 'initial' when only the first letter can
        be trusted — the rule the original server scored with.
        """
        if not isinstance(text, str) or not text.strip():
            return None, None
        toks = [norm(t) for t in re.split(r"\s+", text.strip())] + [norm(text)]
        toks = [t for t in toks if t]
        best: Tuple[Optional[str], Optional[str], float] = (None, None, 0.0)
        for tok in toks:
            if len(tok) < 3:
                continue
            for form, (name, kind) in self._forms.items():
                if tok == form:
                    score, k = 1.0, kind
                elif form.startswith(tok):
                    score, k = 0.80 + 0.19 * len(tok) / len(form), "partial"
                else:
                    score = max(SequenceMatcher(None, tok, form).ratio(),
                                SequenceMatcher(None, tok, form[:len(tok) + 1]).ratio() * 0.9)
                    k = "partial"
                if score > best[2]:
                    best = (name, k, score)
        if best[2] >= FUZZY_MIN:
            return best[0], best[1]
        for tok in sorted(toks, key=len, reverse=True):
            name = self._initial.get(tok[0])
            if not name:
                continue
            if len(tok) == 1 or (len(tok) == 2 and norm(name)[1] == tok[1]):
                return name, "initial"
            if len(tok) >= 3 and max(SequenceMatcher(None, tok, f).ratio()
                                     for f, (n, _) in self._forms.items() if n == name) >= 0.5:
                return name, "initial"
        return None, None


def tables_from_fixture(fx: dict) -> Dict[str, List[dict]]:
    """The seed's fixture file → the tables the character carries. ONE mapping, used by
    the seed (to author them) and by `PuzzleSpec.from_file` (to run without a server)."""
    return {T_RESIDENTS: fx["residents"], T_ANSWERS: fx["answers"], T_FAMILIES: fx["families"],
            T_AFFECT: fx["affect"], T_STAGES: fx["stages"]}


# ── the board, and what each answer means ───────────────────────────────────

@dataclass
class Written:
    name: str
    kind: str
    text: str


@dataclass
class PuzzleState:
    """One participant's board. Pure: no clock of its own, no I/O.

    The confidence arithmetic is the original ECRobot StateManager's, unchanged:
    a right answer scores +2 and a wrong one −3, confidence is clamped to [−2, 2]
    and leaks one point every 300 s of thinking, and the affect line is chosen by
    (this answer's score − the confidence BEFORE it).
    """
    spec: PuzzleSpec
    group: str = "neutral"
    participant: str = ""
    board: Dict[Cell, Written] = field(default_factory=dict)
    judged: Dict[Cell, str] = field(default_factory=dict)   # cell → the name already answered for
    spelled: set = field(default_factory=set)               # residents whose clue-form reminder was given
    confidence: float = 0.0
    last_action_at: Optional[float] = None

    CONF_MIN, CONF_MAX, DECAY_SECONDS = -2.0, 2.0, 300.0
    SCORE_RIGHT, SCORE_WRONG = 2.0, -3.0

    # ── what is on the board ──
    def confirmed(self) -> List[str]:
        return [n for n in self.spec.to_place
                if (w := self.board.get(self.spec.residents[n].cell)) and w.name == n]

    def write(self, cell: Cell, text: Optional[str]) -> bool:
        """Record what the cell now holds. True when that is an answer not yet judged."""
        if cell not in self.spec.home or self.spec.residents[self.spec.home[cell]].printed:
            return False  # a stray stroke over a printed name is not an answer
        name, kind = self.spec.resolve(text)
        if name is None:
            # erased, or a scribble: whatever stood here no longer does
            self.board.pop(cell, None)
            self.judged.pop(cell, None)
            return False
        self.board[cell] = Written(name, kind, (text or "").strip())
        if self.judged.get(cell) != name:
            return True
        # Same answer, still being written. The one thing that can still change is
        # that the finished word turns out to be the clue's form of a right answer.
        return self._owes_respell(cell)

    def _owes_respell(self, cell: Cell) -> bool:
        w = self.board.get(cell)
        return bool(w and w.kind == "clue_form" and self.spec.home.get(cell) == w.name
                    and w.name not in self.spelled)

    # ── judging ──
    def judge(self, cell: Cell, now: float) -> Optional[Dict[str, str]]:
        """The variables for this cell's answer, or None when there is nothing new to say."""
        w = self.board.get(cell)
        if w is None:
            return None
        res = self.spec.residents[w.name]
        if self.judged.get(cell) == w.name:
            if not self._owes_respell(cell):
                return None
            self.spelled.add(w.name)
            return self._vars("respell", cell, w, affect="", hint=self._respell_hint(res), emotion=None)
        self.judged[cell] = w.name

        right = res.cell == cell and not res.printed
        score = self.SCORE_RIGHT if right else self.SCORE_WRONG
        emotion = score - self.confidence
        elapsed = 0.0 if self.last_action_at is None else max(0.0, now - self.last_action_at)
        self.confidence = max(self.CONF_MIN, min(self.CONF_MAX,
                                                 self.confidence + score - elapsed / self.DECAY_SECONDS))
        self.last_action_at = now
        affect = self.spec.affect(emotion, self.group)

        if res.printed:
            hint = (res.restore_hint if w.kind == "clue_form" and res.restore_hint
                    else f"{res.name} 已經在地圖上了，不用再填一次。")
            return self._vars("given", cell, w, affect, hint, emotion, label="把題目已給的住戶再填一次")
        if right:
            verdict, hint = "correct", ""
            if w.kind == "clue_form" and w.name not in self.spelled:
                self.spelled.add(w.name)
                verdict, hint = "spelling", self._respell_hint(res)
            # By what has been ANSWERED FOR, not by what is on the board: two names written
            # in quick succession are both on the board before either is judged, and
            # each would otherwise be told it was the last one.
            if all(self.judged.get(self.spec.residents[n].cell) == n for n in self.spec.to_place):
                verdict = "done"
            return self._vars(verdict, cell, w, affect, hint, emotion)
        row = self.spec.answer_row(cell, w.name) or {}
        fam = self.spec.family(row.get("迷思", ""))
        return self._vars("wrong", cell, w, affect, row.get("提示句") or "這一格對不上提示，再看一次提到他的那幾條。",
                          emotion, label=row.get("迷思名稱", ""), belief=fam.get("學生的想法", ""),
                          strategy=fam.get("策略", ""), family=row.get("迷思", ""))

    def _respell_hint(self, res: Resident) -> str:
        return f"不過你寫的是 {res.clue_form}，那是它在提示裡 nuo 後面的樣子。{res.restore_hint}".strip()

    def _vars(self, verdict: str, cell: Cell, w: Written, affect: str, hint: str,
              emotion: Optional[float], *, label: str = "", belief: str = "", strategy: str = "",
              family: str = "") -> Dict[str, str]:
        out = {
            "verdict": verdict, "answer_cell": cell_id(cell), "answer_name": w.name, "answer_text": w.text,
            "affect": affect, "hint": hint, "misconception": label, "misconception_code": family,
            "belief": belief, "strategy": strategy,
            "emotion": "" if emotion is None else str(round(emotion)),
        }
        out.update(self.progress())
        return out

    # ── what the flow (and the AI node's prompt) may know at any time ──
    def stage(self) -> dict:
        done = set(self.confirmed())
        for row in self.spec.stages:
            need = {n.strip() for n in re.split(r"[、,]", str(row["完成條件"])) if n.strip()}
            if not need <= done:
                return row
        return self.spec.stages[-1]

    def board_text(self) -> str:
        lines = []
        for r in range(self.spec.rows):
            cells = []
            for c in range(self.spec.cols):
                home = self.spec.residents[self.spec.home[(r, c)]]
                w = self.board.get((r, c))
                if home.printed:
                    cells.append(f"{cell_id((r, c))} {home.name}（題目）")
                elif w is None:
                    cells.append(f"{cell_id((r, c))} 空")
                else:
                    cells.append(f"{cell_id((r, c))} {w.name}{'✓' if w.name == home.name else '✗'}")
            lines.append("｜".join(cells))
        return "\n".join(lines)

    def spelling_left(self) -> List[str]:
        return [n for n in self.confirmed()
                if self.board[self.spec.residents[n].cell].kind == "clue_form"]

    def progress(self) -> Dict[str, str]:
        done, stage, left = self.confirmed(), self.stage(), self.spelling_left()
        return {
            "board": self.board_text(),
            "confirmed": "、".join(done) or "（還沒有）",
            "confirmed_count": str(len(done)),
            "remaining_count": str(len(self.spec.to_place) - len(done)),
            "tutor_stage": str(stage["階段"]), "stage_goal": str(stage["目標"]),
            "confidence": f"{round(self.confidence, 1) + 0.0:.1f}",  # + 0.0: −0.03 must not print as "-0.0"
            "spelling_note": (f"還有 {len(left)} 個名字寫的是提示裡的樣子（{'、'.join(left)}），"
                              "可以把它們還原成原本的名字。") if left else "",
            "group": self.group, "style": STYLE.get(self.group, STYLE["neutral"]),
            "participant": self.participant,
        }


# ── the live session ────────────────────────────────────────────────────────

class SessionLink:
    """Where verdicts go: one live session, and whether its robot is talking."""

    QUIET_WAIT_SECONDS = 12.0
    SETTLE_AFTER_PUSH_SECONDS = 0.6

    def __init__(self, client: SocialRobotClient, session_id: Optional[str],
                 is_speaking: Callable[[], bool] = lambda: False):
        self.client, self.session_id, self.is_speaking = client, session_id, is_speaking
        self._turn = asyncio.Lock()

    async def push(self, event: Optional[str], variables: Dict[str, str]) -> dict:
        # One at a time, and the robot gets a moment to START the line this push caused:
        # two pushes that both saw a quiet robot would land together, and the second
        # would cut off a line nobody had heard a word of.
        async with self._turn:
            deadline = time.monotonic() + self.QUIET_WAIT_SECONDS
            while self.is_speaking() and time.monotonic() < deadline:
                await asyncio.sleep(0.2)
            if not self.session_id:
                print(f"[bridge] ⚠ 沒有機器人在線上，{event!r} 沒有送出去。")
                return {"matched": False}
            try:
                out = await asyncio.to_thread(self.client.inject, self.session_id, event, variables)
            except Exception as e:
                # The tablet's request must still succeed and the log must still be written:
                # what the participant wrote is the study's data whether or not a robot heard it.
                print(f"[bridge] ⚠ {event!r} 送不進 session {self.session_id}：{e}")
                return {"matched": False}
            if event and out.get("matched") is False:
                # Not an error the server raises: a hook matches the ACTIVE node only, so
                # an event with nowhere to land returns 200 and changes nothing.
                print(f"[bridge] ⚠ 事件 {event!r} 沒有對上任何條件——機器人目前所在的節點沒有接它。")
            elif event:
                await asyncio.sleep(self.SETTLE_AFTER_PUSH_SECONDS)
            return out


def answer_event(verdict: str) -> str:
    """The event a verdict is pushed as. The flow has one condition per name."""
    return f"answer_{verdict}"


class PuzzleBridge:
    """Snapshots in, verdicts out. One participant at a time, like the original."""

    def __init__(self, spec: PuzzleSpec, link: SessionLink, *, settle_seconds: float = 1.5,
                 log_path: Optional[str] = None, clock: Callable[[], float] = time.monotonic):
        self.spec, self.link, self.settle, self.clock = spec, link, settle_seconds, clock
        self.state = PuzzleState(spec)
        self.tokens: Dict[str, str] = {}
        self.log_path = log_path
        self.last_activity = clock()
        self.last_nudge: Optional[float] = None
        self._pending: Dict[Cell, asyncio.Task] = {}

    def _log(self, kind: str, **fields: Any) -> None:
        if not self.log_path:
            return
        row = {"t": time.time(), "kind": kind, "participant": self.state.participant,
               "group": self.state.group, **fields}
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    async def register(self, participant: str, group: str) -> str:
        """A participant sits down: a fresh board, and the flow leaves its waiting node."""
        for task in self._pending.values():
            task.cancel()
        self._pending.clear()
        self.state = PuzzleState(self.spec, group=group if group in GROUP_COLUMN else "neutral",
                                 participant=participant)
        self.last_activity, self.last_nudge = self.clock(), None
        token = secrets.token_urlsafe(24)
        self.tokens = {token: participant}
        self._log("register")
        await self.link.push("participant_ready", {
            **self.state.progress(), "verdict": "", "affect": "", "hint": "", "misconception": "",
            "belief": "", "strategy": "", "answer_name": "", "answer_cell": "", "answer_text": ""})
        return token

    async def snapshot(self, cell: Cell, text: Optional[str]) -> None:
        """One cell's current content, as the tablet reports it after each pause."""
        self.last_activity = self.clock()
        if not (text or "").strip() and cell not in self.state.board and cell not in self._pending:
            # "Still empty" is not news. The 2025-12 iPad build re-sent every cell it had
            # ever cleared on every later stroke — 87.5% of that study's handwriting rows —
            # and a tablet still running it must not bury the log, or the robot, in them.
            return
        fresh = self.state.write(cell, text)
        self._log("snapshot", cell=cell_id(cell), text=text or "",
                  name=(self.state.board.get(cell).name if cell in self.state.board else None))
        old = self._pending.pop(cell, None)
        if old:
            old.cancel()
        if fresh:
            self._pending[cell] = asyncio.create_task(self._settle(cell))

    async def _settle(self, cell: Cell) -> None:
        # A word arrives as B → Bač → Bačkio, one snapshot per pause. Answering the first
        # of them would judge a letter; waiting lets the newer snapshot replace this one.
        try:
            await asyncio.sleep(self.settle)
        except asyncio.CancelledError:
            return
        self._pending.pop(cell, None)
        answer = self.state.judge(cell, self.clock())
        if answer is None:
            return
        self._log("answer", **answer)
        await self.link.push(answer_event(answer["verdict"]), answer)

    async def nudge_if_idle(self, idle_seconds: float = 120.0, cooldown_seconds: float = 300.0) -> bool:
        """The original's 「有什麼需要幫忙的嗎？」: two minutes of nothing, at most every five."""
        now = self.clock()
        if not self.state.participant or now - self.last_activity < idle_seconds:
            return False
        if self.last_nudge is not None and now - self.last_nudge < cooldown_seconds:
            return False
        self.last_nudge = now
        self._log("idle")
        await self.link.push("idle", self.state.progress())
        return True


# ── the tablet's two routes ─────────────────────────────────────────────────

def strokes_to_png(strokes: List[dict], size: int = 600, margin: int = 40, max_zoom: float = 8.0) -> bytes:
    """The cell's strokes as a picture for the recognizer, framed on the WRITING.

    The original server drew each point where the tablet said it was, on a 600-px canvas.
    That only worked because the iPad's grid was about 400 pt wide: a tablet that reports
    in other units (the Android app normalises the grid to 480, a higher-density screen
    sends pixels) would hand the recognizer a word the size of a fingernail, or one that
    is off the canvas altogether. So the ink's own bounding box is scaled to fill the
    picture — capped, so a single dot stays a dot — and the same word gives the same image
    whatever device wrote it.
    """
    import io

    from PIL import Image, ImageDraw
    image = Image.new("RGB", (size, size), "white")
    paths = [[(float(p["x"]), float(p["y"])) for p in (stroke.get("points") or [])] for stroke in strokes]
    points = [p for path in paths for p in path]
    if points:
        xs, ys = [x for x, _ in points], [y for _, y in points]
        left, top = min(xs), min(ys)
        width, height = max(max(xs) - left, 1.0), max(max(ys) - top, 1.0)
        zoom = min((size - 2 * margin) / width, (size - 2 * margin) / height, max_zoom)
        dx, dy = (size - width * zoom) / 2 - left * zoom, (size - height * zoom) / 2 - top * zoom
        pen = max(3, size // 100)                    # in PICTURE pixels: the same line for every tablet
        draw = ImageDraw.Draw(image)
        for path in paths:
            scaled = [(x * zoom + dx, y * zoom + dy) for x, y in path]
            if len(scaled) == 1:
                x, y = scaled[0]
                draw.ellipse([x - pen / 2, y - pen / 2, x + pen / 2, y + pen / 2], fill="black")
            elif scaled:
                draw.line(scaled, fill="black", width=pen, joint="curve")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def google_ocr() -> Callable[[List[dict]], str]:
    from google.cloud import vision
    client = vision.ImageAnnotatorClient()

    def recognise(strokes: List[dict]) -> str:
        resp = client.text_detection(image=vision.Image(content=strokes_to_png(strokes)))
        if resp.error.message:
            raise RuntimeError(f"Vision API: {resp.error.message}")
        return resp.text_annotations[0].description if resp.text_annotations else ""
    return recognise


def make_app(bridge: PuzzleBridge, ocr: Optional[Callable[[List[dict]], str]] = None):
    """`/register` + `/handwriting`, payload-compatible with the ECRobot server the iPad
    app was written against. A request may carry `text` instead of strokes — what any
    client that recognises on its own side (or a test) sends."""
    from fastapi import FastAPI, Header, HTTPException

    app = FastAPI(title="ECRobot puzzle bridge")

    @app.post("/register")
    async def register(body: dict):
        participant = str(body.get("participant_id") or "").strip()
        if not participant:
            raise HTTPException(422, "participant_id is required")
        group = str((body.get("device_info") or {}).get("experiment_group") or "neutral")
        return {"access_token": await bridge.register(participant, group), "token_type": "bearer"}

    @app.post("/handwriting")
    async def handwriting(body: dict, authorization: str = Header("")):
        if authorization.removeprefix("Bearer ").strip() not in bridge.tokens:
            raise HTTPException(401, "register first")
        try:
            cell = (int(body["gridCell"]["row"]), int(body["gridCell"]["col"]))
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "gridCell {row, col} is required")
        text = body.get("text")
        if text is None:
            strokes = body.get("strokes") or []
            if strokes and ocr is None:
                raise HTTPException(503, "no recognizer configured (EC_OCR=none) and no text sent")
            text = await asyncio.to_thread(ocr, strokes) if strokes else ""
        await bridge.snapshot(cell, text)
        return {"status": "success"}

    @app.get("/state")
    async def state():
        return {"participant": bridge.state.participant, **bridge.state.progress()}

    return app


# ── wiring ──────────────────────────────────────────────────────────────────

def resolve_character(client: SocialRobotClient, character: str) -> str:
    if "-" in character and len(character) >= 32:
        return character
    match = next((c for c in client.list_characters() if c["name"] == character), None)
    if not match:
        sys.exit(f"character {character!r} not found")
    return match["id"]


def pick_session(current: Optional[str], live: List[str]) -> Optional[str]:
    """Which session an attached bridge should be pushing into: the one it has while that
    is still live, else whichever the robot holds now, else none."""
    return current if current in live else (live[0] if live else None)


async def follow_robot(client: SocialRobotClient, char_id: str, link: SessionLink, state: dict,
                       every: float = 3.0) -> None:
    """attach mode: keep the link on the session the robot CURRENTLY holds.

    The robot is someone else's process, and it restarts — a NAO client relaunched between
    two participants registers a new session. A bridge still aimed at the old one answers
    every tablet request with a 200 and moves nothing, which in the room looks like a robot
    that has stopped caring.
    """
    watcher: Optional[asyncio.Task] = None

    async def watch(session_id: str) -> None:
        try:
            async with await client.subscribe(session_id) as events:
                async for ev in events:
                    if ev.get("type") == "state":
                        state["speaking"] = ev.get("state") == "SPEAKING"
        except Exception as e:
            print(f"[bridge] session {session_id} 的狀態訂閱中斷：{e}")
        finally:
            state["speaking"] = False

    while True:
        try:
            live = [x["session_id"] for x in await asyncio.to_thread(client.list_sessions)
                    if x.get("character_id") == char_id]
        except Exception as e:
            print(f"[bridge] 查不到線上的 session：{e}")
            live = [link.session_id] if link.session_id else []
        chosen = pick_session(link.session_id, live)
        if chosen != link.session_id:
            if watcher is not None:
                watcher.cancel()
            link.session_id, state["speaking"] = chosen, False
            if chosen:
                watcher = asyncio.create_task(watch(chosen))
                print(f"[bridge] 接上機器人的 session {chosen}")
            else:
                print("[bridge] ⚠ 機器人離線了；等它回來（平板照常可以寫，答案照常記錄）。")
        await asyncio.sleep(every)


async def _idle_loop(bridge: PuzzleBridge) -> None:
    while True:
        await asyncio.sleep(5)
        await bridge.nudge_if_idle()


async def _serve(app, port: int) -> None:
    import uvicorn
    await uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")).serve()


async def main() -> int:
    base = os.getenv("SR_BASE_URL")
    key = os.getenv("SR_API_KEY")
    character = os.getenv("EC_CHARACTER", "立陶宛語地圖解謎")
    if not base or not key:
        sys.exit("Set SR_BASE_URL (…/api/v1) and SR_API_KEY (srk_…).")
    client = SocialRobotClient(base_url=base, api_key=key)
    char_id = resolve_character(client, character)
    spec = PuzzleSpec.from_character(client, char_id)
    ocr = None if os.getenv("EC_OCR", "google") == "none" else google_ocr()
    port = int(os.getenv("EC_PORT", "1468"))
    log_path = os.getenv("EC_LOG", f"ecrobot_puzzle_{time.strftime('%Y%m%d_%H%M%S')}.jsonl")
    settle = float(os.getenv("EC_SETTLE", "1.5"))
    mode = os.getenv("EC_ROBOT", "dry")

    if mode == "attach":
        # The robot is someone else's process (the NAO duplex client, a Kebbi, the web
        # simulator): follow whichever session it holds and only push into it.
        state = {"speaking": False}
        link = SessionLink(client, None, lambda: state["speaking"])
        bridge = PuzzleBridge(spec, link, settle_seconds=settle, log_path=log_path)
        print(f"Bridge live: tablet :{port} → the robot on {character!r} (attached). Ctrl+C to stop.")
        await asyncio.gather(follow_robot(client, char_id, link, state), _idle_loop(bridge),
                             _serve(make_app(bridge, ocr), port))
        return 0

    # This process is the robot too: nao_meme_bridge's body and ears, unchanged.
    import nao_meme_bridge as nao
    if mode == "nao" and not os.getenv("NAO_IP"):
        sys.exit("EC_ROBOT=nao needs NAO_IP.")
    body = nao.make_nao_body() if mode == "nao" else nao.DryRunBody()
    up = nao.drive_up_mic if os.getenv("SR_INPUT") == "mic" else nao.drive_up_console
    async with await RobotSession.start(client, char_id, device_id="ecrobot-puzzle",
                                        robot_type="nao") as session:
        def speaking() -> bool:
            return bool(session.events) and session.events[-1].get("state") == "SPEAKING"
        bridge = PuzzleBridge(spec, SessionLink(client, session.session_id, speaking),
                              settle_seconds=settle, log_path=log_path)
        print(f"Bridge live: tablet :{port} → session {session.session_id} → NAO ({body.kind}). Ctrl+C to stop.")
        await asyncio.gather(nao.drive_down(session, body), up(session), _idle_loop(bridge),
                             _serve(make_app(bridge, ocr), port))
    client.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nStopped.")
