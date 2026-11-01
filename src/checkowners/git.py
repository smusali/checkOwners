"""Running git and parsing what it prints.

Every subprocess call checkOwners makes goes through `run_git` here, so this
module is the single seam between the analysis and the repository on disk. It
also holds the capability probes (git version, shallow clone, mailmap support)
and the small readers that turn git output into values.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from checkowners.errors import IntegrationError

SOURCE_DATE_EPOCH_ENV = "SOURCE_DATE_EPOCH"


MIN_GIT_VERSION = (2, 23, 0)


_GIT_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


_PATH_GIT_CONFIG = ("-c", "core.quotePath=false", "-c", "core.longpaths=true")


class GitRequirementError(IntegrationError):
    pass


FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")


SHA_BATCH = 40


def parse_as_of(value: str) -> datetime:
    """Parse an ISO 8601 instant. Naive values are treated as UTC."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        msg = f"Invalid as-of value: {value!r}"
        raise ValueError(msg) from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_source_date_epoch(raw: str) -> datetime:
    """Parse SOURCE_DATE_EPOCH as integer POSIX seconds in UTC."""
    try:
        seconds = int(raw)
    except ValueError as exc:
        msg = f"Invalid SOURCE_DATE_EPOCH: {raw!r}"
        raise ValueError(msg) from exc
    return datetime.fromtimestamp(seconds, tz=UTC)


def head_commit_datetime(repo_root: Path) -> datetime:
    result = run_git(repo_root, ["git", "log", "-1", "--format=%cI"], check=True)
    parsed = parse_timestamp(result.stdout.strip())
    if parsed is None:
        msg = "HEAD commit has no parseable committer timestamp"
        raise ValueError(msg)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def head_commit_sha(repo_root: Path) -> str:
    result = run_git(repo_root, ["git", "rev-parse", "HEAD"], check=True)
    sha = result.stdout.strip()
    if not sha:
        msg = "Could not resolve HEAD commit SHA"
        raise ValueError(msg)
    return sha


def resolve_as_of(cli_value: str | None, repo_root: Path) -> datetime:
    """Resolve the analysis instant from CLI, SOURCE_DATE_EPOCH, or HEAD.

    Priority: ``cli_value`` (ISO 8601), then ``SOURCE_DATE_EPOCH``, then the
    HEAD committer timestamp. Never uses the wall clock.
    """
    if cli_value:
        return parse_as_of(cli_value)
    epoch = os.environ.get(SOURCE_DATE_EPOCH_ENV)
    if epoch:
        return parse_source_date_epoch(epoch)
    return head_commit_datetime(repo_root)


def analysis_epoch(when: datetime) -> str:
    aware = when if when.tzinfo is not None else when.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat()


def run_git(
    repo_root: Path,
    args: list[str],
    *,
    check: bool,
    paths: bool = False,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    argv = args
    if paths and args and args[0] == "git":
        argv = ["git", *_PATH_GIT_CONFIG, *args[1:]]
    return subprocess.run(  # noqa: S603
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        cwd=repo_root,
        check=check,
        input=stdin,
    )


def git_stdout(repo_root: Path, argv: list[str], *, paths: bool = False) -> str | None:
    try:
        result = run_git(repo_root, argv, check=False, paths=paths)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def is_shallow_repository(repo_root: Path) -> bool:
    stdout = git_stdout(repo_root, ["git", "rev-parse", "--is-shallow-repository"])
    if stdout is not None:
        text = stdout.strip()
        if text == "true":
            return True
        if text == "false":
            return False
    shallow = git_stdout(repo_root, ["git", "rev-parse", "--git-path", "shallow"])
    if shallow is None:
        return False
    text = shallow.strip()
    if not text or "\n" in text:
        return False
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    try:
        return candidate.is_file()
    except OSError:
        return False


def stdout_has_rename(stdout: str) -> bool:
    for line in stdout.splitlines():
        if line.startswith("R") and "\t" in line:
            status = line.split("\t", 1)[0]
            if status == "R" or (len(status) > 1 and status[1:].isdigit()):
                return True
    return False


MAILMAP_FLAGS = frozenset({"--use-mailmap", "--no-use-mailmap"})


def mailmap_flag(enabled: bool) -> str:
    return "--use-mailmap" if enabled else "--no-use-mailmap"


@contextmanager
def suppress_workdir_mailmap(repo_root: Path, *, enabled: bool) -> Iterator[None]:
    visible = repo_root / ".mailmap"
    if enabled or not visible.is_file():
        yield
        return
    hidden = repo_root / ".mailmap.checkowners-hidden"
    visible.replace(hidden)
    try:
        yield
    finally:
        hidden.replace(visible)


def rename_detection(repo_root: Path) -> str:
    return git_config_value(repo_root, "diff.renames") or "true"


def commit_exists(repo_root: Path, ref: str) -> bool:
    result = run_git(repo_root, ["git", "cat-file", "-e", f"{ref}^{{commit}}"], check=False)
    return result.returncode == 0


def is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    if not ancestor or not descendant:
        return False
    result = run_git(
        repo_root,
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        check=False,
    )
    return result.returncode == 0


def ancestor_shas(repo_root: Path, ref: str) -> set[str]:
    result = run_git(repo_root, ["git", "rev-list", ref], check=True)
    return set(result.stdout.split())


def mailmap_enabled(repo_root: Path, requested: bool) -> bool:
    return requested and blame_accepts_mailmap_flags(repo_root)


def blob_shas(repo_root: Path, paths: list[str]) -> dict[str, str]:
    safe = [path for path in paths if "\n" not in path and "\x00" not in path]
    if not safe:
        return {}
    result = run_git(
        repo_root,
        ["git", "hash-object", "--stdin-paths"],
        check=False,
        stdin="\n".join(safe) + "\n",
    )
    if result.returncode != 0:
        return {}
    hashes = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(hashes) != len(safe):
        return {}
    return dict(zip(safe, hashes, strict=True))


def blobs_at_revision(repo_root: Path, revision: str, paths: list[str]) -> dict[str, str]:
    safe = [path for path in paths if "\n" not in path and "\x00" not in path]
    if not safe or not revision:
        return {}
    result = run_git(
        repo_root,
        ["git", "cat-file", "--batch-check"],
        check=False,
        stdin="".join(f"{revision}:{path}\n" for path in safe),
    )
    if result.returncode != 0:
        return {}
    found: dict[str, str] = {}
    for path, line in zip(safe, result.stdout.splitlines(), strict=False):
        blob, _separator, kind = line.partition(" ")
        if kind.startswith("blob") and FULL_SHA_RE.fullmatch(blob):
            found[path] = blob
    return found


def parse_timestamp(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def parse_git_version(raw: str) -> tuple[int, int, int]:
    """Parse `git version` stdout into (major, minor, patch)."""
    match = _GIT_VERSION_RE.search(raw)
    if match is None:
        msg = f"Could not parse git version from {raw!r}"
        raise ValueError(msg)
    major = int(match.group(1))
    minor = int(match.group(2))
    patch = int(match.group(3) or 0)
    return (major, minor, patch)


def ensure_git_version(repo_root: Path) -> None:
    required = ".".join(str(part) for part in MIN_GIT_VERSION)
    try:
        result = run_git(repo_root, ["git", "version"], check=True)
    except OSError as exc:
        msg = (
            f"checkOwners requires Git {required} or newer; found none. "
            "Install a newer Git and rerun."
        )
        raise GitRequirementError(msg) from exc
    try:
        found = parse_git_version(result.stdout)
    except ValueError as exc:
        raise GitRequirementError(str(exc)) from exc
    if found < MIN_GIT_VERSION:
        pretty = ".".join(str(part) for part in found)
        msg = (
            f"checkOwners requires Git {required} or newer; found {pretty}. "
            "Install a newer Git and rerun."
        )
        raise GitRequirementError(msg)


def git_config_value(repo_root: Path, key: str) -> str:
    try:
        result = run_git(repo_root, ["git", "config", "--get", key], check=True)
    except subprocess.CalledProcessError:
        return ""
    return result.stdout.strip()


def existing_file(repo_root: Path, raw: str) -> Path | None:
    if not raw or "\n" in raw or "\x00" in raw:
        return None
    try:
        candidate = Path(raw)
        resolved = candidate if candidate.is_absolute() else repo_root / candidate
        if resolved.is_file():
            return resolved
    except OSError:
        return None
    return None


def resolve_ignore_revs_file(repo_root: Path, configured: str) -> Path | None:
    found = existing_file(repo_root, configured)
    if found is not None:
        return found
    return existing_file(repo_root, git_config_value(repo_root, "blame.ignoreRevsFile"))


def display_ignore_revs_path(repo_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def resolve_mailmap_file(repo_root: Path) -> Path | None:
    candidate = repo_root / ".mailmap"
    if candidate.is_file():
        return candidate
    return None


def tracked_file_count(repo_root: Path) -> int:
    result = run_git(repo_root, ["git", "ls-files"], check=True, paths=True)
    return sum(1 for line in result.stdout.splitlines() if line)


def parse_modified_counts(stdout: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    current_sha = ""
    modified = 0

    def flush() -> None:
        nonlocal current_sha, modified
        if current_sha:
            counts[current_sha] = modified
        current_sha = ""
        modified = 0

    for line in stdout.splitlines():
        if not line:
            flush()
            continue
        if FULL_SHA_RE.fullmatch(line):
            flush()
            current_sha = line
            continue
        if line.startswith("M\t") or line.startswith("M "):
            modified += 1
    flush()
    return counts


def modified_counts(repo_root: Path, revision: str | None = None) -> dict[str, int]:
    argv = ["git", "log", "--name-status", "--pretty=format:%H"]
    if revision:
        argv.append(revision)
    result = run_git(repo_root, argv, check=True, paths=True)
    return parse_modified_counts(result.stdout)


def mass_shas(counts: Mapping[str, int], tracked: int, fraction: float) -> tuple[str, ...]:
    if fraction <= 0.0 or tracked <= 0:
        return ()
    return tuple(sha for sha, modified in counts.items() if modified / tracked >= fraction)


def mass_refactor_revs(repo_root: Path, fraction: float) -> tuple[str, ...]:
    if fraction <= 0.0:
        return ()
    tracked = tracked_file_count(repo_root)
    if tracked == 0:
        return ()
    return mass_shas(modified_counts(repo_root), tracked, fraction)


def write_ignore_revs(shas: tuple[str, ...]) -> Path:
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115
        mode="w",
        encoding="utf-8",
        suffix=".revs",
        delete=False,
    )
    with handle:
        handle.write("\n".join(shas) + "\n")
    return Path(handle.name)


def file_bytes(path: Path | None) -> bytes:
    if path is None:
        return b""
    try:
        return path.read_bytes()
    except OSError:
        return b""


def drop_mailmap_flags(argv: list[str]) -> list[str]:
    return [part for part in argv if part not in MAILMAP_FLAGS]


def blame_accepts_mailmap_flags(repo_root: Path) -> bool:
    result = run_git(repo_root, ["git", "blame", "--use-mailmap"], check=False)
    return "unknown option" not in f"{result.stdout}{result.stderr}"
