"""SQLite schema and CRUD operations."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from ..config import DATA_DIR, DB_PATH
from ..pricing import (
    estimate_cost,
    get_model_pricing_fingerprints,
    get_pricing_epoch,
    get_pricing_fingerprint,
)

_UNSET = object()
_CANCEL_CHECK_ROWS = 256
_PRICING_STATE_KEY = "pricing:fingerprint"
_PRICING_MODELS_KEY = "pricing:model_fingerprints"
_PRICING_MODELS_VERSION = 1
_HISTORY_IDENTITY_VERSION = "provider-data-root-v4"

_SESSIONS_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    data_root TEXT NOT NULL DEFAULT '',
    project_path TEXT,
    git_branch TEXT DEFAULT '',
    model TEXT DEFAULT '',
    message_count INTEGER DEFAULT 0,
    user_turns INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    cache_creation_1h_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL DEFAULT 0,
    started_at TEXT,
    ended_at TEXT,
    first_prompt TEXT DEFAULT '',
    last_prompt TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    PRIMARY KEY (session_id, agent_type, provider, data_root)
);
"""

_SESSION_USAGE_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS session_usage (
    session_id TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    data_root TEXT NOT NULL DEFAULT '',
    usage_date TEXT NOT NULL,
    usage_hour INTEGER NOT NULL,
    project_path TEXT DEFAULT '',
    model TEXT DEFAULT '',
    message_count INTEGER DEFAULT 0,
    user_turns INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    cache_creation_1h_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL DEFAULT 0,
    cost_is_explicit INTEGER DEFAULT 0,
    account_key TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (session_id, agent_type, provider, data_root, usage_date, usage_hour, model, account_key)
);
"""

_SCHEMA = _SESSIONS_TABLE_SQL + """\
CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
""" + _SESSION_USAGE_TABLE_SQL


def _check_cancelled() -> None:
    from ..collectors import check_sync_cancelled  # lazy: collectors import this module's package

    check_sync_cancelled()


def _cancellable(rows):
    for index, row in enumerate(rows):
        if index % _CANCEL_CHECK_ROWS == 0:
            _check_cancelled()
        yield row


class Database:
    def __init__(self, db_path: str | None = None, *, reprice_on_open: bool = True) -> None:
        from ..collectors import current_sync_cancel_event

        self._path = db_path or str(DB_PATH)
        self.pricing_changed = False
        self._tracked_models: set[str] = set()
        self._unrecorded_models: set[str] = set()
        self._pricing_epoch: tuple[int, int] | None = None
        self._price_dirty = False
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        event = current_sync_cancel_event()
        if event is not None:
            self._conn.set_progress_handler(lambda: int(event.is_set()), 1000)
        try:
            _check_cancelled()
            self._enable_wal()
            _check_cancelled()
            self._conn.executescript(_SCHEMA)
            self._migrate()
            if reprice_on_open:
                self.ensure_pricing_current()
        except BaseException as exc:
            self._abandon_connection()
            if (
                event is not None
                and event.is_set()
                and isinstance(exc, sqlite3.OperationalError)
                and "interrupted" in str(exc)
            ):
                from concurrent.futures import CancelledError

                raise CancelledError() from exc
            raise
        if event is not None:
            self._conn.set_progress_handler(None, 0)

    def _abandon_connection(self) -> None:
        self._conn.set_progress_handler(None, 0)
        try:
            self._conn.rollback()
        except sqlite3.Error:
            pass
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def _enable_wal(self) -> None:
        """Let readers proceed during writes; synchronous stays at its durable default."""
        if self._path == ":memory:" or self._path.startswith("file::memory:"):
            return
        try:
            self._conn.execute("PRAGMA journal_mode=WAL").fetchone()
        except sqlite3.OperationalError:
            # Another connection holds the file; keep the current mode.
            pass

    def _migrate(self) -> None:
        """Add columns that may be missing in older databases."""
        info = self._conn.execute("PRAGMA table_info(sessions)").fetchall()
        cols = {r[1] for r in info}
        if "last_prompt" not in cols:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN last_prompt TEXT DEFAULT ''"
            )
            info = self._conn.execute("PRAGMA table_info(sessions)").fetchall()
            cols = {r[1] for r in info}
        if "cache_creation_1h_tokens" not in cols:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN cache_creation_1h_tokens INTEGER DEFAULT 0"
            )
            info = self._conn.execute("PRAGMA table_info(sessions)").fetchall()
            cols = {r[1] for r in info}

        pk_cols = [r[1] for r in sorted(info, key=lambda row: row[5]) if r[5] > 0]
        if (
            "provider" not in cols
            or "data_root" not in cols
            or pk_cols != ["session_id", "agent_type", "provider", "data_root"]
        ):
            self._atomically(self._rebuild_sessions_table, cols)

        usage_info = self._conn.execute("PRAGMA table_info(session_usage)").fetchall()
        usage_cols = {r[1] for r in usage_info}
        usage_pk_cols = [
            r[1] for r in sorted(usage_info, key=lambda row: row[5]) if r[5] > 0
        ]
        if (
            "provider" not in usage_cols
            or "data_root" not in usage_cols
            or "cost_is_explicit" not in usage_cols
            or "cache_creation_1h_tokens" not in usage_cols
            or "account_key" not in usage_cols
            or usage_pk_cols != [
                "session_id",
                "agent_type",
                "provider",
                "data_root",
                "usage_date",
                "usage_hour",
                "model",
                "account_key",
            ]
        ):
            self._atomically(self._rebuild_session_usage_table, usage_cols)

        self._conn.executescript(_SESSION_USAGE_TABLE_SQL + """\
        CREATE INDEX IF NOT EXISTS idx_session_usage_date ON session_usage (usage_date);
        CREATE INDEX IF NOT EXISTS idx_session_usage_agent_date ON session_usage (agent_type, usage_date);
        CREATE INDEX IF NOT EXISTS idx_session_usage_provider_date ON session_usage (provider, usage_date);
        CREATE INDEX IF NOT EXISTS idx_session_usage_root_date ON session_usage (data_root, usage_date);
        CREATE INDEX IF NOT EXISTS idx_session_usage_model_date ON session_usage (model, usage_date);
        CREATE INDEX IF NOT EXISTS idx_session_usage_project_date ON session_usage (project_path, usage_date);
        """)
        self._ensure_history_identity_current()
        self._purge_unscoped_legacy_history()
        # ';' is ':' + 1, so this index range proves nothing matches without taking a write lock.
        if self._conn.execute(
            "SELECT 1 FROM sync_state "
            "WHERE key >= 'cc_assistant_ids:' AND key < 'cc_assistant_ids;' LIMIT 1"
        ).fetchone():
            self._conn.execute(
                "DELETE FROM sync_state WHERE key LIKE 'cc_assistant_ids:%'"
            )
        self._commit()

    @staticmethod
    def _column_expr(cols: set[str], column: str, default_sql: str) -> str:
        return column if column in cols else default_sql

    @staticmethod
    def _pure_agent_expr() -> str:
        return (
            "CASE WHEN instr(agent_type, ':') > 0 "
            "THEN substr(agent_type, 1, instr(agent_type, ':') - 1) "
            "ELSE agent_type END"
        )

    @staticmethod
    def _provider_expr(cols: set[str]) -> str:
        encoded = (
            "CASE WHEN instr(agent_type, ':') > 0 "
            "THEN substr(agent_type, instr(agent_type, ':') + 1) ELSE '' END"
        )
        if "provider" in cols:
            return f"COALESCE(NULLIF(provider, ''), {encoded})"
        return encoded

    def _ensure_history_identity_current(self) -> None:
        """Invalidate old derived rows when identity dimensions change."""
        key = "history_identity:version"
        if self.get_sync_state(key) == _HISTORY_IDENTITY_VERSION:
            return
        self._clear_collector_history()
        self.set_sync_state(key, _HISTORY_IDENTITY_VERSION)

    def _clear_collector_history(self) -> None:
        """Clear derived collector rows so history can be rebuilt from source logs."""
        self._conn.execute("DELETE FROM sessions")
        self._conn.execute("DELETE FROM session_usage")
        self._conn.execute("DELETE FROM sync_state WHERE key LIKE 'codex_jsonl:%'")
        self._conn.execute("DELETE FROM sync_state WHERE key LIKE 'cc_jsonl:%'")

    def _purge_unscoped_legacy_history(self) -> None:
        """Remove rows written before data_root became part of session identity."""
        stale = self._conn.execute(
            """SELECT
                   (SELECT COUNT(*) FROM sessions WHERE data_root = '') +
                   (SELECT COUNT(*) FROM session_usage WHERE data_root = '') AS n"""
        ).fetchone()
        if not stale or stale["n"] == 0:
            return
        self._conn.execute("DELETE FROM sessions WHERE data_root = ''")
        self._conn.execute("DELETE FROM session_usage WHERE data_root = ''")
        self._conn.execute("DELETE FROM sync_state WHERE key LIKE 'codex_jsonl:v5:%'")
        self._conn.execute("DELETE FROM sync_state WHERE key LIKE 'cc_jsonl:v5:%'")
        self._commit()

    def _atomically(self, rebuild, cols: set[str]) -> None:
        """Run a table rebuild in one transaction so an interrupt never leaves a renamed-away table."""
        if not self._conn.in_transaction:
            self._conn.execute("BEGIN")
        try:
            rebuild(cols)
        except BaseException:
            self._conn.rollback()
            raise

    def _rebuild_sessions_table(self, cols: set[str]) -> None:
        """Rebuild the sessions table with the current primary key."""
        self._conn.execute("DROP TABLE IF EXISTS sessions_old")
        self._conn.execute("ALTER TABLE sessions RENAME TO sessions_old")
        self._conn.execute(_SESSIONS_TABLE_SQL)
        self._conn.execute(
            f"""INSERT OR IGNORE INTO sessions
                   (session_id, agent_type, provider, data_root,
                    project_path, git_branch, model,
                    message_count, user_turns, input_tokens, output_tokens,
                    cache_read_tokens, cache_creation_tokens, cache_creation_1h_tokens,
                    estimated_cost_usd,
                    started_at, ended_at, first_prompt, last_prompt, summary)
               SELECT session_id,
                      {self._pure_agent_expr()},
                      {self._provider_expr(cols)},
                      {self._column_expr(cols, "data_root", "''")},
                      project_path,
                      {self._column_expr(cols, "git_branch", "''")},
                      {self._column_expr(cols, "model", "''")},
                      {self._column_expr(cols, "message_count", "0")},
                      {self._column_expr(cols, "user_turns", "0")},
                      {self._column_expr(cols, "input_tokens", "0")},
                      {self._column_expr(cols, "output_tokens", "0")},
                      {self._column_expr(cols, "cache_read_tokens", "0")},
                      {self._column_expr(cols, "cache_creation_tokens", "0")},
                      {self._column_expr(cols, "cache_creation_1h_tokens", "0")},
                      {self._column_expr(cols, "estimated_cost_usd", "0")},
                      {self._column_expr(cols, "started_at", "''")},
                      {self._column_expr(cols, "ended_at", "''")},
                      {self._column_expr(cols, "first_prompt", "''")},
                      {self._column_expr(cols, "last_prompt", "''")},
                      {self._column_expr(cols, "summary", "''")}
               FROM sessions_old"""
        )
        self._conn.execute("DROP TABLE sessions_old")
        self._commit()

    def _rebuild_session_usage_table(self, cols: set[str]) -> None:
        """Extend usage identity, preserving legacy rows as unknown accounts."""
        self._conn.execute("DROP TABLE IF EXISTS session_usage_old")
        self._conn.execute("ALTER TABLE session_usage RENAME TO session_usage_old")
        self._conn.execute(_SESSION_USAGE_TABLE_SQL)
        self._conn.execute(
            f"""INSERT OR IGNORE INTO session_usage
                   (session_id, agent_type, provider, data_root,
                    usage_date, usage_hour, project_path, model,
                    message_count, user_turns, input_tokens, output_tokens,
                    cache_read_tokens, cache_creation_tokens, cache_creation_1h_tokens,
                    estimated_cost_usd,
                    cost_is_explicit, account_key)
               SELECT session_id,
                      {self._pure_agent_expr()},
                      {self._provider_expr(cols)},
                      {self._column_expr(cols, "data_root", "''")},
                      usage_date,
                      usage_hour,
                      {self._column_expr(cols, "project_path", "''")},
                      {self._column_expr(cols, "model", "''")},
                      {self._column_expr(cols, "message_count", "0")},
                      {self._column_expr(cols, "user_turns", "0")},
                      {self._column_expr(cols, "input_tokens", "0")},
                      {self._column_expr(cols, "output_tokens", "0")},
                      {self._column_expr(cols, "cache_read_tokens", "0")},
                      {self._column_expr(cols, "cache_creation_tokens", "0")},
                      {self._column_expr(cols, "cache_creation_1h_tokens", "0")},
                      {self._column_expr(cols, "estimated_cost_usd", "0")},
                      {self._column_expr(cols, "cost_is_explicit", "0")},
                      {self._column_expr(cols, "account_key", "''")}
               FROM session_usage_old"""
        )
        self._conn.execute("DROP TABLE session_usage_old")
        self._commit()

    def _commit(self) -> None:
        _check_cancelled()
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

    def set_sync_state(
        self, key: str, value: str, *, commit: bool = True
    ) -> None:
        if commit:
            self._guard_pricing_epoch()
        self._conn.execute(
            "INSERT INTO sync_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        if commit:
            self._commit()

    def delete_sync_state_prefix(self, prefix: str) -> None:
        self._guard_pricing_epoch()
        self._conn.execute(
            "DELETE FROM sync_state WHERE key LIKE ?",
            (f"{prefix}%",),
        )
        self._commit()

    # ── Per-model pricing baselines ───────────────────────────────

    def _stored_pricing_models(self, *, include_session_fallback: bool = False) -> set[str]:
        """Return the raw model ids whose stored costs depend on pricing, via one index lookup per distinct model."""
        models = {
            row[0]
            for row in self._conn.execute(
                """WITH RECURSIVE m(x) AS (
                       SELECT MIN(model) FROM session_usage
                       UNION ALL
                       SELECT (SELECT MIN(model) FROM session_usage WHERE model > m.x)
                       FROM m WHERE m.x IS NOT NULL
                   )
                   SELECT x FROM m WHERE x IS NOT NULL"""
            )
        }
        if self._conn.execute(
            "SELECT 1 FROM session_usage WHERE model IS NULL LIMIT 1"
        ).fetchone():
            models.add("")
        if include_session_fallback:
            models.update(
                row[0]
                for row in self._conn.execute(
                    """SELECT DISTINCT COALESCE(model, '')
                       FROM sessions AS s
                       WHERE NOT EXISTS (
                           SELECT 1
                           FROM session_usage AS u
                           WHERE u.session_id = s.session_id
                             AND u.agent_type = s.agent_type
                             AND u.provider = s.provider
                             AND u.data_root = s.data_root
                       )"""
                )
            )
        return models

    @staticmethod
    def _full_digest(fingerprint: str) -> str:
        return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()

    def _load_model_fingerprints(self, fingerprint: str | None) -> dict[str, str] | None:
        """Return the per-model baseline only if it belongs to this stored fingerprint."""
        raw = self.get_sync_state(_PRICING_MODELS_KEY)
        if fingerprint is None or not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if (
            not isinstance(data, dict)
            or data.get("v") != _PRICING_MODELS_VERSION
            or data.get("full") != self._full_digest(fingerprint)
        ):
            return None
        models = data.get("models")
        if not isinstance(models, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in models.items()
        ):
            return None
        return models

    def _write_model_fingerprints(
        self, fingerprint: str, models: dict[str, str]
    ) -> None:
        self.set_sync_state(
            _PRICING_MODELS_KEY,
            json.dumps(
                {
                    "v": _PRICING_MODELS_VERSION,
                    "full": self._full_digest(fingerprint),
                    "models": models,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            commit=False,
        )
        self._tracked_models = set(models)

    def _record_new_models(self) -> None:
        """Baseline models written this session, only while pricing provably still matches the stored fingerprint."""
        pending = self._unrecorded_models - self._tracked_models
        self._unrecorded_models = set()
        if not pending:
            return
        stored = self.get_sync_state(_PRICING_STATE_KEY)
        prior = self._load_model_fingerprints(stored)
        if prior is None or stored is None:
            return
        stable = get_pricing_fingerprint() == stored
        signatures = get_model_pricing_fingerprints(pending) if stable else {}
        if not stable or get_pricing_fingerprint() != stored:
            # New rows may be priced under other rules: force a reprice on the next open.
            self.set_sync_state(_PRICING_STATE_KEY, "", commit=False)
            return
        if self._pricing_epoch is None or get_pricing_epoch() != self._pricing_epoch:
            self._invalidate_pricing()
            return
        prior.update(signatures)
        self._write_model_fingerprints(stored, prior)

    def _invalidate_pricing(self) -> None:
        """Force the next open to reprice by blanking the stored fingerprint, without writing signatures."""
        if self.get_sync_state(_PRICING_STATE_KEY) != "":
            self.set_sync_state(_PRICING_STATE_KEY, "", commit=False)

    def _guard_pricing_epoch(self) -> None:
        """Before committing price-dependent rows, invalidate if pricing was reloaded since the verified baseline."""
        if not self._price_dirty:
            return
        self._price_dirty = False
        if self._pricing_epoch is None or get_pricing_epoch() != self._pricing_epoch:
            self._unrecorded_models = set()
            self._invalidate_pricing()

    def ensure_pricing_current(self) -> None:
        """Reconcile stored pricing, then remember the pricing epoch only if it stayed stable throughout."""
        start = get_pricing_epoch()
        self._ensure_pricing_current()
        if get_pricing_epoch() == start:
            self._pricing_epoch = start
            return
        self._pricing_epoch = None
        self._invalidate_pricing()
        self._commit()

    def _ensure_pricing_current(self) -> None:
        """Reprice stored sessions when a stored model's pricing inputs (or an untrusted baseline) changed; otherwise just advance the fingerprint."""
        state_key = _PRICING_STATE_KEY
        fingerprint = get_pricing_fingerprint()
        stored = self.get_sync_state(state_key)
        prior = self._load_model_fingerprints(stored)
        if stored == fingerprint:
            models = self._stored_pricing_models()
            if prior is not None and set(prior) == models:
                self._tracked_models = set(models)
                return
            # Stored costs match the current configuration, so current signatures are the baseline.
            baseline = {m: fp for m, fp in (prior or {}).items() if m in models}
            baseline.update(
                get_model_pricing_fingerprints(models.difference(baseline))
            )
            self._write_model_fingerprints(fingerprint, baseline)
            self._commit()
            return

        current = get_model_pricing_fingerprints(
            self._stored_pricing_models(include_session_fallback=True)
        )
        if prior is not None and all(prior.get(m) == fp for m, fp in current.items()):
            self.set_sync_state(state_key, fingerprint, commit=False)
            self._write_model_fingerprints(fingerprint, current)
            self._commit()
            return

        self.pricing_changed = True
        self._conn.execute("DELETE FROM sync_state WHERE key LIKE 'codex_jsonl:%'")
        self._conn.execute("DELETE FROM sync_state WHERE key LIKE 'cc_jsonl:%'")

        usage_rows = self._conn.execute(
            """SELECT session_id, agent_type, provider, data_root, usage_date, usage_hour, model,
                      account_key, input_tokens, output_tokens,
                      cache_read_tokens, cache_creation_tokens,
                      cache_creation_1h_tokens
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
                    cache_creation_1h_tokens=row["cache_creation_1h_tokens"] or 0,
                    apply_long_context=False,
                ),
                row["session_id"],
                row["agent_type"],
                row["provider"],
                row["data_root"],
                row["usage_date"],
                row["usage_hour"],
                row["model"] or "",
                row["account_key"],
            )
            for row in _cancellable(usage_rows)
        ]
        _check_cancelled()
        if usage_updates:
            self._conn.executemany(
                """UPDATE session_usage
                   SET estimated_cost_usd = ?
                   WHERE session_id = ?
                     AND agent_type = ?
                     AND provider = ?
                     AND data_root = ?
                     AND usage_date = ?
                     AND usage_hour = ?
                     AND model = ?
                     AND account_key = ?""",
                usage_updates,
            )

        rows = self._conn.execute(
            """SELECT session_id, agent_type, provider, data_root, model,
                      input_tokens, output_tokens,
                      cache_read_tokens, cache_creation_tokens,
                      cache_creation_1h_tokens
               FROM sessions AS s
               WHERE NOT EXISTS (
                   SELECT 1
                   FROM session_usage AS u
                   WHERE u.session_id = s.session_id
                     AND u.agent_type = s.agent_type
                     AND u.provider = s.provider
                     AND u.data_root = s.data_root
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
                    cache_creation_1h_tokens=row["cache_creation_1h_tokens"] or 0,
                    apply_long_context=False,
                ),
                row["session_id"],
                row["agent_type"],
                row["provider"],
                row["data_root"],
            )
            for row in _cancellable(rows)
        ]
        _check_cancelled()
        if updates:
            self._conn.executemany(
                """UPDATE sessions
                   SET estimated_cost_usd = ?
                   WHERE session_id = ? AND agent_type = ? AND provider = ? AND data_root = ?""",
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
                         AND u.provider = sessions.provider
                         AND u.data_root = sessions.data_root
                         AND u.estimated_cost_usd IS NULL
                   ) THEN NULL
                   ELSE (
                       SELECT COALESCE(SUM(u.estimated_cost_usd), 0)
                       FROM session_usage AS u
                       WHERE u.session_id = sessions.session_id
                         AND u.agent_type = sessions.agent_type
                         AND u.provider = sessions.provider
                         AND u.data_root = sessions.data_root
                   )
               END
               WHERE EXISTS (
                   SELECT 1
                   FROM session_usage AS u
                   WHERE u.session_id = sessions.session_id
                     AND u.agent_type = sessions.agent_type
                     AND u.provider = sessions.provider
                     AND u.data_root = sessions.data_root
               )"""
        )

        self.set_sync_state(state_key, fingerprint, commit=False)
        self._write_model_fingerprints(fingerprint, current)
        self._commit()

    # ── Session CRUD ──────────────────────────────────────────────

    def upsert_session(
        self,
        session_id: str,
        agent_type: str,
        *,
        provider: str | None = None,
        data_root: str | None = None,
        project_path: str | None = None,
        git_branch: str | None = None,
        model: str | None = None,
        message_count: int | None = None,
        user_turns: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_read_tokens: int | None = None,
        cache_creation_tokens: int | None = None,
        cache_creation_1h_tokens: int | None = None,
        estimated_cost_usd: float | None | object = _UNSET,
        started_at: str | None = None,
        ended_at: str | None = None,
        first_prompt: str | None = None,
        last_prompt: str | None = None,
        summary: str | None = None,
    ) -> None:
        data_root_value = data_root or ""
        provider_value = provider or ""
        if model is not None:
            self._unrecorded_models.add(model)
        if any(
            value is not None
            for value in (
                model, input_tokens, output_tokens, cache_read_tokens,
                cache_creation_tokens, cache_creation_1h_tokens,
            )
        ) or estimated_cost_usd is not _UNSET:
            self._price_dirty = True
        existing = self._conn.execute(
            """SELECT 1
               FROM sessions
               WHERE session_id = ? AND agent_type = ? AND provider = ? AND data_root = ?""",
            (session_id, agent_type, provider_value, data_root_value),
        ).fetchone()

        if existing is None:
            self._conn.execute(
                """INSERT INTO sessions
                       (session_id, agent_type, provider, data_root,
                        project_path, git_branch, model,
                        message_count, user_turns, input_tokens, output_tokens,
                        cache_read_tokens, cache_creation_tokens, cache_creation_1h_tokens,
                        estimated_cost_usd,
                        started_at, ended_at, first_prompt, last_prompt, summary)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    agent_type,
                    provider_value,
                    data_root_value,
                    project_path or "",
                    git_branch or "",
                    model or "",
                    0 if message_count is None else message_count,
                    0 if user_turns is None else user_turns,
                    0 if input_tokens is None else input_tokens,
                    0 if output_tokens is None else output_tokens,
                    0 if cache_read_tokens is None else cache_read_tokens,
                    0 if cache_creation_tokens is None else cache_creation_tokens,
                    0 if cache_creation_1h_tokens is None else cache_creation_1h_tokens,
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
            ("provider", provider),
            ("project_path", project_path),
            ("git_branch", git_branch),
            ("model", model),
            ("message_count", message_count),
            ("user_turns", user_turns),
            ("input_tokens", input_tokens),
            ("output_tokens", output_tokens),
            ("cache_read_tokens", cache_read_tokens),
            ("cache_creation_tokens", cache_creation_tokens),
            ("cache_creation_1h_tokens", cache_creation_1h_tokens),
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

        params.extend((session_id, agent_type, provider_value, data_root_value))
        self._conn.execute(
            f"""UPDATE sessions
                SET {", ".join(updates)}
                WHERE session_id = ? AND agent_type = ? AND provider = ? AND data_root = ?""",
            params,
        )

    def delete_session(
        self,
        session_id: str,
        agent_type: str,
        *,
        provider: str,
        data_root: str | None = None,
    ) -> None:
        """Delete one derived session and its usage rows for one provider/root."""
        if provider is None:
            raise TypeError("provider is required")
        data_root_value = data_root or ""
        provider_value = provider or ""
        params: tuple[object, ...] = (
            session_id,
            agent_type,
            provider_value,
            data_root_value,
        )
        self._conn.execute(
            """DELETE FROM session_usage
               WHERE session_id = ? AND agent_type = ? AND provider = ? AND data_root = ?""",
            params,
        )
        self._conn.execute(
            """DELETE FROM sessions
               WHERE session_id = ? AND agent_type = ? AND provider = ? AND data_root = ?""",
            params,
        )

    def replace_session_usage(
        self,
        session_id: str,
        agent_type: str,
        buckets: list[dict],
        *,
        provider: str | None = None,
        data_root: str | None = None,
    ) -> None:
        """Replace one session's per-local-hour usage buckets."""
        data_root_value = data_root or ""
        provider_value = provider or ""
        self._unrecorded_models.update(b.get("model") or "" for b in buckets)
        if buckets:
            self._price_dirty = True
        self._conn.execute(
            """DELETE FROM session_usage
               WHERE session_id = ? AND agent_type = ? AND provider = ? AND data_root = ?""",
            (session_id, agent_type, provider_value, data_root_value),
        )
        if not buckets:
            return

        rows = []
        for bucket in buckets:
            model = bucket.get("model") or ""
            input_tokens = int(bucket.get("input_tokens") or 0)
            output_tokens = int(bucket.get("output_tokens") or 0)
            cache_read_tokens = int(bucket.get("cache_read_tokens") or 0)
            cache_creation_tokens = int(bucket.get("cache_creation_tokens") or 0)
            cache_creation_1h_tokens = int(bucket.get("cache_creation_1h_tokens") or 0)
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
                    cache_creation_1h_tokens=cache_creation_1h_tokens,
                    apply_long_context=False,
                )
                cost_is_explicit = 0
            rows.append((
                session_id,
                agent_type,
                provider_value,
                data_root_value,
                bucket.get("usage_date") or "",
                int(bucket.get("usage_hour") or 0),
                bucket.get("project_path") or "",
                model,
                int(bucket.get("message_count") or 0),
                int(bucket.get("user_turns") or 0),
                input_tokens,
                output_tokens,
                cache_read_tokens,
                cache_creation_tokens,
                cache_creation_1h_tokens,
                estimated_cost_usd,
                cost_is_explicit,
                bucket.get("account_key") or "",
            ))

        self._conn.executemany(
            """INSERT INTO session_usage
                   (session_id, agent_type, provider, data_root,
                    usage_date, usage_hour, project_path,
                    model, message_count, user_turns, input_tokens, output_tokens,
                    cache_read_tokens, cache_creation_tokens, cache_creation_1h_tokens,
                    estimated_cost_usd,
                    cost_is_explicit, account_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )

    def commit(self) -> None:
        self._guard_pricing_epoch()
        self._record_new_models()
        self._commit()
