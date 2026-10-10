"""念一句 → 停 N 秒 → 再念：from YOUR program, not the flow.

The same thing a looping script node with a 空白 step does (editor: 聲音 ›
空白), written as a program that runs on your own machine and drives a live
robot over the public API — the shape every 「掛自己的程式」 customization
takes today: your code, your machine, the platform's API.

What it shows by being written this way: from outside, a program cannot
tell when the robot has FINISHED the line, so the pause is measured from
when it was sent and padded by an estimate. The flow version waits for the
robot's own 「念完了」 and is exact.

    export SR_BASE_URL="https://<host>/api/v1"
    export SR_API_KEY="srk_..."        # web: 個人設定 → API Key
    python repeat_line.py "來找我玩" --pause 5 --times 3 [--session <id>]
"""
from __future__ import annotations

import argparse
import os
import time

from socialrobot import SocialRobotClient


def estimate_seconds(text: str) -> float:
    """Roughly how long TTS takes to say `text` — the editor's own estimate
    (0.25 s per 漢字, 0.15 s per other character, +1 s)."""
    han = sum(1 for c in text if "一" <= c <= "龥")
    return han * 0.25 + (len(text) - han) * 0.15 + 1.0


def repeat_line(client: SocialRobotClient, session_id: str, text: str,
                pause: float, times: int, sleep=time.sleep) -> list:
    """Say `text`, wait for it plus `pause`, `times` times. Returns when each
    line was sent (monotonic seconds)."""
    sent = []
    for i in range(times):
        sent.append(time.monotonic())
        results = client.say(session_id, text)
        if not any(r.get("success") for r in results):
            raise RuntimeError(f"robot did not take the line: {results}")
        if i < times - 1:
            sleep(estimate_seconds(text) + pause)
    return sent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--pause", type=float, default=5.0)
    ap.add_argument("--times", type=int, default=3)
    ap.add_argument("--session", default=os.environ.get("SR_SESSION_ID"))
    a = ap.parse_args()
    with SocialRobotClient(base_url=os.environ["SR_BASE_URL"],
                           api_key=os.environ["SR_API_KEY"]) as client:
        sid = a.session or client.list_sessions()[0]["session_id"]
        repeat_line(client, sid, a.text, a.pause, a.times)


if __name__ == "__main__":
    main()
