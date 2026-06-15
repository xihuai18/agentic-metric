"""Codex CLI collector: parse session JSONL files into history."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path

from ..config import CODEX_SESSIONS_DIR
from ..pricing import estimate_cost
from ..usage import (
    estimate_token_usage_cost,
    openai_input_tokens_are_separate,
    normalize_openai_usage,
)
from . import BaseCollector


# ── Incremental JSONL accumulator ────────────────────────────────────────


class _SessionAccum:
    """Accumulator for incremental parsing of a single Codex session .jsonl file.

    ``total_token_usage`` snapshots are cumulative. Newer Codex logs also carry
    ``last_token_usage`` for the request that produced the snapshot; those
    request-level values are the preferred billing source, while the cumulative
    snapshot is used to skip repeated token_count events and to support older
    logs that do not expose per-request usage.
    """

    __slots__ = (
        "file_path",
        "project_path",
        "session_id",
        "offset",
        "user_turns",
        "message_count",
        "input_tokens",
        "raw_input_tokens",
        "output_tokens",
        "cache_read",
        "cache_create",
        "_cum_output_tokens",
        "_cum_cache_read",
        "_cum_cache_create",
        "_cum_total_tokens",
        "today_user_turns",
        "today_message_count",
        "today_input_tokens",
        "today_output_tokens",
        "today_cache_read",
        "today_cache_create",
        "today_input_base",
        "today_output_base",
        "today_cache_read_base",
        "today_cache_create_base",
        "today_key",
        "first_ts",
        "last_ts",
        "first_prompt",
        "last_prompt",
        "git_branch",
        "model",
        "partial_line",
        "file_id",
        "file_mtime_ns",
        "is_forked",
        "seen_turn_context",
        "fork_baseline_raw_input",
        "fork_baseline_output",
        "fork_baseline_cache_read",
        "fork_baseline_cache_create",
        "fork_baseline_total_tokens",
        "usage_buckets",
        "provider",
        "provider_locked",
        "observed_provider",
        "data_root",
    )

    def __init__(
        self,
        file_path: Path,
        project_path: str,
        provider: str = "",
        data_root: str = "",
    ) -> None:
        self.file_path = file_path
        self.project_path = project_path
        self.session_id = ""
        self.offset = 0
        self.user_turns = 0
        self.message_count = 0
        self.input_tokens = 0
        self.raw_input_tokens = 0
        self.output_tokens = 0
        self.cache_read = 0
        self.cache_create = 0
        self._cum_output_tokens = 0
        self._cum_cache_read = 0
        self._cum_cache_create = 0
        self._cum_total_tokens = 0
        self.today_user_turns = 0
        self.today_message_count = 0
        self.today_input_tokens = 0
        self.today_output_tokens = 0
        self.today_cache_read = 0
        self.today_cache_create = 0
        self.today_input_base = 0
        self.today_output_base = 0
        self.today_cache_read_base = 0
        self.today_cache_create_base = 0
        self.today_key = ""
        self.first_ts = ""
        self.last_ts = ""
        self.first_prompt = ""
        self.last_prompt = ""
        self.git_branch = ""
        self.model = ""
        self.partial_line = b""
        self.file_id: tuple[int, int] | None = None
        self.file_mtime_ns = -1
        self.is_forked = False
        self.seen_turn_context = False
        self.fork_baseline_raw_input = 0
        self.fork_baseline_output = 0
        self.fork_baseline_cache_read = 0
        self.fork_baseline_cache_create = 0
        self.fork_baseline_total_tokens = 0
        self.usage_buckets: dict[tuple[str, int, str], dict] = {}
        self.provider = provider.strip()
        self.provider_locked = bool(self.provider)
        self.observed_provider = ""
        self.data_root = data_root

    def read_new_lines(self) -> None:
        """Read only bytes appended since last call.

        If the file shrank (truncated or replaced), reset state and re-parse
        from offset 0 — otherwise we'd silently miss data.
        """
        today_str = date.today().strftime("%Y-%m-%d")
        if today_str != self.today_key:
            self._reset_today_counters(today_str)

        try:
            stat = self.file_path.stat()
            size = stat.st_size
            file_id = (stat.st_dev, stat.st_ino)
            mtime_ns = stat.st_mtime_ns
        except OSError:
            return
        same_size_rewrite = (
            size == self.offset
            and self.file_mtime_ns >= 0
            and mtime_ns != self.file_mtime_ns
        )
        if (
            (self.file_id is not None and file_id != self.file_id)
            or size < self.offset
            or same_size_rewrite
        ):
            self._reset_parsed_state(today_str)
        self.file_id = file_id
        if size == self.offset:
            self.file_mtime_ns = mtime_ns
            return
        try:
            with open(self.file_path, "rb") as f:
                f.seek(self.offset)
                new_data = f.read()
            self.offset = size
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
            self._process_raw_line(raw_line)

        if tail.strip() and not self._process_raw_line(tail):
            self.partial_line = tail

    def _reset_parsed_state(self, today_str: str) -> None:
        """Reset parsed counters after file truncation/replacement."""
        self.session_id = ""
        self.offset = 0
        self.user_turns = 0
        self.message_count = 0
        self.input_tokens = 0
        self.raw_input_tokens = 0
        self.output_tokens = 0
        self.cache_read = 0
        self.cache_create = 0
        self._cum_output_tokens = 0
        self._cum_cache_read = 0
        self._cum_cache_create = 0
        self._cum_total_tokens = 0
        self.first_ts = ""
        self.last_ts = ""
        self.first_prompt = ""
        self.last_prompt = ""
        self.git_branch = ""
        self.model = ""
        self.partial_line = b""
        self.is_forked = False
        self.seen_turn_context = False
        self.fork_baseline_raw_input = 0
        self.fork_baseline_output = 0
        self.fork_baseline_cache_read = 0
        self.fork_baseline_cache_create = 0
        self.fork_baseline_total_tokens = 0
        self.usage_buckets.clear()
        self.observed_provider = ""
        self._reset_today_counters(today_str)

    def _reset_today_counters(self, today_str: str) -> None:
        """Reset day-local counters and use current totals as the baseline."""
        self.today_key = today_str
        self.today_user_turns = 0
        self.today_message_count = 0
        self.today_input_tokens = 0
        self.today_output_tokens = 0
        self.today_cache_read = 0
        self.today_cache_create = 0
        self.today_input_base = self.input_tokens
        self.today_output_base = self.output_tokens
        self.today_cache_read_base = self.cache_read
        self.today_cache_create_base = self.cache_create

    @staticmethod
    def _ts_local_date(ts: str) -> str:
        """Convert ISO timestamp to local date string YYYY-MM-DD."""
        day, _hour = _local_bucket(ts)
        return day

    def _add_usage_bucket(
        self,
        ts: str,
        *,
        user_turns: int = 0,
        message_count: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        estimated_cost_usd: float | None = 0.0,
    ) -> None:
        usage_date, usage_hour = _local_bucket(ts)
        if not usage_date:
            return
        key = (usage_date, usage_hour, self.model or "")
        bucket = self.usage_buckets.setdefault(
            key,
            {
                "usage_date": usage_date,
                "usage_hour": usage_hour,
                "project_path": self.project_path,
                "model": self.model or "",
                "message_count": 0,
                "user_turns": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
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
        if estimated_cost_usd is None:
            bucket["estimated_cost_usd"] = None
        elif bucket["estimated_cost_usd"] is not None:
            bucket["estimated_cost_usd"] += estimated_cost_usd

    def _replace_usage_token_snapshot(
        self,
        ts: str,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_creation_tokens: int,
        estimated_cost_usd: float | None,
    ) -> None:
        """Replace fallback token buckets when cumulative counters reclassify.

        Older Codex logs may lack ``last_token_usage`` and only expose
        cumulative snapshots. If a later snapshot moves tokens from input into
        cache read, a raw delta would create a negative input bucket. Rebuilding
        the token snapshot keeps buckets non-negative and preserves the current
        normalized session totals. This rare fallback collapses prior hourly
        token distribution into the current timestamp bucket; the session total
        remains correct, but the historical hour split does not.
        """
        for bucket in self.usage_buckets.values():
            bucket["input_tokens"] = 0
            bucket["output_tokens"] = 0
            bucket["cache_read_tokens"] = 0
            bucket["cache_creation_tokens"] = 0
            if bucket["estimated_cost_usd"] is not None:
                bucket["estimated_cost_usd"] = 0.0

        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read = 0
        self.cache_create = 0
        self._add_usage_bucket(
            ts,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
            estimated_cost_usd=estimated_cost_usd,
        )
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read = cache_read_tokens
        self.cache_create = cache_creation_tokens

    def usage_bucket_rows(self) -> list[dict]:
        return list(self.usage_buckets.values())

    def matches_configured_provider(self) -> bool:
        """False when this configured root is parsing another provider's session."""
        if not self.provider_locked or not self.observed_provider:
            return True
        return self.observed_provider == self.provider

    def _process_raw_line(self, raw_line: bytes) -> bool:
        """Process one JSONL line. Return False for an unparsable line."""
        raw_line = raw_line.strip()
        if not raw_line:
            return True
        try:
            entry = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False
        self._process_entry(entry)
        return True

    def _process_entry(self, entry: dict) -> None:
        ts = entry.get("timestamp", "")
        if ts:
            if not self.first_ts:
                self.first_ts = ts
            self.last_ts = ts

        is_today = self._ts_local_date(ts) == self.today_key if ts else True
        entry_type = entry.get("type", "")

        if entry_type == "session_meta":
            payload = entry.get("payload", {})
            provider = str(payload.get("model_provider") or "").strip()
            if provider:
                self.observed_provider = provider
            if not self.session_id:
                self.session_id = payload.get("id", "")
                source = payload.get("source", {})
                self.is_forked = bool(
                    payload.get("forked_from_id")
                    or (isinstance(source, dict) and source.get("subagent"))
                )
            if not self.project_path:
                self.project_path = payload.get("cwd", "")
            if not self.provider_locked:
                if provider:
                    self.provider = provider
            git = payload.get("git", {})
            if git and not self.git_branch:
                self.git_branch = git.get("branch", "")

        elif entry_type == "turn_context":
            payload = entry.get("payload", {})
            model = payload.get("model", "")
            if model:
                self.model = model
            self.seen_turn_context = True

        elif entry_type == "event_msg":
            self._process_event_msg(entry.get("payload", {}), is_today, ts)

    def _process_event_msg(self, payload: dict, is_today: bool = True, ts: str = "") -> None:
        msg_type = payload.get("type", "")

        if self.is_forked and not self.seen_turn_context:
            if msg_type == "token_count":
                self._update_fork_baseline(payload)
            return

        if msg_type == "user_message":
            self.user_turns += 1
            self.message_count += 1
            self._add_usage_bucket(ts, user_turns=1, message_count=1)
            if is_today:
                self.today_user_turns += 1
                self.today_message_count += 1
            text = payload.get("message", "")
            if isinstance(text, str) and text.strip():
                clean = text.strip()[:80]
                if not self.first_prompt:
                    self.first_prompt = clean
                self.last_prompt = clean

        elif msg_type == "agent_message":
            self.message_count += 1
            self._add_usage_bucket(ts, message_count=1)
            if is_today:
                self.today_message_count += 1

        elif msg_type == "token_count":
            info = payload.get("info")
            if not info:
                return
            usage = info.get("total_token_usage", {})
            if not usage:
                return
            # total_token_usage is cumulative; last_token_usage, when present,
            # is the request-level billing source.
            # OpenAI's ``input_tokens`` is usually the TOTAL (includes cached
            # tokens), whereas some compatible gateways report it as the
            # non-cached portion and add ``cached_input_tokens`` separately.
            # Store only the non-cached portion as ``input_tokens`` so
            # ``estimate_cost`` doesn't double-charge cached reads.
            #
            # Note: all three counters are cumulative. Update each only when
            # its key is present; values of 0 are valid cumulative readings
            # and should overwrite. We use a sentinel (``None``) to detect
            # key absence vs. real-zero.
            raw_input = usage.get("input_tokens")
            cached = usage.get("cached_input_tokens")
            out = usage.get("output_tokens")
            cache_create = usage.get("cache_creation_input_tokens")
            total = usage.get("total_tokens")
            default_separate = _input_tokens_default_separate(self.provider)
            event_usage = normalize_openai_usage(
                info.get("last_token_usage"),
                default_input_tokens_are_separate=default_separate,
            )
            prev_raw_input = self.raw_input_tokens
            prev_output = self._cum_output_tokens
            prev_cache_read = self._cum_cache_read
            prev_cache_create = self._cum_cache_create
            prev_total = self._cum_total_tokens
            if out is not None:
                self._cum_output_tokens = max(out - self.fork_baseline_output, 0)
            if raw_input is not None:
                self.raw_input_tokens = max(raw_input - self.fork_baseline_raw_input, 0)
            if cached is not None:
                self._cum_cache_read = max(cached - self.fork_baseline_cache_read, 0)
            if cache_create is not None:
                self._cum_cache_create = max(cache_create - self.fork_baseline_cache_create, 0)
            if total is not None:
                self._cum_total_tokens = max(total - self.fork_baseline_total_tokens, 0)
            if event_usage is not None:
                cumulative_changed = (
                    self.raw_input_tokens != prev_raw_input
                    or self._cum_output_tokens != prev_output
                    or self._cum_cache_read != prev_cache_read
                    or self._cum_cache_create != prev_cache_create
                    or self._cum_total_tokens != prev_total
                )
                d_input, d_output, d_cache_read, d_cache_create = (
                    event_usage.as_bucket_tuple() if cumulative_changed else (0, 0, 0, 0)
                )
            else:
                detection_usage = usage
                if total is not None:
                    detection_usage = dict(usage)
                    detection_usage["total_tokens"] = self._cum_total_tokens
                input_is_separate = openai_input_tokens_are_separate(
                    detection_usage,
                    raw_input=self.raw_input_tokens,
                    cached_input=self._cum_cache_read,
                    output_tokens=self._cum_output_tokens,
                    default_is_separate=default_separate,
                )
                if input_is_separate:
                    prev_input = prev_raw_input
                    current_input = self.raw_input_tokens
                else:
                    prev_input = max(prev_raw_input - prev_cache_read, 0)
                    current_input = max(self.raw_input_tokens - self._cum_cache_read, 0)
                d_input = current_input - prev_input
                d_output = self._cum_output_tokens - prev_output
                d_cache_read = self._cum_cache_read - prev_cache_read
                d_cache_create = self._cum_cache_create - prev_cache_create
                if min(d_input, d_output, d_cache_read, d_cache_create) < 0:
                    event_cost = estimate_cost(
                        self.model,
                        input_tokens=current_input,
                        output_tokens=self._cum_output_tokens,
                        cache_read_tokens=self._cum_cache_read,
                        cache_creation_tokens=self._cum_cache_create,
                        apply_long_context=False,
                    )
                    self._replace_usage_token_snapshot(
                        ts,
                        input_tokens=current_input,
                        output_tokens=self._cum_output_tokens,
                        cache_read_tokens=self._cum_cache_read,
                        cache_creation_tokens=self._cum_cache_create,
                        estimated_cost_usd=event_cost,
                    )
                    d_input = d_output = d_cache_read = d_cache_create = 0
            if d_input or d_output or d_cache_read or d_cache_create:
                event_cost = (
                    estimate_token_usage_cost(self.model, event_usage)
                    if event_usage is not None
                    else None
                )
                if event_cost is None:
                    event_cost = estimate_cost(
                        self.model,
                        input_tokens=d_input,
                        output_tokens=d_output,
                        cache_read_tokens=d_cache_read,
                        cache_creation_tokens=d_cache_create,
                        apply_long_context=False,
                    )
                self.input_tokens += d_input
                self.output_tokens += d_output
                self.cache_read += d_cache_read
                self.cache_create += d_cache_create
                self._add_usage_bucket(
                    ts,
                    input_tokens=d_input,
                    output_tokens=d_output,
                    cache_read_tokens=d_cache_read,
                    cache_creation_tokens=d_cache_create,
                    estimated_cost_usd=event_cost,
                )
            if is_today:
                self.today_input_tokens = max(self.input_tokens - self.today_input_base, 0)
                self.today_output_tokens = max(self.output_tokens - self.today_output_base, 0)
                self.today_cache_read = max(self.cache_read - self.today_cache_read_base, 0)
                self.today_cache_create = max(self.cache_create - self.today_cache_create_base, 0)
            else:
                self.today_input_base = self.input_tokens
                self.today_output_base = self.output_tokens
                self.today_cache_read_base = self.cache_read
                self.today_cache_create_base = self.cache_create

    def _update_fork_baseline(self, payload: dict) -> None:
        """Remember replayed parent cumulative usage before a forked run starts."""
        info = payload.get("info")
        if not info:
            return
        usage = info.get("total_token_usage", {})
        if not usage:
            return
        raw_input = usage.get("input_tokens")
        cached = usage.get("cached_input_tokens")
        out = usage.get("output_tokens")
        cache_create = usage.get("cache_creation_input_tokens")
        total = usage.get("total_tokens")
        if raw_input is not None:
            self.fork_baseline_raw_input = raw_input
        if cached is not None:
            self.fork_baseline_cache_read = cached
        if out is not None:
            self.fork_baseline_output = out
        if cache_create is not None:
            self.fork_baseline_cache_create = cache_create
        if total is not None:
            self.fork_baseline_total_tokens = total


# ── Collector implementation ─────────────────────────────────────────────


class CodexCollector(BaseCollector):
    """Collector for OpenAI Codex CLI agent data.

    History sync: walk all session JSONL files.
    """

    agent_type = "codex"

    def __init__(
        self,
        sessions_dir: Path | None = None,
        provider: str = "",
        data_root: str = "",
    ) -> None:
        self.sessions_dir = sessions_dir
        self.provider = provider.strip()
        self.data_root = data_root
        self.agent_type = "codex"

    def _sessions_dir(self) -> Path:
        return self.sessions_dir or CODEX_SESSIONS_DIR

    def sync_history(self, db) -> None:
        """Sync Codex session history into the database."""
        self._sync_jsonl_sessions(db)
        db.commit()

    def _sync_jsonl_sessions(self, db) -> None:
        """Walk all ~/.codex/sessions/**/*.jsonl and upsert session data."""
        sessions_dir = self._sessions_dir()
        if not sessions_dir.exists():
            return

        # v9: compatible gateways may report input_tokens as non-cached input
        # with cached_input_tokens added separately; reparse to normalize that
        # provider-specific shape.
        # v10: usage buckets preserve cache_creation_1h_tokens for accurate
        # repricing while still storing total cache write tokens.
        # v11: provider-aware cached-input fallback and forked baseline
        # detection use one consistent total-token frame.
        # v12: Codex/OpenAI-compatible usage no longer produces Anthropic-only
        # 1h cache-write splits.
        # v13: provider is part of the sessions/session_usage primary key;
        # reparse so every usage row has a matching session total row.
        sync_prefix = f"codex_jsonl:v13:{_sync_key_identity(self.provider, self.data_root)}:"

        for jsonl_file in sessions_dir.rglob("rollout-*.jsonl"):
            sync_key = f"{sync_prefix}{jsonl_file}"
            prev_state = db.get_sync_state(sync_key)

            try:
                stat = jsonl_file.stat()
            except OSError:
                continue
            file_size = stat.st_size
            mtime_ns = stat.st_mtime_ns

            if _sync_state_matches(prev_state, file_size, mtime_ns):
                continue

            # Full parse to get cumulative totals
            accum = _SessionAccum(
                jsonl_file,
                project_path="",
                provider=self.provider,
                data_root=self.data_root,
            )
            accum.read_new_lines()

            if not accum.matches_configured_provider():
                session_id = accum.session_id or ""
                if session_id:
                    db.delete_session(
                        session_id,
                        self.agent_type,
                        provider=self.provider,
                        data_root=self.data_root,
                    )
                db.set_sync_state(sync_key, _sync_state_value(file_size, mtime_ns))
                continue

            if accum.user_turns == 0:
                db.set_sync_state(sync_key, _sync_state_value(file_size, mtime_ns))
                continue

            session_id = accum.session_id or jsonl_file.stem

            usage_rows = accum.usage_bucket_rows()
            cost = _usage_rows_cost(usage_rows)

            db.upsert_session(
                session_id,
                self.agent_type,
                provider=accum.provider,
                data_root=self.data_root,
                project_path=accum.project_path,
                git_branch=accum.git_branch,
                model=accum.model,
                message_count=accum.message_count,
                user_turns=accum.user_turns,
                input_tokens=accum.input_tokens,
                output_tokens=accum.output_tokens,
                cache_read_tokens=accum.cache_read,
                cache_creation_tokens=accum.cache_create,
                cache_creation_1h_tokens=0,
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
                provider=accum.provider,
                data_root=self.data_root,
            )

            db.set_sync_state(sync_key, _sync_state_value(file_size, mtime_ns))


def _sync_key_identity(provider: str, data_root: str) -> str:
    """Return a stable sync identity for one configured Codex root/provider."""
    raw = json.dumps(
        {"provider": provider or "", "data_root": data_root or ""},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _sync_state_value(file_size: int, mtime_ns: int) -> str:
    """Return the on-disk sync stamp for a JSONL file."""
    return f"{file_size}:{mtime_ns}"


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


def _input_tokens_default_separate(provider: str) -> bool:
    """Default ambiguous cached-input semantics for one Codex provider.

    Non-OpenAI Codex-compatible gateways are treated as "input is already
    non-cached" only when ``total_tokens`` is missing. Current ichat payloads
    include ``total_tokens`` and use OpenAI-style total-input semantics, so the
    equality check in ``openai_input_tokens_are_separate`` overrides this
    default and prevents double counting cached input.
    """
    return bool(provider) and provider.strip().lower() != "openai"


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
                cache_creation_1h_tokens=0,
                apply_long_context=False,
            )
        if cost is None:
            return None
        total += float(cost)
    return total


def _local_bucket(ts: str) -> tuple[str, int]:
    """Return local (date, hour) for an ISO timestamp."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%Y-%m-%d"), dt.hour
    except (ValueError, TypeError):
        return (ts[:10], 0) if len(ts) >= 10 else ("", 0)
