"""Social Robot Platform SDK — a RUNTIME client, not an authoring tool.

The supported public API is the runtime half: connect a non-Android robot
body (or any program) to a live platform session, and drive/observe it.

    from socialrobot import SocialRobotClient, RobotSession

`RobotSession` is how a NAO, a Raspberry Pi rig, or any Python-driven body
participates as a robot — meme is the brain, your process is the body (see
`examples/nao_meme_bridge.py`). `SocialRobotClient` is the authenticated REST
client underneath it, plus the asset/session endpoints those bodies need.

INTERNAL, NOT PUBLIC API — the Builder DSL (`Character`, `Script`, `Say`,
`Agent`, `Screen`, `Identify`, hooks, …). Authoring a character is the
WEB EDITOR's job: a Builder script still has to `deploy()` its result up to
the backend, so it buys nothing a designer can't do on the canvas, and the
canvas is the surface that actually gets maintained. The module stays in the
repo because the platform's own seed scripts and ~15 e2e drivers provision
their fixture characters with it (`server/scripts/seed_*.py`,
`server/scripts/e2e_*.py`) — it is test/provisioning infrastructure now.

Those names are still importable so that in-repo tooling keeps working, but
they are deliberately absent from `__all__`, excluded from the documented
surface, and carry no compatibility promise for external callers. Do not
build anything new on them; use the web editor, or the REST API directly.
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
from .client import MfaRequiredError, SocialRobotClient, SocialRobotError
from .robot import Robot, RobotBody
from .runtime import ExternalStream, RobotSession, SubscribeStream

__version__ = "0.6.0"

# The PUBLIC surface: the runtime client only. Builder DSL names are
# imported above for in-repo tooling (seeds + e2e drivers) but are
# deliberately NOT listed here — `__all__` is what this package promises.
__all__ = [
    "__version__",
    "ExternalStream", "MfaRequiredError", "Robot", "RobotBody", "RobotSession", "SocialRobotClient",
    "SocialRobotError",
    "SubscribeStream",
]
