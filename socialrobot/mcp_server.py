"""MCP server exposing the Social Robot platform as agent tools.

Wraps the ``socialrobot`` SDK so a terminal agent (Claude Code, etc.) can drive
the platform conversationally: list characters/sessions, **inject events** into a
live session (buzzer / quiz control), upload media clips, and read a flow.

Config via env:
  SOCIALROBOT_BASE_URL   default http://localhost:6868/api/v1
                         (prod: https://sociallab.duckdns.org/ntu_social_robot/api/v1)
  SOCIALROBOT_API_KEY    an `srk_…` developer key (preferred), OR
  SOCIALROBOT_EMAIL + SOCIALROBOT_PASSWORD

Install + run:
  pip install mcp
  SOCIALROBOT_API_KEY=srk_… python -m socialrobot.mcp_server

Register with Claude Code:
  claude mcp add socialrobot \
    -e SOCIALROBOT_API_KEY=srk_… \
    -e SOCIALROBOT_BASE_URL=https://sociallab.duckdns.org/ntu_social_robot/api/v1 \
    -- python -m socialrobot.mcp_server
"""
from __future__ import annotations

import json
import os
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .client import SocialRobotClient

mcp = FastMCP("socialrobot")


def _client() -> SocialRobotClient:
    return SocialRobotClient(
        base_url=os.environ.get("SOCIALROBOT_BASE_URL", "http://localhost:6868/api/v1"),
        api_key=os.environ.get("SOCIALROBOT_API_KEY"),
        email=os.environ.get("SOCIALROBOT_EMAIL"),
        password=os.environ.get("SOCIALROBOT_PASSWORD"),
    )


@mcp.tool()
def list_characters() -> list:
    """List your characters (id, name, is_public)."""
    with _client() as c:
        return [
            {"id": x["id"], "name": x.get("name"), "is_public": x.get("is_public")}
            for x in c.list_characters()
        ]


@mcp.tool()
def list_sessions() -> list:
    """List your running robot sessions (session_id, character, device, state).
    A session must be running (simulator/app/robot connected) to inject into it."""
    with _client() as c:
        return c.list_sessions()


@mcp.tool()
def inject_event(session_id: str, event: Optional[str] = None,
                 variables_json: Optional[str] = None) -> dict:
    """Push an event and/or variables into a live session — the same channel the
    control panel uses. Examples:
      buzz          variables {"winner": "小明"}
      judged        variables {"result": "correct"}   (correct|wrong|skip)
      next_question variables {"answer": "海水聲", "clip_url": "/uploads/conv_xxx.mp3?sig=…"}
    Variables feed {{templates}} / VARIABLE switches; the event fires sensor hooks.
    `variables_json` is a JSON object string (e.g. '{"winner":"小明"}')."""
    variables = json.loads(variables_json) if variables_json else None
    with _client() as c:
        return c.inject(session_id, event=event, variables=variables)


@mcp.tool()
def upload_media(path: str) -> dict:
    """Upload an audio/video clip (mp3/m4a/wav/ogg/webm/mp4) to the platform's
    media store. Returns {"url", "signed_url"} — use `signed_url` as a clip_url
    (e.g. inject_event next_question with clip_url=…) or a Say(audio=…)."""
    with _client() as c:
        return c.upload_media(path)


@mcp.tool()
def get_scripts(workflow_id: str) -> list:
    """Read a workflow's WoZ script lines (text_content, audio_clip_url, expression,
    motion, line_order) — inspect what a node says/plays."""
    with _client() as c:
        return c.get_scripts(workflow_id)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
