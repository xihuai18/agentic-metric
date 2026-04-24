"""Cross-platform process detection helpers."""

from __future__ import annotations

import csv
import ntpath
import os
import platform
import subprocess
from pathlib import Path

try:  # pragma: no cover - optional dependency path is platform/env specific
    import psutil
except ImportError:  # pragma: no cover
    psutil = None


def normalize_cwd_key(path: str) -> str:
    """Normalize a cwd string for cross-platform matching."""
    if not path:
        return ""
    expanded = os.path.expandvars(os.path.expanduser(path))
    if platform.system() == "Windows":
        return ntpath.normcase(ntpath.normpath(expanded.replace("/", "\\")))
    return os.path.normpath(expanded)


def _name_matches(name: str, process_name: str, exact: bool) -> bool:
    name = name or ""
    target = (process_name or "").lower()
    lowered = name.lower()
    stem = Path(lowered).stem
    if exact:
        return lowered == target or stem == target or lowered == f"{target}.exe"
    return target in lowered


def _find_pids_psutil(process_name: str, exact: bool) -> list[int]:
    if psutil is None:
        return []
    pids: list[int] = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if _name_matches(proc.info.get("name") or "", process_name, exact):
                pids.append(int(proc.info["pid"]))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, TypeError, ValueError):
            continue
    return pids


def _find_pids_windows_tasklist(process_name: str, exact: bool) -> list[int]:
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []

    pids: list[int] = []
    for row in csv.reader(result.stdout.splitlines()):
        if len(row) < 2:
            continue
        if not _name_matches(row[0], process_name, exact):
            continue
        try:
            pids.append(int(row[1]))
        except ValueError:
            continue
    return pids


def find_pids(process_name: str, exact: bool = True) -> list[int]:
    """Find PIDs matching a process name.

    Args:
        process_name: Name to search for.
        exact: If True, match exact name (pgrep -x). If False, match pattern (pgrep -f).
    """
    psutil_pids = _find_pids_psutil(process_name, exact)
    if psutil_pids:
        return psutil_pids

    if platform.system() == "Windows":
        return _find_pids_windows_tasklist(process_name, exact)

    try:
        flag = "-x" if exact else "-f"
        result = subprocess.run(
            ["pgrep", flag, process_name],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode != 0:
            return []
        return [int(pid.strip()) for pid in result.stdout.strip().split("\n") if pid.strip()]
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        return []


def get_pid_cwd(pid: int) -> str:
    """Get the working directory of a process by PID. Cross-platform."""
    if psutil is not None:
        try:
            return psutil.Process(pid).cwd()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            pass

    system = platform.system()

    if system == "Linux":
        try:
            return str(Path(f"/proc/{pid}/cwd").resolve())
        except (OSError, PermissionError):
            return ""

    elif system == "Darwin":
        try:
            result = subprocess.run(
                ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            for line in result.stdout.split("\n"):
                if line.startswith("n") and line[1:].startswith("/"):
                    return line[1:]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return ""

    return ""


def get_running_cwds(process_name: str, exact: bool = True) -> dict[int, str]:
    """Return {pid: cwd} for all matching processes."""
    result: dict[int, str] = {}
    for pid in find_pids(process_name, exact=exact):
        cwd = get_pid_cwd(pid)
        if cwd:
            result[pid] = cwd
    return result
