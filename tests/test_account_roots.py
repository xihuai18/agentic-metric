import json

import pytest

import agentic_metric.config as config
from agentic_metric.accounts import (
    account_report, add_payment, set_account_profile, validate_account_key,
)
from agentic_metric.store.database import Database

CODEX = "codex:" + "a" * 64
CLAUDE = "claude:" + "b" * 64
CLAUDE_REMOTE = "claude:" + "c" * 64


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_FILE", path)
    monkeypatch.setattr(config, "CLAUDE_HOME", tmp_path / "default-claude")
    monkeypatch.setattr(config, "CODEX_HOME", tmp_path / "default-codex")

    def write(data):
        path.write_text(json.dumps(data), encoding="utf-8")
    return write


def test_string_roots_and_defaults_have_no_account(cfg):
    cfg({"collectors": {"claude_code": ["~/.claude-a"], "codex": {"roots": ["~/.codex-a"]}}})
    assert [r.account_key for r in config.get_claude_code_roots()] == [""]
    assert [r.account_key for r in config.get_codex_roots()] == [""]
    cfg({})
    assert config.get_claude_code_roots()[0].account_key == ""
    assert config.get_codex_roots()[0].account_key == ""


def test_object_roots_across_providers_keep_independent_keys(cfg):
    cfg({"collectors": {
        "claude_code": {"roots": [
            {"path": "~/.claude-a", "account_key": CLAUDE},
            {"path": "~/.claude-b"},
            "~/.claude-c",
        ]},
        "codex": [{"path": "~/.codex-a", "provider": "openai", "account_key": CODEX}],
    }})
    claude = config.get_claude_code_roots()
    assert [r.account_key for r in claude] == [CLAUDE, "", ""]
    assert [r.raw_path for r in claude] == ["~/.claude-a", "~/.claude-b", "~/.claude-c"]
    codex = config.get_codex_roots()
    assert (codex[0].account_key, codex[0].provider) == (CODEX, "openai")


def test_remote_keys_are_independent_and_never_inherited_from_local(cfg):
    cfg({
        "collectors": {"claude_code": [{"path": "~/.claude-a", "account_key": CLAUDE}]},
        "remotes": [
            {"host": "omitted"},
            {"host": "explicit", "collectors": {
                "claude_code": {"roots": [{"path": "~/.claude", "account_key": CLAUDE_REMOTE}, "~/.claude-2"]},
                "codex": [{"path": "~/.codex", "account_key": CODEX}],
            }},
            "string-host",
        ],
    })
    assert config.get_claude_code_roots()[0].account_key == CLAUDE
    omitted, explicit, plain = config.get_remote_specs()
    assert omitted.collectors["claude_code"] == [config.RemoteCollectorRoot("~/.claude-a")]
    assert plain.collectors["claude_code"][0].account_key == ""
    assert [r.account_key for r in explicit.collectors["claude_code"]] == [CLAUDE_REMOTE, ""]
    assert explicit.collectors["codex"][0].account_key == CODEX
    assert config.get_claude_code_roots()[0].account_key == CLAUDE


@pytest.mark.parametrize("bad", [
    "", " ", "claude:" + "B" * 64, "claude:" + "b" * 63, "claude:" + "b" * 64 + "\n",
    "claude:user@example.com", "other:" + "b" * 64, 7, ["x"], {"a": 1}, False,
])
def test_invalid_local_account_key_raises_instead_of_defaulting(cfg, bad):
    cfg({"collectors": {"claude_code": [{"path": "~/.claude-a", "account_key": bad}]}})
    with pytest.raises(ValueError, match="collectors.claude_code.roots"):
        config.get_claude_code_roots()


@pytest.mark.parametrize("bad", [123, "claude:xyz", ""])
def test_invalid_remote_account_key_raises_with_host(cfg, bad):
    cfg({"remotes": [{"host": "dev", "collectors": {
        "claude_code": [{"path": "~/.claude", "account_key": bad}]}}]})
    with pytest.raises(ValueError, match=r"remotes\[dev\]\.collectors\.claude_code"):
        config.get_remote_specs()


def test_null_account_key_is_unset(cfg):
    cfg({"collectors": {"codex": [{"path": "~/.codex-a", "account_key": None}]}})
    assert config.get_codex_roots()[0].account_key == ""


def test_claude_keys_work_for_profiles_payments_and_reports_without_changing_balances(tmp_path):
    db = Database(str(tmp_path / "roots.db"))
    try:
        assert validate_account_key(CLAUDE) == CLAUDE
        for bad in ("claude:" + "B" * 64, "claude:" + "b" * 65, "claude:"):
            with pytest.raises(ValueError):
                validate_account_key(bad)
        db.replace_session_usage("s", "claude_code", [
            {"usage_date": "2026-10-12", "usage_hour": 1, "model": "claude-sonnet-5-5",
             "account_key": CLAUDE, "input_tokens": 10, "message_count": 2,
             "user_turns": 1, "estimated_cost_usd": 2.5},
        ], provider="", data_root="root")
        db.commit()
        before = account_report(db.conn, "2026-10-01", "2026-10-31")["accounts"][0]
        set_account_profile(db.conn, CLAUDE, "Claude work", "Max")
        add_payment(db.conn, CLAUDE, "100", paid_on="2026-10-02", kind="subscription")
        after = account_report(db.conn, "2026-10-01", "2026-10-31")["accounts"][0]
        assert after["account_key"] == CLAUDE and after["label"] == "Claude work"
        assert after["api_equivalent_usd"] == before["api_equivalent_usd"] == 2.5
        assert after["requests"] == before["requests"] == 1
        assert after["payments"] == [{"currency": "USD", "kind": "subscription", "amount_minor": 10000}]
    finally:
        db.close()
