"""Drive a live robot session from a *local* computer's camera over Tailscale.

This is a recipe, not a platform feature — it's built entirely on the SDK's
existing inbound APIs (`client.inject` / `client.list_sessions`). A laptop on
your tailnet runs face detection locally and pushes events into a running robot
session; the robot reacts through its sensor hooks / variable switches. The
robot backend never sees the camera — only the events you inject.

Tailscale is just networking: point `SR_BASE_URL` at the backend's tailnet
address. Auth is a developer API key (`srk_…`, generate it on the web under
個人設定 → API Key) — the same key the Android app's "API Key" login accepts.

    pip install opencv-python httpx
    export SR_BASE_URL="http://<tailnet-ip-or-host>:<backend port>/api/v1"
    export SR_API_KEY="srk_..."
    # optional: target a specific session; otherwise the first live one is used
    export SR_SESSION_ID="..."
    python tailscale_camera_driver.py

Robot side: give the character a stage whose hook reacts to the injected
events — see README.md for the Builder-DSL snippet, e.g.
    Script("待命", loop=True, idle_expression=Mirror,
        hooks=[OnSensor("person_arrived", goto="打招呼")])
"""
from __future__ import annotations

import os
import sys
import time

import cv2

# Import the installed SDK, or fall back to the repo checkout one dir up.
try:
    from socialrobot import SocialRobotClient
except ImportError:  # running straight from the repo
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from socialrobot import SocialRobotClient


# Inject at most this often; face detection per frame is cheap but we don't
# want to spam the session — events are state-transition driven below anyway.
MIN_INJECT_INTERVAL_S = 0.5
# Require this many consecutive frames of the new presence state before
# reporting it, so a one-frame miss doesn't flap person_arrived/person_left.
CONFIRM_FRAMES = 3


def _pick_session(client: SocialRobotClient) -> str:
    explicit = os.getenv("SR_SESSION_ID")
    if explicit:
        return explicit
    sessions = client.list_sessions()
    if not sessions:
        sys.exit("No live robot sessions. Start the robot/app first, or set SR_SESSION_ID.")
    sid = sessions[0].get("session_id") or sessions[0].get("id")
    print(f"Using live session {sid} ({len(sessions)} available)")
    return sid


def main() -> int:
    base_url = os.getenv("SR_BASE_URL")
    api_key = os.getenv("SR_API_KEY")
    if not base_url or not api_key:
        sys.exit("Set SR_BASE_URL (…/api/v1) and SR_API_KEY to a srk_… developer key (web: 個人設定 → API Key).")

    client = SocialRobotClient(base_url=base_url, api_key=api_key)
    session_id = _pick_session(client)

    cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        sys.exit("Could not open the local camera (index 0).")

    print(f"Driving session {session_id} from the local camera. Ctrl+C to stop.")
    present = None            # last *reported* presence (None until first confirm)
    candidate = None          # pending new state being confirmed
    streak = 0
    last_count = -1
    last_inject = 0.0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.1)
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(gray, 1.2, 5, minSize=(60, 60))
            count = len(faces)
            now_present = count > 0

            # Debounce the presence transition.
            if now_present == candidate:
                streak += 1
            else:
                candidate, streak = now_present, 1

            now = time.monotonic()
            throttled = now - last_inject < MIN_INJECT_INTERVAL_S

            if streak >= CONFIRM_FRAMES and candidate != present and not throttled:
                present = candidate
                event = "person_arrived" if present else "person_left"
                client.inject(session_id, event=event, variables={"people_count": count})
                last_count = count
                last_inject = now
                print(f"→ inject {event} (people_count={count})")
            elif present and count != last_count and not throttled:
                # Presence unchanged but the headcount moved — update the var so
                # VARIABLE switches / templates can read it.
                client.inject(session_id, variables={"people_count": count})
                last_count = count
                last_inject = now
                print(f"→ inject people_count={count}")

            time.sleep(0.1)  # ~10 fps detect; inject is throttled above
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        cap.release()
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
