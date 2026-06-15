"""Collector plugin architecture."""

from __future__ import annotations

from abc import ABC, abstractmethod


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

    def register(self, collector: BaseCollector) -> None:
        self._collectors.append(collector)

    def get_all(self) -> list[BaseCollector]:
        return list(self._collectors)

    def sync_all(self, db) -> None:
        """Sync all collectors' history into the database."""
        for collector in self._collectors:
            try:
                collector.sync_history(db)
            except Exception:
                pass

    def get_sync_errors(self) -> list[str]:
        """Return collector sync errors that were captured during sync."""
        errors: list[str] = []
        for collector in self._collectors:
            last_error = getattr(collector, "last_error", "")
            if not last_error:
                continue
            agent_type = getattr(collector, "agent_type", "collector")
            data_root = getattr(collector, "data_root", "")
            label = f"{agent_type} {data_root}".strip()
            errors.append(f"{label}: {last_error}")
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
