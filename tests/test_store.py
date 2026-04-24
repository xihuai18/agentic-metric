"""Tests for store module."""

import json
import tempfile
from datetime import datetime
from unittest.mock import patch

from agentic_metric.models import DailyTrend, LiveSession, TodayOverview
from agentic_metric.store.database import Database
from agentic_metric.store.aggregator import (
    get_daily_trends,
    get_today_overview,
    merge_live_into_overview,
    merge_live_into_trends,
)


def _make_db() -> Database:
    """Create an in-memory database for testing."""
    tmp = tempfile.mktemp(suffix=".db")
    return Database(db_path=tmp)


def test_database_creation():
    db = _make_db()
    # Check tables exist
    tables = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    names = {r["name"] for r in tables}
    assert "sessions" in names
    assert "sync_state" in names
    db.close()


def test_upsert_session():
    db = _make_db()
    db.upsert_session(
        "s1", "claude_code",
        project_path="/home/test/project",
        input_tokens=1000,
        output_tokens=500,
    )
    db.commit()

    row = db.conn.execute("SELECT * FROM sessions WHERE session_id = 's1'").fetchone()
    assert row is not None
    assert row["agent_type"] == "claude_code"
    assert row["input_tokens"] == 1000

    # Upsert updates
    db.upsert_session("s1", "claude_code", input_tokens=2000, output_tokens=1000)
    db.commit()
    row = db.conn.execute("SELECT * FROM sessions WHERE session_id = 's1'").fetchone()
    assert row["input_tokens"] == 2000
    db.close()


def test_upsert_session_allows_zero_and_started_at_updates():
    db = _make_db()
    db.upsert_session(
        "s1", "claude_code",
        input_tokens=1000,
        estimated_cost_usd=12.0,
        started_at="",
    )
    db.commit()

    db.upsert_session(
        "s1", "claude_code",
        input_tokens=0,
        estimated_cost_usd=0.0,
        started_at="2026-04-23T10:00:00Z",
    )
    db.commit()

    row = db.conn.execute("SELECT * FROM sessions WHERE session_id = 's1'").fetchone()
    assert row["input_tokens"] == 0
    assert row["estimated_cost_usd"] == 0.0
    assert row["started_at"] == "2026-04-23T10:00:00Z"
    db.close()


def test_upsert_session_is_scoped_by_agent_type():
    db = _make_db()
    db.upsert_session("s1", "claude_code", input_tokens=1000)
    db.upsert_session("s1", "codex", input_tokens=2000)
    db.commit()

    rows = db.conn.execute(
        "SELECT agent_type, input_tokens FROM sessions WHERE session_id = 's1' ORDER BY agent_type"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["input_tokens"] != rows[1]["input_tokens"]
    db.close()


def test_database_reprices_sessions_when_pricing_changes(tmp_path):
    pricing_file = tmp_path / "pricing.json"
    db_path = str(tmp_path / "data.db")
    pricing_file.write_text(json.dumps({"custom-model": [1.0, 2.0, 0.0, 0.0]}))

    with patch("agentic_metric.pricing.PRICING_FILE", pricing_file):
        db = Database(db_path=db_path)
        db.upsert_session("s1", "claude_code", model="custom-model", input_tokens=1_000_000)
        db.commit()
        db.close()

        pricing_file.write_text(json.dumps({"custom-model": [3.0, 2.0, 0.0, 0.0]}))
        db = Database(db_path=db_path)
        row = db.conn.execute(
            "SELECT estimated_cost_usd FROM sessions WHERE session_id = 's1' AND agent_type = 'claude_code'"
        ).fetchone()
        assert row["estimated_cost_usd"] == 3.0
        db.close()


def test_sync_state():
    db = _make_db()
    assert db.get_sync_state("test_key") is None
    db.set_sync_state("test_key", "test_value")
    assert db.get_sync_state("test_key") == "test_value"
    db.set_sync_state("test_key", "updated")
    assert db.get_sync_state("test_key") == "updated"
    db.close()


def test_today_overview_empty():
    db = _make_db()
    overview = get_today_overview(db)
    assert overview.session_count == 0
    assert overview.total_tokens == 0
    db.close()


def test_today_overview_from_sessions():
    db = _make_db()
    today = datetime.now().strftime("%Y-%m-%d")
    db.upsert_session(
        "s1", "claude_code",
        started_at=f"{today}T10:00:00",
        input_tokens=1000, output_tokens=500, message_count=10,
    )
    db.upsert_session(
        "s2", "codex",
        started_at=f"{today}T11:00:00",
        input_tokens=2000, output_tokens=1000, message_count=20,
    )
    db.commit()

    overview = get_today_overview(db)
    assert overview.session_count == 2
    assert overview.input_tokens == 3000
    assert overview.output_tokens == 1500
    assert overview.message_count == 30
    assert len(overview.by_agent) == 2
    db.close()


def test_merge_live_overview_matches_by_session_and_agent():
    today = datetime.now().strftime("%Y-%m-%d")
    today_sessions = [{
        "session_id": "same-id",
        "agent_type": "claude_code",
        "message_count": 4,
        "user_turns": 2,
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "estimated_cost_usd": 10.0,
    }]
    overview = TodayOverview(
        date=today,
        session_count=1,
        message_count=4,
        tool_call_count=2,
        input_tokens=1000,
        output_tokens=200,
        estimated_cost_usd=10.0,
        by_agent={
            "claude_code": {
                "session_count": 1,
                "turns": 2,
                "message_count": 4,
                "input_tokens": 1000,
                "output_tokens": 200,
                "cost": 10.0,
            }
        },
    )
    live = LiveSession(
        session_id="same-id",
        agent_type="codex",
        project_path="/tmp/project",
        model="gpt-5.4",
        message_count=2,
        user_turns=1,
        input_tokens=50,
        output_tokens=5,
    )

    merge_live_into_overview(overview, [live], today_sessions)

    assert overview.session_count == 2
    assert overview.input_tokens == 1050
    assert overview.output_tokens == 205
    assert overview.by_agent["codex"]["session_count"] == 1


def test_merge_live_trends_matches_by_session_and_agent():
    today = datetime.now().strftime("%Y-%m-%d")
    today_sessions = [{
        "session_id": "same-id",
        "agent_type": "claude_code",
        "message_count": 4,
        "user_turns": 2,
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "estimated_cost_usd": 10.0,
    }]
    trends = [
        DailyTrend(
            date=today,
            session_count=1,
            user_turns=2,
            message_count=4,
            input_tokens=1000,
            output_tokens=200,
            estimated_cost_usd=10.0,
        )
    ]
    live = LiveSession(
        session_id="same-id",
        agent_type="codex",
        project_path="/tmp/project",
        model="gpt-5.4",
        message_count=2,
        user_turns=1,
        input_tokens=50,
        output_tokens=5,
    )

    merge_live_into_trends(trends, [live], today_sessions)

    assert trends[0].session_count == 2
    assert trends[0].input_tokens == 1050
    assert trends[0].output_tokens == 205


def test_daily_trends():
    db = _make_db()
    db.upsert_session(
        "s1", "claude_code",
        started_at="2025-01-01T10:00:00",
        input_tokens=10000, output_tokens=5000, message_count=10,
    )
    db.upsert_session(
        "s2", "claude_code",
        started_at="2025-01-02T10:00:00",
        input_tokens=20000, output_tokens=10000, message_count=20,
    )
    db.upsert_session(
        "s3", "codex",
        started_at="2025-01-02T11:00:00",
        input_tokens=5000, output_tokens=2000, message_count=5,
    )
    db.commit()

    trends = get_daily_trends(db, days=365 * 10)
    assert len(trends) == 2
    # trends are ordered DESC (most recent first)
    assert trends[0].date == "2025-01-02"
    assert trends[0].session_count == 2
    assert trends[0].input_tokens == 25000
    assert trends[1].date == "2025-01-01"
    assert trends[1].session_count == 1
    assert trends[1].input_tokens == 10000
    db.close()
