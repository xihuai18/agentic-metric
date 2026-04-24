"""Codex CLI collector: parse session JSONL files + live process monitoring."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from ..config import CODEX_SESSIONS_DIR
from ..models import LiveSession
from ..pricing import estimate_cost
from . import BaseCollector
from ._process import get_running_cwds


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
        "first_ts",
        "last_ts",
        "first_prompt",
        "last_prompt",
        "git_branch",
        "model",
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
        self.first_ts = ""
        self.last_ts = ""
        self.first_prompt = ""
        self.last_prompt = ""
        self.git_branch = ""
        self.model = ""

    def read_new_lines(self) -> None:
        """Read only bytes appended since last call.

        If the file shrank (truncated or replaced), reset state and re-parse
        from offset 0 — otherwise we'd silently miss data.
        """
        try:
            size = self.file_path.stat().st_size
        except OSError:
            return
        if size == self.offset:
            return
        if size < self.offset:
            # File was truncated or replaced; reset accumulator-level state
            # that comes from parsing, but keep identity fields.
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
        try:
            with open(self.file_path, "rb") as f:
                f.seek(self.offset)
                new_data = f.read()
            self.offset = size
        except OSError:
            return

        for raw_line in new_data.split(b"\n"):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                entry = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            self._process_entry(entry)

    def _process_entry(self, entry: dict) -> None:
        ts = entry.get("timestamp", "")
        if ts:
            if not self.first_ts:
                self.first_ts = ts
            self.last_ts = ts

        entry_type = entry.get("type", "")

        if entry_type == "session_meta":
            payload = entry.get("payload", {})
            if not self.session_id:
                self.session_id = payload.get("id", "")
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

        elif entry_type == "event_msg":
            self._process_event_msg(entry.get("payload", {}))

    def _process_event_msg(self, payload: dict) -> None:
        msg_type = payload.get("type", "")

        if msg_type == "user_message":
            self.user_turns += 1
            self.message_count += 1
            text = payload.get("message", "")
            if isinstance(text, str) and text.strip():
                clean = text.strip()[:80]
                if not self.first_prompt:
                    self.first_prompt = clean
                self.last_prompt = clean

        elif msg_type == "agent_message":
            self.message_count += 1

        elif msg_type == "token_count":
            info = payload.get("info")
            if not info:
                return
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
            if out is not None:
                self.output_tokens = out
            if raw_input is not None:
                self.raw_input_tokens = raw_input
            if cached is not None:
                self.cache_read = cached
            if raw_input is not None or cached is not None:
                self.input_tokens = max(self.raw_input_tokens - self.cache_read, 0)

    def to_live_session(self) -> LiveSession:
        return LiveSession(
            session_id=self.session_id or self.file_path.stem,
            agent_type="codex",
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

        if not candidate_files:
            return []

        jsonl_files = sorted(candidate_files, key=lambda f: candidate_files[f], reverse=True)

        cwd_to_pids: dict[str, list[int]] = {}
        for pid, cwd in pid_cwds.items():
            cwd_to_pids.setdefault(cwd, []).append(pid)

        cwd_to_files: dict[str, list[Path]] = {}
        for jsonl_file in jsonl_files:
            cwd = self._read_cwd(jsonl_file)
            if not cwd or cwd not in cwd_to_pids:
                continue
            cwd_to_files.setdefault(cwd, []).append(jsonl_file)

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
                    accum = _SessionAccum(jsonl_file, cwd, pid=pid)
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

        sync_prefix = "codex_jsonl:"

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

            cost = estimate_cost(
                accum.model,
                input_tokens=accum.input_tokens,
                output_tokens=accum.output_tokens,
                cache_read_tokens=accum.cache_read,
                cache_creation_tokens=accum.cache_create,
            )

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
