"""SSH-backed collector wrappers for remote agent history."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha1
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
import posixpath
import shlex
import subprocess
import tarfile

from ..config import DATA_DIR, RemoteSpec
from ..models import LiveSession
from . import BaseCollector
from .claude_code import ClaudeCodeCollector
from .codex import CodexCollector


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

    def get_live_sessions(self) -> list[LiveSession]:
        # Remote live process inspection is intentionally not attempted.
        return []

    def sync_history(self, db) -> None:
        try:
            if not _sync_target_to_cache(self.target):
                self.last_error = (
                    f"remote path not found: "
                    f"{self.target.source_root}/{self.target.source_child}"
                )
                return
            self.last_error = ""
        except Exception as exc:
            self.last_error = str(exc)
            return
        self._inner.sync_history(db)


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
    return (
        _remote_base_script(target.source_root, target.source_child)
        + 'if [ ! -d "$base" ]; then printf "MISSING\\0"; exit 0; fi; '
        + 'printf "OK\\0"; cd "$base"; '
        + f"find . -type f {find_expr} -exec stat -c '%s\\t%Y\\t%n\\0' {{}} +"
    )


def _download_command(target: RemoteSyncTarget) -> str:
    return (
        _remote_base_script(target.source_root, target.source_child)
        + 'if [ -d "$base" ]; then cd "$base"; tar -czf - --null -T -; fi'
    )


def _sync_target_to_cache(target: RemoteSyncTarget) -> bool:
    cache_root = _cache_root_for(target)
    cache_root.mkdir(parents=True, exist_ok=True)

    manifest = _read_remote_manifest(target)
    if manifest is None:
        return False

    previous = _load_manifest(cache_root)
    changed = [
        rel for rel, meta in manifest.items()
        if previous.get(rel) != meta or not (cache_root / target.source_child / rel).exists()
    ]
    if changed:
        payload = "\0".join(changed).encode("utf-8") + b"\0"
        proc = _run_ssh(target.remote, _download_command(target), input_bytes=payload)
        if proc.stdout:
            _extract_tarball(proc.stdout, cache_root / target.source_child)
    _archive_stale_cache_files(cache_root, target, manifest)
    _save_manifest(cache_root, manifest)
    return True


def _run_ssh(
    remote: RemoteSpec,
    remote_cmd: str,
    *,
    input_bytes: bytes | None = None,
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
    if proc.returncode != 0:
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
        manifest[str(PurePosixPath(*safe.parts))] = {"size": size, "mtime": mtime}
    return manifest


def _manifest_path(cache_root: Path) -> Path:
    return cache_root / ".remote-manifest.json"


def _load_manifest(cache_root: Path) -> dict[str, dict[str, str]]:
    try:
        data = json.loads(_manifest_path(cache_root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


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


def _next_archive_path(path: Path) -> Path:
    if not path.exists():
        return path
    for idx in range(1, 10_000):
        candidate = path.with_name(f"{path.name}.stale-{idx}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.name}.stale")


def _extract_tarball(payload: bytes, dest: Path) -> None:
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


def _safe_member_path(name: str) -> Path | None:
    normalized = posixpath.normpath(name)
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or ".." in pure.parts or normalized in ("", "."):
        return None
    return Path(*pure.parts)
