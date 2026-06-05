"""SSH-backed collector wrappers for remote agent history."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha1
from io import BytesIO
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
            str(target.remote.timeout),
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


def _ssh_command(remote: RemoteSpec, remote_root: str, subdir: str) -> list[str]:
    cmd = ["ssh"]
    if remote.port:
        cmd.extend(["-p", str(remote.port)])
    cmd.extend(remote.ssh_options)
    cmd.append(_ssh_destination(remote))

    root = shlex.quote(remote_root)
    child = shlex.quote(subdir)
    remote_cmd = (
        "set -e; "
        f"root={root}; child={child}; "
        'case "$root" in '
        '"~") root="$HOME" ;; '
        '"~/"*) root="$HOME/${root#~/}" ;; '
        "esac; "
        'if [ -d "$root/$child" ]; then '
        'tar -C "$root" -czf - "$child"; '
        "fi"
    )
    cmd.append(remote_cmd)
    return cmd


def _sync_target_to_cache(target: RemoteSyncTarget) -> bool:
    cache_root = _cache_root_for(target)
    cache_root.mkdir(parents=True, exist_ok=True)

    try:
        proc = subprocess.run(
            _ssh_command(target.remote, target.source_root, target.source_child),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=target.remote.timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ssh timed out after {target.remote.timeout}s") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(stderr or f"ssh exited with status {proc.returncode}")
    if not proc.stdout:
        return False

    _extract_tarball(proc.stdout, cache_root)
    return True


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
