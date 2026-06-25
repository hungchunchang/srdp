"""Synchronous REST client for the Social Robot Platform.

Auth: either a developer API key (``srk_…``, generated in UserSettings —
sent as a Bearer token; the backend's get_current_user accepts it on every
endpoint including /robot/register) or email+password (logs in for a JWT).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx


class SocialRobotClient:
    def __init__(
        self,
        base_url: str = "http://localhost:6868/api/v1",
        api_key: Optional[str] = None,
        email: Optional[str] = None,
        password: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(timeout=timeout)
        if api_key:
            self._token = api_key
        elif email and password:
            r = self._http.post(f"{self.base_url}/auth/login",
                                json={"email": email, "password": password})
            r.raise_for_status()
            self._token = r.json()["access_token"]
        else:
            raise ValueError("provide api_key or email+password")

    # ---------- plumbing ----------

    @property
    def token(self) -> str:
        """The resolved bearer (API key or minted JWT) — used to auth the
        inbound stream websocket via its ?token= query param."""
        return self._token

    @property
    def _auth(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _req(self, method: str, path: str, **kw) -> Any:
        r = self._http.request(method, f"{self.base_url}{path}", headers=self._auth, **kw)
        r.raise_for_status()
        return r.json() if r.content else None

    # ---------- characters ----------

    def list_characters(self) -> List[dict]:
        return self._req("GET", "/characters/")

    def create_character(self, name: str, description: str = "", is_public: bool = False,
                         robot_type: str = "kebbi", **extra) -> dict:
        return self._req("POST", "/characters/", json={
            "name": name, "description": description,
            "is_public": is_public, "robot_type": robot_type, **extra,
        })

    def update_character(self, character_id: str, **fields) -> dict:
        return self._req("PUT", f"/characters/{character_id}", json=fields)

    def delete_character(self, character_id: str) -> None:
        self._req("DELETE", f"/characters/{character_id}")

    # ---------- workflows ----------

    def create_workflow(self, character_id: str, payload: dict) -> dict:
        return self._req("POST", f"/characters/{character_id}/workflows", json=payload)

    def update_workflow(self, workflow_id: str, payload: dict) -> dict:
        return self._req("PUT", f"/workflows/{workflow_id}", json=payload)

    def put_agent_config(self, workflow_id: str, payload: dict) -> dict:
        return self._req("PUT", f"/workflows/{workflow_id}/agent-config", json=payload)

    def bulk_scripts(self, workflow_id: str, lines: List[dict]) -> Any:
        return self._req("POST", f"/workflows/{workflow_id}/scripts/bulk", json={"lines": lines})

    def get_scripts(self, workflow_id: str) -> List[dict]:
        return self._req("GET", f"/workflows/{workflow_id}/scripts")

    def update_script(self, script_id: str, payload: dict) -> dict:
        return self._req("PUT", f"/scripts/{script_id}", json=payload)

    # ---------- assets ----------

    def upload_expression(self, path: str, name: str) -> dict:
        p = Path(path)
        with p.open("rb") as f:
            r = self._http.post(
                f"{self.base_url}/expressions/upload", headers=self._auth,
                data={"name": name},
                files={"file": (p.name, f, "video/mp4")},
            )
        r.raise_for_status()
        return r.json()

    def upload_media(self, path: str) -> dict:
        """Upload an audio/video media file (mp3/m4a/wav/ogg/webm/mp4) to the
        platform's media store. Returns ``{"url", "signed_url"}`` — use
        ``signed_url`` for a client to fetch/play it (e.g. a WoZ line's
        ``Say(audio=...)`` / an injected ``{{clip_url}}``)."""
        import mimetypes
        p = Path(path)
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        with p.open("rb") as f:
            r = self._http.post(
                f"{self.base_url}/uploads/conversation-media", headers=self._auth,
                files={"file": (p.name, f, ctype)},
            )
        r.raise_for_status()
        return r.json()

    def upload_vision_model(self, path: str, name: str, model_type: str = "onnx",
                            labels: Optional[List[str]] = None) -> dict:
        """Upload a custom classifier (.onnx/.pt/.tflite/.h5/.weights) for
        on-demand inference. Returns the VisionModel dict (its ``id`` is what
        a Vision switch references). `labels` must match the model's output
        order — they're the values a Vision switch's cases compare against."""
        import json as _json
        p = Path(path)
        with p.open("rb") as f:
            r = self._http.post(
                f"{self.base_url}/vision-models/upload", headers=self._auth,
                data={"name": name, "model_type": model_type,
                      "labels": _json.dumps(labels or [])},
                files={"file": (p.name, f, "application/octet-stream")},
            )
        r.raise_for_status()
        return r.json()

    def list_vision_models(self) -> List[dict]:
        return self._req("GET", "/vision-models/")

    def delete_vision_model(self, model_id: str) -> None:
        self._req("DELETE", f"/vision-models/{model_id}")

    # ---------- runtime / sessions ----------

    def register_robot(self, character_id: str, device_id: str = "sdk") -> dict:
        return self._req("POST", "/robot/register", json={
            "device_id": device_id, "role": character_id, "app_version": "sdk",
        })

    def list_sessions(self) -> List[dict]:
        return self._req("GET", "/sessions/")

    def inject(self, session_id: str, event: Optional[str] = None,
               variables: Optional[Dict[str, Any]] = None) -> dict:
        """One-shot push into a live session: variables feed VARIABLE
        switches / templates; the event fires sensor hooks and SENSOR
        switches. For high-frequency streaming use open_stream()."""
        return self._req("POST", f"/sessions/{session_id}/events",
                         json={"event": event, "variables": variables})

    async def open_stream(self, session_id: str):
        """Open a persistent inbound stream (await it). Returns an
        ExternalStream async context manager for pushing many
        events/variables. See runtime.ExternalStream."""
        from .runtime import ExternalStream
        return await ExternalStream.open(self, session_id)

    async def subscribe(self, session_id: str):
        """Open a persistent OUTBOUND stream (await it). Returns a
        SubscribeStream — `async for ev in stream` yields the robot's events
        (sensor/client events + state/stage transitions) as dicts. The inverse
        of open_stream/inject. See runtime.SubscribeStream."""
        from .runtime import SubscribeStream
        return await SubscribeStream.open(self, session_id)

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
