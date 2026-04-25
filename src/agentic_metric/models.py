"""Data models shared across all layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class LiveSession:
    """A currently running agent session (real-time from JSONL or process)."""

    session_id: str
    agent_type: str  # 'claude_code', 'codex', etc.
    project_path: str
    git_branch: str = ""
    model: str = ""
    message_count: int = 0
    user_turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    started: str = ""
    last_active: str = ""
    first_prompt: str = ""
    last_prompt: str = ""
    pid: int = 0
    # Today-only counters (for cross-day sessions; equal to totals if started today).
    # Default -1 means "not computed" — collectors set these to 0+ once they
    # have enough data to split today's portion from the session total.
    today_input_tokens: int = -1
    today_output_tokens: int = -1
    today_cache_read_tokens: int = -1
    today_cache_creation_tokens: int = -1
    today_user_turns: int = -1
    today_message_count: int = -1

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )

    @property
    def today_total_tokens(self) -> int:
        if self.today_input_tokens < 0:
            return self.total_tokens
        return (
            self.today_input_tokens
            + self.today_output_tokens
            + self.today_cache_read_tokens
            + self.today_cache_creation_tokens
        )

    @property
    def duration_minutes(self) -> float:
        if not self.started or not self.last_active:
            return 0.0
        try:
            t1 = datetime.fromisoformat(self.started.replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(self.last_active.replace("Z", "+00:00"))
            return max((t2 - t1).total_seconds() / 60.0, 0.0)
        except (ValueError, TypeError):
            return 0.0


@dataclass
class TodayOverview:
    """Aggregated stats for today across all agents."""

    date: str
    active_agents: int = 0
    session_count: int = 0
    message_count: int = 0
    tool_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    estimated_cost_usd: float = 0.0
    unknown_cost_count: int = 0
    by_agent: dict[str, dict] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )


@dataclass
class DailyTrend:
    """One day's aggregated stats for trend display."""

    date: str
    agent_type: str = ""
    session_count: int = 0
    user_turns: int = 0
    message_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    estimated_cost_usd: float = 0.0
    unknown_cost_count: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )
