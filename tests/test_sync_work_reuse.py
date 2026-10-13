import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from agentic_metric.cli import app
from agentic_metric.collectors import remote
from agentic_metric.config import RemoteSpec
from agentic_metric.store.database import Database
from agentic_metric.tui.app import AgenticMetricApp


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr("agentic_metric.store.database.DATA_DIR", tmp_path)
    monkeypatch.setattr("agentic_metric.collectors.remote.DATA_DIR", tmp_path)
    monkeypatch.setattr("agentic_metric.pricing.PRICING_FILE", tmp_path / "prices.json")
    monkeypatch.setattr("agentic_metric.pricing.PRICING_CATALOG_FILE", tmp_path / "catalog.json")
    database = Database(str(tmp_path / "data.db"))
    yield database
    database.close()


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_complete_remote_manifest_supplies_inventory_without_walking_cache(db, tmp_path, agent):
    target = remote.RemoteSyncTarget(RemoteSpec(host="fixture"), agent, "~/.agent", "", 0)
    root = remote._cache_root_for(target)
    rel = "project/session.jsonl" if agent == "claude_code" else "date/rollout-one.jsonl"
    path = root / target.source_child / rel
    path.parent.mkdir(parents=True)
    if agent == "claude_code":
        entries = [{"type": "assistant", "timestamp": "2026-10-10T10:00:00", "message": {
            "id": "request", "model": "claude-sonnet-4-6", "usage": {"input_tokens": 100, "output_tokens": 1}}}]
    else:
        entries = [{"type": "session_meta", "payload": {"id": "one"}},
                   {"type": "event_msg", "timestamp": "2026-10-10T10:00:00", "payload": {
                       "type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 1}}}}]
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    manifest = {rel: {"size": str(path.stat().st_size), "mtime": "1"}}
    remote._save_manifest(root, manifest)
    collector = remote.RemoteHistoryCollector(target)
    collector._prepared = (True, True, remote._manifest_digest(manifest), True)
    with patch.object(Path, "rglob", side_effect=AssertionError("redundant cache walk")):
        collector.sync_history(db)
    assert db.conn.execute("SELECT SUM(input_tokens) FROM session_usage").fetchone()[0] == 100


def test_remote_retries_incomplete_parser_inventory_with_unchanged_manifest(db, monkeypatch):
    from agentic_metric.collectors import codex
    target = remote.RemoteSyncTarget(RemoteSpec(host="fixture"), "codex", "~/.codex", "", 0)
    root = remote._cache_root_for(target)
    path = root / "sessions" / "rollout-one.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "one"}}) + "\n")
    manifest = {path.name: {"size": str(path.stat().st_size), "mtime": "1"}}
    remote._save_manifest(root, manifest)
    digest = remote._manifest_digest(manifest)
    collector = remote.RemoteHistoryCollector(target)
    signature = codex._stat_signature(path)
    collector._prepared = (True, True, digest, True)
    with patch.object(codex, "_stat_signature", side_effect=[signature, signature, None]):
        collector.sync_history(db)
    assert not collector._inner.last_sync_complete
    assert db.get_sync_state(collector._ready_state_key()) is None
    collector._prepared = (True, False, digest, True)
    collector.sync_history(db)
    assert collector._inner.last_sync_complete
    assert db.get_sync_state(collector._ready_state_key()) == digest
    collector._prepared = (True, False, digest, True)
    with patch.object(collector._inner, "sync_history", side_effect=AssertionError("redundant parse")):
        collector.sync_history(db)


def test_stale_archive_uses_manifest_difference_when_previous_is_complete(tmp_path):
    target = remote.RemoteSyncTarget(RemoteSpec(host="fixture"), "codex", "~/.codex", "", 0)
    active = tmp_path / "sessions"
    active.mkdir()
    removed, kept = active / "rollout-removed.jsonl", active / "rollout-kept.jsonl"
    removed.write_text("removed\n")
    kept.write_text("kept\n")
    previous = {removed.name: {}, kept.name: {}}
    with patch.object(Path, "rglob", side_effect=AssertionError("redundant archive walk")):
        remote._archive_stale_cache_files(tmp_path, target, {kept.name: {}}, previous=previous)
    assert kept.read_text() == "kept\n"
    assert not removed.exists()
    assert (tmp_path / ".stale" / "sessions" / removed.name).read_text() == "removed\n"


def test_missing_previous_manifest_still_discovers_stray_files(tmp_path):
    target = remote.RemoteSyncTarget(RemoteSpec(host="fixture"), "codex", "~/.codex", "", 0)
    stray = tmp_path / "sessions" / "rollout-stray.jsonl"
    stray.parent.mkdir()
    stray.write_text("stray\n")
    remote._archive_stale_cache_files(tmp_path, target, {})
    assert not stray.exists()
    assert (tmp_path / ".stale" / "sessions" / stray.name).exists()


def test_full_json_report_shares_one_fallback_probe(db, monkeypatch):
    db.upsert_session("one", "codex", data_root="fixture", model="gpt-5.4")
    db.replace_session_usage("one", "codex", [{"usage_date": "2026-10-10", "model": "gpt-5.4", "input_tokens": 100}], data_root="fixture")
    db.commit()
    db.pricing_changed = False
    monkeypatch.setattr("agentic_metric.store.database.Database", lambda: db)
    monkeypatch.setattr("agentic_metric.pricing.refresh_pricing_catalog_if_stale", lambda: False)
    statements = []
    db.conn.set_trace_callback(statements.append)
    result = CliRunner().invoke(app, ["report", "--range", "2026-10-10:2026-10-10", "--no-sync", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["totals"]["input_tokens"] == 100
    assert sum("FROM sessions AS s" in sql and "LIMIT 1" in sql for sql in statements) == 1


def test_fresh_catalog_skips_helper_but_forced_update_does_not(db, monkeypatch):
    metric = AgenticMetricApp(db=db, sync_on_mount=False)
    monkeypatch.setattr("agentic_metric.tui.app.pricing_catalog_refresh_due", lambda: False)
    with patch("agentic_metric.tui.app._run_command", side_effect=AssertionError("unnecessary child")):
        assert metric._refresh_catalog_in_worker(force=False) == 0
    monkeypatch.setattr("agentic_metric.tui.app.pricing_catalog_refresh_due", lambda: True)
    with patch("agentic_metric.tui.app.get_current_worker", side_effect=AssertionError("worker requested")):
        with pytest.raises(AssertionError, match="worker requested"):
            metric._refresh_catalog_in_worker(force=False)
        monkeypatch.setattr("agentic_metric.tui.app.pricing_catalog_refresh_due", lambda: False)
        with pytest.raises(AssertionError, match="worker requested"):
            metric._refresh_catalog_in_worker(force=True)
