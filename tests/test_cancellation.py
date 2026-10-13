"""Cancellation must stop owned work without accepting incomplete derived state."""

from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextvars import copy_context
import json
import os
import subprocess
import sys
from threading import Event, get_ident
import time
from unittest.mock import patch

import pytest

from agentic_metric.collectors import (
    BaseCollector, CollectorRegistry, current_sync_cancel_event, sync_cancellation,
)
from agentic_metric.collectors.remote import _run_command
from agentic_metric.store.database import Database


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr("agentic_metric.store.database.DATA_DIR", tmp_path)
    monkeypatch.setattr("agentic_metric.pricing.PRICING_FILE", tmp_path / "pricing.json")
    monkeypatch.setattr("agentic_metric.pricing.PRICING_CATALOG_FILE", tmp_path / "catalog.json")


def test_cancelled_subprocess_is_reaped_promptly(monkeypatch):
    cancelled = Event()
    started = Event()
    processes = []
    original = subprocess.Popen

    def spawn(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(subprocess, "Popen", spawn)
    with sync_cancellation(cancelled), ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(copy_context().run, _run_command,
                             [sys.executable, "-c", "from threading import Event; Event().wait(30)"], 30, "probe")
        assert started.wait(2)
        before = time.perf_counter()
        cancelled.set()
        with pytest.raises(CancelledError):
            future.result(timeout=2)
        assert time.perf_counter() - before < 1.5
    assert processes[0].poll() is not None
    assert current_sync_cancel_event() is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group cancellation")
def test_cancel_kills_descendant_after_leader_closes_pipes(tmp_path, monkeypatch):
    import psutil

    child_code = "import signal,sys,os; from pathlib import Path; from threading import Event; signal.signal(signal.SIGTERM,signal.SIG_IGN); Path(sys.argv[1]).write_text(str(os.getpid())); print('ready',flush=True); Event().wait(30)"
    parent_code = "import subprocess,sys; from threading import Event; child=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL); child.stdout.readline(); print('ready',flush=True); Event().wait(30)"
    pid_path = tmp_path / "child.pid"
    cancelled = Event()
    started = Event()
    original = subprocess.Popen

    def spawn(*args, **kwargs):
        process = original(*args, **kwargs)
        assert process.stdout.readline() == b"ready\n"
        started.set()
        return process

    monkeypatch.setattr(subprocess, "Popen", spawn)
    with sync_cancellation(cancelled), ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(copy_context().run, _run_command,
                             [sys.executable, "-c", parent_code, child_code, str(pid_path)], 30, "probe")
        assert started.wait(2)
        cancelled.set()
        with pytest.raises(CancelledError):
            future.result(timeout=2)
    try:
        child = psutil.Process(int(pid_path.read_text()))
        psutil.wait_procs([child], timeout=1)
        status = child.status()
    except psutil.NoSuchProcess:
        status = psutil.STATUS_DEAD
    assert status in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD)


def test_cancellable_command_retains_input_output_and_timeout():
    payload = b"fixture" * 100_000
    with sync_cancellation(Event()):
        result = _run_command(
            [sys.executable, "-c", "import sys; from threading import Event; data=sys.stdin.buffer.read(); Event().wait(.15); print(len(data))"],
            3, "probe", input_bytes=payload,
        )
        assert int(result.stdout) == len(payload)
        with pytest.raises(RuntimeError, match="probe timed out"):
            _run_command([sys.executable, "-c", "from threading import Event; Event().wait(30)"], 0.1, "probe")


def test_cancel_rolls_back_only_uncommitted_collector_work(tmp_path):
    cancelled = Event()
    db = Database(str(tmp_path / "state.db"))

    class Committed(BaseCollector):
        agent_type = "codex"

        def sync_history(self, db):
            db.upsert_session("complete", "codex", data_root="fixture", message_count=1)
            db.commit()

    class Interrupted(BaseCollector):
        agent_type = "codex"

        def sync_history(self, db):
            db.upsert_session("pending", "codex", data_root="fixture", message_count=1)
            db.set_sync_state("pending-checkpoint", "incomplete", commit=False)
            cancelled.set()

    registry = CollectorRegistry()
    registry.register(Committed())
    registry.register(Interrupted())
    with pytest.raises(CancelledError):
        registry.sync_all(db, cancel_event=cancelled)
    assert [row[0] for row in db.conn.execute("SELECT session_id FROM sessions")] == ["complete"]
    assert db.get_sync_state("pending-checkpoint") is None
    assert current_sync_cancel_event() is None
    assert db.conn.execute("SELECT 1").fetchone()[0] == 1
    db.close()


def test_remote_scope_parses_while_other_scope_is_still_mirroring():
    released = Event()
    parsed_threads = []
    caller = get_ident()

    class Fast(BaseCollector):
        agent_type = "codex"

        def prepare_cache(self):
            pass

        def sync_history(self, db):
            parsed_threads.append(get_ident())
            released.set()

    class Slow(BaseCollector):
        agent_type = "claude_code"

        def prepare_cache(self):
            assert released.wait(2)

        def sync_history(self, db):
            parsed_threads.append(get_ident())

    db = Database(":memory:")
    registry = CollectorRegistry()
    registry.register(Slow())
    registry.register(Fast())
    registry.sync_all(db)
    assert registry.get_sync_errors() == []
    assert parsed_threads == [caller, caller]
    db.close()


def test_cancel_keeps_only_verified_completed_transfer_progress(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from agentic_metric.collectors import check_sync_cancelled
    from agentic_metric.collectors import remote
    from agentic_metric.config import RemoteSpec

    cancelled = Event()
    saved = Event()
    waiting = Event()
    target = remote.RemoteSyncTarget(RemoteSpec(host="fixture"), "claude_code", "~/.claude", "", 0)
    manifest = {name: {"size": "10", "mtime": "123"} for name in ("a.jsonl", "b.jsonl")}
    monkeypatch.setattr(remote, "_DOWNLOAD_BATCH_FILES", 1)
    monkeypatch.setattr(remote.shutil, "which", lambda _: "/usr/bin/rsync")
    original_save = remote._save_manifest

    def save(root, data):
        original_save(root, data)
        saved.set()

    def download(argv, timeout, label, *, input_bytes, allowed_returncodes):
        name = input_bytes.decode().rstrip("\0")
        if name == "b.jsonl":
            waiting.set()
            assert current_sync_cancel_event().wait(2)
            check_sync_cancelled()
        path = tmp_path / "projects" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("verified\n")
        return Mock(stdout=f">f+++++++++ {name}\n".encode())

    monkeypatch.setattr(remote, "_save_manifest", save)
    monkeypatch.setattr(remote, "_run_command", download)
    with sync_cancellation(cancelled), ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(copy_context().run, remote._download_changed_files,
                             target, tmp_path, manifest, {}, list(manifest), rsync_base="/fixture/projects")
        assert saved.wait(2) and waiting.wait(2)
        cancelled.set()
        with pytest.raises(CancelledError):
            future.result(timeout=2)
    assert set(json.loads((tmp_path / ".remote-manifest.json").read_text())) == {"a.jsonl"}
    assert (tmp_path / ".remote-manifest.incomplete").exists()


@pytest.mark.parametrize("pricing", [False, True])
def test_tui_process_quits_without_waiting_for_active_work(tmp_path, pricing):
    code = r'''
import asyncio,json,subprocess,sys,time
from pathlib import Path
from threading import Event
from unittest.mock import patch
from agentic_metric.collectors import BaseCollector,CollectorRegistry
from agentic_metric.collectors.remote import _run_command
from agentic_metric.store.database import Database
from agentic_metric.tui.app import AgenticMetricApp
root=Path(sys.argv[1]); pricing=sys.argv[2]=='1'; started=Event(); processes=[]
original=subprocess.Popen
def spawn(*args,**kwargs):
    process=original(*args,**kwargs); processes.append(process); started.set(); return process
class Remote(BaseCollector):
    agent_type='codex'
    def prepare_cache(self):
        _run_command([sys.executable,'-c','from threading import Event; Event().wait(30)'],30,'probe')
    def sync_history(self,db): pass
def fetch(argv,timeout,label,**kwargs):
    assert 'refresh_pricing_catalog_if_stale' in argv[2]
    return _run_command([sys.executable,'-c','from threading import Event; Event().wait(30); print(0)'],30,'probe')
async def exercise():
    db=Database(str(root/'data.db')); db.upsert_session('cached','codex',data_root='fixture',message_count=1); db.commit(); registry=CollectorRegistry()
    if not pricing: registry.register(Remote())
    app=AgenticMetricApp(db=db,collectors=registry,sync_on_mount=True,refresh_pricing_on_mount=pricing,show_clock=False)
    async with app.run_test(size=(90,32)) as pilot:
        assert await asyncio.to_thread(started.wait,2)
        requested=time.perf_counter(); await pilot.press('q')
    print(json.dumps({'quit_seconds':time.perf_counter()-requested}),flush=True)
with patch('agentic_metric.store.database.DATA_DIR',root),patch('agentic_metric.pricing.PRICING_FILE',root/'price.json'),patch('agentic_metric.pricing.PRICING_CATALOG_FILE',root/'catalog.json'),patch('agentic_metric.tui.app.Database',lambda:Database(str(root/'data.db'))),patch('agentic_metric.tui.app._run_command',fetch),patch('subprocess.Popen',spawn):
    asyncio.run(exercise())
assert all(p.poll() is not None for p in processes)
'''
    before = time.perf_counter()
    try:
        result = subprocess.run([sys.executable, "-c", code, str(tmp_path), str(int(pricing))],
                                capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"{exc.stdout!r}\n{exc.stderr!r}")
    assert result.returncode == 0, result.stderr
    assert time.perf_counter() - before < 2
    assert json.loads(result.stdout.splitlines()[-1])["quit_seconds"] < 0.5


def test_cached_ui_open_defers_repricing_to_sync_connection(tmp_path):
    path = str(tmp_path / "cached.db")
    db = Database(path)
    db.set_sync_state("pricing:fingerprint", "old")
    db.set_sync_state("cc_jsonl:fixture", "verified-checkpoint")
    db.close()

    cached = Database(path, reprice_on_open=False)
    assert not cached.pricing_changed
    assert cached.get_sync_state("pricing:fingerprint") == "old"
    assert cached.get_sync_state("cc_jsonl:fixture") == "verified-checkpoint"
    cached.close()

    sync = Database(path)
    assert sync.pricing_changed
    assert sync.get_sync_state("cc_jsonl:fixture") is None
    sync.close()


def test_parser_observes_cancel_before_accepting_more_records(tmp_path):
    from agentic_metric.collectors.claude_code import _SessionAccum

    path = tmp_path / "session.jsonl"
    path.write_text(''.join(json.dumps({"type": "user", "timestamp": "2026-10-13T10:00:00Z", "message": {"content": "fixture"}}) + '\n' for _ in range(3)))
    cancelled = Event()
    original = _SessionAccum._process_entry

    def process(self, entry, today):
        original(self, entry, today)
        cancelled.set()

    accum = _SessionAccum(path, "fixture")
    with patch.object(_SessionAccum, "_process_entry", process), sync_cancellation(cancelled):
        with pytest.raises(CancelledError):
            accum.read_new_lines()
    assert accum.user_turns == 1
