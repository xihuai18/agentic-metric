"""Configuration constants and paths."""

import os
import platform
from pathlib import Path

_HOME = Path.home()
_IS_MAC = platform.system() == "Darwin"

# Platform-specific base directories
_APP_SUPPORT = _HOME / "Library" / "Application Support" if _IS_MAC else None


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
    else (_HOME / ".local" / "share" / "agentic_metric")
)
DB_PATH = DATA_DIR / "data.db"
PRICING_FILE = DATA_DIR / "pricing.json"

# Refresh intervals (seconds)
LIVE_REFRESH_INTERVAL = 1  # running sessions
DATA_SYNC_INTERVAL = 300  # history sync to sqlite
