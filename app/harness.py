from __future__ import annotations

import time
from typing import Any

from .opencode_client import OpenCodeClient
from .session_status import BUSY, IDLE, RETRY, messages_indicate_busy, normalize_session_status
from .store import Store
from .task_spec import (
    TaskSpec,
    build_bootstrap_prompt,
    build_periodic_check_prompt,
    detect_progress_from_messages,
    needs_continue_reply,
    summarize_messages,
)


def session_status_text(value: Any) -> str:
    return normalize_session_status(value, default="")


def session_is_idle(live: str, messages: list[dict[str, Any]]) -> bool:
    if live in {BUSY, RETRY} or "wait" in live or "permission" in live:
        return False
    if messages_indicate_busy(messages):
        return False
    return True


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
        messages = await self.opencode.messages(session_id, limit=50)
        transcript = summarize_messages(messages)
        progress = detect_progress_from_messages(messages, spec)
        is_idle = session_is_idle(live, messages)
        completed_steps = list(task.get("completed_steps") or [])
        agent_name = None if spec.agent == "opencode" else spec.agent

        current_step = int(task.get("current_step") or 0)
        if progress["step_done"] > 0:
            current_step = max(
                current_step,
                min(progress["step_done"], len(spec.steps) - 1),
            )

        all_steps_reported = (
            len(spec.steps) > 0 and progress["step_done"] >= len(spec.steps)
        )
        task_finished = progress["task_complete"] or (
            spec.acceptance and progress["acceptance_checked"] >= len(spec.acceptance)
        ) or all_steps_reported

        status = "running"
        summary = "Agent is busy"
        if live == BUSY or messages_indicate_busy(messages):
            status = "running"
            summary = "Agent is busy"
        elif live == RETRY or "wait" in live or "permission" in live:
            status = "waiting"
            summary = "Waiting for permission or input"
        elif "fail" in live or "error" in live:
            status = "failed"
            summary = "Session reported failure"
        elif task_finished:
            status = "completed"
            summary = (
                "Agent reported TASK COMPLETE"
                if progress["task_complete"]
                else "All harness steps appear complete"
            )
        elif is_idle:
            status = "running"
            summary = "Session idle — verifying progress"
            nudge: str | None = None
            if needs_continue_reply(messages):
                step_name = spec.steps[min(current_step, len(spec.steps) - 1)]
                nudge = (
                    f"Yes — proceed autonomously with step {current_step + 1}: {step_name}. "
                    "Do not ask for confirmation between harness steps; implement directly. "
                    "Reply with STEP DONE: "
                    f"{current_step + 1} when this step is finished."
                )
                summary = f"Auto-continued to step {current_step + 1}"
            elif (
                progress["step_done"] > 0
                and progress["step_done"] not in completed_steps
                and current_step < len(spec.steps) - 1
            ):
                next_index = min(progress["step_done"], len(spec.steps) - 1)
                nudge = (
                    f"Continue harness task '{spec.name}'. "
                    f"Focus on step {next_index + 1}: {spec.steps[next_index]}. "
                    "Proceed without asking for permission. "
                    "Reply with STEP DONE: <number> or TASK COMPLETE when appropriate."
                )
                current_step = next_index
                summary = f"Nudged agent toward step {next_index + 1}"
            else:
                nudge = build_periodic_check_prompt(
                    spec,
                    current_step=current_step,
                    progress=progress,
                    completed_steps=completed_steps,
                )
                summary = "Periodic check — requested status confirmation"
            if nudge:
                await self.opencode.send_prompt_async(
                    session_id=session_id,
                    prompt=nudge,
                    agent=agent_name,
                )

        if progress["step_done"] > 0 and progress["step_done"] not in completed_steps:
            completed_steps.append(progress["step_done"])

        idle_checks = int(task.get("idle_checks") or 0)
        if is_idle:
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
