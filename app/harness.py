from __future__ import annotations

import time
from typing import Any

from .opencode_client import OpenCodeClient
from .session_status import BUSY, IDLE, RETRY, normalize_session_status
from .store import Store
from .task_spec import TaskSpec, build_bootstrap_prompt, detect_progress, summarize_messages


def session_status_text(value: Any) -> str:
    return normalize_session_status(value, default="")


class HarnessRunner:
    def __init__(self, store: Store, opencode: OpenCodeClient, allowed_workspace) -> None:
        self.store = store
        self.opencode = opencode
        self.allowed_workspace = allowed_workspace

    async def tick(self) -> None:
        due = self.store.list_due_harness_tasks()
        if not due:
            return

        sessions: list[dict[str, Any]] = []
        session_status: dict[str, Any] = {}
        try:
            sessions = await self.opencode.list_sessions()
            session_status = await self.opencode.session_status()
        except Exception:
            return

        archived_ids = {
            s["id"]
            for s in sessions
            if s.get("id")
            and isinstance(s.get("time"), dict)
            and (s["time"].get("archived") or 0) > 0
        }
        known_ids = {s["id"] for s in sessions if s.get("id")}

        for task in due:
            try:
                await self._check_task(
                    task,
                    archived_ids=archived_ids,
                    known_ids=known_ids,
                    session_status=session_status,
                )
            except Exception as exc:  # noqa: BLE001
                self.store.append_harness_check(
                    task["id"],
                    status=task.get("status", "running"),
                    summary="Harness check failed",
                    detail=str(exc),
                )
                self.store.update_harness_task(
                    task["id"],
                    error=str(exc),
                    next_check_at=time.time() + task.get("check_interval_seconds", 30),
                )

    async def start_task(self, task_id: str) -> None:
        task = self.store.get_harness_task(task_id)
        if not task:
            raise ValueError("Task not found")

        spec = TaskSpec.from_dict(task["spec"])
        if not self.allowed_workspace(spec.workspace):
            raise ValueError("Workspace is outside allowed roots")

        session = await self.opencode.create_session(cwd=spec.workspace, title=spec.name)
        session_id = session["id"]
        prompt = build_bootstrap_prompt(spec, current_step=int(task.get("current_step") or 0))
        await self.opencode.send_prompt_async(
            session_id=session_id,
            prompt=prompt,
            agent=None if spec.agent == "opencode" else spec.agent,
        )

        session_ids = list(task.get("session_ids") or [])
        if session_id not in session_ids:
            session_ids.append(session_id)

        now = time.time()
        self.store.update_harness_task(
            task_id,
            status="running",
            active_session_id=session_id,
            session_ids=session_ids,
            last_check_at=now,
            next_check_at=now + spec.check_interval_seconds,
            error=None,
        )
        self.store.append_harness_check(
            task_id,
            status="running",
            summary="Harness started and bootstrap prompt sent",
            detail=f"session={session_id}",
        )

    async def _check_task(
        self,
        task: dict[str, Any],
        *,
        archived_ids: set[str],
        known_ids: set[str],
        session_status: dict[str, Any],
    ) -> None:
        task_id = task["id"]
        spec = TaskSpec.from_dict(task["spec"])
        now = time.time()
        interval = int(task.get("check_interval_seconds") or spec.check_interval_seconds or 30)

        if task.get("status") == "pending":
            await self.start_task(task_id)
            return

        if task.get("status") in {"paused", "completed", "failed", "archived"}:
            return

        session_id = task.get("active_session_id")
        if not session_id:
            await self.start_task(task_id)
            return

        if session_id in archived_ids:
            self._finish_check(
                task_id,
                status="archived",
                summary="Linked session was archived in OpenCode",
                detail="",
                next_check_at=None,
            )
            return

        if session_id not in known_ids:
            self._finish_check(
                task_id,
                status="failed",
                summary="Linked session no longer exists",
                detail=session_id,
                next_check_at=None,
            )
            return

        live = session_status_text(session_status.get(session_id))
        messages = await self.opencode.messages(session_id, limit=12)
        transcript = summarize_messages(messages)
        progress = detect_progress(transcript, spec)

        current_step = int(task.get("current_step") or 0)
        if progress["step_done"] > 0:
            current_step = max(current_step, progress["step_done"] - 1)

        status = "running"
        summary = "Agent is busy"
        if live == BUSY:
            status = "running"
            summary = "Agent is busy"
        elif live == RETRY or "wait" in live or "permission" in live:
            status = "waiting"
            summary = "Waiting for permission or input"
        elif "fail" in live or "error" in live:
            status = "failed"
            summary = "Session reported failure"
        elif progress["task_complete"] or (
            spec.acceptance and progress["acceptance_checked"] >= len(spec.acceptance)
        ):
            status = "completed"
            summary = "Acceptance criteria appear satisfied"
        elif live == IDLE or "idle" in live or "complete" in live:
            status = "running"
            summary = "Session idle — monitoring progress"
            if current_step < len(spec.steps) - 1 and task.get("idle_checks", 0) >= 1:
                current_step += 1
                nudge = (
                    f"Continue harness task '{spec.name}'. "
                    f"Focus on step {current_step + 1}: {spec.steps[current_step]}. "
                    "Reply with STEP DONE or TASK COMPLETE when appropriate."
                )
                await self.opencode.send_prompt_async(session_id=session_id, prompt=nudge)
                summary = f"Nudged agent toward step {current_step + 1}"

        completed_steps = list(task.get("completed_steps") or [])
        if progress["step_done"] > 0 and progress["step_done"] not in completed_steps:
            completed_steps.append(progress["step_done"])

        idle_checks = int(task.get("idle_checks") or 0)
        if "idle" in live or "complete" in live:
            idle_checks += 1
        else:
            idle_checks = 0

        next_check = None if status in {"completed", "failed", "archived"} else now + interval
        self.store.update_harness_task(
            task_id,
            status=status,
            current_step=current_step,
            completed_steps=completed_steps,
            idle_checks=idle_checks,
            progress=progress["acceptance_progress"],
            last_check_at=now,
            next_check_at=next_check,
            last_summary=summary,
        )
        self.store.append_harness_check(
            task_id,
            status=status,
            summary=summary,
            detail=transcript[-1200:] if transcript else live or "no activity",
        )

    def _finish_check(
        self,
        task_id: str,
        *,
        status: str,
        summary: str,
        detail: str,
        next_check_at: float | None,
    ) -> None:
        now = time.time()
        self.store.update_harness_task(
            task_id,
            status=status,
            last_check_at=now,
            next_check_at=next_check_at,
            last_summary=summary,
        )
        self.store.append_harness_check(
            task_id,
            status=status,
            summary=summary,
            detail=detail,
        )
