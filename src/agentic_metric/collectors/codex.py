"""Codex CLI collector: parse session JSONL files + live process monitoring."""

from __future__ import annotations

import json
import platform
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from ..config import CODEX_SESSIONS_DIR
from ..models import LiveSession
from ..pricing import estimate_cost
from . import BaseCollector
from ._process import get_running_cwds, normalize_cwd_key


_RECENT_ACTIVITY_SECONDS = 300


# ── Incremental JSONL accumulator ────────────────────────────────────────


class _SessionAccum:
    """Accumulator for incremental parsing of a single Codex session .jsonl file.

    Key difference from Claude Code: token counts in ``total_token_usage``
    are **cumulative**, so we overwrite rather than sum.
    """

    __slots__ = (
        "file_path",
        "project_path",
        "session_id",
        "pid",
        "offset",
        "user_turns",
        "message_count",
        "input_tokens",
        "raw_input_tokens",
        "output_tokens",
        "cache_read",
        "cache_create",
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
        "service_tier",
        "partial_line",
        "file_id",
        "file_mtime_ns",
        "is_forked",
        "seen_turn_context",
        "fork_baseline_raw_input",
        "fork_baseline_output",
        "fork_baseline_cache_read",
        "fork_baseline_cache_create",
        "usage_buckets",
    )

    def __init__(self, file_path: Path, project_path: str, pid: int = 0) -> None:
        self.file_path = file_path
        self.project_path = project_path
        self.session_id = ""
        self.pid = pid
        self.offset = 0
        self.user_turns = 0
        self.message_count = 0
        self.input_tokens = 0
        self.raw_input_tokens = 0
        self.output_tokens = 0
        self.cache_read = 0
        self.cache_create = 0
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
        self.service_tier = ""
        self.partial_line = b""
        self.file_id: tuple[int, int] | None = None
        self.file_mtime_ns = -1
        self.is_forked = False
        self.seen_turn_context = False
        self.fork_baseline_raw_input = 0
        self.fork_baseline_output = 0
        self.fork_baseline_cache_read = 0
        self.fork_baseline_cache_create = 0
        self.usage_buckets: dict[tuple[str, int, str, str], dict] = {}

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
        self.first_ts = ""
        self.last_ts = ""
        self.first_prompt = ""
        self.last_prompt = ""
        self.git_branch = ""
        self.model = ""
        self.service_tier = ""
        self.partial_line = b""
        self.is_forked = False
        self.seen_turn_context = False
        self.fork_baseline_raw_input = 0
        self.fork_baseline_output = 0
        self.fork_baseline_cache_read = 0
        self.fork_baseline_cache_create = 0
        self.usage_buckets.clear()
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
        service_tier: str | None = None,
    ) -> None:
        usage_date, usage_hour = _local_bucket(ts)
        if not usage_date:
            return
        bucket_service_tier = self.service_tier if service_tier is None else service_tier
        key = (usage_date, usage_hour, self.model or "", bucket_service_tier or "")
        bucket = self.usage_buckets.setdefault(
            key,
            {
                "usage_date": usage_date,
                "usage_hour": usage_hour,
                "project_path": self.project_path,
                "model": self.model or "",
                "service_tier": bucket_service_tier or "",
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
        bucket["service_tier"] = bucket_service_tier or ""
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

    def usage_bucket_rows(self) -> list[dict]:
        return list(self.usage_buckets.values())

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
            if not self.session_id:
                self.session_id = payload.get("id", "")
                source = payload.get("source", {})
                self.is_forked = bool(
                    payload.get("forked_from_id")
                    or (isinstance(source, dict) and source.get("subagent"))
                )
            if not self.project_path:
                self.project_path = payload.get("cwd", "")
            git = payload.get("git", {})
            if git and not self.git_branch:
                self.git_branch = git.get("branch", "")

        elif entry_type == "turn_context":
            payload = entry.get("payload", {})
            model = payload.get("model", "")
            if model:
                self.model = model
            service_tier = _extract_service_tier(payload)
            if service_tier:
                self.service_tier = service_tier
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
            service_tier = _extract_service_tier(payload) or _extract_service_tier(info)
            if service_tier:
                self.service_tier = service_tier
            usage = info.get("total_token_usage", {})
            if not usage:
                return
            # Cumulative: overwrite, don't sum.
            # OpenAI's ``input_tokens`` is the TOTAL (includes cached tokens),
            # whereas ``cached_input_tokens`` is the cached subset. Store the
            # non-cached portion as ``input_tokens`` so ``estimate_cost``
            # doesn't double-charge — its formula charges ``cache_read`` at
            # cache pricing AND ``input_tokens`` at full input pricing.
            #
            # Note: all three counters are cumulative. Update each only when
            # its key is present; values of 0 are valid cumulative readings
            # and should overwrite. We use a sentinel (``None``) to detect
            # key absence vs. real-zero.
            raw_input = usage.get("input_tokens")
            cached = usage.get("cached_input_tokens")
            out = usage.get("output_tokens")
            cache_create = usage.get("cache_creation_input_tokens")
            prev_input = self.input_tokens
            prev_output = self.output_tokens
            prev_cache_read = self.cache_read
            prev_cache_create = self.cache_create
            if out is not None:
                self.output_tokens = max(out - self.fork_baseline_output, 0)
            if raw_input is not None:
                self.raw_input_tokens = max(raw_input - self.fork_baseline_raw_input, 0)
            if cached is not None:
                self.cache_read = max(cached - self.fork_baseline_cache_read, 0)
            if cache_create is not None:
                self.cache_create = max(cache_create - self.fork_baseline_cache_create, 0)
            if raw_input is not None or cached is not None:
                self.input_tokens = max(self.raw_input_tokens - self.cache_read, 0)
            d_input = self.input_tokens - prev_input
            d_output = self.output_tokens - prev_output
            d_cache_read = self.cache_read - prev_cache_read
            d_cache_create = self.cache_create - prev_cache_create
            if d_input or d_output or d_cache_read or d_cache_create:
                event_cost = _event_cost_from_token_usage(
                    self.model,
                    info.get("last_token_usage"),
                    self.service_tier,
                )
                if event_cost is None:
                    event_cost = estimate_cost(
                        self.model,
                        input_tokens=d_input,
                        output_tokens=d_output,
                        cache_read_tokens=d_cache_read,
                        cache_creation_tokens=d_cache_create,
                        service_tier=self.service_tier,
                        apply_long_context=False,
                    )
                self._add_usage_bucket(
                    ts,
                    input_tokens=d_input,
                    output_tokens=d_output,
                    cache_read_tokens=d_cache_read,
                    cache_creation_tokens=d_cache_create,
                    estimated_cost_usd=event_cost,
                    service_tier=self.service_tier,
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
        if raw_input is not None:
            self.fork_baseline_raw_input = raw_input
        if cached is not None:
            self.fork_baseline_cache_read = cached
        if out is not None:
            self.fork_baseline_output = out
        if cache_create is not None:
            self.fork_baseline_cache_create = cache_create

    def to_live_session(self) -> LiveSession:
        return LiveSession(
            session_id=self.session_id or self.file_path.stem,
            agent_type="codex",
            project_path=self.project_path,
            git_branch=self.git_branch,
            model=self.model,
            service_tier=self.service_tier,
            message_count=self.message_count,
            user_turns=self.user_turns,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read,
            cache_creation_tokens=self.cache_create,
            started=self.first_ts,
            last_active=self.last_ts,
            first_prompt=self.first_prompt,
            last_prompt=self.last_prompt,
            pid=self.pid,
            today_input_tokens=self.today_input_tokens,
            today_output_tokens=self.today_output_tokens,
            today_cache_read_tokens=self.today_cache_read,
            today_cache_creation_tokens=self.today_cache_create,
            today_user_turns=self.today_user_turns,
            today_message_count=self.today_message_count,
        )


# ── Live monitor ─────────────────────────────────────────────────────────


class _LiveMonitor:
    """Monitors running Codex sessions with incremental JSONL parsing.

    Uses process detection to find running ``codex`` processes, then
    matches their CWDs to today's session files under
    ``~/.codex/sessions/YYYY/MM/DD/``.
    """

    def __init__(self) -> None:
        self._accums: dict[Path, _SessionAccum] = {}

    def refresh(self) -> list[LiveSession]:
        """Return currently running sessions."""
        pid_cwds: dict[int, str] = get_running_cwds("codex", exact=True)
        if not pid_cwds:
            if platform.system() == "Windows":
                return self._refresh_recent_files()
            return []

        cwd_to_pids: dict[str, list[int]] = {}
        cwd_display: dict[str, str] = {}
        for pid, cwd in pid_cwds.items():
            key = normalize_cwd_key(cwd)
            if not key:
                continue
            cwd_to_pids.setdefault(key, []).append(pid)
            cwd_display.setdefault(key, cwd)
        if not cwd_to_pids:
            return []

        today = date.today()
        candidate_files: dict[Path, float] = {}
        for day_offset in range(3):
            day = today - timedelta(days=day_offset)
            day_dir = CODEX_SESSIONS_DIR / str(day.year) / f"{day.month:02d}" / f"{day.day:02d}"
            if not day_dir.is_dir():
                continue
            try:
                for jsonl_file in day_dir.glob("rollout-*.jsonl"):
                    candidate_files[jsonl_file] = jsonl_file.stat().st_mtime
            except OSError:
                continue

        for jsonl_file in list(self._accums):
            if not jsonl_file.exists():
                continue
            try:
                candidate_files[jsonl_file] = jsonl_file.stat().st_mtime
            except OSError:
                continue

        cwd_to_files = self._files_by_active_cwd(candidate_files, cwd_to_pids)
        missing_cwds = set(cwd_to_pids) - set(cwd_to_files)
        if missing_cwds and CODEX_SESSIONS_DIR.exists():
            try:
                for jsonl_file in CODEX_SESSIONS_DIR.rglob("rollout-*.jsonl"):
                    if jsonl_file in candidate_files:
                        continue
                    cwd = normalize_cwd_key(self._read_cwd(jsonl_file))
                    if cwd not in missing_cwds:
                        continue
                    try:
                        candidate_files[jsonl_file] = jsonl_file.stat().st_mtime
                    except OSError:
                        continue
            except OSError:
                pass
            cwd_to_files = self._files_by_active_cwd(candidate_files, cwd_to_pids)

        if not cwd_to_files:
            return []

        results: list[LiveSession] = []
        active_files: set[Path] = set()

        for cwd, pids in cwd_to_pids.items():
            files = cwd_to_files.get(cwd, [])
            if not files:
                continue
            for idx, jsonl_file in enumerate(files[: max(1, len(pids))]):
                if jsonl_file in active_files:
                    continue
                active_files.add(jsonl_file)

                pid = pids[idx] if idx < len(pids) else 0
                accum = self._accums.get(jsonl_file)
                if accum is None:
                    accum = _SessionAccum(jsonl_file, cwd_display.get(cwd, cwd), pid=pid)
                    self._accums[jsonl_file] = accum
                else:
                    accum.pid = pid

                accum.read_new_lines()
                if accum.user_turns > 0:
                    results.append(accum.to_live_session())

        # Prune stale accumulators
        stale = [k for k in self._accums if k not in active_files]
        for k in stale:
            del self._accums[k]

        results.sort(key=lambda s: s.last_active, reverse=True)
        return results

    def _refresh_recent_files(self) -> list[LiveSession]:
        """Windows fallback when process CWD lookup is unavailable."""
        if not CODEX_SESSIONS_DIR.exists():
            return []
        cutoff = time.time() - _RECENT_ACTIVITY_SECONDS
        candidates: list[tuple[float, Path]] = []
        try:
            for jsonl_file in CODEX_SESSIONS_DIR.rglob("rollout-*.jsonl"):
                try:
                    mtime = jsonl_file.stat().st_mtime
                    if mtime >= cutoff:
                        candidates.append((mtime, jsonl_file))
                except OSError:
                    continue
        except OSError:
            return []

        results: list[LiveSession] = []
        active_files: set[Path] = set()
        for _mtime, jsonl_file in sorted(candidates, key=lambda item: item[0], reverse=True):
            active_files.add(jsonl_file)
            accum = self._accums.get(jsonl_file)
            if accum is None:
                accum = _SessionAccum(jsonl_file, project_path="")
                self._accums[jsonl_file] = accum
            accum.read_new_lines()
            if accum.user_turns > 0:
                results.append(accum.to_live_session())

        stale = [k for k in self._accums if k not in active_files]
        for k in stale:
            del self._accums[k]
        results.sort(key=lambda s: s.last_active, reverse=True)
        return results

    @staticmethod
    def _files_by_active_cwd(
        candidate_files: dict[Path, float],
        cwd_to_pids: dict[str, list[int]],
    ) -> dict[str, list[Path]]:
        """Group candidate JSONL files by active process CWD, newest first."""
        cwd_to_files: dict[str, list[Path]] = {}
        jsonl_files = sorted(candidate_files, key=lambda f: candidate_files[f], reverse=True)
        for jsonl_file in jsonl_files:
            cwd = normalize_cwd_key(_LiveMonitor._read_cwd(jsonl_file))
            if not cwd or cwd not in cwd_to_pids:
                continue
            cwd_to_files.setdefault(cwd, []).append(jsonl_file)
        return cwd_to_files

    @staticmethod
    def _read_cwd(jsonl_file: Path) -> str:
        """Extract cwd from the session_meta entry in a JSONL file."""
        try:
            with open(jsonl_file) as f:
                for i, line in enumerate(f):
                    if i > 10:
                        break
                    try:
                        entry = json.loads(line)
                        if entry.get("type") == "session_meta":
                            return entry.get("payload", {}).get("cwd", "")
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass
        return ""


# ── Collector implementation ─────────────────────────────────────────────


class CodexCollector(BaseCollector):
    """Collector for OpenAI Codex CLI agent data.

    - Live sessions: process detection + incremental JSONL parsing
    - History sync: walk all session JSONL files
    """

    agent_type = "codex"

    def __init__(self) -> None:
        self._monitor = _LiveMonitor()

    def get_live_sessions(self) -> list[LiveSession]:
        return self._monitor.refresh()

    def sync_history(self, db) -> None:
        """Sync Codex session history into the database."""
        self._sync_jsonl_sessions(db)
        db.commit()

    def _sync_jsonl_sessions(self, db) -> None:
        """Walk all ~/.codex/sessions/**/*.jsonl and upsert session data."""
        if not CODEX_SESSIONS_DIR.exists():
            return

        sync_prefix = "codex_jsonl:v5:"

        for jsonl_file in CODEX_SESSIONS_DIR.rglob("rollout-*.jsonl"):
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
            accum = _SessionAccum(jsonl_file, project_path="")
            accum.read_new_lines()

            if accum.user_turns == 0:
                db.set_sync_state(sync_key, _sync_state_value(file_size, mtime_ns))
                continue

            session_id = accum.session_id or jsonl_file.stem

            usage_rows = accum.usage_bucket_rows()
            cost = _usage_rows_cost(usage_rows)

            db.upsert_session(
                session_id,
                self.agent_type,
                project_path=accum.project_path,
                git_branch=accum.git_branch,
                model=accum.model,
                message_count=accum.message_count,
                user_turns=accum.user_turns,
                input_tokens=accum.input_tokens,
                output_tokens=accum.output_tokens,
                cache_read_tokens=accum.cache_read,
                cache_creation_tokens=accum.cache_create,
                estimated_cost_usd=cost,
                started_at=accum.first_ts,
                ended_at=accum.last_ts,
                first_prompt=accum.first_prompt,
                last_prompt=accum.last_prompt,
            )
            db.replace_session_usage(session_id, self.agent_type, usage_rows)

            db.set_sync_state(sync_key, _sync_state_value(file_size, mtime_ns))


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
                service_tier=str(row.get("service_tier") or ""),
                apply_long_context=False,
            )
        if cost is None:
            return None
        total += float(cost)
    return total


def _event_cost_from_token_usage(model: str, usage: object, service_tier: str = "") -> float | None:
    """Estimate one Codex/OpenAI token-count event when last-token usage exists."""
    if not isinstance(usage, dict):
        return None
    if not any(k in usage for k in ("input_tokens", "output_tokens", "cached_input_tokens")):
        return None
    raw_input = int(usage.get("input_tokens") or 0)
    cached = int(usage.get("cached_input_tokens") or 0)
    output = int(usage.get("output_tokens") or 0)
    cache_create = int(usage.get("cache_creation_input_tokens") or 0)
    return estimate_cost(
        model,
        input_tokens=max(raw_input - cached, 0),
        output_tokens=output,
        cache_read_tokens=cached,
        cache_creation_tokens=cache_create,
        service_tier=service_tier,
    )


def _extract_service_tier(payload: object) -> str:
    """Return a Codex service tier such as ``fast`` if present in log payloads."""
    if not isinstance(payload, dict):
        return ""
    for key in ("service_tier", "serviceTier", "speed"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    collab = payload.get("collaboration_mode")
    if isinstance(collab, dict):
        settings = collab.get("settings")
        nested = _extract_service_tier(settings)
        if nested:
            return nested
    settings = payload.get("settings")
    if settings is not payload:
        nested = _extract_service_tier(settings)
        if nested:
            return nested
    return ""


def _local_bucket(ts: str) -> tuple[str, int]:
    """Return local (date, hour) for an ISO timestamp."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%Y-%m-%d"), dt.hour
    except (ValueError, TypeError):
        return (ts[:10], 0) if len(ts) >= 10 else ("", 0)
