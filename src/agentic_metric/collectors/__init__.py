"""Collector plugin architecture."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import LiveSession


class BaseCollector(ABC):
    """Abstract base class for agent collectors."""

    @property
    @abstractmethod
    def agent_type(self) -> str:
        """Unique identifier for this agent type (e.g., 'claude_code')."""

    @abstractmethod
    def get_live_sessions(self) -> list[LiveSession]:
        """Return currently active sessions."""

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

    def get_live_sessions(self) -> list[LiveSession]:
        """Get live sessions from all registered collectors."""
        sessions: list[LiveSession] = []
        for collector in self._collectors:
            try:
                sessions.extend(collector.get_live_sessions())
            except Exception:
                pass
        sessions.sort(key=lambda s: s.last_active, reverse=True)
        return sessions

    def sync_all(self, db) -> None:
        """Sync all collectors' history into the database."""
        for collector in self._collectors:
            try:
                collector.sync_history(db)
            except Exception:
                pass


def create_default_registry() -> CollectorRegistry:
    """Create a registry with all available collectors.

    Supported agents:
    - Claude Code (Anthropic CLI)
    - Codex (OpenAI CLI)
    """
    registry = CollectorRegistry()

    from ..config import get_claude_code_roots, get_codex_roots

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

    return registry
