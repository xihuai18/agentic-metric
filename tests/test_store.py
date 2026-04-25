"""Tests for store module."""

import json
import tempfile
from datetime import datetime
from unittest.mock import patch

from agentic_metric.models import DailyTrend, LiveSession, TodayOverview
from agentic_metric.store.database import Database
from agentic_metric.store.aggregator import (
    get_daily_trends,
    get_range_by_project,
    get_range_by_agent_model,
    get_range_daily,
    get_range_by_time_model,
    get_range_top_sessions,
    get_range_totals,
    get_today_sessions,
    get_today_overview,
    merge_live_into_overview,
    merge_live_into_trends,
)
from agentic_metric.cli import _fmt_cost as cli_fmt_cost
from agentic_metric.tui.widgets import Breakdown, fmt_cost as tui_fmt_cost


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
    assert "session_usage" in names
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
    pricing_file.write_text(json.dumps({
        "models": {"custom-model": [1.0, 2.0, 0.0, 0.0]},
    }))

    with patch("agentic_metric.pricing.PRICING_FILE", pricing_file):
        db = Database(db_path=db_path)
        db.upsert_session("s1", "claude_code", model="custom-model", input_tokens=1_000_000)
        db.commit()
        db.close()

        pricing_file.write_text(json.dumps({
            "models": {"custom-model": [3.0, 2.0, 0.0, 0.0]},
        }))
        db = Database(db_path=db_path)
        assert db.pricing_changed is True
        row = db.conn.execute(
            "SELECT estimated_cost_usd FROM sessions WHERE session_id = 's1' AND agent_type = 'claude_code'"
        ).fetchone()
        assert row["estimated_cost_usd"] == 3.0
        db.close()

        db = Database(db_path=db_path)
        assert db.pricing_changed is False
        db.close()


def test_database_reprices_session_usage_when_pricing_changes(tmp_path):
    pricing_file = tmp_path / "pricing.json"
    db_path = str(tmp_path / "data.db")
    pricing_file.write_text(json.dumps({
        "models": {
            "cheap-model": [1.0, 0.0, 0.0, 0.0],
            "expensive-model": [10.0, 0.0, 0.0, 0.0],
        },
    }))

    with patch("agentic_metric.pricing.PRICING_FILE", pricing_file):
        db = Database(db_path=db_path)
        db.upsert_session(
            "s1", "codex",
            model="expensive-model",
            input_tokens=2_000_000,
            estimated_cost_usd=11.0,
        )
        db.replace_session_usage(
            "s1",
            "codex",
            [
                {
                    "usage_date": "2026-04-23",
                    "usage_hour": 23,
                    "model": "cheap-model",
                    "input_tokens": 1_000_000,
                },
                {
                    "usage_date": "2026-04-24",
                    "usage_hour": 0,
                    "model": "expensive-model",
                    "input_tokens": 1_000_000,
                },
            ],
        )
        db.commit()
        db.close()

        pricing_file.write_text(json.dumps({
            "models": {
                "cheap-model": [2.0, 0.0, 0.0, 0.0],
                "expensive-model": [20.0, 0.0, 0.0, 0.0],
            },
        }))
        db = Database(db_path=db_path)
        row = db.conn.execute(
            "SELECT estimated_cost_usd FROM sessions WHERE session_id = 's1' AND agent_type = 'codex'"
        ).fetchone()
        assert row["estimated_cost_usd"] == 22.0
        db.close()


def test_replace_session_usage_preserves_collector_estimated_cost():
    db = _make_db()
    db.replace_session_usage(
        "s1",
        "codex",
        [
            {
                "usage_date": "2026-04-24",
                "usage_hour": 10,
                "model": "gpt-5.4",
                "input_tokens": 1,
                "estimated_cost_usd": 123.45,
            },
        ],
    )
    db.commit()

    row = db.conn.execute(
        "SELECT estimated_cost_usd FROM session_usage WHERE session_id = 's1' AND agent_type = 'codex'"
    ).fetchone()
    assert row["estimated_cost_usd"] == 123.45
    db.close()


def test_range_reports_group_by_model_only():
    db = _make_db()
    db.replace_session_usage(
        "s1",
        "codex",
        [
            {
                "usage_date": "2026-04-24",
                "usage_hour": 10,
                "model": "gpt-5.5",
                "input_tokens": 1_000_000,
                "output_tokens": 1_000_000,
            },
            {
                "usage_date": "2026-04-24",
                "usage_hour": 11,
                "model": "gpt-5.5",
                "input_tokens": 1_000_000,
                "output_tokens": 1_000_000,
            },
        ],
    )
    db.commit()

    rows = db.conn.execute(
        """SELECT model, estimated_cost_usd
           FROM session_usage
           WHERE session_id = 's1' AND agent_type = 'codex'
           ORDER BY usage_hour"""
    ).fetchall()
    assert [row["estimated_cost_usd"] for row in rows] == [35.0, 35.0]

    model_rows = get_range_by_agent_model(db, "2026-04-24", "2026-04-24")
    assert len(model_rows) == 1
    assert model_rows[0]["model"] == "gpt-5.5"
    assert model_rows[0]["estimated_cost_usd"] == 70.0

    time_rows = get_range_by_time_model(db, "2026-04-24", "2026-04-24", limit=10)
    assert {row["usage_hour"] for row in time_rows} == {10, 11}
    assert all(row["model"] == "gpt-5.5" for row in time_rows)

    session_rows = get_range_top_sessions(db, "2026-04-24", "2026-04-24", limit=1)
    session_models = {
        model.strip() for model in session_rows[0]["models"].split(",") if model.strip()
    }
    assert session_models == {"gpt-5.5"}
    db.close()


def test_replace_session_usage_prices_known_model():
    db = _make_db()
    db.replace_session_usage(
        "s1",
        "codex",
        [
            {
                "usage_date": "2026-04-24",
                "usage_hour": 10,
                "model": "gpt-5.4",
                "input_tokens": 1_000,
            },
        ],
    )
    db.commit()

    row = db.conn.execute(
        """SELECT estimated_cost_usd
           FROM session_usage
           WHERE session_id = 's1' AND agent_type = 'codex'"""
    ).fetchone()
    assert row["estimated_cost_usd"] == 0.0025

    model_rows = get_range_by_agent_model(db, "2026-04-24", "2026-04-24")
    assert model_rows[0]["model"] == "gpt-5.4"
    assert model_rows[0]["unknown_cost_count"] == 0

    top_sessions = get_range_top_sessions(db, "2026-04-24", "2026-04-24", limit=1)
    assert top_sessions[0]["models"] == "gpt-5.4"
    assert top_sessions[0]["unknown_cost_count"] == 0
    db.close()


def test_explicit_usage_cost_survives_pricing_reprice(tmp_path):
    pricing_file = tmp_path / "pricing.json"
    db_path = str(tmp_path / "data.db")
    explicit_cost = (300_000 * 5.0 + 1_000 * 22.5) / 1_000_000

    with patch("agentic_metric.pricing.PRICING_FILE", pricing_file):
        db = Database(db_path=db_path)
        db.upsert_session(
            "s1",
            "codex",
            model="gpt-5.4",
            input_tokens=300_000,
            output_tokens=1_000,
            estimated_cost_usd=explicit_cost,
        )
        db.replace_session_usage(
            "s1",
            "codex",
            [
                {
                    "usage_date": "2026-04-24",
                    "usage_hour": 10,
                    "model": "gpt-5.4",
                    "input_tokens": 300_000,
                    "output_tokens": 1_000,
                    "estimated_cost_usd": explicit_cost,
                },
            ],
        )
        db.set_sync_state("codex_jsonl:v5:/tmp/rollout.jsonl", "1:1")
        db.set_sync_state("pricing:fingerprint", "stale")
        db.close()

        db = Database(db_path=db_path)
        row = db.conn.execute(
            """SELECT estimated_cost_usd, cost_is_explicit
               FROM session_usage
               WHERE session_id = 's1' AND agent_type = 'codex'"""
        ).fetchone()
        session = db.conn.execute(
            """SELECT estimated_cost_usd
               FROM sessions
               WHERE session_id = 's1' AND agent_type = 'codex'"""
        ).fetchone()
        assert abs(row["estimated_cost_usd"] - explicit_cost) < 0.001
        assert row["cost_is_explicit"] == 1
        assert abs(session["estimated_cost_usd"] - explicit_cost) < 0.001
        assert db.get_sync_state("codex_jsonl:v5:/tmp/rollout.jsonl") is None
        db.close()


def test_unknown_model_cost_stays_null_and_surfaces_as_unknown(tmp_path):
    import agentic_metric.pricing as pricing

    pricing._user_cache = None
    pricing._user_cache_mtime = -1.0
    with patch("agentic_metric.pricing.PRICING_FILE", tmp_path / "pricing.json"):
        db = _make_db()
        db.upsert_session(
            "s_unknown",
            "codex",
            project_path="/tmp/project",
            model="gpt-5.4-pro",
            input_tokens=1_000,
            estimated_cost_usd=None,
            started_at="2026-04-24T10:00:00Z",
            first_prompt="unknown model prompt",
        )
        db.replace_session_usage(
            "s_unknown",
            "codex",
            [
                {
                    "usage_date": "2026-04-24",
                    "usage_hour": 10,
                    "project_path": "/tmp/project",
                    "model": "gpt-5.4-pro",
                    "input_tokens": 1_000,
                },
            ],
        )
        db.commit()

        row = db.conn.execute(
            "SELECT estimated_cost_usd FROM session_usage WHERE session_id = 's_unknown'"
        ).fetchone()
        assert row["estimated_cost_usd"] is None

        totals = get_range_totals(db, "2026-04-24", "2026-04-24")
        assert totals["estimated_cost_usd"] == 0
        assert totals["unknown_cost_count"] == 1

        model_rows = get_range_by_agent_model(db, "2026-04-24", "2026-04-24")
        assert model_rows[0]["model"] == "Unknown"
        assert model_rows[0]["unknown_cost_count"] == 1

        time_rows = get_range_by_time_model(db, "2026-04-24", "2026-04-24", limit=1)
        assert time_rows[0]["model"] == "Unknown"
        assert time_rows[0]["unknown_cost_count"] == 1

        project_rows = get_range_by_project(db, "2026-04-24", "2026-04-24", limit=1)
        assert project_rows[0]["unknown_cost_count"] == 1

        top_sessions = get_range_top_sessions(db, "2026-04-24", "2026-04-24", limit=1)
        assert top_sessions[0]["models"] == "Unknown"
        assert top_sessions[0]["unknown_cost_count"] == 1
        db.close()
    pricing._user_cache = None
    pricing._user_cache_mtime = -1.0


def test_aggregate_usage_rows_do_not_trigger_long_context_surcharge():
    db = _make_db()
    db.replace_session_usage(
        "s_agg",
        "codex",
        [
            {
                "usage_date": "2026-04-24",
                "usage_hour": 10,
                "model": "gpt-5.4",
                "input_tokens": 300_000,
                "output_tokens": 1_000,
            },
        ],
    )
    db.commit()

    row = db.conn.execute(
        "SELECT estimated_cost_usd FROM session_usage WHERE session_id = 's_agg'"
    ).fetchone()
    expected = (300_000 * 2.5 + 1_000 * 15.0) / 1_000_000
    assert abs(row["estimated_cost_usd"] - expected) < 0.001
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


def test_session_usage_splits_cross_day_range_queries():
    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 4, 24, 12, 0, 0)

    db = _make_db()
    db.upsert_session(
        "cross", "codex",
        project_path="/tmp/project",
        model="gpt-5.4",
        started_at="2026-04-23T23:50:00+08:00",
        ended_at="2026-04-24T00:10:00+08:00",
        input_tokens=300,
        output_tokens=30,
        cache_read_tokens=100,
        cache_creation_tokens=10,
        message_count=5,
        user_turns=2,
        first_prompt="cross day prompt",
    )
    db.replace_session_usage(
        "cross",
        "codex",
        [
            {
                "usage_date": "2026-04-23",
                "usage_hour": 23,
                "project_path": "/tmp/project",
                "model": "gpt-5.4",
                "message_count": 2,
                "user_turns": 1,
                "input_tokens": 100,
                "output_tokens": 10,
                "cache_read_tokens": 20,
                "cache_creation_tokens": 0,
            },
            {
                "usage_date": "2026-04-24",
                "usage_hour": 0,
                "project_path": "/tmp/project",
                "model": "gpt-5.4",
                "message_count": 3,
                "user_turns": 1,
                "input_tokens": 200,
                "output_tokens": 20,
                "cache_read_tokens": 80,
                "cache_creation_tokens": 10,
            },
        ],
    )
    db.commit()

    full = get_range_totals(db, "2026-04-23", "2026-04-24")
    assert full["session_count"] == 1
    assert full["input_tokens"] == 300
    assert full["output_tokens"] == 30

    today = get_range_totals(db, "2026-04-24", "2026-04-24")
    assert today["session_count"] == 1
    assert today["message_count"] == 3
    assert today["user_turns"] == 1
    assert today["input_tokens"] == 200
    assert today["output_tokens"] == 20
    assert today["cache_read_tokens"] == 80
    assert today["cache_creation_tokens"] == 10

    daily = get_range_daily(db, "2026-04-23", "2026-04-24")
    assert [(r["date"], r["input_tokens"]) for r in daily] == [
        ("2026-04-23", 100),
        ("2026-04-24", 200),
    ]

    model_rows = get_range_by_agent_model(db, "2026-04-24", "2026-04-24")
    assert len(model_rows) == 1
    assert model_rows[0]["agent_type"] == "codex"
    assert model_rows[0]["model"] == "gpt-5.4"
    assert model_rows[0]["input_tokens"] == 200

    time_rows = get_range_by_time_model(db, "2026-04-23", "2026-04-24", limit=2)
    assert time_rows[0]["usage_date"] == "2026-04-24"
    assert time_rows[0]["usage_hour"] == 0
    assert time_rows[0]["input_tokens"] == 200

    top_sessions = get_range_top_sessions(db, "2026-04-24", "2026-04-24", limit=1)
    assert len(top_sessions) == 1
    assert top_sessions[0]["session_id"] == "cross"
    assert top_sessions[0]["input_tokens"] == 200
    assert top_sessions[0]["first_prompt"] == "cross day prompt"

    with patch("agentic_metric.store.aggregator.datetime", FakeDateTime):
        overview = get_today_overview(db)
        today_sessions = get_today_sessions(db)

    assert overview.session_count == 1
    assert overview.input_tokens == 200
    assert overview.output_tokens == 20
    assert len(today_sessions) == 1
    assert today_sessions[0]["session_id"] == "cross"
    assert today_sessions[0]["started_at"].startswith("2026-04-23")
    assert today_sessions[0]["input_tokens"] == 200
    db.close()


def test_top_sessions_omits_synthetic_model_markers():
    db = _make_db()
    db.upsert_session(
        "s1", "claude_code",
        project_path="/tmp/project",
        model="<synthetic>",
        started_at="2026-04-24T10:00:00Z",
        first_prompt="hello",
    )
    db.replace_session_usage(
        "s1",
        "claude_code",
        [
            {
                "usage_date": "2026-04-24",
                "usage_hour": 10,
                "project_path": "/tmp/project",
                "model": "claude-opus-4-7",
                "message_count": 2,
                "user_turns": 1,
                "input_tokens": 1_000,
                "output_tokens": 100,
            },
            {
                "usage_date": "2026-04-24",
                "usage_hour": 10,
                "project_path": "/tmp/project",
                "model": "<synthetic>",
                "message_count": 1,
                "user_turns": 0,
                "input_tokens": 0,
                "output_tokens": 0,
            },
        ],
    )
    db.commit()

    rows = get_range_top_sessions(db, "2026-04-24", "2026-04-24", limit=1)
    assert len(rows) == 1
    assert rows[0]["session_id"] == "s1"
    assert rows[0]["model"] == "claude-opus-4-7"
    assert rows[0]["models"] == "claude-opus-4-7"
    db.close()


def test_today_sessions_prefer_real_usage_model_over_synthetic_session_model():
    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 4, 24, 12, 0, 0)

    db = _make_db()
    db.upsert_session(
        "s1", "claude_code",
        project_path="/tmp/project",
        model="<synthetic>",
        started_at="2026-04-24T10:00:00Z",
    )
    db.replace_session_usage(
        "s1",
        "claude_code",
        [
            {
                "usage_date": "2026-04-24",
                "usage_hour": 10,
                "project_path": "/tmp/project",
                "model": "claude-opus-4-7",
                "message_count": 1,
                "input_tokens": 1_000,
                "estimated_cost_usd": 1.0,
            },
        ],
    )
    db.commit()

    with patch("agentic_metric.store.aggregator.datetime", FakeDateTime):
        rows = get_today_sessions(db)

    assert rows[0]["model"] == "claude-opus-4-7"
    db.close()


def test_tui_breakdown_keeps_unknown_visible_before_model_limit():
    widget = Breakdown()
    widget._total_cost = 10.0
    widget._groups = [
        {
            "agent": "codex",
            "cost": 10.0,
            "unknown_cost_count": 1,
            "input": 0,
            "output": 0,
            "cache": 0,
            "models": [
                {"model": "known-1", "cost": 4.0, "input": 1, "output": 0, "cache": 0},
                {"model": "known-2", "cost": 3.0, "input": 1, "output": 0, "cache": 0},
                {"model": "known-3", "cost": 2.0, "input": 1, "output": 0, "cache": 0},
                {"model": "known-4", "cost": 1.0, "input": 1, "output": 0, "cache": 0},
                {
                    "model": "Unknown",
                    "cost": 0.0,
                    "unknown_cost_count": 1,
                    "input": 1,
                    "output": 0,
                    "cache": 0,
                },
            ],
        }
    ]

    rendered = widget.render().plain
    assert "Unknown" in rendered
    assert "?" in rendered


def test_cost_format_shows_known_amount_plus_unknown_marker():
    assert cli_fmt_cost(12.34, unknown=True) == "$12.34 + ?"
    assert tui_fmt_cost(12.34, unknown=True) == "$12.34 + ?"
    assert cli_fmt_cost(0.1234, unknown=True) == "$0.123 + ?"
    assert tui_fmt_cost(0.1234, unknown=True) == "$0.123 + ?"
    assert cli_fmt_cost(0.0, unknown=True) == "?"
    assert tui_fmt_cost(0.0, unknown=True) == "?"
    assert cli_fmt_cost(None, unknown=True) == "?"
    assert tui_fmt_cost(None, unknown=True) == "?"


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
