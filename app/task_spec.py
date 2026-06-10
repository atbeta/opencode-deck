from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml


@dataclass
class TaskSpec:
    name: str
    workspace: str
    goal: str
    steps: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    mode: str = "normal"
    agent: str = "opencode"
    check_interval_seconds: int = 300
    initial_prompt: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "workspace": self.workspace,
            "goal": self.goal,
            "steps": self.steps,
            "acceptance": self.acceptance,
            "mode": self.mode,
            "agent": self.agent,
            "check_interval_seconds": self.check_interval_seconds,
            "initial_prompt": self.initial_prompt,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskSpec:
        steps = [str(s).strip() for s in (data.get("steps") or []) if str(s).strip()]
        acceptance = [
            str(a).strip() for a in (data.get("acceptance") or []) if str(a).strip()
        ]
        interval = parse_interval_seconds(data)
        return cls(
            name=str(data.get("name") or "Untitled task").strip() or "Untitled task",
            workspace=str(data.get("workspace") or "").strip(),
            goal=str(data.get("goal") or "").strip(),
            steps=steps,
            acceptance=acceptance,
            mode=str(data.get("mode") or "normal").strip() or "normal",
            agent=str(data.get("agent") or "opencode").strip() or "opencode",
            check_interval_seconds=interval,
            initial_prompt=(str(data["initial_prompt"]).strip() if data.get("initial_prompt") else None),
        )


def _interval_from_text(value: str, *, default_seconds: int = 300) -> int:
    raw = value.strip().lower()
    digits = int(re.sub(r"[^0-9]", "", raw) or "0")
    if not digits:
        return default_seconds
    if raw.endswith("m"):
        return max(60, digits * 60)
    if raw.endswith("s"):
        return max(10, digits)
    return max(60, digits * 60)


def parse_interval_seconds(data: dict[str, Any]) -> int:
    if data.get("check_interval_minutes") is not None:
        return max(60, int(data["check_interval_minutes"]) * 60)
    if data.get("check_interval_seconds") is not None:
        return max(10, int(data["check_interval_seconds"]))
    if data.get("check_interval") is not None:
        value = data["check_interval"]
        if isinstance(value, str):
            return _interval_from_text(value)
        return max(60, int(value) * 60)
    return 300


def parse_task_spec(text: str, fmt: str = "yaml") -> TaskSpec:
    normalized = (fmt or "yaml").strip().lower()
    if normalized == "markdown":
        return _parse_markdown(text)
    return TaskSpec.from_dict(_parse_yaml(text))


def _parse_yaml(text: str) -> dict[str, Any]:
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("Task spec must be a YAML mapping")
    return data


def _parse_markdown(text: str) -> TaskSpec:
    lines = text.splitlines()
    name = "Untitled task"
    workspace = ""
    mode = "normal"
    agent = "opencode"
    check_interval_seconds = 300
    goal = ""
    acceptance: list[str] = []
    steps: list[str] = []

    meta_re = re.compile(r"^(workspace|mode|agent|check_interval(?:_seconds)?)\s*:\s*(.+)$", re.I)
    for line in lines[:20]:
        if line.startswith("# "):
            name = line[2:].strip() or name
            continue
        match = meta_re.match(line.strip())
        if not match:
            continue
        key, value = match.group(1).lower(), match.group(2).strip()
        if key == "workspace":
            workspace = value.strip("` ")
        elif key == "mode":
            mode = value
        elif key == "agent":
            agent = value
        elif key.startswith("check_interval"):
            check_interval_seconds = _interval_from_text(value)

    section = ""
    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("## goal"):
            section = "goal"
            continue
        if stripped.lower().startswith("## acceptance"):
            section = "acceptance"
            continue
        if stripped.lower().startswith("## steps"):
            section = "steps"
            continue
        if stripped.startswith("## "):
            section = ""
            continue
        if not stripped:
            continue

        if section == "goal":
            goal = f"{goal}\n{stripped}".strip() if goal else stripped
        elif section == "acceptance":
            item = re.sub(r"^[-*]\s*", "", stripped)
            item = re.sub(r"^\[[ xX]\]\s*", "", item).strip()
            if item:
                acceptance.append(item)
        elif section == "steps":
            item = re.sub(r"^\d+\.\s*", "", stripped).strip()
            if item:
                steps.append(item)

    return TaskSpec(
        name=name,
        workspace=workspace,
        goal=goal,
        steps=steps,
        acceptance=acceptance,
        mode=mode,
        agent=agent,
        check_interval_seconds=check_interval_seconds,
    )


def build_bootstrap_prompt(spec: TaskSpec, *, current_step: int = 0) -> str:
    if spec.initial_prompt:
        return spec.initial_prompt

    steps_block = "\n".join(f"{i + 1}. {step}" for i, step in enumerate(spec.steps)) or "1. Complete the goal"
    acceptance_block = "\n".join(f"- [ ] {item}" for item in spec.acceptance) or "- [ ] Goal achieved"
    step_name = spec.steps[current_step] if 0 <= current_step < len(spec.steps) else "Finish remaining work"

    return f"""You are executing an OpenDeck harness task.

Task: {spec.name}
Workspace: {spec.workspace}

Goal:
{spec.goal or spec.name}

Steps:
{steps_block}

Acceptance criteria:
{acceptance_block}

Current focus: step {current_step + 1} — {step_name}

When you complete a step, include a line: STEP DONE: <number>
When all acceptance criteria are satisfied, include a line: TASK COMPLETE
"""


def summarize_messages(messages: list[dict[str, Any]], limit: int = 6) -> str:
    chunks: list[str] = []
    for message in messages[-limit:]:
        role = str(message.get("role") or message.get("type") or "message").lower()
        text = _message_text(message)
        if text:
            chunks.append(f"[{role}] {text[:400]}")
    return "\n".join(chunks)


def _message_text(message: dict[str, Any]) -> str:
    if isinstance(message.get("text"), str):
        return message["text"]
    parts = message.get("parts") or message.get("content") or []
    if not isinstance(parts, list):
        return ""
    out: list[str] = []
    for part in parts:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, dict):
            out.append(str(part.get("text") or part.get("content") or ""))
    return "".join(out)


def detect_progress(text: str, spec: TaskSpec) -> dict[str, Any]:
    upper = text.upper()
    task_complete = "TASK COMPLETE" in upper
    step_done = 0
    for match in re.finditer(r"STEP DONE:\s*(\d+)", text, re.I):
        step_done = max(step_done, int(match.group(1)))

    checked = len(re.findall(r"\[[xX]\]", text))
    total_acceptance = len(spec.acceptance)
    acceptance_progress = (
        min(1.0, checked / total_acceptance) if total_acceptance else (1.0 if task_complete else 0.0)
    )

    return {
        "task_complete": task_complete,
        "step_done": step_done,
        "acceptance_checked": checked,
        "acceptance_total": total_acceptance,
        "acceptance_progress": acceptance_progress,
    }
