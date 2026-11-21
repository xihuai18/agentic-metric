"""SSH-backed collector wrappers for remote agent history."""

from __future__ import annotations

from concurrent.futures import CancelledError, FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextvars import copy_context
from dataclasses import dataclass
from hashlib import sha1
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shlex
import shutil
import signal
import stat
import subprocess
import tarfile
from threading import BoundedSemaphore, Event
import time

from ..config import DATA_DIR, RemoteSpec
from . import BaseCollector, check_sync_cancelled, current_sync_cancel_event
from .claude_code import ClaudeCodeCollector
from .codex import CodexCollector

# Stale archives keep a short-lived local copy of files that disappeared from
# a remote; they are never parsed again, so age them out quickly to bound growth.
_STALE_RETENTION_DAYS = 3

# Cold mirrored Claude Code JSONL files older than this age whose metrics and
# replay checkpoints are already stored in SQLite are evicted from disk while
# their manifest/sync metadata stays recorded so unchanged files are not re-fetched.
_COLD_EVICT_AGE_DAYS = 7

# Download budget per ssh/tar round trip, measured in remote (uncompressed)
# bytes. Small enough that one batch still fits in the default 30s ssh timeout
# on a slow link, large enough that a multi-GB mirror needs tens — not
# thousands — of round trips.
_DOWNLOAD_BATCH_BYTES = 128 * 1024 * 1024
_DOWNLOAD_BATCH_FILES = 500

# How many unsplittable (single-file) batches may be skipped before the whole
# download is treated as failing. Keeps one oversized file from blocking the
# mirror, without spending one timeout per batch when the remote is down.
_MAX_SKIPPED_BATCHES = 3
_RSYNC_BATCH_WORKERS = 4
_RSYNC_TRANSFER_SLOTS = BoundedSemaphore(_RSYNC_BATCH_WORKERS)

# Exit 24 may omit files; only confirmed deliveries advance the manifest.
_RSYNC_VANISHED_EXIT = 24

# Openrsync cannot protect remote arguments, so unsafe paths stay on tar.
_RSYNC_BASE_RE = re.compile(r"^/[A-Za-z0-9_.+=,/-]*$")


@dataclass(frozen=True)
class RemoteSyncTarget:
    """One remote agent root to mirror into the local cache before parsing."""

    remote: RemoteSpec
    agent_type: str
    remote_root: str
    provider: str
    index: int
    account_key: str = ""
    auto_provider: bool = True
    provider_rules: tuple[tuple[str, str], ...] = ()

    @property
    def child_dir(self) -> str:
        return "sessions" if self.agent_type == "codex" else "projects"

    @property
    def source_root(self) -> str:
        raw = self.remote_root.rstrip("/") or self.remote_root
        path = PurePosixPath(raw)
        if path.name == self.child_dir:
            parent = str(path.parent)
            return parent if parent != "." else "."
        return raw

    @property
    def source_child(self) -> str:
        return self.child_dir

    @property
    def label(self) -> str:
        if self.remote.name:
            return self.remote.name
        dest = _ssh_destination(self.remote)
        return f"{dest}:{self.remote.port}" if self.remote.port else dest

    @property
    def data_root(self) -> str:
        return f"ssh://{self.label}/{self.source_root}"


class RemoteHistoryCollector(BaseCollector):
    """Download one remote data root via SSH, then parse it with a local collector."""

    def __init__(self, target: RemoteSyncTarget) -> None:
        self.target = target
        self.provider = target.provider
        self.data_root = target.data_root
        self._agent_type = target.agent_type
        self.last_error = ""
        # (found, cache_changed, manifest_digest, complete) from prepare_cache()
        # awaiting its sync_history(); None when no mirror result is pending.
        self._prepared: tuple[bool, bool, str, bool] | None = None

        cache_root = _cache_root_for(target)
        if target.agent_type == "codex":
            self._inner = CodexCollector(
                sessions_dir=cache_root / "sessions",
                provider=self.provider,
                data_root=self.data_root,
                account_key=target.account_key,
            )
        elif target.agent_type == "claude_code":
            self._inner = ClaudeCodeCollector(
                projects_dir=cache_root / "projects",
                provider=self.provider,
                data_root=self.data_root,
                account_key=target.account_key,
                auto_provider=target.auto_provider,
                provider_rules=target.provider_rules,
            )
        else:
            raise ValueError(f"unsupported remote agent type: {target.agent_type}")

    @property
    def agent_type(self) -> str:
        return self._agent_type

    @property
    def auto_provider(self) -> bool:
        return self.target.agent_type == "claude_code" and self.target.auto_provider

    @property
    def provider_rules(self) -> tuple[tuple[str, str], ...]:
        return self.target.provider_rules if self.target.agent_type == "claude_code" else ()

    @property
    def sync_state_prefix(self) -> str:
        return self._inner.sync_state_prefix

    def prepare_cache(self) -> None:
        """Mirror the remote root into the local cache (no DB access).

        Network-only, so a registry can run one prepare per remote target in
        parallel before the serial DB parsing phase. Archived remotes never
        open SSH connections and reuse their local cache snapshot when present.
        """
        if self.target.remote.archived:
            self._prepared = _read_archived_cache(self.target)
            self.last_error = ""
            return
        try:
            self._prepared = _sync_target_to_cache(self.target)
            if not self._prepared[0]:
                self.last_error = (
                    f"remote path not found: "
                    f"{self.target.source_root}/{self.target.source_child}"
                )
            elif not self._prepared[3]:
                self.last_error = f"remote mirror incomplete: {self.data_root}"
            else:
                self.last_error = ""
        except CancelledError:
            self._prepared = None
            raise
        except Exception as exc:
            self._prepared = (False, False, "", False)
            self.last_error = str(exc)

    def sync_history(self, db) -> None:
        check_sync_cancelled()
        prepared = self._prepared
        self._prepared = None
        if prepared is None:
            self.prepare_cache()
            prepared = self._prepared or (False, False, "", False)
            self._prepared = None
        found, cache_changed, manifest_digest, complete = prepared
        if not found:
            return
        archived = self.target.remote.archived
        ready_key = self._ready_state_key()
        if complete and not cache_changed and db.get_sync_state(ready_key) == manifest_digest:
            # The mirror is byte-identical to the last fully parsed state, so
            # the per-file scan of the whole cache tree can be skipped.
            return
        files = None
        root = _cache_root_for(self.target)
        manifest = None
        if complete:
            manifest = _load_manifest(root)
            if manifest is not None and _manifest_digest(manifest) == manifest_digest:
                paths = [_safe_member_path(rel) for rel in manifest]
                if all(path is not None for path in paths):
                    files = [root / self.target.source_child / path for path in paths if path is not None]
        evicted_map = _load_evicted(root) if self.agent_type == "claude_code" else {}
        evicted_files: dict[Path, dict[str, str]] = {}
        evicted_ids: set[str] = set()
        if evicted_map:
            active_root = root / self.target.source_child
            for rel, meta in evicted_map.items():
                member = _safe_member_path(rel)
                if member is None:
                    continue
                evicted_files[active_root / member] = meta
                if meta.get("session_id"):
                    evicted_ids.add(meta["session_id"])
        if not archived and cache_changed and complete and self.agent_type == "claude_code":
            _purge_removed_remote_sessions(db, self.target, files=files, evicted_ids=evicted_ids)
        allow_reconcile = complete and not archived
        if isinstance(self._inner, CodexCollector):
            self._inner.sync_history(
                db,
                reconcile=allow_reconcile,
                files=files,
                preserve_missing_parents=not allow_reconcile,
            )
        else:
            self._inner.sync_history(
                db,
                reconcile=allow_reconcile,
                files=files,
                evicted_files=evicted_files or None,
            )
            rehydrate = getattr(self._inner, "evicted_rehydrate_needed", set())
            if rehydrate and evicted_map:
                active_root = root / self.target.source_child
                changed_evicted = False
                for path in rehydrate:
                    try:
                        rel = str(PurePosixPath(*path.relative_to(active_root).parts))
                    except ValueError:
                        continue
                    if evicted_map.pop(rel, None) is not None:
                        changed_evicted = True
                if changed_evicted:
                    _save_evicted(root, evicted_map)
        if complete and self._inner.last_sync_complete:
            db.set_sync_state(ready_key, manifest_digest)
            if not archived and self.agent_type == "claude_code" and manifest is not None:
                _evict_cold_cache_files(
                    root,
                    self.target,
                    self._inner,
                    db,
                    manifest,
                )
        else:
            db.conn.execute("DELETE FROM sync_state WHERE key = ?", (ready_key,))
            db.commit()

    def _ready_state_key(self) -> str:
        """Sync-state key marking a fully parsed mirror state.

        Shares the collector sync-state prefixes so pricing-fingerprint
        migrations and history rebuilds invalidate it together with the
        per-file parse states.
        """
        prefix = self._inner.sync_state_prefix
        return f"{prefix}:remote_ready:v2:{_cache_root_for(self.target).name}"


def _cache_root_for(target: RemoteSyncTarget) -> Path:
    raw = "|".join(
        (
            target.remote.host,
            target.remote.name,
            target.remote.user,
            str(target.remote.port or ""),
            " ".join(target.remote.ssh_options),
            target.agent_type,
            str(target.index),
            target.remote_root,
            target.provider,
        )
    )
    digest = sha1(raw.encode("utf-8")).hexdigest()[:16]
    return DATA_DIR / "remote-cache" / digest


def _purge_removed_remote_sessions(
    db,
    target: RemoteSyncTarget,
    *,
    files: list[Path] | None = None,
    evicted_ids: set[str] | None = None,
) -> None:
    """Drop derived DB rows whose remote JSONL no longer exists in active cache."""
    active_ids = (
        _active_claude_session_ids(_cache_root_for(target) / target.source_child, files=files)
        if target.agent_type == "claude_code" else _active_remote_session_ids(target)
    )
    if evicted_ids:
        active_ids |= evicted_ids
    if not active_ids:
        return
    dynamic = target.agent_type == "claude_code" and (
        target.auto_provider or bool(target.provider_rules)
    )
    if dynamic:
        rows = db.conn.execute(
            """SELECT session_id, provider
               FROM sessions
               WHERE session_id != ''
                 AND agent_type = ?
                 AND data_root = ?""",
            (target.agent_type, target.data_root),
        ).fetchall()
    else:
        rows = db.conn.execute(
            """SELECT session_id, provider
               FROM sessions
               WHERE session_id != ''
                 AND agent_type = ?
                 AND provider = ?
                 AND data_root = ?""",
            (target.agent_type, target.provider, target.data_root),
        ).fetchall()
    for row in rows:
        if row["session_id"] in active_ids:
            continue
        db.delete_session(
            row["session_id"],
            target.agent_type,
            provider=str(row["provider"] or ""),
            data_root=target.data_root,
        )
    db.commit()


def _active_remote_session_ids(target: RemoteSyncTarget) -> set[str]:
    cache_root = _cache_root_for(target)
    active_root = cache_root / target.source_child
    if not active_root.exists():
        return set()
    if target.agent_type == "codex":
        return _active_codex_session_ids(active_root, target.provider)
    if target.agent_type == "claude_code":
        return _active_claude_session_ids(active_root)
    return set()


def _active_codex_session_ids(active_root: Path, provider: str) -> set[str]:
    ids: set[str] = set()
    for path in active_root.rglob("rollout-*.jsonl"):
        if not path.is_file():
            continue
        session_id, observed_provider = _read_codex_session_identity(path)
        if provider and observed_provider and observed_provider != provider:
            continue
        ids.add(session_id or path.stem)
    return ids


def _read_codex_session_identity(path: Path) -> tuple[str, str]:
    try:
        with path.open(encoding="utf-8") as handle:
            for idx, line in enumerate(handle):
                if idx > 20:
                    break
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "session_meta":
                    continue
                payload = entry.get("payload", {})
                if not isinstance(payload, dict):
                    return ("", "")
                return (
                    str(payload.get("id") or ""),
                    str(payload.get("model_provider") or "").strip(),
                )
    except (OSError, UnicodeDecodeError):
        return ("", "")
    return ("", "")


def _active_claude_session_ids(active_root: Path, *, files: list[Path] | None = None) -> set[str]:
    ids: set[str] = set()
    if files is not None:
        for path in files:
            parts = path.relative_to(active_root).parts
            if len(parts) >= 2 and path.suffix == ".jsonl" and path.is_file():
                ids.add(_claude_session_id_for_jsonl(active_root / parts[0], path))
        return ids
    try:
        project_dirs = [path for path in active_root.iterdir() if path.is_dir()]
    except OSError:
        return ids
    for project_dir in project_dirs:
        try:
            jsonl_files = list(project_dir.rglob("*.jsonl"))
        except OSError:
            continue
        for path in jsonl_files:
            if path.is_file():
                ids.add(_claude_session_id_for_jsonl(project_dir, path))
    return ids


def _claude_session_id_for_jsonl(project_dir: Path, jsonl_file: Path) -> str:
    try:
        rel = jsonl_file.relative_to(project_dir)
    except ValueError:
        return jsonl_file.stem
    parts = rel.parts
    if len(parts) >= 3 and parts[-2] == "subagents":
        return f"{parts[-3]}:{jsonl_file.stem}"
    return jsonl_file.stem


def _ssh_destination(remote: RemoteSpec) -> str:
    return f"{remote.user}@{remote.host}" if remote.user else remote.host


def _ssh_command(remote: RemoteSpec, remote_cmd: str) -> list[str]:
    cmd = ["ssh"]
    if remote.port:
        cmd.extend(["-p", str(remote.port)])
    cmd.extend(remote.ssh_options)
    cmd.append(_ssh_destination(remote))
    cmd.append(remote_cmd)
    return cmd


def _remote_base_script(remote_root: str, subdir: str) -> str:
    root = shlex.quote(remote_root)
    child = shlex.quote(subdir)
    return (
        "set -e; "
        f"root={root}; child={child}; "
        'case "$root" in '
        '"~") root="$HOME" ;; '
        '"~/"*) root="$HOME/${root#\\~/}" ;; '
        "esac; "
        'base="$root/$child"; '
    )


def _manifest_command(target: RemoteSyncTarget) -> str:
    if target.agent_type == "codex":
        find_expr = "-name 'rollout-*.jsonl'"
    else:
        find_expr = "\\( -name '*.jsonl' -o -name 'sessions-index.json' \\)"
    # GNU find prints size/mtime natively (one process for the whole tree);
    # BSD find lacks -printf and falls back to one stat per file batch. The
    # local parser truncates fractional %T@ mtimes to whole seconds so both
    # formats produce identical manifest values.
    return (
        _remote_base_script(target.source_root, target.source_child)
        + 'if [ ! -d "$base" ]; then printf "MISSING\\0"; exit 0; fi; '
        + 'printf "OK\\0"; cd "$base"; '
        + 'if command -v rsync >/dev/null 2>&1; then printf "RSYNC\\0%s\\0" "$PWD"; fi; '
        + "if find . -maxdepth 0 -printf '' 2>/dev/null; then "
        + f"find . -type f {find_expr} -printf '%s\\t%T@\\t%p\\0'; "
        + f"else find . -type f {find_expr} {_manifest_stat_exec()}; fi"
    )


def _manifest_stat_exec() -> str:
    stat_script = (
        "for path do "
        "if stat --printf '%s\\t%Y\\t%n\\0' \"$path\" 2>/dev/null; then "
        ":; "
        "elif out=$(stat -f '%z\\t%m\\t%N' \"$path\" 2>/dev/null); then "
        "printf '%s\\0' \"$out\"; "
        "else exit 1; "
        "fi; "
        "done"
    )
    return f"-exec sh -c {shlex.quote(stat_script)} sh {{}} +"


def _download_command(target: RemoteSyncTarget) -> str:
    return (
        _remote_base_script(target.source_root, target.source_child)
        + 'if [ -d "$base" ]; then cd "$base"; tar -czf - --null -T -; fi'
    )


def _read_archived_cache(target: RemoteSyncTarget) -> tuple[bool, bool, str, bool]:
    """Read local mirror metadata for an archived remote without touching SSH."""
    cache_root = _cache_root_for(target)
    if not (cache_root / target.source_child).exists():
        return (False, False, "", False)
    manifest = _load_manifest(cache_root)
    complete = manifest is not None and not _manifest_is_incomplete(cache_root)
    digest = _manifest_digest(manifest) if manifest is not None else ""
    return (True, False, digest, complete)


def _sync_target_to_cache(target: RemoteSyncTarget) -> tuple[bool, bool, str, bool]:
    """Mirror one remote root and report whether its inventory is complete."""
    cache_root = _cache_root_for(target)
    cache_root.mkdir(parents=True, exist_ok=True)

    manifest, rsync_base = _read_remote_inventory(target)
    if manifest is None:
        return (False, False, "", False)

    loaded = _load_manifest(cache_root)
    # A partial manifest (left by a download that skipped files) is not
    # authoritative: treat it like a missing one so stale archiving and session
    # purging still run once the mirror completes.
    had_manifest = loaded is not None and not _manifest_is_incomplete(cache_root)
    previous = loaded or {}
    evicted = _load_evicted(cache_root) if target.agent_type == "claude_code" else {}
    changed = [
        rel for rel, meta in manifest.items()
        if previous.get(rel) != meta
        or (
            not (cache_root / target.source_child / rel).exists()
            and not _is_evicted_current(evicted, rel, meta)
        )
    ]
    if changed:
        missing = _download_changed_files(
            target, cache_root, manifest, previous, changed, rsync_base=rsync_base
        )
    else:
        missing = set()
    # A missing saved manifest means the cache state is unknown (first run or
    # cleared), so stray files must still be archived and sessions re-purged.
    cache_changed = bool(changed) or not had_manifest or previous != manifest
    # Files the remote listed but the download never delivered must stay out of
    # the saved manifest, or an outdated local copy would pass as current.
    saved_manifest = (
        manifest if not missing
        else {rel: meta for rel, meta in manifest.items() if rel not in missing}
    )
    if evicted:
        pruned_evicted = {
            rel: entry
            for rel, entry in evicted.items()
            if rel in saved_manifest
            and _is_evicted_current(evicted, rel, saved_manifest[rel])
            and not (cache_root / target.source_child / rel).exists()
        }
        if pruned_evicted != evicted:
            _save_evicted(cache_root, pruned_evicted)
    if cache_changed:
        # Stale archiving and session purging only matter when the mirror
        # actually changed; skipping them keeps unchanged remotes cheap. Stale
        # detection uses the full remote manifest so files that are merely
        # pending download are not archived.
        _archive_stale_cache_files(cache_root, target, manifest, previous=previous if had_manifest else None)
        if missing:
            _mark_manifest_incomplete(cache_root)
        _save_manifest(cache_root, saved_manifest)
    if not missing:
        # Every changed file was downloaded, so the saved manifest covers the
        # whole remote again.
        _clear_manifest_incomplete(cache_root)
    # Age out expired stale archives on every sync — an unchanged remote must
    # not keep them alive forever. The walk only touches .stale, so it's cheap.
    _prune_old_stale_archives(cache_root)
    return (True, cache_changed, _manifest_digest(saved_manifest), not missing)


def _download_batches(
    manifest: dict[str, dict[str, str]],
    changed: list[str],
) -> list[list[str]]:
    """Split changed files into transfer batches bounded by remote file size.

    A first-run mirror of a multi-GB remote cannot be streamed as one tar
    within ``remote.timeout``, and a timeout used to discard the whole download
    so the mirror never caught up. Batching keeps every ssh call small enough
    to finish inside the timeout. Sizes come from the remote manifest, so a
    single file larger than the batch budget still gets its own batch.
    """
    batches: list[list[str]] = []
    batch: list[str] = []
    batch_bytes = 0
    for rel in changed:
        try:
            size = max(0, int(manifest.get(rel, {}).get("size") or 0))
        except ValueError:
            size = 0
        if batch and (
            batch_bytes + size > _DOWNLOAD_BATCH_BYTES
            or len(batch) >= _DOWNLOAD_BATCH_FILES
        ):
            batches.append(batch)
            batch, batch_bytes = [], 0
        batch.append(rel)
        batch_bytes += size
    if batch:
        batches.append(batch)
    return batches


def _download_changed_files(
    target: RemoteSyncTarget,
    cache_root: Path,
    manifest: dict[str, dict[str, str]],
    previous: dict[str, dict[str, str]],
    changed: list[str],
    *,
    rsync_base: str | None = None,
) -> set[str]:
    """Download changed files batch by batch, persisting progress as it goes.

    Returns the files the remote listed but the download did not deliver, so the
    caller can keep them out of the saved manifest.

    The saved manifest is advanced after each extracted batch, so a failure
    part-way through (ssh timeout, dropped connection) leaves the already
    mirrored files recorded and the next sync only fetches the remainder.

    A batch holding a single file cannot be split any further, so one file that
    never transfers inside ``remote.timeout`` is skipped rather than allowed to
    starve every batch behind it. Wider failures — or too many single-file ones —
    mean the remote itself is unhealthy and stop the download immediately
    instead of spending one timeout per remaining batch.
    """
    progress = dict(previous)
    first_error: Exception | None = None
    skipped = 0
    missing: set[str] = set()

    def persist() -> None:
        # An empty manifest is not the same as no manifest: writing one would
        # turn "cache state unknown" into an authoritative "remote is empty"
        # and skip stale archiving and session purging on the next sync.
        if progress:
            # Mark before writing: a crash between the two writes must leave the
            # partial manifest flagged, never authoritative.
            _mark_manifest_incomplete(cache_root)
            _save_manifest(cache_root, progress)

    rsync_plan = _rsync_plan(target.remote, rsync_base) if rsync_base else None
    if rsync_plan is None:
        plan = [(batch, None) for batch in _download_batches(manifest, changed)]
    else:
        # Names rsync's itemized output cannot round-trip stay on the tar path.
        via_rsync = [rel for rel in changed if _rsync_name_ok(rel)]
        via_tar = [rel for rel in changed if not _rsync_name_ok(rel)]
        plan = [(batch, rsync_plan) for batch in _download_batches(manifest, via_rsync)]
        plan += [(batch, None) for batch in _download_batches(manifest, via_tar)]
        if via_rsync:
            (cache_root / target.source_child).mkdir(parents=True, exist_ok=True)

    def record(batch: list[str], written: set[str]) -> None:
        for rel in batch:
            if rel in written:
                progress[rel] = manifest[rel]
            else:
                progress.pop(rel, None)
                missing.add(rel)
        persist()

    def failed(batch: list[str], exc: RuntimeError) -> bool:
        nonlocal skipped, first_error
        for rel in batch:
            progress.pop(rel, None)
        if len(batch) == 1 and skipped < _MAX_SKIPPED_BATCHES:
            skipped += 1
            missing.update(batch)
            if first_error is None:
                first_error = exc
            return False
        return True

    stop = Event()

    def download_rsync(batch: list[str], transport: _RsyncPlan) -> set[str]:
        with _RSYNC_TRANSFER_SLOTS:
            check_sync_cancelled()
            if stop.is_set():
                raise CancelledError()
            proc = _run_command(
                _rsync_command(transport, cache_root, target.source_child),
                target.remote.timeout,
                "rsync",
                input_bytes="\0".join(batch).encode("utf-8") + b"\0",
                allowed_returncodes=(0, _RSYNC_VANISHED_EXIT),
            )
            return _rsync_delivered(proc.stdout, batch, cache_root / target.source_child)

    try:
        rsync_batches = iter(sorted(
            (batch for batch, transport in plan if transport is not None),
            key=lambda batch: sum(int(manifest[rel]["size"]) for rel in batch),
            reverse=True,
        ))
        if rsync_plan is not None:
            fatal_error: BaseException | None = None
            with ThreadPoolExecutor(max_workers=_RSYNC_BATCH_WORKERS) as pool:
                pending = {}
                try:
                    while True:
                        event = current_sync_cancel_event()
                        if event is not None and event.is_set():
                            stop.set()
                        while not stop.is_set() and len(pending) < _RSYNC_BATCH_WORKERS:
                            batch = next(rsync_batches, None)
                            if batch is None:
                                break
                            pending[pool.submit(copy_context().run, download_rsync, batch, rsync_plan)] = batch
                        if not pending:
                            break
                        done, _ = wait(pending, return_when=FIRST_COMPLETED)
                        for future in done:
                            batch = pending.pop(future)
                            try:
                                written = future.result()
                            except CancelledError:
                                continue
                            except RuntimeError as exc:
                                if failed(batch, exc):
                                    fatal_error = fatal_error or exc
                                    stop.set()
                            except BaseException as exc:
                                for rel in batch:
                                    progress.pop(rel, None)
                                fatal_error = fatal_error or exc
                                stop.set()
                            else:
                                record(batch, written)
                        if stop.is_set():
                            for future in pending:
                                future.cancel()
                    check_sync_cancelled()
                    if fatal_error is not None:
                        raise fatal_error
                finally:
                    stop.set()
                    for future in pending:
                        future.cancel()
        for batch, batch_rsync_plan in plan:
            check_sync_cancelled()
            if batch_rsync_plan is not None:
                continue
            payload = "\0".join(batch).encode("utf-8") + b"\0"
            # tar exits 1 (not fatal) when a file changes/shrinks while it is
            # being read — common when a remote session is live. The archive it
            # streamed is still valid, so accept exit 1 and extract it; only
            # exit >= 2 is a real failure. Without this, any active remote
            # session would make every sync discard the whole download and never
            # catch up.
            try:
                proc = _run_ssh(
                    target.remote,
                    _download_command(target),
                    input_bytes=payload,
                    allowed_returncodes=(0, 1),
                )
            except RuntimeError as exc:
                if not failed(batch, exc):
                    continue
                raise
            try:
                written = (
                    _extract_tarball(proc.stdout, cache_root / target.source_child)
                    if proc.stdout
                    else set()
                )
            except Exception:
                # A local extraction failure (full disk, unreadable archive) is
                # not something another batch can work around, so never skip it.
                for rel in batch:
                    progress.pop(rel, None)
                raise
            record(batch, written)
        if first_error is not None:
            # Report the skipped files as a failed sync; they stay "changed", so
            # the next run retries them while the rest of the mirror is current.
            raise first_error
    except BaseException:
        # Any exit other than full success leaves a partial mirror; record what
        # was fetched so the next run resumes instead of starting over.
        persist()
        raise
    return missing


def _manifest_digest(manifest: dict[str, dict[str, str]]) -> str:
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return sha1(raw.encode("utf-8")).hexdigest()


def _run_ssh(
    remote: RemoteSpec,
    remote_cmd: str,
    *,
    input_bytes: bytes | None = None,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess:
    return _run_command(
        _ssh_command(remote, remote_cmd),
        remote.timeout,
        "ssh",
        input_bytes=input_bytes,
        allowed_returncodes=allowed_returncodes,
    )


def _run_command(
    argv: list[str],
    timeout: float,
    label: str,
    *,
    input_bytes: bytes | None = None,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess:
    check_sync_cancelled()
    try:
        if current_sync_cancel_event() is None:
            proc = subprocess.run(
                argv,
                input=input_bytes,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
        else:
            proc = _run_cancellable_command(argv, input_bytes, timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{label} timed out after {timeout}s") from exc
    if proc.returncode not in allowed_returncodes:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(stderr or f"{label} exited with status {proc.returncode}")
    return proc


def _run_cancellable_command(argv: list[str], payload: bytes | None, timeout: float) -> subprocess.CompletedProcess:
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name != "nt",
    )
    deadline = time.monotonic() + timeout
    try:
        while True:
            check_sync_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            try:
                stdout, stderr = process.communicate(input=payload, timeout=min(0.1, remaining))
                return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                payload = None
    except BaseException:
        try:
            if os.name == "nt":
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        try:
            if os.name == "nt":
                if process.poll() is None:
                    process.kill()
            else:
                # The leader may exit before descendants that closed their pipes.
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise


@dataclass(frozen=True)
class _RsyncPlan:
    shell: str
    source: str


def _rsync_plan(remote: RemoteSpec, base: str) -> _RsyncPlan | None:
    if shutil.which("rsync") is None or not _RSYNC_BASE_RE.fullmatch(base):
        return None
    host, user = remote.host, remote.user
    if not host or any(ch in host + user for ch in "/\\ \t\r\n"):
        return None
    words = ["ssh"]
    if remote.port:
        words.extend(["-p", str(remote.port)])
    words.extend(remote.ssh_options)
    quoted: list[str] = []
    for word in words:
        if not word or any(ch in word for ch in "\r\n\0"):
            return None
        if any(ch in word for ch in " \t'\""):
            if '"' not in word:
                word = f'"{word}"'
            elif "'" not in word:
                word = f"'{word}'"
            else:
                return None
        quoted.append(word)
    host_spec = f"[{host}]" if ":" in host else host
    source = f"{user + '@' if user else ''}{host_spec}:{base.rstrip('/')}/"
    if source.startswith("-"):
        return None
    return _RsyncPlan(shell=" ".join(quoted), source=source)


def _rsync_command(plan: _RsyncPlan, cache_root: Path, child: str) -> list[str]:
    # Force comparison of same-size rewrites without disabling block deltas.
    return [
        "rsync", "-I", "--no-whole-file",
        "-z", "--compress-level=1",
        "--from0", "--files-from=-",
        "--8-bit-output", "--out-format=%i %n",
        "-e", plan.shell,
        "--", plan.source, f"{cache_root / child}/",
    ]


def _rsync_name_ok(rel: str) -> bool:
    """True when rsync can report this name back unescaped."""
    return rel.isprintable() and "\\" not in rel and rel == rel.strip()


def _rsync_delivered(stdout: bytes, batch: list[str], dest: Path) -> set[str]:
    # A skipped or vanished source can leave an old regular file behind.
    wanted = set(batch)
    delivered: set[str] = set()
    for line in stdout.decode("utf-8", errors="surrogateescape").split("\n"):
        flags, sep, name = line.partition(" ")
        if not sep or not flags.startswith(">f") or name not in wanted:
            continue
        try:
            if stat.S_ISREG((dest / name).lstat().st_mode):
                delivered.add(name)
        except OSError:
            continue
    return delivered


def _read_remote_inventory(
    target: RemoteSyncTarget,
) -> tuple[dict[str, dict[str, str]] | None, str | None]:
    proc = _run_ssh(target.remote, _manifest_command(target))
    parts = proc.stdout.split(b"\0")
    if not parts or parts[0] == b"MISSING":
        return None, None
    if parts[0] != b"OK":
        raise RuntimeError("unexpected remote manifest response")

    rsync_base: str | None = None
    entries = parts[1:]
    if len(entries) >= 2 and entries[0] == b"RSYNC":
        rsync_base = entries[1].decode("utf-8", errors="replace")
        entries = entries[2:]

    manifest: dict[str, dict[str, str]] = {}
    for item in entries:
        if not item:
            continue
        try:
            size, mtime, name = item.decode("utf-8", errors="surrogateescape").split("\t", 2)
        except ValueError:
            continue
        rel = name[2:] if name.startswith("./") else name
        safe = _safe_member_path(rel)
        if safe is None:
            continue
        # GNU find %T@ is fractional; stat %Y/%m are whole seconds. Truncate
        # so switching manifest sources never re-flags every file as changed.
        manifest[str(PurePosixPath(*safe.parts))] = {
            "size": size,
            "mtime": mtime.split(".", 1)[0],
        }
    return manifest, rsync_base


def _manifest_path(cache_root: Path) -> Path:
    return cache_root / ".remote-manifest.json"


def _incomplete_marker_path(cache_root: Path) -> Path:
    return cache_root / ".remote-manifest.incomplete"


def _mark_manifest_incomplete(cache_root: Path) -> None:
    """Record that the saved manifest only covers part of the remote."""
    _incomplete_marker_path(cache_root).write_text("", encoding="utf-8")


def _manifest_is_incomplete(cache_root: Path) -> bool:
    return _incomplete_marker_path(cache_root).exists()


def _clear_manifest_incomplete(cache_root: Path) -> None:
    try:
        _incomplete_marker_path(cache_root).unlink()
    except OSError:
        pass


def _load_manifest(cache_root: Path) -> dict[str, dict[str, str]] | None:
    """Return the saved manifest, or None when missing/unreadable.

    ``None`` (unknown previous state) must not compare equal to an empty
    remote manifest, otherwise stale archiving and session purging would be
    skipped with leftover cache files still on disk.
    """
    try:
        data = json.loads(_manifest_path(cache_root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _save_manifest(cache_root: Path, manifest: dict[str, dict[str, str]]) -> None:
    _manifest_path(cache_root).write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _evicted_path(cache_root: Path) -> Path:
    return cache_root / ".remote-evicted.json"


def _load_evicted(cache_root: Path) -> dict[str, dict[str, str]]:
    path = _evicted_path(cache_root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(k): {str(mk): str(mv) for mk, mv in v.items()}
        for k, v in data.items()
        if isinstance(v, dict)
    }


def _save_evicted(cache_root: Path, evicted: dict[str, dict[str, str]]) -> None:
    path = _evicted_path(cache_root)
    if not evicted:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(evicted, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def _is_evicted_current(
    evicted: dict[str, dict[str, str]],
    rel: str,
    meta: dict[str, str],
) -> bool:
    entry = evicted.get(rel)
    if not entry:
        return False
    return (
        entry.get("size") == meta.get("size")
        and entry.get("mtime") == meta.get("mtime")
        and bool(entry.get("sync_state"))
    )


def _evict_cold_cache_files(
    cache_root: Path,
    target: RemoteSyncTarget,
    collector: ClaudeCodeCollector,
    db,
    manifest: dict[str, dict[str, str]],
    *,
    min_age_days: int = _COLD_EVICT_AGE_DAYS,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Evict cold already-ingested remote Claude Code JSONL files from disk.

    Keeps manifest and sync_state metadata so unchanged files are not
    re-downloaded and session reconciliation stays accurate.
    Returns (evicted_count, reclaimed_bytes).
    """
    if target.agent_type != "claude_code" or target.remote.archived:
        return (0, 0)
    active_root = cache_root / target.source_child
    if not active_root.exists():
        return (0, 0)
    cutoff = time.time() - max(0, min_age_days) * 86400
    sync_prefix = f"{collector.sync_state_prefix}:"
    assistant_ids_prefix = f"{collector.sync_state_prefix}:assistant_ids:"
    evicted = _load_evicted(cache_root)
    to_unlink: list[tuple[Path, int]] = []
    updated_evicted = dict(evicted)

    for rel, meta in manifest.items():
        if not rel.endswith(".jsonl"):
            continue
        member = _safe_member_path(rel)
        if member is None or len(member.parts) < 2:
            continue
        try:
            remote_mtime = float(meta.get("mtime") or 0)
        except ValueError:
            continue
        if remote_mtime <= 0 or remote_mtime >= cutoff:
            continue
        path = active_root / member
        if not path.is_file():
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        state = f"{st.st_size}:{st.st_mtime_ns}"
        sync_key = f"{sync_prefix}{path}"
        if db.get_sync_state(sync_key) != state:
            continue
        if db.get_sync_state(f"{assistant_ids_prefix}{path}") is None:
            continue
        project_dir = active_root / member.parts[0]
        session_id = _claude_session_id_for_jsonl(project_dir, path)
        updated_evicted[rel] = {
            "size": str(meta.get("size", "")),
            "mtime": str(meta.get("mtime", "")),
            "session_id": session_id,
            "sync_state": state,
        }
        to_unlink.append((path, st.st_size))

    if not to_unlink:
        return (0, 0)
    reclaimed = sum(size for _, size in to_unlink)
    if dry_run:
        return (len(to_unlink), reclaimed)

    _save_evicted(cache_root, updated_evicted)
    unlinked_count = 0
    unlinked_bytes = 0
    touched_parents: set[Path] = set()
    for path, size in to_unlink:
        try:
            path.unlink()
            unlinked_count += 1
            unlinked_bytes += size
            touched_parents.add(path.parent)
        except OSError:
            continue
    for parent in sorted(touched_parents, key=lambda p: len(p.parts), reverse=True):
        cur = parent
        while cur != active_root and active_root in cur.parents:
            try:
                if not any(cur.iterdir()):
                    cur.rmdir()
                    cur = cur.parent
                    continue
            except OSError:
                pass
            break
    return (unlinked_count, unlinked_bytes)


def evict_cold_remote_cache(
    db,
    *,
    min_age_days: int = _COLD_EVICT_AGE_DAYS,
    dry_run: bool = False,
) -> dict:
    """Evict cold already-ingested remote JSONL files across all active mirrors."""
    targets = _configured_targets()
    total_files = 0
    total_bytes = 0
    details: list[dict] = []
    for digest, target in targets.items():
        if target.agent_type != "claude_code" or target.remote.archived:
            continue
        cache_root = _cache_root_for(target)
        manifest = _load_manifest(cache_root)
        if not manifest:
            continue
        collector = ClaudeCodeCollector(
            projects_dir=cache_root / target.source_child,
            provider=target.provider,
            data_root=target.data_root,
            account_key=target.account_key,
            auto_provider=target.auto_provider,
            provider_rules=target.provider_rules,
        )
        count, reclaimed = _evict_cold_cache_files(
            cache_root,
            target,
            collector,
            db,
            manifest,
            min_age_days=min_age_days,
            dry_run=dry_run,
        )
        if count:
            total_files += count
            total_bytes += reclaimed
            details.append({
                "path": cache_root,
                "owner": f"{target.label}/{target.agent_type}/{target.remote_root}",
                "files": count,
                "bytes": reclaimed,
            })
    return {
        "evicted_files": total_files,
        "reclaimed_bytes": total_bytes,
        "targets": details,
    }


def _archive_stale_cache_files(
    cache_root: Path,
    target: RemoteSyncTarget,
    manifest: dict[str, dict[str, str]],
    *,
    previous: dict[str, dict[str, str]] | None = None,
) -> None:
    active_root = cache_root / target.source_child
    if not active_root.exists():
        return

    patterns = ["rollout-*.jsonl"] if target.agent_type == "codex" else ["*.jsonl", "sessions-index.json"]
    if previous is None:
        candidates = (path for pattern in patterns for path in active_root.rglob(pattern))
    else:
        removed = (_safe_member_path(rel) for rel in previous.keys() - manifest.keys())
        candidates = (active_root / path for path in removed if path is not None)
    stale_root = cache_root / ".stale" / target.source_child
    for path in candidates:
        check_sync_cancelled()
        if not path.is_file():
            continue
        rel = str(PurePosixPath(*path.relative_to(active_root).parts))
        if rel in manifest:
            continue
        archive_path = _next_archive_path(stale_root / rel)
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        path.replace(archive_path)
        # Retention starts at archive time, not the original log's mtime.
        archive_path.touch()


def _prune_old_stale_archives(cache_root: Path, retention_days: int = _STALE_RETENTION_DAYS) -> int:
    """Delete stale archives older than the retention window.

    Returns the number of bytes reclaimed.
    """
    stale_root = cache_root / ".stale"
    if not stale_root.exists():
        return 0
    cutoff = time.time() - retention_days * 86400
    reclaimed = 0
    for path in sorted(stale_root.rglob("*"), reverse=True):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                reclaimed += path.stat().st_size
                path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        except OSError:
            continue
    try:
        if stale_root.exists() and not any(stale_root.iterdir()):
            stale_root.rmdir()
    except OSError:
        pass
    return reclaimed


# ── Cache accounting and pruning ─────────────────────────────────────────


def _remotes_config_readable() -> bool:
    """True when the config file exists, parses, and has a sane shape.

    A readable config with zero remotes is a deliberate state (the user
    removed them), so orphan detection may trust it. A missing or corrupt
    file — or one whose top level / ``remotes`` key has the wrong type,
    which ``get_remote_specs`` also treats as "no remotes" — is
    indistinguishable from a mistake and must not be trusted.
    """
    from ..config import CONFIG_FILE

    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    remotes = data.get("remotes")
    return remotes is None or isinstance(remotes, list)


def _configured_targets() -> dict[str, RemoteSyncTarget]:
    """Map active cache-dir digests to their configured remote targets."""
    from ..config import get_remote_specs

    targets: dict[str, RemoteSyncTarget] = {}
    for remote in get_remote_specs():
        for agent_type in ("claude_code", "codex"):
            for index, root in enumerate((remote.collectors or {}).get(agent_type, [])):
                target = RemoteSyncTarget(
                    remote=remote,
                    agent_type=agent_type,
                    remote_root=root.path,
                    provider=root.provider,
                    index=index,
                    account_key=root.account_key,
                    auto_provider=root.auto_provider,
                    provider_rules=root.provider_rules,
                )
                targets[_cache_root_for(target).name] = target
    return targets


def _tree_bytes(root: Path) -> int:
    total = 0
    try:
        for path in root.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


def remote_cache_report(*, include_active_sizes: bool = False) -> dict:
    """Describe the remote mirror cache: active mirrors, stale data, orphans.

    Orphans are cache dirs whose digest no longer matches any configured
    remote target (left behind by config changes); together with stale
    archives they are safe to reclaim. Active mirror sizes require a full
    tree walk, so they are only computed on request.
    """
    cache_dir = DATA_DIR / "remote-cache"
    targets = _configured_targets()
    # An empty target set with a readable config means the user removed the
    # remotes — old mirrors really are orphans. But when the config file is
    # missing or corrupt, that state is indistinguishable from a transient
    # failure; calling every mirror an orphan then would let prune delete all
    # (expensively re-downloadable) mirrors, so orphan detection is disabled.
    config_unavailable = not targets and not _remotes_config_readable()
    entries: list[dict] = []
    if cache_dir.exists():
        for path in sorted(cache_dir.iterdir()):
            if not path.is_dir():
                continue
            target = targets.get(path.name)
            is_orphan = target is None and not config_unavailable
            stale_bytes = _tree_bytes(path / ".stale")
            entry = {
                "path": path,
                "owner": (
                    f"{target.label}/{target.agent_type}/{target.remote_root}"
                    if target is not None
                    else ""
                ),
                "is_orphan": is_orphan,
                "stale_bytes": stale_bytes,
                "total_bytes": (
                    _tree_bytes(path) if include_active_sizes or is_orphan else None
                ),
            }
            entries.append(entry)
    reclaimable = sum(
        (entry["total_bytes"] if entry["is_orphan"] else entry["stale_bytes"]) or 0
        for entry in entries
    )
    return {
        "entries": entries,
        "reclaimable_bytes": reclaimable,
        "config_unavailable": config_unavailable,
    }


def prune_remote_cache(*, dry_run: bool = False) -> dict:
    """Delete orphaned cache dirs and all stale archives.

    Only touches this tool's own derived mirror cache under
    ``DATA_DIR/remote-cache``; active mirrors and source data are never
    removed. Returns the report of what was (or would be) reclaimed.
    """
    cache_dir = (DATA_DIR / "remote-cache").resolve()
    report = remote_cache_report()
    removed: list[dict] = []
    failed: list[dict] = []
    for entry in report["entries"]:
        path = entry["path"].resolve()
        if cache_dir not in path.parents:
            continue
        if entry["is_orphan"]:
            item = {"path": path, "bytes": entry["total_bytes"] or 0, "kind": "orphan"}
        elif entry["stale_bytes"]:
            item = {"path": path / ".stale", "bytes": entry["stale_bytes"], "kind": "stale"}
        else:
            continue
        if not dry_run:
            shutil.rmtree(item["path"], ignore_errors=True)
            if item["path"].exists():
                # Deletion silently failed (permissions, concurrent writes);
                # don't report space as reclaimed when it wasn't.
                failed.append(item)
                continue
        removed.append(item)
    return {
        "removed": removed,
        "failed": failed,
        "reclaimed_bytes": sum(r["bytes"] for r in removed),
        "config_unavailable": report["config_unavailable"],
    }


def _next_archive_path(path: Path) -> Path:
    if not path.exists():
        return path
    for idx in range(1, 10_000):
        candidate = path.with_name(f"{path.name}.stale-{idx}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.name}.stale")


def _extract_tarball(payload: bytes, dest: Path) -> set[str]:
    """Extract a downloaded archive. Returns the manifest keys actually written."""
    written: set[str] = set()
    with tarfile.open(fileobj=BytesIO(payload), mode="r:gz") as tar:
        for member in tar.getmembers():
            check_sync_cancelled()
            if member.issym() or member.islnk():
                continue
            rel = _safe_member_path(member.name)
            if rel is None:
                continue
            target = dest / rel
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            if src is None:
                continue
            with src, target.open("wb") as out:
                out.write(src.read())
            written.add(str(PurePosixPath(*rel.parts)))
    return written


def _safe_member_path(name: str) -> Path | None:
    normalized = posixpath.normpath(name)
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or ".." in pure.parts or normalized in ("", "."):
        return None
    return Path(*pure.parts)
