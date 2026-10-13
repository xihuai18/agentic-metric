"""Tests for the per-refresh query snapshot."""

import sqlite3

import pytest

from agentic_metric.store import aggregator
from agentic_metric.store.aggregator import (
    get_range_by_agent,
    get_range_daily,
    get_range_totals,
    get_used_models,
    query_snapshot,
)
from agentic_metric.store.database import Database

DAY = "2026-07-13"


def _bucket(tokens=1_000_000):
    return {"usage_date": DAY, "usage_hour": 8, "model": "gpt-5.4", "input_tokens": tokens}


def _add_session(db, session_id, *, with_usage=True):
    db.upsert_session(
        session_id, "codex", provider="openai", data_root="/r",
        model="gpt-5.4", started_at=f"{DAY}T08:00:00Z",
    )
    if with_usage:
        db.replace_session_usage(
            session_id, "codex", [_bucket()], provider="openai", data_root="/r"
        )
    db.commit()


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=str(tmp_path / "q.db"))
    _add_session(database, "s1")
    yield database
    database.close()


def _count_probes(conn):
    statements = []
    conn.set_trace_callback(statements.append)
    return lambda: sum(
        1 for s in statements if "FROM sessions AS s" in s and "LIMIT 1" in s
    )


def _run_queries(db):
    get_range_totals(db, DAY, DAY)
    get_range_by_agent(db, DAY, DAY)
    get_range_daily(db, DAY, DAY)
    get_used_models(db)


def test_snapshot_runs_one_pending_probe_for_many_queries(db):
    probes = _count_probes(db.conn)
    _run_queries(db)
    unshared = probes()
    assert unshared >= 4

    with query_snapshot(db):
        _run_queries(db)
    assert probes() - unshared == 1


def test_snapshot_gives_every_query_the_same_committed_state(db, tmp_path):
    other = Database(db_path=str(tmp_path / "q.db"))
    with query_snapshot(db):
        before = get_range_totals(db, DAY, DAY)
        _add_session(other, "s2")
        during = get_range_totals(db, DAY, DAY)
        assert during == before
    after = get_range_totals(db, DAY, DAY)
    assert after != before
    other.close()


def test_snapshot_releases_transaction_and_cache(db):
    assert not db.conn.in_transaction
    with query_snapshot(db):
        assert db.conn.in_transaction
        _run_queries(db)
    assert not db.conn.in_transaction
    assert aggregator._snapshots == {}


def test_snapshot_releases_on_error(db):
    with pytest.raises(RuntimeError):
        with query_snapshot(db):
            _run_queries(db)
            raise RuntimeError("boom")
    assert not db.conn.in_transaction
    assert aggregator._snapshots == {}


def test_cache_does_not_leak_between_contexts(db, tmp_path):
    other = Database(db_path=str(tmp_path / "q.db"))
    with query_snapshot(db):
        assert aggregator._usage_source(db) == "session_usage"
    _add_session(other, "s2", with_usage=False)  # pending resync appears
    with query_snapshot(db):
        assert "UNION ALL" in aggregator._usage_source(db)
        assert "UNION ALL" in aggregator._usage_source(db)
    other.close()


def test_write_inside_snapshot_drops_cached_probe(db):
    with query_snapshot(db):
        assert aggregator._usage_source(db) == "session_usage"
        db.upsert_session("s2", "codex", provider="openai", data_root="/r", model="m")
        assert "UNION ALL" in aggregator._usage_source(db)
    # This connection wrote inside the block, so its transaction was left to the caller.
    assert db.conn.in_transaction
    db.commit()


def test_caller_owned_transaction_is_neither_committed_nor_rolled_back(db, tmp_path):
    db.conn.execute("BEGIN")
    db.upsert_session("pending", "codex", provider="openai", data_root="/r", model="m")
    with query_snapshot(db):
        _run_queries(db)
        assert db.conn.in_transaction
    assert db.conn.in_transaction
    seen = sqlite3.connect(str(tmp_path / "q.db"))
    assert seen.execute("SELECT COUNT(*) FROM sessions WHERE session_id='pending'").fetchone()[0] == 0
    db.commit()
    assert seen.execute("SELECT COUNT(*) FROM sessions WHERE session_id='pending'").fetchone()[0] == 1
    seen.close()


def test_nested_snapshot_reuses_outer_state(db):
    probes = _count_probes(db.conn)
    with query_snapshot(db):
        _run_queries(db)
        with query_snapshot(db):
            _run_queries(db)
        assert db.conn.in_transaction
        _run_queries(db)
    assert probes() == 1
    assert not db.conn.in_transaction


class _Adapter:
    """Minimal DB stand-in whose connection exposes only the given attributes."""

    def __init__(self, conn, *, in_transaction=None, begin_fails=False, begin_noop=False):
        outer = self
        self.begins = 0

        class Conn:
            def execute(self, sql, *args):
                if sql == "BEGIN":
                    outer.begins += 1
                    if begin_fails:
                        raise sqlite3.OperationalError("cannot begin")
                    if begin_noop:
                        return None
                return conn.execute(sql, *args)

        self.conn = Conn()
        if in_transaction is not None:
            self.conn.in_transaction = in_transaction


def _fallback_session(path):
    other = Database(db_path=path)
    other.upsert_session("late", "codex", provider="openai", data_root="/r", model="m")
    other.commit()
    other.close()


def test_adapter_without_transaction_metadata_is_not_cached(db, tmp_path):
    adapter = _Adapter(db.conn)
    with query_snapshot(adapter):
        assert aggregator._usage_source(adapter) == "session_usage"
        _fallback_session(str(tmp_path / "q.db"))
        assert "UNION ALL" in aggregator._usage_source(adapter)
    assert adapter.begins == 0
    assert aggregator._snapshots == {}


def test_failed_begin_is_not_cached(db, tmp_path):
    adapter = _Adapter(db.conn, in_transaction=False, begin_fails=True)
    with query_snapshot(adapter):
        assert aggregator._usage_source(adapter) == "session_usage"
        _fallback_session(str(tmp_path / "q.db"))
        assert "UNION ALL" in aggregator._usage_source(adapter)
    assert adapter.begins == 1
    assert aggregator._snapshots == {}


def test_begin_that_does_not_open_a_transaction_is_not_cached(db, tmp_path):
    adapter = _Adapter(db.conn, in_transaction=False, begin_noop=True)
    with query_snapshot(adapter):
        assert aggregator._usage_source(adapter) == "session_usage"
        _fallback_session(str(tmp_path / "q.db"))
        assert "UNION ALL" in aggregator._usage_source(adapter)


def test_concurrent_commit_is_invisible_inside_a_verified_snapshot(db, tmp_path):
    with query_snapshot(db):
        assert aggregator._usage_source(db) == "session_usage"
        _fallback_session(str(tmp_path / "q.db"))
        assert aggregator._usage_source(db) == "session_usage"
    with query_snapshot(db):
        assert "UNION ALL" in aggregator._usage_source(db)
