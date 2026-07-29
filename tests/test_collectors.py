"""Tests for collector module."""

import json
import os
import time
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from agentic_metric.collectors import CollectorRegistry, BaseCollector, create_default_registry
from agentic_metric.collectors.claude_code import (
    ClaudeCodeCollector,
    _read_cwd as claude_read_cwd,
    _SessionAccum as ClaudeSessionAccum,
)
from agentic_metric.collectors.codex import (
    CodexCollector,
    _SessionAccum as CodexSessionAccum,
)
from agentic_metric.collectors.remote import (
    RemoteHistoryCollector,
    RemoteSyncTarget,
    _MAX_SKIPPED_BATCHES,
    _cache_root_for,
    _extract_tarball as remote_extract_tarball,
    _manifest_command,
    _ssh_command,
)
from agentic_metric.config import (
    RemoteCollectorRoot,
    RemoteSpec,
    config_is_reconcilable,
    get_remote_specs,
)
from agentic_metric.pricing import estimate_cost
from agentic_metric.store.database import Database


class MockCollector(BaseCollector):
    @property
    def agent_type(self) -> str:
        return "mock"

    def sync_history(self, db) -> None:
        pass


def test_registry_register():
    registry = CollectorRegistry()
    collector = MockCollector()
    registry.register(collector)
    assert len(registry.get_all()) == 1
    assert registry.get_all()[0].agent_type == "mock"


def test_registry_reports_collector_exceptions_and_rolls_back():
    class FailingCollector(BaseCollector):
        @property
        def agent_type(self) -> str:
            return "failing"

        def sync_history(self, db) -> None:
            db.conn.execute(
                "INSERT INTO sync_state (key, value) VALUES ('partial', 'written')"
            )
            raise RuntimeError("collector failed")

    db = Database(db_path=":memory:")
    registry = CollectorRegistry()
    registry.register(FailingCollector())

    registry.sync_all(db)

    assert db.get_sync_state("partial") is None
    assert registry.get_sync_errors() == ["failing: collector failed"]
    db.close()


def test_default_registry_uses_configured_roots(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({
        "collectors": {
            "codex": {
                "roots": [
                    {"path": str(tmp_path / "codex-openai"), "provider": "openai"},
                    {"path": str(tmp_path / "codex-custom"), "provider": "custom"},
                ],
            },
            "claude_code": {
                "roots": [
                    {"path": str(tmp_path / "claude-alt")},
                    {"path": str(tmp_path / "claude-provider-b"), "provider": "provider-b"},
                ],
            },
        },
    }))

    with patch("agentic_metric.config.CONFIG_FILE", config_file):
        registry = create_default_registry()

    collectors = registry.get_all()
    assert [(c.agent_type, c.provider, c.data_root) for c in collectors] == [
        ("claude_code", "", str(tmp_path / "claude-alt")),
        ("claude_code", "provider-b", str(tmp_path / "claude-provider-b")),
        ("codex", "openai", str(tmp_path / "codex-openai")),
        ("codex", "custom", str(tmp_path / "codex-custom")),
    ]
    assert collectors[0].projects_dir == tmp_path / "claude-alt" / "projects"
    assert collectors[2].sessions_dir == tmp_path / "codex-openai" / "sessions"


def test_scope_reconciliation_requires_sane_config_shapes(tmp_path):
    config_file = tmp_path / "config.json"
    with patch("agentic_metric.config.CONFIG_FILE", config_file):
        config_file.write_text(json.dumps({"collectors": {}, "remotes": []}))
        assert config_is_reconcilable() is True

        config_file.write_text(json.dumps({"collectors": {"codex": "bad"}}))
        assert config_is_reconcilable() is False

        config_file.write_text(json.dumps({"remotes": [{"name": "missing-host"}]}))
        assert config_is_reconcilable() is False


def test_remote_specs_default_to_local_collector_roots(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({
        "collectors": {
            "codex": {"roots": [{"path": "~/.codex-openai", "provider": "openai"}]},
            "claude_code": {"roots": [{"path": "~/.claude-main"}]},
        },
        "remotes": [{"name": "dev", "host": "remote-dev", "timeout": 7}],
    }))

    with patch("agentic_metric.config.CONFIG_FILE", config_file):
        remotes = get_remote_specs()

    assert len(remotes) == 1
    assert remotes[0].name == "dev"
    assert remotes[0].host == "remote-dev"
    assert remotes[0].timeout == 7
    assert remotes[0].collectors == {
        "codex": [RemoteCollectorRoot(path="~/.codex-openai", provider="openai")],
        "claude_code": [RemoteCollectorRoot(path="~/.claude-main", provider="")],
    }


def test_default_registry_includes_remote_collectors(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({
        "remotes": [
            {
                "name": "dev",
                "host": "remote-dev",
                "collectors": {
                    "codex": {"roots": [{"path": "~/.codex-remote/sessions", "provider": "openai"}]},
                    "claude_code": {"roots": [{"path": "~/.claude-remote/projects"}]},
                },
            }
        ],
    }))

    with patch("agentic_metric.config.CONFIG_FILE", config_file):
        registry = create_default_registry()

    remote_collectors = [
        c for c in registry.get_all()
        if getattr(c, "data_root", "").startswith("ssh://dev/")
    ]
    assert [(c.agent_type, c.provider, c.data_root) for c in remote_collectors] == [
        ("claude_code", "", "ssh://dev/~/.claude-remote"),
        ("codex", "openai", "ssh://dev/~/.codex-remote"),
    ]


def test_remote_ssh_command_expands_tilde_on_remote_host():
    remote = RemoteSpec(host="remote-dev", user="leo", port=2222)
    target = RemoteSyncTarget(remote, "codex", "~/.codex", "openai", 0)
    cmd = _ssh_command(remote, _manifest_command(target))

    assert cmd[:4] == ["ssh", "-p", "2222", "leo@remote-dev"]
    assert "root='~/.codex'" in cmd[-1]
    assert 'case "$root"' in cmd[-1]
    assert 'root="$HOME/${root#\\~/}"' in cmd[-1]


def test_remote_manifest_command_supports_gnu_and_bsd_stat():
    remote = RemoteSpec(host="remote-dev")
    target = RemoteSyncTarget(remote, "codex", "~/.codex", "openai", 0)
    cmd = _manifest_command(target)

    assert "stat --printf" in cmd
    assert "stat -f" in cmd
    assert "printf" in cmd
    assert "%s\\0" in cmd
    assert "stat -c" not in cmd


def test_codex_session_meta_provider_sets_provider():
    accum = CodexSessionAccum(Path("/tmp/fake.jsonl"), project_path="/test")
    accum._process_entry({
        "type": "session_meta",
        "payload": {
            "id": "sid",
            "cwd": "/test",
            "model_provider": "custom",
        },
    })

    assert accum.provider == "custom"


def test_codex_configured_provider_overrides_session_provider():
    accum = CodexSessionAccum(
        Path("/tmp/fake.jsonl"),
        project_path="/test",
        provider="openai",
    )
    accum._process_entry({
        "type": "session_meta",
        "payload": {
            "id": "sid",
            "cwd": "/test",
            "model_provider": "custom",
        },
    })

    assert accum.provider == "openai"


def test_codex_history_sync_skips_mismatched_configured_provider(tmp_path):
    def write_rollout(sessions_root: Path) -> None:
        day_dir = sessions_root / "2026" / "04" / "23"
        day_dir.mkdir(parents=True)
        lines = [
            {
                "timestamp": "2026-04-23T10:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "sid",
                    "cwd": "/tmp/project",
                    "model_provider": "custom",
                },
            },
            {
                "timestamp": "2026-04-23T10:00:00Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.5"},
            },
            {
                "timestamp": "2026-04-23T10:00:01Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "hello"},
            },
            {
                "timestamp": "2026-04-23T10:00:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 20}},
                },
            },
        ]
        (day_dir / "rollout-test.jsonl").write_text(
            "".join(json.dumps(line) + "\n" for line in lines)
        )

    openai_sessions = tmp_path / "codex-openai" / "sessions"
    custom_sessions = tmp_path / "codex-custom" / "sessions"
    write_rollout(openai_sessions)
    write_rollout(custom_sessions)

    db = Database(db_path=str(tmp_path / "data.db"))
    CodexCollector(
        sessions_dir=openai_sessions,
        provider="openai",
        data_root=str(tmp_path / "codex-openai"),
    ).sync_history(db)
    CodexCollector(
        sessions_dir=custom_sessions,
        provider="custom",
        data_root=str(tmp_path / "codex-custom"),
    ).sync_history(db)

    rows = db.conn.execute(
        "SELECT provider, data_root FROM sessions WHERE session_id = 'sid' AND agent_type = 'codex'"
    ).fetchall()
    assert [(row["provider"], row["data_root"]) for row in rows] == [
        ("custom", str(tmp_path / "codex-custom"))
    ]
    db.close()


def test_codex_history_sync_supports_same_root_provider_filters(tmp_path):
    def write_rollout(session_id: str, provider: str) -> None:
        day_dir = sessions_dir / "2026" / "04" / "23"
        day_dir.mkdir(parents=True, exist_ok=True)
        lines = [
            {
                "timestamp": "2026-04-23T10:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "cwd": "/tmp/project",
                    "model_provider": provider,
                },
            },
            {
                "timestamp": "2026-04-23T10:00:00Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.5"},
            },
            {
                "timestamp": "2026-04-23T10:00:01Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": session_id},
            },
            {
                "timestamp": "2026-04-23T10:00:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 20}},
                },
            },
        ]
        (day_dir / f"rollout-{session_id}.jsonl").write_text(
            "".join(json.dumps(line) + "\n" for line in lines)
        )

    sessions_dir = tmp_path / "codex" / "sessions"
    data_root = str(tmp_path / "codex")
    write_rollout("openai-sid", "openai")
    write_rollout("custom-sid", "custom")

    db = Database(db_path=str(tmp_path / "data.db"))
    CodexCollector(
        sessions_dir=sessions_dir,
        provider="openai",
        data_root=data_root,
    ).sync_history(db)
    CodexCollector(
        sessions_dir=sessions_dir,
        provider="custom",
        data_root=data_root,
    ).sync_history(db)

    rows = db.conn.execute(
        """SELECT session_id, provider, data_root
           FROM sessions
           WHERE agent_type = 'codex'
           ORDER BY session_id"""
    ).fetchall()
    assert [(row["session_id"], row["provider"], row["data_root"]) for row in rows] == [
        ("custom-sid", "custom", data_root),
        ("openai-sid", "openai", data_root),
    ]
    assert db.conn.execute(
        "SELECT COUNT(*) AS n FROM sync_state WHERE key LIKE 'codex_jsonl:v17:%'"
    ).fetchone()["n"] == 4
    db.close()


def test_claude_history_sync_scopes_state_by_provider_and_root(tmp_path):
    projects = tmp_path / "projects"
    project = projects / "-tmp-project"
    project.mkdir(parents=True)
    session_file = project / "sid.jsonl"
    session_file.write_text("".join(json.dumps(line) + "\n" for line in [
        {
            "timestamp": "2026-04-23T10:00:00Z",
            "type": "user",
            "cwd": "/tmp/project",
            "message": {"content": "hello"},
        },
        {
            "timestamp": "2026-04-23T10:00:01Z",
            "type": "assistant",
            "cwd": "/tmp/project",
            "message": {
                "id": "msg-1",
                "model": "gpt-5.5",
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        },
    ]))

    db = Database(db_path=str(tmp_path / "data.db"))
    data_root = str(tmp_path)
    for provider in ("p1", "p2"):
        ClaudeCodeCollector(
            projects_dir=projects,
            provider=provider,
            data_root=data_root,
        ).sync_history(db)

    rows = db.conn.execute(
        """SELECT provider, input_tokens FROM sessions
           WHERE session_id = 'sid' AND agent_type = 'claude_code'
           ORDER BY provider"""
    ).fetchall()
    assert [tuple(row) for row in rows] == [("p1", 100), ("p2", 100)]
    state_keys = db.conn.execute(
        "SELECT key FROM sync_state WHERE key LIKE 'cc_jsonl:v13:%'"
    ).fetchall()
    assert len(state_keys) == 4
    assert sum(":assistant_ids:" in row["key"] for row in state_keys) == 2
    db.close()


def test_codex_history_sync_removes_sessions_for_missing_local_files(tmp_path):
    sessions = tmp_path / "sessions"
    day_dir = sessions / "2026" / "04" / "23"
    day_dir.mkdir(parents=True)

    def write_rollout(session_id: str) -> Path:
        path = day_dir / f"rollout-{session_id}.jsonl"
        path.write_text("".join(json.dumps(line) + "\n" for line in [
            {
                "timestamp": "2026-04-23T10:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "cwd": "/tmp/project",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-04-23T10:00:01Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.5"},
            },
            {
                "timestamp": "2026-04-23T10:00:01Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": session_id},
            },
            {
                "timestamp": "2026-04-23T10:00:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 100,
                            "output_tokens": 20,
                        },
                    },
                },
            },
        ]))
        return path

    preserved = write_rollout("sid-a")
    removed = write_rollout("sid-b")
    db = Database(db_path=str(tmp_path / "data.db"))
    collector = CodexCollector(
        sessions_dir=sessions,
        provider="openai",
        data_root=str(tmp_path),
    )
    collector.sync_history(db)
    removed.rename(tmp_path / "archived-rollout.jsonl")
    collector.sync_history(db)

    rows = db.conn.execute(
        "SELECT session_id FROM sessions WHERE agent_type = 'codex'"
    ).fetchall()
    assert [row["session_id"] for row in rows] == ["sid-a"]
    stale_states = db.conn.execute(
        "SELECT key FROM sync_state WHERE key LIKE ? AND key LIKE '%sid-b%'",
        (f"{collector.sync_state_prefix}:%",),
    ).fetchall()
    assert stale_states == []

    preserved.rename(tmp_path / "archived-last-rollout.jsonl")
    collector.sync_history(db)
    row = db.conn.execute(
        "SELECT session_id FROM sessions WHERE session_id = 'sid-a'"
    ).fetchone()
    assert row is not None
    db.close()


def test_claude_history_sync_removes_sessions_for_missing_local_files(tmp_path):
    projects = tmp_path / "projects"
    project = projects / "-tmp-project"
    project.mkdir(parents=True)

    def write_session(session_id: str) -> Path:
        path = project / f"{session_id}.jsonl"
        path.write_text("".join(json.dumps(line) + "\n" for line in [
            {
                "timestamp": "2026-04-23T10:00:00Z",
                "type": "user",
                "cwd": "/tmp/project",
                "message": {"content": session_id},
            },
            {
                "timestamp": "2026-04-23T10:00:01Z",
                "type": "assistant",
                "cwd": "/tmp/project",
                "message": {
                    "id": f"msg-{session_id}",
                    "model": "gpt-5.5",
                    "usage": {"input_tokens": 100, "output_tokens": 20},
                },
            },
        ]))
        return path

    preserved = write_session("sid-a")
    removed = write_session("sid-b")
    db = Database(db_path=str(tmp_path / "data.db"))
    collector = ClaudeCodeCollector(
        projects_dir=projects,
        provider="ichat",
        data_root=str(tmp_path),
    )
    collector.sync_history(db)
    removed.rename(tmp_path / "archived-session.jsonl")
    collector.sync_history(db)

    rows = db.conn.execute(
        "SELECT session_id FROM sessions WHERE agent_type = 'claude_code'"
    ).fetchall()
    assert [row["session_id"] for row in rows] == ["sid-a"]
    stale_states = db.conn.execute(
        "SELECT key FROM sync_state WHERE key LIKE ? AND key LIKE '%sid-b%'",
        (f"{collector.sync_state_prefix}:%",),
    ).fetchall()
    assert stale_states == []

    preserved.rename(tmp_path / "archived-last-session.jsonl")
    collector.sync_history(db)
    row = db.conn.execute(
        "SELECT session_id FROM sessions WHERE session_id = 'sid-a'"
    ).fetchone()
    assert row is not None
    db.close()


def test_claude_invalid_index_prevents_destructive_scope_sweep(tmp_path):
    projects = tmp_path / "projects"
    project = projects / "-tmp-project"
    project.mkdir(parents=True)
    (project / "sessions-index.json").write_text("not-json")

    db = Database(db_path=str(tmp_path / "data.db"))
    db.upsert_session(
        "preserved",
        "claude_code",
        provider="ichat",
        data_root=str(tmp_path),
    )
    db.commit()
    ClaudeCodeCollector(
        projects_dir=projects,
        provider="ichat",
        data_root=str(tmp_path),
    ).sync_history(db)

    row = db.conn.execute(
        "SELECT session_id FROM sessions WHERE session_id = 'preserved'"
    ).fetchone()
    assert row is not None
    db.close()


def test_registry_reconciles_removed_scopes_only_after_success():
    class ScopedCollector(BaseCollector):
        agent_type = "codex"
        provider = "openai"
        data_root = "/active"
        sync_state_prefix = "codex_jsonl:v17:active"

        def sync_history(self, db) -> None:
            pass

    db = Database(db_path=":memory:")
    for session_id, data_root in (("active", "/active"), ("stale", "/stale")):
        db.upsert_session(
            session_id,
            "codex",
            provider="openai",
            data_root=data_root,
        )
    db.set_sync_state("codex_jsonl:v17:active:file", "1:1")
    db.set_sync_state("codex_jsonl:v13:stale:file", "1:1")

    registry = CollectorRegistry(reconcile_scopes=True)
    registry.register(ScopedCollector())
    registry.sync_all(db)

    rows = db.conn.execute("SELECT session_id FROM sessions ORDER BY session_id").fetchall()
    assert [row["session_id"] for row in rows] == ["active"]
    states = db.conn.execute(
        "SELECT key FROM sync_state WHERE key LIKE 'codex_jsonl:%'"
    ).fetchall()
    assert [row["key"] for row in states] == ["codex_jsonl:v17:active:file"]
    db.close()


def test_registry_skips_scope_reconciliation_after_collector_error():
    class FailingScopedCollector(BaseCollector):
        agent_type = "codex"
        provider = "openai"
        data_root = "/active"
        sync_state_prefix = "codex_jsonl:v17:active"

        def sync_history(self, db) -> None:
            raise RuntimeError("sync failed")

    db = Database(db_path=":memory:")
    db.upsert_session(
        "stale",
        "codex",
        provider="openai",
        data_root="/stale",
    )
    db.commit()
    registry = CollectorRegistry(reconcile_scopes=True)
    registry.register(FailingScopedCollector())
    registry.sync_all(db)

    row = db.conn.execute(
        "SELECT session_id FROM sessions WHERE session_id = 'stale'"
    ).fetchone()
    assert row is not None
    db.close()


def test_remote_ready_key_tracks_inner_parser_version():
    codex = RemoteHistoryCollector(RemoteSyncTarget(
        remote=RemoteSpec(host="remote", name="remote"),
        agent_type="codex",
        remote_root="~/.codex",
        provider="openai",
        index=0,
    ))
    claude = RemoteHistoryCollector(RemoteSyncTarget(
        remote=RemoteSpec(host="remote", name="remote"),
        agent_type="claude_code",
        remote_root="~/.wcc",
        provider="ichat",
        index=0,
    ))

    assert codex._ready_state_key().startswith(codex._inner.sync_state_prefix)
    assert "codex_jsonl:v17:" in codex._ready_state_key()
    assert claude._ready_state_key().startswith(claude._inner.sync_state_prefix)
    assert "cc_jsonl:v13:" in claude._ready_state_key()


def test_remote_codex_collector_syncs_tarball_into_history(tmp_path):
    import io
    import tarfile

    lines = [
        {
            "timestamp": "2026-04-23T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": "remote-sid", "cwd": "/work/project", "model_provider": "openai"},
        },
        {
            "timestamp": "2026-04-23T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "hello"},
        },
        {
            "timestamp": "2026-04-23T10:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 20}},
            },
        },
    ]
    payload = "".join(json.dumps(line) + "\n" for line in lines).encode()
    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w:gz") as tar:
        info = tarfile.TarInfo("2026/04/23/rollout-remote.jsonl")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    manifest = (
        b"OK\0"
        + f"{len(payload)}\t123\t./2026/04/23/rollout-remote.jsonl".encode()
        + b"\0"
    )
    run_mock = Mock(side_effect=[
        Mock(returncode=0, stdout=manifest, stderr=b""),
        Mock(returncode=0, stdout=tar_bytes.getvalue(), stderr=b""),
    ])
    remote = RemoteSpec(host="remote-dev", name="dev", timeout=9)
    target = RemoteSyncTarget(
        remote=remote,
        agent_type="codex",
        remote_root="~/.codex",
        provider="openai",
        index=0,
    )
    db = Database(db_path=str(tmp_path / "data.db"))
    with patch("agentic_metric.collectors.remote.DATA_DIR", tmp_path / "data"), \
         patch("agentic_metric.collectors.remote.subprocess.run", run_mock):
        RemoteHistoryCollector(target).sync_history(db)

    assert [call.kwargs["timeout"] for call in run_mock.call_args_list] == [9, 9]
    assert run_mock.call_args_list[1].kwargs["input"] == b"2026/04/23/rollout-remote.jsonl\0"
    row = db.conn.execute(
        "SELECT provider, data_root, project_path, input_tokens, output_tokens "
        "FROM sessions WHERE session_id = 'remote-sid'"
    ).fetchone()
    assert dict(row) == {
        "provider": "openai",
        "data_root": "ssh://dev/~/.codex",
        "project_path": "/work/project",
        "input_tokens": 100,
        "output_tokens": 20,
    }
    db.close()


def test_remote_download_tolerates_tar_file_changed_exit_1(tmp_path):
    """A live remote session makes tar exit 1 ('file changed as we read it').

    The streamed archive is still valid, so the session must still be parsed
    into history instead of the whole download being discarded.
    """
    import io
    import tarfile

    lines = [
        {
            "timestamp": "2026-04-23T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": "live-sid", "cwd": "/work/project", "model_provider": "openai"},
        },
        {
            "timestamp": "2026-04-23T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "hello"},
        },
        {
            "timestamp": "2026-04-23T10:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 20}},
            },
        },
    ]
    payload = "".join(json.dumps(line) + "\n" for line in lines).encode()
    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w:gz") as tar:
        info = tarfile.TarInfo("2026/04/23/rollout-remote.jsonl")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    manifest = (
        b"OK\0"
        + f"{len(payload)}\t123\t./2026/04/23/rollout-remote.jsonl".encode()
        + b"\0"
    )
    run_mock = Mock(side_effect=[
        Mock(returncode=0, stdout=manifest, stderr=b""),
        # tar streamed a valid archive but warns + exits 1 on the live file.
        Mock(
            returncode=1,
            stdout=tar_bytes.getvalue(),
            stderr=b"tar: ./2026/04/23/rollout-remote.jsonl: file changed as we read it",
        ),
    ])
    remote = RemoteSpec(host="remote-dev", name="dev", timeout=9)
    target = RemoteSyncTarget(
        remote=remote,
        agent_type="codex",
        remote_root="~/.codex",
        provider="openai",
        index=0,
    )
    db = Database(db_path=str(tmp_path / "data.db"))
    with patch("agentic_metric.collectors.remote.DATA_DIR", tmp_path / "data"), \
         patch("agentic_metric.collectors.remote.subprocess.run", run_mock):
        collector = RemoteHistoryCollector(target)
        collector.sync_history(db)

    assert collector.last_error == ""
    row = db.conn.execute(
        "SELECT input_tokens, output_tokens "
        "FROM sessions WHERE session_id = 'live-sid'"
    ).fetchone()
    assert dict(row) == {"input_tokens": 100, "output_tokens": 20}
    db.close()


def _codex_rollout_tarball(name: str, session_id: str) -> tuple[bytes, bytes]:
    """Return (raw jsonl bytes, gzipped tar bytes) for one remote rollout file."""
    payloads = _codex_rollout_payloads([(name, session_id)])
    return payloads[name], _codex_tarball(payloads)


def _codex_rollout_payloads(entries: list[tuple[str, str]]) -> dict[str, bytes]:
    payloads: dict[str, bytes] = {}
    for name, session_id in entries:
        lines = [
            {
                "timestamp": "2026-04-23T10:00:00Z",
                "type": "session_meta",
                "payload": {"id": session_id, "cwd": "/work/project", "model_provider": "openai"},
            },
            {
                "timestamp": "2026-04-23T10:00:01Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "hello"},
            },
            {
                "timestamp": "2026-04-23T10:00:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 20}},
                },
            },
        ]
        payloads[name] = "".join(json.dumps(line) + "\n" for line in lines).encode()
    return payloads


def _codex_tarball(payloads: dict[str, bytes]) -> bytes:
    """Pack one gzipped tar holding every given member."""
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in payloads.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def test_remote_file_absent_from_the_archive_is_not_recorded(tmp_path):
    """A member the archive did not carry must stay pending, not look current.

    Recording it would let an outdated local copy pass as up to date forever.
    """
    files = [
        ("2026/04/23/rollout-a.jsonl", "sid-a"),
        ("2026/04/23/rollout-b.jsonl", "sid-b"),
    ]
    payloads = _codex_rollout_payloads(files)
    manifest = b"OK\0" + b"".join(
        f"{len(payloads[name])}\t123\t./{name}".encode() + b"\0" for name, _ in files
    )
    # tar delivered only A even though both were requested.
    partial_tar = _codex_tarball({files[0][0]: payloads[files[0][0]]})

    data_dir = tmp_path / "data"
    db = Database(db_path=str(tmp_path / "data.db"))
    target = RemoteSyncTarget(
        remote=RemoteSpec(host="remote-dev", name="dev", timeout=9),
        agent_type="codex",
        remote_root="~/.codex",
        provider="openai",
        index=0,
    )
    db.upsert_session(
        "sid-b",
        "codex",
        provider="openai",
        data_root=target.data_root,
        input_tokens=100,
        output_tokens=20,
    )
    db.commit()
    first_run = Mock(side_effect=[
        Mock(returncode=0, stdout=manifest, stderr=b""),
        Mock(returncode=0, stdout=partial_tar, stderr=b""),
    ])
    with patch("agentic_metric.collectors.remote.DATA_DIR", data_dir), \
         patch("agentic_metric.collectors.remote.subprocess.run", first_run):
        collector = RemoteHistoryCollector(target)
        collector.sync_history(db)
        cache_root = _cache_root_for(target)

    assert "incomplete" in collector.last_error
    saved = json.loads((cache_root / ".remote-manifest.json").read_text())
    assert set(saved) == {files[0][0]}
    assert (cache_root / ".remote-manifest.incomplete").exists()
    ids = {row["session_id"] for row in db.conn.execute("SELECT session_id FROM sessions")}
    assert ids == {"sid-a", "sid-b"}

    # The next sync retries only the file that never arrived.
    second_run = Mock(side_effect=[
        Mock(returncode=0, stdout=manifest, stderr=b""),
        Mock(returncode=0, stdout=_codex_tarball({files[1][0]: payloads[files[1][0]]}), stderr=b""),
    ])
    with patch("agentic_metric.collectors.remote.DATA_DIR", data_dir), \
         patch("agentic_metric.collectors.remote.subprocess.run", second_run):
        collector = RemoteHistoryCollector(target)
        collector.sync_history(db)

    assert [call.kwargs["input"] for call in second_run.call_args_list[1:]] == [
        f"{files[1][0]}\0".encode()
    ]
    assert not (cache_root / ".remote-manifest.incomplete").exists()
    assert json.loads((cache_root / ".remote-manifest.json").read_text()).keys() == {
        files[0][0], files[1][0]
    }
    db.close()


def test_remote_download_splits_large_payload_into_batches(tmp_path):
    """A multi-GB first run must not be one all-or-nothing tar stream.

    Batches are bounded by the remote file sizes in the manifest, so each ssh
    call stays inside the configured timeout.
    """
    files = [
        ("2026/04/23/rollout-a.jsonl", "sid-a"),
        ("2026/04/23/rollout-b.jsonl", "sid-b"),
        ("2026/04/23/rollout-c.jsonl", "sid-c"),
    ]
    big = 100 * 1024 * 1024  # 3 x 100 MB against a 128 MB batch budget
    manifest = b"OK\0" + b"".join(
        f"{big}\t123\t./{name}".encode() + b"\0" for name, _ in files
    )
    tarballs = [_codex_rollout_tarball(name, sid)[1] for name, sid in files]
    run_mock = Mock(side_effect=[
        Mock(returncode=0, stdout=manifest, stderr=b""),
        *[Mock(returncode=0, stdout=tar, stderr=b"") for tar in tarballs],
    ])
    target = RemoteSyncTarget(
        remote=RemoteSpec(host="remote-dev", name="dev", timeout=9),
        agent_type="codex",
        remote_root="~/.codex",
        provider="openai",
        index=0,
    )
    db = Database(db_path=str(tmp_path / "data.db"))
    with patch("agentic_metric.collectors.remote.DATA_DIR", tmp_path / "data"), \
         patch("agentic_metric.collectors.remote.subprocess.run", run_mock):
        collector = RemoteHistoryCollector(target)
        collector.sync_history(db)

    assert collector.last_error == ""
    downloads = [call.kwargs["input"] for call in run_mock.call_args_list[1:]]
    assert downloads == [f"{name}\0".encode() for name, _ in files]
    ids = {row["session_id"] for row in db.conn.execute("SELECT session_id FROM sessions")}
    assert ids == {"sid-a", "sid-b", "sid-c"}
    db.close()


def test_remote_download_timeout_keeps_completed_batches(tmp_path):
    """A timeout mid-download must not throw away the batches already mirrored.

    Without persisted progress a remote too large for one transfer window can
    never catch up: every sync re-downloads from scratch and times out again.
    """
    import subprocess

    files = [
        ("2026/04/23/rollout-a.jsonl", "sid-a"),
        ("2026/04/23/rollout-b.jsonl", "sid-b"),
    ]
    big = 100 * 1024 * 1024
    manifest = b"OK\0" + b"".join(
        f"{big}\t123\t./{name}".encode() + b"\0" for name, _ in files
    )
    tar_a = _codex_rollout_tarball(files[0][0], files[0][1])[1]
    tar_b = _codex_rollout_tarball(files[1][0], files[1][1])[1]

    data_dir = tmp_path / "data"
    db = Database(db_path=str(tmp_path / "data.db"))
    first_run = Mock(side_effect=[
        Mock(returncode=0, stdout=manifest, stderr=b""),
        Mock(returncode=0, stdout=tar_a, stderr=b""),
        subprocess.TimeoutExpired(cmd="ssh", timeout=9),
    ])
    target = RemoteSyncTarget(
        remote=RemoteSpec(host="remote-dev", name="dev", timeout=9),
        agent_type="codex",
        remote_root="~/.codex",
        provider="openai",
        index=0,
    )
    with patch("agentic_metric.collectors.remote.DATA_DIR", data_dir), \
         patch("agentic_metric.collectors.remote.subprocess.run", first_run):
        collector = RemoteHistoryCollector(target)
        collector.sync_history(db)
        cache_root = _cache_root_for(target)

    assert "timed out" in collector.last_error
    saved = json.loads((cache_root / ".remote-manifest.json").read_text())
    assert set(saved) == {files[0][0]}

    second_run = Mock(side_effect=[
        Mock(returncode=0, stdout=manifest, stderr=b""),
        Mock(returncode=0, stdout=tar_b, stderr=b""),
    ])
    with patch("agentic_metric.collectors.remote.DATA_DIR", data_dir), \
         patch("agentic_metric.collectors.remote.subprocess.run", second_run):
        collector = RemoteHistoryCollector(target)
        collector.sync_history(db)

    assert collector.last_error == ""
    # Only the missing file is fetched the second time.
    assert [call.kwargs["input"] for call in second_run.call_args_list[1:]] == [
        f"{files[1][0]}\0".encode()
    ]
    ids = {row["session_id"] for row in db.conn.execute("SELECT session_id FROM sessions")}
    assert ids == {"sid-a", "sid-b"}
    db.close()


def test_remote_first_batch_failure_keeps_cache_state_unknown(tmp_path):
    """No completed batch means no manifest: an empty one would claim the remote
    is empty and skip stale archiving / session purging on the next sync."""
    import subprocess

    name, sid = "2026/04/23/rollout-a.jsonl", "sid-a"
    payload, _ = _codex_rollout_tarball(name, sid)
    manifest = b"OK\0" + f"{len(payload)}\t123\t./{name}".encode() + b"\0"
    run_mock = Mock(side_effect=[
        Mock(returncode=0, stdout=manifest, stderr=b""),
        subprocess.TimeoutExpired(cmd="ssh", timeout=9),
    ])
    target = RemoteSyncTarget(
        remote=RemoteSpec(host="remote-dev", name="dev", timeout=9),
        agent_type="codex",
        remote_root="~/.codex",
        provider="openai",
        index=0,
    )
    db = Database(db_path=str(tmp_path / "data.db"))
    with patch("agentic_metric.collectors.remote.DATA_DIR", tmp_path / "data"), \
         patch("agentic_metric.collectors.remote.subprocess.run", run_mock):
        collector = RemoteHistoryCollector(target)
        collector.sync_history(db)
        cache_root = _cache_root_for(target)

    assert "timed out" in collector.last_error
    assert not (cache_root / ".remote-manifest.json").exists()
    db.close()


def test_remote_failed_batch_is_refetched_after_extraction_error(tmp_path):
    """A batch that failed to extract must not be recorded as mirrored.

    Its files may be truncated on disk, so the next sync has to download them
    again even though the remote metadata is unchanged.
    """
    files = [
        ("2026/04/23/rollout-a.jsonl", "sid-a"),
        ("2026/04/23/rollout-b.jsonl", "sid-b"),
    ]
    big = 100 * 1024 * 1024
    manifest = b"OK\0" + b"".join(
        f"{big}\t123\t./{name}".encode() + b"\0" for name, _ in files
    )
    tar_a = _codex_rollout_tarball(files[0][0], files[0][1])[1]
    tar_b = _codex_rollout_tarball(files[1][0], files[1][1])[1]

    data_dir = tmp_path / "data"
    db = Database(db_path=str(tmp_path / "data.db"))
    target = RemoteSyncTarget(
        remote=RemoteSpec(host="remote-dev", name="dev", timeout=9),
        agent_type="codex",
        remote_root="~/.codex",
        provider="openai",
        index=0,
    )
    real_extract = remote_extract_tarball
    calls = {"n": 0}

    def flaky_extract(data, dest):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full mid-extract")
        return real_extract(data, dest)

    first_run = Mock(side_effect=[
        Mock(returncode=0, stdout=manifest, stderr=b""),
        Mock(returncode=0, stdout=tar_a, stderr=b""),
        Mock(returncode=0, stdout=tar_b, stderr=b""),
    ])
    with patch("agentic_metric.collectors.remote.DATA_DIR", data_dir), \
         patch("agentic_metric.collectors.remote.subprocess.run", first_run), \
         patch("agentic_metric.collectors.remote._extract_tarball", flaky_extract):
        collector = RemoteHistoryCollector(target)
        collector.sync_history(db)
        cache_root = _cache_root_for(target)

    assert collector.last_error
    saved = json.loads((cache_root / ".remote-manifest.json").read_text())
    assert set(saved) == {files[0][0]}

    second_run = Mock(side_effect=[
        Mock(returncode=0, stdout=manifest, stderr=b""),
        Mock(returncode=0, stdout=tar_b, stderr=b""),
    ])
    with patch("agentic_metric.collectors.remote.DATA_DIR", data_dir), \
         patch("agentic_metric.collectors.remote.subprocess.run", second_run):
        collector = RemoteHistoryCollector(target)
        collector.sync_history(db)

    assert collector.last_error == ""
    assert [call.kwargs["input"] for call in second_run.call_args_list[1:]] == [
        f"{files[1][0]}\0".encode()
    ]
    db.close()


def test_remote_unsplittable_batch_does_not_starve_later_files(tmp_path):
    """One file too big for the timeout window must not block the rest.

    A single-file batch cannot be split further, so it is skipped and reported;
    the files behind it still land in the cache and the next sync retries only
    the skipped one.
    """
    import subprocess

    files = [
        ("2026/04/23/rollout-huge.jsonl", "sid-huge"),
        ("2026/04/23/rollout-small.jsonl", "sid-small"),
    ]
    big = 200 * 1024 * 1024  # each file gets its own batch
    manifest = b"OK\0" + b"".join(
        f"{big}\t123\t./{name}".encode() + b"\0" for name, _ in files
    )
    tar_small = _codex_rollout_tarball(files[1][0], files[1][1])[1]

    data_dir = tmp_path / "data"
    db = Database(db_path=str(tmp_path / "data.db"))
    target = RemoteSyncTarget(
        remote=RemoteSpec(host="remote-dev", name="dev", timeout=9),
        agent_type="codex",
        remote_root="~/.codex",
        provider="openai",
        index=0,
    )
    first_run = Mock(side_effect=[
        Mock(returncode=0, stdout=manifest, stderr=b""),
        subprocess.TimeoutExpired(cmd="ssh", timeout=9),
        Mock(returncode=0, stdout=tar_small, stderr=b""),
    ])
    with patch("agentic_metric.collectors.remote.DATA_DIR", data_dir), \
         patch("agentic_metric.collectors.remote.subprocess.run", first_run):
        collector = RemoteHistoryCollector(target)
        collector.sync_history(db)
        cache_root = _cache_root_for(target)

    assert "timed out" in collector.last_error
    # The small file was fetched even though the huge one came first.
    assert (cache_root / "sessions" / files[1][0]).exists()
    saved = json.loads((cache_root / ".remote-manifest.json").read_text())
    assert set(saved) == {files[1][0]}

    second_run = Mock(side_effect=[
        Mock(returncode=0, stdout=manifest, stderr=b""),
        Mock(returncode=0, stdout=_codex_rollout_tarball(*files[0])[1], stderr=b""),
    ])
    with patch("agentic_metric.collectors.remote.DATA_DIR", data_dir), \
         patch("agentic_metric.collectors.remote.subprocess.run", second_run):
        collector = RemoteHistoryCollector(target)
        collector.sync_history(db)

    assert collector.last_error == ""
    assert [call.kwargs["input"] for call in second_run.call_args_list[1:]] == [
        f"{files[0][0]}\0".encode()
    ]
    db.close()


def test_remote_multi_file_batch_failure_stops_the_download(tmp_path):
    """A failure on a splittable batch means the remote is unhealthy: stop.

    Continuing would spend one full timeout per remaining batch.
    """
    import subprocess

    files = [(f"2026/04/23/rollout-{i}.jsonl", f"sid-{i}") for i in range(4)]
    # 60 MB each against a 128 MB budget: two files per batch, so the failing
    # batch is splittable rather than an unavoidable single-file one.
    manifest = b"OK\0" + b"".join(
        f"{60 * 1024 * 1024}\t123\t./{name}".encode() + b"\0" for name, _ in files
    )
    run_mock = Mock(side_effect=[
        Mock(returncode=0, stdout=manifest, stderr=b""),
        subprocess.TimeoutExpired(cmd="ssh", timeout=9),
        Mock(returncode=0, stdout=b"", stderr=b""),
    ])
    target = RemoteSyncTarget(
        remote=RemoteSpec(host="remote-dev", name="dev", timeout=9),
        agent_type="codex",
        remote_root="~/.codex",
        provider="openai",
        index=0,
    )
    db = Database(db_path=str(tmp_path / "data.db"))
    with patch("agentic_metric.collectors.remote.DATA_DIR", tmp_path / "data"), \
         patch("agentic_metric.collectors.remote.subprocess.run", run_mock):
        collector = RemoteHistoryCollector(target)
        collector.sync_history(db)

    assert "timed out" in collector.last_error
    # Manifest call plus exactly one download attempt: no further batches.
    assert run_mock.call_count == 2
    db.close()


def test_remote_extraction_failure_is_never_skipped(tmp_path):
    """A local extraction failure must stop the download, not skip a batch.

    Nothing another batch does can fix a full disk or an unreadable archive.
    """
    files = [
        ("2026/04/23/rollout-a.jsonl", "sid-a"),
        ("2026/04/23/rollout-b.jsonl", "sid-b"),
    ]
    big = 200 * 1024 * 1024  # one single-file batch per file
    manifest = b"OK\0" + b"".join(
        f"{big}\t123\t./{name}".encode() + b"\0" for name, _ in files
    )
    run_mock = Mock(side_effect=[
        Mock(returncode=0, stdout=manifest, stderr=b""),
        Mock(returncode=0, stdout=_codex_rollout_tarball(*files[0])[1], stderr=b""),
        Mock(returncode=0, stdout=_codex_rollout_tarball(*files[1])[1], stderr=b""),
    ])
    target = RemoteSyncTarget(
        remote=RemoteSpec(host="remote-dev", name="dev", timeout=9),
        agent_type="codex",
        remote_root="~/.codex",
        provider="openai",
        index=0,
    )
    db = Database(db_path=str(tmp_path / "data.db"))
    with patch("agentic_metric.collectors.remote.DATA_DIR", tmp_path / "data"), \
         patch("agentic_metric.collectors.remote.subprocess.run", run_mock), \
         patch(
             "agentic_metric.collectors.remote._extract_tarball",
             side_effect=OSError("disk full"),
         ):
        collector = RemoteHistoryCollector(target)
        collector.sync_history(db)

    assert "disk full" in collector.last_error
    # Manifest call plus the one failed download: the second file is not tried.
    assert run_mock.call_count == 2
    db.close()


def test_remote_download_stops_after_too_many_skipped_batches(tmp_path):
    """Skipping is bounded: a dead connection must not burn a timeout per batch."""
    import subprocess

    files = [(f"2026/04/23/rollout-{i}.jsonl", f"sid-{i}") for i in range(8)]
    big = 200 * 1024 * 1024  # every file becomes its own batch
    manifest = b"OK\0" + b"".join(
        f"{big}\t123\t./{name}".encode() + b"\0" for name, _ in files
    )
    run_mock = Mock(side_effect=[
        Mock(returncode=0, stdout=manifest, stderr=b""),
        *[subprocess.TimeoutExpired(cmd="ssh", timeout=9) for _ in files],
    ])
    target = RemoteSyncTarget(
        remote=RemoteSpec(host="remote-dev", name="dev", timeout=9),
        agent_type="codex",
        remote_root="~/.codex",
        provider="openai",
        index=0,
    )
    db = Database(db_path=str(tmp_path / "data.db"))
    with patch("agentic_metric.collectors.remote.DATA_DIR", tmp_path / "data"), \
         patch("agentic_metric.collectors.remote.subprocess.run", run_mock):
        collector = RemoteHistoryCollector(target)
        collector.sync_history(db)

    assert "timed out" in collector.last_error
    # Manifest call, three skipped batches, then one fatal one.
    assert run_mock.call_count == 1 + _MAX_SKIPPED_BATCHES + 1
    db.close()


def test_remote_skipped_file_deleted_upstream_is_still_archived(tmp_path):
    """A partial manifest must not become authoritative.

    Sequence: a file is mirrored, then changes and gets skipped, then disappears
    from the remote. Its outdated local copy must be archived instead of staying
    active just because the partial manifest matched the shrunken remote.
    """
    import subprocess

    a_name, b_name = "2026/04/23/rollout-a.jsonl", "2026/04/23/rollout-b.jsonl"
    a_payload, _ = _codex_rollout_tarball(a_name, "sid-a")
    b_payload, b_tar = _codex_rollout_tarball(b_name, "sid-b")
    huge = 200 * 1024 * 1024

    def manifest_for(entries: list[tuple[str, int, int]]) -> bytes:
        return b"OK\0" + b"".join(
            f"{size}\t{mtime}\t./{name}".encode() + b"\0" for name, size, mtime in entries
        )

    full = manifest_for([(a_name, len(a_payload), 111), (b_name, len(b_payload), 222)])
    a_changed = manifest_for([(a_name, huge, 333), (b_name, len(b_payload), 222)])
    without_a = manifest_for([(b_name, len(b_payload), 222)])

    data_dir = tmp_path / "data"
    db = Database(db_path=str(tmp_path / "data.db"))
    target = RemoteSyncTarget(
        remote=RemoteSpec(host="remote-dev", name="dev", timeout=9),
        agent_type="codex",
        remote_root="~/.codex",
        provider="openai",
        index=0,
    )

    def run_sync(side_effect):
        with patch("agentic_metric.collectors.remote.DATA_DIR", data_dir), \
             patch("agentic_metric.collectors.remote.subprocess.run", Mock(side_effect=side_effect)):
            collector = RemoteHistoryCollector(target)
            collector.sync_history(db)
            return collector, _cache_root_for(target)

    # 1. Healthy mirror of both files: one batch carrying both members.
    collector, cache_root = run_sync([
        Mock(returncode=0, stdout=full, stderr=b""),
        Mock(returncode=0, stdout=_codex_tarball({a_name: a_payload, b_name: b_payload}), stderr=b""),
    ])
    assert collector.last_error == ""
    assert (cache_root / "sessions" / a_name).exists()
    assert (cache_root / "sessions" / b_name).exists()
    assert not (cache_root / ".remote-manifest.incomplete").exists()

    # 2. A grew too large to transfer and is skipped; B is refetched fine.
    collector, cache_root = run_sync([
        Mock(returncode=0, stdout=a_changed, stderr=b""),
        subprocess.TimeoutExpired(cmd="ssh", timeout=9),
        Mock(returncode=0, stdout=b_tar, stderr=b""),
    ])
    assert "timed out" in collector.last_error
    assert json.loads((cache_root / ".remote-manifest.json").read_text()).keys() == {b_name}
    assert (cache_root / ".remote-manifest.incomplete").exists()
    # The previous copy of A is still sitting in the active cache.
    assert (cache_root / "sessions" / a_name).exists()

    # 3. A disappears from the remote: the stale local copy must be archived.
    collector, cache_root = run_sync([Mock(returncode=0, stdout=without_a, stderr=b"")])
    assert collector.last_error == ""
    assert not (cache_root / "sessions" / a_name).exists()
    assert list((cache_root / ".stale").rglob("rollout-a*.jsonl"))
    assert not (cache_root / ".remote-manifest.incomplete").exists()
    db.close()


def test_remote_missing_path_does_not_parse_stale_cache(tmp_path):
    remote = RemoteSpec(host="remote-dev", name="dev")
    target = RemoteSyncTarget(
        remote=remote,
        agent_type="codex",
        remote_root="~/.codex",
        provider="openai",
        index=0,
    )
    stale_lines = [
        {
            "timestamp": "2026-04-23T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": "stale-sid", "cwd": "/work/project", "model_provider": "openai"},
        },
        {
            "timestamp": "2026-04-23T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "stale"},
        },
        {
            "timestamp": "2026-04-23T10:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 20}},
            },
        },
    ]
    completed = Mock(returncode=0, stdout=b"MISSING\0", stderr=b"")
    db = Database(db_path=str(tmp_path / "data.db"))

    with patch("agentic_metric.collectors.remote.DATA_DIR", tmp_path / "data"), \
         patch("agentic_metric.collectors.remote.subprocess.run", return_value=completed):
        cache_file = (
            _cache_root_for(target)
            / "sessions"
            / "2026"
            / "04"
            / "23"
            / "rollout-stale.jsonl"
        )
        cache_file.parent.mkdir(parents=True)
        cache_file.write_text("".join(json.dumps(line) + "\n" for line in stale_lines))

        collector = RemoteHistoryCollector(target)
        collector.sync_history(db)

    assert "remote path not found" in collector.last_error
    assert db.conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE session_id = 'stale-sid'"
    ).fetchone()["n"] == 0
    db.close()


def test_remote_unchanged_manifest_skips_download(tmp_path):
    remote = RemoteSpec(host="remote-dev", name="dev")
    target = RemoteSyncTarget(
        remote=remote,
        agent_type="codex",
        remote_root="~/.codex",
        provider="openai",
        index=0,
    )
    rel = "2026/04/23/rollout-remote.jsonl"
    manifest = b"OK\0" + b"100\t123\t./2026/04/23/rollout-remote.jsonl\0"

    with patch("agentic_metric.collectors.remote.DATA_DIR", tmp_path / "data"), \
         patch(
             "agentic_metric.collectors.remote.subprocess.run",
             return_value=Mock(returncode=0, stdout=manifest, stderr=b""),
         ) as run_mock:
        cache_file = _cache_root_for(target) / "sessions" / rel
        cache_file.parent.mkdir(parents=True)
        cache_file.write_text("{}\n")
        manifest_file = _cache_root_for(target) / ".remote-manifest.json"
        manifest_file.write_text(
            json.dumps({rel: {"size": "100", "mtime": "123"}}) + "\n"
        )

        db = Database(db_path=str(tmp_path / "data.db"))
        RemoteHistoryCollector(target).sync_history(db)
        db.close()

    assert run_mock.call_count == 1


def test_remote_unchanged_manifest_skips_session_purge(tmp_path):
    remote = RemoteSpec(host="remote-dev", name="dev")
    target = RemoteSyncTarget(
        remote=remote,
        agent_type="codex",
        remote_root="~/.codex",
        provider="openai",
        index=0,
    )
    rel = "2026/04/23/rollout-remote.jsonl"
    manifest = b"OK\0" + b"100\t123\t./2026/04/23/rollout-remote.jsonl\0"

    with patch("agentic_metric.collectors.remote.DATA_DIR", tmp_path / "data"), \
         patch(
             "agentic_metric.collectors.remote.subprocess.run",
             return_value=Mock(returncode=0, stdout=manifest, stderr=b""),
         ), \
         patch(
             "agentic_metric.collectors.remote._purge_removed_remote_sessions"
         ) as purge_mock:
        cache_file = _cache_root_for(target) / "sessions" / rel
        cache_file.parent.mkdir(parents=True)
        cache_file.write_text("{}\n")
        manifest_file = _cache_root_for(target) / ".remote-manifest.json"
        manifest_file.write_text(
            json.dumps({rel: {"size": "100", "mtime": "123"}}) + "\n"
        )

        db = Database(db_path=str(tmp_path / "data.db"))
        collector = RemoteHistoryCollector(target)
        collector.sync_history(db)
        db.close()

    assert collector.last_error == ""
    purge_mock.assert_not_called()


def test_remote_unchanged_manifest_skips_inner_reparse(tmp_path):
    """Second sync with identical manifest must not rescan the cache tree."""
    import io
    import tarfile

    remote = RemoteSpec(host="remote-dev", name="dev")
    target = RemoteSyncTarget(
        remote=remote,
        agent_type="codex",
        remote_root="~/.codex",
        provider="openai",
        index=0,
    )
    lines = [
        {
            "timestamp": "2026-04-23T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": "ready-sid", "cwd": "/work/project", "model_provider": "openai"},
        },
        {
            "timestamp": "2026-04-23T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "hello"},
        },
        {
            "timestamp": "2026-04-23T10:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 20}},
            },
        },
    ]
    payload = "".join(json.dumps(line) + "\n" for line in lines).encode()
    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w:gz") as tar:
        info = tarfile.TarInfo("2026/04/23/rollout-ready.jsonl")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    manifest = (
        b"OK\0"
        + f"{len(payload)}\t123\t./2026/04/23/rollout-ready.jsonl".encode()
        + b"\0"
    )

    db = Database(db_path=str(tmp_path / "data.db"))
    with patch("agentic_metric.collectors.remote.DATA_DIR", tmp_path / "data"), \
         patch(
             "agentic_metric.collectors.remote.subprocess.run",
             side_effect=[
                 Mock(returncode=0, stdout=manifest, stderr=b""),
                 Mock(returncode=0, stdout=tar_bytes.getvalue(), stderr=b""),
                 Mock(returncode=0, stdout=manifest, stderr=b""),
             ],
         ):
        RemoteHistoryCollector(target).sync_history(db)
        with patch.object(CodexCollector, "sync_history") as inner_mock:
            RemoteHistoryCollector(target).sync_history(db)

    inner_mock.assert_not_called()
    row = db.conn.execute(
        "SELECT input_tokens FROM sessions WHERE session_id = 'ready-sid'"
    ).fetchone()
    assert row["input_tokens"] == 100
    db.close()


def test_remote_fractional_mtime_matches_previous_integer_manifest(tmp_path):
    """GNU find %T@ fractional mtimes must not re-flag unchanged files."""
    remote = RemoteSpec(host="remote-dev", name="dev")
    target = RemoteSyncTarget(
        remote=remote,
        agent_type="codex",
        remote_root="~/.codex",
        provider="openai",
        index=0,
    )
    rel = "2026/04/23/rollout-remote.jsonl"
    fractional = b"OK\0" + b"100\t123.4567890\t./2026/04/23/rollout-remote.jsonl\0"

    with patch("agentic_metric.collectors.remote.DATA_DIR", tmp_path / "data"), \
         patch(
             "agentic_metric.collectors.remote.subprocess.run",
             return_value=Mock(returncode=0, stdout=fractional, stderr=b""),
         ) as run_mock:
        cache_file = _cache_root_for(target) / "sessions" / rel
        cache_file.parent.mkdir(parents=True)
        cache_file.write_text("{}\n")
        manifest_file = _cache_root_for(target) / ".remote-manifest.json"
        manifest_file.write_text(
            json.dumps({rel: {"size": "100", "mtime": "123"}}) + "\n"
        )

        db = Database(db_path=str(tmp_path / "data.db"))
        RemoteHistoryCollector(target).sync_history(db)
        db.close()

    # Only the manifest call: no tar download of "changed" files.
    assert run_mock.call_count == 1


def test_remote_corrupted_manifest_still_purges_and_archives(tmp_path):
    """A corrupted saved manifest means unknown state; purge must still run."""
    remote = RemoteSpec(host="remote-dev", name="dev")
    target = RemoteSyncTarget(
        remote=remote,
        agent_type="codex",
        remote_root="~/.codex",
        provider="openai",
        index=0,
    )
    manifest = b"OK\0"  # remote now empty

    with patch("agentic_metric.collectors.remote.DATA_DIR", tmp_path / "data"), \
         patch(
             "agentic_metric.collectors.remote.subprocess.run",
             return_value=Mock(returncode=0, stdout=manifest, stderr=b""),
         ), \
         patch(
             "agentic_metric.collectors.remote._purge_removed_remote_sessions"
         ) as purge_mock:
        cache_root = _cache_root_for(target)
        stray = cache_root / "sessions" / "2026" / "04" / "23" / "rollout-stray.jsonl"
        stray.parent.mkdir(parents=True)
        stray.write_text("{}\n")
        (cache_root / ".remote-manifest.json").write_text("{not json")

        db = Database(db_path=str(tmp_path / "data.db"))
        RemoteHistoryCollector(target).sync_history(db)
        db.close()

    purge_mock.assert_called_once()
    assert not stray.exists()  # archived into .stale


def test_registry_prepares_remote_caches_before_serial_sync():
    calls: list[str] = []

    class FakeRemoteCollector(BaseCollector):
        def __init__(self, name: str) -> None:
            self._name = name
            self.last_error = ""

        @property
        def agent_type(self) -> str:
            return f"remote-{self._name}"

        def prepare_cache(self) -> None:
            calls.append(f"prepare-{self._name}")

        def sync_history(self, db) -> None:
            calls.append(f"sync-{self._name}")

    registry = CollectorRegistry()
    registry.register(FakeRemoteCollector("a"))
    registry.register(FakeRemoteCollector("b"))
    registry.sync_all(db=Mock())

    assert sorted(c for c in calls if c.startswith("prepare-")) == ["prepare-a", "prepare-b"]
    assert calls[-2:] == ["sync-a", "sync-b"]
    assert registry.get_sync_errors() == []


def test_registry_parses_local_roots_while_remotes_mirror():
    """Local parsing must overlap remote mirroring, not wait for it.

    The remote prepare blocks until a local collector has started parsing, so a
    serial implementation would hit the timeout instead of finishing.
    """
    import threading

    local_started = threading.Event()
    overlapped: list[bool] = []

    class FakeLocalCollector(BaseCollector):
        @property
        def agent_type(self) -> str:
            return "local"

        def sync_history(self, db) -> None:
            local_started.set()

    class FakeRemoteCollector(BaseCollector):
        def __init__(self) -> None:
            self.last_error = ""

        @property
        def agent_type(self) -> str:
            return "remote"

        def prepare_cache(self) -> None:
            overlapped.append(local_started.wait(timeout=5))

        def sync_history(self, db) -> None:
            pass

    registry = CollectorRegistry()
    registry.register(FakeRemoteCollector())
    registry.register(FakeLocalCollector())
    registry.sync_all(db=Mock())

    assert overlapped == [True]
    assert registry.get_sync_errors() == []


def test_local_time_bucket_matches_uncached_conversion():
    """The per-minute cache must never merge instants from different hours.

    Half-hour offsets and DST shifts mean "same source hour" is not one local
    hour, so the cache key is the parsed instant truncated to the minute.
    """
    from datetime import datetime

    from agentic_metric.collectors import local_time_bucket

    samples = [
        "2024-01-01T00:10:00+05:30",
        "2024-01-01T00:40:00+05:30",
        "2024-10-05T15:20:00Z",
        "2024-10-05T15:40:00Z",
        "2026-07-28T03:14:15.123456Z",
        "2026-07-28T03:14:59Z",
        "2026-07-28T03:15:00Z",
        "2026-07-28T03:14:15-05:00",
        "2026-07-28T03:14:15",
    ]
    for ts in samples:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        assert local_time_bucket(ts) == (dt.strftime("%Y-%m-%d"), dt.hour), ts
    # Repeat calls come from the cache and must not change.
    for ts in samples:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        assert local_time_bucket(ts) == (dt.strftime("%Y-%m-%d"), dt.hour), ts


def test_local_time_bucket_falls_back_for_unparseable_timestamps():
    """Malformed input keeps the old fallback and never reads a cached bucket."""
    from agentic_metric.collectors import local_time_bucket

    assert local_time_bucket("2026-07-28T03:14:9XZ") == ("2026-07-28", 0)
    assert local_time_bucket("2026-07-28T03:99:99Z") == ("2026-07-28", 0)
    assert local_time_bucket("nonsense") == ("", 0)
    assert local_time_bucket("") == ("", 0)


def test_registry_reports_prepare_cache_exceptions_without_aborting(tmp_path):
    """A remote whose mirroring raises must not stop the rest of the sync."""

    class ExplodingRemote(BaseCollector):
        @property
        def agent_type(self) -> str:
            return "remote-boom"

        def prepare_cache(self) -> None:
            raise RuntimeError("ssh exploded")

        def sync_history(self, db) -> None:
            pass

    synced: list[str] = []

    class FakeLocalCollector(BaseCollector):
        @property
        def agent_type(self) -> str:
            return "local"

        def sync_history(self, db) -> None:
            synced.append("local")

    registry = CollectorRegistry()
    registry.register(ExplodingRemote())
    registry.register(FakeLocalCollector())
    registry.sync_all(db=Mock())

    assert synced == ["local"]
    assert registry.get_sync_errors() == ["remote-boom: ssh exploded"]


def test_remote_empty_codex_inventory_archives_without_purging_usage(tmp_path):
    remote = RemoteSpec(host="remote-dev", name="dev")
    target = RemoteSyncTarget(
        remote=remote,
        agent_type="codex",
        remote_root="~/.codex",
        provider="openai",
        index=0,
    )
    stale_lines = [
        {
            "timestamp": "2026-04-23T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": "removed-sid", "cwd": "/work/project", "model_provider": "openai"},
        },
        {
            "timestamp": "2026-04-23T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "removed"},
        },
        {
            "timestamp": "2026-04-23T10:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 20}},
            },
        },
    ]
    manifest = b"OK\0"

    with patch("agentic_metric.collectors.remote.DATA_DIR", tmp_path / "data"), \
         patch(
             "agentic_metric.collectors.remote.subprocess.run",
             return_value=Mock(returncode=0, stdout=manifest, stderr=b""),
         ):
        cache_file = (
            _cache_root_for(target)
            / "sessions"
            / "2026"
            / "04"
            / "23"
            / "rollout-removed.jsonl"
        )
        cache_file.parent.mkdir(parents=True)
        cache_file.write_text("".join(json.dumps(line) + "\n" for line in stale_lines))

        db = Database(db_path=str(tmp_path / "data.db"))
        db.upsert_session(
            "removed-sid",
            "codex",
            provider="openai",
            data_root="ssh://dev/~/.codex",
            project_path="/work/project",
            model="gpt-5.5",
            input_tokens=100,
            output_tokens=20,
            estimated_cost_usd=0.001,
        )
        db.replace_session_usage(
            "removed-sid",
            "codex",
            [{
                "usage_date": "2026-04-23",
                "usage_hour": 10,
                "model": "gpt-5.5",
                "input_tokens": 100,
                "output_tokens": 20,
                "estimated_cost_usd": 0.001,
            }],
            provider="openai",
            data_root="ssh://dev/~/.codex",
        )
        db.commit()
        RemoteHistoryCollector(target).sync_history(db)

        assert not cache_file.exists()
        assert (
            _cache_root_for(target)
            / ".stale"
            / "sessions"
            / "2026"
            / "04"
            / "23"
            / "rollout-removed.jsonl"
        ).exists()
        assert db.conn.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE session_id = 'removed-sid'"
        ).fetchone()["n"] == 1
        assert db.conn.execute(
            "SELECT COUNT(*) AS n FROM session_usage WHERE session_id = 'removed-sid'"
        ).fetchone()["n"] == 1
        db.close()


def test_remote_empty_claude_inventory_archives_without_purging_usage(tmp_path):
    remote = RemoteSpec(host="remote-dev", name="dev")
    target = RemoteSyncTarget(
        remote=remote,
        agent_type="claude_code",
        remote_root="~/.wcc",
        provider="ichat",
        index=0,
    )
    manifest = b"OK\0"

    with patch("agentic_metric.collectors.remote.DATA_DIR", tmp_path / "data"), \
         patch(
             "agentic_metric.collectors.remote.subprocess.run",
             return_value=Mock(returncode=0, stdout=manifest, stderr=b""),
         ):
        cache_file = (
            _cache_root_for(target)
            / "projects"
            / "-work-project"
            / "removed-claude.jsonl"
        )
        cache_file.parent.mkdir(parents=True)
        cache_file.write_text(json.dumps({
            "timestamp": "2026-04-23T10:00:00Z",
            "type": "user",
            "message": {"content": "removed"},
        }) + "\n")

        db = Database(db_path=str(tmp_path / "data.db"))
        db.upsert_session(
            "removed-claude",
            "claude_code",
            provider="ichat",
            data_root="ssh://dev/~/.wcc",
            project_path="/work/project",
            model="claude-opus-4-8",
            input_tokens=100,
            output_tokens=20,
            estimated_cost_usd=0.001,
        )
        db.replace_session_usage(
            "removed-claude",
            "claude_code",
            [{
                "usage_date": "2026-04-23",
                "usage_hour": 10,
                "model": "claude-opus-4-8",
                "input_tokens": 100,
                "output_tokens": 20,
                "estimated_cost_usd": 0.001,
            }],
            provider="ichat",
            data_root="ssh://dev/~/.wcc",
        )
        db.commit()

        RemoteHistoryCollector(target).sync_history(db)

        assert not cache_file.exists()
        assert (
            _cache_root_for(target)
            / ".stale"
            / "projects"
            / "-work-project"
            / "removed-claude.jsonl"
        ).exists()
        assert db.conn.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE session_id = 'removed-claude'"
        ).fetchone()["n"] == 1
        assert db.conn.execute(
            "SELECT COUNT(*) AS n FROM session_usage WHERE session_id = 'removed-claude'"
        ).fetchone()["n"] == 1
        db.close()


def test_remote_stale_archives_age_out(tmp_path):
    from agentic_metric.collectors.remote import _prune_old_stale_archives

    stale_root = tmp_path / ".stale" / "sessions"
    stale_root.mkdir(parents=True)
    old_file = stale_root / "old.jsonl"
    old_file.write_text("{}\n")
    fresh_file = stale_root / "fresh.jsonl"
    fresh_file.write_text("{}\n")
    expired = time.time() - 40 * 86400
    os.utime(old_file, (expired, expired))

    reclaimed = _prune_old_stale_archives(tmp_path, retention_days=30)

    assert reclaimed == 3
    assert not old_file.exists()
    assert fresh_file.exists()

    os.utime(fresh_file, (expired, expired))
    _prune_old_stale_archives(tmp_path, retention_days=30)
    assert not (tmp_path / ".stale").exists()


def test_remote_cache_prune_removes_orphans_and_stale_only(tmp_path):
    from agentic_metric.collectors.remote import prune_remote_cache, remote_cache_report

    remote = RemoteSpec(
        host="remote-dev",
        name="dev",
        collectors={
            "codex": [RemoteCollectorRoot(path="~/.codex", provider="openai")],
            "claude_code": [],
        },
    )
    target = RemoteSyncTarget(
        remote=remote,
        agent_type="codex",
        remote_root="~/.codex",
        provider="openai",
        index=0,
    )

    with patch("agentic_metric.collectors.remote.DATA_DIR", tmp_path / "data"), \
         patch("agentic_metric.config.get_remote_specs", return_value=[remote]):
        active_root = _cache_root_for(target)
        active_file = active_root / "sessions" / "keep.jsonl"
        active_file.parent.mkdir(parents=True)
        active_file.write_text("{}\n")
        stale_file = active_root / ".stale" / "sessions" / "gone.jsonl"
        stale_file.parent.mkdir(parents=True)
        stale_file.write_text("stale-data\n")

        orphan_root = tmp_path / "data" / "remote-cache" / "0123abcd456789ef"
        orphan_file = orphan_root / "sessions" / "orphan.jsonl"
        orphan_file.parent.mkdir(parents=True)
        orphan_file.write_text("orphan-data\n")

        report = remote_cache_report()
        by_orphan = {entry["is_orphan"]: entry for entry in report["entries"]}
        assert by_orphan[False]["owner"].startswith("dev/codex/")
        assert by_orphan[False]["total_bytes"] is None  # active size not computed
        assert by_orphan[True]["total_bytes"] == orphan_file.stat().st_size
        assert report["reclaimable_bytes"] == (
            orphan_file.stat().st_size + stale_file.stat().st_size
        )

        dry = prune_remote_cache(dry_run=True)
        assert dry["reclaimed_bytes"] == report["reclaimable_bytes"]
        assert orphan_file.exists() and stale_file.exists()

        result = prune_remote_cache()
        assert result["reclaimed_bytes"] == report["reclaimable_bytes"]
        assert {r["kind"] for r in result["removed"]} == {"orphan", "stale"}
        assert not orphan_root.exists()
        assert not (active_root / ".stale").exists()
        assert active_file.exists()


def test_remote_sync_prunes_expired_stale_archives(tmp_path):
    """A sync that changes the cache also ages out old stale archives."""
    remote = RemoteSpec(host="remote-dev", name="dev")
    target = RemoteSyncTarget(
        remote=remote,
        agent_type="codex",
        remote_root="~/.codex",
        provider="openai",
        index=0,
    )
    # Remote reports no files while the saved manifest still lists one:
    # the cache changed (removal) without any download round-trip.
    manifest = b"OK\0"

    with patch("agentic_metric.collectors.remote.DATA_DIR", tmp_path / "data"), \
         patch(
             "agentic_metric.collectors.remote.subprocess.run",
             return_value=Mock(returncode=0, stdout=manifest, stderr=b""),
         ):
        cache_root = _cache_root_for(target)
        cache_root.mkdir(parents=True)
        (cache_root / ".remote-manifest.json").write_text(
            json.dumps({"2026/04/23/rollout-remote.jsonl": {"size": "100", "mtime": "123"}}) + "\n"
        )
        expired_file = cache_root / ".stale" / "sessions" / "ancient.jsonl"
        expired_file.parent.mkdir(parents=True)
        expired_file.write_text("{}\n")
        expired = time.time() - 40 * 86400
        os.utime(expired_file, (expired, expired))

        db = Database(db_path=str(tmp_path / "data.db"))
        collector = RemoteHistoryCollector(target)
        collector.sync_history(db)
        db.close()

    assert collector.last_error == ""
    assert not expired_file.exists()


def test_remote_unchanged_sync_still_prunes_expired_stale_archives(tmp_path):
    """Stale archives age out even when the remote never changes again."""
    remote = RemoteSpec(host="remote-dev", name="dev")
    target = RemoteSyncTarget(
        remote=remote,
        agent_type="codex",
        remote_root="~/.codex",
        provider="openai",
        index=0,
    )
    rel = "2026/04/23/rollout-remote.jsonl"
    manifest = b"OK\0" + b"100\t123\t./2026/04/23/rollout-remote.jsonl\0"

    with patch("agentic_metric.collectors.remote.DATA_DIR", tmp_path / "data"), \
         patch(
             "agentic_metric.collectors.remote.subprocess.run",
             return_value=Mock(returncode=0, stdout=manifest, stderr=b""),
         ):
        cache_root = _cache_root_for(target)
        cache_file = cache_root / "sessions" / rel
        cache_file.parent.mkdir(parents=True)
        cache_file.write_text("{}\n")
        (cache_root / ".remote-manifest.json").write_text(
            json.dumps({rel: {"size": "100", "mtime": "123"}}) + "\n"
        )
        expired_file = cache_root / ".stale" / "sessions" / "ancient.jsonl"
        expired_file.parent.mkdir(parents=True)
        expired_file.write_text("{}\n")
        expired = time.time() - 40 * 86400
        os.utime(expired_file, (expired, expired))

        db = Database(db_path=str(tmp_path / "data.db"))
        collector = RemoteHistoryCollector(target)
        collector.sync_history(db)
        db.close()

    assert collector.last_error == ""
    assert cache_file.exists()  # unchanged mirror untouched
    assert not expired_file.exists()


def test_remote_archiving_resets_stale_retention_clock(tmp_path):
    """A just-archived file must get the full retention window, even if the
    source file's own mtime is far older than the retention cutoff."""
    remote = RemoteSpec(host="remote-dev", name="dev")
    target = RemoteSyncTarget(
        remote=remote,
        agent_type="codex",
        remote_root="~/.codex",
        provider="openai",
        index=0,
    )
    rel = "2026/04/23/rollout-remote.jsonl"

    with patch("agentic_metric.collectors.remote.DATA_DIR", tmp_path / "data"), \
         patch(
             "agentic_metric.collectors.remote.subprocess.run",
             return_value=Mock(returncode=0, stdout=b"OK\0", stderr=b""),
         ):
        cache_root = _cache_root_for(target)
        cache_file = cache_root / "sessions" / rel
        cache_file.parent.mkdir(parents=True)
        cache_file.write_text("{}\n")
        old = time.time() - 40 * 86400
        os.utime(cache_file, (old, old))
        (cache_root / ".remote-manifest.json").write_text(
            json.dumps({rel: {"size": "100", "mtime": "123"}}) + "\n"
        )

        db = Database(db_path=str(tmp_path / "data.db"))
        collector = RemoteHistoryCollector(target)
        collector.sync_history(db)
        db.close()

        archived = cache_root / ".stale" / "sessions" / rel
        assert collector.last_error == ""
        assert not cache_file.exists()
        assert archived.exists()
        assert archived.stat().st_mtime > time.time() - 60


def test_remote_cache_prune_skips_orphans_when_config_is_unreadable(tmp_path):
    """Missing/corrupt config must not classify every mirror as an orphan."""
    from agentic_metric.collectors.remote import prune_remote_cache, remote_cache_report

    corrupt_config = tmp_path / "config.json"
    corrupt_config.write_text("{not json")

    with patch("agentic_metric.collectors.remote.DATA_DIR", tmp_path / "data"), \
         patch("agentic_metric.config.CONFIG_FILE", corrupt_config), \
         patch("agentic_metric.config.get_remote_specs", return_value=[]):
        mirror = tmp_path / "data" / "remote-cache" / "0123abcd456789ef"
        mirror_file = mirror / "sessions" / "keep.jsonl"
        mirror_file.parent.mkdir(parents=True)
        mirror_file.write_text("{}\n")
        stale_file = mirror / ".stale" / "sessions" / "gone.jsonl"
        stale_file.parent.mkdir(parents=True)
        stale_file.write_text("stale\n")

        report = remote_cache_report()
        assert report["config_unavailable"] is True
        assert all(not entry["is_orphan"] for entry in report["entries"])
        assert report["reclaimable_bytes"] == stale_file.stat().st_size

        result = prune_remote_cache()
        assert result["config_unavailable"] is True
        assert mirror_file.exists()  # mirror survives
        assert not stale_file.exists()  # stale archives still pruned

        # Missing config file is equally untrusted.
        corrupt_config.unlink()
        assert remote_cache_report()["config_unavailable"] is True

        # Valid JSON with the wrong shape parses to "no remotes" in
        # get_remote_specs, but must not be trusted for orphan deletion.
        for bad_shape in ('[]', '"x"', '{"remotes": "bad"}'):
            corrupt_config.write_text(bad_shape)
            assert remote_cache_report()["config_unavailable"] is True


def test_remote_cache_prune_reclaims_orphans_after_remotes_removed(tmp_path):
    """A readable config with zero remotes is deliberate: mirrors are orphans."""
    from agentic_metric.collectors.remote import prune_remote_cache, remote_cache_report

    empty_config = tmp_path / "config.json"
    empty_config.write_text("{}\n")

    with patch("agentic_metric.collectors.remote.DATA_DIR", tmp_path / "data"), \
         patch("agentic_metric.config.CONFIG_FILE", empty_config), \
         patch("agentic_metric.config.get_remote_specs", return_value=[]):
        mirror = tmp_path / "data" / "remote-cache" / "0123abcd456789ef"
        mirror_file = mirror / "sessions" / "old.jsonl"
        mirror_file.parent.mkdir(parents=True)
        mirror_file.write_text("{}\n")

        report = remote_cache_report()
        assert report["config_unavailable"] is False
        assert [entry["is_orphan"] for entry in report["entries"]] == [True]

        prune_remote_cache()
        assert not mirror.exists()


def test_remote_cache_prune_reports_failed_deletions(tmp_path):
    from agentic_metric.collectors.remote import prune_remote_cache

    remote = RemoteSpec(
        host="remote-dev",
        name="dev",
        collectors={
            "codex": [RemoteCollectorRoot(path="~/.codex", provider="openai")],
            "claude_code": [],
        },
    )

    with patch("agentic_metric.collectors.remote.DATA_DIR", tmp_path / "data"), \
         patch("agentic_metric.config.get_remote_specs", return_value=[remote]), \
         patch("agentic_metric.collectors.remote.shutil.rmtree"):  # deletion no-op
        orphan_file = tmp_path / "data" / "remote-cache" / "deadbeef" / "sessions" / "x.jsonl"
        orphan_file.parent.mkdir(parents=True)
        orphan_file.write_text("orphan\n")

        result = prune_remote_cache()

    assert result["removed"] == []
    assert result["reclaimed_bytes"] == 0
    assert [item["kind"] for item in result["failed"]] == ["orphan"]
    assert orphan_file.exists()


def test_codex_provider_mismatch_removes_only_same_provider_stale_rows(tmp_path):
    sessions_dir = tmp_path / "codex" / "sessions"
    data_root = str(tmp_path / "codex")
    day_dir = sessions_dir / "2026" / "04" / "23"
    day_dir.mkdir(parents=True)
    lines = [
        {
            "timestamp": "2026-04-23T10:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": "custom-sid",
                "cwd": "/tmp/project",
                "model_provider": "custom",
            },
        },
        {
            "timestamp": "2026-04-23T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "hello"},
        },
        {
            "timestamp": "2026-04-23T10:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 20}},
            },
        },
    ]
    (day_dir / "rollout-custom.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines)
    )

    db = Database(db_path=str(tmp_path / "data.db"))
    db.upsert_session(
        "custom-sid",
        "codex",
        provider="openai",
        data_root=data_root,
        input_tokens=999,
    )
    db.commit()

    CodexCollector(
        sessions_dir=sessions_dir,
        provider="openai",
        data_root=data_root,
    ).sync_history(db)
    assert db.conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE session_id = 'custom-sid'"
    ).fetchone()["n"] == 0

    CodexCollector(
        sessions_dir=sessions_dir,
        provider="custom",
        data_root=data_root,
    ).sync_history(db)
    row = db.conn.execute(
        "SELECT provider, input_tokens FROM sessions WHERE session_id = 'custom-sid'"
    ).fetchone()
    assert row["provider"] == "custom"
    assert row["input_tokens"] == 100
    db.close()


def test_codex_cached_only_update_recomputes_input_tokens():
    accum = CodexSessionAccum(Path("/tmp/fake.jsonl"), project_path="/test")
    accum._process_event_msg({
        "type": "token_count",
        "info": {"total_token_usage": {"input_tokens": 1000, "cached_input_tokens": 100, "output_tokens": 50}},
    }, ts="2026-04-24T10:00:00Z")
    assert accum.input_tokens == 900

    accum._process_event_msg({
        "type": "token_count",
        "info": {"total_token_usage": {"cached_input_tokens": 200}},
    }, ts="2026-04-24T10:01:00Z")
    assert accum.input_tokens == 800
    assert sum(r["input_tokens"] for r in accum.usage_bucket_rows()) == 800
    assert sum(r["cache_read_tokens"] for r in accum.usage_bucket_rows()) == 200


def test_codex_thread_settings_service_tier_prices_priority_requests():
    accum = CodexSessionAccum(Path("/tmp/fake.jsonl"), project_path="/test")
    accum.model = "gpt-5.6-sol"
    accum._process_event_msg({
        "type": "thread_settings_applied",
        "thread_settings": {"service_tier": "priority", "model": "gpt-5.6-sol"},
    }, ts="2026-07-20T10:00:00Z")
    accum._process_event_msg({
        "type": "token_count",
        "info": {
            "total_token_usage": {
                "input_tokens": 1_000,
                "cached_input_tokens": 200,
                "output_tokens": 100,
                "total_tokens": 1_100,
            },
            "last_token_usage": {
                "input_tokens": 1_000,
                "cached_input_tokens": 200,
                "output_tokens": 100,
                "total_tokens": 1_100,
            },
        },
    }, ts="2026-07-20T10:00:01Z")

    expected = estimate_cost(
        "gpt-5.6-sol",
        input_tokens=800,
        output_tokens=100,
        cache_read_tokens=200,
        service_tier="priority",
    )
    rows = accum.usage_bucket_rows()
    assert abs(sum(r["estimated_cost_usd"] for r in rows) - expected) < 1e-12
    assert expected > estimate_cost(
        "gpt-5.6-sol",
        input_tokens=800,
        output_tokens=100,
        cache_read_tokens=200,
    )


def test_codex_thread_settings_default_tier_restores_standard_pricing():
    accum = CodexSessionAccum(Path("/tmp/fake.jsonl"), project_path="/test")
    accum.model = "gpt-5.6-sol"
    accum.service_tier = "priority"
    accum._process_event_msg({
        "type": "thread_settings_applied",
        "thread_settings": {"service_tier": "default"},
    }, ts="2026-07-20T10:00:00Z")
    accum._process_event_msg({
        "type": "token_count",
        "info": {
            "total_token_usage": {"input_tokens": 1_000, "output_tokens": 100},
            "last_token_usage": {"input_tokens": 1_000, "output_tokens": 100},
        },
    }, ts="2026-07-20T10:00:01Z")

    expected = estimate_cost("gpt-5.6-sol", input_tokens=1_000, output_tokens=100)
    rows = accum.usage_bucket_rows()
    assert abs(sum(r["estimated_cost_usd"] for r in rows) - expected) < 1e-12


def test_codex_forked_session_inherits_replayed_service_tier():
    accum = CodexSessionAccum(Path("/tmp/fake.jsonl"), project_path="/test")
    accum.is_forked = True
    accum.model = "gpt-5.6-sol"
    # Replayed parent events arrive before the fork's first turn_context.
    accum._process_event_msg({
        "type": "thread_settings_applied",
        "thread_settings": {"service_tier": "priority"},
    }, ts="2026-07-20T10:00:00Z")
    assert accum.service_tier == "priority"


def test_codex_gpt56_cache_write_tokens_are_billed():
    accum = CodexSessionAccum(Path("/tmp/fake.jsonl"), project_path="/test")
    accum.model = "gpt-5.6-sol"
    accum._process_event_msg({
        "type": "token_count",
        "info": {
            "total_token_usage": {
                "input_tokens": 1_000,
                "cached_input_tokens": 200,
                "output_tokens": 100,
                "cache_write_input_tokens": 300,
                "total_tokens": 1_100,
            },
            "last_token_usage": {
                "input_tokens": 1_000,
                "cached_input_tokens": 200,
                "output_tokens": 100,
                "cache_write_input_tokens": 300,
                "total_tokens": 1_100,
            },
        },
    }, ts="2026-07-20T10:00:01Z")

    rows = accum.usage_bucket_rows()
    assert sum(r["input_tokens"] for r in rows) == 500
    assert sum(r["cache_read_tokens"] for r in rows) == 200
    assert sum(r["cache_creation_tokens"] for r in rows) == 300
    expected = estimate_cost(
        "gpt-5.6-sol",
        input_tokens=500,
        output_tokens=100,
        cache_read_tokens=200,
        cache_creation_tokens=300,
    )
    assert abs(sum(r["estimated_cost_usd"] for r in rows) - expected) < 1e-12


def test_codex_unbilled_model_keeps_cache_write_subset_in_input():
    """gpt-5.5 has no cache-write fee: writes must stay billed as input."""
    accum = CodexSessionAccum(Path("/tmp/fake.jsonl"), project_path="/test")
    accum.model = "gpt-5.5"
    accum._process_event_msg({
        "type": "token_count",
        "info": {
            "total_token_usage": {
                "input_tokens": 1_000,
                "cached_input_tokens": 200,
                "output_tokens": 100,
                "cache_write_input_tokens": 300,
                "total_tokens": 1_100,
            },
            "last_token_usage": {
                "input_tokens": 1_000,
                "cached_input_tokens": 200,
                "output_tokens": 100,
                "cache_write_input_tokens": 300,
                "total_tokens": 1_100,
            },
        },
    }, ts="2026-07-20T10:00:01Z")

    rows = accum.usage_bucket_rows()
    assert sum(r["input_tokens"] for r in rows) == 800
    assert sum(r["cache_creation_tokens"] for r in rows) == 0
    expected = estimate_cost(
        "gpt-5.5",
        input_tokens=800,
        output_tokens=100,
        cache_read_tokens=200,
    )
    assert abs(sum(r["estimated_cost_usd"] for r in rows) - expected) < 1e-12


def test_codex_cumulative_fallback_keeps_cache_write_subset_in_input():
    """Without last_token_usage, subset writes stay billed as plain input.

    The cumulative counters cannot attribute writes per model across
    mid-session model switches, so the legacy fallback never moves them.
    """
    accum = CodexSessionAccum(Path("/tmp/fake.jsonl"), project_path="/test")
    accum.model = "gpt-5.6-sol"
    accum._process_event_msg({
        "type": "token_count",
        "info": {
            "total_token_usage": {
                "input_tokens": 1_000,
                "cached_input_tokens": 200,
                "output_tokens": 100,
                "cache_write_input_tokens": 300,
                "total_tokens": 1_100,
            },
        },
    }, ts="2026-07-20T10:00:01Z")

    rows = accum.usage_bucket_rows()
    assert sum(r["input_tokens"] for r in rows) == 800
    assert sum(r["cache_read_tokens"] for r in rows) == 200
    assert sum(r["cache_creation_tokens"] for r in rows) == 0
    expected = estimate_cost(
        "gpt-5.6-sol",
        input_tokens=800,
        output_tokens=100,
        cache_read_tokens=200,
    )
    assert abs(sum(r["estimated_cost_usd"] for r in rows) - expected) < 1e-12


def test_codex_cumulative_fallback_dual_key_matches_event_path_shape():
    """Dual-key cumulative snapshots use the same subset preference as events."""
    accum = CodexSessionAccum(Path("/tmp/fake.jsonl"), project_path="/test")
    accum.model = "gpt-5.6-sol"
    accum._process_event_msg({
        "type": "token_count",
        "info": {
            "total_token_usage": {
                "input_tokens": 1_000,
                "cached_input_tokens": 200,
                "output_tokens": 100,
                "cache_write_input_tokens": 300,
                "cache_creation_input_tokens": 300,
                "total_tokens": 1_100,
            },
        },
    }, ts="2026-07-20T10:00:01Z")

    rows = accum.usage_bucket_rows()
    # Subset preference: writes stay inside input on the legacy fallback and
    # the separate-shaped copy is not added on top of them.
    assert sum(r["input_tokens"] for r in rows) == 800
    assert sum(r["cache_creation_tokens"] for r in rows) == 0


def test_codex_fork_baseline_records_subset_writes_before_model_is_known():
    accum = CodexSessionAccum(Path("/tmp/fake.jsonl"), project_path="/test")
    accum.is_forked = True
    # Replayed parent usage arrives before the fork's turn_context, so the
    # model is still empty when the baseline is captured.
    accum._process_event_msg({
        "type": "token_count",
        "info": {
            "total_token_usage": {
                "input_tokens": 5_000,
                "cached_input_tokens": 1_000,
                "output_tokens": 500,
                "cache_write_input_tokens": 300,
                "total_tokens": 5_500,
            },
        },
    }, ts="2026-07-20T10:00:00Z")

    assert accum.fork_baseline_cache_create == 300
    assert accum.fork_baseline_raw_input == 5_000


def test_codex_cumulative_fallback_model_switch_never_reclassifies_writes():
    """5.6 → 5.5 → 5.6 switches must not move 5.5-era writes into a write bucket."""
    accum = CodexSessionAccum(Path("/tmp/fake.jsonl"), project_path="/test")

    def snapshot(input_tokens, writes, output):
        return {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": 0,
                    "output_tokens": output,
                    "cache_write_input_tokens": writes,
                    "total_tokens": input_tokens + output,
                },
            },
        }

    accum.model = "gpt-5.6-sol"
    accum._process_event_msg(snapshot(1_000, 300, 100), ts="2026-07-20T10:00:01Z")
    accum.model = "gpt-5.5"
    accum._process_event_msg(snapshot(2_000, 700, 200), ts="2026-07-20T10:00:02Z")
    accum.model = "gpt-5.6-sol"
    accum._process_event_msg(snapshot(3_000, 900, 300), ts="2026-07-20T10:00:03Z")

    rows = accum.usage_bucket_rows()
    assert sum(r["cache_creation_tokens"] for r in rows) == 0
    assert sum(r["input_tokens"] for r in rows) == 3_000
    assert sum(r["output_tokens"] for r in rows) == 300
    assert all(r["input_tokens"] >= 0 for r in rows)


def test_codex_last_token_usage_drives_long_context_cost():
    accum = CodexSessionAccum(Path("/tmp/fake.jsonl"), project_path="/test")
    accum.model = "gpt-5.4"
    accum._process_event_msg({
        "type": "token_count",
        "info": {
            "total_token_usage": {
                "input_tokens": 300_000,
                "cached_input_tokens": 0,
                "output_tokens": 1_000,
            },
            "last_token_usage": {
                "input_tokens": 300_000,
                "cached_input_tokens": 0,
                "output_tokens": 1_000,
            },
        },
    }, ts="2026-04-24T10:00:00Z")

    expected = estimate_cost("gpt-5.4", input_tokens=300_000, output_tokens=1_000)
    assert abs(sum(r["estimated_cost_usd"] for r in accum.usage_bucket_rows()) - expected) < 1e-12


def test_codex_last_token_usage_supports_separate_cached_input_semantics():
    accum = CodexSessionAccum(Path("/tmp/fake.jsonl"), project_path="/test")
    accum.model = "gpt-5.5"
    accum._process_event_msg({
        "type": "token_count",
        "info": {
            "total_token_usage": {
                "input_tokens": 4_543,
                "cached_input_tokens": 14_848,
                "output_tokens": 699,
                "total_tokens": 20_090,
            },
            "last_token_usage": {
                "input_tokens": 4_543,
                "cached_input_tokens": 14_848,
                "output_tokens": 699,
                "total_tokens": 20_090,
            },
        },
    }, ts="2026-04-24T10:00:00Z")

    rows = accum.usage_bucket_rows()
    assert sum(r["input_tokens"] for r in rows) == 4_543
    assert sum(r["cache_read_tokens"] for r in rows) == 14_848
    expected = estimate_cost(
        "gpt-5.5",
        input_tokens=4_543,
        output_tokens=699,
        cache_read_tokens=14_848,
    )
    assert abs(sum(r["estimated_cost_usd"] for r in rows) - expected) < 1e-12


def test_codex_last_token_usage_ignores_anthropic_cache_creation_1h_shape():
    accum = CodexSessionAccum(Path("/tmp/fake.jsonl"), project_path="/test")
    accum.model = "gpt-5.5"
    accum._process_event_msg({
        "type": "token_count",
        "info": {
            "total_token_usage": {
                "input_tokens": 1_000,
                "cached_input_tokens": 0,
                "output_tokens": 100,
                "cache_creation_input_tokens": 40,
            },
            "last_token_usage": {
                "input_tokens": 1_000,
                "cached_input_tokens": 0,
                "output_tokens": 100,
                "cache_creation_input_tokens": 40,
                "cache_creation": {"ephemeral_1h_input_tokens": 15},
            },
        },
    }, ts="2026-04-24T10:00:00Z")

    rows = accum.usage_bucket_rows()
    assert accum.cache_create == 40
    assert sum(r["cache_creation_tokens"] for r in rows) == 40
    assert sum(r.get("cache_creation_1h_tokens", 0) for r in rows) == 0
    expected = estimate_cost(
        "gpt-5.5",
        input_tokens=1_000,
        output_tokens=100,
        cache_creation_tokens=40,
    )
    assert abs(sum(r["estimated_cost_usd"] for r in rows) - expected) < 1e-12


def test_codex_cumulative_fallback_supports_separate_cached_input_semantics():
    accum = CodexSessionAccum(Path("/tmp/fake.jsonl"), project_path="/test")
    accum.model = "gpt-5.5"
    accum._process_event_msg({
        "type": "token_count",
        "info": {
            "total_token_usage": {
                "input_tokens": 4_543,
                "cached_input_tokens": 14_848,
                "output_tokens": 699,
                "total_tokens": 20_090,
            },
        },
    }, ts="2026-04-24T10:00:00Z")

    rows = accum.usage_bucket_rows()
    assert sum(r["input_tokens"] for r in rows) == 4_543
    assert sum(r["cache_read_tokens"] for r in rows) == 14_848
    expected = estimate_cost(
        "gpt-5.5",
        input_tokens=4_543,
        output_tokens=699,
        cache_read_tokens=14_848,
        apply_long_context=False,
    )
    assert abs(sum(r["estimated_cost_usd"] for r in rows) - expected) < 1e-12


def test_codex_compatible_provider_defaults_to_separate_input_without_total_tokens():
    accum = CodexSessionAccum(
        Path("/tmp/fake.jsonl"),
        project_path="/test",
        provider="ichat",
    )
    accum.model = "gpt-5.5"
    accum._process_event_msg({
        "type": "token_count",
        "info": {
            "total_token_usage": {
                "input_tokens": 2_000,
                "cached_input_tokens": 1_000,
                "output_tokens": 100,
            },
        },
    }, ts="2026-04-24T10:00:00Z")

    rows = accum.usage_bucket_rows()
    assert sum(r["input_tokens"] for r in rows) == 2_000
    assert sum(r["output_tokens"] for r in rows) == 100
    assert sum(r["cache_read_tokens"] for r in rows) == 1_000
    expected = estimate_cost(
        "gpt-5.5",
        input_tokens=2_000,
        output_tokens=100,
        cache_read_tokens=1_000,
        apply_long_context=False,
    )
    assert abs(sum(r["estimated_cost_usd"] for r in rows) - expected) < 1e-12


def test_codex_cumulative_fallback_keeps_separate_input_semantics_across_snapshots():
    accum = CodexSessionAccum(Path("/tmp/fake.jsonl"), project_path="/test")
    accum.model = "gpt-5.5"
    accum._process_event_msg({
        "type": "token_count",
        "info": {
            "total_token_usage": {
                "input_tokens": 2_000,
                "cached_input_tokens": 1_000,
                "output_tokens": 100,
                "total_tokens": 3_100,
            },
        },
    }, ts="2026-04-24T10:00:00Z")
    accum._process_event_msg({
        "type": "token_count",
        "info": {
            "total_token_usage": {
                "input_tokens": 2_500,
                "cached_input_tokens": 1_200,
                "output_tokens": 200,
                "total_tokens": 3_900,
            },
        },
    }, ts="2026-04-24T10:01:00Z")

    rows = accum.usage_bucket_rows()
    assert sum(r["input_tokens"] for r in rows) == 2_500
    assert sum(r["output_tokens"] for r in rows) == 200
    assert sum(r["cache_read_tokens"] for r in rows) == 1_200
    expected = (
        estimate_cost(
            "gpt-5.5",
            input_tokens=2_000,
            output_tokens=100,
            cache_read_tokens=1_000,
            apply_long_context=False,
        )
        + estimate_cost(
            "gpt-5.5",
            input_tokens=500,
            output_tokens=100,
            cache_read_tokens=200,
            apply_long_context=False,
        )
    )
    assert abs(sum(r["estimated_cost_usd"] for r in rows) - expected) < 1e-12


def test_codex_last_token_usage_ignores_cumulative_counter_reset():
    accum = CodexSessionAccum(Path("/tmp/fake.jsonl"), project_path="/test")
    accum.model = "gpt-5.5"
    accum._process_event_msg({
        "type": "token_count",
        "info": {
            "total_token_usage": {
                "input_tokens": 1_000,
                "cached_input_tokens": 0,
                "output_tokens": 100,
            },
            "last_token_usage": {
                "input_tokens": 1_000,
                "cached_input_tokens": 0,
                "output_tokens": 100,
            },
        },
    }, ts="2026-04-24T10:00:00Z")
    accum._process_event_msg({
        "type": "token_count",
        "info": {
            "total_token_usage": {
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
            },
            "last_token_usage": {
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
            },
        },
    }, ts="2026-04-24T10:01:00Z")
    accum._process_event_msg({
        "type": "token_count",
        "info": {
            "total_token_usage": {
                "input_tokens": 200,
                "cached_input_tokens": 50,
                "output_tokens": 20,
            },
            "last_token_usage": {
                "input_tokens": 200,
                "cached_input_tokens": 50,
                "output_tokens": 20,
            },
        },
    }, ts="2026-04-24T10:02:00Z")

    rows = accum.usage_bucket_rows()
    assert sum(r["input_tokens"] for r in rows) == 1_150
    assert sum(r["output_tokens"] for r in rows) == 120
    assert sum(r["cache_read_tokens"] for r in rows) == 50
    expected = (
        estimate_cost("gpt-5.5", input_tokens=1_000, output_tokens=100)
        + estimate_cost(
            "gpt-5.5",
            input_tokens=150,
            output_tokens=20,
            cache_read_tokens=50,
        )
    )
    assert abs(sum(r["estimated_cost_usd"] for r in rows) - expected) < 1e-12


def test_codex_last_token_usage_skips_repeated_cumulative_snapshot():
    accum = CodexSessionAccum(Path("/tmp/fake.jsonl"), project_path="/test")
    accum.model = "gpt-5.5"
    payload = {
        "type": "token_count",
        "info": {
            "total_token_usage": {
                "input_tokens": 1_000,
                "cached_input_tokens": 200,
                "output_tokens": 100,
            },
            "last_token_usage": {
                "input_tokens": 1_000,
                "cached_input_tokens": 200,
                "output_tokens": 100,
            },
        },
    }
    accum._process_event_msg(payload, ts="2026-04-24T10:00:00Z")
    accum._process_event_msg(payload, ts="2026-04-24T10:00:01Z")

    rows = accum.usage_bucket_rows()
    assert sum(r["input_tokens"] for r in rows) == 800
    assert sum(r["output_tokens"] for r in rows) == 100
    assert sum(r["cache_read_tokens"] for r in rows) == 200
    expected = estimate_cost(
        "gpt-5.5",
        input_tokens=800,
        output_tokens=100,
        cache_read_tokens=200,
    )
    assert abs(sum(r["estimated_cost_usd"] for r in rows) - expected) < 1e-12


def test_codex_cumulative_fallback_rebuilds_negative_reclassification():
    accum = CodexSessionAccum(Path("/tmp/fake.jsonl"), project_path="/test")
    accum.model = "gpt-5.5"
    accum._process_event_msg({
        "type": "token_count",
        "info": {
            "total_token_usage": {
                "input_tokens": 1_000,
                "cached_input_tokens": 0,
                "output_tokens": 100,
            },
        },
    }, ts="2026-04-24T10:00:00Z")
    accum._process_event_msg({
        "type": "token_count",
        "info": {
            "total_token_usage": {
                "input_tokens": 1_000,
                "cached_input_tokens": 200,
                "output_tokens": 100,
            },
        },
    }, ts="2026-04-24T10:01:00Z")

    rows = accum.usage_bucket_rows()
    for row in rows:
        assert row["input_tokens"] >= 0
        assert row["output_tokens"] >= 0
        assert row["cache_read_tokens"] >= 0
        assert row["cache_creation_tokens"] >= 0
    assert sum(r["input_tokens"] for r in rows) == 800
    assert sum(r["output_tokens"] for r in rows) == 100
    assert sum(r["cache_read_tokens"] for r in rows) == 200
    expected = estimate_cost(
        "gpt-5.5",
        input_tokens=800,
        output_tokens=100,
        cache_read_tokens=200,
        apply_long_context=False,
    )
    assert abs(sum(r["estimated_cost_usd"] for r in rows) - expected) < 1e-12


def test_codex_cumulative_fallback_does_not_apply_long_context_cost():
    accum = CodexSessionAccum(Path("/tmp/fake.jsonl"), project_path="/test")
    accum.model = "gpt-5.4"
    accum._process_event_msg({
        "type": "token_count",
        "info": {
            "total_token_usage": {
                "input_tokens": 300_000,
                "cached_input_tokens": 0,
                "output_tokens": 1_000,
            },
        },
    }, ts="2026-04-24T10:00:00Z")

    expected = estimate_cost(
        "gpt-5.4",
        input_tokens=300_000,
        output_tokens=1_000,
        apply_long_context=False,
    )
    assert abs(sum(r["estimated_cost_usd"] for r in accum.usage_bucket_rows()) - expected) < 1e-12


def test_codex_unknown_model_event_cost_stays_unknown(tmp_path):
    import agentic_metric.pricing as pricing

    pricing._user_cache = None
    pricing._user_cache_mtime = -1.0
    with patch("agentic_metric.pricing.PRICING_FILE", tmp_path / "pricing.json"):
        accum = CodexSessionAccum(Path("/tmp/fake.jsonl"), project_path="/test")
        accum._process_entry({
            "timestamp": "2026-04-24T09:59:59Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.4-pro"},
        })
        accum._process_event_msg({
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": 1_000,
                    "cached_input_tokens": 0,
                    "output_tokens": 100,
                },
                "last_token_usage": {
                    "input_tokens": 1_000,
                    "cached_input_tokens": 0,
                    "output_tokens": 100,
                },
            },
        }, ts="2026-04-24T10:00:00Z")
    pricing._user_cache = None
    pricing._user_cache_mtime = -1.0

    rows = accum.usage_bucket_rows()
    assert rows[0]["model"] == "gpt-5.4-pro"
    assert rows[0]["estimated_cost_usd"] is None


def test_codex_partial_trailing_jsonl_is_retried(tmp_path):
    session_file = tmp_path / "rollout-test.jsonl"
    token_line = {
        "timestamp": "2026-04-23T10:00:02Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 20}},
        },
    }
    prefix = [
        {
            "timestamp": "2026-04-23T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": "sid", "cwd": "/tmp/project"},
        },
        {
            "timestamp": "2026-04-23T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "hello"},
        },
    ]
    partial = json.dumps(token_line)[:-1]
    session_file.write_text("".join(json.dumps(line) + "\n" for line in prefix) + partial)

    accum = CodexSessionAccum(session_file, project_path="/tmp/project")
    accum.read_new_lines()
    assert accum.output_tokens == 0

    with session_file.open("a") as f:
        f.write("}\n")
    accum.read_new_lines()
    assert accum.output_tokens == 20


def test_claude_partial_trailing_jsonl_is_retried(tmp_path):
    session_file = tmp_path / "session.jsonl"
    assistant_line = {
        "timestamp": "2026-04-23T10:00:02Z",
        "type": "assistant",
        "message": {
            "model": "claude-sonnet-4-6",
            "usage": {"input_tokens": 100, "output_tokens": 20},
        },
    }
    user_line = {
        "timestamp": "2026-04-23T10:00:01Z",
        "type": "user",
        "message": {"content": "hello"},
    }
    session_file.write_text(json.dumps(user_line) + "\n" + json.dumps(assistant_line)[:-1])

    accum = ClaudeSessionAccum(session_file, project_path="/tmp/project")
    accum.read_new_lines()
    assert accum.output_tokens == 0

    with session_file.open("a") as f:
        f.write("}\n")
    accum.read_new_lines()
    assert accum.output_tokens == 20


def test_claude_accumulator_resets_after_truncation(tmp_path):
    session_file = tmp_path / "session.jsonl"
    first = [
        {
            "timestamp": "2026-04-23T10:00:00Z",
            "type": "user",
            "message": {"content": "first"},
        },
        {
            "timestamp": "2026-04-23T10:00:01Z",
            "type": "assistant",
            "message": {
                "model": "claude-sonnet-4-6",
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        },
    ]
    second = [{
        "timestamp": "2026-04-23T10:00:02Z",
        "type": "user",
        "message": {"content": "second"},
    }]
    session_file.write_text("".join(json.dumps(line) + "\n" for line in first))

    accum = ClaudeSessionAccum(session_file, project_path="/tmp/project")
    accum.read_new_lines()
    assert accum.user_turns == 1
    assert accum.output_tokens == 20

    session_file.write_text("".join(json.dumps(line) + "\n" for line in second))
    accum.read_new_lines()
    assert accum.user_turns == 1
    assert accum.output_tokens == 0
    assert accum.first_prompt == "second"


def test_claude_duplicate_assistant_message_id_uses_last_usage(tmp_path):
    session_file = tmp_path / "session.jsonl"
    lines = [
        {
            "timestamp": "2026-04-23T10:00:00Z",
            "type": "user",
            "message": {"content": "hello"},
        },
        {
            "timestamp": "2026-04-23T10:00:01Z",
            "type": "assistant",
            "message": {
                "id": "msg-1",
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 1,
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 40,
                    "cache_creation": {"ephemeral_1h_input_tokens": 15},
                },
            },
        },
        {
            "timestamp": "2026-04-23T10:00:02Z",
            "type": "assistant",
            "message": {
                "id": "msg-1",
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 40,
                    "cache_creation": {"ephemeral_1h_input_tokens": 15},
                },
            },
        },
    ]
    session_file.write_text("".join(json.dumps(line) + "\n" for line in lines))

    accum = ClaudeSessionAccum(session_file, project_path="/tmp/project")
    accum.read_new_lines()

    assert accum.message_count == 2
    assert accum.input_tokens == 10
    assert accum.output_tokens == 20
    assert accum.cache_read == 100
    assert accum.cache_create == 40
    assert accum.cache_create_1h == 15
    assert sum(r["message_count"] for r in accum.usage_bucket_rows()) == 2
    assert sum(r["input_tokens"] for r in accum.usage_bucket_rows()) == 10
    assert sum(r["output_tokens"] for r in accum.usage_bucket_rows()) == 20
    assert sum(r["cache_read_tokens"] for r in accum.usage_bucket_rows()) == 100
    assert sum(r["cache_creation_tokens"] for r in accum.usage_bucket_rows()) == 40
    assert sum(r["cache_creation_1h_tokens"] for r in accum.usage_bucket_rows()) == 15
    expected_cost = estimate_cost(
        "claude-sonnet-4-6",
        input_tokens=10,
        output_tokens=20,
        cache_read_tokens=100,
        cache_creation_tokens=40,
        cache_creation_1h_tokens=15,
    )
    assert abs(sum(r["estimated_cost_usd"] for r in accum.usage_bucket_rows()) - expected_cost) < 1e-12


def test_claude_usage_speed_fast_prices_fast_mode(tmp_path):
    session_file = tmp_path / "session.jsonl"
    lines = [
        {
            "timestamp": "2026-07-20T10:00:00Z",
            "type": "user",
            "message": {"content": "hello"},
        },
        {
            "timestamp": "2026-07-20T10:00:01Z",
            "type": "assistant",
            "message": {
                "id": "msg-fast",
                "model": "claude-opus-4-8",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 1_000,
                    "cache_creation_input_tokens": 200,
                    "speed": "fast",
                },
            },
        },
        {
            "timestamp": "2026-07-20T10:00:02Z",
            "type": "assistant",
            "message": {
                "id": "msg-standard",
                "model": "claude-opus-4-8",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "speed": "standard",
                },
            },
        },
    ]
    session_file.write_text("".join(json.dumps(line) + "\n" for line in lines))

    accum = ClaudeSessionAccum(session_file, project_path="/tmp/project")
    accum.read_new_lines()

    expected = estimate_cost(
        "claude-opus-4-8",
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=1_000,
        cache_creation_tokens=200,
        speed="fast",
    ) + estimate_cost(
        "claude-opus-4-8",
        input_tokens=100,
        output_tokens=50,
    )
    total = sum(r["estimated_cost_usd"] for r in accum.usage_bucket_rows())
    assert abs(total - expected) < 1e-12


def test_claude_real_model_replaces_initial_synthetic(tmp_path):
    session_file = tmp_path / "session.jsonl"
    lines = [
        {
            "timestamp": "2026-04-23T10:00:00Z",
            "type": "user",
            "message": {"content": "hello"},
        },
        {
            "timestamp": "2026-04-23T10:00:01Z",
            "type": "assistant",
            "message": {
                "id": "synthetic",
                "model": "<synthetic>",
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
        {
            "timestamp": "2026-04-23T10:00:02Z",
            "type": "assistant",
            "message": {
                "id": "real",
                "model": "claude-opus-4-7",
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        },
    ]
    session_file.write_text("".join(json.dumps(line) + "\n" for line in lines))

    accum = ClaudeSessionAccum(session_file, project_path="/tmp/project")
    accum.read_new_lines()

    assert accum.model == "claude-opus-4-7"
    assert any(r["model"] == "claude-opus-4-7" for r in accum.usage_bucket_rows())


def test_claude_today_counters_reset_after_midnight(tmp_path):
    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 4, 23, 0, 5, 0)

    session_file = tmp_path / "session.jsonl"
    session_file.write_text("")
    accum = ClaudeSessionAccum(session_file, project_path="/test")
    accum.today_key = "2026-04-22"
    accum.today_user_turns = 3
    accum.today_message_count = 7
    accum.today_input_tokens = 100
    accum.today_output_tokens = 50
    accum.today_cache_read = 25
    accum.today_cache_create = 10

    with patch("agentic_metric.collectors.claude_code.datetime", FakeDateTime):
        accum.read_new_lines()

    assert accum.today_key == "2026-04-23"
    assert accum.today_user_turns == 0
    assert accum.today_message_count == 0
    assert accum.today_input_tokens == 0
    assert accum.today_output_tokens == 0
    assert accum.today_cache_read == 0
    assert accum.today_cache_create == 0


def test_claude_history_sync_scans_subagent_jsonl(tmp_path):
    projects = tmp_path / "projects"
    subagents = projects / "-tmp-project" / "parent-session" / "subagents"
    subagents.mkdir(parents=True)
    subagent_file = subagents / "agent-a1.jsonl"
    lines = [
        {
            "timestamp": "2026-04-23T10:00:00Z",
            "type": "user",
            "cwd": "/tmp/project",
            "message": {"content": "sub task"},
        },
        {
            "timestamp": "2026-04-23T10:00:01Z",
            "type": "assistant",
            "cwd": "/tmp/project",
            "message": {
                "id": "msg-sub",
                "model": "claude-sonnet-4-6",
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        },
    ]
    subagent_file.write_text("".join(json.dumps(line) + "\n" for line in lines))

    db = Database(db_path=str(tmp_path / "data.db"))
    with patch("agentic_metric.collectors.claude_code.PROJECTS_DIR", projects):
        ClaudeCodeCollector().sync_history(db)

    row = db.conn.execute(
        "SELECT project_path, input_tokens, output_tokens "
        "FROM sessions WHERE session_id = 'parent-session:agent-a1' "
        "AND agent_type = 'claude_code'"
    ).fetchone()
    assert row is not None
    assert row["project_path"] == "/tmp/project"
    assert row["input_tokens"] == 100
    assert row["output_tokens"] == 20
    # Usage buckets must record the real cwd too, not the on-disk projects/
    # dir (which is a local cache path for SSH-backed remotes).
    usage = db.conn.execute(
        "SELECT DISTINCT project_path FROM session_usage "
        "WHERE session_id = 'parent-session:agent-a1' "
        "AND agent_type = 'claude_code'"
    ).fetchall()
    assert [r["project_path"] for r in usage] == ["/tmp/project"]
    db.close()


def test_claude_history_sync_skips_replayed_subagent_assistant_usage(tmp_path):
    projects = tmp_path / "projects"
    subagents = projects / "-tmp-project" / "parent-session" / "subagents"
    subagents.mkdir(parents=True)

    def write_subagent(path: Path, prompt: str, extra_id: str | None = None) -> None:
        lines = [
            {
                "timestamp": "2026-04-23T10:00:00Z",
                "type": "user",
                "cwd": "/tmp/project",
                "message": {"content": prompt},
            },
            {
                "timestamp": "2026-04-23T10:00:01Z",
                "type": "assistant",
                "cwd": "/tmp/project",
                "message": {
                    "id": "msg-replayed",
                    "model": "claude-opus-4-8",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 20,
                        "cache_read_input_tokens": 300,
                        "cache_creation_input_tokens": 40,
                    },
                },
            },
        ]
        if extra_id:
            lines.append(
                {
                    "timestamp": "2026-04-23T10:00:02Z",
                    "type": "assistant",
                    "cwd": "/tmp/project",
                    "message": {
                        "id": extra_id,
                        "model": "claude-opus-4-8",
                        "usage": {"input_tokens": 7, "output_tokens": 8},
                    },
                }
            )
        path.write_text("".join(json.dumps(line) + "\n" for line in lines))

    write_subagent(subagents / "agent-a1.jsonl", "sub task 1")
    write_subagent(subagents / "agent-a2.jsonl", "sub task 2", extra_id="msg-unique")

    db = Database(db_path=str(tmp_path / "data.db"))
    with patch("agentic_metric.collectors.claude_code.PROJECTS_DIR", projects):
        ClaudeCodeCollector().sync_history(db)

    rows = db.conn.execute(
        """SELECT session_id, input_tokens, output_tokens,
                  cache_read_tokens, cache_creation_tokens
           FROM sessions
           WHERE agent_type = 'claude_code'
           ORDER BY session_id"""
    ).fetchall()
    assert [dict(row) for row in rows] == [
        {
            "session_id": "parent-session:agent-a1",
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_tokens": 300,
            "cache_creation_tokens": 40,
        },
        {
            "session_id": "parent-session:agent-a2",
            "input_tokens": 7,
            "output_tokens": 8,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        },
    ]
    totals = db.conn.execute(
        """SELECT SUM(input_tokens) AS input_tokens,
                  SUM(output_tokens) AS output_tokens,
                  SUM(cache_read_tokens) AS cache_read_tokens,
                  SUM(cache_creation_tokens) AS cache_creation_tokens
           FROM session_usage
           WHERE agent_type = 'claude_code'"""
    ).fetchone()
    assert dict(totals) == {
        "input_tokens": 17,
        "output_tokens": 28,
        "cache_read_tokens": 300,
        "cache_creation_tokens": 40,
    }
    db.close()


def test_claude_history_sync_keeps_zero_turn_unique_usage(tmp_path):
    projects = tmp_path / "projects"
    subagents = projects / "-tmp-project" / "parent-session" / "subagents"
    subagents.mkdir(parents=True)

    zero_turn = [
        {
            "timestamp": "2026-04-23T10:00:01Z",
            "type": "assistant",
            "cwd": "/tmp/project",
            "message": {
                "id": "msg-shared",
                "model": "gpt-5.5",
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        },
    ]
    later = [
        {
            "timestamp": "2026-04-23T10:00:02Z",
            "type": "user",
            "cwd": "/tmp/project",
            "message": {"content": "later task"},
        },
        zero_turn[0],
        {
            "timestamp": "2026-04-23T10:00:03Z",
            "type": "assistant",
            "cwd": "/tmp/project",
            "message": {
                "id": "msg-unique",
                "model": "gpt-5.5",
                "usage": {"input_tokens": 7, "output_tokens": 8},
            },
        },
    ]
    (subagents / "agent-a1.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in zero_turn)
    )
    (subagents / "agent-a2.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in later)
    )

    db = Database(db_path=str(tmp_path / "data.db"))
    ClaudeCodeCollector(
        projects_dir=projects,
        provider="ichat",
        data_root=str(tmp_path),
    ).sync_history(db)

    rows = db.conn.execute(
        """SELECT session_id, user_turns, input_tokens, output_tokens
           FROM sessions
           WHERE agent_type = 'claude_code'
           ORDER BY session_id"""
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("parent-session:agent-a1", 0, 10, 20),
        ("parent-session:agent-a2", 1, 7, 8),
    ]
    db.close()


def test_claude_incremental_sync_uses_skipped_files_for_replay_dedupe(tmp_path):
    projects = tmp_path / "projects"
    subagents = projects / "-tmp-project" / "parent-session" / "subagents"
    subagents.mkdir(parents=True)

    first = subagents / "agent-a1.jsonl"
    second = subagents / "agent-a2.jsonl"

    def write_file(path: Path, *, extra_input: int) -> None:
        lines = [
            {
                "timestamp": "2026-04-23T10:00:00Z",
                "type": "user",
                "cwd": "/tmp/project",
                "message": {"content": path.stem},
            },
            {
                "timestamp": "2026-04-23T10:00:01Z",
                "type": "assistant",
                "cwd": "/tmp/project",
                "message": {
                    "id": "msg-replayed",
                    "model": "claude-opus-4-8",
                    "usage": {"input_tokens": 10, "output_tokens": 20},
                },
            },
            {
                "timestamp": "2026-04-23T10:00:02Z",
                "type": "assistant",
                "cwd": "/tmp/project",
                "message": {
                    "id": f"msg-unique-{extra_input}",
                    "model": "claude-opus-4-8",
                    "usage": {"input_tokens": extra_input, "output_tokens": 1},
                },
            },
        ]
        path.write_text("".join(json.dumps(line) + "\n" for line in lines))

    first.write_text(
        json.dumps({
            "timestamp": "2026-04-23T10:00:00Z",
            "type": "user",
            "cwd": "/tmp/project",
            "message": {"content": "first"},
        }) + "\n" +
        json.dumps({
            "timestamp": "2026-04-23T10:00:01Z",
            "type": "assistant",
            "cwd": "/tmp/project",
            "message": {
                "id": "msg-replayed",
                "model": "claude-opus-4-8",
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        }) + "\n"
    )
    write_file(second, extra_input=3)

    db = Database(db_path=str(tmp_path / "data.db"))
    with patch("agentic_metric.collectors.claude_code.PROJECTS_DIR", projects):
        ClaudeCodeCollector().sync_history(db)

    # Change only the second file. The first file should be skipped by
    # sync_state on the next sync, but its cached assistant ids must still seed
    # the replay-dedupe set for the changed sibling without reopening the file.
    write_file(second, extra_input=5)
    with patch("agentic_metric.collectors.claude_code.PROJECTS_DIR", projects), \
         patch(
             "agentic_metric.collectors.claude_code._read_assistant_ids",
             side_effect=AssertionError("assistant ids should come from cache"),
         ):
        ClaudeCodeCollector().sync_history(db)

    row = db.conn.execute(
        """SELECT input_tokens, output_tokens
           FROM sessions
           WHERE session_id = 'parent-session:agent-a2'"""
    ).fetchone()
    assert dict(row) == {"input_tokens": 5, "output_tokens": 1}
    db.close()


def test_claude_incremental_sync_rewrites_skipped_later_replay_file(tmp_path):
    projects = tmp_path / "projects"
    project_dir = projects / "-tmp-project"
    subagents = project_dir / "parent-session" / "subagents"
    subagents.mkdir(parents=True)

    subagent_file = subagents / "agent-a1.jsonl"
    subagent_file.write_text(
        json.dumps({
            "timestamp": "2026-04-23T10:00:00Z",
            "type": "user",
            "cwd": "/tmp/project",
            "message": {"content": "subagent"},
        }) + "\n" +
        json.dumps({
            "timestamp": "2026-04-23T10:00:01Z",
            "type": "assistant",
            "cwd": "/tmp/project",
            "message": {
                "id": "msg-parent",
                "model": "claude-opus-4-8",
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        }) + "\n"
    )

    db = Database(db_path=str(tmp_path / "data.db"))
    with patch("agentic_metric.collectors.claude_code.PROJECTS_DIR", projects):
        ClaudeCodeCollector().sync_history(db)

    parent_file = project_dir / "parent-session.jsonl"
    parent_file.write_text(
        json.dumps({
            "timestamp": "2026-04-23T09:59:00Z",
            "type": "user",
            "cwd": "/tmp/project",
            "message": {"content": "parent"},
        }) + "\n" +
        json.dumps({
            "timestamp": "2026-04-23T09:59:01Z",
            "type": "assistant",
            "cwd": "/tmp/project",
            "message": {
                "id": "msg-parent",
                "model": "claude-opus-4-8",
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        }) + "\n"
    )
    with patch("agentic_metric.collectors.claude_code.PROJECTS_DIR", projects):
        ClaudeCodeCollector().sync_history(db)

    rows = db.conn.execute(
        """SELECT session_id, input_tokens, output_tokens
           FROM sessions
           WHERE agent_type = 'claude_code'
           ORDER BY session_id"""
    ).fetchall()
    assert [(r["session_id"], r["input_tokens"], r["output_tokens"]) for r in rows] == [
        ("parent-session", 10, 20),
        ("parent-session:agent-a1", 0, 0),
    ]
    totals = db.conn.execute(
        """SELECT SUM(input_tokens) AS input_tokens,
                  SUM(output_tokens) AS output_tokens
           FROM session_usage
           WHERE agent_type = 'claude_code'"""
    ).fetchone()
    assert dict(totals) == {"input_tokens": 10, "output_tokens": 20}
    db.close()


def test_claude_history_sync_invalidates_cache_for_same_size_rewrite(tmp_path):
    projects = tmp_path / "projects"
    project_dir = projects / "-tmp-project"
    project_dir.mkdir(parents=True)
    session_file = project_dir / "session.jsonl"

    def write_session(output_tokens: int) -> None:
        lines = [
            {
                "timestamp": "2026-04-23T10:00:00Z",
                "type": "user",
                "cwd": "/tmp/project",
                "message": {"content": "hello"},
            },
            {
                "timestamp": "2026-04-23T10:00:01Z",
                "type": "assistant",
                "cwd": "/tmp/project",
                "message": {
                    "id": "msg-same-size",
                    "model": "claude-opus-4-8",
                    "usage": {"input_tokens": 10, "output_tokens": output_tokens},
                },
            },
        ]
        session_file.write_text("".join(json.dumps(line) + "\n" for line in lines))

    db = Database(db_path=str(tmp_path / "data.db"))
    collector = ClaudeCodeCollector(projects_dir=projects)
    write_session(10)
    collector.sync_history(db)
    previous_mtime = session_file.stat().st_mtime_ns

    write_session(99)
    os.utime(
        session_file,
        ns=(previous_mtime + 1_000_000, previous_mtime + 1_000_000),
    )
    collector.sync_history(db)

    row = db.conn.execute(
        "SELECT output_tokens FROM sessions "
        "WHERE session_id = 'session' AND agent_type = 'claude_code'"
    ).fetchone()
    assert row["output_tokens"] == 99
    db.close()


def test_claude_history_sync_reads_utf8_jsonl_on_windows_locale(tmp_path):
    projects = tmp_path / "projects"
    project_dir = projects / "-Users-Leo-中文项目"
    project_dir.mkdir(parents=True)
    session_file = project_dir / "session.jsonl"
    cwd = r"C:\Users\Leo\中文项目"
    lines = [
        {
            "timestamp": "2026-04-23T10:00:00Z",
            "type": "user",
            "cwd": cwd,
            "message": {"content": "hello"},
        },
        {
            "timestamp": "2026-04-23T10:00:01Z",
            "type": "assistant",
            "cwd": cwd,
            "message": {
                "id": "msg-utf8",
                "model": "claude-sonnet-4-6",
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        },
    ]
    session_file.write_bytes("".join(
        json.dumps(line, ensure_ascii=False) + "\n" for line in lines
    ).encode("utf-8"))

    import builtins

    real_open = builtins.open

    def strict_locale_open(file, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if Path(file) == session_file and "b" not in mode and kwargs.get("encoding") is None:
            raise UnicodeDecodeError("cp936", b"\x80", 0, 1, "invalid start byte")
        return real_open(file, *args, **kwargs)

    db = Database(db_path=str(tmp_path / "data.db"))
    with (
        patch("agentic_metric.collectors.claude_code.PROJECTS_DIR", projects),
        patch("builtins.open", strict_locale_open),
    ):
        ClaudeCodeCollector().sync_history(db)

    row = db.conn.execute(
        "SELECT project_path, input_tokens, output_tokens "
        "FROM sessions WHERE session_id = 'session' "
        "AND agent_type = 'claude_code'"
    ).fetchone()
    assert row is not None
    assert row["project_path"] == cwd
    assert row["input_tokens"] == 100
    assert row["output_tokens"] == 20
    db.close()


def test_claude_sessions_index_reads_utf8_on_windows_locale(tmp_path):
    projects = tmp_path / "projects"
    project_dir = projects / "-Users-Leo-中文项目"
    project_dir.mkdir(parents=True)
    index_file = project_dir / "sessions-index.json"
    index_file.write_bytes(json.dumps({
        "entries": [{
            "sessionId": "indexed-session",
            "projectPath": r"C:\Users\Leo\中文项目",
            "gitBranch": "main",
            "messageCount": 3,
            "created": "2026-04-23T10:00:00Z",
            "modified": "2026-04-23T10:05:00Z",
            "summary": "中文 summary",
        }],
    }, ensure_ascii=False).encode("utf-8"))
    (project_dir / "indexed-session.jsonl").write_text("")

    real_read_text = Path.read_text

    def strict_locale_read_text(self, *args, **kwargs):
        if self == index_file and kwargs.get("encoding") is None:
            raise UnicodeDecodeError("cp936", b"\x80", 0, 1, "invalid start byte")
        return real_read_text(self, *args, **kwargs)

    db = Database(db_path=str(tmp_path / "data.db"))
    with (
        patch("agentic_metric.collectors.claude_code.PROJECTS_DIR", projects),
        patch.object(Path, "read_text", strict_locale_read_text),
    ):
        ClaudeCodeCollector().sync_history(db)

    row = db.conn.execute(
        "SELECT project_path, message_count "
        "FROM sessions WHERE session_id = 'indexed-session' "
        "AND agent_type = 'claude_code'"
    ).fetchone()
    assert row is not None
    assert row["project_path"] == r"C:\Users\Leo\中文项目"
    assert row["message_count"] == 3
    usage = db.conn.execute(
        """SELECT message_count, user_turns, input_tokens, output_tokens,
                  cache_read_tokens, cache_creation_tokens, estimated_cost_usd
           FROM session_usage WHERE session_id = 'indexed-session'
             AND agent_type = 'claude_code'"""
    ).fetchone()
    assert tuple(usage) == (0, 0, 0, 0, 0, 0, 0.0)
    orphan = db.conn.execute(
        """SELECT 1 FROM sessions AS s
           WHERE NOT EXISTS (
               SELECT 1 FROM session_usage AS u
               WHERE u.session_id = s.session_id
                 AND u.agent_type = s.agent_type
                 AND u.provider = s.provider
                 AND u.data_root = s.data_root
           )"""
    ).fetchone()
    assert orphan is None
    db.close()


def test_claude_read_cwd_uses_utf8(tmp_path):
    session_file = tmp_path / "session.jsonl"
    cwd = r"C:\Users\Leo\中文项目"
    session_file.write_bytes(
        (json.dumps({"cwd": cwd}, ensure_ascii=False) + "\n").encode("utf-8")
    )

    import builtins

    real_open = builtins.open

    def strict_locale_open(file, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if Path(file) == session_file and "b" not in mode and kwargs.get("encoding") is None:
            raise UnicodeDecodeError("cp936", b"\x80", 0, 1, "invalid start byte")
        return real_open(file, *args, **kwargs)

    with patch("builtins.open", strict_locale_open):
        assert claude_read_cwd(session_file) == cwd


def test_codex_cross_day_session_tracks_today_counters(tmp_path):
    class FakeDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 4, 24)

    session_file = tmp_path / "rollout-test.jsonl"
    lines = [
        {
            "timestamp": "2026-04-23T08:55:00Z",
            "type": "session_meta",
            "payload": {"id": "sid", "cwd": "/tmp/project"},
        },
        {
            "timestamp": "2026-04-23T08:56:00Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "yesterday"},
        },
        {
            "timestamp": "2026-04-23T08:57:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1000,
                        "cached_input_tokens": 200,
                        "output_tokens": 100,
                    }
                },
            },
        },
        {
            "timestamp": "2026-04-24T08:01:00Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "today"},
        },
        {
            "timestamp": "2026-04-24T08:02:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1500,
                        "cached_input_tokens": 300,
                        "output_tokens": 150,
                    }
                },
            },
        },
    ]
    session_file.write_text("".join(json.dumps(line) + "\n" for line in lines))

    accum = CodexSessionAccum(session_file, project_path="/tmp/project")
    with patch("agentic_metric.collectors.codex.date", FakeDate):
        accum.read_new_lines()

    assert accum.input_tokens == 1200
    assert accum.output_tokens == 150
    assert accum.cache_read == 300
    assert accum.today_input_tokens == 400
    assert accum.today_output_tokens == 50
    assert accum.today_cache_read == 100
    assert accum.today_user_turns == 1

    buckets = {r["usage_date"]: r for r in accum.usage_bucket_rows()}
    assert buckets["2026-04-23"]["message_count"] == 1
    assert buckets["2026-04-23"]["user_turns"] == 1
    assert buckets["2026-04-23"]["input_tokens"] == 800
    assert buckets["2026-04-23"]["output_tokens"] == 100
    assert buckets["2026-04-23"]["cache_read_tokens"] == 200
    assert buckets["2026-04-24"]["message_count"] == 1
    assert buckets["2026-04-24"]["user_turns"] == 1
    assert buckets["2026-04-24"]["input_tokens"] == 400
    assert buckets["2026-04-24"]["output_tokens"] == 50
    assert buckets["2026-04-24"]["cache_read_tokens"] == 100


def test_codex_forked_session_subtracts_replayed_parent_baseline(tmp_path):
    session_file = tmp_path / "rollout-forked.jsonl"
    lines = [
        {
            "timestamp": "2026-04-24T03:05:12Z",
            "type": "session_meta",
            "payload": {
                "id": "child",
                "forked_from_id": "parent",
                "cwd": "/tmp/project",
                "source": {"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}},
            },
        },
        {
            "timestamp": "2026-04-24T03:05:12Z",
            "type": "session_meta",
            "payload": {"id": "parent", "cwd": "/tmp/project"},
        },
        {
            "timestamp": "2026-04-24T03:05:12Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "old parent prompt"},
        },
        {
            "timestamp": "2026-04-24T03:05:12Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1000,
                        "cached_input_tokens": 800,
                        "output_tokens": 100,
                    }
                },
            },
        },
        {
            "timestamp": "2026-04-24T03:05:16Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.5"},
        },
        {
            "timestamp": "2026-04-24T03:05:16Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "child task"},
        },
        {
            "timestamp": "2026-04-24T03:05:17Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1000,
                        "cached_input_tokens": 800,
                        "output_tokens": 100,
                    }
                },
            },
        },
        {
            "timestamp": "2026-04-24T03:05:18Z",
            "type": "event_msg",
            "payload": {"type": "agent_message"},
        },
        {
            "timestamp": "2026-04-24T03:05:19Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1300,
                        "cached_input_tokens": 900,
                        "output_tokens": 120,
                    }
                },
            },
        },
    ]
    session_file.write_text("".join(json.dumps(line) + "\n" for line in lines))

    accum = CodexSessionAccum(session_file, project_path="/tmp/project")
    accum.read_new_lines()

    assert accum.session_id == "child"
    assert accum.model == "gpt-5.5"
    assert accum.user_turns == 1
    assert accum.message_count == 2
    assert accum.first_prompt == "child task"
    assert accum.input_tokens == 200
    assert accum.cache_read == 100
    assert accum.output_tokens == 20


def test_codex_history_sync_skips_replayed_parent_prefix_with_real_fork_order(tmp_path):
    sessions_dir = tmp_path / "sessions"
    day_dir = sessions_dir / "2026" / "07" / "12"
    day_dir.mkdir(parents=True)

    def token_count(ts, total, last=None):
        info = {"total_token_usage": total}
        if last is not None:
            info["last_token_usage"] = last
        return {
            "timestamp": ts,
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": info,
            },
        }

    parent_meta = {
        "timestamp": "2026-07-12T10:00:00Z",
        "type": "session_meta",
        "payload": {
            "id": "parent",
            "model_provider": "openai",
            "cwd": "/tmp/project",
            "source": "cli",
        },
    }
    turn_context = {
        "timestamp": "2026-07-12T10:00:01Z",
        "type": "turn_context",
        "payload": {"model": "gpt-5.6-sol"},
    }
    first_last = {
        "input_tokens": 1_000,
        "cached_input_tokens": 800,
        "output_tokens": 100,
        "total_tokens": 1_100,
    }
    first_total = dict(first_last)
    second_last = {
        "input_tokens": 1_500,
        "cached_input_tokens": 1_000,
        "output_tokens": 200,
        "total_tokens": 1_700,
    }
    second_total = {
        "input_tokens": 2_500,
        "cached_input_tokens": 1_800,
        "output_tokens": 300,
        "total_tokens": 2_800,
    }
    parent_third_last = {
        "input_tokens": 700,
        "cached_input_tokens": 500,
        "output_tokens": 60,
        "total_tokens": 760,
    }
    parent_third_total = {
        "input_tokens": 3_200,
        "cached_input_tokens": 2_300,
        "output_tokens": 360,
        "total_tokens": 3_560,
    }
    child_total = {
        "input_tokens": 3_100,
        "cached_input_tokens": 2_200,
        "output_tokens": 350,
        "total_tokens": 3_450,
    }
    suffix_child_last = {
        "input_tokens": 500,
        "cached_input_tokens": 300,
        "output_tokens": 40,
        "total_tokens": 540,
    }
    suffix_child_total = {
        "input_tokens": 3_700,
        "cached_input_tokens": 2_600,
        "output_tokens": 400,
        "total_tokens": 4_100,
    }

    parent_lines = [
        parent_meta,
        turn_context,
        {
            "timestamp": "2026-07-12T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "parent task"},
        },
        token_count("2026-07-12T10:00:02Z", first_total, first_last),
        token_count("2026-07-12T10:00:03Z", second_total, second_last),
        token_count("2026-07-12T10:00:03Z", second_total, second_last),
        token_count(
            "2026-07-12T10:00:20Z",
            parent_third_total,
            parent_third_last,
        ),
    ]
    child_lines = [
        {
            "timestamp": "2026-07-12T10:00:10Z",
            "type": "session_meta",
            "payload": {
                "id": "child",
                "model_provider": "openai",
                "cwd": "/tmp/project",
                "forked_from_id": "parent",
                "source": {
                    "subagent": {
                        "thread_spawn": {"parent_thread_id": "parent"},
                    },
                },
            },
        },
        parent_meta,
        turn_context,
        {
            "timestamp": "2026-07-12T10:00:10Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "replayed parent task"},
        },
        token_count("2026-07-12T10:00:10Z", first_total, first_last),
        token_count("2026-07-12T10:00:10Z", second_total, second_last),
        {
            "timestamp": "2026-07-12T10:00:10Z",
            "type": "event_msg",
            "payload": {"type": "task_started"},
        },
        {
            "timestamp": "2026-07-12T10:00:11Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol"},
        },
        {
            "timestamp": "2026-07-12T10:00:11Z",
            "type": "event_msg",
            "payload": {"type": "agent_message"},
        },
        token_count("2026-07-12T10:00:12Z", child_total),
    ]
    suffix_child_lines = [
        {
            "timestamp": "2026-07-12T10:00:30Z",
            "type": "session_meta",
            "payload": {
                "id": "suffix-child",
                "model_provider": "openai",
                "cwd": "/tmp/project",
                "forked_from_id": "parent",
                "source": {
                    "subagent": {
                        "thread_spawn": {"parent_thread_id": "parent"},
                    },
                },
            },
        },
        token_count("2026-07-12T10:00:30Z", second_total, second_last),
        token_count(
            "2026-07-12T10:00:30Z",
            parent_third_total,
            parent_third_last,
        ),
        {
            "timestamp": "2026-07-12T10:00:31Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol"},
        },
        token_count(
            "2026-07-12T10:00:32Z",
            suffix_child_total,
            suffix_child_last,
        ),
    ]

    parent_file = day_dir / "rollout-2026-07-12T10-00-00-parent.jsonl"
    child_file = day_dir / "rollout-2026-07-12T10-00-10-child.jsonl"
    suffix_child_file = day_dir / "rollout-2026-07-12T10-00-30-suffix-child.jsonl"
    child_file.write_text("".join(json.dumps(line) + "\n" for line in child_lines))

    db = Database(db_path=str(tmp_path / "data.db"))
    collector = CodexCollector(
        sessions_dir=sessions_dir,
        provider="openai",
        data_root=str(tmp_path / "codex"),
    )
    collector.sync_history(db)
    child_without_parent = db.conn.execute(
        """SELECT user_turns, input_tokens, output_tokens, cache_read_tokens
           FROM sessions WHERE session_id = 'child'"""
    ).fetchone()
    assert tuple(child_without_parent) == (1, 900, 350, 2_200)

    parent_file.write_text(
        "".join(json.dumps(line) + "\n" for line in parent_lines[:4])
    )
    collector.sync_history(db)
    child_with_partial_parent = db.conn.execute(
        """SELECT user_turns, input_tokens, output_tokens, cache_read_tokens
           FROM sessions WHERE session_id = 'child'"""
    ).fetchone()
    assert tuple(child_with_partial_parent) == (0, 200, 50, 400)

    parent_file.write_text("".join(json.dumps(line) + "\n" for line in parent_lines))
    suffix_child_file.write_text(
        "".join(json.dumps(line) + "\n" for line in suffix_child_lines)
    )
    collector.sync_history(db)

    rows = db.conn.execute(
        """SELECT session_id, user_turns, input_tokens, output_tokens,
                  cache_read_tokens
           FROM sessions
           WHERE agent_type = 'codex'
           ORDER BY session_id"""
    ).fetchall()
    expected = [
        ("child", 0, 200, 50, 400),
        ("parent", 1, 900, 360, 2_300),
        ("suffix-child", 0, 200, 40, 300),
    ]
    assert [tuple(row) for row in rows] == expected

    parent_file.rename(tmp_path / "archived-parent.jsonl")
    collector.sync_history(db)
    rows = db.conn.execute(
        """SELECT session_id, user_turns, input_tokens, output_tokens,
                  cache_read_tokens
           FROM sessions
           WHERE agent_type = 'codex'
           ORDER BY session_id"""
    ).fetchall()
    assert [tuple(row) for row in rows] == expected
    db.close()


def test_codex_fork_replay_abort_keeps_observed_child_turn_context(tmp_path):
    sessions_dir = tmp_path / "sessions"
    day_dir = sessions_dir / "2026" / "04" / "26"
    day_dir.mkdir(parents=True)

    def token_count(ts, input_tokens, cached_input_tokens, output_tokens):
        return {
            "timestamp": ts,
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": input_tokens,
                        "cached_input_tokens": cached_input_tokens,
                        "output_tokens": output_tokens,
                    },
                },
            },
        }

    parent_lines = [
        {
            "timestamp": "2026-04-26T22:16:10Z",
            "type": "session_meta",
            "payload": {
                "id": "parent",
                "model_provider": "openai",
                "cwd": "/tmp/project",
            },
        },
        {
            "timestamp": "2026-04-26T22:16:11Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.4"},
        },
        token_count("2026-04-26T22:16:12Z", 1_000, 800, 100),
        token_count("2026-04-26T22:16:13Z", 2_500, 1_800, 300),
        token_count("2026-04-26T22:16:14Z", 3_200, 2_300, 360),
    ]
    child_lines = [
        {
            "timestamp": "2026-04-26T22:22:51Z",
            "type": "session_meta",
            "payload": {
                "id": "child",
                "model_provider": "openai",
                "cwd": "/tmp/project",
                "forked_from_id": "parent",
            },
        },
        token_count("2026-04-26T22:22:52Z", 1_000, 800, 100),
        {
            "timestamp": "2026-04-26T22:22:53Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.4"},
        },
        token_count("2026-04-26T22:22:54Z", 2_500, 1_800, 300),
        token_count("2026-04-26T22:22:55Z", 3_100, 2_200, 350),
    ]

    (day_dir / "rollout-parent.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in parent_lines)
    )
    (day_dir / "rollout-child.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in child_lines)
    )

    db = Database(db_path=str(tmp_path / "data.db"))
    collector = CodexCollector(
        sessions_dir=sessions_dir,
        provider="openai",
        data_root=str(tmp_path / "codex"),
    )
    collector.sync_history(db)

    child = db.conn.execute(
        """SELECT model, input_tokens, output_tokens, cache_read_tokens
           FROM sessions WHERE session_id = 'child'"""
    ).fetchone()
    assert tuple(child) == ("gpt-5.4", 200, 50, 400)
    db.close()


def test_codex_forked_compatible_session_keeps_separate_input_semantics(tmp_path):
    session_file = tmp_path / "rollout-forked.jsonl"
    lines = [
        {
            "timestamp": "2026-04-24T03:05:12Z",
            "type": "session_meta",
            "payload": {
                "id": "child",
                "model_provider": "ichat",
                "forked_from_id": "parent",
                "cwd": "/tmp/project",
                "source": {"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}},
            },
        },
        {
            "timestamp": "2026-04-24T03:05:12Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1_000,
                        "cached_input_tokens": 800,
                        "output_tokens": 100,
                        "total_tokens": 1_900,
                    }
                },
            },
        },
        {
            "timestamp": "2026-04-24T03:05:16Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.5"},
        },
        {
            "timestamp": "2026-04-24T03:05:16Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "child task"},
        },
        {
            "timestamp": "2026-04-24T03:05:17Z",
            "type": "event_msg",
            "payload": {"type": "agent_message"},
        },
        {
            "timestamp": "2026-04-24T03:05:19Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1_300,
                        "cached_input_tokens": 900,
                        "output_tokens": 120,
                        "total_tokens": 2_320,
                    }
                },
            },
        },
    ]
    session_file.write_text("".join(json.dumps(line) + "\n" for line in lines))

    accum = CodexSessionAccum(
        session_file,
        project_path="/tmp/project",
        provider="ichat",
    )
    accum.read_new_lines()

    assert accum.session_id == "child"
    assert accum.provider == "ichat"
    assert accum.input_tokens == 300
    assert accum.cache_read == 100
    assert accum.output_tokens == 20


def test_codex_history_sync_detects_same_size_file_edits(tmp_path):
    def write_rollout(path: Path, output_tokens: int) -> None:
        lines = [
            {
                "timestamp": "2026-04-23T10:00:00Z",
                "type": "session_meta",
                "payload": {"id": "sid", "cwd": "/tmp/project", "git": {"branch": "main"}},
            },
            {
                "timestamp": "2026-04-23T10:00:00Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.4"},
            },
            {
                "timestamp": "2026-04-23T10:00:01Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "hello"},
            },
            {
                "timestamp": "2026-04-23T10:00:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 0,
                            "output_tokens": output_tokens,
                        }
                    },
                },
            },
        ]
        path.write_text("".join(json.dumps(line) + "\n" for line in lines))
        os.utime(path, None)

    sessions_dir = tmp_path / "sessions" / "2026" / "04" / "23"
    sessions_dir.mkdir(parents=True)
    rollout = sessions_dir / "rollout-test.jsonl"
    db = Database(db_path=str(tmp_path / "data.db"))
    collector = CodexCollector()

    with patch("agentic_metric.collectors.codex.CODEX_SESSIONS_DIR", tmp_path / "sessions"):
        write_rollout(rollout, 10)
        collector.sync_history(db)
        row = db.conn.execute(
            "SELECT output_tokens FROM sessions WHERE session_id = 'sid' AND agent_type = 'codex'"
        ).fetchone()
        assert row["output_tokens"] == 10

        write_rollout(rollout, 99)
        collector.sync_history(db)
        row = db.conn.execute(
            "SELECT output_tokens FROM sessions WHERE session_id = 'sid' AND agent_type = 'codex'"
        ).fetchone()
        assert row["output_tokens"] == 99

    db.close()


def test_codex_history_sync_cost_uses_bucket_models(tmp_path):
    sessions_dir = tmp_path / "sessions" / "2026" / "04" / "23"
    sessions_dir.mkdir(parents=True)
    rollout = sessions_dir / "rollout-test.jsonl"
    lines = [
        {
            "timestamp": "2026-04-23T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": "sid", "cwd": "/tmp/project"},
        },
        {
            "timestamp": "2026-04-23T10:00:00Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.4"},
        },
        {
            "timestamp": "2026-04-23T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "first"},
        },
        {
            "timestamp": "2026-04-23T10:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1000,
                        "cached_input_tokens": 0,
                        "output_tokens": 100,
                    }
                },
            },
        },
        {
            "timestamp": "2026-04-24T10:00:00Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.5"},
        },
        {
            "timestamp": "2026-04-24T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "second"},
        },
        {
            "timestamp": "2026-04-24T10:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 2000,
                        "cached_input_tokens": 0,
                        "output_tokens": 200,
                    }
                },
            },
        },
    ]
    rollout.write_text("".join(json.dumps(line) + "\n" for line in lines))

    db = Database(db_path=str(tmp_path / "data.db"))
    with patch("agentic_metric.collectors.codex.CODEX_SESSIONS_DIR", tmp_path / "sessions"):
        CodexCollector().sync_history(db)

    row = db.conn.execute(
        "SELECT estimated_cost_usd FROM sessions WHERE session_id = 'sid' AND agent_type = 'codex'"
    ).fetchone()
    assert abs(row["estimated_cost_usd"] - 0.012) < 1e-12

    db.close()
