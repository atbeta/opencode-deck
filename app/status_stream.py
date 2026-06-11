from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

import httpx

from .session_status import BUSY, IDLE, RETRY, extract_status_type, normalize_session_status


def _status_priority(value: Any) -> int:
    text = extract_status_type(value) or IDLE
    if text in {BUSY, "running", "streaming", "working"}:
        return 3
    if text in {RETRY, "waiting", "permission"}:
        return 2
    return 1


class SessionStatusStream:
    """Subscribe to OpenCode /global/event for live session.status updates."""

    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = (username, password)
        self._live: dict[str, str] = {}
        self._seen_at: dict[str, float] = {}
        self._task: asyncio.Task[None] | None = None
        self._running = False

    def start(self) -> None:
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    def get(self, session_id: str) -> str | None:
        return self._live.get(session_id)

    def set_status(self, session_id: str, status: str) -> None:
        self._live[session_id] = status
        self._seen_at[session_id] = time.time()

    def clear_status(self, session_id: str) -> None:
        self._live.pop(session_id, None)
        self._seen_at.pop(session_id, None)

    def seed_from_raw(self, raw: dict[str, Any]) -> None:
        now = time.time()
        for session_id, value in raw.items():
            status = normalize_session_status(value, default=None)
            if status in {BUSY, RETRY}:
                self._live[session_id] = status
                self._seen_at[session_id] = now

    def merge_raw(self, raw: dict[str, Any]) -> dict[str, Any]:
        merged = dict(raw)
        for session_id, status in self._live.items():
            current = merged.get(session_id)
            if _status_priority({"type": status}) > _status_priority(current):
                merged[session_id] = {"type": status}
        return merged

    async def _run(self) -> None:
        while self._running:
            try:
                async with httpx.AsyncClient(
                    base_url=self.base_url,
                    auth=self.auth,
                    timeout=None,
                    transport=httpx.AsyncHTTPTransport(trust_env=False),
                ) as client:
                    async with client.stream("GET", "/global/event") as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if not self._running:
                                break
                            if not line.startswith("data: "):
                                continue
                            try:
                                envelope = json.loads(line[6:])
                            except json.JSONDecodeError:
                                continue
                            payload = envelope.get("payload") or envelope
                            if payload.get("type") != "session.status":
                                continue
                            properties = payload.get("properties") or {}
                            session_id = properties.get("sessionID")
                            if not session_id:
                                continue
                            status = normalize_session_status(properties.get("status"))
                            self._live[session_id] = status
                            self._seen_at[session_id] = time.time()
            except Exception:
                if self._running:
                    await asyncio.sleep(2)
