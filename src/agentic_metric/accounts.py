"""Account-attributed API equivalents and a separate manual cash ledger."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import re
import sqlite3
import unicodedata
from uuid import uuid4

_ACCOUNT_KEY = re.compile(r"(?:codex:[0-9a-f]{64}|manual:[a-z0-9][a-z0-9_.-]{0,79})\Z")


def ensure_account_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS account_profiles (
        account_key TEXT PRIMARY KEY, label TEXT NOT NULL, plan TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS account_payments (
        payment_id TEXT PRIMARY KEY, account_key TEXT NOT NULL,
        paid_on TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('subscription', 'credits')),
        amount_minor INTEGER NOT NULL CHECK(amount_minor >= 0), currency TEXT NOT NULL,
        plan TEXT NOT NULL, note TEXT NOT NULL
    )""")


def validate_account_key(value: str) -> str:
    if not _ACCOUNT_KEY.fullmatch(value):
        raise ValueError("Use an observed codex:<fingerprint> or manual:<name> account key")
    return value


def _text(value: str, limit: int) -> str:
    if len(value) > limit or any(unicodedata.category(c) == "Cc" for c in value):
        raise ValueError(f"Text must be at most {limit} characters without control characters")
    return value.strip()


def validate_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError("Date must use YYYY-MM-DD") from None
    if parsed.isoformat() != value:
        raise ValueError("Date must use YYYY-MM-DD")
    return value


def set_account_profile(conn: sqlite3.Connection, account_key: str, label: str, plan: str) -> None:
    key = validate_account_key(account_key)
    label, plan = _text(label, 80), _text(plan, 80)
    if not label:
        raise ValueError("Account label must not be empty")
    ensure_account_tables(conn)
    conn.execute("""INSERT INTO account_profiles VALUES (?, ?, ?)
        ON CONFLICT(account_key) DO UPDATE SET label=excluded.label, plan=excluded.plan""",
        (key, label, plan))
    conn.commit()


def add_payment(
    conn: sqlite3.Connection, account_key: str, amount: str, *, paid_on: str,
    kind: str, currency: str = "USD", plan: str = "", note: str = "",
    payment_id: str | None = None,
) -> str:
    key = validate_account_key(account_key)
    paid_on = validate_date(paid_on)
    if kind not in {"subscription", "credits"}:
        raise ValueError("Payment kind must be subscription or credits")
    currency = currency.upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("Currency must be a three-letter code; currencies are never converted")
    try:
        value = Decimal(amount)
    except InvalidOperation:
        raise ValueError("Amount must be a nonnegative decimal with at most two decimal places") from None
    if not value.is_finite() or value < 0 or value > Decimal("999999999999.99") or value * 100 != (value * 100).to_integral_value():
        raise ValueError("Amount must be nonnegative, at most 999999999999.99, with at most two decimal places")
    plan, note = _text(plan, 80), _text(note, 500)
    identifier = _text(payment_id, 120) if payment_id is not None else str(uuid4())
    if not identifier:
        raise ValueError("Payment id must not be empty")
    ensure_account_tables(conn)
    values = (identifier, key, paid_on, kind, int(value * 100), currency, plan, note)
    existing = conn.execute("SELECT * FROM account_payments WHERE payment_id = ?", (identifier,)).fetchone()
    if existing is not None:
        if tuple(existing) == values:
            return identifier
        raise ValueError("Payment id already exists with different data")
    conn.execute("INSERT INTO account_payments VALUES (?, ?, ?, ?, ?, ?, ?, ?)", values)
    conn.commit()
    return identifier


def remove_payment(conn: sqlite3.Connection, payment_id: str) -> bool:
    ensure_account_tables(conn)
    deleted = conn.execute("DELETE FROM account_payments WHERE payment_id = ?", (payment_id,)).rowcount
    conn.commit()
    return bool(deleted)


def date_range(start: str | None = None, end: str | None = None) -> tuple[str, str]:
    today = date.today()
    start = validate_date(start or today.replace(day=1).isoformat())
    end = validate_date(end or today.isoformat())
    if start > end:
        raise ValueError("Start date must not be after end date")
    return start, end


def list_payments(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    start, end = date_range(start, end)
    ensure_account_tables(conn)
    return [dict(row) for row in conn.execute(
        "SELECT * FROM account_payments WHERE paid_on BETWEEN ? AND ? ORDER BY paid_on, payment_id",
        (start, end),
    )]


def account_report(conn: sqlite3.Connection, start: str, end: str) -> dict:
    start, end = date_range(start, end)
    ensure_account_tables(conn)
    profiles = {row["account_key"]: dict(row) for row in conn.execute("SELECT * FROM account_profiles")}
    usage = {row["account_key"]: dict(row) for row in conn.execute("""
        SELECT account_key, SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
            SUM(cache_read_tokens) AS cache_read_tokens, SUM(cache_creation_tokens) AS cache_creation_tokens,
            SUM(message_count - user_turns) AS requests,
            COALESCE(SUM(estimated_cost_usd), 0) AS known_api_equivalent_usd,
            SUM(CASE WHEN estimated_cost_usd IS NULL AND
                input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens > 0
                THEN 1 ELSE 0 END) AS unpriced_buckets
        FROM session_usage WHERE usage_date BETWEEN ? AND ? GROUP BY account_key
    """, (start, end))}
    payments = list_payments(conn, start, end)
    keys = set(profiles) | set(usage) | {row["account_key"] for row in payments}
    accounts = []
    for key in sorted(keys):
        profile, activity = profiles.get(key, {}), usage.get(key, {})
        paid: dict[tuple[str, str], int] = {}
        for payment in payments:
            if payment["account_key"] == key:
                group = (payment["currency"], payment["kind"])
                paid[group] = paid.get(group, 0) + payment["amount_minor"]
        unpriced = activity.get("unpriced_buckets", 0)
        known = activity.get("known_api_equivalent_usd", 0)
        accounts.append({
            "account_key": key,
            "label": profile.get("label") or (key if key else "Unknown account"),
            "manual_plan": profile.get("plan", ""),
            "api_equivalent_usd": None if unpriced else known,
            "known_api_equivalent_usd": known,
            "unpriced_buckets": unpriced,
            "requests": activity.get("requests", 0),
            "input_tokens": activity.get("input_tokens", 0),
            "output_tokens": activity.get("output_tokens", 0),
            "cache_read_tokens": activity.get("cache_read_tokens", 0),
            "cache_creation_tokens": activity.get("cache_creation_tokens", 0),
            "payments": [{"currency": currency, "kind": kind, "amount_minor": amount}
                         for (currency, kind), amount in sorted(paid.items())],
        })
    return {"start": start, "end": end, "accounts": accounts, "payments": payments}


def format_money(amount_minor: int, currency: str) -> str:
    return f"{currency} {amount_minor // 100:,}.{amount_minor % 100:02d}"


def format_account_report(report: dict) -> str:
    lines = [f"Accounts: {report['start']} to {report['end']}",
             "API equivalents are estimates, not subscription bills.",
             "Payments are manual cash entries by payment date, not measured credit burn.",
             "Plan labels are manually maintained current labels, not historical entitlements.", ""]
    if not report["accounts"]:
        lines.append("No account usage or manual payments recorded in this period.")
    for account in report["accounts"]:
        lines += [account["label"], f"  Key: {account['account_key'] or '(unknown)'}",
                  f"  Manual plan: {account['manual_plan'] or '(not set)'}"]
        cost = account["known_api_equivalent_usd"]
        incomplete = f" + {account['unpriced_buckets']} unpriced buckets" if account["unpriced_buckets"] else ""
        lines.append(f"  API equivalent: USD {cost:,.2f}{incomplete} | Requests: {account['requests']:,}")
        if not account["payments"]:
            lines.append("  Recorded payments: none (actual spend unknown)")
        for payment in account["payments"]:
            lines.append(f"  Recorded {payment['kind']}: {format_money(payment['amount_minor'], payment['currency'])}")
        lines.append("")
    return "\n".join(lines)
