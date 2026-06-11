from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Unattended-execution policy. This block is prepended to every prompt the
# Harness sends to an Agent, so even if the spec author forgets to add
# constraints the agent still knows it is running headless.
# ---------------------------------------------------------------------------
UNATTENDED_POLICY = """[OPENDEPLOY EXECUTION POLICY — NON-NEGOTIABLE]
You are running an OpenDeck harness task WITHOUT a human at the keyboard.
- If you encounter ambiguity, PICK A REASONABLE DEFAULT and document the
  choice in your STEP DONE summary. Do NOT ask the user to choose between
  options.
- Tool permissions for the workspace owner have been pre-approved. Do not
  request approval for read / edit / bash inside this workspace — proceed
  directly. If you genuinely need input that cannot be inferred (e.g. a
  destructive production operation), emit `BLOCKED: <reason>` and stop.
- After every meaningful step, emit exactly one line: `STEP DONE: <n>`.
- When all acceptance criteria are satisfied, emit: `TASK COMPLETE`.
- If a step requires no changes, still emit `STEP DONE: <n>` to confirm.
- Do NOT propose plans and ask for approval. Implement, then report.
"""


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
        return f"{UNATTENDED_POLICY}\n\n{spec.initial_prompt}"

    steps_block = (
        "\n".join(f"{i + 1}. {step}" for i, step in enumerate(spec.steps))
        or "1. Complete the goal"
    )
    acceptance_block = (
        "\n".join(f"- [ ] {item}" for item in spec.acceptance) or "- [ ] Goal achieved"
    )
    step_name = (
        spec.steps[current_step]
        if 0 <= current_step < len(spec.steps)
        else "Finish remaining work"
    )

    return f"""{UNATTENDED_POLICY}

You are executing an OpenDeck harness task.

Task: {spec.name}
Workspace: {spec.workspace}

Goal:
{spec.goal or spec.name}

Steps:
{steps_block}

Acceptance criteria:
{acceptance_block}

Current focus: step {current_step + 1} — {step_name}

Proceed through steps autonomously. Do not ask for confirmation between steps.
When you complete a step, include a line: STEP DONE: <number>
When all acceptance criteria are satisfied, include a line: TASK COMPLETE
"""


def message_role(message: dict[str, Any]) -> str:
    info = message.get("info") if isinstance(message.get("info"), dict) else message
    if isinstance(info, dict) and info.get("role"):
        return str(info["role"]).lower()
    return str(message.get("role") or message.get("type") or "message").lower()


def _message_text(message: dict[str, Any]) -> str:
    """Concatenate the human-readable text from a message.

    Tools and reasoning parts are intentionally skipped — they bloat the
    transcript and confuse the progress detection heuristics. The full
    structure is still available via the API; the spec extractor only needs
    the parts an Agent would actually print to a human user.
    """
    if isinstance(message.get("text"), str):
        return message["text"]
    parts = message.get("parts") or message.get("content") or []
    if not isinstance(parts, list):
        return ""
    out: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            if isinstance(part, str):
                out.append(part)
            continue
        part_type = str(part.get("type") or "").lower()
        # Skip noisy / non-readable parts.
        if part_type in {"tool", "tool_use", "tool_result", "reasoning", "step-start", "step-finish"}:
            continue
        text = part.get("text") or part.get("content")
        if isinstance(text, str) and text:
            out.append(text)
    return "".join(out)


def _summarize_part(part: Any) -> str | None:
    """Compress a single message part into a short, human-readable label.

    Used by the dashboard message list. The goal is "one line per meaningful
    event" — not a faithful JSON dump.
    """
    if isinstance(part, str):
        return part.strip() or None
    if not isinstance(part, dict):
        return None
    part_type = str(part.get("type") or "").lower()
    if part_type in {"text", ""}:
        text = part.get("text") or part.get("content")
        if isinstance(text, str) and text.strip():
            return text.strip()
        return None
    if part_type in {"reasoning"}:
        text = part.get("text") or part.get("content") or ""
        if isinstance(text, str) and text.strip():
            return f"💭 {text.strip()[:160]}"
        return None
    if part_type in {"tool", "tool_use", "tool-invocation"}:
        name = part.get("name") or part.get("tool") or "tool"
        args = part.get("input") or part.get("args") or part.get("arguments")
        if isinstance(args, dict):
            # Pick the first non-empty string/short value as a hint.
            hint = ""
            for key in ("command", "filePath", "path", "file", "url", "query", "prompt"):
                v = args.get(key)
                if isinstance(v, str) and v.strip():
                    hint = v.strip()
                    break
            if hint:
                return f"🔧 {name} — {hint[:80]}"
        return f"🔧 {name}"
    if part_type in {"tool_result", "tool-result"}:
        name = part.get("name") or part.get("tool") or "tool"
        output = part.get("output") or part.get("content")
        if isinstance(output, str):
            return f"↪ {name}: {output.strip().splitlines()[0][:120]}"
        return f"↪ {name}"
    if part_type in {"step-start", "step-finish"}:
        return None
    return None


def summarize_message(message: dict[str, Any], *, max_parts: int = 4) -> str:
    """Render a message as a list of short human-readable lines.

    Each part becomes one line; if there are too many parts we keep the first
    N and summarize the rest as "(+M more parts)". Empty parts are dropped.
    """
    parts = message.get("parts") or message.get("content") or []
    if not isinstance(parts, list):
        if isinstance(message.get("text"), str):
            return message["text"]
        return ""
    lines: list[str] = []
    for part in parts:
        rendered = _summarize_part(part)
        if rendered:
            lines.append(rendered)
    if len(lines) > max_parts:
        kept = lines[:max_parts]
        kept.append(f"(+{len(lines) - max_parts} more parts)")
        return "\n".join(kept)
    return "\n".join(lines)


def full_transcript(messages: list[dict[str, Any]], limit: int = 12) -> str:
    chunks: list[str] = []
    for message in messages[-limit:]:
        text = _message_text(message)
        if text:
            chunks.append(text)
    return "\n\n".join(chunks)


def summarize_messages(messages: list[dict[str, Any]], limit: int = 6) -> str:
    chunks: list[str] = []
    for message in messages[-limit:]:
        role = message_role(message)
        text = _message_text(message)
        if text:
            chunks.append(f"[{role}] {text[:400]}")
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# Asking-detection. When the Agent responds with "should I do X or Y?" we
# classify the question so the Harness can auto-decide instead of stalling.
# ---------------------------------------------------------------------------
_ASKING_PATTERNS = (
    "should i proceed",
    "should i continue",
    "want me to proceed",
    "want me to continue",
    "shall i proceed",
    "shall i continue",
    "ready for step",
    "may i proceed",
    "do you want me to",
    "would you like",
    "which approach",
    "which option",
    "which one should",
    "let me know if",
    "please confirm",
    "please choose",
    "please decide",
)


def looks_like_asking(text: str) -> bool:
    """True if the assistant appears to be asking the user a question."""
    if not text:
        return False
    lower = text.lower()
    if "?" not in lower:
        return False
    return any(pattern in lower for pattern in _ASKING_PATTERNS)


_AUTO_DECISION_PROMPT = (
    "The previous turn asked a question, but OpenDeck runs unattended. "
    "Pick the safest interpretation (smallest blast radius, follows existing "
    "conventions, least invasive change) and proceed. "
    "Do not ask again. Emit STEP DONE: <n> or TASK COMPLETE when done."
)


def auto_decision_count(messages: list[dict[str, Any]]) -> int:
    """How many auto-decision prompts we have already sent in this session.

    Used to avoid an infinite ping-pong if the Agent keeps asking.
    """
    count = 0
    for message in messages:
        if message_role(message) != "user":
            continue
        text = _message_text(message)
        if text and _AUTO_DECISION_PROMPT[:40] in text:
            count += 1
    return count


# Kept for backwards compatibility — older code referenced these names.
_CONTINUE_PATTERNS = _ASKING_PATTERNS


def agent_awaiting_continue(text: str) -> bool:
    return looks_like_asking(text)


def needs_continue_reply(messages: list[dict[str, Any]]) -> bool:
    awaiting_index: int | None = None
    for index in range(len(messages) - 1, -1, -1):
        if message_role(messages[index]) != "assistant":
            continue
        if agent_awaiting_continue(_message_text(messages[index])):
            awaiting_index = index
            break
    if awaiting_index is None:
        return False
    for index in range(awaiting_index + 1, len(messages)):
        if message_role(messages[index]) == "user":
            return False
    return True


def assistant_transcript(messages: list[dict[str, Any]], limit: int | None = None) -> str:
    assistants = [message for message in messages if message_role(message) == "assistant"]
    if limit is None:
        return full_transcript(assistants)
    return full_transcript(assistants[-limit:])


def detect_progress_from_messages(messages: list[dict[str, Any]], spec: TaskSpec) -> dict[str, Any]:
    return detect_progress(assistant_transcript(messages, limit=None), spec)


def build_periodic_check_prompt(
    spec: TaskSpec,
    *,
    current_step: int,
    progress: dict[str, Any],
    completed_steps: list[int],
) -> str:
    step_lines: list[str] = []
    for index, step in enumerate(spec.steps):
        step_number = index + 1
        markers = completed_steps + ([progress["step_done"]] if progress["step_done"] else [])
        done = step_number in markers or progress["step_done"] >= step_number
        step_lines.append(f"{step_number}. {step}{' (done)' if done else ''}")
    steps_block = "\n".join(step_lines) or "1. Complete the goal"
    acceptance_block = "\n".join(f"- [ ] {item}" for item in spec.acceptance) or "- [ ] Goal achieved"
    focus_index = min(current_step, len(spec.steps) - 1) if spec.steps else 0
    focus_step = spec.steps[focus_index] if spec.steps else "Finish remaining work"

    return f"""{UNATTENDED_POLICY}

OpenDeck harness periodic check for "{spec.name}".

The session is idle. Confirm task status before we schedule the next check.

Steps:
{steps_block}

Acceptance:
{acceptance_block}

Reply with ONE of:
- STEP DONE: <number> — if a step is finished (required after each step)
- TASK COMPLETE — if all acceptance criteria are satisfied
- Or continue working on step {focus_index + 1}: {focus_step}

Proceed autonomously. Do not ask for confirmation."""


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
