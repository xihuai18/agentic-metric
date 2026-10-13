"""Persistent Codex rollout identity cache."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentic_metric.collectors.codex import CodexCollector
from agentic_metric.store.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=str(tmp_path / "data.db"))
    yield database
    database.close()


def _rollout(session_id, *, parent=None, provider="openai", tokens=100, turns=1, pad=""):
    payload = {"id": session_id, "cwd": "/work/project", "model_provider": provider}
    if parent:
        payload["forked_from_id"] = parent
    lines = [
        {"timestamp": "2026-04-23T10:00:00Z", "type": "session_meta", "payload": payload},
        {"timestamp": "2026-04-23T10:00:00Z", "type": "turn_context",
         "payload": {"model": "gpt-5.5"}},
    ]
    for i in range(turns):
        lines.append({"timestamp": f"2026-04-23T10:00:{i + 1:02d}Z", "type": "event_msg",
                      "payload": {"type": "user_message", "message": f"hello{pad}{i}"}})
    lines.append({"timestamp": "2026-04-23T10:05:00Z", "type": "event_msg",
                  "payload": {"type": "token_count", "info": {
                      "total_token_usage": {"input_tokens": tokens, "output_tokens": 20}}}})
    return "".join(json.dumps(line) + "\n" for line in lines)


def _write(root: Path, name: str, text: str) -> Path:
    path = root / "sessions" / "2026" / "04" / "23" / f"rollout-{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _collector(root: Path, provider="openai") -> CodexCollector:
    return CodexCollector(sessions_dir=root / "sessions", provider=provider,
                          data_root=str(root))


def _dump(db):
    sessions = db.conn.execute(
        "SELECT * FROM sessions WHERE agent_type = 'codex' "
        "ORDER BY session_id, provider, data_root").fetchall()
    usage = db.conn.execute(
        "SELECT * FROM session_usage WHERE agent_type = 'codex' "
        "ORDER BY session_id, provider, data_root, usage_date, usage_hour, model, "
        "account_key").fetchall()
    return [tuple(r) for r in sessions], [tuple(r) for r in usage]


def _fresh_dump(tmp_path, roots):
    fresh = Database(db_path=str(tmp_path / "fresh.db"))
    try:
        for root, provider in roots:
            _collector(root, provider).sync_history(fresh)
        return _dump(fresh)
    finally:
        fresh.close()


def _count_opens(monkeypatch):
    """Record rollout opens from both Path.open (identity) and open() (parse)."""
    import builtins

    opened: list[str] = []
    real_path_open, real_open = Path.open, builtins.open

    def path_spy(self, *args, **kwargs):
        if self.name.startswith("rollout-"):
            opened.append(self.name)
        return real_path_open(self, *args, **kwargs)

    def open_spy(file, *args, **kwargs):
        name = os.path.basename(os.fspath(file)) if isinstance(file, (str, Path)) else ""
        if name.startswith("rollout-"):
            opened.append(name)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(Path, "open", path_spy)
    monkeypatch.setattr(builtins, "open", open_spy)
    return opened


def _identity_keys(db):
    return {r["key"] for r in db.conn.execute(
        "SELECT key FROM sync_state WHERE key LIKE '%:identity:%'")}


def test_unchanged_new_collector_opens_zero_rollouts(db, tmp_path, monkeypatch):
    root = tmp_path / "codex"
    _write(root, "a", _rollout("a"))
    _write(root, "b", _rollout("b", parent="a"))
    _collector(root).sync_history(db)
    db.commit()
    baseline = _dump(db)
    assert len(_identity_keys(db)) == 2

    opened = _count_opens(monkeypatch)
    _collector(root).sync_history(db)
    assert opened == []
    assert _dump(db) == baseline == _fresh_dump(tmp_path, [(root, "openai")])


def test_missing_identity_reparses_once_then_zero_opens(db, tmp_path, monkeypatch):
    root = tmp_path / "codex"
    _write(root, "a", _rollout("a"))
    _collector(root).sync_history(db)
    db.conn.execute("DELETE FROM sync_state WHERE key LIKE '%:identity:%'")
    db.commit()
    baseline = _dump(db)

    opened = _count_opens(monkeypatch)
    _collector(root).sync_history(db)
    assert opened.count("rollout-a.jsonl") == 2  # identity read plus full parse
    assert _dump(db) == baseline
    opened.clear()
    _collector(root).sync_history(db)
    assert opened == []


def test_missing_identity_does_not_trust_parse_state_for_session_swap(db, tmp_path):
    root = tmp_path / "codex"
    path = _write(root, "a", _rollout("a", tokens=111))
    _collector(root).sync_history(db)
    db.conn.execute("DELETE FROM sync_state WHERE key LIKE '%:identity:%'")
    db.commit()
    mtime = path.stat().st_mtime_ns
    swapped = _rollout("a", tokens=222).replace('"a"', '"b"', 1)
    assert len(swapped) == len(path.read_text())
    path.write_text(swapped)
    os.utime(path, ns=(mtime, mtime))  # same size, restored mtime
    _collector(root).sync_history(db)
    assert {r[0] for r in db.conn.execute("SELECT session_id FROM sessions")} == {"b"}
    assert _dump(db) == _fresh_dump(tmp_path, [(root, "openai")])


def test_growth_updates_usage(db, tmp_path):
    root = tmp_path / "codex"
    path = _write(root, "a", _rollout("a", tokens=100))
    _collector(root).sync_history(db)
    path.write_text(_rollout("a", tokens=500, turns=3))
    _collector(root).sync_history(db)
    assert _dump(db) == _fresh_dump(tmp_path, [(root, "openai")])
    assert db.conn.execute("SELECT input_tokens FROM sessions").fetchone()[0] == 500


@pytest.mark.parametrize("damage", ["missing", "corrupt", "bad_version", "bad_types"])
def test_bad_identity_cache_is_rebuilt(db, tmp_path, monkeypatch, damage):
    root = tmp_path / "codex"
    _write(root, "a", _rollout("a"))
    _collector(root).sync_history(db)
    key = next(iter(_identity_keys(db)))
    good = json.loads(db.get_sync_state(key))
    if damage == "missing":
        db.conn.execute("DELETE FROM sync_state WHERE key = ?", (key,))
    else:
        if damage == "corrupt":
            value = "{not json"
        elif damage == "bad_version":
            value = json.dumps({**good, "v": 99})
        else:
            value = json.dumps({**good, "stat": ["x"] * 5, "id": [1, 2, 3]})
        db.set_sync_state(key, value, commit=False)
    db.commit()
    baseline = _dump(db)

    opened = _count_opens(monkeypatch)
    _collector(root).sync_history(db)
    assert opened and set(opened) == {"rollout-a.jsonl"}
    assert _dump(db) == baseline == _fresh_dump(tmp_path, [(root, "openai")])
    rebuilt = json.loads(db.get_sync_state(key))
    assert rebuilt == good
    opened.clear()
    _collector(root).sync_history(db)
    assert opened == []


def test_same_size_rewrite_with_restored_mtime_reparses(db, tmp_path):
    root = tmp_path / "codex"
    path = _write(root, "a", _rollout("a", tokens=111))
    _collector(root).sync_history(db)
    mtime = path.stat().st_mtime_ns
    new_text = _rollout("a", tokens=222)
    assert len(new_text) == len(path.read_text())
    path.write_text(new_text)
    os.utime(path, ns=(mtime, mtime))
    _collector(root).sync_history(db)
    assert db.conn.execute("SELECT input_tokens FROM sessions").fetchone()[0] == 222
    assert _dump(db) == _fresh_dump(tmp_path, [(root, "openai")])


def test_atomic_replacement_with_restored_mtime_reparses(db, tmp_path):
    root = tmp_path / "codex"
    path = _write(root, "a", _rollout("a", tokens=111))
    _collector(root).sync_history(db)
    stat = path.stat()
    replacement = path.with_name("replacement.tmp")
    replacement.write_text(_rollout("a", tokens=222))
    os.utime(replacement, ns=(stat.st_mtime_ns, stat.st_mtime_ns))
    os.replace(replacement, path)
    _collector(root).sync_history(db)
    assert db.conn.execute("SELECT input_tokens FROM sessions").fetchone()[0] == 222
    assert _dump(db) == _fresh_dump(tmp_path, [(root, "openai")])


def test_provider_identity_change_moves_session(db, tmp_path):
    root = tmp_path / "codex"
    path = _write(root, "a", _rollout("a", provider="openai"))
    _collector(root, "").sync_history(db)
    assert db.conn.execute("SELECT provider FROM sessions").fetchone()[0] == "openai"
    path.write_text(_rollout("a", provider="custom"))
    _collector(root, "").sync_history(db)
    assert [r[0] for r in db.conn.execute("SELECT provider FROM sessions")] == ["custom"]
    assert _dump(db) == _fresh_dump(tmp_path, [(root, "")])


def test_parent_dependency_updates_child(db, tmp_path):
    root = tmp_path / "codex"
    parent = _write(root, "p", _rollout("p", tokens=100))
    _write(root, "c", _rollout("c", parent="p", tokens=100))
    _collector(root).sync_history(db)
    parent.write_text(_rollout("p", tokens=300, turns=2))
    _collector(root).sync_history(db)
    assert _dump(db) == _fresh_dump(tmp_path, [(root, "openai")])
    # parent removed: child becomes a root fork with a missing parent
    parent.unlink()
    _collector(root).sync_history(db)
    assert _dump(db) == _fresh_dump(tmp_path, [(root, "openai")])


def test_two_scopes_match_fresh_parse_and_stay_isolated(db, tmp_path, monkeypatch):
    root_a, root_b = tmp_path / "scope-a", tmp_path / "scope-b"
    _write(root_a, "a", _rollout("shared", provider="openai", tokens=100))
    _write(root_b, "b", _rollout("shared", provider="custom", tokens=700))
    scopes = [(root_a, "openai"), (root_b, "custom")]
    for root, provider in scopes:
        _collector(root, provider).sync_history(db)
    opened = _count_opens(monkeypatch)
    for root, provider in scopes:
        _collector(root, provider).sync_history(db)
    assert opened == []
    assert _dump(db) == _fresh_dump(tmp_path, scopes)
    assert len(_identity_keys(db)) == 2


def test_explicit_files_use_supplied_inventory(db, tmp_path):
    root = tmp_path / "codex"
    a = _write(root, "a", _rollout("a"))
    b = _write(root, "b", _rollout("b"))
    _collector(root).sync_history(db, files=[b, a, a])
    assert {r[0] for r in db.conn.execute("SELECT session_id FROM sessions")} == {"a", "b"}
    _collector(root).sync_history(db, files=[a])  # complete list: b is gone
    assert {r[0] for r in db.conn.execute("SELECT session_id FROM sessions")} == {"a"}
    assert all(str(b) not in key for key in _identity_keys(db))


def test_reconcile_drops_stale_identity_and_keeps_active(db, tmp_path):
    root = tmp_path / "codex"
    _write(root, "a", _rollout("a"))
    gone = _write(root, "b", _rollout("b"))
    _collector(root).sync_history(db)
    assert len(_identity_keys(db)) == 2
    gone.unlink()
    _collector(root).sync_history(db)
    keys = _identity_keys(db)
    assert len(keys) == 1 and not any(str(gone) in key for key in keys)


def test_incomplete_inventory_keeps_valid_scope(db, tmp_path):
    root = tmp_path / "codex"
    _write(root, "a", _rollout("a"))
    broken = _write(root, "b", _rollout("b"))
    _collector(root).sync_history(db)
    broken.write_text("not a rollout\n")
    _collector(root).sync_history(db)
    assert {r[0] for r in db.conn.execute("SELECT session_id FROM sessions")} == {"a", "b"}
    broken.write_text(_rollout("b", tokens=900))
    _collector(root).sync_history(db)
    assert _dump(db) == _fresh_dump(tmp_path, [(root, "openai")])


def test_unstable_metadata_is_not_persisted(db, tmp_path, monkeypatch):
    root = tmp_path / "codex"
    path = _write(root, "a", _rollout("a"))
    from agentic_metric.collectors import codex

    real = codex._session_file_identity

    def racing(jsonl_file):
        result = real(jsonl_file)
        with jsonl_file.open("a") as handle:  # file grows during the read
            handle.write("\n")
        return result

    monkeypatch.setattr(codex, "_session_file_identity", racing)
    _collector(root).sync_history(db)
    assert _identity_keys(db) == set()
    monkeypatch.setattr(codex, "_session_file_identity", real)
    _collector(root).sync_history(db)
    assert len(_identity_keys(db)) == 1
    assert _dump(db) == _fresh_dump(tmp_path, [(root, "openai")])
    assert path.exists()


def _ghost(db, root):
    db.upsert_session("ghost", "codex", provider="openai", data_root=str(root),
                      user_turns=1, message_count=1)
    db.commit()


def _ghost_present(db):
    return db.conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE session_id = 'ghost'").fetchone()[0] == 1


def test_rewrite_between_inventory_and_loop_reparses_without_reconcile(db, tmp_path):
    root = tmp_path / "codex"
    path = _write(root, "a", _rollout("a", tokens=111))
    _collector(root).sync_history(db)
    _ghost(db, root)
    mtime = path.stat().st_mtime_ns
    swapped = _rollout("a", tokens=222)
    assert len(swapped) == len(path.read_text())

    collector = _collector(root)
    real = collector._load_identity

    def racing(db_, jsonl_file, key):
        result = real(db_, jsonl_file, key)  # cache hit on the old file
        jsonl_file.write_text(swapped)
        os.utime(jsonl_file, ns=(mtime, mtime))
        return result

    collector._load_identity = racing
    collector.sync_history(db)
    assert db.conn.execute(
        "SELECT input_tokens FROM sessions WHERE session_id = 'a'").fetchone()[0] == 222
    assert _ghost_present(db)  # incomplete inventory must not reconcile-delete

    _collector(root).sync_history(db)  # clean run rebuilds identity and reconciles
    assert not _ghost_present(db)
    assert _dump(db) == _fresh_dump(tmp_path, [(root, "openai")])


def test_unstable_identity_read_does_not_reconcile(db, tmp_path, monkeypatch):
    from agentic_metric.collectors import codex

    root = tmp_path / "codex"
    _write(root, "a", _rollout("a"))
    _collector(root).sync_history(db)
    _ghost(db, root)
    db.conn.execute("DELETE FROM sync_state WHERE key LIKE '%:identity:%'")
    real = codex._session_file_identity

    def racing(jsonl_file):
        result = real(jsonl_file)
        os.utime(jsonl_file, ns=(1, 1))  # metadata changes during the read
        return result

    monkeypatch.setattr(codex, "_session_file_identity", racing)
    _collector(root).sync_history(db)
    assert _ghost_present(db)
    monkeypatch.setattr(codex, "_session_file_identity", real)
    _collector(root).sync_history(db)
    assert not _ghost_present(db)


def test_parent_same_size_restored_mtime_rewrite_reparses_child(db, tmp_path, monkeypatch):
    root = tmp_path / "codex"
    parent = _write(root, "p", _rollout("p", tokens=111))
    _write(root, "c", _rollout("c", parent="p", tokens=111))
    _collector(root).sync_history(db)
    mtime = parent.stat().st_mtime_ns
    rewritten = _rollout("p", tokens=222)
    assert len(rewritten) == len(parent.read_text())
    parent.write_text(rewritten)
    os.utime(parent, ns=(mtime, mtime))

    opened = _count_opens(monkeypatch)
    _collector(root).sync_history(db)
    assert "rollout-c.jsonl" in opened and "rollout-p.jsonl" in opened
    assert _dump(db) == _fresh_dump(tmp_path, [(root, "openai")])


def test_last_sync_complete_reports_stable_inventory(db, tmp_path, monkeypatch):
    from agentic_metric.collectors import codex

    root = tmp_path / "codex"
    _write(root, "a", _rollout("a"))
    collector = _collector(root)
    assert collector.last_sync_complete is False
    collector.sync_history(db)
    assert collector.last_sync_complete is True
    collector.sync_history(db)  # all-cache-hit run stays complete
    assert collector.last_sync_complete is True

    missing = _collector(tmp_path / "absent")
    missing.sync_history(db)
    assert missing.last_sync_complete is False

    vanished = tmp_path / "codex" / "sessions" / "rollout-gone.jsonl"
    collector.sync_history(db, files=[vanished])  # unreadable supplied file
    assert collector.last_sync_complete is False

    db.conn.execute("DELETE FROM sync_state WHERE key LIKE '%:identity:%'")
    real = codex._session_file_identity

    def racing(jsonl_file):
        result = real(jsonl_file)
        os.utime(jsonl_file, ns=(1, 1))  # metadata changes during the read
        return result

    monkeypatch.setattr(codex, "_session_file_identity", racing)
    collector.sync_history(db)
    assert collector.last_sync_complete is False
    monkeypatch.setattr(codex, "_session_file_identity", real)
    collector.sync_history(db)
    assert collector.last_sync_complete is True


def test_last_sync_complete_resets_when_sync_raises(db, tmp_path, monkeypatch):
    root = tmp_path / "codex"
    _write(root, "a", _rollout("a"))
    collector = _collector(root)
    collector.sync_history(db)
    assert collector.last_sync_complete is True

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(collector, "_load_identity", boom)
    with pytest.raises(RuntimeError):
        collector.sync_history(db)
    assert collector.last_sync_complete is False


def test_preserve_missing_parents_flag(db, tmp_path):
    root = tmp_path / "codex"
    parent = _write(root, "p", _rollout("p", tokens=100))
    child = _write(root, "c", _rollout("c", parent="p", tokens=300, turns=2))
    _collector(root).sync_history(db)
    assert _session_ids(db) == {"p", "c"}

    # Local default keeps a parent whose file is absent from the inventory.
    _collector(root).sync_history(db, files=[child])
    assert _session_ids(db) == {"p", "c"}

    # An authoritative complete list (remote) drops the deleted parent.
    authoritative = _collector(root)
    authoritative.sync_history(db, files=[child], preserve_missing_parents=False)
    assert authoritative.last_sync_complete is True
    assert _session_ids(db) == {"c"}
    assert parent.exists()

    fresh = Database(db_path=str(tmp_path / "fresh.db"))
    try:
        _collector(root).sync_history(fresh, files=[child], preserve_missing_parents=False)
        assert _dump(db) == _dump(fresh)
    finally:
        fresh.close()


def _session_ids(db):
    return {r[0] for r in db.conn.execute("SELECT session_id FROM sessions")}


def test_preserve_missing_parents_false_not_destructive_when_unstable(db, tmp_path):
    root = tmp_path / "codex"
    _write(root, "p", _rollout("p"))
    child = _write(root, "c", _rollout("c", parent="p", tokens=300, turns=2))
    _collector(root).sync_history(db)
    assert _session_ids(db) == {"p", "c"}
    bad = root / "sessions" / "rollout-gone.jsonl"
    collector = _collector(root)
    collector.sync_history(db, files=[child, bad], preserve_missing_parents=False)
    assert collector.last_sync_complete is False
    assert _session_ids(db) == {"p", "c"}
