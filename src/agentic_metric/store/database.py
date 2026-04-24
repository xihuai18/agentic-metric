"""SQLite schema and CRUD operations."""

from __future__ import annotations

import sqlite3

from ..config import DATA_DIR, DB_PATH
from ..pricing import estimate_cost, get_pricing_fingerprint

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

_SCHEMA = _SESSIONS_TABLE_SQL + """\
CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
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

        rows = self._conn.execute(
            """SELECT session_id, agent_type, model,
                      input_tokens, output_tokens,
                      cache_read_tokens, cache_creation_tokens
               FROM sessions"""
        ).fetchall()

        updates = [
            (
                estimate_cost(
                    row["model"] or "",
                    input_tokens=row["input_tokens"] or 0,
                    output_tokens=row["output_tokens"] or 0,
                    cache_read_tokens=row["cache_read_tokens"] or 0,
                    cache_creation_tokens=row["cache_creation_tokens"] or 0,
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
        estimated_cost_usd: float | None = None,
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
                    0.0 if estimated_cost_usd is None else estimated_cost_usd,
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
            if value is not None:
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

    def commit(self) -> None:
        self._conn.commit()
