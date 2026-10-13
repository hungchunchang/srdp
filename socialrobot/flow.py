"""Write a flow like a program — ``If`` / ``Repeat`` / ``While`` / ``ForEach``
that still compile to an ordinary flow graph (captain 2026-10-13, C18).

A Python ``for`` in a deploy script runs ONCE, at deploy time: it can generate
twenty question nodes, but it cannot decide anything while the robot runs,
because by then the script has exited and the robot runs the graph. These
blocks are for the decisions that only exist at run time. Each one becomes
nodes and 進入時 rows the editor shows and edits like any other::

    from socialrobot import Flow, NEXT, Button, Screen, Script, Say, var

    f = Flow()
    f.then(Script("問候", lines=[Say("早安")]))
    with f.Repeat(3, counter="第幾次", until=var("吃了") == "yes"):
        f.then(Screen("問吃藥", prompt="第 {{第幾次}} 次：吃藥了嗎？", input_var="吃了",
                      buttons=[Button("吃了", value="yes", goto=NEXT),
                               Button("還沒", value="no", goto=NEXT)]))
    with f.If(var("吃了") == "yes"):
        f.then(Script("好", lines=[Say("太好了")]))
    with f.Else():
        f.then(Script("提醒家人", lines=[Say("我請家人來看看")]))
    Character(name="吃藥", nodes=f.nodes).deploy(client)

``f.then(node)`` makes ``node`` the 正常結束 of whatever came before, and so
does every ``NEXT`` left in a button or a hook. What each block compiles to:

- ``If`` / ``Elif`` / ``Else`` — one silent node whose 進入時 rows are the
  conditions in order, the last row (其他) going to ``Else`` or past the block.
- ``While(cond)`` — a silent check node before the body: cond → body, 其他 →
  past the block; the body's end goes back to the check.
- ``Repeat(n)`` — a silent node that sets ``counter`` to 1 and enters the
  body, and a check node after it with one row per round (``counter`` is 1 →
  set it to 2, body again … ``n``-1 → ``n``) and 其他 → past the block. The
  graph has no arithmetic, so the count is written out, one row per round:
  ``{{counter}}`` can be said in a line. ``until=`` is checked first, after
  each round.
- ``ForEach("題庫")`` — the body runs with the structure's current row; a
  check node after it leaves when ``{{題庫.序號}}`` reaches ``{{題庫.總數}}``,
  else moves ↻ to the next row and runs the body again. Leaving also moves ↻,
  which wraps the list back to its first row for the next time.

A condition is ``var("x") == "yes"`` (string equality, the hooks' own) or
``>`` ``>=`` ``<`` ``<=`` (numeric; the other side may be ``var("y")``). There is
no ``and`` / ``or``: nest two ``If``. ``!=`` is written as the two rows it
means (== goes the other way), so it works on ``If`` without ``Elif`` and on
``While``.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, List, Optional

from .builder import Hook, Node, NodeRef, OnEnter, OnSensor, Script


class _Next:
    """The node `then()` adds next — use it as a button's or a hook's goto."""

    def __repr__(self) -> str:
        return "NEXT"


NEXT: Any = _Next()

_OPS = {"eq": "==", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


def _operand(value: Any) -> str:
    if isinstance(value, Var):
        return "{{" + value.name + "}}"
    if isinstance(value, bool):
        raise TypeError("條件的值寫成字串，例如 var(\"x\") == \"yes\"；平台的值都是文字")
    return str(value)


@dataclass(frozen=True)
class Cond:
    variable: str
    op: str          # eq / gt / gte / lt / lte
    value: str
    negated: bool = False  # `!=`: eq, taken the other way

    def __bool__(self) -> bool:
        raise TypeError(
            "條件只在機器人跑的時候才有答案，不能用 Python 的 if／and／or／not："
            "寫成 with f.If(...)；兩個條件就巢狀兩個 If")

    def label(self) -> str:
        sym = "!=" if self.negated else _OPS[self.op]
        return f"{self.variable} {sym} {self.value}"

    def hook(self, goto=None, **fields) -> Hook:
        return OnEnter(goto, when_var=self.variable, equals=self.value,
                       op=None if self.op == "eq" else self.op, **fields)


def Program(name: str, step: Optional[str] = None, *, exits: Optional[dict] = None,
            otherwise: NodeRef = NEXT, no_reply: NodeRef = NEXT, seconds: float = 10) -> Script:
    """程式節點: ask your own program (``client.serve({step: fn})``) here.

    The node waits silently until the program answers (say something on the
    node before it if the wait should not be quiet). ``exits`` maps the program's ``exit`` to a node;
    any other answer goes to ``otherwise``; not connected, too slow (past
    ``seconds``) or an error goes to ``no_reply``. Both default to ``NEXT``.
    The program's words are {{program_say}} and its variables are {{…}} in
    every node after — speak them on the next node::

        f.then(Program("挑題", step="挑下一題", exits={"過關": done}))
        f.then(Script("出題", lines=[Say("{{program_say}}")]))
    """
    step = step or name
    rows: List[Hook] = [OnSensor("program_reply", goto=target, when_var="program_exit", equals=str(value))
                        for value, target in (exits or {}).items()]
    rows.append(OnSensor("program_reply", goto=otherwise))
    rows.append(OnSensor("program_no_reply", goto=no_reply))
    node = Script(name, lines=[], loop=True,
                  extra_config={"program": {"step": step, "timeout_seconds": seconds}})
    node.hooks = rows
    return node


class Var:
    """A session variable in a condition: ``var("分數") >= 60``."""

    def __init__(self, name: str):
        if not name or "{" in name:
            raise ValueError(f"變數名稱直接寫名字，不要加 {{{{ }}}}：{name!r}")
        self.name = name

    def _c(self, op: str, other: Any, negated: bool = False) -> Cond:
        return Cond(self.name, op, _operand(other), negated)

    def __eq__(self, other):  # type: ignore[override]
        return self._c("eq", other)

    def __ne__(self, other):  # type: ignore[override]
        return self._c("eq", other, negated=True)

    def __gt__(self, other):
        return self._c("gt", other)

    def __ge__(self, other):
        return self._c("gte", other)

    def __lt__(self, other):
        return self._c("lt", other)

    def __le__(self, other):
        return self._c("lte", other)

    __hash__ = None  # type: ignore[assignment]


def var(name: str) -> Var:
    return Var(name)


Setter = Callable[[Node], None]


def _set_goto(holder) -> Setter:
    def put(target: Node) -> None:
        holder.goto = target
    return put


@dataclass
class _IfChain:
    route: Script
    exits: List[Setter]
    other: Hook            # the 其他 row, always last on the route
    negated: bool


class Flow:
    """A cursor that wires nodes in order, plus the run-time blocks."""

    def __init__(self) -> None:
        self._nodes: List[Node] = []
        self._pending: List[Setter] = []
        self._names: set = set()
        self._if: Optional[_IfChain] = None
        self._ended = False
        self._counters = 0

    # ---------- wiring ----------

    def _unique(self, name: str) -> str:
        if name not in self._names:
            return name
        i = 2
        while f"{name}（{i}）" in self._names:
            i += 1
        return f"{name}（{i}）"

    def _add(self, node: Node) -> Node:
        if self._ended:
            raise RuntimeError("這個流程已經 end() 了，不能再接節點")
        if node.name in self._names:
            raise ValueError(f"流程裡已經有叫「{node.name}」的節點")
        self._names.add(node.name)
        self._nodes.append(node)
        for put in self._pending:
            put(node)
        self._pending = []
        # What this node leaves open: its own 正常結束, and every NEXT in it.
        # A silent loop (Program, a wait node) never ends by itself — its rows
        # are its only ways out, so its own 正常結束 is not an open end.
        holds = isinstance(node, Script) and node.loop and not node.lines and node.loop_max_rounds is None
        if holds and node.goto is None:
            pass
        elif node.goto is None or node.goto is NEXT:
            node.goto = None
            self._pending.append(_set_goto(node))
        for h in node.hooks:
            if h.goto is NEXT:
                h.goto = None
                self._pending.append(_set_goto(h))
        for b in getattr(node, "buttons", None) or []:
            if b.goto is NEXT:
                b.goto = None
                self._pending.append(_set_goto(b))
        return node

    def then(self, *nodes: Node) -> Node:
        """Append nodes in order; each is the next of the one before."""
        if not nodes:
            raise ValueError("then() 要給至少一個節點")
        self._if = None
        for n in nodes:
            if not isinstance(n, Node):
                raise TypeError(f"then() 只收節點，收到 {type(n).__name__}")
            self._add(n)
        return nodes[-1]

    def _silent(self, name: str, hooks: List[Hook]) -> Script:
        node = Script(self._unique(name), lines=[])
        node.hooks = hooks
        return node

    def _body(self, entry: Setter) -> "_Body":
        return _Body(self, entry)

    # ---------- If / Elif / Else ----------

    @contextmanager
    def If(self, cond: Cond) -> Iterator[None]:
        _need(cond, "If")
        other = OnEnter()
        if cond.negated:
            # x != v  →  [x == v → past/else]  [其他 → body]
            eq = cond.hook()
            route = self._silent(f"如果 {cond.label()}", [eq, other])
            self.then(route)
            self._pending = []
            with self._body(_set_goto(other)) as b:
                yield
            self._if = _IfChain(route, b.exits, eq, negated=True)
            self._pending = b.exits + [_set_goto(eq)]
            return
        row = cond.hook()
        route = self._silent(f"如果 {cond.label()}", [row, other])
        self.then(route)
        self._pending = []
        with self._body(_set_goto(row)) as b:
            yield
        self._if = _IfChain(route, b.exits, other, negated=False)
        self._pending = b.exits + [_set_goto(other)]

    @contextmanager
    def Elif(self, cond: Cond) -> Iterator[None]:
        _need(cond, "Elif")
        chain = self._take_if("Elif")
        if chain.negated or cond.negated:
            raise ValueError("!= 只能用在沒有 Elif 的 If：改寫成兩個 == 的分支")
        row = cond.hook()
        # The new row goes before 其他, which stays the route's last row.
        chain.route.hooks.insert(len(chain.route.hooks) - 1, row)
        self._pending = []
        with self._body(_set_goto(row)) as b:
            yield
        chain.exits = chain.exits + b.exits
        self._if = chain
        self._pending = chain.exits + [_set_goto(chain.other)]

    @contextmanager
    def Else(self) -> Iterator[None]:
        chain = self._take_if("Else")
        self._pending = []
        with self._body(_set_goto(chain.other)) as b:
            yield
        self._pending = chain.exits + b.exits

    def _take_if(self, what: str) -> _IfChain:
        chain = self._if
        if chain is None:
            raise RuntimeError(f"{what} 要緊接在 If 或 Elif 的區塊後面")
        self._if = None
        return chain

    # ---------- loops ----------

    @contextmanager
    def While(self, cond: Cond) -> Iterator[None]:
        _need(cond, "While")
        other = OnEnter()
        if cond.negated:
            out = cond.hook()                 # x == v → leave
            check = self._silent(f"當 {cond.label()}", [out, other])
            into = other
        else:
            into = cond.hook()
            check = self._silent(f"當 {cond.label()}", [into, other])
            out = other
        self.then(check)
        self._pending = []
        with self._body(_set_goto(into)) as b:
            yield
        for put in b.exits:
            put(check)
        self._pending = [_set_goto(out)]

    @contextmanager
    def Repeat(self, times: int, *, counter: Optional[str] = None,
               until: Optional[Cond] = None) -> Iterator[None]:
        if not isinstance(times, int) or times < 1:
            raise ValueError("Repeat(n) 的 n 是正整數")
        if times > 50:
            raise ValueError("Repeat 最多 50 次：每一次在檢查節點上是一列；更多請用 ForEach 走資料表")
        if until is not None:
            _need(until, "until")
            if until.negated:
                raise ValueError("until 不能用 !=：寫成 == 另一個值")
        if counter is None:
            self._counters += 1
            counter = "重複次數" if self._counters == 1 else f"重複次數{self._counters}"
        Var(counter)
        start = OnEnter(variables={counter: "1"})
        init = self._silent(f"重複 {times} 次", [start])
        self.then(init)
        self._pending = []
        with self._body(_set_goto(start)) as b:
            yield
        first = start.goto
        rows: List[Hook] = []
        if until is not None:
            rows.append(until.hook())
        for k in range(1, times):
            rows.append(OnEnter(first, when_var=counter, equals=str(k),
                                variables={counter: str(k + 1)}))
        done = OnEnter()
        rows.append(done)
        check = self._silent(f"重複 {times} 次：再一次？", rows)
        self._add(check)
        self._pending = [_set_goto(done)] + ([_set_goto(rows[0])] if until is not None else [])
        self._if = None
        # _add queued check's own 正常結束 — a silent node whose rows always
        # route (the last is unconditional), so that edge never fires.
        for put in b.exits:
            put(check)

    @contextmanager
    def ForEach(self, structure: str) -> Iterator[None]:
        Var(structure)
        if "." in structure:
            raise ValueError("ForEach 給資料結構的名字（資料節點裡那個），不是欄位")
        self._if = None
        holder = _Holder()
        with self._body(_set_goto(holder)) as b:
            yield
        first = holder.goto
        leave = OnEnter(when_var=f"{structure}.序號", op="gte",
                        equals="{{" + structure + ".總數}}", advance_dataset=structure)
        again = OnEnter(first, advance_dataset=structure)
        check = self._silent(f"{structure}：下一列", [leave, again])
        self._add(check)
        self._pending = [_set_goto(leave)]
        for put in b.exits:
            put(check)

    # ---------- the end ----------

    def end(self, name: str = "結束") -> Script:
        """A last node that everything still open goes to. `nodes` adds one
        by itself when something is open; call it to name it yourself."""
        node = Script(self._unique(name), lines=[])
        self.then(node)
        self._pending = []
        self._ended = True
        return node

    @property
    def nodes(self) -> List[Node]:
        """Every node, in order. Anything still open (a 其他 row, a NEXT, the
        last node's 正常結束) is closed onto an explicit 結束 node — a node
        with no target falls through to whichever node sits next in the
        character's list. Put these nodes last in that list."""
        if self._pending and not self._ended:
            self.end()
        return list(self._nodes)


class _Holder:
    goto: Any = None


class _Body:
    """Runs a block: the first node added inside resolves `entry`; whatever
    the block leaves open when it ends is `exits`."""

    def __init__(self, flow: Flow, entry: Setter):
        self.flow = flow
        self.entry = entry
        self.exits: List[Setter] = []

    def __enter__(self) -> "_Body":
        self.flow._if = None
        self.flow._pending.append(self.entry)
        self._start = len(self.flow._nodes)
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is not None:
            return False
        if len(self.flow._nodes) == self._start:
            raise ValueError("區塊裡至少要有一個節點（用 f.then(...) 加）")
        self.exits = self.flow._pending
        self.flow._pending = []
        return False


def _need(cond: Any, what: str) -> None:
    if not isinstance(cond, Cond):
        raise TypeError(f"{what} 要一個條件，例如 var(\"分數\") >= 60")
