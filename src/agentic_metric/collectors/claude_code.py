"""Claude Code collector: parse local JSONL/JSON files into history."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import zlib
from datetime import datetime
from pathlib import Path

from ..config import PROJECTS_DIR
from ..pricing import estimate_cost
from ..usage import estimate_token_usage_cost, normalize_anthropic_usage
from . import (
    BaseCollector,
    local_time_bucket,
    reconcile_scope_sessions,
    reconcile_sync_state_keys,
    sync_key_identity,
)


CLAUDE_SYNC_VERSION = "v14"


# ── Helpers ──────────────────────────────────────────────────────────────


def _subscriber_account_key(usage: object) -> str:
    """Accept only explicit request-local account fingerprints from WCC usage."""
    if not isinstance(usage, dict):
        return ""
    account = usage.get("wcc_subscriber_account")
    if not isinstance(account, dict) or account.get("provider") != "codex":
        return ""
    fingerprint = account.get("account_fingerprint")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", fingerprint):
        return ""
    return f"codex:{fingerprint.lower()}"


def _extract_prompt(content: str) -> str:
    """Extract meaningful user prompt from message content, stripping system noise."""
    # Strip XML-like tags
    clean = re.sub(r"<[^>]+>", "", content).strip()
    # Skip known noise prefixes
    for prefix in ("Caveat:", "login"):
        if clean.startswith(prefix):
            lines = [line.strip() for line in clean.split("\n") if line.strip()]
            for line in lines:
                skip = False
                for p in ("Caveat:", "login", "init"):
                    if line.startswith(p):
                        skip = True
                        break
                if not skip and len(line) > 2:
                    return line
            return ""
    return clean


# ── Incremental JSONL accumulator ────────────────────────────────────────


class _SessionAccum:
    """Accumulator for incremental parsing of a single session .jsonl file.

    Tracks byte offset so repeated calls only parse newly appended data.
    """

    __slots__ = (
        "file_path",
        "project_path",
        "session_id",
        "_replayed_assistant_ids",
        "offset",
        "user_turns",
        "message_count",
        "input_tokens",
        "output_tokens",
        "cache_read",
        "cache_create",
        "cache_create_1h",
        "first_ts",
        "last_ts",
        "first_prompt",
        "last_prompt",
        "git_branch",
        "model",
        "today_user_turns",
        "today_message_count",
        "today_input_tokens",
        "today_output_tokens",
        "today_cache_read",
        "today_cache_create",
        "today_cache_create_1h",
        "today_key",
        "partial_line",
        "file_id",
        "file_mtime_ns",
        "assistant_message_dates",
        "all_assistant_ids",
        "assistant_usage_by_id",
        "usage_buckets",
        "_hasher",
        "tail_unterminated",
        "_checkpoint_restored",
    )

    def __init__(
        self,
        file_path: Path,
        project_path: str,
        replayed_assistant_ids: set[str] | None = None,
    ) -> None:
        self.file_path = file_path
        self.project_path = project_path
        self.session_id = file_path.stem
        self._replayed_assistant_ids = (
            replayed_assistant_ids if replayed_assistant_ids is not None else set()
        )
        self.offset = 0
        self.user_turns = 0
        self.message_count = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read = 0
        self.cache_create = 0
        self.cache_create_1h = 0
        self.first_ts = ""
        self.last_ts = ""
        self.first_prompt = ""
        self.last_prompt = ""
        self.git_branch = ""
        self.model = ""
        self.today_user_turns = 0
        self.today_message_count = 0
        self.today_input_tokens = 0
        self.today_output_tokens = 0
        self.today_cache_read = 0
        self.today_cache_create = 0
        self.today_cache_create_1h = 0
        self.today_key = ""
        self.partial_line = b""
        self.file_id: tuple[int, int] | None = None
        self.file_mtime_ns = -1
        self.assistant_message_dates: dict[str, tuple[str, int, str, str]] = {}
        self.all_assistant_ids: set[str] = set()
        self.assistant_usage_by_id: dict[
            str,
            tuple[int, int, int, int, int, float | None, str, int, str, str],
        ] = {}
        self.usage_buckets: dict[tuple[str, int, str, str], dict] = {}
        self._hasher = hashlib.sha256()
        self.tail_unterminated = False
        self._checkpoint_restored = False

    def read_new_lines(self) -> None:
        """Read only bytes appended since last call."""
        today_str = datetime.now().strftime("%Y-%m-%d")
        if today_str != self.today_key:
            self._reset_today_counters(today_str)

        try:
            with open(self.file_path, "rb") as f:
                stat = os.fstat(f.fileno())
                size = stat.st_size
                file_id = (stat.st_dev, stat.st_ino)
                mtime_ns = stat.st_mtime_ns
                restored = self._checkpoint_restored
                same_size_rewrite = (
                    size == self.offset
                    and self.file_mtime_ns >= 0
                    and mtime_ns != self.file_mtime_ns
                )
                if (
                    (self.file_id is not None and file_id != self.file_id)
                    or size < self.offset
                    or same_size_rewrite
                    or (restored and mtime_ns != self.file_mtime_ns)
                ):
                    self._reset_parsed_state(today_str)
                self.file_id = file_id
                if size == self.offset:
                    self.file_mtime_ns = mtime_ns
                    return
                f.seek(self.offset)
                new_data = f.read()
                final_stat = os.fstat(f.fileno())
                if restored and (final_stat.st_size, final_stat.st_mtime_ns) != (size, mtime_ns):
                    self._reset_parsed_state(today_str)
                    f.seek(0)
                    new_data = f.read()
                    stable_stat = os.fstat(f.fileno())
                    if (stable_stat.st_size, stable_stat.st_mtime_ns) != (final_stat.st_size, final_stat.st_mtime_ns):
                        raise RuntimeError("session changed while verifying checkpoint")
                    self.file_id = file_id
                    final_stat = stable_stat
                mtime_ns = final_stat.st_mtime_ns
            self._checkpoint_restored = False
            self._hasher.update(new_data)
            self.offset += len(new_data)
            self.file_mtime_ns = mtime_ns
        except OSError:
            return

        data = self.partial_line + new_data
        self.partial_line = b""
        lines = data.split(b"\n")
        tail = b""
        if data and not data.endswith(b"\n"):
            tail = lines.pop()

        for raw_line in lines:
            self._process_raw_line(raw_line, today_str)

        self.tail_unterminated = False
        self._checkpoint_restored = False
        if tail.strip():
            if self._process_raw_line(tail, today_str):
                self.tail_unterminated = True
            else:
                self.partial_line = tail

    def _reset_parsed_state(self, today_str: str) -> None:
        """Reset parsed counters after file truncation/replacement."""
        self.session_id = self.file_path.stem
        self.offset = 0
        self.user_turns = 0
        self.message_count = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read = 0
        self.cache_create = 0
        self.cache_create_1h = 0
        self.first_ts = ""
        self.last_ts = ""
        self.first_prompt = ""
        self.last_prompt = ""
        self.git_branch = ""
        self.model = ""
        self.partial_line = b""
        self._hasher = hashlib.sha256()
        self.tail_unterminated = False
        self._checkpoint_restored = False
        self.assistant_message_dates.clear()
        self.all_assistant_ids.clear()
        self.assistant_usage_by_id.clear()
        self.usage_buckets.clear()
        self._reset_today_counters(today_str)

    def _process_raw_line(self, raw_line: bytes, today_str: str) -> bool:
        """Process one JSONL line. Return False for an unparsable line."""
        raw_line = raw_line.strip()
        if not raw_line:
            return True
        try:
            entry = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False
        self._process_entry(entry, today_str)
        return True

    def _reset_today_counters(self, today_str: str) -> None:
        """Reset day-local counters when the local date changes."""
        self.today_key = today_str
        self.today_user_turns = 0
        self.today_message_count = 0
        self.today_input_tokens = 0
        self.today_output_tokens = 0
        self.today_cache_read = 0
        self.today_cache_create = 0
        self.today_cache_create_1h = 0

    @staticmethod
    def _ts_local_date(ts: str) -> str:
        """Convert ISO timestamp to local date string YYYY-MM-DD."""
        day, _hour = local_time_bucket(ts)
        return day

    def _add_usage_bucket(
        self,
        usage_date: str,
        usage_hour: int,
        *,
        model: str | None = None,
        account_key: str = "",
        user_turns: int = 0,
        message_count: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        cache_creation_1h_tokens: int = 0,
        estimated_cost_usd: float | None = 0.0,
    ) -> None:
        if not usage_date:
            return
        bucket_model = model if model is not None else self.model
        key = (usage_date, usage_hour, bucket_model or "", account_key)
        bucket = self.usage_buckets.setdefault(
            key,
            {
                "usage_date": usage_date,
                "usage_hour": usage_hour,
                "project_path": self.project_path,
                "model": bucket_model or "",
                "account_key": account_key,
                "message_count": 0,
                "user_turns": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "cache_creation_1h_tokens": 0,
                "estimated_cost_usd": 0.0,
            },
        )
        bucket["project_path"] = self.project_path
        bucket["message_count"] += message_count
        bucket["user_turns"] += user_turns
        bucket["input_tokens"] += input_tokens
        bucket["output_tokens"] += output_tokens
        bucket["cache_read_tokens"] += cache_read_tokens
        bucket["cache_creation_tokens"] += cache_creation_tokens
        bucket["cache_creation_1h_tokens"] += cache_creation_1h_tokens
        if estimated_cost_usd is None:
            bucket["estimated_cost_usd"] = None
        elif bucket["estimated_cost_usd"] is not None:
            bucket["estimated_cost_usd"] += estimated_cost_usd

    def usage_bucket_rows(self) -> list[dict]:
        rows = []
        for bucket in self.usage_buckets.values():
            if (
                bucket["message_count"]
                or bucket["user_turns"]
                or bucket["input_tokens"]
                or bucket["output_tokens"]
                or bucket["cache_read_tokens"]
                or bucket["cache_creation_tokens"]
                or bucket["cache_creation_1h_tokens"]
                or bucket["estimated_cost_usd"]
            ):
                rows.append(bucket)
        return rows

    def _process_entry(self, entry: dict, today_str: str) -> None:
        ts = entry.get("timestamp", "")
        if ts:
            if not self.first_ts:
                self.first_ts = ts
            self.last_ts = ts

        is_today = self._ts_local_date(ts) == today_str if ts else True
        entry_type = entry.get("type", "")

        if entry_type == "user":
            if not self.git_branch:
                self.git_branch = entry.get("gitBranch", "")
            msg = entry.get("message", {})
            content = msg.get("content", "") if isinstance(msg, dict) else ""
            # tool_result entries also have type="user"; only count real human input
            is_tool_result = isinstance(content, list) and any(
                isinstance(c, dict) and c.get("type") == "tool_result"
                for c in content
            )
            if not is_tool_result:
                entry_date, entry_hour = local_time_bucket(ts)
                self.user_turns += 1
                self.message_count += 1
                self._add_usage_bucket(
                    entry_date,
                    entry_hour,
                    user_turns=1,
                    message_count=1,
                )
                if is_today:
                    self.today_user_turns += 1
                    self.today_message_count += 1
                if isinstance(content, str):
                    clean = _extract_prompt(content)
                    if clean:
                        if not self.first_prompt:
                            self.first_prompt = clean[:80]
                        self.last_prompt = clean[:80]

        elif entry_type == "assistant":
            msg = entry.get("message", {})
            msg_id = msg.get("id", "") if isinstance(msg, dict) else ""
            if not isinstance(msg_id, str):
                msg_id = ""
            if msg_id:
                self.all_assistant_ids.add(msg_id)
            if msg_id and msg_id in self._replayed_assistant_ids:
                return
            msg_model = msg.get("model", "") if isinstance(msg, dict) else ""
            if msg_model and msg_model != "<synthetic>":
                self.model = msg_model
            elif msg_model and not self.model:
                self.model = msg_model
            entry_date, entry_hour = local_time_bucket(ts)
            if not entry_date:
                entry_date, entry_hour = today_str, 0
            bucket_model = msg_model or self.model
            usage = msg.get("usage", {}) if isinstance(msg, dict) else {}
            normalized = normalize_anthropic_usage(usage)
            account_key = _subscriber_account_key(usage)
            if normalized is None and msg_id in self.assistant_usage_by_id:
                account_key = self.assistant_usage_by_id[msg_id][-1]
            self._count_assistant_message(
                msg_id,
                entry_date,
                entry_hour,
                today_str,
                bucket_model,
                account_key,
            )

            if normalized is not None:
                inp, out, cr, cw = normalized.as_bucket_tuple()
                cw_1h = normalized.cache_write_1h_tokens
                # Anthropic reports the speed that actually served the request
                # (fast mode bills at a premium; standard fallbacks report
                # "standard"), so each event is priced at its own mode.
                speed = usage.get("speed")
                usage_cost = estimate_token_usage_cost(
                    bucket_model,
                    normalized,
                    speed=speed if isinstance(speed, str) else "",
                )
                self._set_assistant_usage(
                    msg_id,
                    (inp, out, cr, cw, cw_1h, usage_cost),
                    entry_date,
                    entry_hour,
                    today_str,
                    bucket_model,
                    account_key,
                )

    def _count_assistant_message(
        self,
        msg_id: str,
        entry_date: str,
        entry_hour: int,
        today_str: str,
        model: str,
        account_key: str,
    ) -> None:
        """Count each Claude Code assistant message once by message id."""
        if not msg_id:
            self.message_count += 1
            self._add_usage_bucket(
                entry_date,
                entry_hour,
                model=model,
                account_key=account_key,
                message_count=1,
            )
            if entry_date == today_str:
                self.today_message_count += 1
            return

        prev = self.assistant_message_dates.get(msg_id)
        if prev is None:
            self.assistant_message_dates[msg_id] = (entry_date, entry_hour, model, account_key)
            self.message_count += 1
            self._add_usage_bucket(
                entry_date,
                entry_hour,
                model=model,
                account_key=account_key,
                message_count=1,
            )
            if entry_date == today_str:
                self.today_message_count += 1
            return

        prev_date, prev_hour, prev_model, prev_account = prev
        if prev == (entry_date, entry_hour, model, account_key):
            return

        # Stream updates can change the date, model, or account attribution;
        # move the count without changing the session total.
        self._add_usage_bucket(
            prev_date,
            prev_hour,
            model=prev_model,
            account_key=prev_account,
            message_count=-1,
        )
        self._add_usage_bucket(
            entry_date,
            entry_hour,
            model=model,
            account_key=account_key,
            message_count=1,
        )
        if prev_date == today_str and entry_date != today_str:
            self.today_message_count = max(0, self.today_message_count - 1)
        elif prev_date != today_str and entry_date == today_str:
            self.today_message_count += 1
        self.assistant_message_dates[msg_id] = (entry_date, entry_hour, model, account_key)

    def _set_assistant_usage(
        self,
        msg_id: str,
        usage: tuple[int, int, int, int, int, float | None],
        entry_date: str,
        entry_hour: int,
        today_str: str,
        model: str,
        account_key: str,
    ) -> None:
        """Use the last usage snapshot for a Claude Code assistant message."""
        inp, out, cr, cw, cw_1h, cost = usage

        if msg_id:
            prev = self.assistant_usage_by_id.get(msg_id)
            if prev is not None:
                (
                    p_in, p_out, p_cr, p_cw, p_cw_1h, p_cost,
                    p_date, p_hour, p_model, p_account,
                ) = prev
                self.input_tokens = max(0, self.input_tokens - p_in)
                self.output_tokens = max(0, self.output_tokens - p_out)
                self.cache_read = max(0, self.cache_read - p_cr)
                self.cache_create = max(0, self.cache_create - p_cw)
                self.cache_create_1h = max(0, self.cache_create_1h - p_cw_1h)
                self._add_usage_bucket(
                    p_date,
                    p_hour,
                    model=p_model,
                    account_key=p_account,
                    input_tokens=-p_in,
                    output_tokens=-p_out,
                    cache_read_tokens=-p_cr,
                    cache_creation_tokens=-p_cw,
                    cache_creation_1h_tokens=-p_cw_1h,
                    estimated_cost_usd=-p_cost if p_cost is not None else None,
                )
                if p_date == today_str:
                    self.today_input_tokens = max(0, self.today_input_tokens - p_in)
                    self.today_output_tokens = max(0, self.today_output_tokens - p_out)
                    self.today_cache_read = max(0, self.today_cache_read - p_cr)
                    self.today_cache_create = max(0, self.today_cache_create - p_cw)
                    self.today_cache_create_1h = max(0, self.today_cache_create_1h - p_cw_1h)

            self.assistant_usage_by_id[msg_id] = (
                inp,
                out,
                cr,
                cw,
                cw_1h,
                cost,
                entry_date,
                entry_hour,
                model,
                account_key,
            )

        self.input_tokens += inp
        self.output_tokens += out
        self.cache_read += cr
        self.cache_create += cw
        self.cache_create_1h += cw_1h
        self._add_usage_bucket(
            entry_date,
            entry_hour,
            model=model,
            account_key=account_key,
            input_tokens=inp,
            output_tokens=out,
            cache_read_tokens=cr,
            cache_creation_tokens=cw,
            cache_creation_1h_tokens=cw_1h,
            estimated_cost_usd=cost,
        )
        if entry_date == today_str:
            self.today_input_tokens += inp
            self.today_output_tokens += out
            self.today_cache_read += cr
            self.today_cache_create += cw
            self.today_cache_create_1h += cw_1h


# ── CWD resolution ────────────────────────────────────────────────────────


def _read_cwd(jsonl_file: Path) -> str:
    """Extract the cwd field from the first few lines of a JSONL file."""
    try:
        with open(jsonl_file, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i > 10:
                    break
                try:
                    entry = json.loads(line)
                    cwd = entry.get("cwd", "")
                    if cwd:
                        return cwd
                except json.JSONDecodeError:
                    continue
    except (OSError, UnicodeDecodeError):
        pass
    return ""


# ── Collector implementation ─────────────────────────────────────────────


class ClaudeCodeCollector(BaseCollector):
    """Collector for Claude Code agent data.

    History sync: sessions-index.json + per-session JSONL token data.
    """

    agent_type = "claude_code"

    def __init__(
        self,
        projects_dir: Path | None = None,
        provider: str = "",
        data_root: str = "",
    ) -> None:
        self.projects_dir = projects_dir
        self.provider = provider.strip()
        self.data_root = data_root
        self.agent_type = "claude_code"

    @property
    def sync_state_prefix(self) -> str:
        identity = sync_key_identity(self.provider, self.data_root)
        return f"cc_jsonl:{CLAUDE_SYNC_VERSION}:{identity}"

    def sync_history(self, db, *, reconcile: bool = True) -> None:
        """Sync Claude Code history into the database.

        Parses two data sources:
        1. ``sessions-index.json`` -- session metadata (per project)
        2. ``.jsonl`` files -- per-session token data (incremental via sync_state)
        """
        indexed_ids, index_complete = self._sync_sessions_index(db)
        jsonl_ids, jsonl_complete = self._sync_jsonl_tokens(
            db,
            indexed_ids,
            reconcile=reconcile,
        )
        if reconcile and index_complete and jsonl_complete:
            reconcile_scope_sessions(
                db,
                agent_type=self.agent_type,
                data_root=self.data_root,
                active_by_provider={self.provider: indexed_ids | jsonl_ids},
                owned_providers={self.provider},
            )
        db.commit()

    # ── sessions-index.json ──────────────────────────────────────

    def _sync_sessions_index(self, db) -> tuple[set[str], bool]:
        """Parse all sessions-index.json files under PROJECTS_DIR."""
        projects_dir = self.projects_dir or PROJECTS_DIR
        if not projects_dir.exists():
            return (set(), False)

        active_ids: set[str] = set()
        complete = True
        try:
            index_files = list(projects_dir.glob("*/sessions-index.json"))
        except OSError:
            return (active_ids, False)

        for index_file in index_files:
            try:
                data = json.loads(index_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                complete = False
                continue
            if not isinstance(data, dict) or not isinstance(data.get("entries", []), list):
                complete = False
                continue

            for entry in data.get("entries", []):
                if not isinstance(entry, dict):
                    complete = False
                    continue
                session_id = entry.get("sessionId", "")
                if not session_id:
                    continue
                active_ids.add(session_id)

                created = entry.get("created", "")
                modified = entry.get("modified", "")

                project_path = entry.get("projectPath", "")
                db.upsert_session(
                    session_id,
                    self.agent_type,
                    provider=self.provider,
                    data_root=self.data_root,
                    project_path=project_path,
                    git_branch=entry.get("gitBranch", ""),
                    message_count=entry.get("messageCount", 0),
                    started_at=created,
                    ended_at=modified,
                    summary=entry.get("summary", ""),
                )
                has_usage = db.conn.execute(
                    """SELECT 1 FROM session_usage
                       WHERE session_id = ? AND agent_type = ?
                         AND provider = ? AND data_root = ?
                       LIMIT 1""",
                    (session_id, self.agent_type, self.provider, self.data_root),
                ).fetchone()
                if has_usage is None:
                    db.replace_session_usage(
                        session_id,
                        self.agent_type,
                        [_zero_usage_bucket(created, project_path)],
                        provider=self.provider,
                        data_root=self.data_root,
                    )

        return (active_ids, complete)

    # ── JSONL token scanning ─────────────────────────────────────

    def _sync_jsonl_tokens(
        self,
        db,
        indexed_ids: set[str],
        *,
        reconcile: bool = True,
    ) -> tuple[set[str], bool]:
        """Scan .jsonl files for per-session token data.

        Uses db sync_state to track which files/offsets have already been
        processed, making incremental re-syncs cheap.
        """
        projects_dir = self.projects_dir or PROJECTS_DIR
        if not projects_dir.exists():
            return (set(), False)

        active_ids: set[str] = set()
        active_sync_keys: set[str] = set()
        complete = True

        # v7: usage buckets now record the real cwd instead of the on-disk
        # projects/ dir. Bumping the key forces a one-time re-parse so already
        # synced sessions get their project_path corrected without --rebuild.
        # v8: subagent JSONL files may replay parent assistant messages with
        # identical message ids; parse a project tree once and skip replayed
        # assistant usage across sibling files.
        # v9: unchanged earlier files still seed replay ids during incremental
        # sync, so a changed sibling cannot re-count skipped parent usage.
        # v10: usage buckets preserve cache_creation_1h_tokens for accurate
        # repricing of 1-hour prompt-cache writes.
        # v11: skipped later files are reparsed when a newly seen earlier file
        # owns the same assistant id, replacing stale replayed usage rows.
        # v12: provider is part of the sessions/session_usage primary key;
        # reparse so every usage row has a matching session total row.
        # v13: sync state is scoped by provider/data_root and zero-turn files
        # with unique usage are retained.
        # v14: usage buckets split explicit request-local account fingerprints;
        # reparse enriched logs without clearing stored history.
        sync_prefix = f"{self.sync_state_prefix}:"
        assistant_ids_prefix = f"{self.sync_state_prefix}:assistant_ids:"
        checkpoint_prefix = f"{self.sync_state_prefix}:checkpoint:"

        try:
            project_dirs = list(projects_dir.iterdir())
        except OSError:
            return (active_ids, False)

        for project_dir in project_dirs:
            if not project_dir.is_dir():
                continue

            try:
                jsonl_files = sorted(
                    project_dir.rglob("*.jsonl"),
                    key=lambda path: (len(path.relative_to(project_dir).parts), str(path)),
                )
            except OSError:
                complete = False
                continue

            file_states = []
            for jsonl_file in jsonl_files:
                sync_key = f"{sync_prefix}{jsonl_file}"
                try:
                    stat = jsonl_file.stat()
                except OSError:
                    complete = False
                    continue
                state = _sync_state_value(stat.st_size, stat.st_mtime_ns)
                unchanged = db.get_sync_state(sync_key) == state
                file_states.append((jsonl_file, sync_key, state, unchanged))
                active_ids.add(
                    _session_id_for_jsonl(project_dir, jsonl_file, jsonl_file.stem)
                )
                active_sync_keys.add(sync_key)
                active_sync_keys.add(f"{assistant_ids_prefix}{jsonl_file}")
                active_sync_keys.add(f"{checkpoint_prefix}{jsonl_file}")

            if file_states and all(unchanged for _, _, _, unchanged in file_states):
                continue

            seen_assistant_ids: set[str] = set()
            for jsonl_file, sync_key, state, unchanged in file_states:
                reprocess_for_replay = False
                if unchanged:
                    cached = _cached_assistant_ids(
                        db,
                        f"{assistant_ids_prefix}{jsonl_file}",
                        state,
                    )
                    if cached is not None:
                        assistant_ids, owned_ids = cached
                        excluded_ids = assistant_ids - owned_ids
                        reprocess_for_replay = (
                            {item for item in assistant_ids if item in seen_assistant_ids}
                            != excluded_ids
                        )
                        if not reprocess_for_replay:
                            seen_assistant_ids.update(owned_ids)
                            continue

                real_cwd = _read_cwd(jsonl_file)
                project_path = real_cwd if real_cwd else str(project_dir)
                checkpoint_key = f"{checkpoint_prefix}{jsonl_file}"
                accum = _restore_checkpoint(
                    db,
                    checkpoint_key,
                    jsonl_file,
                    project_path,
                    seen_assistant_ids,
                )
                restored_offset = accum.offset if accum is not None else -1
                if accum is None:
                    accum = _SessionAccum(
                        jsonl_file,
                        project_path=project_path,
                        replayed_assistant_ids=seen_assistant_ids,
                    )
                accum.read_new_lines()
                seen_assistant_ids.update(accum.assistant_message_dates)
                _store_assistant_ids(
                    db,
                    f"{assistant_ids_prefix}{jsonl_file}",
                    state,
                    accum.all_assistant_ids,
                    set(accum.assistant_message_dates),
                )
                if accum.offset != restored_offset and not accum.tail_unterminated:
                    db.set_sync_state(
                        checkpoint_key,
                        _encode_checkpoint(accum),
                        commit=False,
                    )

                session_id = _session_id_for_jsonl(
                    project_dir,
                    jsonl_file,
                    accum.session_id,
                )
                has_token_usage = bool(
                    accum.input_tokens
                    or accum.output_tokens
                    or accum.cache_read
                    or accum.cache_create
                )
                if accum.user_turns == 0 and not has_token_usage:
                    if session_id in indexed_ids:
                        db.upsert_session(
                            session_id,
                            self.agent_type,
                            provider=self.provider,
                            data_root=self.data_root,
                            input_tokens=0,
                            output_tokens=0,
                            cache_read_tokens=0,
                            cache_creation_tokens=0,
                            cache_creation_1h_tokens=0,
                            estimated_cost_usd=0.0,
                        )
                        session = db.conn.execute(
                            """SELECT started_at, project_path FROM sessions
                               WHERE session_id = ? AND agent_type = ?
                                 AND provider = ? AND data_root = ?""",
                            (
                                session_id,
                                self.agent_type,
                                self.provider,
                                self.data_root,
                            ),
                        ).fetchone()
                        db.replace_session_usage(
                            session_id,
                            self.agent_type,
                            [_zero_usage_bucket(
                                session["started_at"] if session else "",
                                session["project_path"] if session else project_path,
                            )],
                            provider=self.provider,
                            data_root=self.data_root,
                        )
                    else:
                        db.delete_session(
                            session_id,
                            self.agent_type,
                            provider=self.provider,
                            data_root=self.data_root,
                        )
                    db.set_sync_state(sync_key, state, commit=False)
                    continue

                usage_rows = accum.usage_bucket_rows()
                cost = _usage_rows_cost(usage_rows)

                db.upsert_session(
                    session_id,
                    self.agent_type,
                    provider=self.provider,
                    data_root=self.data_root,
                    project_path=project_path,
                    git_branch=accum.git_branch,
                    model=accum.model,
                    message_count=accum.message_count,
                    user_turns=accum.user_turns,
                    input_tokens=accum.input_tokens,
                    output_tokens=accum.output_tokens,
                    cache_read_tokens=accum.cache_read,
                    cache_creation_tokens=accum.cache_create,
                    cache_creation_1h_tokens=accum.cache_create_1h,
                    estimated_cost_usd=cost,
                    started_at=accum.first_ts,
                    ended_at=accum.last_ts,
                    first_prompt=accum.first_prompt,
                    last_prompt=accum.last_prompt,
                )
                db.replace_session_usage(
                    session_id,
                    self.agent_type,
                    usage_rows,
                    provider=self.provider,
                    data_root=self.data_root,
                )

                db.set_sync_state(sync_key, state, commit=False)

        if complete and reconcile:
            reconcile_sync_state_keys(
                db,
                self.sync_state_prefix,
                active_sync_keys,
            )
        return (active_ids, complete)


def _sync_state_value(file_size: int, mtime_ns: int) -> str:
    """Return the on-disk sync stamp for a JSONL file."""
    return f"{file_size}:{mtime_ns}"


def _cached_assistant_ids(
    db,
    cache_key: str,
    state: str,
) -> tuple[set[str], set[str]] | None:
    """Return (all ids, owned ids), or None when ownership is unknown."""
    cached = db.get_sync_state(cache_key)
    if not cached:
        return None
    try:
        payload = json.loads(cached)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("state") != state:
        return None
    ids = payload.get("ids")
    owned = payload.get("owned_ids")
    if not (
        isinstance(ids, list)
        and isinstance(owned, list)
        and all(isinstance(item, str) for item in ids)
        and all(isinstance(item, str) for item in owned)
    ):
        return None
    ids_set, owned_set = set(ids), set(owned)
    return (ids_set, owned_set) if owned_set <= ids_set else None


def _store_assistant_ids(
    db,
    cache_key: str,
    state: str,
    ids: set[str],
    owned_ids: set[str],
) -> None:
    value = json.dumps(
        {"state": state, "ids": sorted(ids), "owned_ids": sorted(owned_ids)},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    db.set_sync_state(cache_key, value, commit=False)


# ── Persistent parse checkpoints ─────────────────────────────────────────

_CHECKPOINT_VERSION = 1
_CHECKPOINT_INTS = (
    "user_turns", "message_count", "input_tokens", "output_tokens",
    "cache_read", "cache_create", "cache_create_1h",
)
_CHECKPOINT_STRS = (
    "first_ts", "last_ts", "first_prompt", "last_prompt", "git_branch", "model",
)
_COST = (int, float, type(None))
_DATES_TYPES = (str, int, str, str)
_USAGE_TYPES = (int, int, int, int, int, _COST, str, int, str, str)
_BUCKET_TYPES = (str, int, str, str, int, int, int, int, int, int, int, _COST)
_BUCKET_FIELDS = (
    "message_count", "user_turns", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_creation_tokens", "cache_creation_1h_tokens",
    "estimated_cost_usd",
)


def _timezone_signature() -> list:
    return [os.environ.get("TZ", ""), list(time.tzname), time.timezone, time.altzone, time.daylight]


def _typed_row(row: object, types: tuple) -> bool:
    return (
        isinstance(row, list)
        and len(row) == len(types)
        and all(isinstance(value, kind) and not isinstance(value, bool) for value, kind in zip(row, types))
    )


def _encode_checkpoint(accum: _SessionAccum) -> str:
    ids: dict[str, list | None] = {}
    for msg_id in accum.all_assistant_ids:
        dates = accum.assistant_message_dates.get(msg_id)
        usage = accum.assistant_usage_by_id.get(msg_id)
        ids[msg_id] = None if dates is None else [
            list(dates),
            None if usage is None else list(usage),
        ]
    payload = {
        "v": _CHECKPOINT_VERSION,
        "tz": _timezone_signature(),
        "project": accum.project_path,
        "offset": accum.offset,
        "sha256": accum._hasher.hexdigest(),
        "partial": len(accum.partial_line),
        "scalars": {name: getattr(accum, name) for name in _CHECKPOINT_INTS + _CHECKPOINT_STRS},
        "ids": ids,
        "buckets": [
            [*key, *(bucket[field] for field in _BUCKET_FIELDS)]
            for key, bucket in accum.usage_buckets.items()
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(zlib.compress(raw, 1)).decode("ascii")


def _decode_checkpoint(value: str | None) -> dict | None:
    if not value:
        return None
    try:
        payload = json.loads(zlib.decompress(base64.b64decode(value, validate=True)))
        scalars = payload["scalars"]
        offset, partial = payload["offset"], payload["partial"]
        valid = (
            payload["v"] == _CHECKPOINT_VERSION
            and payload["tz"] == _timezone_signature()
            and isinstance(payload["project"], str)
            and type(offset) is int and type(partial) is int
            and 0 <= partial <= offset
            and isinstance(payload["sha256"], str)
            and all(type(scalars[name]) is int for name in _CHECKPOINT_INTS)
            and all(isinstance(scalars[name], str) for name in _CHECKPOINT_STRS)
            and isinstance(payload["ids"], dict)
            and all(
                entry is None or (
                    isinstance(entry, list) and len(entry) == 2
                    and _typed_row(entry[0], _DATES_TYPES)
                    and (entry[1] is None or _typed_row(entry[1], _USAGE_TYPES))
                )
                for entry in payload["ids"].values()
            )
            and isinstance(payload["buckets"], list)
            and all(_typed_row(row, _BUCKET_TYPES) for row in payload["buckets"])
        )
    except (ValueError, TypeError, KeyError, zlib.error):
        return None
    return payload if valid else None


def _restore_checkpoint(
    db,
    checkpoint_key: str,
    jsonl_file: Path,
    project_path: str,
    seen_assistant_ids: set[str],
) -> _SessionAccum | None:
    """Rebuild a parsed accumulator only after hashing its whole consumed prefix."""
    payload = _decode_checkpoint(db.get_sync_state(checkpoint_key))
    if payload is None or payload["project"] != project_path:
        return None
    ids = payload["ids"]
    replayed = {msg_id for msg_id, entry in ids.items() if entry is None}
    if {msg_id for msg_id in ids if msg_id in seen_assistant_ids} != replayed:
        return None

    offset, partial = payload["offset"], payload["partial"]
    hasher = hashlib.sha256()
    tail = b""
    try:
        with open(jsonl_file, "rb") as f:
            initial_stat = os.fstat(f.fileno())
            remaining = offset
            while remaining:
                chunk = f.read(min(remaining, 1 << 20))
                if not chunk:
                    return None
                remaining -= len(chunk)
                hasher.update(chunk)
                if partial:
                    tail = (tail + chunk)[-partial:]
            stat = os.fstat(f.fileno())
    except OSError:
        return None
    if (stat.st_size, stat.st_mtime_ns) != (initial_stat.st_size, initial_stat.st_mtime_ns):
        return None
    if hasher.hexdigest() != payload["sha256"]:
        return None

    accum = _SessionAccum(jsonl_file, project_path, seen_assistant_ids)
    for name, value in payload["scalars"].items():
        if name in _CHECKPOINT_INTS or name in _CHECKPOINT_STRS:
            setattr(accum, name, value)
    for msg_id, entry in ids.items():
        accum.all_assistant_ids.add(msg_id)
        if entry is not None:
            accum.assistant_message_dates[msg_id] = tuple(entry[0])
            if entry[1] is not None:
                accum.assistant_usage_by_id[msg_id] = tuple(entry[1])
    for row in payload["buckets"]:
        key = (row[0], row[1], row[2], row[3])
        accum.usage_buckets[key] = {
            "usage_date": row[0],
            "usage_hour": row[1],
            "project_path": project_path,
            "model": row[2],
            "account_key": row[3],
            **dict(zip(_BUCKET_FIELDS, row[4:])),
        }
    accum.offset = offset
    accum._hasher = hasher
    accum.partial_line = tail
    accum.file_id = (stat.st_dev, stat.st_ino)
    accum.file_mtime_ns = stat.st_mtime_ns
    accum._checkpoint_restored = True
    return accum


def _sync_state_matches(state: str | None, file_size: int, mtime_ns: int) -> bool:
    """Return True when the persisted sync stamp matches the current file."""
    if not state:
        return False
    parts = state.split(":", 1)
    if len(parts) != 2:
        return False
    try:
        return int(parts[0]) == file_size and int(parts[1]) == mtime_ns
    except ValueError:
        return False


def _session_id_for_jsonl(project_dir: Path, jsonl_file: Path, default: str) -> str:
    """Return a stable session id for top-level and subagent Claude JSONL files."""
    try:
        rel = jsonl_file.relative_to(project_dir)
    except ValueError:
        return default
    parts = rel.parts
    if len(parts) >= 3 and parts[-2] == "subagents":
        return f"{parts[-3]}:{jsonl_file.stem}"
    return default


def _zero_usage_bucket(started_at: str, project_path: str) -> dict:
    usage_date, usage_hour = local_time_bucket(started_at)
    return {
        "usage_date": usage_date,
        "usage_hour": usage_hour,
        "project_path": project_path,
        "model": "",
        "message_count": 0,
        "user_turns": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_creation_1h_tokens": 0,
        "estimated_cost_usd": 0.0,
    }


def _usage_rows_cost(rows: list[dict]) -> float | None:
    """Estimate cost using each bucket's own model."""
    total = 0.0
    for row in rows:
        if "estimated_cost_usd" in row:
            cost = row["estimated_cost_usd"]
        else:
            cost = estimate_cost(
                row.get("model") or "",
                input_tokens=int(row.get("input_tokens") or 0),
                output_tokens=int(row.get("output_tokens") or 0),
                cache_read_tokens=int(row.get("cache_read_tokens") or 0),
                cache_creation_tokens=int(row.get("cache_creation_tokens") or 0),
                cache_creation_1h_tokens=int(row.get("cache_creation_1h_tokens") or 0),
                apply_long_context=False,
            )
        if cost is None:
            return None
        total += float(cost)
    return total
