from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            self._drop_legacy_tasks_table(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS harness_tasks (
                  id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  spec_format TEXT NOT NULL,
                  spec_text TEXT NOT NULL,
                  spec_json TEXT NOT NULL,
                  workspace TEXT NOT NULL,
                  mode TEXT NOT NULL,
                  agent TEXT NOT NULL,
                  status TEXT NOT NULL,
                  current_step INTEGER NOT NULL DEFAULT 0,
                  session_ids_json TEXT NOT NULL DEFAULT '[]',
                  active_session_id TEXT,
                  check_interval_seconds INTEGER NOT NULL DEFAULT 30,
                  completed_steps_json TEXT NOT NULL DEFAULT '[]',
                  idle_checks INTEGER NOT NULL DEFAULT 0,
                  progress REAL NOT NULL DEFAULT 0,
                  check_log_json TEXT NOT NULL DEFAULT '[]',
                  last_summary TEXT,
                  error TEXT,
                  last_check_at REAL,
                  next_check_at REAL,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recent_workspaces (
                  path TEXT PRIMARY KEY,
                  used_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dismissed_workspaces (
                  path TEXT PRIMARY KEY,
                  dismissed_at REAL NOT NULL
                )
                """
            )
    @staticmethod
    def _drop_legacy_tasks_table(conn: sqlite3.Connection) -> None:
        """Remove pre-harness dispatch log table (replaced by harness_tasks)."""
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()
        if row:
            conn.execute("DROP TABLE tasks")

    def create_harness_task(self, task: dict[str, Any]) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO harness_tasks (
                  id, name, spec_format, spec_text, spec_json, workspace, mode, agent,
                  status, current_step, session_ids_json, active_session_id,
                  check_interval_seconds, completed_steps_json, idle_checks, progress,
                  check_log_json, last_summary, error, last_check_at, next_check_at,
                  created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task["id"],
                    task["name"],
                    task["spec_format"],
                    task["spec_text"],
                    json.dumps(task["spec"], ensure_ascii=False),
                    task["workspace"],
                    task["mode"],
                    task["agent"],
                    task.get("status", "pending"),
                    int(task.get("current_step") or 0),
                    json.dumps(task.get("session_ids") or [], ensure_ascii=False),
                    task.get("active_session_id"),
                    int(task.get("check_interval_seconds") or 30),
                    json.dumps(task.get("completed_steps") or [], ensure_ascii=False),
                    int(task.get("idle_checks") or 0),
                    float(task.get("progress") or 0),
                    json.dumps(task.get("check_log") or [], ensure_ascii=False),
                    task.get("last_summary"),
                    task.get("error"),
                    task.get("last_check_at"),
                    task.get("next_check_at", now),
                    now,
                    now,
                ),
            )

    def update_harness_task(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        json_fields = {
            "spec": "spec_json",
            "session_ids": "session_ids_json",
            "completed_steps": "completed_steps_json",
            "check_log": "check_log_json",
        }
        normalized: dict[str, Any] = {}
        for key, value in fields.items():
            if key in json_fields:
                normalized[json_fields[key]] = json.dumps(value, ensure_ascii=False)
            else:
                normalized[key] = value
        normalized["updated_at"] = time.time()
        assignments = ", ".join(f"{key} = ?" for key in normalized)
        values = list(normalized.values())
        values.append(task_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE harness_tasks SET {assignments} WHERE id = ?", values)

    def append_harness_check(
        self,
        task_id: str,
        *,
        status: str,
        summary: str,
        detail: str = "",
    ) -> None:
        task = self.get_harness_task(task_id)
        if not task:
            return
        log = list(task.get("check_log") or [])
        log.append(
            {
                "at": time.time(),
                "status": status,
                "summary": summary,
                "detail": detail[:2000],
            }
        )
        log = log[-100:]
        self.update_harness_task(task_id, check_log=log, last_summary=summary)

    def get_harness_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM harness_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        return self._row_to_harness_task(row) if row else None

    def list_harness_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM harness_tasks ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_harness_task(row) for row in rows]

    def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        """Backward-compatible alias for harness tasks."""
        return self.list_harness_tasks(limit)

    def list_due_harness_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        now = time.time()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM harness_tasks
                WHERE status IN ('pending', 'running', 'waiting')
                  AND (next_check_at IS NULL OR next_check_at <= ?)
                ORDER BY COALESCE(next_check_at, 0) ASC
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
        return [self._row_to_harness_task(row) for row in rows]

    def _row_to_harness_task(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["spec"] = json.loads(item.pop("spec_json") or "{}")
        item["session_ids"] = json.loads(item.pop("session_ids_json") or "[]")
        item["completed_steps"] = json.loads(item.pop("completed_steps_json") or "[]")
        item["check_log"] = json.loads(item.pop("check_log_json") or "[]")
        return item

    @staticmethod
    def _normalize_workspace_path(path: str) -> str:
        return str(Path(path).expanduser().resolve())

    def record_recent_workspace(self, path: str) -> None:
        normalized = self._normalize_workspace_path(path)
        now = time.time()
        with self._connect() as conn:
            conn.execute("DELETE FROM dismissed_workspaces WHERE path = ?", (normalized,))
            conn.execute(
                "INSERT OR REPLACE INTO recent_workspaces (path, used_at) VALUES (?, ?)",
                (normalized, now),
            )
            stale = conn.execute(
                """
                SELECT path FROM recent_workspaces
                ORDER BY used_at DESC
                LIMIT -1 OFFSET 20
                """
            ).fetchall()
            for row in stale:
                conn.execute("DELETE FROM recent_workspaces WHERE path = ?", (row["path"],))

    def list_recent_workspaces(self, limit: int = 12) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT path FROM recent_workspaces ORDER BY used_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [row["path"] for row in rows]

    def dismiss_workspace(self, path: str) -> str:
        normalized = self._normalize_workspace_path(path)
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO dismissed_workspaces (path, dismissed_at) VALUES (?, ?)",
                (normalized, now),
            )
        return normalized

    def is_workspace_dismissed(self, path: str) -> bool:
        normalized = self._normalize_workspace_path(path)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM dismissed_workspaces WHERE path = ?",
                (normalized,),
            ).fetchone()
        return row is not None

    def remove_recent_workspace(self, path: str) -> bool:
        normalized = self.dismiss_workspace(path)
        candidates = {path, normalized}
        with self._connect() as conn:
            removed = False
            for candidate in candidates:
                cursor = conn.execute(
                    "DELETE FROM recent_workspaces WHERE path = ?",
                    (candidate,),
                )
                if cursor.rowcount > 0:
                    removed = True
        return removed

    def seed_recent_workspaces_from_sessions(
        self,
        directories: list[str],
        *,
        limit: int = 12,
    ) -> None:
        now = time.time()
        seen: set[str] = set()
        with self._connect() as conn:
            for directory in directories:
                if not directory:
                    continue
                normalized = self._normalize_workspace_path(directory)
                if normalized in seen:
                    continue
                dismissed = conn.execute(
                    "SELECT 1 FROM dismissed_workspaces WHERE path = ?",
                    (normalized,),
                ).fetchone()
                if dismissed:
                    continue
                seen.add(normalized)
                conn.execute(
                    "INSERT OR IGNORE INTO recent_workspaces (path, used_at) VALUES (?, ?)",
                    (normalized, now),
                )
                if len(seen) >= limit:
                    break

