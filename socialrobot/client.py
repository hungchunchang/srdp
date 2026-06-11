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

    # ---------- runtime / sessions ----------

    def register_robot(self, character_id: str, device_id: str = "sdk") -> dict:
        return self._req("POST", "/robot/register", json={
            "device_id": device_id, "role": character_id, "app_version": "sdk",
        })

    def list_sessions(self) -> List[dict]:
        return self._req("GET", "/sessions/")

    def inject(self, session_id: str, event: Optional[str] = None,
               variables: Optional[Dict[str, Any]] = None) -> dict:
        """Push an external result into a live session: variables feed
        VARIABLE switches / templates; the event fires sensor hooks and
        SENSOR switches."""
        return self._req("POST", f"/sessions/{session_id}/events",
                         json={"event": event, "variables": variables})

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
