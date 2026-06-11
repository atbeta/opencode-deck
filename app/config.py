from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_paths(value: str) -> list[Path]:
    """Parse OPENDECK_ALLOWED_ROOTS using the platform separator.

    On POSIX (macOS/Linux) the documented separator is ``:``. On Windows we
    additionally accept ``;`` to mirror the native ``PATH`` style. Empty
    segments are ignored.
    """
    if not value:
        return []
    # Try the native separator first, then fall back to the other one.
    candidates: list[str]
    if ";" in value and ":" not in value:
        candidates = value.split(";")
    else:
        candidates = value.split(":")
    paths: list[Path] = []
    for item in candidates:
        item = item.strip().strip('"').strip("'")
        if not item:
            continue
        paths.append(Path(item).expanduser().resolve())
    return paths


def _env_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Settings(BaseSettings):
    """Runtime settings sourced from environment variables and ``.env``.

    The values are read automatically by ``pydantic-settings`` so users do not
    need to ``set -a && source .env && set +a`` before launching OpenDeck.
    On Windows, ``.env`` is still parsed and a ``set`` script is generated
    only when users opt in via ``opendeck --print-env`` for debugging.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    opencode_server_url: str = Field(default="http://127.0.0.1:14096")
    opencode_server_username: str = Field(default="opencode")
    opencode_server_password: str = Field(default="")

    opendeck_host: str = Field(default="127.0.0.1")
    opendeck_port: int = Field(default=55413)
    opendeck_database: str = Field(default=".opendeck/opendeck.sqlite3")
    opendeck_allowed_roots: str = Field(default="")
    opendeck_strict_roots: bool = Field(default=False)
    opendeck_basic_auth_user: str = Field(default="")
    opendeck_basic_auth_pass: str = Field(default="")

    # Auto-decision policy for Harness — see app/session_status.py.
    opendeck_auto_decide: bool = Field(default=True)
    opendeck_auto_decide_max: int = Field(default=3)

    @property
    def opencode_url(self) -> str:
        return self.opencode_server_url.rstrip("/")

    @property
    def opencode_username(self) -> str:
        return self.opencode_server_username

    @property
    def opencode_password(self) -> str:
        return self.opencode_server_password

    @property
    def database_path(self) -> Path:
        path = Path(self.opendeck_database).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        return path

    @property
    def allowed_roots(self) -> list[Path]:
        return _split_paths(self.opendeck_allowed_roots)

    @property
    def strict_roots(self) -> bool:
        return bool(self.opendeck_strict_roots)

    @property
    def basic_auth_enabled(self) -> bool:
        return bool(self.opendeck_basic_auth_user) and bool(self.opendeck_basic_auth_pass)

    def is_allowed_workspace(self, cwd: str) -> bool:
        path = Path(cwd).expanduser().resolve()
        if not path.exists() or not path.is_dir():
            return False
        if not self.strict_roots:
            return True
        if not self.allowed_roots:
            return False
        return any(path == root or root in path.parents for root in self.allowed_roots)


# Backwards-compatibility shim: existing callers (and tests) refer to
# ``Settings.from_env()``. New code should construct ``Settings()`` directly.
def _from_env() -> Settings:  # pragma: no cover - thin wrapper
    return Settings()


Settings.from_env = staticmethod(_from_env)  # type: ignore[attr-defined]
