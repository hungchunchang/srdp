"""Social Robot Platform SDK.

Three halves, all public:

- **Runtime** — connect a robot body or any program to a live platform
  session and drive/observe it (`SocialRobotClient`, `RobotSession`, `Robot`).
- **Authoring** — build a character's flow in Python and deploy it
  (`Character`, `Script`, `Say`, `Screen`, `Agent`, the `On*` hooks, …), with
  `Flow` for the decisions only the running robot can make: `If` / `Repeat` /
  `While` / `ForEach` compile to nodes and 進入時 rows the web editor shows.
  Python's own `for` / `if` run once, at deploy time.
- **Flow in git** — the `socialrobot` command (`export` / `import --into`).

    from socialrobot import Flow, SocialRobotClient, var

The node classes were internal until 0.7.0 (the platform's seeds and e2e
drivers still build their fixtures with them); since C18 (2026-10-13) they
are the documented way to write a flow in code.
"""
from .builder import (
    Agent,
    Button,
    Call,
    Character,
    Data,
    DataSource,
    DeployedCharacter,
    Hook,
    Identify,
    Judgment,
    Mirror,
    Node,
    OnApiPoll,
    OnEnter,
    OnFace,
    OnPlaybackDone,
    OnSchedule,
    OnSensor,
    OnTimeout,
    OnUtterance,
    OnVision,
    OnWebSocket,
    OnWs,
    Return,
    Say,
    Screen,
    Script,
    subflow,
)
from .flow import NEXT, Cond, Flow, Program, Var, var
from .program import Answer, ProgramCall
from .client import MfaRequiredError, SocialRobotClient, SocialRobotError
from .robot import Robot, RobotBody
from .runtime import ExternalStream, RobotSession, SubscribeStream

__version__ = "0.8.0"

__all__ = [
    "__version__",
    # runtime
    "ExternalStream", "MfaRequiredError", "Robot", "RobotBody", "RobotSession", "SocialRobotClient",
    "SocialRobotError", "SubscribeStream",
    # authoring
    "Agent", "Button", "Call", "Character", "Data", "DataSource", "DeployedCharacter", "Hook",
    "Identify", "Judgment", "Mirror", "Node", "OnApiPoll", "OnEnter", "OnFace", "OnPlaybackDone",
    "OnSchedule", "OnSensor", "OnTimeout", "OnUtterance", "OnVision", "OnWebSocket", "OnWs",
    "Return", "Say", "Screen", "Script", "subflow",
    # run-time decisions in a flow (C18)
    "Cond", "Flow", "NEXT", "Var", "var",
    # 程式節點: your code, asked while the robot runs
    "Answer", "Program", "ProgramCall",
]
