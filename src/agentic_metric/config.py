"""Configuration constants and paths."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import platform
from pathlib import Path

_HOME = Path.home()
_SYSTEM = platform.system()
_IS_MAC = _SYSTEM == "Darwin"
_IS_WINDOWS = _SYSTEM == "Windows"

# Platform-specific base directories
_APP_SUPPORT = _HOME / "Library" / "Application Support" if _IS_MAC else None
_WINDOWS_APPDATA = Path(
    os.environ.get("LOCALAPPDATA")
    or os.environ.get("APPDATA")
    or str(_HOME / "AppData" / "Local")
) if _IS_WINDOWS else None


def _env_path(var: str, default: Path) -> Path:
    """Read a path from env var, expanding ~ and $VARS. Fall back to *default*."""
    raw = os.environ.get(var)
    if not raw:
        return default
    return Path(os.path.expandvars(raw)).expanduser()


# Claude Code data paths. Honors CLAUDE_CONFIG_DIR — the variable the
# official Claude Code CLI uses to relocate its config directory.
CLAUDE_HOME = _env_path("CLAUDE_CONFIG_DIR", _HOME / ".claude")
STATS_CACHE = CLAUDE_HOME / "stats-cache.json"
PROJECTS_DIR = CLAUDE_HOME / "projects"

# Codex CLI data paths. Honors CODEX_HOME — the variable the official
# OpenAI Codex CLI uses.
CODEX_HOME = _env_path("CODEX_HOME", _HOME / ".codex")
CODEX_SESSIONS_DIR = CODEX_HOME / "sessions"

# Application data (this tool's own DB + pricing overrides).
DATA_DIR = (
    (_APP_SUPPORT / "agentic_metric") if _IS_MAC
    else (_WINDOWS_APPDATA / "agentic_metric") if _IS_WINDOWS
    else (_HOME / ".local" / "share" / "agentic_metric")
)
DB_PATH = DATA_DIR / "data.db"
PRICING_FILE = DATA_DIR / "pricing.json"
CONFIG_FILE = _env_path("AGENTIC_METRIC_CONFIG", DATA_DIR / "config.json")


@dataclass(frozen=True)
class CollectorRoot:
    """One configured data root for an agent collector."""

    path: Path
    provider: str = ""
    raw_path: str = ""


@dataclass(frozen=True)
class RemoteCollectorRoot:
    """One configured remote data root for an agent collector."""

    path: str
    provider: str = ""


@dataclass(frozen=True)
class RemoteSpec:
    """One SSH remote whose agent data should be included in history sync."""

    host: str
    name: str = ""
    user: str = ""
    port: int | None = None
    timeout: int = 30
    ssh_options: tuple[str, ...] = ()
    collectors: dict[str, list[RemoteCollectorRoot]] | None = None


def _expand_path(raw: object) -> Path | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    return Path(os.path.expandvars(raw.strip())).expanduser()


def _load_config() -> dict:
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _raw_collector_roots(config: dict, name: str) -> list[tuple[str, str]]:
    collectors = config.get("collectors")
    raw_collector = collectors.get(name) if isinstance(collectors, dict) else None
    raw_roots = None
    if isinstance(raw_collector, dict):
        raw_roots = raw_collector.get("roots")
    elif isinstance(raw_collector, list):
        raw_roots = raw_collector

    roots: list[tuple[str, str]] = []
    if isinstance(raw_roots, list):
        for item in raw_roots:
            if isinstance(item, str):
                raw_path = item.strip()
                provider = ""
            elif isinstance(item, dict):
                raw = item.get("path")
                raw_path = raw.strip() if isinstance(raw, str) else ""
                provider = str(item.get("provider") or "").strip()
            else:
                continue
            if raw_path:
                roots.append((raw_path, provider))
    return roots


def _collector_roots(name: str, default_path: Path, default_raw_path: str) -> list[CollectorRoot]:
    """Return configured roots for a collector, falling back to its env/default root.

    Supported config shape in ``CONFIG_FILE``:

    {
      "collectors": {
        "codex": {
          "roots": [
            {"path": "~/.codex", "provider": "openai"},
            {"path": "~/.codex-custom", "provider": "custom"}
          ]
        },
        "claude_code": {
          "roots": [
            {"path": "~/.claude"},
            {"path": "~/.claude-alt"}
          ]
        }
      }
    }
    """
    config = _load_config()
    roots: list[CollectorRoot] = []
    for raw_path, provider in _raw_collector_roots(config, name):
        path = _expand_path(raw_path)
        if path is not None:
            roots.append(CollectorRoot(path=path, provider=provider, raw_path=raw_path))

    return roots or [CollectorRoot(default_path, raw_path=default_raw_path)]


def get_codex_roots() -> list[CollectorRoot]:
    return _collector_roots("codex", CODEX_HOME, os.environ.get("CODEX_HOME") or "~/.codex")


def get_claude_code_roots() -> list[CollectorRoot]:
    return _collector_roots(
        "claude_code",
        CLAUDE_HOME,
        os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude",
    )


def _remote_collector_roots(
    remote: dict,
    name: str,
    local_fallback: list[CollectorRoot],
) -> list[RemoteCollectorRoot]:
    collectors = remote.get("collectors")
    raw_collector = collectors.get(name) if isinstance(collectors, dict) else None

    if isinstance(raw_collector, dict):
        raw_roots = raw_collector.get("roots")
    elif isinstance(raw_collector, list):
        raw_roots = raw_collector
    else:
        raw_roots = None

    roots: list[RemoteCollectorRoot] = []
    if isinstance(raw_roots, list):
        for item in raw_roots:
            if isinstance(item, str):
                raw_path = item.strip()
                provider = ""
            elif isinstance(item, dict):
                raw = item.get("path")
                raw_path = raw.strip() if isinstance(raw, str) else ""
                provider = str(item.get("provider") or "").strip()
            else:
                continue
            if raw_path:
                roots.append(RemoteCollectorRoot(path=raw_path, provider=provider))

    if roots:
        return roots

    return [
        RemoteCollectorRoot(path=root.raw_path or str(root.path), provider=root.provider)
        for root in local_fallback
    ]


def get_remote_specs() -> list[RemoteSpec]:
    """Return SSH remotes configured for history sync.

    Supported config shape:

    {
      "remotes": [
        {
          "name": "remote-dev",
          "host": "remote-dev",
          "collectors": {
            "codex": {"roots": [{"path": "~/.codex", "provider": "openai"}]},
            "claude_code": {"roots": [{"path": "~/.claude"}]}
          }
        }
      ]
    }
    """
    config = _load_config()
    raw_remotes = config.get("remotes")
    if not isinstance(raw_remotes, list):
        return []

    local_codex = get_codex_roots()
    local_claude = get_claude_code_roots()
    remotes: list[RemoteSpec] = []
    for item in raw_remotes:
        if isinstance(item, str):
            host = item.strip()
            remote = {"host": host}
        elif isinstance(item, dict):
            remote = item
            host = str(remote.get("host") or "").strip()
        else:
            continue
        if not host:
            continue

        raw_port = remote.get("port")
        port = raw_port if isinstance(raw_port, int) and raw_port > 0 else None
        raw_timeout = remote.get("timeout")
        timeout = raw_timeout if isinstance(raw_timeout, int) and raw_timeout > 0 else 30
        raw_options = remote.get("ssh_options")
        ssh_options = tuple(str(opt) for opt in raw_options) if isinstance(raw_options, list) else ()
        collectors = {
            "codex": _remote_collector_roots(remote, "codex", local_codex),
            "claude_code": _remote_collector_roots(remote, "claude_code", local_claude),
        }
        remotes.append(
            RemoteSpec(
                host=host,
                name=str(remote.get("name") or "").strip(),
                user=str(remote.get("user") or "").strip(),
                port=port,
                timeout=timeout,
                ssh_options=ssh_options,
                collectors=collectors,
            )
        )
    return remotes

# Refresh intervals (seconds). Defaults can be overridden in CONFIG_FILE:
#   { "intervals": { "data_sync": 300, "auto_refresh": 30 } }
def _interval(name: str, default: int) -> int:
    section = _load_config().get("intervals")
    if isinstance(section, dict):
        val = section.get(name)
        if isinstance(val, (int, float)) and val > 0:
            return int(val)
    return default


DATA_SYNC_INTERVAL = _interval("data_sync", 300)  # history sync to sqlite
AUTO_REFRESH_INTERVAL = _interval("auto_refresh", 30)  # TUI auto-refresh mode (toggled with R)
