import json

import pytest

from agentic_metric.accounts import account_report
from agentic_metric.collectors import create_default_registry
from agentic_metric.collectors.claude_code import ClaudeCodeCollector, _subscriber_account_key
from agentic_metric.collectors.codex import CodexCollector
from agentic_metric.config import RemoteCollectorRoot, RemoteSpec
from agentic_metric.collectors.remote import RemoteHistoryCollector, RemoteSyncTarget
from agentic_metric.store.aggregator import get_range_totals
from agentic_metric.store.database import Database

DAY = "2026-10-10"
CLAUDE_A = "claude:" + "a" * 64
CLAUDE_B = "claude:" + "b" * 64
CODEX_A = "codex:" + "a" * 64


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr("agentic_metric.store.database.DATA_DIR", tmp_path)
    monkeypatch.setattr("agentic_metric.pricing.PRICING_FILE", tmp_path / "prices.json")
    monkeypatch.setattr("agentic_metric.pricing.PRICING_CATALOG_FILE", tmp_path / "catalog.json")
    database = Database(str(tmp_path / "data.db"))
    yield database
    database.close()


def _claude(path, account="", identifier="one"):
    usage = {"input_tokens": 100, "output_tokens": 20}
    if account:
        usage["wcc_subscriber_account"] = {
            "provider": account.split(":")[0], "account_fingerprint": account.split(":")[1],
        }
    entry = {"type": "assistant", "timestamp": DAY + "T10:00:00",
             "message": {"id": identifier, "model": "claude-sonnet-4-6", "usage": usage}}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(entry) + "\n")


def _codex(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = [
        {"type": "session_meta", "timestamp": DAY + "T10:00:00", "payload": {
            "id": "native", "cwd": "/fixture", "model_provider": "openai"}},
        {"type": "turn_context", "timestamp": DAY + "T10:00:00", "payload": {"model": "gpt-5.4"}},
        {"type": "event_msg", "timestamp": DAY + "T10:00:01", "payload": {
            "type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 20}}}},
    ]
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))


def test_claude_markers_override_declared_source_and_keep_unknown_history(db, tmp_path):
    root = tmp_path / "projects"
    _claude(root / "project" / "session.jsonl", CLAUDE_A, "one")
    _claude(root / "project" / "session.jsonl", CLAUDE_B, "two")
    _claude(root / "project" / "session.jsonl", "", "three")
    collector = ClaudeCodeCollector(root, data_root="/fixture")
    collector.sync_history(db)
    totals = get_range_totals(db, DAY, DAY)
    assert {row[0] for row in db.conn.execute("SELECT account_key FROM session_usage")} == {CLAUDE_A, CLAUDE_B, ""}
    collector = ClaudeCodeCollector(root, data_root="/fixture", account_key="manual:cc-root")
    collector.sync_history(db)
    collector.sync_history(db)
    assert get_range_totals(db, DAY, DAY) == totals
    report = account_report(db.conn, DAY, DAY)
    assert {item["account_key"]: item["requests"] for item in report["accounts"]} == {
        CLAUDE_A: 1, CLAUDE_B: 1, "manual:cc-root": 1,
    }


def test_declared_account_merges_explicit_and_unmarked_usage_and_checkpoint_append(db, tmp_path):
    root = tmp_path / "projects"
    path = root / "project" / "session.jsonl"
    _claude(path, CLAUDE_A, "one")
    _claude(path, "", "two")
    collector = ClaudeCodeCollector(root, data_root="/fixture", account_key=CLAUDE_A)
    collector.sync_history(db)
    row = db.conn.execute("SELECT * FROM session_usage").fetchone()
    assert row["account_key"] == CLAUDE_A
    assert row["input_tokens"] == 200
    assert row["message_count"] == 2
    _claude(path, "", "three")
    ClaudeCodeCollector(root, data_root="/fixture", account_key=CLAUDE_A).sync_history(db)
    row = db.conn.execute("SELECT * FROM session_usage").fetchone()
    assert row["input_tokens"] == 300
    assert row["message_count"] == 3
    assert db.conn.execute("SELECT COUNT(*) FROM session_usage").fetchone()[0] == 1


def test_source_account_merging_keeps_unknown_cost_and_original_buckets():
    from agentic_metric.accounts import assign_source_account
    rows = [{"usage_date": DAY, "usage_hour": 10, "model": "unknown", "input_tokens": 1,
             "account_key": CLAUDE_A, "estimated_cost_usd": 1.0},
            {"usage_date": DAY, "usage_hour": 10, "model": "unknown", "input_tokens": 2,
             "account_key": "", "estimated_cost_usd": None}]
    merged = assign_source_account(rows, CLAUDE_A)
    assert len(merged) == 1
    assert merged[0]["input_tokens"] == 3
    assert merged[0]["estimated_cost_usd"] is None
    assert rows[1]["account_key"] == ""
    assert rows[0]["input_tokens"] == 1


@pytest.mark.parametrize("provider", [None, [], {}, "other"])
def test_malformed_claude_identity_stays_unknown(provider):
    assert _subscriber_account_key({"wcc_subscriber_account": {
        "provider": provider, "account_fingerprint": "a" * 64,
    }}) == ""


def test_native_codex_source_binding_changes_without_changing_usage(db, tmp_path):
    root = tmp_path / "sessions"
    _codex(root / "rollout-native.jsonl")
    CodexCollector(root, data_root="/fixture").sync_history(db)
    totals = get_range_totals(db, DAY, DAY)
    assert db.conn.execute("SELECT account_key FROM session_usage").fetchone()[0] == ""
    CodexCollector(root, data_root="/fixture", account_key=CODEX_A).sync_history(db)
    assert get_range_totals(db, DAY, DAY) == totals
    assert db.conn.execute("SELECT account_key FROM session_usage").fetchone()[0] == CODEX_A
    CodexCollector(root, data_root="/fixture", account_key="manual:second").sync_history(db)
    assert get_range_totals(db, DAY, DAY) == totals
    assert db.conn.execute("SELECT account_key FROM session_usage").fetchone()[0] == "manual:second"


def test_configured_compatible_roots_keep_separate_account_usage(db, tmp_path, monkeypatch):
    claude_home, codex_home = tmp_path / "cc-like", tmp_path / "codex-like"
    _claude(claude_home / "projects" / "project" / "session.jsonl")
    _codex(codex_home / "sessions" / "rollout-native.jsonl")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"collectors": {
        "claude_code": {"roots": [{"path": str(claude_home), "account_key": "manual:cc"}]},
        "codex": {"roots": [{"path": str(codex_home), "account_key": CODEX_A}]},
    }}))
    monkeypatch.setattr("agentic_metric.config.CONFIG_FILE", config)
    registry = create_default_registry()
    registry.sync_all(db)
    assert registry.get_sync_errors() == []
    report = account_report(db.conn, DAY, DAY)
    assert {item["account_key"]: item["input_tokens"] for item in report["accounts"]} == {
        "manual:cc": 100, CODEX_A: 100,
    }


def test_claude_and_codex_accounts_are_visible_in_tui(db, tmp_path, monkeypatch):
    import asyncio
    from textual.widgets import Static
    from agentic_metric.accounts import set_account_profile
    from agentic_metric.collectors import CollectorRegistry
    from agentic_metric.tui.app import AgenticMetricApp
    from agentic_metric.tui.accounts_screen import AccountsScreen

    root = tmp_path / "projects"
    _claude(root / "project" / "session.jsonl", CLAUDE_A, "cc")
    _claude(root / "project" / "session.jsonl", CODEX_A, "codex")
    _claude(root / "project" / "session.jsonl", "", "unknown")
    ClaudeCodeCollector(root, data_root="/fixture").sync_history(db)
    set_account_profile(db.conn, CLAUDE_A, "Personal CC", "")
    set_account_profile(db.conn, CODEX_A, "Work Codex", "")
    monkeypatch.setattr("agentic_metric.tui.app.resolve_range", lambda *_args, **_kwargs: ("Fixture", DAY, DAY))

    async def exercise():
        metric = AgenticMetricApp(db=db, collectors=CollectorRegistry(), sync_on_mount=False)
        async with metric.run_test(size=(120, 40)) as pilot:
            await pilot.press("a")
            assert isinstance(metric.screen, AccountsScreen)
            content = str(metric.screen.query_one("#accounts-content", Static).render())
            assert "Personal CC" in content
            assert "Work Codex" in content
            assert "Unknown account" in content
            await pilot.press("escape")
            assert not isinstance(metric.screen, AccountsScreen)
            await pilot.press("right", "pageup", "period")
            await pilot.press("q")

    asyncio.run(exercise())


def test_remote_account_binding_does_not_duplicate_cache(tmp_path, monkeypatch):
    monkeypatch.setattr("agentic_metric.collectors.remote.DATA_DIR", tmp_path)
    from agentic_metric.collectors.remote import _cache_root_for
    remote = RemoteSpec(host="fixture", collectors={"claude_code": [RemoteCollectorRoot("~/.claude")]})
    one = RemoteSyncTarget(remote, "claude_code", "~/.claude", "", 0, CLAUDE_A)
    two = RemoteSyncTarget(remote, "claude_code", "~/.claude", "", 0, CLAUDE_B)
    first, second = RemoteHistoryCollector(one), RemoteHistoryCollector(two)
    assert _cache_root_for(one) == _cache_root_for(two)
    assert first._inner.account_key == CLAUDE_A
    assert second._inner.account_key == CLAUDE_B
    assert first._ready_state_key() != second._ready_state_key()
