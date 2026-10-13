"""Request-local account attribution, migration, and aggregate regressions."""

import json
import sqlite3

import pytest

from agentic_metric.collectors import claude_code
from agentic_metric.collectors.claude_code import ClaudeCodeCollector, _SessionAccum
from agentic_metric.collectors.codex import _SessionAccum as CodexSessionAccum
from agentic_metric.pricing import estimate_cost
from agentic_metric.store.aggregator import get_range_by_model, get_range_totals
from agentic_metric.store.database import Database


ACCOUNT_A = "codex:" + "a" * 64
ACCOUNT_B = "codex:" + "b" * 64
MODEL = "codex/gpt-5.4"
DAY = "2026-10-10"


@pytest.fixture
def db(tmp_path, monkeypatch):
    # Never consult the user's pricing or create application data outside tmp_path.
    monkeypatch.setattr("agentic_metric.store.database.DATA_DIR", tmp_path)
    monkeypatch.setattr("agentic_metric.pricing.PRICING_FILE", tmp_path / "pricing.json")
    monkeypatch.setattr("agentic_metric.pricing.PRICING_CATALOG_FILE", tmp_path / "catalog.json")
    monkeypatch.setattr("agentic_metric.pricing._user_cache", None)
    monkeypatch.setattr("agentic_metric.pricing._user_cache_mtime", -1.0)
    database = Database(str(tmp_path / "history.db"))
    yield database
    database.close()


def _assistant(msg_id="request", account_key="", *, tokens=100, timestamp=None):
    usage = {
        "input_tokens": tokens,
        "output_tokens": 20,
        "cache_read_input_tokens": 30,
        "cache_creation_input_tokens": 10,
        "cache_creation": {"ephemeral_1h_input_tokens": 5},
    }
    if account_key:
        usage["wcc_subscriber_account"] = {
            "provider": "codex",
            "account_fingerprint": account_key.split(":", 1)[1],
        }
    return {
        "type": "assistant",
        "timestamp": timestamp or f"{DAY}T10:00:00",
        "message": {"id": msg_id, "model": MODEL, "usage": usage},
    }


def _write_lines(path, entries):
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))


def _bucket(account_key="", tokens=100):
    return {
        "usage_date": DAY,
        "usage_hour": 10,
        "model": MODEL,
        "account_key": account_key,
        "input_tokens": tokens,
        "message_count": 1,
    }


def test_two_accounts_share_hour_and_model_without_changing_totals(db, tmp_path):
    projects = tmp_path / "projects"
    project = projects / "project"
    project.mkdir(parents=True)
    first = _assistant("first", ACCOUNT_A)
    second = _assistant("second", ACCOUNT_B, tokens=200)
    _write_lines(project / "session.jsonl", [
        first,
        {"type": "user", "timestamp": f"{DAY}T10:00:01", "message": {"content": "next"}},
        second,
        first,
        second,
    ])
    collector = ClaudeCodeCollector(projects, provider="wcc", data_root="/source/wcc")
    collector.sync_history(db)
    collector.sync_history(db)

    rows = {row["account_key"]: dict(row) for row in db.conn.execute("SELECT * FROM session_usage")}
    assert set(rows) == {"", ACCOUNT_A, ACCOUNT_B}
    assert rows[""]["user_turns"] == rows[""]["message_count"] == 1
    assert rows[""]["input_tokens"] == 0
    for key, tokens in ((ACCOUNT_A, 100), (ACCOUNT_B, 200)):
        assert rows[key]["model"] == MODEL
        assert rows[key]["usage_hour"] == 10
        assert rows[key]["message_count"] == 1
        assert rows[key]["user_turns"] == 0
        assert rows[key]["input_tokens"] == tokens
        assert rows[key]["output_tokens"] == 20
        assert rows[key]["cache_read_tokens"] == 30
        assert rows[key]["cache_creation_tokens"] == 10
        assert rows[key]["cache_creation_1h_tokens"] == 5
        assert rows[key]["estimated_cost_usd"] == pytest.approx(estimate_cost(
            MODEL, input_tokens=tokens, output_tokens=20, cache_read_tokens=30,
            cache_creation_tokens=10, cache_creation_1h_tokens=5,
        ))
    totals = get_range_totals(db, DAY, DAY)
    session = db.conn.execute("SELECT * FROM sessions").fetchone()
    assert totals["session_count"] == 1
    for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
                  "message_count", "user_turns", "estimated_cost_usd"):
        assert totals[field] == pytest.approx(session[field])
    by_model = get_range_by_model(db, DAY, DAY)
    assert len(by_model) == 1
    assert by_model[0]["session_count"] == 1
    assert by_model[0]["input_tokens"] == 300


@pytest.mark.parametrize("old_account,new_account", [
    ("", ACCOUNT_A), (ACCOUNT_A, ACCOUNT_B), (ACCOUNT_A, ""),
])
@pytest.mark.parametrize("next_day", [False, True])
def test_usage_snapshot_moves_count_tokens_and_cost(tmp_path, db, old_account, new_account, next_day):
    path = tmp_path / "session.jsonl"
    _write_lines(path, [_assistant(account_key=old_account)])
    accum = _SessionAccum(path, "/project")
    accum.read_new_lines()
    timestamp = "2026-10-11T00:00:00" if next_day else f"{DAY}T10:00:02"
    final = _assistant(account_key=new_account, tokens=250, timestamp=timestamp)
    with path.open("a") as handle:
        handle.write(json.dumps(final) + "\n" + json.dumps(final) + "\n")
    accum.read_new_lines()
    rows = accum.usage_bucket_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["account_key"] == new_account
    assert row["usage_date"] == timestamp[:10]
    assert row["message_count"] == accum.message_count == 1
    assert row["input_tokens"] == accum.input_tokens == 250
    assert row["output_tokens"] == accum.output_tokens == 20
    assert row["cache_read_tokens"] == accum.cache_read == 30
    assert row["cache_creation_tokens"] == accum.cache_create == 10
    assert row["cache_creation_1h_tokens"] == accum.cache_create_1h == 5
    assert row["estimated_cost_usd"] == pytest.approx(estimate_cost(
        MODEL, input_tokens=250, output_tokens=20, cache_read_tokens=30,
        cache_creation_tokens=10, cache_creation_1h_tokens=5,
    ))


@pytest.mark.parametrize("usage", [None, {}, [], {"speed": "fast"}])
def test_content_only_stream_update_preserves_last_usage_account(tmp_path, db, usage):
    path = tmp_path / "session.jsonl"
    update = _assistant(account_key=ACCOUNT_B)
    update["message"]["usage"] = usage
    _write_lines(path, [_assistant(account_key=ACCOUNT_A), update])
    accum = _SessionAccum(path, "/project")
    accum.read_new_lines()

    rows = accum.usage_bucket_rows()
    assert len(rows) == 1
    assert rows[0]["account_key"] == ACCOUNT_A
    assert rows[0]["message_count"] == 1
    assert rows[0]["input_tokens"] == 100
    db.replace_session_usage("session", "claude_code", rows)
    report = db.conn.execute("SELECT account_key, SUM(message_count - user_turns) FROM session_usage GROUP BY account_key").fetchall()
    assert [tuple(row) for row in report] == [(ACCOUNT_A, 1)]


@pytest.mark.parametrize("marker", [
    None, "codex:" + "a" * 64, [], {},
    {"provider": "cursor", "account_fingerprint": "a" * 64},
    {"provider": "Codex", "account_fingerprint": "a" * 64},
    {"provider": "codex"},
    {"provider": "codex", "account_fingerprint": None},
    {"provider": "codex", "account_fingerprint": 123},
    {"provider": "codex", "account_fingerprint": "a" * 63},
    {"provider": "codex", "account_fingerprint": "a" * 65},
    {"provider": "codex", "account_fingerprint": "g" * 64},
    {"provider": "codex", "account_fingerprint": "a" * 64 + "\n"},
])
def test_malformed_marker_stays_unknown(tmp_path, db, marker):
    event = _assistant()
    event["message"]["usage"]["wcc_subscriber_account"] = marker
    path = tmp_path / "session.jsonl"
    _write_lines(path, [event])
    accum = _SessionAccum(path, "/project")
    accum.read_new_lines()
    assert accum.usage_bucket_rows()[0]["account_key"] == ""
    assert accum.input_tokens == 100


def test_only_usage_marker_counts_and_hex_is_canonical(tmp_path, db):
    misplaced = _assistant("misplaced")
    marker = {"provider": "codex", "account_fingerprint": "A" * 64}
    misplaced["wcc_subscriber_account"] = marker
    misplaced["message"]["wcc_subscriber_account"] = marker
    explicit = _assistant("explicit")
    explicit["message"]["usage"]["wcc_subscriber_account"] = marker
    path = tmp_path / "session.jsonl"
    _write_lines(path, [misplaced, explicit])
    accum = _SessionAccum(path, "/project")
    accum.read_new_lines()
    assert {row["account_key"]: row["input_tokens"] for row in accum.usage_bucket_rows()} == {
        "": 100, ACCOUNT_A: 100,
    }


def test_replacing_account_buckets_preserves_other_scopes(db):
    scopes = [("claude_code", "wcc", "/one"), ("claude_code", "other", "/one"),
              ("claude_code", "wcc", "/two"), ("codex", "wcc", "/one")]
    for agent, provider, root in scopes:
        db.upsert_session("same", agent, provider=provider, data_root=root, input_tokens=300)
        db.replace_session_usage("same", agent, [_bucket(ACCOUNT_A), _bucket(ACCOUNT_B, 200)],
                                 provider=provider, data_root=root)
    db.replace_session_usage("same", "claude_code", [_bucket(ACCOUNT_B, 500)],
                             provider="wcc", data_root="/one")
    rows = db.conn.execute("SELECT * FROM session_usage").fetchall()
    assert len(rows) == 7
    assert sum(row["input_tokens"] for row in rows) == 1400
    db.delete_session("same", "claude_code", provider="wcc", data_root="/one")
    assert db.conn.execute("SELECT COUNT(*) FROM session_usage").fetchone()[0] == 6
    assert db.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 3


def test_repricing_uses_account_in_update_key(db):
    db.upsert_session("same", "claude_code", data_root="/one")
    db.replace_session_usage("same", "claude_code", [
        _bucket(ACCOUNT_A, 100), _bucket(ACCOUNT_B, 200),
        {**_bucket("", 300), "estimated_cost_usd": 9.0},
    ], data_root="/one")
    db.set_sync_state("pricing:fingerprint", "stale")
    db.ensure_pricing_current()
    rows = {row["account_key"]: row["estimated_cost_usd"]
            for row in db.conn.execute("SELECT * FROM session_usage")}
    assert rows == {ACCOUNT_A: estimate_cost(MODEL, input_tokens=100),
                    ACCOUNT_B: estimate_cost(MODEL, input_tokens=200), "": 9.0}
    assert db.conn.execute("SELECT estimated_cost_usd FROM sessions").fetchone()[0] == sum(rows.values())


def test_account_schema_migration_preserves_history_and_manual_tables(db, tmp_path):
    db.upsert_session("legacy", "claude_code", provider="wcc", data_root="/offline",
                      input_tokens=123, message_count=1, estimated_cost_usd=7.5)
    db.commit()
    # Reproduce the immediately previous schema, with no account dimension.
    db.conn.executescript("""
        DROP TABLE session_usage;
        CREATE TABLE session_usage (
            session_id TEXT NOT NULL, agent_type TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT '', data_root TEXT NOT NULL DEFAULT '',
            usage_date TEXT NOT NULL, usage_hour INTEGER NOT NULL,
            project_path TEXT DEFAULT '', model TEXT DEFAULT '',
            message_count INTEGER DEFAULT 0, user_turns INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0, cache_creation_tokens INTEGER DEFAULT 0,
            cache_creation_1h_tokens INTEGER DEFAULT 0, estimated_cost_usd REAL DEFAULT 0,
            cost_is_explicit INTEGER DEFAULT 0,
            PRIMARY KEY (session_id, agent_type, provider, data_root, usage_date, usage_hour, model)
        );
        INSERT INTO session_usage
            (session_id, agent_type, provider, data_root, usage_date, usage_hour,
             model, input_tokens, message_count, estimated_cost_usd, cost_is_explicit)
        VALUES ('legacy', 'claude_code', 'wcc', '/offline', '2026-10-10', 10,
                'codex/gpt-5.4', 123, 1, 7.5, 1);
        CREATE TABLE manual_payments (id INTEGER PRIMARY KEY, amount REAL);
        INSERT INTO manual_payments VALUES (1, 42.5);
    """)
    db.set_sync_state("cc_jsonl:v13:offline:file", "1:1")
    before = dict(db.conn.execute("SELECT * FROM sessions").fetchone())
    migrated = Database(str(tmp_path / "history.db"))
    try:
        assert dict(migrated.conn.execute("SELECT * FROM sessions").fetchone()) == before
        row = migrated.conn.execute("SELECT * FROM session_usage").fetchone()
        assert row["account_key"] == ""
        assert row["input_tokens"] == 123
        assert row["estimated_cost_usd"] == 7.5
        assert migrated.conn.execute("SELECT amount FROM manual_payments").fetchone()[0] == 42.5
        assert migrated.get_sync_state("cc_jsonl:v13:offline:file") == "1:1"
        assert migrated.get_sync_state("history_identity:version") == "provider-data-root-v4"
        info = migrated.conn.execute("PRAGMA table_info(session_usage)").fetchall()
        account = next(column for column in info if column["name"] == "account_key")
        assert account["notnull"] == 1
        assert account["dflt_value"] == "''"
        assert [col["name"] for col in sorted(info, key=lambda col: col["pk"]) if col["pk"]] == [
            "session_id", "agent_type", "provider", "data_root", "usage_date", "usage_hour", "model", "account_key",
        ]
        with pytest.raises(sqlite3.IntegrityError):
            migrated.conn.execute("UPDATE session_usage SET account_key = NULL")
        migrated._migrate()
        assert migrated.conn.execute("SELECT COUNT(*) FROM session_usage").fetchone()[0] == 1
        assert migrated.conn.execute("SELECT amount FROM manual_payments").fetchone()[0] == 42.5
    finally:
        migrated.close()


def test_parser_version_reparses_enriched_unchanged_logs_without_clearing_history(db, tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    project = projects / "project"
    project.mkdir(parents=True)
    _write_lines(project / "session.jsonl", [_assistant(account_key=ACCOUNT_A)])
    collector = ClaudeCodeCollector(projects, provider="wcc", data_root="/source")
    with monkeypatch.context() as old_parser:
        old_parser.setattr(claude_code, "CLAUDE_SYNC_VERSION", "v13")
        old_parser.setattr(claude_code, "_subscriber_account_key", lambda usage: "")
        collector.sync_history(db)
    assert db.conn.execute("SELECT account_key FROM session_usage").fetchone()[0] == ""
    db.replace_session_usage("offline", "claude_code", [_bucket(tokens=999)],
                             provider="wcc", data_root="/offline")
    collector.sync_history(db)
    rows = db.conn.execute("SELECT * FROM session_usage ORDER BY data_root").fetchall()
    assert [(row["data_root"], row["account_key"], row["input_tokens"]) for row in rows] == [
        ("/offline", "", 999), ("/source", ACCOUNT_A, 100),
    ]


def test_pricing_fingerprint_reparse_recovers_account_attribution(db, tmp_path):
    projects = tmp_path / "projects"
    project = projects / "project"
    project.mkdir(parents=True)
    _write_lines(project / "session.jsonl", [_assistant(account_key=ACCOUNT_A)])
    collector = ClaudeCodeCollector(projects, provider="wcc", data_root="/source")
    collector.sync_history(db)
    db.conn.execute("UPDATE session_usage SET account_key = ''")
    db.set_sync_state("pricing:fingerprint", "old")
    db.ensure_pricing_current()
    collector.sync_history(db)
    row = db.conn.execute("SELECT * FROM session_usage").fetchone()
    assert row["account_key"] == ACCOUNT_A
    assert row["input_tokens"] == 100
    assert row["message_count"] == 1


def test_native_codex_remains_unknown(db, tmp_path):
    accum = CodexSessionAccum(tmp_path / "native.jsonl", project_path="/project")
    accum.model = "gpt-5.4"
    accum._process_event_msg({
        "type": "token_count",
        "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 20}},
    }, ts=f"{DAY}T10:00:00")
    db.replace_session_usage("native", "codex", accum.usage_bucket_rows(),
                             provider="openai", data_root="/native")
    row = db.conn.execute("SELECT * FROM session_usage").fetchone()
    assert row["account_key"] == ""
    assert row["input_tokens"] == 100
