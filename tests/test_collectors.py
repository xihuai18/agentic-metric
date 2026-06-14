"""Tests for collector module."""

import json
import os
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from agentic_metric.collectors import CollectorRegistry, BaseCollector, create_default_registry
from agentic_metric.collectors.claude_code import (
    ClaudeCodeCollector,
    _LiveMonitor as ClaudeLiveMonitor,
    _SessionAccum as ClaudeSessionAccum,
)
from agentic_metric.collectors.codex import (
    CodexCollector,
    _LiveMonitor as CodexLiveMonitor,
    _SessionAccum as CodexSessionAccum,
)
from agentic_metric.collectors.remote import (
    RemoteHistoryCollector,
    RemoteSyncTarget,
    _cache_root_for,
    _manifest_command,
    _ssh_command,
)
from agentic_metric.config import RemoteCollectorRoot, RemoteSpec, get_remote_specs
from agentic_metric.collectors._process import find_pids, get_pid_cwd, normalize_cwd_key
from agentic_metric.models import LiveSession
from agentic_metric.pricing import estimate_cost
from agentic_metric.store.database import Database


class MockCollector(BaseCollector):
    @property
    def agent_type(self) -> str:
        return "mock"

    def get_live_sessions(self) -> list[LiveSession]:
        return [
            LiveSession(
                session_id="test-1",
                agent_type="mock",
                project_path="/test/project",
                user_turns=5,
                output_tokens=1000,
            )
        ]

    def sync_history(self, db) -> None:
        pass


def test_registry_register():
    registry = CollectorRegistry()
    collector = MockCollector()
    registry.register(collector)
    assert len(registry.get_all()) == 1
    assert registry.get_all()[0].agent_type == "mock"


def test_registry_get_live_sessions():
    registry = CollectorRegistry()
    registry.register(MockCollector())
    sessions = registry.get_live_sessions()
    assert len(sessions) == 1
    assert sessions[0].session_id == "test-1"
    assert sessions[0].agent_type == "mock"


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


def test_live_session_total_tokens():
    s = LiveSession(
        session_id="x",
        agent_type="test",
        project_path="/test",
        input_tokens=100,
        output_tokens=200,
    )
    assert s.total_tokens == 300


def test_live_session_duration():
    s = LiveSession(
        session_id="x",
        agent_type="test",
        project_path="/test",
        started="2025-01-01T10:00:00Z",
        last_active="2025-01-01T10:30:00Z",
    )
    assert abs(s.duration_minutes - 30.0) < 0.1


def test_codex_session_meta_provider_sets_agent_type():
    accum = CodexSessionAccum(Path("/tmp/fake.jsonl"), project_path="/test")
    accum._process_entry({
        "type": "session_meta",
        "payload": {
            "id": "sid",
            "cwd": "/test",
            "model_provider": "custom",
        },
    })

    live = accum.to_live_session()
    assert live.agent_type == "codex"
    assert live.provider == "custom"


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

    live = accum.to_live_session()
    assert live.agent_type == "codex"
    assert live.provider == "openai"


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
        "SELECT COUNT(*) AS n FROM sync_state WHERE key LIKE 'codex_jsonl:v8:%'"
    ).fetchone()["n"] == 4
    db.close()


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


def test_remote_removed_file_is_archived_not_reparsed(tmp_path):
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
        ).fetchone()["n"] == 0
        assert db.conn.execute(
            "SELECT COUNT(*) AS n FROM session_usage WHERE session_id = 'removed-sid'"
        ).fetchone()["n"] == 0
        db.close()


def test_remote_removed_claude_file_purges_existing_db_usage(tmp_path):
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
        ).fetchone()["n"] == 0
        assert db.conn.execute(
            "SELECT COUNT(*) AS n FROM session_usage WHERE session_id = 'removed-claude'"
        ).fetchone()["n"] == 0
        db.close()


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


def test_codex_cumulative_fallback_allows_negative_reclassification():
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
    assert sum(r["input_tokens"] for r in rows) == 800
    assert sum(r["cache_read_tokens"] for r in rows) == 200
    expected = (
        estimate_cost(
            "gpt-5.5",
            input_tokens=1_000,
            output_tokens=100,
            apply_long_context=False,
        )
        + estimate_cost(
            "gpt-5.5",
            input_tokens=-200,
            cache_read_tokens=200,
            apply_long_context=False,
        )
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
                    "cache_creation_input_tokens": 5,
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
                    "cache_creation_input_tokens": 5,
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
    assert accum.cache_create == 5
    assert sum(r["message_count"] for r in accum.usage_bucket_rows()) == 2
    assert sum(r["input_tokens"] for r in accum.usage_bucket_rows()) == 10
    assert sum(r["output_tokens"] for r in accum.usage_bucket_rows()) == 20
    assert sum(r["cache_read_tokens"] for r in accum.usage_bucket_rows()) == 100
    assert sum(r["cache_creation_tokens"] for r in accum.usage_bucket_rows()) == 5
    expected_cost = estimate_cost(
        "claude-sonnet-4-6",
        input_tokens=10,
        output_tokens=20,
        cache_read_tokens=100,
        cache_creation_tokens=5,
    )
    assert abs(sum(r["estimated_cost_usd"] for r in accum.usage_bucket_rows()) - expected_cost) < 1e-12


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
        assert ClaudeLiveMonitor._read_cwd(session_file) == cwd


def test_codex_cross_day_live_session_uses_today_counters(tmp_path):
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

    live = accum.to_live_session()
    assert live.input_tokens == 1200
    assert live.output_tokens == 150
    assert live.cache_read_tokens == 300
    assert live.today_input_tokens == 400
    assert live.today_output_tokens == 50
    assert live.today_cache_read_tokens == 100
    assert live.today_user_turns == 1

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


def test_codex_live_monitor_finds_older_active_session(tmp_path):
    class FakeDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 4, 24)

    sessions_root = tmp_path / "sessions"
    old_dir = sessions_root / "2026" / "04" / "20"
    old_dir.mkdir(parents=True)
    rollout = old_dir / "rollout-old.jsonl"
    lines = [
        {
            "timestamp": "2026-04-20T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": "old-sid", "cwd": "/tmp/project"},
        },
        {
            "timestamp": "2026-04-20T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "still running"},
        },
    ]
    rollout.write_text("".join(json.dumps(line) + "\n" for line in lines))

    monitor = CodexLiveMonitor()
    with (
        patch("agentic_metric.collectors.codex.CODEX_SESSIONS_DIR", sessions_root),
        patch("agentic_metric.collectors.codex.get_running_cwds", return_value={123: "/tmp/project"}),
        patch("agentic_metric.collectors.codex.date", FakeDate),
    ):
        sessions = monitor.refresh()

    assert len(sessions) == 1
    assert sessions[0].session_id == "old-sid"


def test_windows_cwd_normalization_matches_codex_live_session(tmp_path):
    class FakeDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 4, 24)

    with patch("agentic_metric.collectors._process.platform.system", return_value="Windows"):
        assert normalize_cwd_key(r"C:\Users\Leo\Repo") == normalize_cwd_key("c:/users/leo/repo")

    sessions_root = tmp_path / "sessions"
    old_dir = sessions_root / "2026" / "04" / "24"
    old_dir.mkdir(parents=True)
    rollout = old_dir / "rollout-win.jsonl"
    lines = [
        {
            "timestamp": "2026-04-24T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": "win-sid", "cwd": r"C:\Users\Leo\Repo"},
        },
        {
            "timestamp": "2026-04-24T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "still running"},
        },
    ]
    rollout.write_text("".join(json.dumps(line) + "\n" for line in lines))

    monitor = CodexLiveMonitor()
    with (
        patch("agentic_metric.collectors.codex.CODEX_SESSIONS_DIR", sessions_root),
        patch("agentic_metric.collectors.codex.get_running_cwds", return_value={123: "c:/users/leo/repo"}),
        patch("agentic_metric.collectors.codex.date", FakeDate),
        patch("agentic_metric.collectors._process.platform.system", return_value="Windows"),
    ):
        sessions = monitor.refresh()

    assert len(sessions) == 1
    assert sessions[0].session_id == "win-sid"


def test_get_pid_cwd_falls_back_when_psutil_cwd_fails():
    import agentic_metric.collectors._process as proc

    class FakeAccessDenied(Exception):
        pass

    class FakePsutil:
        NoSuchProcess = RuntimeError
        AccessDenied = FakeAccessDenied
        ZombieProcess = RuntimeError

        class Process:
            def __init__(self, pid):
                self.pid = pid

            def cwd(self):
                raise FakeAccessDenied()

    with (
        patch.object(proc, "psutil", FakePsutil),
        patch("agentic_metric.collectors._process.platform.system", return_value="Linux"),
        patch("agentic_metric.collectors._process.Path.resolve", return_value=Path("/tmp/fallback")),
    ):
        assert get_pid_cwd(123) == "/tmp/fallback"


def test_find_pids_uses_windows_tasklist_fallback():
    import subprocess
    import agentic_metric.collectors._process as proc

    result = subprocess.CompletedProcess(
        ["tasklist"],
        0,
        stdout='"codex.exe","123","Console","1","10,000 K"\n"other.exe","999","Console","1","1,000 K"\n',
        stderr="",
    )
    with (
        patch.object(proc, "psutil", None),
        patch("agentic_metric.collectors._process.platform.system", return_value="Windows"),
        patch("agentic_metric.collectors._process.subprocess.run", return_value=result),
    ):
        assert find_pids("codex", exact=True) == [123]


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
