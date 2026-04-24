"""Claude Code collector: parse local JSONL/JSON files + live process monitoring."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from ..config import PROJECTS_DIR
from ..models import LiveSession
from ..pricing import estimate_cost
from . import BaseCollector
from ._process import get_running_cwds


# ── Helpers ──────────────────────────────────────────────────────────────


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
    Builds LiveSession objects with agent_type='claude_code'.
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
        "output_tokens",
        "cache_read",
        "cache_create",
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
        "today_key",
        "partial_line",
        "file_id",
        "file_mtime_ns",
        "assistant_message_dates",
        "assistant_usage_by_id",
        "usage_buckets",
    )

    def __init__(self, file_path: Path, project_path: str, pid: int = 0) -> None:
        self.file_path = file_path
        self.project_path = project_path
        self.session_id = file_path.stem
        self.pid = pid
        self.offset = 0
        self.user_turns = 0
        self.message_count = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read = 0
        self.cache_create = 0
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
        self.today_key = ""
        self.partial_line = b""
        self.file_id: tuple[int, int] | None = None
        self.file_mtime_ns = -1
        self.assistant_message_dates: dict[str, tuple[str, int, str]] = {}
        self.assistant_usage_by_id: dict[str, tuple[int, int, int, int, str, int, str]] = {}
        self.usage_buckets: dict[tuple[str, int, str], dict] = {}

    def read_new_lines(self) -> None:
        """Read only bytes appended since last call."""
        today_str = datetime.now().strftime("%Y-%m-%d")
        if today_str != self.today_key:
            self._reset_today_counters(today_str)

        try:
            stat = self.file_path.stat()
            size = stat.st_size
            file_id = (stat.st_dev, stat.st_ino)
            mtime_ns = stat.st_mtime_ns
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
            self._process_raw_line(raw_line, today_str)

        if tail.strip() and not self._process_raw_line(tail, today_str):
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
        self.first_ts = ""
        self.last_ts = ""
        self.first_prompt = ""
        self.last_prompt = ""
        self.git_branch = ""
        self.model = ""
        self.partial_line = b""
        self.assistant_message_dates.clear()
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

    @staticmethod
    def _ts_local_date(ts: str) -> str:
        """Convert ISO timestamp to local date string YYYY-MM-DD."""
        day, _hour = _local_bucket(ts)
        return day

    def _add_usage_bucket(
        self,
        usage_date: str,
        usage_hour: int,
        *,
        model: str | None = None,
        user_turns: int = 0,
        message_count: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
    ) -> None:
        if not usage_date:
            return
        bucket_model = model if model is not None else self.model
        key = (usage_date, usage_hour, bucket_model or "")
        bucket = self.usage_buckets.setdefault(
            key,
            {
                "usage_date": usage_date,
                "usage_hour": usage_hour,
                "project_path": self.project_path,
                "model": bucket_model or "",
                "message_count": 0,
                "user_turns": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
            },
        )
        bucket["project_path"] = self.project_path
        bucket["message_count"] += message_count
        bucket["user_turns"] += user_turns
        bucket["input_tokens"] += input_tokens
        bucket["output_tokens"] += output_tokens
        bucket["cache_read_tokens"] += cache_read_tokens
        bucket["cache_creation_tokens"] += cache_creation_tokens

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
                entry_date, entry_hour = _local_bucket(ts)
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
            msg_model = msg.get("model", "") if isinstance(msg, dict) else ""
            if msg_model and not self.model:
                self.model = msg_model
            entry_date, entry_hour = _local_bucket(ts)
            if not entry_date:
                entry_date, entry_hour = today_str, 0
            bucket_model = msg_model or self.model
            self._count_assistant_message(
                msg_id,
                entry_date,
                entry_hour,
                today_str,
                bucket_model,
            )

            usage = msg.get("usage", {}) if isinstance(msg, dict) else {}
            if usage:
                inp = usage.get("input_tokens", 0)
                out = usage.get("output_tokens", 0)
                cr = usage.get("cache_read_input_tokens", 0)
                cw = usage.get("cache_creation_input_tokens", 0)
                self._set_assistant_usage(
                    msg_id,
                    (inp, out, cr, cw),
                    entry_date,
                    entry_hour,
                    today_str,
                    bucket_model,
                )

    def _count_assistant_message(
        self,
        msg_id: str,
        entry_date: str,
        entry_hour: int,
        today_str: str,
        model: str,
    ) -> None:
        """Count each Claude Code assistant message once by message id."""
        if not msg_id:
            self.message_count += 1
            self._add_usage_bucket(
                entry_date,
                entry_hour,
                model=model,
                message_count=1,
            )
            if entry_date == today_str:
                self.today_message_count += 1
            return

        prev = self.assistant_message_dates.get(msg_id)
        if prev is None:
            self.assistant_message_dates[msg_id] = (entry_date, entry_hour, model)
            self.message_count += 1
            self._add_usage_bucket(
                entry_date,
                entry_hour,
                model=model,
                message_count=1,
            )
            if entry_date == today_str:
                self.today_message_count += 1
            return

        prev_date, prev_hour, prev_model = prev
        if (prev_date, prev_hour, prev_model) == (entry_date, entry_hour, model):
            return

        # A rare cross-midnight update for the same streamed message should
        # move the day-local count without changing the session total.
        self._add_usage_bucket(prev_date, prev_hour, model=prev_model, message_count=-1)
        self._add_usage_bucket(entry_date, entry_hour, model=model, message_count=1)
        if prev_date == today_str and entry_date != today_str:
            self.today_message_count = max(0, self.today_message_count - 1)
        elif prev_date != today_str and entry_date == today_str:
            self.today_message_count += 1
        self.assistant_message_dates[msg_id] = (entry_date, entry_hour, model)

    def _set_assistant_usage(
        self,
        msg_id: str,
        usage: tuple[int, int, int, int],
        entry_date: str,
        entry_hour: int,
        today_str: str,
        model: str,
    ) -> None:
        """Use the last usage snapshot for a Claude Code assistant message."""
        inp, out, cr, cw = usage

        if msg_id:
            prev = self.assistant_usage_by_id.get(msg_id)
            if prev is not None:
                p_in, p_out, p_cr, p_cw, p_date, p_hour, p_model = prev
                self.input_tokens = max(0, self.input_tokens - p_in)
                self.output_tokens = max(0, self.output_tokens - p_out)
                self.cache_read = max(0, self.cache_read - p_cr)
                self.cache_create = max(0, self.cache_create - p_cw)
                self._add_usage_bucket(
                    p_date,
                    p_hour,
                    model=p_model,
                    input_tokens=-p_in,
                    output_tokens=-p_out,
                    cache_read_tokens=-p_cr,
                    cache_creation_tokens=-p_cw,
                )
                if p_date == today_str:
                    self.today_input_tokens = max(0, self.today_input_tokens - p_in)
                    self.today_output_tokens = max(0, self.today_output_tokens - p_out)
                    self.today_cache_read = max(0, self.today_cache_read - p_cr)
                    self.today_cache_create = max(0, self.today_cache_create - p_cw)

            self.assistant_usage_by_id[msg_id] = (
                inp,
                out,
                cr,
                cw,
                entry_date,
                entry_hour,
                model,
            )

        self.input_tokens += inp
        self.output_tokens += out
        self.cache_read += cr
        self.cache_create += cw
        self._add_usage_bucket(
            entry_date,
            entry_hour,
            model=model,
            input_tokens=inp,
            output_tokens=out,
            cache_read_tokens=cr,
            cache_creation_tokens=cw,
        )
        if entry_date == today_str:
            self.today_input_tokens += inp
            self.today_output_tokens += out
            self.today_cache_read += cr
            self.today_cache_create += cw

    def to_live_session(self) -> LiveSession:
        return LiveSession(
            session_id=self.session_id,
            agent_type="claude_code",
            project_path=self.project_path,
            git_branch=self.git_branch,
            model=self.model,
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
    """Monitors running Claude Code sessions with incremental JSONL parsing.

    Uses process detection to find running ``claude`` processes, maps their
    CWDs to PROJECTS_DIR subdirectories, then incrementally parses the
    most-recently-modified .jsonl file for each active project.

    Designed for ~1 s refresh cadence: first call does full parse,
    subsequent calls only read newly appended bytes.
    """

    def __init__(self) -> None:
        # cwd -> project_dir mapping (rebuilt when unknown cwds appear)
        self._cwd_map: dict[str, Path] = {}
        self._cwd_map_built = False
        # file_path -> accumulator (persists across refreshes)
        self._accums: dict[Path, _SessionAccum] = {}

    def refresh(self) -> list[LiveSession]:
        """Return currently running sessions. Fast on repeated calls."""
        pid_cwds: dict[int, str] = get_running_cwds("claude", exact=True)
        if not pid_cwds:
            return []

        cwd_set = set(pid_cwds.values())

        # Rebuild cwd map if we see unknown cwds
        if not self._cwd_map_built or not cwd_set.issubset(self._cwd_map.keys()):
            self._build_cwd_map()

        # Build cwd -> list of pids (multiple sessions may share a cwd)
        cwd_to_pids: dict[str, list[int]] = {}
        for pid, cwd in pid_cwds.items():
            cwd_to_pids.setdefault(cwd, []).append(pid)

        results: list[LiveSession] = []
        active_files: set[Path] = set()

        for cwd in cwd_set:
            project_dir = self._cwd_map.get(cwd)
            if not project_dir:
                continue

            # Find most recently modified .jsonl files
            try:
                jsonl_files = sorted(
                    project_dir.glob("*.jsonl"),
                    key=lambda f: f.stat().st_mtime,
                    reverse=True,
                )
            except OSError:
                continue
            if not jsonl_files:
                continue

            # Pick as many JSONL files as there are active PIDs for this CWD
            pids = cwd_to_pids.get(cwd, [])
            num_sessions = max(1, len(pids))

            for idx, jf in enumerate(jsonl_files[:num_sessions]):
                if jf in active_files:
                    continue
                active_files.add(jf)

                pid = pids[idx] if idx < len(pids) else 0

                # Get or create accumulator
                accum = self._accums.get(jf)
                if accum is None:
                    accum = _SessionAccum(jf, cwd, pid=pid)
                    self._accums[jf] = accum
                else:
                    # Update pid in case it changed across refreshes
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

    def _build_cwd_map(self) -> None:
        """Map real CWDs to PROJECTS_DIR subdirectories by reading JSONL headers."""
        self._cwd_map.clear()
        if not PROJECTS_DIR.exists():
            return
        for project_dir in PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            try:
                jsonl_files = sorted(
                    project_dir.glob("*.jsonl"),
                    key=lambda f: f.stat().st_mtime,
                    reverse=True,
                )
            except OSError:
                continue
            if not jsonl_files:
                continue
            real_cwd = self._read_cwd(jsonl_files[0])
            if real_cwd:
                self._cwd_map[real_cwd] = project_dir
        self._cwd_map_built = True

    @staticmethod
    def _read_cwd(jsonl_file: Path) -> str:
        """Extract the cwd field from the first few lines of a JSONL file."""
        try:
            with open(jsonl_file) as f:
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
        except OSError:
            pass
        return ""


# ── Collector implementation ─────────────────────────────────────────────


class ClaudeCodeCollector(BaseCollector):
    """Collector for Claude Code agent data.

    - Live sessions: process detection + incremental JSONL parsing
    - History sync: stats-cache.json, sessions-index.json, and JSONL token data
    """

    agent_type = "claude_code"

    def __init__(self) -> None:
        self._monitor = _LiveMonitor()

    def get_live_sessions(self) -> list[LiveSession]:
        """Return currently active Claude Code sessions."""
        return self._monitor.refresh()

    def sync_history(self, db) -> None:
        """Sync Claude Code history into the database.

        Parses three data sources:
        1. ``stats-cache.json`` -- daily_stats and model_daily_usage
        2. ``sessions-index.json`` -- session metadata (per project)
        3. ``.jsonl`` files -- per-session token data (incremental via sync_state)
        """
        self._sync_sessions_index(db)
        self._sync_jsonl_tokens(db)
        db.commit()

    # ── sessions-index.json ──────────────────────────────────────

    def _sync_sessions_index(self, db) -> None:
        """Parse all sessions-index.json files under PROJECTS_DIR."""
        if not PROJECTS_DIR.exists():
            return

        for index_file in PROJECTS_DIR.glob("*/sessions-index.json"):
            try:
                data = json.loads(index_file.read_text())
            except (json.JSONDecodeError, OSError):
                continue

            for entry in data.get("entries", []):
                session_id = entry.get("sessionId", "")
                if not session_id:
                    continue

                created = entry.get("created", "")
                modified = entry.get("modified", "")

                db.upsert_session(
                    session_id,
                    self.agent_type,
                    project_path=entry.get("projectPath", ""),
                    git_branch=entry.get("gitBranch", ""),
                    message_count=entry.get("messageCount", 0),
                    started_at=created,
                    ended_at=modified,
                    summary=entry.get("summary", ""),
                )

    # ── JSONL token scanning ─────────────────────────────────────

    def _sync_jsonl_tokens(self, db) -> None:
        """Scan .jsonl files for per-session token data.

        Uses db sync_state to track which files/offsets have already been
        processed, making incremental re-syncs cheap.
        """
        if not PROJECTS_DIR.exists():
            return

        sync_prefix = "cc_jsonl:v3:"

        for project_dir in PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue

            try:
                jsonl_files = list(project_dir.rglob("*.jsonl"))
            except OSError:
                continue

            for jsonl_file in jsonl_files:
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

                # Build an accumulator starting from the previous offset
                accum = _SessionAccum(jsonl_file, project_path=str(project_dir))
                # First pass: read everything from scratch to get full picture
                # (we need totals, not deltas, for upsert)
                accum.read_new_lines()

                if accum.user_turns == 0:
                    # Mark as processed even if empty
                    db.set_sync_state(sync_key, _sync_state_value(file_size, mtime_ns))
                    continue

                usage_rows = accum.usage_bucket_rows()
                cost = _usage_rows_cost(usage_rows)

                # Read the cwd from the JSONL to get the real project path
                real_cwd = _LiveMonitor._read_cwd(jsonl_file)
                project_path = real_cwd if real_cwd else str(project_dir)
                session_id = _session_id_for_jsonl(project_dir, jsonl_file, accum.session_id)

                db.upsert_session(
                    session_id,
                    self.agent_type,
                    project_path=project_path,
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


def _usage_rows_cost(rows: list[dict]) -> float:
    """Estimate cost using each bucket's own model."""
    return sum(
        estimate_cost(
            row.get("model") or "",
            input_tokens=int(row.get("input_tokens") or 0),
            output_tokens=int(row.get("output_tokens") or 0),
            cache_read_tokens=int(row.get("cache_read_tokens") or 0),
            cache_creation_tokens=int(row.get("cache_creation_tokens") or 0),
        )
        for row in rows
    )


def _local_bucket(ts: str) -> tuple[str, int]:
    """Return local (date, hour) for an ISO timestamp."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%Y-%m-%d"), dt.hour
    except (ValueError, TypeError):
        return (ts[:10], 0) if len(ts) >= 10 else ("", 0)
