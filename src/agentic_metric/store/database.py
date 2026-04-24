"""SQLite schema and CRUD operations."""

from __future__ import annotations

import sqlite3

from ..config import DATA_DIR, DB_PATH
from ..pricing import estimate_cost, get_pricing_fingerprint

_UNSET = object()

_SESSIONS_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    project_path TEXT,
    git_branch TEXT DEFAULT '',
    model TEXT DEFAULT '',
    message_count INTEGER DEFAULT 0,
    user_turns INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL DEFAULT 0,
    started_at TEXT,
    ended_at TEXT,
    first_prompt TEXT DEFAULT '',
    last_prompt TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    PRIMARY KEY (session_id, agent_type)
);
"""

_SESSION_USAGE_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS session_usage (
    session_id TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    usage_date TEXT NOT NULL,
    usage_hour INTEGER NOT NULL,
    project_path TEXT DEFAULT '',
    model TEXT DEFAULT '',
    service_tier TEXT DEFAULT '',
    message_count INTEGER DEFAULT 0,
    user_turns INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL DEFAULT 0,
    cost_is_explicit INTEGER DEFAULT 0,
    PRIMARY KEY (session_id, agent_type, usage_date, usage_hour, model, service_tier)
);
"""

_SCHEMA = _SESSIONS_TABLE_SQL + """\
CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
""" + _SESSION_USAGE_TABLE_SQL + """\
CREATE INDEX IF NOT EXISTS idx_session_usage_date ON session_usage (usage_date);
CREATE INDEX IF NOT EXISTS idx_session_usage_agent_date ON session_usage (agent_type, usage_date);
CREATE INDEX IF NOT EXISTS idx_session_usage_model_date ON session_usage (model, usage_date);
CREATE INDEX IF NOT EXISTS idx_session_usage_project_date ON session_usage (project_path, usage_date);
"""


class Database:
    def __init__(self, db_path: str | None = None) -> None:
        self._path = db_path or str(DB_PATH)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self.ensure_pricing_current()

    def _migrate(self) -> None:
        """Add columns that may be missing in older databases."""
        info = self._conn.execute("PRAGMA table_info(sessions)").fetchall()
        cols = {r[1] for r in info}
        if "last_prompt" not in cols:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN last_prompt TEXT DEFAULT ''"
            )
            info = self._conn.execute("PRAGMA table_info(sessions)").fetchall()

        pk_cols = [r[1] for r in sorted(info, key=lambda row: row[5]) if r[5] > 0]
        if pk_cols != ["session_id", "agent_type"]:
            self._rebuild_sessions_table()

        usage_info = self._conn.execute("PRAGMA table_info(session_usage)").fetchall()
        usage_pk_cols = [r[1] for r in sorted(usage_info, key=lambda row: row[5]) if r[5] > 0]
        if usage_pk_cols != ["session_id", "agent_type", "usage_date", "usage_hour", "model", "service_tier"]:
            self._rebuild_session_usage_table(usage_info)
            usage_info = self._conn.execute("PRAGMA table_info(session_usage)").fetchall()
        usage_cols = {r[1] for r in usage_info}
        if "cost_is_explicit" not in usage_cols:
            self._conn.execute(
                "ALTER TABLE session_usage ADD COLUMN cost_is_explicit INTEGER DEFAULT 0"
            )

        self._conn.executescript(_SESSION_USAGE_TABLE_SQL + """\
        CREATE INDEX IF NOT EXISTS idx_session_usage_date ON session_usage (usage_date);
        CREATE INDEX IF NOT EXISTS idx_session_usage_agent_date ON session_usage (agent_type, usage_date);
        CREATE INDEX IF NOT EXISTS idx_session_usage_model_date ON session_usage (model, usage_date);
        CREATE INDEX IF NOT EXISTS idx_session_usage_project_date ON session_usage (project_path, usage_date);
        """)

    def _rebuild_sessions_table(self) -> None:
        """Rebuild the sessions table with the current primary key."""
        self._conn.execute("ALTER TABLE sessions RENAME TO sessions_old")
        self._conn.execute(_SESSIONS_TABLE_SQL)
        self._conn.execute(
            """INSERT INTO sessions
                   (session_id, agent_type, project_path, git_branch, model,
                    message_count, user_turns, input_tokens, output_tokens,
                    cache_read_tokens, cache_creation_tokens, estimated_cost_usd,
                    started_at, ended_at, first_prompt, last_prompt, summary)
               SELECT session_id, agent_type, project_path, git_branch, model,
                      message_count, user_turns, input_tokens, output_tokens,
                      cache_read_tokens, cache_creation_tokens, estimated_cost_usd,
                      started_at, ended_at, first_prompt, last_prompt, summary
               FROM sessions_old"""
        )
        self._conn.execute("DROP TABLE sessions_old")
        self._conn.commit()

    def _rebuild_session_usage_table(self, old_info: list[sqlite3.Row]) -> None:
        """Rebuild session_usage with the current primary key."""
        old_cols = {r[1] for r in old_info}
        service_tier_expr = "service_tier" if "service_tier" in old_cols else "''"
        explicit_expr = "cost_is_explicit" if "cost_is_explicit" in old_cols else "0"
        self._conn.execute("ALTER TABLE session_usage RENAME TO session_usage_old")
        self._conn.execute(_SESSION_USAGE_TABLE_SQL)
        self._conn.execute(
            f"""INSERT INTO session_usage
                   (session_id, agent_type, usage_date, usage_hour, project_path,
                    model, service_tier, message_count, user_turns, input_tokens,
                    output_tokens, cache_read_tokens, cache_creation_tokens,
                    estimated_cost_usd, cost_is_explicit)
               SELECT session_id, agent_type, usage_date, usage_hour, project_path,
                      model, {service_tier_expr}, message_count, user_turns,
                      input_tokens, output_tokens, cache_read_tokens,
                      cache_creation_tokens, estimated_cost_usd, {explicit_expr}
               FROM session_usage_old"""
        )
        self._conn.execute("DROP TABLE session_usage_old")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    # ── Sync state ────────────────────────────────────────────────

    def get_sync_state(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM sync_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_sync_state(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO sync_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def delete_sync_state_prefix(self, prefix: str) -> None:
        self._conn.execute(
            "DELETE FROM sync_state WHERE key LIKE ?",
            (f"{prefix}%",),
        )
        self._conn.commit()

    def ensure_pricing_current(self) -> None:
        """Reprice stored sessions when pricing rules change."""
        state_key = "pricing:fingerprint"
        fingerprint = get_pricing_fingerprint()
        if self.get_sync_state(state_key) == fingerprint:
            return

        self._conn.execute("DELETE FROM sync_state WHERE key LIKE 'codex_jsonl:%'")
        self._conn.execute("DELETE FROM sync_state WHERE key LIKE 'cc_jsonl:%'")

        usage_rows = self._conn.execute(
            """SELECT session_id, agent_type, usage_date, usage_hour, model, service_tier,
                      input_tokens, output_tokens,
                      cache_read_tokens, cache_creation_tokens
               FROM session_usage
               WHERE COALESCE(cost_is_explicit, 0) = 0"""
        ).fetchall()
        usage_updates = [
            (
                estimate_cost(
                    row["model"] or "",
                    input_tokens=row["input_tokens"] or 0,
                    output_tokens=row["output_tokens"] or 0,
                    cache_read_tokens=row["cache_read_tokens"] or 0,
                    cache_creation_tokens=row["cache_creation_tokens"] or 0,
                    service_tier=row["service_tier"] or "",
                    apply_long_context=False,
                ),
                row["session_id"],
                row["agent_type"],
                row["usage_date"],
                row["usage_hour"],
                row["model"] or "",
                row["service_tier"] or "",
            )
            for row in usage_rows
        ]
        if usage_updates:
            self._conn.executemany(
                """UPDATE session_usage
                   SET estimated_cost_usd = ?
                   WHERE session_id = ?
                     AND agent_type = ?
                     AND usage_date = ?
                     AND usage_hour = ?
                     AND model = ?
                     AND service_tier = ?""",
                usage_updates,
            )

        rows = self._conn.execute(
            """SELECT session_id, agent_type, model,
                      input_tokens, output_tokens,
                      cache_read_tokens, cache_creation_tokens
               FROM sessions AS s
               WHERE NOT EXISTS (
                   SELECT 1
                   FROM session_usage AS u
                   WHERE u.session_id = s.session_id
                     AND u.agent_type = s.agent_type
               )"""
        ).fetchall()

        updates = [
            (
                estimate_cost(
                    row["model"] or "",
                    input_tokens=row["input_tokens"] or 0,
                    output_tokens=row["output_tokens"] or 0,
                    cache_read_tokens=row["cache_read_tokens"] or 0,
                    cache_creation_tokens=row["cache_creation_tokens"] or 0,
                    apply_long_context=False,
                ),
                row["session_id"],
                row["agent_type"],
            )
            for row in rows
        ]
        if updates:
            self._conn.executemany(
                """UPDATE sessions
                   SET estimated_cost_usd = ?
                   WHERE session_id = ? AND agent_type = ?""",
                updates,
            )

        self._conn.execute(
            """UPDATE sessions
               SET estimated_cost_usd = CASE
                   WHEN EXISTS (
                       SELECT 1
                       FROM session_usage AS u
                       WHERE u.session_id = sessions.session_id
                         AND u.agent_type = sessions.agent_type
                         AND u.estimated_cost_usd IS NULL
                   ) THEN NULL
                   ELSE (
                       SELECT COALESCE(SUM(u.estimated_cost_usd), 0)
                       FROM session_usage AS u
                       WHERE u.session_id = sessions.session_id
                         AND u.agent_type = sessions.agent_type
                   )
               END
               WHERE EXISTS (
                   SELECT 1
                   FROM session_usage AS u
                   WHERE u.session_id = sessions.session_id
                     AND u.agent_type = sessions.agent_type
               )"""
        )

        self._conn.execute(
            "INSERT INTO sync_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (state_key, fingerprint),
        )
        self._conn.commit()

    # ── Session CRUD ──────────────────────────────────────────────

    def upsert_session(
        self,
        session_id: str,
        agent_type: str,
        *,
        project_path: str | None = None,
        git_branch: str | None = None,
        model: str | None = None,
        message_count: int | None = None,
        user_turns: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_read_tokens: int | None = None,
        cache_creation_tokens: int | None = None,
        estimated_cost_usd: float | None | object = _UNSET,
        started_at: str | None = None,
        ended_at: str | None = None,
        first_prompt: str | None = None,
        last_prompt: str | None = None,
        summary: str | None = None,
    ) -> None:
        existing = self._conn.execute(
            """SELECT 1
               FROM sessions
               WHERE session_id = ? AND agent_type = ?""",
            (session_id, agent_type),
        ).fetchone()

        if existing is None:
            self._conn.execute(
                """INSERT INTO sessions
                       (session_id, agent_type, project_path, git_branch, model,
                        message_count, user_turns, input_tokens, output_tokens,
                        cache_read_tokens, cache_creation_tokens, estimated_cost_usd,
                        started_at, ended_at, first_prompt, last_prompt, summary)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    agent_type,
                    project_path or "",
                    git_branch or "",
                    model or "",
                    0 if message_count is None else message_count,
                    0 if user_turns is None else user_turns,
                    0 if input_tokens is None else input_tokens,
                    0 if output_tokens is None else output_tokens,
                    0 if cache_read_tokens is None else cache_read_tokens,
                    0 if cache_creation_tokens is None else cache_creation_tokens,
                    0.0 if estimated_cost_usd is _UNSET else estimated_cost_usd,
                    started_at or "",
                    ended_at or "",
                    first_prompt or "",
                    last_prompt or "",
                    summary or "",
                ),
            )
            return

        updates: list[str] = []
        params: list[object] = []
        for field, value in (
            ("project_path", project_path),
            ("git_branch", git_branch),
            ("model", model),
            ("message_count", message_count),
            ("user_turns", user_turns),
            ("input_tokens", input_tokens),
            ("output_tokens", output_tokens),
            ("cache_read_tokens", cache_read_tokens),
            ("cache_creation_tokens", cache_creation_tokens),
            ("estimated_cost_usd", estimated_cost_usd),
            ("started_at", started_at),
            ("ended_at", ended_at),
            ("first_prompt", first_prompt),
            ("last_prompt", last_prompt),
            ("summary", summary),
        ):
            if field == "estimated_cost_usd":
                if value is _UNSET:
                    continue
                updates.append(f"{field} = ?")
                params.append(value)
            elif value is not None:
                updates.append(f"{field} = ?")
                params.append(value)

        if not updates:
            return

        params.extend((session_id, agent_type))
        self._conn.execute(
            f"""UPDATE sessions
                SET {", ".join(updates)}
                WHERE session_id = ? AND agent_type = ?""",
            params,
        )

    def replace_session_usage(
        self,
        session_id: str,
        agent_type: str,
        buckets: list[dict],
    ) -> None:
        """Replace one session's per-local-hour usage buckets."""
        self._conn.execute(
            "DELETE FROM session_usage WHERE session_id = ? AND agent_type = ?",
            (session_id, agent_type),
        )
        if not buckets:
            return

        rows = []
        for bucket in buckets:
            model = bucket.get("model") or ""
            service_tier = bucket.get("service_tier") or ""
            input_tokens = int(bucket.get("input_tokens") or 0)
            output_tokens = int(bucket.get("output_tokens") or 0)
            cache_read_tokens = int(bucket.get("cache_read_tokens") or 0)
            cache_creation_tokens = int(bucket.get("cache_creation_tokens") or 0)
            if "estimated_cost_usd" in bucket:
                raw_cost = bucket.get("estimated_cost_usd")
                estimated_cost_usd = None if raw_cost is None else float(raw_cost)
                cost_is_explicit = 1 if raw_cost is not None else 0
            else:
                estimated_cost_usd = estimate_cost(
                    model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_creation_tokens=cache_creation_tokens,
                    service_tier=service_tier,
                    apply_long_context=False,
                )
                cost_is_explicit = 0
            rows.append((
                session_id,
                agent_type,
                bucket.get("usage_date") or "",
                int(bucket.get("usage_hour") or 0),
                bucket.get("project_path") or "",
                model,
                service_tier,
                int(bucket.get("message_count") or 0),
                int(bucket.get("user_turns") or 0),
                input_tokens,
                output_tokens,
                cache_read_tokens,
                cache_creation_tokens,
                estimated_cost_usd,
                cost_is_explicit,
            ))

        self._conn.executemany(
            """INSERT INTO session_usage
                   (session_id, agent_type, usage_date, usage_hour, project_path,
                    model, service_tier, message_count, user_turns, input_tokens, output_tokens,
                    cache_read_tokens, cache_creation_tokens, estimated_cost_usd,
                    cost_is_explicit)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )

    def commit(self) -> None:
        self._conn.commit()
