"""Collector plugin architecture."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

# Both collectors bucket every parsed entry by local date and hour. Converting
# each ISO timestamp on its own dominated a full reparse (millions of
# ``astimezone``/``strftime`` calls). Instants within the same minute always
# share a local date and hour — UTC offsets and DST shifts are whole minutes —
# so the conversion is cached on the parsed timestamp truncated to the minute.
# The key keeps its ``tzinfo``, so equal wall clocks at different offsets stay
# distinct, and unparseable input never reaches the cache.
_bucket_cache: dict[datetime, tuple[str, int]] = {}

# Roughly one entry per active minute of history; bound it so a long-running
# process cannot grow the cache without limit.
_BUCKET_CACHE_MAX = 200_000

# Python 3.11+ parses a trailing "Z" directly; older versions need it rewritten.
try:
    datetime.fromisoformat("2020-01-01T00:00:00Z")
    _ISO_PARSES_Z = True
except ValueError:  # pragma: no cover - depends on the interpreter version
    _ISO_PARSES_Z = False


def local_time_bucket(ts: str) -> tuple[str, int]:
    """Return local (date, hour) for an ISO timestamp."""
    try:
        if _ISO_PARSES_Z:
            parsed = datetime.fromisoformat(ts)
        else:  # pragma: no cover - only on Python 3.10
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return (ts[:10], 0) if len(ts) >= 10 else ("", 0)
    key = parsed.replace(second=0, microsecond=0)
    cached = _bucket_cache.get(key)
    if cached is not None:
        return cached
    local = parsed.astimezone()
    bucket = (local.strftime("%Y-%m-%d"), local.hour)
    if len(_bucket_cache) >= _BUCKET_CACHE_MAX:
        _bucket_cache.clear()
    _bucket_cache[key] = bucket
    return bucket


class BaseCollector(ABC):
    """Abstract base class for agent collectors."""

    @property
    @abstractmethod
    def agent_type(self) -> str:
        """Unique identifier for this agent type (e.g., 'claude_code')."""

    @abstractmethod
    def sync_history(self, db) -> None:
        """Sync historical data into the database."""


class CollectorRegistry:
    """Registry of all available collectors."""

    def __init__(self) -> None:
        self._collectors: list[BaseCollector] = []
        self._sync_errors: list[str] = []

    def register(self, collector: BaseCollector) -> None:
        self._collectors.append(collector)

    def get_all(self) -> list[BaseCollector]:
        return list(self._collectors)

    @staticmethod
    def _label(collector: BaseCollector) -> str:
        agent_type = getattr(collector, "agent_type", "collector")
        data_root = getattr(collector, "data_root", "")
        return f"{agent_type} {data_root}".strip()

    def sync_all(self, db) -> None:
        """Sync all collectors' history into the database.

        Remote mirroring is network-only, so it runs in background threads while
        the local roots are already being parsed; the database itself is only
        ever written from this thread.
        """
        self._sync_errors = []
        remote = [
            collector
            for collector in self._collectors
            if callable(getattr(collector, "prepare_cache", None))
        ]
        local = [collector for collector in self._collectors if collector not in remote]

        if not remote:
            self._sync_collectors(db, local)
            return

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(
            max_workers=min(8, len(remote)),
            thread_name_prefix="remote-cache",
        ) as pool:
            futures = [
                pool.submit(getattr(collector, "prepare_cache")) for collector in remote
            ]
            self._sync_collectors(db, local)
            for collector, future in zip(remote, futures):
                try:
                    future.result()
                except Exception as exc:
                    # Builtin collectors report mirroring failures through
                    # ``last_error``; a raising one must not abort the sync.
                    self._sync_errors.append(f"{self._label(collector)}: {exc}")
        self._sync_collectors(db, remote)

    def _sync_collectors(self, db, collectors: list[BaseCollector]) -> None:
        for collector in collectors:
            try:
                collector.sync_history(db)
            except Exception as exc:
                db.conn.rollback()
                self._sync_errors.append(f"{self._label(collector)}: {exc}")

    def get_sync_errors(self) -> list[str]:
        """Return collector sync errors that were captured during sync."""
        errors = list(self._sync_errors)
        for collector in self._collectors:
            last_error = getattr(collector, "last_error", "")
            if last_error:
                errors.append(f"{self._label(collector)}: {last_error}")
        return errors


def create_default_registry() -> CollectorRegistry:
    """Create a registry with all available collectors.

    Supported agents:
    - Claude Code (Anthropic CLI)
    - Codex (OpenAI CLI)
    """
    registry = CollectorRegistry()

    from ..config import get_claude_code_roots, get_codex_roots, get_remote_specs

    from .claude_code import ClaudeCodeCollector
    for root in get_claude_code_roots():
        projects_dir = root.path if root.path.name == "projects" else root.path / "projects"
        data_root = root.path.parent if root.path.name == "projects" else root.path
        registry.register(
            ClaudeCodeCollector(
                projects_dir=projects_dir,
                provider=root.provider,
                data_root=str(data_root),
            )
        )

    from .codex import CodexCollector
    for root in get_codex_roots():
        sessions_dir = root.path if root.path.name == "sessions" else root.path / "sessions"
        data_root = root.path.parent if root.path.name == "sessions" else root.path
        registry.register(
            CodexCollector(
                sessions_dir=sessions_dir,
                provider=root.provider,
                data_root=str(data_root),
            )
        )

    from .remote import RemoteHistoryCollector, RemoteSyncTarget
    for remote in get_remote_specs():
        collectors = remote.collectors or {}
        for agent_type in ("claude_code", "codex"):
            for idx, root in enumerate(collectors.get(agent_type, [])):
                registry.register(
                    RemoteHistoryCollector(
                        RemoteSyncTarget(
                            remote=remote,
                            agent_type=agent_type,
                            remote_root=root.path,
                            provider=root.provider,
                            index=idx,
                        )
                    )
                )

    return registry
