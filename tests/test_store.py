"""Tests for store module."""

import json
import sqlite3
import tempfile
import asyncio
import threading
import pytest
from typer.testing import CliRunner
from datetime import datetime, timedelta
from unittest.mock import patch

from rich.console import Console, Group
from textual.geometry import Size

from agentic_metric import cli as cli_module
from agentic_metric.formatting import cache_hit_rate, cache_hit_rate_band, token_summary
from agentic_metric.store import aggregator as aggregator_module
from agentic_metric.store.database import Database
from agentic_metric.store.aggregator import (
    get_heatmap,
    get_range_by_project,
    get_range_by_project_agent,
    get_range_by_agent_type,
    get_range_by_agent_model,
    get_range_by_host,
    get_range_by_model,
    get_range_by_provider,
    get_range_daily,
    get_range_totals,
    get_today_sessions,
    get_trend,
    get_unpriced_models,
)
from agentic_metric.formatting import fmt_cost as cli_fmt_cost
from agentic_metric.tui.app import _summary_label
from agentic_metric.tui.widgets import (
    Breakdown,
    PeriodicHeatmap,
    SummaryCell,
    TrendBlocks,
    _cache_pct_style as tui_cache_pct_style,
    _render_histogram,
    fmt_cost as tui_fmt_cost,
)
from agentic_metric.tui.help_screen import HelpScreen, _SECTIONS
from agentic_metric.cli import app as cli_app


def _make_db() -> Database:
    """Create a temporary file-based database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp = f.name
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


def test_usage_source_skips_session_fallback_when_every_session_has_buckets():
    db = _make_db()
    db.upsert_session(
        "s1",
        "codex",
        provider="openai",
        data_root="/tmp/.codex",
        started_at="2026-07-13T00:00:00Z",
    )

    assert "UNION ALL" in aggregator_module._usage_source(db)

    db.replace_session_usage(
        "s1",
        "codex",
        [{"usage_date": "2026-07-13", "usage_hour": 8, "model": "gpt-5.4"}],
        provider="openai",
        data_root="/tmp/.codex",
    )
    db.commit()

    assert aggregator_module._usage_source(db) == "session_usage"
    db.close()


def test_database_identity_migration_clears_legacy_history(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """CREATE TABLE sessions (
               session_id TEXT NOT NULL,
               agent_type TEXT NOT NULL,
               project_path TEXT,
               git_branch TEXT DEFAULT '',
               model TEXT DEFAULT '',
               message_count INTEGER DEFAULT 0,
               user_turns INTEGER DEFAULT 0,
               input_tokens INTEGER DEFAULT 0,
               output_tokens INTEGER DEFAULT 0,
               cache_read_tokens INTEGER DEFAULT 0,
               cache_creation_tokens INTEGER DEFAULT 0,
               estimated_cost_usd REAL DEFAULT 0,
               started_at TEXT,
               ended_at TEXT,
               first_prompt TEXT DEFAULT '',
               summary TEXT DEFAULT '',
               PRIMARY KEY (session_id, agent_type)
           );
           CREATE TABLE session_usage (
               session_id TEXT NOT NULL,
               agent_type TEXT NOT NULL,
               usage_date TEXT NOT NULL,
               usage_hour INTEGER NOT NULL,
               project_path TEXT DEFAULT '',
               model TEXT DEFAULT '',
               message_count INTEGER DEFAULT 0,
               user_turns INTEGER DEFAULT 0,
               input_tokens INTEGER DEFAULT 0,
               output_tokens INTEGER DEFAULT 0,
               cache_read_tokens INTEGER DEFAULT 0,
               cache_creation_tokens INTEGER DEFAULT 0,
               estimated_cost_usd REAL DEFAULT 0,
               PRIMARY KEY (session_id, agent_type, usage_date, usage_hour, model)
           );
           INSERT INTO sessions (session_id, agent_type, project_path)
           VALUES ('s1', 'codex:openai', '/tmp/project');
           INSERT INTO session_usage
               (session_id, agent_type, usage_date, usage_hour, model, input_tokens)
           VALUES ('s1', 'codex:openai', '2026-04-24', 10, 'gpt-5.4', 1000);
           CREATE TABLE sessions_old (dummy TEXT);
           CREATE TABLE session_usage_old (dummy TEXT);
        """
    )
    conn.close()

    db = Database(db_path=str(db_path))
    session_cols = {
        row["name"] for row in db.conn.execute("PRAGMA table_info(sessions)")
    }
    usage_cols = {
        row["name"] for row in db.conn.execute("PRAGMA table_info(session_usage)")
    }
    assert {"provider", "data_root"}.issubset(session_cols)
    assert {"provider", "data_root"}.issubset(usage_cols)
    assert db.conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] == 0
    assert db.conn.execute("SELECT COUNT(*) AS n FROM session_usage").fetchone()["n"] == 0
    assert db.get_sync_state("history_identity:version") == "provider-data-root-v4"
    db.close()


def test_database_identity_migration_reclears_v2_rows(tmp_path):
    db_path = tmp_path / "v1.db"
    db = Database(db_path=str(db_path))
    db.close()

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """INSERT INTO sessions
               (session_id, agent_type, provider, data_root, project_path, model, started_at)
           VALUES ('s1', 'codex', '', '', '/tmp/project', 'gpt-5.5', '2026-05-04T10:00:00Z');
           INSERT INTO session_usage
               (session_id, agent_type, provider, data_root, usage_date, usage_hour, model, input_tokens)
           VALUES ('s1', 'codex', '', '', '2026-05-04', 10, 'gpt-5.5', 1000);
           INSERT INTO sync_state (key, value)
           VALUES ('history_identity:version', 'provider-data-root-v2')
           ON CONFLICT(key) DO UPDATE SET value = excluded.value;
           INSERT INTO sync_state (key, value)
           VALUES ('codex_jsonl:v6:/tmp/session.jsonl', '1:1');
        """
    )
    conn.close()

    db = Database(db_path=str(db_path))
    assert db.conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] == 0
    assert db.conn.execute("SELECT COUNT(*) AS n FROM session_usage").fetchone()["n"] == 0
    assert db.get_sync_state("codex_jsonl:v6:/tmp/session.jsonl") is None
    assert db.get_sync_state("history_identity:version") == "provider-data-root-v4"
    db.close()


def test_database_purges_unscoped_rows_even_when_identity_current(tmp_path):
    db_path = tmp_path / "current.db"
    db = Database(db_path=str(db_path))
    db.close()

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """INSERT INTO sessions
               (session_id, agent_type, provider, data_root, project_path, model, started_at)
           VALUES ('legacy', 'codex', '', '', '/tmp/project', 'gpt-5.5', '2026-05-04T10:00:00Z'),
                  ('scoped', 'codex', 'openai', '/tmp/.codex', '/tmp/project', 'gpt-5.5', '2026-05-04T10:00:00Z');
           INSERT INTO session_usage
               (session_id, agent_type, provider, data_root, usage_date, usage_hour, model, input_tokens)
           VALUES ('legacy', 'codex', '', '', '2026-05-04', 10, 'gpt-5.5', 1000),
                  ('scoped', 'codex', 'openai', '/tmp/.codex', '2026-05-04', 10, 'gpt-5.5', 2000);
           INSERT INTO sync_state (key, value)
           VALUES ('cc_jsonl:v5:/tmp/session.jsonl', '1:1');
        """
    )
    conn.close()

    db = Database(db_path=str(db_path))
    rows = db.conn.execute(
        "SELECT session_id, data_root FROM sessions ORDER BY session_id"
    ).fetchall()
    usage_rows = db.conn.execute(
        "SELECT session_id, data_root FROM session_usage ORDER BY session_id"
    ).fetchall()
    assert [(row["session_id"], row["data_root"]) for row in rows] == [
        ("scoped", "/tmp/.codex")
    ]
    assert [(row["session_id"], row["data_root"]) for row in usage_rows] == [
        ("scoped", "/tmp/.codex")
    ]
    assert db.get_sync_state("cc_jsonl:v5:/tmp/session.jsonl") is None
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


def test_upsert_session_is_scoped_by_data_root():
    db = _make_db()
    db.upsert_session(
        "s1",
        "codex",
        provider="openai",
        data_root="/tmp/codex-openai",
        input_tokens=1000,
    )
    db.upsert_session(
        "s1",
        "codex",
        provider="custom",
        data_root="/tmp/codex-custom",
        input_tokens=2000,
    )
    db.commit()

    rows = db.conn.execute(
        """SELECT provider, data_root, input_tokens
           FROM sessions
           WHERE session_id = 's1' AND agent_type = 'codex'
           ORDER BY data_root"""
    ).fetchall()
    assert len(rows) == 2
    assert {row["provider"] for row in rows} == {"openai", "custom"}
    assert {row["input_tokens"] for row in rows} == {1000, 2000}
    db.close()


def test_upsert_session_is_scoped_by_provider_when_data_root_matches():
    db = _make_db()
    db.upsert_session(
        "s1",
        "codex",
        provider="openai",
        data_root="/tmp/codex",
        input_tokens=1000,
    )
    db.upsert_session(
        "s1",
        "codex",
        provider="ichat",
        data_root="/tmp/codex",
        input_tokens=2000,
    )
    db.commit()

    rows = db.conn.execute(
        """SELECT provider, data_root, input_tokens
           FROM sessions
           WHERE session_id = 's1' AND agent_type = 'codex'
           ORDER BY provider"""
    ).fetchall()
    assert [(r["provider"], r["data_root"], r["input_tokens"]) for r in rows] == [
        ("ichat", "/tmp/codex", 2000),
        ("openai", "/tmp/codex", 1000),
    ]
    db.close()


def test_replace_session_usage_is_scoped_by_provider_when_identity_matches():
    db = _make_db()
    bucket = {
        "usage_date": "2026-04-23",
        "usage_hour": 10,
        "model": "gpt-5.5",
        "input_tokens": 1000,
    }
    db.replace_session_usage(
        "s1",
        "codex",
        [bucket],
        provider="openai",
        data_root="/tmp/codex",
    )
    db.replace_session_usage(
        "s1",
        "codex",
        [{**bucket, "input_tokens": 2000}],
        provider="ichat",
        data_root="/tmp/codex",
    )
    db.replace_session_usage(
        "s1",
        "codex",
        [{**bucket, "input_tokens": 1500}],
        provider="openai",
        data_root="/tmp/codex",
    )
    db.commit()

    rows = db.conn.execute(
        """SELECT provider, data_root, input_tokens
           FROM session_usage
           WHERE session_id = 's1' AND agent_type = 'codex'
           ORDER BY provider"""
    ).fetchall()
    assert [(r["provider"], r["data_root"], r["input_tokens"]) for r in rows] == [
        ("ichat", "/tmp/codex", 2000),
        ("openai", "/tmp/codex", 1500),
    ]
    db.close()


def test_delete_session_can_be_scoped_by_provider():
    db = _make_db()
    db.upsert_session(
        "s1",
        "codex",
        provider="openai",
        data_root="/tmp/codex",
        input_tokens=1000,
    )
    db.upsert_session(
        "s2",
        "codex",
        provider="custom",
        data_root="/tmp/codex",
        input_tokens=2000,
    )
    db.replace_session_usage(
        "s1",
        "codex",
        [{"usage_date": "2026-04-23", "usage_hour": 10, "model": "gpt-5.5", "input_tokens": 1000}],
        provider="openai",
        data_root="/tmp/codex",
    )
    db.replace_session_usage(
        "s2",
        "codex",
        [{"usage_date": "2026-04-23", "usage_hour": 10, "model": "gpt-5.5", "input_tokens": 2000}],
        provider="custom",
        data_root="/tmp/codex",
    )
    db.commit()

    db.delete_session("s1", "codex", provider="custom", data_root="/tmp/codex")
    assert db.conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE session_id = 's1'"
    ).fetchone()["n"] == 1

    db.delete_session("s1", "codex", provider="openai", data_root="/tmp/codex")
    assert db.conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE session_id = 's1'"
    ).fetchone()["n"] == 0
    assert db.conn.execute(
        "SELECT COUNT(*) AS n FROM session_usage WHERE session_id = 's1'"
    ).fetchone()["n"] == 0
    assert db.conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE session_id = 's2'"
    ).fetchone()["n"] == 1
    db.close()


def test_delete_session_requires_provider():
    db = _make_db()
    with pytest.raises(TypeError):
        db.delete_session("s1", "codex", data_root="/tmp/codex")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        db.delete_session(
            "s1",
            "codex",
            provider=None,  # type: ignore[arg-type]
            data_root="/tmp/codex",
        )
    db.close()


def test_database_reprices_sessions_when_pricing_changes(tmp_path):
    pricing_file = tmp_path / "pricing.json"
    db_path = str(tmp_path / "data.db")
    pricing_file.write_text(json.dumps({
        "models": {"custom-model": [1.0, 2.0, 0.0, 0.0]},
    }))

    with patch("agentic_metric.pricing.PRICING_FILE", pricing_file):
        db = Database(db_path=db_path)
        db.upsert_session(
            "s1",
            "claude_code",
            data_root="/tmp/.claude",
            model="custom-model",
            input_tokens=1_000_000,
        )
        db.commit()
        db.close()

        pricing_file.write_text(json.dumps({
            "models": {"custom-model": [3.0, 2.0, 0.0, 0.0]},
        }))
        db = Database(db_path=db_path)
        assert db.pricing_changed is True
        row = db.conn.execute(
            """SELECT estimated_cost_usd
               FROM sessions
               WHERE session_id = 's1'
                 AND agent_type = 'claude_code'
                 AND data_root = '/tmp/.claude'"""
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
            data_root="/tmp/.codex",
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
            data_root="/tmp/.codex",
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
            """SELECT estimated_cost_usd
               FROM sessions
               WHERE session_id = 's1'
                 AND agent_type = 'codex'
                 AND data_root = '/tmp/.codex'"""
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
    db.close()


def test_range_reports_split_provider_and_data_root():
    db = _make_db()
    for provider, data_root, input_tokens in (
        ("openai", "/tmp/codex-openai", 1_000),
        ("custom", "/tmp/codex-custom", 2_000),
    ):
        db.replace_session_usage(
            "same-session",
            "codex",
            [
                {
                    "usage_date": "2026-04-24",
                    "usage_hour": 10,
                    "model": "gpt-5.4",
                    "input_tokens": input_tokens,
                },
            ],
            provider=provider,
            data_root=data_root,
        )
    db.commit()

    totals = get_range_totals(db, "2026-04-24", "2026-04-24")
    assert totals["session_count"] == 2

    rows = get_range_by_agent_model(db, "2026-04-24", "2026-04-24")
    assert {
        (row["agent_type"], row["provider"], row["data_root"], row["input_tokens"])
        for row in rows
    } == {
        ("codex", "openai", "/tmp/codex-openai", 1_000),
        ("codex", "custom", "/tmp/codex-custom", 2_000),
    }
    db.close()


def test_range_dimension_breakdowns_group_without_model_session_overcount():
    db = _make_db()
    db.replace_session_usage(
        "local-multi-model",
        "codex",
        [
            {
                "usage_date": "2026-04-24",
                "usage_hour": 10,
                "model": "gpt-5.4",
                "message_count": 2,
                "user_turns": 1,
                "input_tokens": 1_000,
            },
            {
                "usage_date": "2026-04-24",
                "usage_hour": 11,
                "model": "gpt-5.5",
                "message_count": 2,
                "user_turns": 1,
                "input_tokens": 2_000,
            },
        ],
        provider="openai",
        data_root="/tmp/codex-openai",
    )
    db.replace_session_usage(
        "local-other-root",
        "claude_code",
        [
            {
                "usage_date": "2026-04-24",
                "usage_hour": 12,
                "model": "claude-sonnet-4-6",
                "message_count": 2,
                "user_turns": 1,
                "input_tokens": 3_000,
            },
        ],
        provider="anthropic",
        data_root="/tmp/claude",
    )
    db.replace_session_usage(
        "remote-session",
        "codex",
        [
            {
                "usage_date": "2026-04-24",
                "usage_hour": 13,
                "model": "gpt-5.4",
                "message_count": 2,
                "user_turns": 1,
                "input_tokens": 4_000,
            },
        ],
        provider="openai",
        data_root="ssh://remote-a/~/.agent-data",
    )
    db.commit()

    by_host = get_range_by_host(db, "2026-04-24", "2026-04-24")
    assert {
        (row["host"], row["session_count"], row["input_tokens"])
        for row in by_host
    } == {
        ("local", 2, 6_000),
        ("remote-a", 1, 4_000),
    }

    by_agent = get_range_by_agent_type(db, "2026-04-24", "2026-04-24")
    assert {
        (row["agent_type"], row["session_count"], row["input_tokens"])
        for row in by_agent
    } == {
        ("codex", 2, 7_000),
        ("claude_code", 1, 3_000),
    }

    by_provider = get_range_by_provider(db, "2026-04-24", "2026-04-24")
    assert {
        (row["provider"], row["session_count"], row["input_tokens"])
        for row in by_provider
    } == {
        ("openai", 2, 7_000),
        ("anthropic", 1, 3_000),
    }

    by_model = get_range_by_model(db, "2026-04-24", "2026-04-24")
    assert {
        (row["model"], row["session_count"], row["input_tokens"])
        for row in by_model
    } == {
        ("gpt-5.4", 2, 5_000),
        ("gpt-5.5", 1, 2_000),
        ("claude-sonnet-4-6", 1, 3_000),
    }
    assert sum(row["session_count"] for row in by_model) == 4
    assert sum(row["session_count"] for row in by_agent) == 3
    db.close()


def test_top_projects_split_same_path_by_data_root():
    db = _make_db()
    for session_id, data_root, input_tokens in (
        ("local-sid", "/tmp/codex", 1_000),
        ("remote-sid", "ssh://dev/~/.codex", 2_000),
    ):
        db.replace_session_usage(
            session_id,
            "codex",
            [
                {
                    "usage_date": "2026-04-24",
                    "usage_hour": 10,
                    "project_path": "/work/project",
                    "model": "gpt-5.4",
                    "input_tokens": input_tokens,
                },
            ],
            provider="openai",
            data_root=data_root,
        )
    db.commit()

    rows = get_range_by_project(db, "2026-04-24", "2026-04-24", limit=10)
    assert {
        (row["data_root"], row["project_path"], row["input_tokens"])
        for row in rows
    } == {
        ("/tmp/codex", "/work/project", 1_000),
        ("ssh://dev/~/.codex", "/work/project", 2_000),
    }
    db.close()


def test_top_projects_merge_same_path_across_local_roots():
    db = _make_db()
    for session_id, data_root, input_tokens in (
        ("root-a-sid", "/tmp/root-a", 1_000),
        ("root-b-sid", "/tmp/root-b", 2_000),
    ):
        db.replace_session_usage(
            session_id,
            "codex",
            [
                {
                    "usage_date": "2026-04-24",
                    "usage_hour": 10,
                    "project_path": "/work/project",
                    "model": "gpt-5.4",
                    "input_tokens": input_tokens,
                },
            ],
            provider="openai",
            data_root=data_root,
        )
    db.commit()

    rows = get_range_by_project(db, "2026-04-24", "2026-04-24", limit=10)
    # Two local roots sharing a project path collapse into one row whose
    # totals combine both roots' sessions.
    assert len(rows) == 1
    row = rows[0]
    assert row["project_path"] == "/work/project"
    assert row["input_tokens"] == 3_000
    assert row["session_count"] == 2
    db.close()


def test_project_agent_breakdown_merges_local_roots_but_splits_agents():
    db = _make_db()
    rows = [
        ("codex-a", "codex", "/tmp/root-a", "/work/a", 1_000),
        ("codex-b", "codex", "/tmp/root-b", "/work/a", 2_000),
        ("claude-a", "claude_code", "/tmp/root-c", "/work/a", 3_000),
        ("remote-a", "codex", "ssh://remote-a/~/.agent-data", "/work/a", 4_000),
    ]
    for session_id, agent_type, data_root, project_path, input_tokens in rows:
        db.replace_session_usage(
            session_id,
            agent_type,
            [
                {
                    "usage_date": "2026-04-24",
                    "usage_hour": 10,
                    "project_path": project_path,
                    "model": "gpt-5.4",
                    "message_count": 2,
                    "user_turns": 1,
                    "input_tokens": input_tokens,
                },
            ],
            provider="openai",
            data_root=data_root,
        )
    db.commit()

    result = get_range_by_project_agent(db, "2026-04-24", "2026-04-24", limit=10)
    assert {
        (
            row["data_root"],
            row["project_path"],
            row["agent_type"],
            row["session_count"],
            row["input_tokens"],
        )
        for row in result
    } == {
        ("/tmp/root-a", "/work/a", "codex", 2, 3_000),
        ("/tmp/root-c", "/work/a", "claude_code", 1, 3_000),
        ("ssh://remote-a/~/.agent-data", "/work/a", "codex", 1, 4_000),
    }
    db.close()


def test_top_projects_limit_applies_after_merge():
    db = _make_db()
    # Project A is split across two local roots; each split alone would rank
    # below B and C, but the merged total must outrank them and survive limit=2.
    splits = [
        ("a1", "/tmp/root-a", "/work/a", 3_000),
        ("a2", "/tmp/root-b", "/work/a", 3_000),
        ("b1", "/tmp/root-b", "/work/b", 4_000),
        ("c1", "/tmp/root-b", "/work/c", 3_500),
    ]
    for session_id, data_root, project_path, input_tokens in splits:
        db.replace_session_usage(
            session_id,
            "codex",
            [
                {
                    "usage_date": "2026-04-24",
                    "usage_hour": 10,
                    "project_path": project_path,
                    "model": "gpt-5.4",
                    "input_tokens": input_tokens,
                },
            ],
            provider="openai",
            data_root=data_root,
        )
    db.commit()

    rows = get_range_by_project(db, "2026-04-24", "2026-04-24", limit=2)
    paths = [r["project_path"] for r in rows]
    # Merged A (6_000) ranks first; without merge-before-limit each A split
    # (3_000) would fall below B (4_000) and C (3_500) and drop out entirely.
    assert paths == ["/work/a", "/work/b"]
    assert "/work/c" not in paths
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

    db.close()


def test_replace_session_usage_prices_cache_creation_1h_tokens():
    db = _make_db()
    db.replace_session_usage(
        "s1",
        "claude_code",
        [
            {
                "usage_date": "2026-04-24",
                "usage_hour": 10,
                "model": "claude-opus-4-8",
                "cache_creation_tokens": 1_000_000,
                "cache_creation_1h_tokens": 1_000_000,
            },
        ],
    )
    db.commit()

    row = db.conn.execute(
        """SELECT cache_creation_tokens, cache_creation_1h_tokens, estimated_cost_usd
           FROM session_usage
           WHERE session_id = 's1' AND agent_type = 'claude_code'"""
    ).fetchone()
    assert row["cache_creation_tokens"] == 1_000_000
    assert row["cache_creation_1h_tokens"] == 1_000_000
    assert row["estimated_cost_usd"] == 10.0

    db.close()


def test_session_fallback_reprices_cache_creation_1h_tokens():
    db = _make_db()
    db.upsert_session(
        "s1",
        "claude_code",
        model="claude-opus-4-8",
        cache_creation_tokens=1_000_000,
        cache_creation_1h_tokens=1_000_000,
        estimated_cost_usd=0.0,
    )
    db.conn.execute("DELETE FROM sync_state WHERE key = 'pricing:fingerprint'")
    db.ensure_pricing_current()

    row = db.conn.execute(
        """SELECT cache_creation_tokens, cache_creation_1h_tokens, estimated_cost_usd
           FROM sessions
           WHERE session_id = 's1' AND agent_type = 'claude_code'"""
    ).fetchone()
    assert row["cache_creation_tokens"] == 1_000_000
    assert row["cache_creation_1h_tokens"] == 1_000_000
    assert row["estimated_cost_usd"] == 10.0

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
            data_root="/tmp/.codex",
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
            data_root="/tmp/.codex",
        )
        db.set_sync_state("codex_jsonl:v5:/tmp/rollout.jsonl", "1:1")
        db.set_sync_state("pricing:fingerprint", "stale")
        db.close()

        db = Database(db_path=db_path)
        row = db.conn.execute(
            """SELECT estimated_cost_usd, cost_is_explicit
               FROM session_usage
               WHERE session_id = 's1'
                 AND agent_type = 'codex'
                 AND data_root = '/tmp/.codex'"""
        ).fetchone()
        session = db.conn.execute(
            """SELECT estimated_cost_usd
               FROM sessions
               WHERE session_id = 's1'
                 AND agent_type = 'codex'
                 AND data_root = '/tmp/.codex'"""
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

        project_rows = get_range_by_project(db, "2026-04-24", "2026-04-24", limit=1)
        assert project_rows[0]["unknown_cost_count"] == 1
        assert get_unpriced_models(db) == ["gpt-5.4-pro"]

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

    with patch("agentic_metric.store.aggregator.datetime", FakeDateTime):
        today_sessions = get_today_sessions(db)

    assert len(today_sessions) == 1
    assert today_sessions[0]["session_id"] == "cross"
    assert today_sessions[0]["started_at"].startswith("2026-04-23")
    assert today_sessions[0]["input_tokens"] == 200
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


def test_tui_breakdown_renders_all_models_for_scrollable_panel():
    widget = Breakdown()
    widget._total_cost = 10.0
    widget._groups = [
        {
            "host": "local",
            "cost": 10.0,
            "unknown_cost_count": 1,
            "input": 0,
            "output": 0,
            "cache": 0,
            "agents": [
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
            ],
        }
    ]

    console = Console(record=True, width=120, color_system=None)
    console.print(widget.render())
    rendered = console.export_text()
    assert "known-4" in rendered
    assert "Unknown" in rendered
    assert "?" not in rendered
    assert "—" in rendered
    assert "more models" not in rendered


def test_tui_breakdown_shows_host_level_only_for_multiple_hosts():
    def _host(name, cost):
        return {
            "host": name,
            "cost": cost,
            "unknown_cost_count": 0,
            "input": 0, "output": 0, "cache": 0,
            "agents": [{
                "agent": "claude-code",
                "cost": cost,
                "unknown_cost_count": 0,
                "input": 0, "output": 0, "cache": 0,
                "providers": [{
                    "provider": "anthropic",
                    "data_root": f"~/{name}",
                    "cost": cost,
                    "unknown_cost_count": 0,
                    "input": 0, "output": 0, "cache": 0,
                    "models": [{"model": "opus", "cost": cost, "input": 1, "output": 0, "cache": 0}],
                }],
            }],
        }

    # Multiple hosts: each host name is rendered as a top-level row.
    multi = Breakdown()
    multi._total_cost = 10.0
    multi._groups = [_host("myserver", 7.0), _host("local", 3.0)]
    console = Console(record=True, width=120, color_system=None)
    console.print(multi.render())
    rendered = console.export_text()
    assert "myserver" in rendered
    assert "local" in rendered
    assert "opus" in rendered

    # Single host: the host level is folded away (agent sits at the top).
    single = Breakdown()
    single._total_cost = 3.0
    single._groups = [_host("local", 3.0)]
    console = Console(record=True, width=120, color_system=None)
    console.print(single.render())
    lines = [ln for ln in console.export_text().splitlines() if ln.strip()]
    # First non-empty row after the header is the agent, not a "local" host row.
    assert not lines[1].startswith("local")
    assert "claude-code" in lines[1]


def test_tui_trend_blocks_render_compact_summary():
    widget = TrendBlocks()
    widget.update_data(
        [("05-23", 1.0), ("05-24", 0.0), ("05-25", 3.0)],
        "last 14 days",
    )

    console = Console(record=True, width=120, color_system=None)
    console.print(widget.render())
    rendered = console.export_text()
    assert "latest 05-25 $3.00" in rendered
    assert "peak 05-25 $3.00" in rendered
    assert "total $4.00" in rendered


def test_tui_trend_blocks_summary_adapts_to_width():
    """Narrow terminals drop peak/total so the summary stays one line.

    The chart panel is a fixed three rows, so a wrapped summary would
    overflow it; only "latest" is guaranteed to render.
    """
    widget = TrendBlocks()
    widget.update_data(
        [("05-23", 1.0), ("05-24", 0.0), ("05-25", 3.0)],
        "last 14 days",
    )

    wide = widget._summary(80).plain
    assert "latest 05-25 $3.00" in wide
    assert "peak 05-25 $3.00" in wide
    assert "total $4.00" in wide

    narrow = widget._summary(24).plain
    assert "latest 05-25 $3.00" in narrow
    assert "peak" not in narrow
    assert "total" not in narrow
    # Single line: no embedded newline regardless of width.
    assert "\n" not in narrow


def test_tui_trend_blocks_renders_provider_totals_line():
    widget = TrendBlocks()
    widget.update_data(
        [("05-23", 1.0), ("05-24", 0.0), ("05-25", 3.0)],
        "last 14 days",
        provider_totals=[
            {"provider": "ichat", "estimated_cost_usd": 3.0, "unknown_cost_count": 0},
            {"provider": "openai", "estimated_cost_usd": 1.0, "unknown_cost_count": 0},
            {"provider": "anthropic", "estimated_cost_usd": 0.0, "unknown_cost_count": 0},
        ],
    )

    console = Console(record=True, width=120, color_system=None)
    console.print(widget.render())
    rendered = console.export_text()
    # No "by provider" caption — the provider names speak for themselves.
    assert "by provider" not in rendered
    assert "ichat $3.00" in rendered
    assert "openai $1.00" in rendered
    # zero-cost provider is filtered out so the line stays informative.
    assert "anthropic" not in rendered


def test_tui_trend_blocks_provider_line_drops_lower_priority_when_narrow():
    widget = TrendBlocks()
    widget.update_data(
        [("05-25", 3.0)],
        "last 14 days",
        provider_totals=[
            {"provider": "ichat", "estimated_cost_usd": 3.0, "unknown_cost_count": 0},
            {"provider": "openai", "estimated_cost_usd": 1.0, "unknown_cost_count": 0},
        ],
    )

    wide = widget._provider_line(80)
    assert wide is not None
    assert "ichat $3.00" in wide.plain
    assert "openai $1.00" in wide.plain

    narrow = widget._provider_line(20)
    assert narrow is not None
    assert "ichat $3.00" in narrow.plain
    assert "openai" not in narrow.plain


def test_tui_trend_blocks_no_provider_line_without_data():
    widget = TrendBlocks()
    widget.update_data([("05-25", 3.0)], "last 14 days")
    assert widget._provider_line(80) is None


def test_tui_heatmap_renders_current_range_provider_rollup():
    widget = PeriodicHeatmap()
    widget.update_data(
        [{"label": "10", "cost": 4.0, "tokens": 420}],
        totals={
            "estimated_cost_usd": 4.0,
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_tokens": 300,
            "cache_creation_tokens": 0,
            "unknown_cost_count": 0,
        },
        providers=[
            {"provider": "ichat", "estimated_cost_usd": 3.0, "unknown_cost_count": 0},
            {"provider": "openai", "estimated_cost_usd": 1.0, "unknown_cost_count": 0},
            {"provider": "anthropic", "estimated_cost_usd": 0.0, "unknown_cost_count": 0},
        ],
        total_cost=4.0,
    )

    console = Console(record=True, width=120, color_system=None)
    console.print(widget.render())
    rendered = console.export_text()
    assert "Providers" in rendered
    assert "ichat · $3.00 (75.0%)" in rendered
    assert "openai · $1.00 (25.0%)" in rendered
    assert "anthropic" not in rendered


def test_week_and_month_trends_use_one_aggregate_query(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 14, 12, 0, 0)

    db = _make_db()
    for usage_date, cost in (
        ("2026-06-01", 1.0),
        ("2026-06-30", 2.0),
        ("2026-07-13", 3.0),
    ):
        db.replace_session_usage(
            usage_date,
            "codex",
            [{
                "usage_date": usage_date,
                "usage_hour": 10,
                "model": "gpt-5.4",
                "estimated_cost_usd": cost,
            }],
        )
    db.commit()
    monkeypatch.setattr(aggregator_module, "datetime", FixedDateTime)

    statements = []
    db.conn.set_trace_callback(statements.append)
    week = get_trend(db, "week", 3)
    week_selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
    statements.clear()
    month = get_trend(db, "month", 3)
    month_selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]

    assert week == [("06-29", 2.0), ("07-06", 0.0), ("07-13", 3.0)]
    assert month == [("26-05", 0.0), ("26-06", 3.0), ("26-07", 3.0)]
    assert len(week_selects) == 2  # usage-source probe + aggregate
    assert len(month_selects) == 2
    db.close()


def test_tui_heatmap_reports_enough_height_for_all_rendered_rows():
    widget = PeriodicHeatmap()
    widget.update_data(
        [{"label": "00", "cost": 4.0, "tokens": 420}],
        totals={
            "estimated_cost_usd": 4.0,
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_tokens": 300,
            "cache_creation_tokens": 0,
            "unknown_cost_count": 0,
        },
        providers=[
            {"provider": "ichat", "estimated_cost_usd": 3.0, "unknown_cost_count": 0},
            {"provider": "openai", "estimated_cost_usd": 1.0, "unknown_cost_count": 0},
        ],
        projects=[
            {
                "project_path": f"/workspace/project-{index}",
                "data_root": "/tmp/.codex",
                "estimated_cost_usd": 1.0,
                "unknown_cost_count": 0,
            }
            for index in range(3)
        ],
        total_cost=4.0,
    )

    assert widget.get_content_height(Size(120, 40), Size(120, 40), 120) >= 18


def test_tui_sync_keeps_cached_data_visible_and_reports_progress(monkeypatch):
    from agentic_metric.tui.app import AgenticMetricApp

    db = _make_db()
    app = AgenticMetricApp(db=db, sync_on_mount=False)
    workers = []
    monkeypatch.setattr(app, "run_worker", lambda worker, **kwargs: workers.append(worker))
    monkeypatch.setattr(app, "_populate_all", lambda: None)

    app._tick_sync()

    assert app.sub_title == "syncing… · showing cached data"
    assert app._sync_in_progress is True
    assert len(workers) == 1

    app._tick_sync()
    assert len(workers) == 1

    app._on_sync_done()
    assert app.sub_title.startswith("synced ")
    assert app._sync_in_progress is False
    db.close()


def test_tui_initial_sync_shows_loading_page_until_dashboard_is_ready(
    monkeypatch, tmp_path
):
    from agentic_metric.store.database import Database as RealDatabase
    from agentic_metric.tui.app import AgenticMetricApp

    started = threading.Event()
    release = threading.Event()

    class BlockingCollectors:
        def sync_all(self, db) -> None:
            started.set()
            assert release.wait(timeout=5)

        def get_sync_errors(self) -> list[str]:
            return []

    monkeypatch.setattr(
        "agentic_metric.tui.app.Database",
        lambda *args, **kwargs: RealDatabase(db_path=str(tmp_path / "sync.db")),
    )

    async def run() -> None:
        db = RealDatabase(db_path=str(tmp_path / "app.db"))
        app = AgenticMetricApp(db=db, collectors=BlockingCollectors())
        async with app.run_test(headless=True, size=(120, 36)) as pilot:
            assert await asyncio.to_thread(started.wait, 1)
            assert app.query_one("#loading-page").display is True
            assert app.query_one("#dashboard").display is False

            release.set()
            for _ in range(20):
                await pilot.pause()
                if not app._sync_in_progress:
                    break

            assert app.query_one("#loading-page").display is False
            assert app.query_one("#dashboard").display is True

    asyncio.run(run())


def test_tui_pricing_warning_is_deduplicated(monkeypatch, tmp_path):
    from agentic_metric.store.database import Database as RealDatabase
    from agentic_metric.tui.app import AgenticMetricApp

    db = RealDatabase(db_path=str(tmp_path / "app.db"))
    db.replace_session_usage(
        "unpriced",
        "codex",
        [{
            "usage_date": "2026-07-14",
            "usage_hour": 10,
            "model": "future-model",
            "input_tokens": 100,
        }],
    )
    db.commit()
    app = AgenticMetricApp(db=db, sync_on_mount=False)
    notices = []
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: notices.append((message, kwargs)))

    app._refresh_pricing_warning()
    app._refresh_pricing_warning()

    assert app._unpriced_models == ["future-model"]
    assert len(notices) == 1
    assert "future-model" in notices[0][0]
    assert notices[0][1]["title"] == "Pricing missing"
    db.close()


def test_tui_initial_sync_failure_reveals_cached_dashboard(monkeypatch, tmp_path):
    from agentic_metric.store.database import Database as RealDatabase
    from agentic_metric.tui.app import AgenticMetricApp

    class FailingCollectors:
        def sync_all(self, db) -> None:
            raise RuntimeError("sync exploded")

        def get_sync_errors(self) -> list[str]:
            return []

    monkeypatch.setattr(
        "agentic_metric.tui.app.Database",
        lambda *args, **kwargs: RealDatabase(db_path=str(tmp_path / "sync.db")),
    )

    async def run() -> None:
        db = RealDatabase(db_path=str(tmp_path / "app.db"))
        app = AgenticMetricApp(db=db, collectors=FailingCollectors())
        async with app.run_test(
            headless=True, size=(120, 36), notifications=False
        ) as pilot:
            for _ in range(20):
                await pilot.pause()
                if not app._sync_in_progress:
                    break

            assert app.query_one("#loading-page").display is False
            assert app.query_one("#dashboard").display is True
            assert app.sub_title == "sync failed · showing cached data"

    asyncio.run(run())


def test_tui_heatmap_provider_rollup_handles_unknown_cost():
    widget = PeriodicHeatmap()
    widget.update_data(
        [{"label": "10", "cost": 3.0, "tokens": 420, "unknown_cost_count": 1}],
        totals={
            "estimated_cost_usd": 3.0,
            "input_tokens": 100,
            "unknown_cost_count": 1,
        },
        providers=[
            {"provider": "ichat", "estimated_cost_usd": 3.0, "unknown_cost_count": 0},
            {"provider": "custom", "estimated_cost_usd": 0.0, "unknown_cost_count": 1},
        ],
        total_cost=3.0,
    )

    console = Console(record=True, width=120, color_system=None)
    console.print(widget.render())
    rendered = console.export_text()
    assert "ichat · $3.00" in rendered
    assert "custom · —" in rendered
    assert "(100.0%)" not in rendered


def test_tui_breakdown_multi_host_tree_structure():
    """Lock the header columns and tree connectors for a multi-host render."""
    def _host(name, cost):
        return {
            "host": name,
            "cost": cost,
            "unknown_cost_count": 0,
            "input": 0, "output": 0, "cache": 0,
            "agents": [{
                "agent": "codex",
                "cost": cost,
                "unknown_cost_count": 0,
                "input": 0, "output": 0, "cache": 0,
                "providers": [{
                    "provider": "openai",
                    "data_root": f"~/{name}",
                    "cost": cost,
                    "unknown_cost_count": 0,
                    "input": 0, "output": 0, "cache": 0,
                    "models": [{"model": "gpt-5.5", "cost": cost, "input": 1, "output": 0, "cache": 0}],
                }],
            }],
        }

    widget = Breakdown()
    widget._total_cost = 10.0
    widget._groups = [_host("myserver", 7.0), _host("local", 3.0)]
    console = Console(record=True, width=120, color_system=None)
    console.print(widget.render())
    rendered = console.export_text()

    # Header columns are present and labeled once.
    assert "Cost" in rendered and "Cache%" in rendered
    # Host rows sit at column 0; their agents/providers/models nest with
    # box-drawing connectors below them.
    assert "myserver" in rendered
    assert "├ codex" in rendered or "└ codex" in rendered
    assert "openai" in rendered
    assert "gpt-5.5" in rendered


def test_help_screen_renders_grouped_sections():
    section_titles = [name for name, _keys in _SECTIONS]
    assert section_titles == ["Navigation", "Data", "Other"]

    console = Console(record=True, width=80, color_system=None)
    console.print(HelpScreen()._build_content())
    rendered = console.export_text()
    for title in section_titles:
        assert title in rendered
    # The removed copy feature now points users at the CLI.
    assert "use the CLI" in rendered
    # No stale references to deleted keys.
    assert "Sync now" not in rendered
    assert "Ctrl+B" not in rendered
    assert "Ctrl+F" not in rendered
    assert "t / w / m" not in rendered
    assert "h l" not in rendered
    assert "k j" not in rendered
    assert "r" in rendered
    assert "fast auto-refresh" in rendered


def test_report_renders_tables_sequentially_and_highlights_cache_pct(monkeypatch):
    console = Console(record=True, width=220, color_system=None)
    monkeypatch.setattr(cli_module, "console", console)

    totals = {
        "estimated_cost_usd": 12.0,
        "session_count": 2,
        "message_count": 10,
        "user_turns": 4,
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 300,
        "cache_creation_tokens": 0,
    }
    by_agent = [{
        "agent_type": "codex",
        "provider": "openai",
        "data_root": "/tmp/codex",
        "session_count": 2,
        "user_turns": 4,
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 300,
        "cache_creation_tokens": 0,
        "estimated_cost_usd": 12.0,
    }]
    by_host = [{
        "host": "local",
        "session_count": 2,
        "message_count": 10,
        "user_turns": 4,
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 300,
        "cache_creation_tokens": 0,
        "estimated_cost_usd": 12.0,
    }]
    by_agent_type = [{
        "agent_type": "codex",
        "session_count": 2,
        "message_count": 10,
        "user_turns": 4,
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 300,
        "cache_creation_tokens": 0,
        "estimated_cost_usd": 12.0,
    }]
    by_provider = [{
        "provider": "openai",
        "session_count": 2,
        "message_count": 10,
        "user_turns": 4,
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 300,
        "cache_creation_tokens": 0,
        "estimated_cost_usd": 12.0,
    }]
    by_model = [{
        "model": "gpt-5.5",
        "raw_model": "gpt-5.5",
        "session_count": 2,
        "message_count": 10,
        "user_turns": 4,
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 300,
        "cache_creation_tokens": 0,
        "estimated_cost_usd": 12.0,
    }]
    by_agent_model = [{
        **by_agent[0],
        "model": "gpt-5.5",
        "raw_model": "gpt-5.5",
    }]
    by_project = [{
        "project_path": "/tmp/a-very-long-project-name-that-should-not-force-a-second-report-column",
        "session_count": 2,
        "user_turns": 4,
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 300,
        "cache_creation_tokens": 0,
        "estimated_cost_usd": 12.0,
    }]
    by_project_agent = [{
        **by_project[0],
        "agent_type": "codex",
        "message_count": 10,
    }]
    by_provider_model = [{
        "provider": "openai",
        "model": "gpt-5.5",
        "raw_model": "gpt-5.5",
        "session_count": 2,
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 300,
        "cache_creation_tokens": 0,
        "estimated_cost_usd": 12.0,
    }]
    by_agent_type_model = [{
        **by_provider_model[0],
        "agent_type": "codex",
    }]
    by_project_model = [{
        **by_provider_model[0],
        "project_path": by_project[0]["project_path"],
        "data_root": "",
    }]
    periodic = [{
        "label": "Mon",
        "session_count": 2,
        "tokens": 420,
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 300,
        "cache_creation_tokens": 0,
        "cost": 12.0,
    }]

    cli_module._print_report(
        "This week",
        "2026-05-25",
        "2026-05-26",
        totals,
        by_host,
        by_agent_type,
        by_provider,
        by_model,
        by_agent,
        by_agent_model,
        by_project,
        by_project_agent,
        periodic,
        "week",
        full=True,
        by_provider_model=by_provider_model,
        by_agent_type_model=by_agent_type_model,
        by_project_model=by_project_model,
    )

    rendered = console.export_text()
    assert "Cache %" in rendered
    assert "75%" in rendered
    assert "By host" in rendered
    assert "By agent" in rendered
    assert "By provider" in rendered
    assert "By model" in rendered
    assert "By project × agent" in rendered
    assert "By provider × model" in rendered
    assert "By agent × model" in rendered
    assert "By project × model" in rendered
    assert not any("By provider" in line and "By day" in line for line in rendered.splitlines())


def test_report_header_shows_provider_rollup_in_default_view(monkeypatch):
    console = Console(record=True, width=140, color_system=None)
    monkeypatch.setattr(cli_module, "console", console)

    totals = {
        "estimated_cost_usd": 4.0,
        "session_count": 2,
        "message_count": 10,
        "user_turns": 4,
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 300,
        "cache_creation_tokens": 0,
        "unknown_cost_count": 0,
    }
    by_provider = [
        {"provider": "ichat", "estimated_cost_usd": 3.0, "unknown_cost_count": 0},
        {"provider": "openai", "estimated_cost_usd": 1.0, "unknown_cost_count": 0},
        {"provider": "anthropic", "estimated_cost_usd": 0.0, "unknown_cost_count": 0},
    ]

    cli_module._print_report(
        "Range",
        "2026-05-25",
        "2026-05-26",
        totals,
        [],
        [],
        by_provider,
        [],
        [],
        [],
        [],
        [],
        [],
        None,
        full=False,
    )

    rendered = console.export_text()
    assert "Providers" in rendered
    assert "ichat $3.00 (75.0%)" in rendered
    assert "openai $1.00 (25.0%)" in rendered
    assert "anthropic" not in rendered
    assert "By provider" not in rendered


def test_cli_sync_status_only_animates_on_interactive_output(monkeypatch):
    events = []

    class Status:
        def __enter__(self):
            events.append("enter")

        def __exit__(self, *args):
            events.append("exit")

    class FakeConsole:
        is_terminal = True

        def status(self, message, *, spinner):
            events.append((message, spinner))
            return Status()

    monkeypatch.setattr(cli_module, "console", FakeConsole())
    with cli_module._sync_status("Syncing usage data…"):
        events.append("work")
    assert events == [
        ("Syncing usage data…", "dots"),
        "enter",
        "work",
        "exit",
    ]

    events.clear()
    with cli_module._sync_status("Syncing usage data…", enabled=False):
        events.append("work")
    assert events == ["work"]


def test_sync_command_reports_partial_failure(monkeypatch, tmp_path):
    class FailingRegistry:
        def sync_all(self, db) -> None:
            return None

        def get_sync_errors(self) -> list[str]:
            return ["codex /tmp/root: collector failed"]

        def get_all(self) -> list:
            return []

    from agentic_metric.store.database import Database as RealDatabase

    monkeypatch.setattr(
        "agentic_metric.collectors.create_default_registry",
        lambda: FailingRegistry(),
    )
    monkeypatch.setattr(
        "agentic_metric.store.database.Database",
        lambda: RealDatabase(db_path=str(tmp_path / "sync.db")),
    )

    result = CliRunner().invoke(cli_app, ["sync"])

    assert result.exit_code == 1
    assert "Sync incomplete" in result.output
    assert "collector failed" in result.output
    assert "Sync complete" not in result.output


def test_unknown_models_note_lists_distinct_models():
    note = cli_module._build_unknown_models_note([
        {"model": "Unknown", "raw_model": "gpt-6", "unknown_cost_count": 2},
        {"model": "claude-opus-4-8", "unknown_cost_count": 0},
        {"model": "Unknown", "raw_model": "gpt-6", "unknown_cost_count": 1},
    ])
    assert note is not None
    from rich.console import Console as _C
    import io as _io
    buf = _io.StringIO()
    _C(file=buf, width=100, no_color=True).print(note)
    text = buf.getvalue()
    assert "Pricing missing" in text
    assert "Cost totals exclude unpriced models" in text
    assert text.count("gpt-6") == 2  # warning list + setup command
    assert "pricing set" in text
    # No unknowns → no note
    assert cli_module._build_unknown_models_note([{"model": "gpt-5.5", "unknown_cost_count": 0}]) is None


def test_cli_mixed_pricing_keeps_costs_numeric_and_warns_once():
    rows = [
        {
            "agent_type": "codex",
            "provider": "openai",
            "data_root": "/tmp/codex",
            "model": "gpt-5.4",
            "raw_model": "gpt-5.4",
            "input_tokens": 100,
            "estimated_cost_usd": 5.0,
            "unknown_cost_count": 0,
        },
        {
            "agent_type": "codex",
            "provider": "openai",
            "data_root": "/tmp/codex",
            "model": "Unknown",
            "raw_model": "future-model",
            "input_tokens": 200,
            "estimated_cost_usd": 0.0,
            "unknown_cost_count": 1,
        },
    ]
    table = cli_module._build_by_agent_model_table(rows)
    note = cli_module._build_unknown_models_note(rows)
    from rich.console import Console as _C
    import io as _io
    buf = _io.StringIO()
    _C(file=buf, width=120, no_color=True).print(Group(table, note))
    text = buf.getvalue()

    assert "$5.00" in text
    assert "future-model" in text
    assert "—" in text
    assert "?" not in text
    assert text.count("Pricing missing") == 1


def test_breakdown_table_rolls_up_models_past_limit():
    rows = [
        {"agent_type": "codex", "provider": "openai", "data_root": "~/.codex",
         "model": f"m{i}", "raw_model": f"m{i}", "session_count": 1,
         "input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0,
         "cache_creation_tokens": 0, "estimated_cost_usd": float(10 - i),
         "unknown_cost_count": 0}
        for i in range(5)
    ]
    tbl = cli_module._build_by_agent_model_table(rows, limit=2)
    from rich.console import Console as _C
    import io as _io
    buf = _io.StringIO()
    _C(file=buf, width=120, no_color=True).print(tbl)
    text = buf.getvalue()
    assert "+3 more models" in text


def test_cli_breakdown_table_shows_remote_host():
    rows = [{
        "agent_type": "codex",
        "provider": "openai",
        "data_root": "ssh://dev/~/.codex",
        "model": "gpt-5.4",
        "raw_model": "gpt-5.4",
        "session_count": 1,
        "input_tokens": 1_000,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "estimated_cost_usd": 0.001,
        "unknown_cost_count": 0,
    }]
    tbl = cli_module._build_by_agent_model_table(rows, limit=2)
    from rich.console import Console as _C
    import io as _io
    buf = _io.StringIO()
    _C(file=buf, width=140, no_color=True).print(tbl)
    text = buf.getvalue()
    assert "Source" in text
    assert "dev:~/.codex" in text


def test_cli_top_projects_prefixes_remote_host():
    tbl = cli_module._build_top_projects_table([{
        "data_root": "ssh://dev/~/.codex",
        "project_path": "/work/project",
        "session_count": 1,
        "input_tokens": 1_000,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "estimated_cost_usd": 0.001,
        "unknown_cost_count": 0,
    }])
    from rich.console import Console as _C
    import io as _io
    buf = _io.StringIO()
    _C(file=buf, width=140, no_color=True).print(tbl)
    assert "dev:/work/project" in buf.getvalue()



def test_heatmap_buckets_carry_cache_fields_for_cache_pct():
    db = _make_db()
    today = datetime.now().strftime("%Y-%m-%d")
    db.replace_session_usage(
        "s1",
        "codex",
        [{
            "usage_date": today,
            "usage_hour": 10,
            "project_path": "/tmp/project",
            "model": "gpt-5.5",
            "message_count": 2,
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_tokens": 300,
            "cache_creation_tokens": 0,
            "estimated_cost_usd": 12.0,
        }],
    )
    db.commit()

    buckets = get_heatmap(db, "today")
    bucket = buckets[10]

    assert bucket["input_tokens"] == 100
    assert bucket["cache_read_tokens"] == 300
    assert cache_hit_rate(bucket) == 75.0
    db.close()


def test_month_heatmap_uses_daily_buckets():
    db = _make_db()
    now = datetime.now()
    month_start = datetime(now.year, now.month, 1).date()
    if now.month == 12:
        month_end = datetime(now.year + 1, 1, 1).date() - timedelta(days=1)
    else:
        month_end = datetime(now.year, now.month + 1, 1).date() - timedelta(days=1)
    mid_day = month_start + timedelta(days=14)
    db.replace_session_usage(
        "s1",
        "codex",
        [
            {
                "usage_date": month_start.strftime("%Y-%m-%d"),
                "usage_hour": 10,
                "project_path": "/tmp/project",
                "model": "gpt-5.5",
                "message_count": 2,
                "input_tokens": 100,
                "estimated_cost_usd": 1.0,
            },
            {
                "usage_date": mid_day.strftime("%Y-%m-%d"),
                "usage_hour": 11,
                "project_path": "/tmp/project",
                "model": "gpt-5.5",
                "message_count": 3,
                "input_tokens": 200,
                "estimated_cost_usd": 2.0,
            },
        ],
    )
    db.commit()

    buckets = get_heatmap(db, "month")

    assert len(buckets) == month_end.day
    assert buckets[0]["label"] == "01"
    assert buckets[-1]["label"] == f"{month_end.day:02d}"
    assert buckets[0]["sublabel"] == month_start.strftime("%m-%d")
    assert buckets[0]["cost"] == 1.0
    assert buckets[14]["label"] == "15"
    assert buckets[14]["cost"] == 2.0
    assert buckets[1]["cost"] == 0.0
    db.close()


def test_month_heatmap_handles_leap_february(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2024, 2, 15)

    monkeypatch.setattr(aggregator_module, "datetime", FixedDateTime)
    db = _make_db()

    buckets = get_heatmap(db, "month")

    assert len(buckets) == 29
    assert buckets[0]["label"] == "01"
    assert buckets[-1]["label"] == "29"
    assert buckets[-1]["sublabel"] == "02-29"
    db.close()


def test_cache_pct_threshold_styles_match_tui_and_cli():
    summary = token_summary({
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 300,
        "cache_creation_tokens": 30,
    })
    assert summary["total_tokens"] == 450
    assert summary["cache_pct"] == 300 / 430 * 100

    assert cache_hit_rate({
        "input_tokens": 100,
        "cache_read_tokens": 300,
        "cache_creation_tokens": 0,
    }) == 75.0
    assert cache_hit_rate({
        "input": 100,
        "cache_read": 300,
        "cache_write": 0,
    }) == 75.0

    expected = {
        96: "excellent",
        95: "excellent",
        94: "good",
        90: "good",
        89: "warn",
        80: "warn",
        79: "low",
        -1: "none",
    }
    for pct, band in expected.items():
        assert cache_hit_rate_band(pct) == band
        assert tui_cache_pct_style(pct) == cli_module._cache_pct_style(pct)

    styles = [tui_cache_pct_style(pct) for pct in (95, 94, 89, 79)]
    assert len(set(styles)) == 4


def test_cli_month_periodic_table_uses_daily_label():
    periodic = [{
        "label": "01",
        "sublabel": "02-01",
        "session_count": 1,
        "tokens": 100,
        "input_tokens": 20,
        "output_tokens": 5,
        "cache_read_tokens": 75,
        "cache_creation_tokens": 0,
        "cost": 1.0,
    }]

    table = cli_module._build_periodic_table(periodic, "month")
    console = Console(record=True, width=120, color_system=None)
    console.print(table)
    rendered = console.export_text()

    assert "By day" in rendered
    assert "Day" in rendered
    assert "By week" not in rendered


def test_tui_summary_and_breakdown_render_cache_pct():
    cell = SummaryCell("TODAY")
    cell.update_data(1.0, 2, 420, requests=6, turns=4, cache_pct=75)
    assert "Cache % 75%" in cell.render().plain

    widget = Breakdown()
    widget._total_cost = 12.0
    widget._groups = [{
        "host": "local",
        "cost": 12.0,
        "unknown_cost_count": 0,
        "input": 100,
        "output": 20,
        "cache": 300,
        "cache_read": 300,
        "cache_write": 0,
        "agents": [{
            "agent": "codex",
            "cost": 12.0,
            "unknown_cost_count": 0,
            "input": 100,
            "output": 20,
            "cache": 300,
            "cache_read": 300,
            "cache_write": 0,
            "providers": [],
        }],
    }]

    rendered = widget.render().plain
    # Value columns split tokens into input/output/cache plus cache hit rate.
    assert "Cache read" not in rendered
    assert "Cache%" in rendered
    assert "300" in rendered         # cache-read tokens
    assert "75%" in rendered         # cache hit rate


def test_tui_summary_stats_wrap_on_whole_segments():
    cell = SummaryCell("TODAY")
    cell.update_data(
        1.0,
        195,
        1_200_000_000,
        requests=6_319,
        turns=1_280,
        cache_pct=94,
    )

    lines = ["".join(text for text, _style in row) for row in cell._stats_lines(28)]
    assert lines == [
        "195 sess · 6.3K req",
        "1.3K turns",
    ]


def test_tui_histograms_render_discrete_bars():
    histogram = _render_histogram(
        [4.0, 2.0, 3.0],
        labels=["Mon", "Tue", "Wed"],
        available=80,
        colors=["green"] * 8,
        rows=3,
    )
    console = Console(record=True, width=120, color_system=None)
    console.print(histogram.bars)
    bar_lines = console.export_text().splitlines()
    # Multi-row bar chart: taller value (4.0) fills more rows than shorter (2.0).
    assert len(bar_lines) == 3
    assert "█" in "".join(bar_lines)
    assert any("  " in line.strip() for line in bar_lines)
    assert "Mon" in histogram.axis.plain
    assert "Tue" in histogram.axis.plain
    assert "Wed" in histogram.axis.plain

    heatmap = PeriodicHeatmap()
    heatmap.update_data(
        [
            {"label": "00", "cost": 1.0, "tokens": 100},
            {"label": "12", "cost": 3.0, "tokens": 300},
            {"label": "23", "cost": 2.0, "tokens": 200},
        ],
        totals={
            "input_tokens": 10,
            "output_tokens": 10,
            "cache_read_tokens": 10,
            "cache_creation_tokens": 10,
        },
        total_cost=6.0,
    )
    console = Console(record=True, width=120, color_system=None)
    console.print(heatmap.render())
    heatmap_lines = console.export_text().splitlines()
    assert any("█" in line for line in heatmap_lines)
    assert any("00" in line for line in heatmap_lines)

    trend = TrendBlocks()
    trend.update_data([("06-01", 1.0), ("06-08", 3.0), ("06-14", 2.0)], "last 14 days")
    console = Console(record=True, width=120, color_system=None)
    console.print(trend.render())
    trend_lines = console.export_text().splitlines()
    assert any("█" in line for line in trend_lines)
    assert any("06-01" in line for line in trend_lines)


def test_tui_summary_follows_focused_time_offset(monkeypatch, tmp_path):
    from agentic_metric.tui.app import AgenticMetricApp
    from agentic_metric.store.database import Database as RealDatabase

    monkeypatch.setattr(
        "agentic_metric.tui.app.Database",
        lambda *args, **kwargs: RealDatabase(db_path=str(tmp_path / "data.db")),
    )

    async def run() -> None:
        app = AgenticMetricApp(sync_on_mount=False)
        async with app.run_test(headless=True, size=(120, 36)) as pilot:
            def labels() -> list[str]:
                return [
                    app.query_one(sel, SummaryCell).label
                    for sel in ("#cell-today", "#cell-week", "#cell-month")
                ]

            assert labels() == ["TODAY", "WEEK", "MONTH"]
            breakdown = app.query_one("#breakdown-body", Breakdown)
            breakdown.update_data(
                [{
                    "host": "local",
                    "cost": 100.0,
                    "unknown_cost_count": 0,
                    "input": 100,
                    "output": 100,
                    "cache": 100,
                    "agents": [{
                        "agent": "codex",
                        "cost": 100.0,
                        "unknown_cost_count": 0,
                        "input": 100,
                        "output": 100,
                        "cache": 100,
                        "providers": [{
                            "provider": "openai",
                            "data_root": "/tmp/root",
                            "cost": 100.0,
                            "unknown_cost_count": 0,
                            "input": 100,
                            "output": 100,
                            "cache": 100,
                            "models": [
                                {
                                    "model": f"model-{i:02d}",
                                    "cost": 1.0,
                                    "unknown_cost_count": 0,
                                    "input": 1,
                                    "output": 1,
                                    "cache": 1,
                                }
                                for i in range(40)
                            ],
                        }],
                    }],
                }],
                100.0,
            )
            app.query_one("#breakdown-scroll").refresh(layout=True)
            app.query_one("#breakdown-body").refresh(layout=True)
            await pilot.pause()
            scroller = app.query_one("#breakdown-scroll")
            assert scroller.max_scroll_y > 0
            app.action_scroll_breakdown_down()
            await pilot.pause()
            assert scroller.scroll_y > 0
            app.action_scroll_breakdown_up()

            app.action_focus("week")
            app.action_back_in_time()
            assert labels() == ["TODAY", "LAST WEEK", "MONTH"]

            app.action_forward_in_time()
            assert labels() == ["TODAY", "WEEK", "MONTH"]

            app.action_focus("today")
            app.action_back_in_time()
            assert labels() == ["YESTERDAY", "WEEK", "MONTH"]

            app.action_reset_offset()
            app.action_focus("month")
            app.action_back_in_time()
            assert labels() == ["TODAY", "WEEK", "LAST MONTH"]

    asyncio.run(run())


def test_tui_summary_labels_multi_period_offsets():
    assert _summary_label("today", "Yesterday", 1) == "YESTERDAY"
    assert _summary_label("today", "May 24", 2) == "2 DAYS AGO"
    assert _summary_label("week", "Last week", 1) == "LAST WEEK"
    assert _summary_label("week", "2 weeks ago", 2) == "2 WEEKS AGO"
    assert _summary_label("month", "Last month", 1) == "LAST MONTH"
    assert _summary_label("month", "Mar 2026", 2) == "2 MONTHS AGO"


def test_pricing_set_commands_missing_args_exit_nonzero():
    runner = CliRunner()

    for args in (
        ["pricing", "set"],
        ["pricing", "long-context", "set"],
        ["pricing", "cache", "set"],
    ):
        result = runner.invoke(cli_app, args)
        assert result.exit_code == 1
        assert "Usage:" in result.output


def test_cost_format_keeps_unknown_state_out_of_numeric_values():
    assert cli_fmt_cost(12.34, unknown=True) == "$12.34"
    assert tui_fmt_cost(12.34, unknown=True) == "$12.34"
    assert cli_fmt_cost(0.1234, unknown=True) == "$0.123"
    assert tui_fmt_cost(0.1234, unknown=True) == "$0.123"
    assert cli_fmt_cost(0.0, unknown=True) == "—"
    assert tui_fmt_cost(0.0, unknown=True) == "—"
    assert cli_fmt_cost(None, unknown=True) == "—"
    assert tui_fmt_cost(None, unknown=True) == "—"
