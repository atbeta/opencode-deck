from __future__ import annotations

from typing import Any

IDLE = "idle"
BUSY = "busy"
RETRY = "retry"


def session_parent_id(session: dict[str, Any]) -> str | None:
    for key in ("parentID", "parentId", "parent_id", "parent"):
        value = session.get(key)
        if value:
            text = str(value).strip()
            if text:
                return text
    return None


def is_subagent_session(session: dict[str, Any]) -> bool:
    return session_parent_id(session) is not None


def is_archived_session(session: dict[str, Any]) -> bool:
    time_block = session.get("time")
    if isinstance(time_block, dict):
        return (time_block.get("archived") or 0) > 0
    return False


def is_visible_session(session: dict[str, Any]) -> bool:
    """Match OpenCode desktop/TUI: hide subagent children and archived sessions."""
    if not session.get("id"):
        return False
    return not is_subagent_session(session) and not is_archived_session(session)


def session_updated_at(session: dict[str, Any]) -> float:
    value = session.get("updated")
    if isinstance(value, (int, float)) and value > 0:
        return float(value) / 1000 if value > 1e12 else float(value)

    time_block = session.get("time")
    if isinstance(time_block, dict):
        for key in ("updated", "created"):
            raw = time_block.get(key)
            if isinstance(raw, (int, float)) and raw > 0:
                return float(raw) / 1000 if raw > 1e12 else float(raw)

    for key in ("updated_at", "last_check_at", "created", "at"):
        raw = session.get(key)
        if isinstance(raw, (int, float)) and raw > 0:
            return float(raw) / 1000 if raw > 1e12 else float(raw)

    return 0.0


def extract_status_type(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip().lower()
        return text or None
    if isinstance(value, dict):
        for key in ("type", "status", "state"):
            raw = value.get(key)
            if raw:
                return str(raw).strip().lower()
    return None


def normalize_session_status(value: Any, *, default: str = IDLE) -> str:
    status_type = extract_status_type(value)
    if not status_type:
        return default
    if status_type in {BUSY, "running", "streaming", "working"}:
        return BUSY
    if status_type in {RETRY, "waiting", "permission"}:
        return RETRY
    if status_type in {IDLE, "available", "complete", "completed", "stopped", "aborted"}:
        return IDLE
    return status_type


_last_polled_updated: dict[str, float] = {}
_last_busy_at: dict[str, float] = {}
BUSY_HOLD_SECONDS = 12


def resolve_session_status(
    session: dict[str, Any],
    raw_value: Any,
    *,
    now: float,
    stream_status: str | None = None,
    message_busy: bool | None = None,
) -> str:
    session_id = session.get("id")
    if not session_id:
        return IDLE

    updated = session_updated_at(session)
    previous = _last_polled_updated.get(session_id)
    _last_polled_updated[session_id] = updated

    explicit = (
        normalize_session_status(raw_value, default=None)
        if raw_value is not None
        else None
    )

    if message_busy is True:
        _last_busy_at[session_id] = now
        return BUSY
    if message_busy is False:
        _last_busy_at.pop(session_id, None)
        if explicit == RETRY:
            return RETRY
        return IDLE

    if stream_status == RETRY:
        _last_busy_at.pop(session_id, None)
        return RETRY
    if stream_status == BUSY:
        _last_busy_at[session_id] = now
        return BUSY
    if stream_status == IDLE:
        _last_busy_at.pop(session_id, None)
        return IDLE

    if explicit == RETRY:
        _last_busy_at.pop(session_id, None)
        return RETRY
    if explicit == BUSY:
        _last_busy_at[session_id] = now
        return BUSY

    velocity_busy = previous is not None and updated > previous + 0.25
    if velocity_busy:
        _last_busy_at[session_id] = now
        return BUSY

    last_busy = _last_busy_at.get(session_id)
    if last_busy is not None and now - last_busy < BUSY_HOLD_SECONDS:
        return BUSY

    if explicit == IDLE:
        _last_busy_at.pop(session_id, None)
        return IDLE

    return explicit or IDLE


def _message_is_aborted(info: dict[str, Any]) -> bool:
    error = info.get("error")
    if not isinstance(error, dict):
        return False
    name = str(error.get("name", "")).lower()
    if "abort" in name or "cancel" in name or "stop" in name:
        return True
    message = str(error.get("message", "")).lower()
    return "abort" in message or "cancel" in message or "stopped" in message


def _latest_assistant_message(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for message in reversed(messages):
        info = message.get("info") if isinstance(message.get("info"), dict) else message
        if isinstance(info, dict) and info.get("role") in {"assistant", "agent"}:
            return message
    return None


def messages_indicate_busy(messages: list[dict[str, Any]]) -> bool:
    message = _latest_assistant_message(messages)
    if not message:
        return False
    info = message.get("info") if isinstance(message.get("info"), dict) else message
    if not isinstance(info, dict):
        return False
    if _message_is_aborted(info):
        return False
    time_info = info.get("time") or {}
    if not time_info.get("completed"):
        return True
    for part in message.get("parts") or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") != "tool":
            continue
        state = part.get("state") or {}
        tool_status = str(state.get("status", "")).lower()
        if tool_status in {"running", "pending", "active"}:
            return True
    return False


def mark_session_busy(session_id: str, *, now: float) -> None:
    _last_busy_at[session_id] = now


def clear_session_busy(session_id: str) -> None:
    _last_busy_at.pop(session_id, None)


def build_session_status_map(
    sessions: list[dict[str, Any]],
    raw_status: dict[str, Any] | None,
    *,
    now: float | None = None,
    stream_status_for: Any | None = None,
    message_busy: dict[str, bool] | None = None,
) -> dict[str, str]:
    raw = raw_status or {}
    current_time = now if now is not None else __import__("time").time()
    get_stream = getattr(stream_status_for, "get", None)
    probed = message_busy or {}
    return {
        session["id"]: resolve_session_status(
            session,
            raw.get(session["id"]),
            now=current_time,
            stream_status=get_stream(session["id"]) if get_stream else None,
            message_busy=probed.get(session["id"]),
        )
        for session in sessions
        if session.get("id")
    }
