import asyncio
import json

import pytest
from typer.testing import CliRunner

from agentic_metric.accounts import (
    account_report, add_payment, date_range, format_account_report,
    list_payments, remove_payment, set_account_profile,
)
from agentic_metric.store.database import Database

KEY_A = "codex:" + "a" * 64
KEY_B = "codex:" + "b" * 64
START, END = "2026-10-01", "2026-10-31"


@pytest.fixture
def db(tmp_path, monkeypatch):
    import agentic_metric.pricing as pricing
    import agentic_metric.store.database as database

    monkeypatch.setattr(database, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pricing, "PRICING_FILE", tmp_path / "pricing.json")
    monkeypatch.setattr(pricing, "PRICING_CATALOG_FILE", tmp_path / "catalog.json")
    pricing._user_cache = pricing._catalog_cache = None
    pricing._reset_derived_caches()
    value = Database(str(tmp_path / "accounts.db"))
    yield value
    value.close()


def seed(db):
    db.replace_session_usage("session", "claude_code", [
        {"usage_date": "2026-10-12", "usage_hour": 10, "model": "gpt-6-astra",
         "account_key": key, "input_tokens": 100, "message_count": 1,
         "estimated_cost_usd": cost}
        for key, cost in [(KEY_A, 3.25), (KEY_B, 1.75), ("", None)]
    ], provider="codex", data_root="fixture-root")
    db.commit()


def test_equivalents_payments_and_unknown_accounts_remain_separate(db):
    seed(db)
    set_account_profile(db.conn, KEY_A, "Work account", "Pro 200")
    add_payment(db.conn, KEY_A, "200.10", paid_on="2026-10-03", kind="subscription", plan="Pro 200")
    add_payment(db.conn, KEY_A, "10.20", paid_on="2026-10-04", kind="credits")
    add_payment(db.conn, KEY_A, "80", paid_on="2026-10-04", kind="credits", currency="CNY")
    add_payment(db.conn, KEY_A, "999", paid_on="2026-09-30", kind="subscription")
    result = account_report(db.conn, START, END)
    rows = {row["account_key"]: row for row in result["accounts"]}
    assert rows[KEY_A]["api_equivalent_usd"] == 3.25
    assert rows[KEY_B]["api_equivalent_usd"] == 1.75
    assert rows[""]["api_equivalent_usd"] is None
    assert rows[""]["unpriced_buckets"] == 1
    assert rows[""]["label"] == "Unknown account"
    assert rows[KEY_B]["payments"] == []
    assert {(row["currency"], row["kind"]): row["amount_minor"] for row in rows[KEY_A]["payments"]} == {
        ("USD", "subscription"): 20010, ("USD", "credits"): 1020, ("CNY", "credits"): 8000,
    }
    text = format_account_report(result)
    assert "API equivalent: USD 3.25" in text
    assert "Recorded subscription: USD 200.10" in text
    assert "actual spend unknown" in text
    assert "not measured credit burn" in text


def test_payment_references_are_idempotent_and_plan_snapshots_are_preserved(db):
    set_account_profile(db.conn, KEY_A, "Personal", "Pro 200")
    kwargs = dict(paid_on="2026-10-02", kind="subscription", payment_id="invoice-1", plan="Pro 200")
    assert add_payment(db.conn, KEY_A, "200", **kwargs) == "invoice-1"
    assert add_payment(db.conn, KEY_A, "200.00", **kwargs) == "invoice-1"
    with pytest.raises(ValueError, match="different data"):
        add_payment(db.conn, KEY_A, "500", **kwargs)
    set_account_profile(db.conn, KEY_A, "Personal", "Pro 500")
    assert list_payments(db.conn, START, END)[0]["plan"] == "Pro 200"
    assert account_report(db.conn, START, END)["accounts"][0]["manual_plan"] == "Pro 500"
    assert remove_payment(db.conn, "invoice-1")
    assert not remove_payment(db.conn, "invoice-1")


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1", "0.001", "1e20", "invalid"])
def test_invalid_payment_amounts_are_rejected(db, amount):
    with pytest.raises(ValueError):
        add_payment(db.conn, KEY_A, amount, paid_on="2026-10-01", kind="credits")


def test_manual_accounts_and_input_boundaries(db):
    set_account_profile(db.conn, "manual:standalone", "Standalone", "Plus")
    add_payment(db.conn, "manual:standalone", "20", paid_on="2026-10-01", kind="subscription")
    assert account_report(db.conn, START, END)["accounts"][0]["requests"] == 0
    with pytest.raises(ValueError):
        set_account_profile(db.conn, "", "Unknown", "Plus")
    with pytest.raises(ValueError):
        set_account_profile(db.conn, KEY_A, "name\x1b[31m", "Plus")
    with pytest.raises(ValueError):
        add_payment(db.conn, KEY_A, "20", paid_on="2026-02-30", kind="subscription")
    with pytest.raises(ValueError):
        add_payment(db.conn, KEY_A, "20", paid_on="2026-10-01", kind="quota")
    with pytest.raises(ValueError):
        date_range(END, START)


def test_account_cli_round_trip_without_network(db, monkeypatch):
    from agentic_metric.cli import app
    import agentic_metric.store.database as database

    path = db._path
    monkeypatch.setattr(database, "Database", lambda: Database(path))
    runner = CliRunner()
    assert runner.invoke(app, ["accounts", "set", KEY_A, "--label", "Work", "--plan", "Pro 200"]).exit_code == 0
    args = ["accounts", "payments", "add", KEY_A, "200", "--date", "2026-10-01",
            "--kind", "subscription", "--id", "october", "--plan", "Pro 200"]
    assert runner.invoke(app, args).exit_code == 0
    assert runner.invoke(app, args).exit_code == 0
    report = runner.invoke(app, ["accounts", "report", "--start", START, "--end", END, "--json"])
    assert report.exit_code == 0, report.output
    data = json.loads(report.output)
    assert len(data["payments"]) == 1
    assert data["payments"][0]["amount_minor"] == 20000
    assert runner.invoke(app, ["accounts", "payments", "remove", "october"]).exit_code == 0
    invalid = runner.invoke(app, ["accounts", "payments", "add", KEY_A, "NaN", "--date", START, "--kind", "credits"])
    assert invalid.exit_code != 0


def test_accounts_screen_shows_separate_values_and_refreshes(db):
    from agentic_metric.collectors import CollectorRegistry
    from agentic_metric.tui.app import AgenticMetricApp
    from agentic_metric.tui.accounts_screen import AccountsScreen
    from textual.widgets import Static

    seed(db)
    set_account_profile(db.conn, KEY_A, "Work", "Pro 200")

    async def exercise():
        app = AgenticMetricApp(db=db, collectors=CollectorRegistry(), sync_on_mount=False)
        async with app.run_test(size=(90, 32)) as pilot:
            app.push_screen(AccountsScreen(lambda: account_report(db.conn, START, END)))
            await pilot.pause()
            text = str(app.screen.query_one("#accounts-content", Static).render())
            assert "USD 3.25" in text
            assert "Unknown account" in text
            add_payment(db.conn, KEY_A, "200", paid_on=START, kind="subscription")
            app.screen.refresh_report()
            text = str(app.screen.query_one("#accounts-content", Static).render())
            assert "Recorded subscription: USD 200.00" in text
            await pilot.press("escape")
            assert not isinstance(app.screen, AccountsScreen)

    asyncio.run(exercise())
