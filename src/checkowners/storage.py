"""Where checkOwners keeps its files, and how it reads and writes them.

Everything under ``~/.checkowners`` (or ``CHECKOWNERS_STATE_DIR``) is reached
through this module: the per-repository identity used to key the cache, the
paths of each cache kind, and the locked, atomic JSON read and write. It owns
no ownership semantics, so both ``state`` and ``identity`` can import it.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import threading
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import IO, Protocol

from checkowners.config import (
    Config,
)

#: Windows needs msvcrt locking; the flag keeps the check out of the call site.
_WINDOWS = os.name == "nt"


CACHE_LIMIT_BYTES: int = 256 * 1024 * 1024


_STATE_DIR = Path.home() / ".checkowners"


STATE_SUBDIR = "state"


GRAPH_CACHE_SUBDIR = "graph"


HANDLE_CACHE_SUBDIR = "handles"


IDENTITY_SUBDIR = "identity"


_HANDLE_CACHE_FILENAME = "handles.json"


_SCP_REMOTE = re.compile(r"^(?:[^@/]+@)?([^:/]+):(.+)$")


def cache_directory() -> Path:
    """Return the cache root, honoring CHECKOWNERS_STATE_DIR."""
    return base_dir()


def base_dir() -> Path:
    """Resolve the checkowners state directory, honoring CHECKOWNERS_STATE_DIR."""
    override = os.environ.get("CHECKOWNERS_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return _STATE_DIR


def repository_identity(repo_root: Path) -> str:
    """Return a stable id for `repo_root`: normalized origin, else absolute path."""
    remote = _origin_url(repo_root)
    if remote:
        normalized = _normalize_remote(remote)
        if normalized:
            return f"origin:{normalized}"
    return f"path:{repo_root.resolve()}"


def config_hash(config: Config) -> str:
    """Return a sha256 of the scoring-relevant fields of `config`."""
    payload = {
        "analysis": asdict(config.analysis),
        "qualification": asdict(config.qualification),
        "scoring": asdict(config.scoring),
        "decay": asdict(config.decay),
        "bus_factor": asdict(config.bus_factor),
        "risk": asdict(config.risk),
        "paths": asdict(config.paths),
        "git": asdict(config.git),
        "github": {
            "api_enabled": config.github.api_enabled,
            "org": config.github.org,
            "resolve_handles": config.github.resolve_handles,
            "resolve_teams": config.github.resolve_teams,
        },
        "contributors_exclude": list(config.contributors_exclude),
        "criticality": [list(rule) for rule in config.criticality],
        "negative_cache_ttl_days": config.negative_cache_ttl_days,
        "identity_do_not_merge": [list(pair) for pair in config.identity_do_not_merge],
        "identity_overrides": [
            {"handle": handle, **asdict(override)} for handle, override in config.identity_overrides
        ],
        "contributor_roster": [list(item) for item in config.contributor_roster],
        "bot_addresses": list(config.bot_addresses),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def _git_config_path(repo_root: Path) -> Path | None:
    git_entry = repo_root / ".git"
    if git_entry.is_dir():
        return git_entry / "config"
    if not git_entry.is_file():
        return None
    text = git_entry.read_text(encoding="utf-8", errors="replace").strip()
    if not text.lower().startswith("gitdir:"):
        return None
    raw = text.split(":", 1)[1].strip()
    git_dir = Path(raw)
    if not git_dir.is_absolute():
        git_dir = (repo_root / git_dir).resolve()
    common = git_dir / "commondir"
    if common.is_file():
        common_raw = common.read_text(encoding="utf-8", errors="replace").strip()
        common_dir = Path(common_raw)
        if not common_dir.is_absolute():
            common_dir = (git_dir / common_dir).resolve()
        return common_dir / "config"
    return git_dir / "config"


def _origin_url(repo_root: Path) -> str | None:
    config_path = _git_config_path(repo_root)
    if config_path is None or not config_path.is_file():
        return None
    in_origin = False
    try:
        lines = config_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_origin = stripped.lower() == '[remote "origin"]'
            continue
        if not in_origin:
            continue
        key, separator, value = stripped.partition("=")
        if separator and key.strip().lower() == "url":
            url = value.strip()
            return url or None
    return None


def _normalize_remote(url: str) -> str:
    text = url.strip().rstrip("/")
    if text.endswith(".git"):
        text = text[: -len(".git")]
    if "://" in text:
        parsed = urllib.parse.urlsplit(text)
        host = (parsed.hostname or "").lower()
        path = parsed.path.strip("/")
        if host and path:
            return f"{host}/{path}"
        return ""
    match = _SCP_REMOTE.fullmatch(text)
    if match is None:
        return ""
    host = match.group(1).lower()
    path = match.group(2).strip("/")
    return f"{host}/{path}"


def repo_digest(repo_root: Path) -> str:
    return hashlib.sha256(repository_identity(repo_root).encode("utf-8")).hexdigest()[:16]


def state_path(repo_root: Path) -> Path:
    """Resolve the per-repo state file path, honoring CHECKOWNERS_STATE_DIR."""
    return base_dir() / STATE_SUBDIR / f"{repo_digest(repo_root)}.json"


def graph_cache_path(repo_root: Path) -> Path:
    """Resolve the serialized-graph cache path for a repo (keyed by repo hash)."""
    return base_dir() / GRAPH_CACHE_SUBDIR / f"{repo_digest(repo_root)}.json"


def read_json(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return {str(key): value for key, value in data.items()}


_PROCESS_LOCK = threading.Lock()


def _file_lock(handle: IO[bytes], *, release: bool) -> None:
    try:
        import fcntl  # noqa: PLC0415
    except ImportError:
        _windows_lock(handle, release=release)
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN if release else fcntl.LOCK_EX)


class _Msvcrt(Protocol):
    """The part of the Windows `msvcrt` module this module locks with.

    typeshed gates the real stubs behind `sys.platform == "win32"`, so the
    protocol is what gives the call site a type on every platform.
    """

    LK_LOCK: int
    LK_UNLCK: int

    def locking(self, fd: int, mode: int, nbytes: int, /) -> None: ...


def _windows_lock(handle: IO[bytes], *, release: bool) -> None:
    if not _WINDOWS:
        return
    msvcrt: _Msvcrt = importlib.import_module("msvcrt")
    if not release:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
    handle.seek(0)
    mode = msvcrt.LK_UNLCK if release else msvcrt.LK_LOCK
    msvcrt.locking(handle.fileno(), mode, 1)


@contextmanager
def locked(target: Path) -> Iterator[None]:
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(f"{target.name}.lock")
    with _PROCESS_LOCK, lock_path.open("a+b") as handle:
        _file_lock(handle, release=False)
        try:
            yield
        finally:
            _file_lock(handle, release=True)


def atomic_write(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def handle_cache_files() -> list[Path]:
    files = list(json_files(HANDLE_CACHE_SUBDIR))
    legacy = base_dir() / _HANDLE_CACHE_FILENAME
    if legacy.is_file():
        files.append(legacy)
    return files


def json_files(subdir: str) -> list[Path]:
    directory = base_dir() / subdir
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.json") if path.is_file())


def blame_cache_files() -> list[Path]:
    root = base_dir() / "blame"
    if not root.is_dir():
        return []
    return sorted(path for path in root.rglob("*.json") if path.is_file())


def bounded_files() -> list[Path]:
    return json_files(STATE_SUBDIR) + json_files(GRAPH_CACHE_SUBDIR) + blame_cache_files()


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def identity_store_path(repo_root: Path) -> Path:
    """Return the identity-graph file for `repo_root`."""
    return base_dir() / IDENTITY_SUBDIR / f"{repo_digest(repo_root)}.json"


def read_json_file(path: Path) -> dict[str, object] | None:
    """Return the JSON object at `path`, or None when it is missing or invalid."""
    raw = read_json(path)
    if raw is None:
        return None
    copied: dict[str, object] = {}
    for key, item in raw.items():
        copied[key] = item
    return copied


def write_json_file(path: Path, payload: object) -> None:
    """Atomically write `payload` as JSON at `path`."""
    atomic_write(path, json.dumps(payload, indent=2, sort_keys=True))
