"""Tests for collector module."""

import json
import os
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from agentic_metric.collectors import CollectorRegistry, BaseCollector
from agentic_metric.collectors.claude_code import (
    ClaudeCodeCollector,
    _SessionAccum as ClaudeSessionAccum,
)
from agentic_metric.collectors.codex import (
    CodexCollector,
    _LiveMonitor as CodexLiveMonitor,
    _SessionAccum as CodexSessionAccum,
)
from agentic_metric.models import LiveSession
from agentic_metric.store.database import Database


class MockCollector(BaseCollector):
    @property
    def agent_type(self) -> str:
        return "mock"

    def get_live_sessions(self) -> list[LiveSession]:
        return [
            LiveSession(
                session_id="test-1",
                agent_type="mock",
                project_path="/test/project",
                user_turns=5,
                output_tokens=1000,
            )
        ]

    def sync_history(self, db) -> None:
        pass


def test_registry_register():
    registry = CollectorRegistry()
    collector = MockCollector()
    registry.register(collector)
    assert len(registry.get_all()) == 1
    assert registry.get_all()[0].agent_type == "mock"


def test_registry_get_live_sessions():
    registry = CollectorRegistry()
    registry.register(MockCollector())
    sessions = registry.get_live_sessions()
    assert len(sessions) == 1
    assert sessions[0].session_id == "test-1"
    assert sessions[0].agent_type == "mock"


def test_live_session_total_tokens():
    s = LiveSession(
        session_id="x",
        agent_type="test",
        project_path="/test",
        input_tokens=100,
        output_tokens=200,
    )
    assert s.total_tokens == 300


def test_live_session_duration():
    s = LiveSession(
        session_id="x",
        agent_type="test",
        project_path="/test",
        started="2025-01-01T10:00:00Z",
        last_active="2025-01-01T10:30:00Z",
    )
    assert abs(s.duration_minutes - 30.0) < 0.1


def test_codex_cached_only_update_recomputes_input_tokens():
    accum = CodexSessionAccum(Path("/tmp/fake.jsonl"), project_path="/test")
    accum._process_event_msg({
        "type": "token_count",
        "info": {"total_token_usage": {"input_tokens": 1000, "cached_input_tokens": 100, "output_tokens": 50}},
    }, ts="2026-04-24T10:00:00Z")
    assert accum.input_tokens == 900

    accum._process_event_msg({
        "type": "token_count",
        "info": {"total_token_usage": {"cached_input_tokens": 200}},
    }, ts="2026-04-24T10:01:00Z")
    assert accum.input_tokens == 800
    assert sum(r["input_tokens"] for r in accum.usage_bucket_rows()) == 800
    assert sum(r["cache_read_tokens"] for r in accum.usage_bucket_rows()) == 200


def test_codex_partial_trailing_jsonl_is_retried(tmp_path):
    session_file = tmp_path / "rollout-test.jsonl"
    token_line = {
        "timestamp": "2026-04-23T10:00:02Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 20}},
        },
    }
    prefix = [
        {
            "timestamp": "2026-04-23T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": "sid", "cwd": "/tmp/project"},
        },
        {
            "timestamp": "2026-04-23T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "hello"},
        },
    ]
    partial = json.dumps(token_line)[:-1]
    session_file.write_text("".join(json.dumps(line) + "\n" for line in prefix) + partial)

    accum = CodexSessionAccum(session_file, project_path="/tmp/project")
    accum.read_new_lines()
    assert accum.output_tokens == 0

    with session_file.open("a") as f:
        f.write("}\n")
    accum.read_new_lines()
    assert accum.output_tokens == 20


def test_claude_partial_trailing_jsonl_is_retried(tmp_path):
    session_file = tmp_path / "session.jsonl"
    assistant_line = {
        "timestamp": "2026-04-23T10:00:02Z",
        "type": "assistant",
        "message": {
            "model": "claude-sonnet-4-6",
            "usage": {"input_tokens": 100, "output_tokens": 20},
        },
    }
    user_line = {
        "timestamp": "2026-04-23T10:00:01Z",
        "type": "user",
        "message": {"content": "hello"},
    }
    session_file.write_text(json.dumps(user_line) + "\n" + json.dumps(assistant_line)[:-1])

    accum = ClaudeSessionAccum(session_file, project_path="/tmp/project")
    accum.read_new_lines()
    assert accum.output_tokens == 0

    with session_file.open("a") as f:
        f.write("}\n")
    accum.read_new_lines()
    assert accum.output_tokens == 20


def test_claude_accumulator_resets_after_truncation(tmp_path):
    session_file = tmp_path / "session.jsonl"
    first = [
        {
            "timestamp": "2026-04-23T10:00:00Z",
            "type": "user",
            "message": {"content": "first"},
        },
        {
            "timestamp": "2026-04-23T10:00:01Z",
            "type": "assistant",
            "message": {
                "model": "claude-sonnet-4-6",
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        },
    ]
    second = [{
        "timestamp": "2026-04-23T10:00:02Z",
        "type": "user",
        "message": {"content": "second"},
    }]
    session_file.write_text("".join(json.dumps(line) + "\n" for line in first))

    accum = ClaudeSessionAccum(session_file, project_path="/tmp/project")
    accum.read_new_lines()
    assert accum.user_turns == 1
    assert accum.output_tokens == 20

    session_file.write_text("".join(json.dumps(line) + "\n" for line in second))
    accum.read_new_lines()
    assert accum.user_turns == 1
    assert accum.output_tokens == 0
    assert accum.first_prompt == "second"


def test_claude_duplicate_assistant_message_id_uses_last_usage(tmp_path):
    session_file = tmp_path / "session.jsonl"
    lines = [
        {
            "timestamp": "2026-04-23T10:00:00Z",
            "type": "user",
            "message": {"content": "hello"},
        },
        {
            "timestamp": "2026-04-23T10:00:01Z",
            "type": "assistant",
            "message": {
                "id": "msg-1",
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 1,
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 5,
                },
            },
        },
        {
            "timestamp": "2026-04-23T10:00:02Z",
            "type": "assistant",
            "message": {
                "id": "msg-1",
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 5,
                },
            },
        },
    ]
    session_file.write_text("".join(json.dumps(line) + "\n" for line in lines))

    accum = ClaudeSessionAccum(session_file, project_path="/tmp/project")
    accum.read_new_lines()

    assert accum.message_count == 2
    assert accum.input_tokens == 10
    assert accum.output_tokens == 20
    assert accum.cache_read == 100
    assert accum.cache_create == 5
    assert sum(r["message_count"] for r in accum.usage_bucket_rows()) == 2
    assert sum(r["input_tokens"] for r in accum.usage_bucket_rows()) == 10
    assert sum(r["output_tokens"] for r in accum.usage_bucket_rows()) == 20
    assert sum(r["cache_read_tokens"] for r in accum.usage_bucket_rows()) == 100
    assert sum(r["cache_creation_tokens"] for r in accum.usage_bucket_rows()) == 5


def test_claude_today_counters_reset_after_midnight(tmp_path):
    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 4, 23, 0, 5, 0)

    session_file = tmp_path / "session.jsonl"
    session_file.write_text("")
    accum = ClaudeSessionAccum(session_file, project_path="/test")
    accum.today_key = "2026-04-22"
    accum.today_user_turns = 3
    accum.today_message_count = 7
    accum.today_input_tokens = 100
    accum.today_output_tokens = 50
    accum.today_cache_read = 25
    accum.today_cache_create = 10

    with patch("agentic_metric.collectors.claude_code.datetime", FakeDateTime):
        accum.read_new_lines()

    assert accum.today_key == "2026-04-23"
    assert accum.today_user_turns == 0
    assert accum.today_message_count == 0
    assert accum.today_input_tokens == 0
    assert accum.today_output_tokens == 0
    assert accum.today_cache_read == 0
    assert accum.today_cache_create == 0


def test_claude_history_sync_scans_subagent_jsonl(tmp_path):
    projects = tmp_path / "projects"
    subagents = projects / "-tmp-project" / "parent-session" / "subagents"
    subagents.mkdir(parents=True)
    subagent_file = subagents / "agent-a1.jsonl"
    lines = [
        {
            "timestamp": "2026-04-23T10:00:00Z",
            "type": "user",
            "cwd": "/tmp/project",
            "message": {"content": "sub task"},
        },
        {
            "timestamp": "2026-04-23T10:00:01Z",
            "type": "assistant",
            "cwd": "/tmp/project",
            "message": {
                "id": "msg-sub",
                "model": "claude-sonnet-4-6",
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        },
    ]
    subagent_file.write_text("".join(json.dumps(line) + "\n" for line in lines))

    db = Database(db_path=str(tmp_path / "data.db"))
    with patch("agentic_metric.collectors.claude_code.PROJECTS_DIR", projects):
        ClaudeCodeCollector().sync_history(db)

    row = db.conn.execute(
        "SELECT project_path, input_tokens, output_tokens "
        "FROM sessions WHERE session_id = 'parent-session:agent-a1' "
        "AND agent_type = 'claude_code'"
    ).fetchone()
    assert row is not None
    assert row["project_path"] == "/tmp/project"
    assert row["input_tokens"] == 100
    assert row["output_tokens"] == 20
    db.close()


def test_codex_cross_day_live_session_uses_today_counters(tmp_path):
    class FakeDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 4, 24)

    session_file = tmp_path / "rollout-test.jsonl"
    lines = [
        {
            "timestamp": "2026-04-23T08:55:00Z",
            "type": "session_meta",
            "payload": {"id": "sid", "cwd": "/tmp/project"},
        },
        {
            "timestamp": "2026-04-23T08:56:00Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "yesterday"},
        },
        {
            "timestamp": "2026-04-23T08:57:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1000,
                        "cached_input_tokens": 200,
                        "output_tokens": 100,
                    }
                },
            },
        },
        {
            "timestamp": "2026-04-24T08:01:00Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "today"},
        },
        {
            "timestamp": "2026-04-24T08:02:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1500,
                        "cached_input_tokens": 300,
                        "output_tokens": 150,
                    }
                },
            },
        },
    ]
    session_file.write_text("".join(json.dumps(line) + "\n" for line in lines))

    accum = CodexSessionAccum(session_file, project_path="/tmp/project")
    with patch("agentic_metric.collectors.codex.date", FakeDate):
        accum.read_new_lines()

    live = accum.to_live_session()
    assert live.input_tokens == 1200
    assert live.output_tokens == 150
    assert live.cache_read_tokens == 300
    assert live.today_input_tokens == 400
    assert live.today_output_tokens == 50
    assert live.today_cache_read_tokens == 100
    assert live.today_user_turns == 1

    buckets = {r["usage_date"]: r for r in accum.usage_bucket_rows()}
    assert buckets["2026-04-23"]["message_count"] == 1
    assert buckets["2026-04-23"]["user_turns"] == 1
    assert buckets["2026-04-23"]["input_tokens"] == 800
    assert buckets["2026-04-23"]["output_tokens"] == 100
    assert buckets["2026-04-23"]["cache_read_tokens"] == 200
    assert buckets["2026-04-24"]["message_count"] == 1
    assert buckets["2026-04-24"]["user_turns"] == 1
    assert buckets["2026-04-24"]["input_tokens"] == 400
    assert buckets["2026-04-24"]["output_tokens"] == 50
    assert buckets["2026-04-24"]["cache_read_tokens"] == 100


def test_codex_forked_session_subtracts_replayed_parent_baseline(tmp_path):
    session_file = tmp_path / "rollout-forked.jsonl"
    lines = [
        {
            "timestamp": "2026-04-24T03:05:12Z",
            "type": "session_meta",
            "payload": {
                "id": "child",
                "forked_from_id": "parent",
                "cwd": "/tmp/project",
                "source": {"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}},
            },
        },
        {
            "timestamp": "2026-04-24T03:05:12Z",
            "type": "session_meta",
            "payload": {"id": "parent", "cwd": "/tmp/project"},
        },
        {
            "timestamp": "2026-04-24T03:05:12Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "old parent prompt"},
        },
        {
            "timestamp": "2026-04-24T03:05:12Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1000,
                        "cached_input_tokens": 800,
                        "output_tokens": 100,
                    }
                },
            },
        },
        {
            "timestamp": "2026-04-24T03:05:16Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.5"},
        },
        {
            "timestamp": "2026-04-24T03:05:16Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "child task"},
        },
        {
            "timestamp": "2026-04-24T03:05:17Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1000,
                        "cached_input_tokens": 800,
                        "output_tokens": 100,
                    }
                },
            },
        },
        {
            "timestamp": "2026-04-24T03:05:18Z",
            "type": "event_msg",
            "payload": {"type": "agent_message"},
        },
        {
            "timestamp": "2026-04-24T03:05:19Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1300,
                        "cached_input_tokens": 900,
                        "output_tokens": 120,
                    }
                },
            },
        },
    ]
    session_file.write_text("".join(json.dumps(line) + "\n" for line in lines))

    accum = CodexSessionAccum(session_file, project_path="/tmp/project")
    accum.read_new_lines()

    assert accum.session_id == "child"
    assert accum.model == "gpt-5.5"
    assert accum.user_turns == 1
    assert accum.message_count == 2
    assert accum.first_prompt == "child task"
    assert accum.input_tokens == 200
    assert accum.cache_read == 100
    assert accum.output_tokens == 20


def test_codex_live_monitor_finds_older_active_session(tmp_path):
    class FakeDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 4, 24)

    sessions_root = tmp_path / "sessions"
    old_dir = sessions_root / "2026" / "04" / "20"
    old_dir.mkdir(parents=True)
    rollout = old_dir / "rollout-old.jsonl"
    lines = [
        {
            "timestamp": "2026-04-20T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": "old-sid", "cwd": "/tmp/project"},
        },
        {
            "timestamp": "2026-04-20T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "still running"},
        },
    ]
    rollout.write_text("".join(json.dumps(line) + "\n" for line in lines))

    monitor = CodexLiveMonitor()
    with (
        patch("agentic_metric.collectors.codex.CODEX_SESSIONS_DIR", sessions_root),
        patch("agentic_metric.collectors.codex.get_running_cwds", return_value={123: "/tmp/project"}),
        patch("agentic_metric.collectors.codex.date", FakeDate),
    ):
        sessions = monitor.refresh()

    assert len(sessions) == 1
    assert sessions[0].session_id == "old-sid"


def test_codex_history_sync_detects_same_size_file_edits(tmp_path):
    def write_rollout(path: Path, output_tokens: int) -> None:
        lines = [
            {
                "timestamp": "2026-04-23T10:00:00Z",
                "type": "session_meta",
                "payload": {"id": "sid", "cwd": "/tmp/project", "git": {"branch": "main"}},
            },
            {
                "timestamp": "2026-04-23T10:00:00Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.4"},
            },
            {
                "timestamp": "2026-04-23T10:00:01Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "hello"},
            },
            {
                "timestamp": "2026-04-23T10:00:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 0,
                            "output_tokens": output_tokens,
                        }
                    },
                },
            },
        ]
        path.write_text("".join(json.dumps(line) + "\n" for line in lines))
        os.utime(path, None)

    sessions_dir = tmp_path / "sessions" / "2026" / "04" / "23"
    sessions_dir.mkdir(parents=True)
    rollout = sessions_dir / "rollout-test.jsonl"
    db = Database(db_path=str(tmp_path / "data.db"))
    collector = CodexCollector()

    with patch("agentic_metric.collectors.codex.CODEX_SESSIONS_DIR", tmp_path / "sessions"):
        write_rollout(rollout, 10)
        collector.sync_history(db)
        row = db.conn.execute(
            "SELECT output_tokens FROM sessions WHERE session_id = 'sid' AND agent_type = 'codex'"
        ).fetchone()
        assert row["output_tokens"] == 10

        write_rollout(rollout, 99)
        collector.sync_history(db)
        row = db.conn.execute(
            "SELECT output_tokens FROM sessions WHERE session_id = 'sid' AND agent_type = 'codex'"
        ).fetchone()
        assert row["output_tokens"] == 99

    db.close()


def test_codex_history_sync_cost_uses_bucket_models(tmp_path):
    sessions_dir = tmp_path / "sessions" / "2026" / "04" / "23"
    sessions_dir.mkdir(parents=True)
    rollout = sessions_dir / "rollout-test.jsonl"
    lines = [
        {
            "timestamp": "2026-04-23T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": "sid", "cwd": "/tmp/project"},
        },
        {
            "timestamp": "2026-04-23T10:00:00Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.4"},
        },
        {
            "timestamp": "2026-04-23T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "first"},
        },
        {
            "timestamp": "2026-04-23T10:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1000,
                        "cached_input_tokens": 0,
                        "output_tokens": 100,
                    }
                },
            },
        },
        {
            "timestamp": "2026-04-24T10:00:00Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.5"},
        },
        {
            "timestamp": "2026-04-24T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "second"},
        },
        {
            "timestamp": "2026-04-24T10:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 2000,
                        "cached_input_tokens": 0,
                        "output_tokens": 200,
                    }
                },
            },
        },
    ]
    rollout.write_text("".join(json.dumps(line) + "\n" for line in lines))

    db = Database(db_path=str(tmp_path / "data.db"))
    with patch("agentic_metric.collectors.codex.CODEX_SESSIONS_DIR", tmp_path / "sessions"):
        CodexCollector().sync_history(db)

    row = db.conn.execute(
        "SELECT estimated_cost_usd FROM sessions WHERE session_id = 'sid' AND agent_type = 'codex'"
    ).fetchone()
    assert abs(row["estimated_cost_usd"] - 0.012) < 1e-12

    db.close()
