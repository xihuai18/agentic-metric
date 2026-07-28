"""SSH-backed collector wrappers for remote agent history."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha1
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
import posixpath
import shlex
import shutil
import subprocess
import tarfile
import time

from ..config import DATA_DIR, RemoteSpec
from . import BaseCollector
from .claude_code import ClaudeCodeCollector
from .codex import CodexCollector

# Stale archives keep a short-lived local copy of files that disappeared from
# a remote; they are never parsed again, so age them out to bound cache growth.
_STALE_RETENTION_DAYS = 30

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


@dataclass(frozen=True)
class RemoteSyncTarget:
    """One remote agent root to mirror into the local cache before parsing."""

    remote: RemoteSpec
    agent_type: str
    remote_root: str
    provider: str
    index: int

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
        # (found, cache_changed, manifest_digest) from a prepare_cache() call
        # awaiting its sync_history(); None when no mirror result is pending.
        self._prepared: tuple[bool, bool, str] | None = None

        cache_root = _cache_root_for(target)
        if target.agent_type == "codex":
            self._inner = CodexCollector(
                sessions_dir=cache_root / "sessions",
                provider=self.provider,
                data_root=self.data_root,
            )
        elif target.agent_type == "claude_code":
            self._inner = ClaudeCodeCollector(
                projects_dir=cache_root / "projects",
                provider=self.provider,
                data_root=self.data_root,
            )
        else:
            raise ValueError(f"unsupported remote agent type: {target.agent_type}")

    @property
    def agent_type(self) -> str:
        return self._agent_type

    def prepare_cache(self) -> None:
        """Mirror the remote root into the local cache (no DB access).

        Network-only, so a registry can run one prepare per remote target in
        parallel before the serial DB parsing phase.
        """
        try:
            self._prepared = _sync_target_to_cache(self.target)
            self.last_error = "" if self._prepared[0] else (
                f"remote path not found: "
                f"{self.target.source_root}/{self.target.source_child}"
            )
        except Exception as exc:
            self._prepared = (False, False, "")
            self.last_error = str(exc)

    def sync_history(self, db) -> None:
        prepared = self._prepared
        self._prepared = None
        if prepared is None:
            self.prepare_cache()
            prepared = self._prepared or (False, False, "")
            self._prepared = None
        found, cache_changed, manifest_digest = prepared
        if not found:
            return
        ready_key = self._ready_state_key()
        if not cache_changed and db.get_sync_state(ready_key) == manifest_digest:
            # The mirror is byte-identical to the last fully parsed state, so
            # the per-file scan of the whole cache tree can be skipped.
            return
        if cache_changed:
            _purge_removed_remote_sessions(db, self.target)
        self._inner.sync_history(db)
        db.set_sync_state(ready_key, manifest_digest)

    def _ready_state_key(self) -> str:
        """Sync-state key marking a fully parsed mirror state.

        Shares the collector sync-state prefixes so pricing-fingerprint
        migrations and history rebuilds invalidate it together with the
        per-file parse states.
        """
        prefix = "codex_jsonl" if self.target.agent_type == "codex" else "cc_jsonl"
        return f"{prefix}:remote_ready:v1:{_cache_root_for(self.target).name}"


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


def _purge_removed_remote_sessions(db, target: RemoteSyncTarget) -> None:
    """Drop derived DB rows whose remote JSONL no longer exists in active cache."""
    active_ids = _active_remote_session_ids(target)
    rows = db.conn.execute(
        """SELECT session_id
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
            provider=target.provider,
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


def _active_claude_session_ids(active_root: Path) -> set[str]:
    ids: set[str] = set()
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


def _sync_target_to_cache(target: RemoteSyncTarget) -> tuple[bool, bool, str]:
    """Mirror one remote root. Returns (found, cache_changed, manifest_digest)."""
    cache_root = _cache_root_for(target)
    cache_root.mkdir(parents=True, exist_ok=True)

    manifest = _read_remote_manifest(target)
    if manifest is None:
        return (False, False, "")

    loaded = _load_manifest(cache_root)
    # A partial manifest (left by a download that skipped files) is not
    # authoritative: treat it like a missing one so stale archiving and session
    # purging still run once the mirror completes.
    had_manifest = loaded is not None and not _manifest_is_incomplete(cache_root)
    previous = loaded or {}
    changed = [
        rel for rel, meta in manifest.items()
        if previous.get(rel) != meta or not (cache_root / target.source_child / rel).exists()
    ]
    if changed:
        missing = _download_changed_files(target, cache_root, manifest, previous, changed)
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
    if cache_changed:
        # Stale archiving and session purging only matter when the mirror
        # actually changed; skipping them keeps unchanged remotes cheap. Stale
        # detection uses the full remote manifest so files that are merely
        # pending download are not archived.
        _archive_stale_cache_files(cache_root, target, manifest)
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
    return (True, cache_changed, _manifest_digest(saved_manifest))


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

    try:
        for batch in _download_batches(manifest, changed):
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
                # Never record the failed batch: a half-extracted file must still
                # look changed to the next sync even when its manifest metadata
                # matches, or the truncated copy would be accepted as current.
                for rel in batch:
                    progress.pop(rel, None)
                if len(batch) == 1 and skipped < _MAX_SKIPPED_BATCHES:
                    skipped += 1
                    missing.update(batch)
                    if first_error is None:
                        first_error = exc
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
            for rel in batch:
                meta = manifest.get(rel)
                if meta is None:
                    continue
                if rel in written:
                    progress[rel] = meta
                else:
                    # The archive did not carry this member (deleted mid-transfer,
                    # or dropped by tar). Recording it would let an outdated local
                    # copy pass as current, so leave it for the next sync.
                    progress.pop(rel, None)
                    missing.add(rel)
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
    try:
        proc = subprocess.run(
            _ssh_command(remote, remote_cmd),
            input=input_bytes,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=remote.timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ssh timed out after {remote.timeout}s") from exc
    if proc.returncode not in allowed_returncodes:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(stderr or f"ssh exited with status {proc.returncode}")
    return proc


def _read_remote_manifest(target: RemoteSyncTarget) -> dict[str, dict[str, str]] | None:
    proc = _run_ssh(target.remote, _manifest_command(target))
    parts = proc.stdout.split(b"\0")
    if not parts or parts[0] == b"MISSING":
        return None
    if parts[0] != b"OK":
        raise RuntimeError("unexpected remote manifest response")

    manifest: dict[str, dict[str, str]] = {}
    for item in parts[1:]:
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
    return manifest


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


def _archive_stale_cache_files(
    cache_root: Path,
    target: RemoteSyncTarget,
    manifest: dict[str, dict[str, str]],
) -> None:
    active_root = cache_root / target.source_child
    if not active_root.exists():
        return

    patterns = ["rollout-*.jsonl"] if target.agent_type == "codex" else ["*.jsonl", "sessions-index.json"]
    manifest_paths = set(manifest)
    stale_root = cache_root / ".stale" / target.source_child
    for pattern in patterns:
        for path in active_root.rglob(pattern):
            if not path.is_file():
                continue
            rel = str(PurePosixPath(*path.relative_to(active_root).parts))
            if rel in manifest_paths:
                continue
            archive_path = _next_archive_path(stale_root / rel)
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            path.replace(archive_path)
            # replace() keeps the source mtime; retention must run from the
            # archive time, or an old file removed today would age out at once.
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
