"""Persistent Claude parse checkpoints must match a fresh full parse."""

import base64
import itertools
import json
import os
from pathlib import Path
import zlib

import pytest

from agentic_metric.collectors import claude_code
from agentic_metric.collectors.claude_code import ClaudeCodeCollector, _SessionAccum
from agentic_metric.store.database import Database


ACCOUNT_A = "codex:" + "a" * 64
ACCOUNT_B = "codex:" + "b" * 64
MODEL = "claude-opus-4-8"
DAY = "2026-10-10"
NEXT_DAY = "2026-10-11"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr("agentic_metric.store.database.DATA_DIR", tmp_path)
    monkeypatch.setattr("agentic_metric.pricing.PRICING_FILE", tmp_path / "pricing.json")
    monkeypatch.setattr("agentic_metric.pricing.PRICING_CATALOG_FILE", tmp_path / "catalog.json")
    monkeypatch.setattr("agentic_metric.pricing._user_cache", None)
    monkeypatch.setattr("agentic_metric.pricing._user_cache_mtime", -1.0)
    calls = {"n": 0}
    original = _SessionAccum._process_entry

    def counted(self, entry, today_str):
        calls["n"] += 1
        return original(self, entry, today_str)

    monkeypatch.setattr(_SessionAccum, "_process_entry", counted)
    return calls


def user(text, day=DAY):
    return {"type": "user", "timestamp": f"{day}T10:00:00", "cwd": "/work",
            "message": {"content": text}}


def asst(msg_id, account="", tokens=100, day=DAY, usage="auto", model=MODEL):
    if usage == "auto":
        usage = {"input_tokens": tokens, "output_tokens": 20, "cache_read_input_tokens": 30,
                 "cache_creation_input_tokens": 10}
        if account:
            usage["wcc_subscriber_account"] = {
                "provider": "codex", "account_fingerprint": account.split(":", 1)[1]}
    message = {"id": msg_id, "model": model}
    if usage is not None:
        message["usage"] = usage
    return {"type": "assistant", "timestamp": f"{day}T10:00:00", "message": message}


def lines(entries):
    return "".join(json.dumps(entry) + "\n" for entry in entries)


_clock = [10**18]
_fresh = itertools.count()


def write(path, text, atomic=False):
    _clock[0] += 2 * 10**9
    target = path.with_name(path.name + ".tmp") if atomic else path
    target.write_text(text)
    os.utime(target, ns=(_clock[0], _clock[0]))
    if atomic:
        os.replace(target, path)


def snapshot(db):
    sessions = db.conn.execute(
        """SELECT session_id, project_path, git_branch, model, message_count, user_turns,
                  input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
                  cache_creation_1h_tokens, estimated_cost_usd, started_at, ended_at,
                  first_prompt, last_prompt FROM sessions ORDER BY session_id"""
    ).fetchall()
    usage = db.conn.execute("SELECT * FROM session_usage ORDER BY session_id, usage_date, "
                            "usage_hour, model, account_key").fetchall()
    return [tuple(row) for row in sessions], [tuple(row) for row in usage]


def sync(tmp_path, name, calls):
    calls["n"] = 0
    db = Database(str(tmp_path / name))
    ClaudeCodeCollector(tmp_path / "projects", provider="wcc", data_root="/src").sync_history(db)
    result = snapshot(db)
    db.close()
    return result


def fresh_matches(tmp_path, calls, restored):
    # restored was produced by an earlier connection; a clean DB must agree exactly.
    expected = sync(tmp_path, f"fresh-{next(_fresh)}.db", calls)
    assert restored == expected


def checkpoint_keys(tmp_path):
    db = Database(str(tmp_path / "state.db"))
    keys = [row[0] for row in db.conn.execute(
        "SELECT key FROM sync_state WHERE key LIKE '%:checkpoint:%'")]
    db.close()
    return keys


def setup_single(tmp_path, calls, initial):
    project = tmp_path / "projects" / "-work"
    project.mkdir(parents=True)
    path = project / "session.jsonl"
    write(path, initial)
    sync(tmp_path, "state.db", calls)
    assert len(checkpoint_keys(tmp_path)) == 1
    return path


BASE = [user("hi"), asst("a", ACCOUNT_A, 100), asst("b", "", 50)]
BASE_TEXT = lines(BASE)


@pytest.mark.parametrize("name,extra", [
    ("append", [asst("c", tokens=7), user("more")]),
    ("account_switch_and_usage_less_update", [
        asst("a", ACCOUNT_B, 300, NEXT_DAY, model="claude-sonnet-4-5"),
        asst("a", usage=None, day=DAY),
    ]),
    ("empty_usage_update", [asst("a", usage={}, day=NEXT_DAY)]),
    ("list_usage_update", [asst("b", usage=[], day=NEXT_DAY)]),
])
def test_verified_append_parses_only_new_entries(tmp_path, isolated, name, extra):
    path = setup_single(tmp_path, isolated, BASE_TEXT)
    write(path, BASE_TEXT + lines(extra))
    restored = sync(tmp_path, "state.db", isolated)
    assert isolated["n"] == len(extra)
    fresh_matches(tmp_path, isolated, restored)


def test_partial_line_continuation_is_not_counted_twice(tmp_path, isolated):
    tail = json.dumps(asst("c", tokens=7))
    path = setup_single(tmp_path, isolated, BASE_TEXT + tail[:25])
    write(path, BASE_TEXT + tail + "\n" + lines([user("next")]))
    restored = sync(tmp_path, "state.db", isolated)
    assert isolated["n"] == 2
    fresh_matches(tmp_path, isolated, restored)


def test_parsed_unterminated_tail_is_never_checkpointed(tmp_path, isolated):
    tail = json.dumps(asst("c", tokens=7))
    project = tmp_path / "projects" / "-work"
    project.mkdir(parents=True)
    path = project / "session.jsonl"
    write(path, BASE_TEXT + tail)
    sync(tmp_path, "state.db", isolated)
    assert checkpoint_keys(tmp_path) == []
    write(path, BASE_TEXT + tail + "\n" + lines([user("next")]))
    restored = sync(tmp_path, "state.db", isolated)
    fresh_matches(tmp_path, isolated, restored)


@pytest.mark.parametrize("name,final,atomic,full", [
    ("same_size_rewrite", BASE_TEXT.replace('"input_tokens": 100', '"input_tokens": 200'), False, True),
    ("same_size_atomic_replace", BASE_TEXT.replace('"input_tokens": 100', '"input_tokens": 200'), True, True),
    ("prefix_rewrite_with_growth", BASE_TEXT.replace('"hi"', '"yo"') + lines([user("more")]), False, True),
    ("truncation", lines(BASE[:2]), False, True),
    ("atomic_replace_same_prefix", BASE_TEXT + lines([user("more")]), True, False),
], ids=["same_size", "same_size_atomic", "prefix_growth", "truncation", "atomic_same_prefix"])
def test_rewrites_are_detected_by_full_prefix_hash(tmp_path, isolated, name, final, atomic, full):
    path = setup_single(tmp_path, isolated, BASE_TEXT)
    write(path, final, atomic=atomic)
    restored = sync(tmp_path, "state.db", isolated)
    assert isolated["n"] == (final.count("\n") if full else 1)
    fresh_matches(tmp_path, isolated, restored)


def test_corrupt_incompatible_or_timezone_changed_checkpoint_falls_back(tmp_path, isolated, monkeypatch):
    path = setup_single(tmp_path, isolated, BASE_TEXT)
    key = checkpoint_keys(tmp_path)[0]
    good = Database(str(tmp_path / "state.db"))
    stored = good.get_sync_state(key)
    bad_payload = json.loads(zlib.decompress(base64.b64decode(stored)))
    bad_payload["scalars"]["input_tokens"] = "oops"
    wrong_type = base64.b64encode(zlib.compress(json.dumps(bad_payload).encode())).decode()
    for value in ("not-base64!", base64.b64encode(b"not zlib").decode(), wrong_type):
        good.set_sync_state(key, value)
        write(path, BASE_TEXT + lines([user(f"more {len(value)}")]))
        restored = sync_with_db(tmp_path, good, isolated)
        assert isolated["n"] == path.read_text().count("\n")
        fresh_matches(tmp_path, isolated, restored)
        good.set_sync_state(key, stored)
    good.close()
    write(path, BASE_TEXT + lines([user("tz")]))
    monkeypatch.setattr(claude_code, "_timezone_signature", lambda: ["elsewhere"])
    restored = sync(tmp_path, "state.db", isolated)
    assert isolated["n"] == 4
    fresh_matches(tmp_path, isolated, restored)


def sync_with_db(tmp_path, db, calls):
    calls["n"] = 0
    ClaudeCodeCollector(tmp_path / "projects", provider="wcc", data_root="/src").sync_history(db)
    return snapshot(db)


@pytest.mark.parametrize("buckets", [{}, ""])
def test_malformed_bucket_container_falls_back_to_full_parse(tmp_path, isolated, buckets):
    path = setup_single(tmp_path, isolated, BASE_TEXT)
    key = checkpoint_keys(tmp_path)[0]
    db = Database(str(tmp_path / "state.db"))
    stored = db.get_sync_state(key)
    payload = json.loads(zlib.decompress(base64.b64decode(stored)))
    payload["buckets"] = buckets
    db.set_sync_state(key, base64.b64encode(zlib.compress(json.dumps(payload).encode())).decode())
    db.close()
    write(path, BASE_TEXT + lines([user("more")]))
    restored = sync(tmp_path, "state.db", isolated)
    assert isolated["n"] == 4
    fresh_matches(tmp_path, isolated, restored)


@pytest.mark.parametrize("stage", ["during_hash", "after_restore", "during_suffix"])
def test_prefix_rewrite_during_restore_falls_back_to_full_parse(tmp_path, isolated, monkeypatch, stage):
    import builtins

    path = setup_single(tmp_path, isolated, BASE_TEXT)
    write(path, BASE_TEXT + lines([user("append")]))
    replacement = BASE_TEXT.replace('"input_tokens": 100', '"input_tokens": 200') + lines([user("append"), user("raced")])
    original_open = builtins.open
    original_restore = claude_code._restore_checkpoint
    armed = stage == "during_hash"
    changed = False

    def mutate():
        nonlocal changed
        changed = True
        write(path, replacement)

    class Handle:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def read(self, size=-1):
            data = self.handle.read(size)
            if armed and not changed and ((stage == "during_hash" and size >= 0) or (stage == "during_suffix" and size < 0)):
                mutate()
            return data

    def opening(file, mode="r", *args, **kwargs):
        handle = original_open(file, mode, *args, **kwargs)
        return Handle(handle) if Path(file) == path and mode == "rb" else handle

    def restore(*args, **kwargs):
        nonlocal armed
        accum = original_restore(*args, **kwargs)
        if stage == "after_restore" and accum is not None and not changed:
            mutate()
        if stage == "during_suffix":
            armed = True
        return accum

    with monkeypatch.context() as patcher:
        patcher.setattr(builtins, "open", opening)
        patcher.setattr(claude_code, "_restore_checkpoint", restore)
        restored = sync(tmp_path, "state.db", isolated)
    assert changed
    assert isolated["n"] == 5
    fresh_matches(tmp_path, isolated, restored)


def test_pricing_invalidation_drops_checkpoints_and_reparses(tmp_path, isolated):
    path = setup_single(tmp_path, isolated, BASE_TEXT)
    db = Database(str(tmp_path / "state.db"))
    db.set_sync_state("pricing:fingerprint", "old")
    db.close()
    assert checkpoint_keys(tmp_path) == []
    restored = sync(tmp_path, "state.db", isolated)
    assert isolated["n"] == 3
    assert len(checkpoint_keys(tmp_path)) == 1
    fresh_matches(tmp_path, isolated, restored)


def test_removed_file_checkpoint_is_reconciled(tmp_path, isolated):
    path = setup_single(tmp_path, isolated, BASE_TEXT)
    path.unlink()
    sync(tmp_path, "state.db", isolated)
    assert checkpoint_keys(tmp_path) == []


def setup_replay(tmp_path, calls, parent=None):
    project = tmp_path / "projects" / "-work"
    subagents = project / "parent" / "subagents"
    subagents.mkdir(parents=True)
    child = subagents / "agent-1.jsonl"
    write(child, lines([user("child"), asst("shared", tokens=10), asst("own-1", tokens=5)]))
    if parent is not None:
        write(project / "parent.jsonl", lines(parent))
    sync(tmp_path, "state.db", calls)
    return project / "parent.jsonl", child


CHILD_APPEND = [asst("own-2", tokens=3)]


def test_stable_replay_checkpoint_and_unchanged_sibling(tmp_path, isolated):
    parent, child = setup_replay(tmp_path, isolated, [user("p"), asst("shared", tokens=10)])
    child_text = child.read_text()
    write(child, child_text + lines(CHILD_APPEND))
    restored = sync(tmp_path, "state.db", isolated)
    assert isolated["n"] == 1
    fresh_matches(tmp_path, isolated, restored)
    write(parent, parent.read_text() + lines([user("p2")]))
    restored = sync(tmp_path, "state.db", isolated)
    assert isolated["n"] == 1
    fresh_matches(tmp_path, isolated, restored)


@pytest.mark.parametrize("append_child", [False, True])
def test_new_earlier_owner_forces_child_full_parse(tmp_path, isolated, append_child):
    parent, child = setup_replay(tmp_path, isolated)
    write(parent, lines([user("p"), asst("shared", tokens=10)]))
    if append_child:
        write(child, child.read_text() + lines(CHILD_APPEND))
    restored = sync(tmp_path, "state.db", isolated)
    assert isolated["n"] == 2 + 3 + append_child
    fresh_matches(tmp_path, isolated, restored)


def test_disappearing_owner_forces_child_full_parse(tmp_path, isolated):
    parent, child = setup_replay(tmp_path, isolated, [user("p"), asst("shared", tokens=10)])
    parent.unlink()
    write(child, child.read_text() + lines(CHILD_APPEND))
    restored = sync(tmp_path, "state.db", isolated)
    assert isolated["n"] == 4
    fresh_matches(tmp_path, isolated, restored)


def test_legacy_assistant_id_cache_without_ownership_is_reparsed(tmp_path, isolated):
    parent, child = setup_replay(tmp_path, isolated, [user("p"), asst("shared", tokens=10)])
    db = Database(str(tmp_path / "state.db"))
    for row in db.conn.execute("SELECT key, value FROM sync_state WHERE key LIKE '%:assistant_ids:%'").fetchall():
        payload = json.loads(row["value"])
        payload.pop("owned_ids")
        db.set_sync_state(row["key"], json.dumps(payload))
    db.close()
    write(parent, parent.read_text() + lines([user("p2")]))
    restored = sync(tmp_path, "state.db", isolated)
    assert isolated["n"] == 1
    db = Database(str(tmp_path / "state.db"))
    cached = [json.loads(row[0]) for row in db.conn.execute(
        "SELECT value FROM sync_state WHERE key LIKE '%:assistant_ids:%'")]
    db.close()
    assert all("owned_ids" in payload for payload in cached)
    fresh_matches(tmp_path, isolated, restored)
