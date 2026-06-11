from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings
from .folder_picker import pick_folder_native
from .harness import HarnessRunner
from .opencode_client import OpenCodeClient, OpenCodeError
from .session_status import (
    BUSY,
    build_session_status_map,
    clear_session_busy,
    is_visible_session,
    mark_session_busy,
    messages_indicate_busy,
    session_updated_at,
)
from .status_stream import SessionStatusStream
from .store import Store
from .task_spec import parse_task_spec


# Build settings. ``pydantic-settings`` already loads ``.env`` from the
# current working directory, so Windows / macOS / Linux all "just work".
settings = Settings()

if not settings.opencode_username or not settings.opencode_password:
    import sys

    print(
        "[opendeck] WARNING: OPENCODE_SERVER_USERNAME / OPENCODE_SERVER_PASSWORD "
        "are not set. The dashboard will not be able to talk to opencode serve "
        "until you fill them in .env. See .env.example for details.",
        file=sys.stderr,
    )
store = Store(settings.database_path)
opencode = OpenCodeClient(
    settings.opencode_url,
    settings.opencode_username,
    settings.opencode_password,
)
status_stream = SessionStatusStream(
    settings.opencode_url,
    settings.opencode_username,
    settings.opencode_password,
)
harness = HarnessRunner(
    store,
    opencode,
    settings.is_allowed_workspace,
    auto_decide=settings.opendeck_auto_decide,
    auto_decide_max=settings.opendeck_auto_decide_max,
)


MESSAGE_BUSY_PROBE_WINDOW_SECONDS = 600
MESSAGE_BUSY_PROBE_LIMIT = 12
# Cache per-session busy probes for a short window so that the dashboard's
# 5-second poll does not fan out N HTTP messages calls every refresh.
_PROBE_CACHE_TTL_SECONDS = 8.0
_probe_cache: dict[str, tuple[float, bool]] = {}


async def _probe_message_busy_sessions(
    sessions: list[dict[str, Any]],
    *,
    now: float,
) -> dict[str, bool]:
    candidates = [
        session
        for session in sessions
        if is_visible_session(session)
        and session_updated_at(session) >= now - MESSAGE_BUSY_PROBE_WINDOW_SECONDS
    ]
    candidates.sort(key=session_updated_at, reverse=True)
    candidates = candidates[:MESSAGE_BUSY_PROBE_LIMIT]

    # Evict stale entries.
    stale = [sid for sid, (ts, _) in _probe_cache.items() if now - ts > _PROBE_CACHE_TTL_SECONDS * 4]
    for sid in stale:
        _probe_cache.pop(sid, None)

    cached: dict[str, bool] = {}
    todo: list[dict[str, Any]] = []
    for session in candidates:
        sid = session["id"]
        hit = _probe_cache.get(sid)
        if hit and now - hit[0] < _PROBE_CACHE_TTL_SECONDS:
            cached[sid] = hit[1]
        else:
            todo.append(session)

    async def probe(session: dict[str, Any]) -> tuple[str, bool]:
        session_id = session["id"]
        try:
            messages = await opencode.messages(session_id, limit=4)
        except Exception:  # noqa: BLE001
            return session_id, False
        return session_id, messages_indicate_busy(messages)

    if todo:
        results = await asyncio.gather(*(probe(session) for session in todo))
        for sid, busy in results:
            _probe_cache[sid] = (now, busy)
            cached[sid] = busy
    return cached


async def _harness_loop() -> None:
    while True:
        try:
            await harness.tick()
        except Exception:
            pass
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(_: FastAPI):
    status_stream.start()
    task = asyncio.create_task(_harness_loop())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await status_stream.stop()


app = FastAPI(title="OpenDeck", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Optional Basic Auth — for users that expose OpenDeck on a LAN/VPN.
# ---------------------------------------------------------------------------
if settings.basic_auth_enabled:
    import secrets

    @app.middleware("http")
    async def _basic_auth_gate(request, call_next):
        if request.url.path.startswith("/api"):
            header = request.headers.get("authorization") or ""
            if not header.lower().startswith("basic "):
                return _basic_auth_unauthorized()
            try:
                import base64

                decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8", "replace")
                user, _, password = decoded.partition(":")
            except Exception:
                return _basic_auth_unauthorized()
            user_ok = secrets.compare_digest(user, settings.opendeck_basic_auth_user)
            pass_ok = secrets.compare_digest(password, settings.opendeck_basic_auth_pass)
            if not (user_ok and pass_ok):
                return _basic_auth_unauthorized()
        return await call_next(request)


def _basic_auth_unauthorized():
    from fastapi.responses import Response

    return Response(
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="OpenDeck"'},
        content="Authentication required",
    )


# ---------------------------------------------------------------------------
# Request models.
# ---------------------------------------------------------------------------
class WorkspaceTarget(BaseModel):
    type: Literal["workspace"]
    cwd: str


class SessionTarget(BaseModel):
    type: Literal["session"]
    sessionId: str


class DispatchRequest(BaseModel):
    """Legacy one-shot dispatch. Internally converted to a Harness task with
    ``mode: ephemeral``."""

    target: WorkspaceTarget | SessionTarget
    agent: str = "opencode"
    mode: str = "normal"
    prompt: str = Field(min_length=1)


class HarnessTaskRequest(BaseModel):
    """Unified task submission. ``workspace`` is required when ``sessionId``
    is not provided. When ``sessionId`` is given, OpenDeck attaches the
    harness to the existing session instead of creating a new one."""

    spec: str = Field(min_length=1)
    format: Literal["yaml", "markdown"] = "yaml"
    session_id: str | None = Field(default=None, alias="sessionId")
    bind: bool = Field(
        default=False,
        description=(
            "When true and ``sessionId`` is set, attach the harness to the "
            "existing session for periodic check-ins. Otherwise the session "
            "is treated as a one-shot ephemeral task."
        ),
    )

    model_config = {"populate_by_name": True}


class PickFolderRequest(BaseModel):
    initial: str | None = None


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
def _workspace_access_error() -> str:
    return (
        "Workspace is outside allowed roots"
        if settings.strict_roots
        else "Workspace path does not exist or is not a directory"
    )


def _resolve_browse_path(path: str | None) -> Path:
    if path:
        return Path(path).expanduser().resolve()
    recent = store.list_recent_workspaces(limit=1)
    if recent:
        return Path(recent[0]).expanduser().resolve()
    return Path.home().resolve()


def _title_from_prompt(prompt: str) -> str:
    first = " ".join(prompt.strip().split())
    if not first:
        return "Untitled"
    return first[:72] + ("…" if len(first) > 72 else "")


def _status_counts(tasks: list[dict[str, Any]]) -> dict[str, int]:
    running = 0
    waiting = 0
    failed = 0
    completed = 0

    for task in tasks:
        status = str(task.get("status", "")).lower()
        if status == "waiting":
            waiting += 1
        elif status == "failed":
            failed += 1
        elif status in {"completed", "archived"}:
            completed += 1
        elif status in {"running", "pending"}:
            running += 1

    return {
        "running": running,
        "waiting": waiting,
        "failed": failed,
        "completedToday": completed,
    }


# ---------------------------------------------------------------------------
# Routes.
# ---------------------------------------------------------------------------
@app.get("/api/state")
async def state() -> dict[str, Any]:
    health = await opencode.health()
    tasks = store.list_harness_tasks()
    sessions: list[dict[str, Any]] = []
    session_status: dict[str, str] = {}
    session_error: str | None = None

    if health.ok:
        try:
            sessions = await opencode.list_sessions()
            now = time.time()
            raw_session_status = await opencode.session_status()
            message_busy = await _probe_message_busy_sessions(sessions, now=now)
            status_stream.seed_from_raw(raw_session_status)
            for session_id, busy in message_busy.items():
                if busy:
                    raw_session_status[session_id] = {"type": BUSY}
                    mark_session_busy(session_id, now=now)
                    status_stream.set_status(session_id, BUSY)
                else:
                    clear_session_busy(session_id)
                    status_stream.clear_status(session_id)
            merged_session_status = status_stream.merge_raw(raw_session_status)
            session_status = build_session_status_map(
                sessions,
                merged_session_status,
                now=now,
                stream_status_for=status_stream,
                message_busy=message_busy,
            )
        except Exception as exc:  # noqa: BLE001
            session_error = str(exc)

    try:
        archived_sessions = [
            s
            for s in sessions
            if isinstance(s.get("time"), dict) and (s["time"].get("archived") or 0) > 0
        ]
    except Exception:
        archived_sessions = []

    visible_sessions = sorted(
        [s for s in sessions if is_visible_session(s)],
        key=session_updated_at,
        reverse=True,
    )

    sessions_by_directory: dict[str, list[dict[str, Any]]] = {}
    for session in visible_sessions:
        directory = session.get("directory") or "(unknown)"
        sessions_by_directory.setdefault(directory, []).append(session)

    for path, items in sessions_by_directory.items():
        sessions_by_directory[path] = sorted(
            items,
            key=session_updated_at,
            reverse=True,
        )

    unknown_sessions = sessions_by_directory.pop("(unknown)", None)
    ordered_directories = sorted(
        sessions_by_directory.items(),
        key=lambda item: max(session_updated_at(s) for s in item[1]) if item[1] else 0,
        reverse=True,
    )
    sessions_by_directory = dict(ordered_directories)
    if unknown_sessions is not None:
        sessions_by_directory["(unknown)"] = unknown_sessions

    recent_workspaces = store.list_recent_workspaces()
    if not recent_workspaces:
        session_directories = [
            session.get("directory")
            for session in sessions
            if session.get("directory") and is_visible_session(session)
        ]
        store.seed_recent_workspaces_from_sessions(session_directories)
        recent_workspaces = store.list_recent_workspaces()

    return {
        "server": {
            "ok": health.ok,
            "message": health.message,
            "status_code": health.status_code,
            "url": settings.opencode_url,
            "username": settings.opencode_username or None,
            "configured": bool(settings.opencode_username and settings.opencode_password),
        },
        "config": {
            "autoDecide": settings.opendeck_auto_decide,
            "autoDecideMax": settings.opendeck_auto_decide_max,
            "basicAuth": settings.basic_auth_enabled,
        },
        "recentWorkspaces": recent_workspaces,
        "tasks": tasks,
        "sessions": visible_sessions,
        "sessionsByDirectory": sessions_by_directory,
        "archivedSessions": [
            {
                "id": s["id"],
                "title": s.get("title", ""),
                "directory": s.get("directory", ""),
                "archived_at": s.get("time", {}).get("archived"),
            }
            for s in archived_sessions
        ],
        "sessionStatus": session_status,
        "sessionError": session_error,
        "metrics": _status_counts(tasks),
        "now": time.time(),
    }


@app.post("/api/dispatch")
async def dispatch_message(request: DispatchRequest) -> dict[str, Any]:
    """One-shot dispatch (legacy).

    Translated into a unified Harness task with ``mode: ephemeral`` so the
    Task and Harness code paths share the same execution engine.
    """

    target = request.target
    title = _title_from_prompt(request.prompt)
    session_id: str | None = None
    cwd: str | None = None

    try:
        if target.type == "workspace":
            cwd = str(Path(target.cwd).expanduser().resolve())
            if not settings.is_allowed_workspace(cwd):
                detail = (
                    "Workspace is outside allowed roots"
                    if settings.strict_roots
                    else "Workspace path does not exist or is not a directory"
                )
                raise HTTPException(status_code=400, detail=detail)
            session = await opencode.create_session(cwd=cwd, title=title)
            session_id = session["id"]
            store.record_recent_workspace(cwd)
        else:
            session_id = target.sessionId

        agent = None if request.agent == "opencode" else request.agent
        await opencode.send_prompt_async(
            session_id=session_id,
            prompt=request.prompt,
            agent=agent,
        )
        return {
            "ok": True,
            "sessionId": session_id,
            "title": title,
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/tasks")
async def create_harness_task(request: HarnessTaskRequest) -> dict[str, Any]:
    try:
        spec = parse_task_spec(request.spec, request.format)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Invalid task spec: {exc}") from exc

    session_id: str | None = request.session_id
    bind = bool(request.bind) and bool(session_id)

    # When attaching to an existing session we still want the spec to carry a
    # workspace so the dashboard can show a path. Fall back to the session's
    # directory, then to a synthetic value the user can ignore.
    cwd: str | None = None
    if session_id:
        try:
            sessions = await opencode.list_sessions()
        except Exception:
            sessions = []
        match = next((s for s in sessions if s.get("id") == session_id), None)
        if match and match.get("directory"):
            cwd = str(Path(match["directory"]).expanduser().resolve())
        if cwd is None:
            cwd = spec.workspace or "(attached)"
    else:
        if not spec.workspace:
            raise HTTPException(status_code=400, detail="Task spec must include workspace")
        cwd = str(Path(spec.workspace).expanduser().resolve())
        if not settings.is_allowed_workspace(cwd):
            detail = (
                "Workspace is outside allowed roots"
                if settings.strict_roots
                else "Workspace path does not exist or is not a directory"
            )
            raise HTTPException(status_code=400, detail=detail)
        spec.workspace = cwd

    task_id = f"task_{uuid.uuid4().hex[:12]}"
    now = time.time()
    task = {
        "id": task_id,
        "name": spec.name,
        "spec_format": request.format,
        "spec_text": request.spec,
        "spec": spec.to_dict(),
        "workspace": cwd,
        "mode": "ephemeral" if (session_id and not bind) else spec.mode,
        "agent": spec.agent,
        "status": "pending",
        "check_interval_seconds": spec.check_interval_seconds,
        "next_check_at": now,
        "active_session_id": session_id,
        "bind_to_session": bind,
    }
    store.create_harness_task(task)
    if not session_id and cwd and cwd != "(attached)":
        store.record_recent_workspace(cwd)

    return {
        "ok": True,
        "taskId": task_id,
        "status": "pending",
        "name": spec.name,
        "boundSessionId": session_id,
    }


@app.get("/api/tasks/{task_id}")
async def get_harness_task(task_id: str) -> dict[str, Any]:
    task = store.get_harness_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.post("/api/tasks/{task_id}/pause")
async def pause_harness_task(task_id: str) -> dict[str, Any]:
    task = store.get_harness_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    store.update_harness_task(task_id, status="paused", next_check_at=None)
    return {"ok": True, "taskId": task_id, "status": "paused"}


@app.post("/api/tasks/{task_id}/resume")
async def resume_harness_task(task_id: str) -> dict[str, Any]:
    task = store.get_harness_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.get("status") in {"completed", "failed", "archived"}:
        store.update_harness_task(
            task_id,
            status="running",
            next_check_at=time.time(),
            last_summary="Resumed manually",
            error=None,
        )
    else:
        store.update_harness_task(task_id, status="running", next_check_at=time.time())
    return {"ok": True, "taskId": task_id, "status": "running"}


@app.post("/api/tasks/{task_id}/complete")
async def complete_harness_task(task_id: str) -> dict[str, Any]:
    task = store.get_harness_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    store.update_harness_task(
        task_id,
        status="completed",
        next_check_at=None,
        last_summary="Marked complete manually",
        progress=1.0,
    )
    store.append_harness_check(
        task_id,
        status="completed",
        summary="Marked complete manually",
    )
    return {"ok": True, "taskId": task_id, "status": "completed"}


@app.post("/api/tasks/{task_id}/archive")
async def archive_harness_task(task_id: str) -> dict[str, Any]:
    task = store.get_harness_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    store.update_harness_task(
        task_id,
        status="archived",
        next_check_at=None,
        last_summary="Archived manually",
    )
    store.append_harness_check(
        task_id,
        status="archived",
        summary="Archived manually",
    )
    session_id = task.get("active_session_id")
    if session_id:
        with contextlib.suppress(Exception):
            await opencode.set_archived(session_id, int(time.time() * 1000))
    return {"ok": True, "taskId": task_id, "status": "archived"}


@app.get("/api/sessions/{session_id}/messages")
async def session_messages(session_id: str) -> dict[str, Any]:
    try:
        return {"messages": await opencode.messages(session_id)}
    except OpenCodeError as exc:
        raise HTTPException(status_code=502, detail=f"opencode: {exc.message}")


@app.get("/api/sessions/{session_id}/diff")
async def session_diff(session_id: str) -> dict[str, Any]:
    try:
        return {"diff": await opencode.diff(session_id)}
    except OpenCodeError as exc:
        raise HTTPException(status_code=502, detail=f"opencode: {exc.message}")


@app.post("/api/sessions/{session_id}/archive")
async def archive_session(session_id: str) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    try:
        await opencode.set_archived(session_id, now_ms)
    except OpenCodeError as exc:
        raise HTTPException(status_code=502, detail=f"opencode: {exc.message}")
    return {"ok": True, "sessionId": session_id, "archived": True}


@app.delete("/api/sessions/{session_id}/archive")
async def unarchive_session(session_id: str) -> dict[str, Any]:
    try:
        await opencode.set_archived(session_id, 0)
    except OpenCodeError as exc:
        raise HTTPException(status_code=502, detail=f"opencode: {exc.message}")
    return {"ok": True, "sessionId": session_id, "archived": False}


@app.post("/api/sessions/{session_id}/delete")
async def hard_delete_session(session_id: str) -> dict[str, Any]:
    try:
        ok = await opencode.delete_session(session_id)
    except OpenCodeError as exc:
        raise HTTPException(status_code=502, detail=f"opencode: {exc.message}")
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found or already deleted")
    return {"ok": True, "sessionId": session_id, "deleted": True}


@app.post("/api/pick-folder")
async def pick_folder(request: PickFolderRequest | None = None) -> dict[str, Any]:
    if platform.system() not in {"Darwin", "Linux"}:  # type: ignore[name-defined]
        return {"ok": False, "unsupported": True}
    initial = request.initial if request else None
    picked = await asyncio.to_thread(pick_folder_native, initial)
    if not picked:
        return {"ok": False, "cancelled": True}
    if not settings.is_allowed_workspace(picked):
        raise HTTPException(status_code=400, detail=_workspace_access_error())
    store.record_recent_workspace(picked)
    return {"ok": True, "path": picked}


@app.delete("/api/recent-workspaces")
async def remove_recent_workspace(path: str = Query(...)) -> dict[str, Any]:
    removed = store.remove_recent_workspace(path)
    return {"ok": True, "removed": removed, "path": path}


@app.get("/api/browse")
async def browse(path: str | None = Query(default=None)) -> dict[str, Any]:
    root_path = _resolve_browse_path(path)
    if not settings.is_allowed_workspace(str(root_path)):
        raise HTTPException(status_code=400, detail="Path is not accessible")
    if not root_path.exists() or not root_path.is_dir():
        raise HTTPException(status_code=404, detail="Directory not found")

    children = []
    for child in sorted(root_path.iterdir(), key=lambda p: p.name.lower()):
        if child.is_dir() and not child.name.startswith("."):
            children.append({"name": child.name, "path": str(child)})
    parent = root_path.parent
    return {
        "path": str(root_path),
        "parent": str(parent) if parent != root_path else None,
        "children": children[:200],
    }


dist_dir = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if dist_dir.exists():
    app.mount("/assets", StaticFiles(directory=dist_dir / "assets"), name="assets")

    @app.middleware("http")
    async def no_cache_assets(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        return response


@app.get("/{path:path}", response_model=None)
async def index(path: str = ""):
    index_file = dist_dir / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {
        "message": "OpenDeck API is running. Start the Svelte dev server with `npm run dev` in frontend/.",
    }


# ---------------------------------------------------------------------------
# Entry point. ``pydantic-settings`` loads ``.env`` automatically, so this
# command works on Windows / macOS / Linux without any ``set -a`` dance.
# ---------------------------------------------------------------------------
def main() -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.opendeck_host,
        port=settings.opendeck_port,
        reload=False,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
