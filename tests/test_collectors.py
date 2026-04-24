"""Tests for collector module."""

import json
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from agentic_metric.collectors import CollectorRegistry, BaseCollector
from agentic_metric.collectors.claude_code import _SessionAccum as ClaudeSessionAccum
from agentic_metric.collectors.codex import CodexCollector, _SessionAccum as CodexSessionAccum
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
    })
    assert accum.input_tokens == 900

    accum._process_event_msg({
        "type": "token_count",
        "info": {"total_token_usage": {"cached_input_tokens": 200}},
    })
    assert accum.input_tokens == 800


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
