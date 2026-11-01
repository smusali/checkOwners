"""The git blame pass and its on-disk cache.

`gather_blame_coverage` turns a set of paths into per-author line shares.
Results are cached by blob SHA and blame inputs, and `coverage_with_reuse`
replays a stored baseline for the paths a run did not touch.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from checkowners import git
from checkowners.config import Config, GitConfig
from checkowners.state import BlameLines, HistoryBaseline, read_blame_cache, write_blame_cache

#: Optional progress hook: called as on_progress(done, total) while blame runs.
#: Injected by the CLI to drive a progress bar; analyze.py stays console-free.
ProgressHook = Callable[[int, int], None]


@dataclass(frozen=True)
class BlamePass:
    coverage: dict[str, dict[str, float]] = field(default_factory=dict)
    ignore_revs_file: str = ""
    ignore_revs_applied: bool = False
    truncated: bool = False
    lines: dict[str, dict[str, int]] = field(default_factory=dict)
    mass: tuple[str, ...] = ()


@dataclass(frozen=True)
class BlameDetail:
    fractions: dict[str, float]
    commits: dict[str, int]
    reusable: bool
    lines: dict[str, int] = field(default_factory=dict)


_BLAME_SINK_LOCK = threading.Lock()


BLAME_DEADLINE: ContextVar[float | None] = ContextVar("checkowners_blame_deadline", default=None)


def blame_inputs_token(repo_root: Path, config: Config, mass: tuple[str, ...]) -> str:
    ignore = git.resolve_ignore_revs_file(repo_root, config.git.blame_ignore_revs_file)
    text = ""
    if ignore is not None:
        try:
            text = ignore.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    payload = json.dumps(
        {"ignore": text, "mass": sorted(mass), "moves": config.git.detect_moves},
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _author_counts(commit_lines: Mapping[str, int], emails: Mapping[str, str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for sha, count in commit_lines.items():
        email = emails.get(sha)
        if email is None or count <= 0:
            continue
        counts[email] = counts.get(email, 0) + count
    return dict(sorted(counts.items()))


def _author_emails(repo_root: Path, shas: Iterable[str], *, use_mailmap: bool) -> dict[str, str]:
    unique = sorted({sha for sha in shas if git.FULL_SHA_RE.fullmatch(sha)})
    found: dict[str, str] = {}
    for start in range(0, len(unique), git.SHA_BATCH):
        batch = unique[start : start + git.SHA_BATCH]
        result = git.run_git(
            repo_root,
            [
                "git",
                "log",
                "--no-walk",
                git.mailmap_flag(use_mailmap),
                "--format=%H%x00%aE",
                *batch,
            ],
            check=True,
        )
        for line in result.stdout.splitlines():
            sha, separator, email = line.partition("\x00")
            if separator and email and git.FULL_SHA_RE.fullmatch(sha):
                found[sha] = email
    return found


def reused_fraction(
    stored: BlameLines | None,
    blob: str,
    emails: Mapping[str, str],
) -> dict[str, float] | None:
    if stored is None or not blob or stored.blob_sha != blob or not stored.lines:
        return None
    total = 0
    counts: dict[str, int] = {}
    for sha, count in stored.lines.items():
        email = emails.get(sha)
        if not email:
            return None
        total += count
        counts[email] = counts.get(email, 0) + count
    if total <= 0:
        return None
    return dict(sorted((author, count / total) for author, count in counts.items()))


def coverage_with_reuse(
    paths: Iterable[str],
    repo_root: Path,
    config: Config,
    *,
    baseline: HistoryBaseline | None,
    modified: Mapping[str, int],
    on_progress: ProgressHook | None,
    max_workers: int | None,
) -> tuple[BlamePass, dict[str, BlameLines], str, bool]:
    tracked = git.tracked_file_count(repo_root)
    mass = git.mass_shas(modified, tracked, config.git.mass_refactor_file_fraction)
    inputs = blame_inputs_token(repo_root, config, mass)
    path_list = sorted(paths)
    reuse_ok = baseline is not None and not baseline.truncated and baseline.blame_inputs == inputs
    blobs = git.blob_shas(repo_root, path_list)
    emails: dict[str, str] = {}
    if reuse_ok and baseline is not None:
        wanted: list[str] = []
        for path in path_list:
            stored = baseline.blame.get(path)
            if stored is not None and stored.blob_sha == blobs.get(path, "") and stored.blob_sha:
                wanted.extend(stored.lines)
        emails = _author_emails(
            repo_root,
            wanted,
            use_mailmap=git.mailmap_enabled(repo_root, config.git.use_mailmap),
        )
    reused: dict[str, dict[str, float]] = {}
    saved: dict[str, BlameLines] = {}
    dirty: list[str] = []
    for path in path_list:
        if reuse_ok and baseline is not None:
            fraction = reused_fraction(
                baseline.blame.get(path),
                blobs.get(path, ""),
                emails,
            )
            if fraction is not None:
                reused[path] = fraction
                saved[path] = baseline.blame[path]
                continue
        dirty.append(path)
    ignore = git.resolve_ignore_revs_file(repo_root, config.git.blame_ignore_revs_file)
    display = git.display_ignore_revs_path(repo_root, ignore) if ignore else ""
    sink: dict[str, BlameDetail] = {}
    if dirty:
        observed = len(reused)
        hook: ProgressHook | None
        if on_progress is None:
            hook = None
        else:
            callback = on_progress

            def report(done: int, _total: int) -> None:
                callback(observed + done, len(path_list))

            hook = report
        fresh = gather_blame_coverage(
            dirty,
            repo_root,
            git_config=config.git,
            on_progress=hook,
            max_workers=max_workers,
            mass_revs=mass,
            detail_sink=sink,
            blobs=blobs,
        )
    else:
        fresh = BlamePass(ignore_revs_file=display, ignore_revs_applied=ignore is not None)
        if on_progress is not None and path_list:
            on_progress(len(path_list), len(path_list))
    coverage = {**reused, **fresh.coverage}
    lines = {path: dict(counts) for path, counts in fresh.lines.items()}
    for path in reused:
        lines[path] = _author_counts(saved[path].lines, emails)
    passed = BlamePass(
        coverage=dict(sorted(coverage.items())),
        ignore_revs_file=fresh.ignore_revs_file or display,
        ignore_revs_applied=fresh.ignore_revs_applied or ignore is not None,
        truncated=fresh.truncated,
        lines=lines,
        mass=fresh.mass,
    )
    if fresh.truncated:
        return passed, {}, inputs, True
    for path, detail in sink.items():
        blob = blobs.get(path) or git.blob_shas(repo_root, [path]).get(path, "")
        if detail.reusable and blob and detail.commits:
            saved[path] = BlameLines(blob_sha=blob, lines=dict(detail.commits))
    return passed, saved, inputs, False


def blame_argv(
    git_config: GitConfig,
    ignore_revs: Path | None,
    extra_revs: Path | None,
    revision: str | None = None,
) -> list[str]:
    argv = ["git", "blame", "--line-porcelain", "-w", git.mailmap_flag(git_config.use_mailmap)]
    if git_config.detect_moves:
        argv.extend(["-M", "-C"])
    if ignore_revs is not None:
        argv.append(f"--ignore-revs-file={ignore_revs}")
    if extra_revs is not None:
        argv.append(f"--ignore-revs-file={extra_revs}")
    if revision is not None:
        argv.append(revision)
    return argv


def blame_cache_digest(
    blob: str,
    argv: list[str],
    *,
    path: str,
    ignore_bytes: bytes,
    mass: tuple[str, ...],
    mailmap_bytes: bytes,
) -> str:
    flags = tuple(
        part for part in argv if part in {"-w", "-M", "-C", "--use-mailmap", "--no-use-mailmap"}
    )
    payload = json.dumps(
        {
            "blob": blob,
            "path": path,
            "flags": flags,
            "ignore": ignore_bytes.decode("utf-8", "surrogateescape"),
            "mailmap": mailmap_bytes.decode("utf-8", "surrogateescape"),
            "mass": mass,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def fractions_from_counts(counts: Mapping[str, int]) -> dict[str, float]:
    total = sum(counts.values())
    if total == 0:
        return {}
    return dict(sorted((author, count / total) for author, count in counts.items()))


def gather_blame_coverage(
    paths: Iterable[str],
    repo_root: Path,
    *,
    git_config: GitConfig | None = None,
    on_progress: ProgressHook | None = None,
    max_workers: int | None = None,
    mass_revs: tuple[str, ...] | None = None,
    detail_sink: dict[str, BlameDetail] | None = None,
    revision: str | None = None,
    blobs: Mapping[str, str] | None = None,
    report_cache: bool = True,
) -> BlamePass:
    """Return author coverage for `paths`, reusing `mass_revs` when it is passed."""
    git_config = GitConfig() if git_config is None else git_config
    path_list = sorted(paths)
    total = len(path_list)
    if total == 0:
        return BlamePass()
    git.ensure_git_version(repo_root)
    ignore_revs = git.resolve_ignore_revs_file(repo_root, git_config.blame_ignore_revs_file)
    extra_path: Path | None = None
    try:
        mass = (
            mass_revs
            if mass_revs is not None
            else git.mass_refactor_revs(repo_root, git_config.mass_refactor_file_fraction)
        )
        if mass:
            extra_path = git.write_ignore_revs(mass)
        argv = blame_argv(git_config, ignore_revs, extra_path, revision)
        if not git.blame_accepts_mailmap_flags(repo_root):
            argv = git.drop_mailmap_flags(argv)
        mailmap_bytes = git.file_bytes(repo_root / ".mailmap")
        ignore_bytes = git.file_bytes(ignore_revs)
        mass_key = tuple(sorted(mass))
        if blobs is not None:
            resolved_blobs = dict(blobs)
        elif revision is not None:
            resolved_blobs = git.blobs_at_revision(repo_root, revision, path_list)
        else:
            resolved_blobs = git.blob_shas(repo_root, path_list)
        use_cache = git_config.use_blame_cache
        coverage: dict[str, dict[str, float]] = {}
        line_counts: dict[str, dict[str, int]] = {}
        pending_paths: list[tuple[str, str | None]] = []
        hits = 0
        sink = detail_sink if detail_sink is not None else {}
        for path in path_list:
            blob = resolved_blobs.get(path, "")
            digest = (
                blame_cache_digest(
                    blob,
                    argv,
                    path=path,
                    ignore_bytes=ignore_bytes,
                    mass=mass_key,
                    mailmap_bytes=mailmap_bytes,
                )
                if use_cache and blob
                else ""
            )
            cached = read_blame_cache(digest) if digest else None
            if cached is None:
                pending_paths.append((path, digest or None))
                continue
            hits += 1
            line_counts[path] = dict(cached)
            fractions = fractions_from_counts(cached)
            if fractions:
                coverage[path] = fractions
        if report_cache and git_config.report_blame_cache:
            print(
                f"blame cache: hits={hits} misses={len(pending_paths)}",
                file=sys.stderr,
            )
        workers = max_workers if max_workers is not None else min(32, os.cpu_count() or 4)
        workers = max(workers, 1)
        done = hits
        truncated = False
        deadline = BLAME_DEADLINE.get()
        if on_progress is not None and hits == total:
            on_progress(total, total)
        with (
            git.suppress_workdir_mailmap(repo_root, enabled=git_config.use_mailmap),
            ThreadPoolExecutor(max_workers=workers) as pool,
        ):
            pending: list[tuple[str, Future[dict[str, float]]]] = []
            for path, cache_id in pending_paths:
                if deadline is not None and time.monotonic() >= deadline:
                    truncated = True
                    break
                pending.append(
                    (
                        path,
                        pool.submit(
                            blame_for_path,
                            repo_root,
                            path,
                            argv,
                            sink,
                            cache_id if use_cache else None,
                        ),
                    )
                )
            for path, future in pending:
                per_author = future.result()
                done += 1
                if on_progress is not None:
                    on_progress(done, total)
                if per_author:
                    coverage[path] = per_author
                detail = sink.get(path)
                if detail is not None:
                    line_counts[path] = dict(detail.lines)
        display = git.display_ignore_revs_path(repo_root, ignore_revs) if ignore_revs else ""
        return BlamePass(
            coverage=dict(sorted(coverage.items())),
            ignore_revs_file=display,
            ignore_revs_applied=ignore_revs is not None,
            truncated=truncated,
            lines={
                path: dict(sorted(counts.items())) for path, counts in sorted(line_counts.items())
            },
            mass=tuple(mass),
        )
    finally:
        if extra_path is not None:
            extra_path.unlink(missing_ok=True)


def blame_for_path(
    repo_root: Path,
    path: str,
    argv: list[str],
    sink: dict[str, BlameDetail] | None = None,
    digest: str | None = None,
) -> dict[str, float]:
    """Run git blame with the prebuilt argv on one path; return coverage fractions."""
    try:
        result = git.run_git(repo_root, [*argv, "--", path], check=True, paths=True)
    except subprocess.CalledProcessError:
        detail = BlameDetail(fractions={}, commits={}, reusable=False)
    else:
        detail = parse_blame_detail(result.stdout)
        if digest is not None:
            write_blame_cache(digest, detail.lines)
    if sink is not None:
        with _BLAME_SINK_LOCK:
            sink[path] = detail
    return detail.fractions


def parse_blame_output(stdout: str) -> dict[str, float]:
    return parse_blame_detail(stdout).fractions


def parse_blame_detail(stdout: str) -> BlameDetail:
    counts: dict[str, int] = {}
    commits: dict[str, int] = {}
    total = 0
    reusable = True
    current = ""
    for line in stdout.splitlines():
        if "\t" not in line:
            token = line.split(" ", 1)[0]
            if git.FULL_SHA_RE.fullmatch(token):
                current = token
                if token == "0" * 40:
                    reusable = False
                continue
        if not line.startswith("author-mail "):
            continue
        email = line[len("author-mail ") :].strip().strip("<>")
        counts[email] = counts.get(email, 0) + 1
        total += 1
        if not current or not reusable:
            reusable = False
            continue
        commits[current] = commits.get(current, 0) + 1
    if total == 0:
        return BlameDetail(fractions={}, commits={}, reusable=False)
    fractions = dict(sorted((author, count / total) for author, count in counts.items()))
    return BlameDetail(
        fractions=fractions,
        commits=commits,
        reusable=reusable,
        lines=dict(sorted(counts.items())),
    )
