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


def _collector_roots(name: str, default_path: Path) -> list[CollectorRoot]:
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
    collectors = config.get("collectors")
    raw_collector = collectors.get(name) if isinstance(collectors, dict) else None
    raw_roots = None
    if isinstance(raw_collector, dict):
        raw_roots = raw_collector.get("roots")
    elif isinstance(raw_collector, list):
        raw_roots = raw_collector

    roots: list[CollectorRoot] = []
    if isinstance(raw_roots, list):
        for item in raw_roots:
            if isinstance(item, str):
                path = _expand_path(item)
                provider = ""
            elif isinstance(item, dict):
                path = _expand_path(item.get("path"))
                provider = str(item.get("provider") or "").strip()
            else:
                continue
            if path is not None:
                roots.append(CollectorRoot(path=path, provider=provider))

    return roots or [CollectorRoot(default_path)]


def get_codex_roots() -> list[CollectorRoot]:
    return _collector_roots("codex", CODEX_HOME)


def get_claude_code_roots() -> list[CollectorRoot]:
    return _collector_roots("claude_code", CLAUDE_HOME)

# Refresh intervals (seconds)
LIVE_REFRESH_INTERVAL = 1  # running sessions
DATA_SYNC_INTERVAL = 300  # history sync to sqlite
AUTO_REFRESH_INTERVAL = 30  # TUI auto-refresh mode (toggled with R)
