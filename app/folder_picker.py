from __future__ import annotations

import platform
import shlex
import subprocess
from pathlib import Path


def pick_folder_native(initial: str | None = None) -> str | None:
    system = platform.system()
    if system == "Darwin":
        return _pick_macos(initial)
    if system == "Linux":
        return _pick_linux(initial)
    return None


def _pick_macos(initial: str | None) -> str | None:
    if initial:
        start = Path(initial).expanduser().resolve()
        if not start.is_dir():
            start = start.parent if start.parent.is_dir() else Path.home()
        quoted = shlex.quote(str(start))
        script = (
            "POSIX path of (choose folder with prompt "
            f'"Select workspace folder" default location (POSIX file {quoted}))'
        )
    else:
        script = 'POSIX path of (choose folder with prompt "Select workspace folder")'
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    path = result.stdout.strip()
    if not path:
        return None
    return str(Path(path).expanduser().resolve())


def _pick_linux(initial: str | None) -> str | None:
    command = ["zenity", "--file-selection", "--directory", "--title=Select workspace folder"]
    if initial:
        start = Path(initial).expanduser().resolve()
        if start.is_dir():
            command.extend(["--filename", str(start)])
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=600, check=False)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    path = result.stdout.strip()
    if not path:
        return None
    return str(Path(path).expanduser().resolve())
