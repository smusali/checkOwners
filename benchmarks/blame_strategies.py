"""Timed blame prototypes. Analyze does not call this module."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_WORKERS = 4


def _git(repo: Path, args: list[str]) -> str:
    git = "git"
    completed = subprocess.run(  # noqa: S603
        [git, *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout


def _paths(repo: Path) -> list[str]:
    listed = _git(repo, ["ls-files", "*.py"])
    return sorted(line for line in listed.splitlines() if line)


def _fractions(counts: dict[str, int]) -> dict[str, float]:
    total = sum(counts.values())
    if total == 0:
        return {}
    return {author: count / total for author, count in sorted(counts.items())}


def _porcelain_counts(stdout: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in stdout.splitlines():
        if not line.startswith("author-mail "):
            continue
        email = line[len("author-mail ") :].strip().strip("<>")
        counts[email] = counts.get(email, 0) + 1
    return counts


def _incremental_counts(stdout: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    pending = 0
    author = ""
    for line in stdout.splitlines():
        parts = line.split(" ")
        if len(parts) == 4 and len(parts[0]) == 40:
            if author and pending:
                counts[author] = counts.get(author, 0) + pending
            pending = int(parts[3])
            author = ""
            continue
        if line.startswith("author-mail "):
            author = line[len("author-mail ") :].strip().strip("<>")
    if author and pending:
        counts[author] = counts.get(author, 0) + pending
    return counts


def _blame_file(repo: Path, path: str, mode: str) -> dict[str, int]:
    flag = "--line-porcelain" if mode == "porcelain" else "--incremental"
    stdout = _git(repo, ["blame", flag, "-w", "-M", "-C", "--use-mailmap", "--", path])
    if mode == "porcelain":
        return _porcelain_counts(stdout)
    return _incremental_counts(stdout)


def _pool(repo: Path, paths: list[str], mode: str) -> dict[str, dict[str, float]]:
    coverage: dict[str, dict[str, float]] = {}
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futures = [(path, pool.submit(_blame_file, repo, path, mode)) for path in paths]
        for path, future in futures:
            coverage[path] = _fractions(future.result())
    return coverage


def porcelain(repo: Path, paths: list[str]) -> dict[str, dict[str, float]]:
    return _pool(repo, paths, "porcelain")


def incremental(repo: Path, paths: list[str]) -> dict[str, dict[str, float]]:
    return _pool(repo, paths, "incremental")


def numstat(repo: Path) -> dict[str, dict[str, float]]:
    stdout = _git(
        repo,
        ["log", "--numstat", "-M", "--pretty=format:%aE%x00%H", "--", "*.py"],
    )
    counts: dict[str, dict[str, int]] = {}
    author = ""
    for line in stdout.splitlines():
        if "\x00" in line:
            author = line.split("\x00", 1)[0]
            continue
        fields = [*line.split("\t"), "", "", ""]
        added, _removed, path = fields[:3]
        if not author or not path.endswith(".py") or not added.isdigit():
            continue
        per_path = counts.setdefault(path, {})
        per_path[author] = per_path.get(author, 0) + int(added)
    return {path: _fractions(authors) for path, authors in counts.items()}


def pygit2_blame(repo: Path, paths: list[str]) -> dict[str, dict[str, float]] | None:
    try:
        import pygit2  # noqa: PLC0415
    except ImportError:
        return None
    repository = pygit2.Repository(str(repo))
    coverage: dict[str, dict[str, float]] = {}

    def one(path: str) -> dict[str, float]:
        counts: dict[str, int] = {}
        try:
            blamed = repository.blame(path)
        except pygit2.GitError:
            return {}
        for hunk in blamed:
            email = hunk.final_signature.email or ""
            counts[email] = counts.get(email, 0) + hunk.lines_in_hunk
        return _fractions(counts)

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futures = [(path, pool.submit(one, path)) for path in paths]
        for path, future in futures:
            coverage[path] = future.result()
    return coverage


def _same(left: dict[str, dict[str, float]], right: dict[str, dict[str, float]]) -> bool:
    if set(left) != set(right):
        return False
    for path, authors in left.items():
        other = right[path]
        if set(authors) != set(other):
            return False
        for author, share in authors.items():
            if abs(share - other[author]) > 1e-9:
                return False
    return True


def measure(repo: Path) -> dict[str, object]:
    paths = _paths(repo)
    started = time.perf_counter()
    porcelain_coverage = porcelain(repo, paths)
    porcelain_seconds = time.perf_counter() - started
    started = time.perf_counter()
    incremental_coverage = incremental(repo, paths)
    incremental_seconds = time.perf_counter() - started
    started = time.perf_counter()
    numstat_coverage = numstat(repo)
    numstat_seconds = time.perf_counter() - started
    started = time.perf_counter()
    pygit2_coverage = pygit2_blame(repo, paths)
    pygit2_seconds = time.perf_counter() - started
    incremental_match = _same(porcelain_coverage, incremental_coverage)
    numstat_match = _same(porcelain_coverage, numstat_coverage)
    pygit2_match = pygit2_coverage is not None and _same(porcelain_coverage, pygit2_coverage)
    return {
        "files": len(paths),
        "porcelain_seconds": round(porcelain_seconds, 4),
        "incremental_seconds": round(incremental_seconds, 4),
        "incremental_matches": incremental_match,
        "numstat_seconds": round(numstat_seconds, 4),
        "numstat_matches": numstat_match,
        "pygit2_seconds": None if pygit2_coverage is None else round(pygit2_seconds, 4),
        "pygit2_matches": pygit2_match,
    }


def _generate(root: Path, files: int) -> None:
    root.mkdir(parents=True)
    git = "git"
    subprocess.run(  # noqa: S603
        [git, "init"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    for index in range(files):
        path = root / "src" / f"file_{index}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"def value_{index}():\n    return {index}\n", encoding="utf-8")
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = "2026-10-01T12:00:00+00:00"
    env["GIT_COMMITTER_DATE"] = "2026-10-01T12:00:00+00:00"
    git = "git"
    subprocess.run(  # noqa: S603
        [git, "-c", "user.name=Alice", "-c", "user.email=alice@example.com", "add", "-A"],
        cwd=root,
        check=True,
        capture_output=True,
        env=env,
    )
    subprocess.run(  # noqa: S603
        [
            git,
            "-c",
            "user.name=Alice",
            "-c",
            "user.email=alice@example.com",
            "commit",
            "-m",
            "seed",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        env=env,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmarks/blame_strategies.py")
    parser.add_argument("--files", type=int, default=0)
    parser.add_argument("--ci", action="store_true")
    parser.add_argument("--repo", type=Path)
    args = parser.parse_args(argv)
    if args.repo is not None:
        print(json.dumps(measure(args.repo), indent=2))
        return 0
    with tempfile.TemporaryDirectory(prefix="checkowners-blame-") as tmp:
        repo = Path(tmp) / "repo"
        if args.ci:
            from benchmarks.harness import generate_ci_repo  # noqa: PLC0415

            generate_ci_repo(repo, files=80, commits=8)
        else:
            _generate(repo, args.files or 80)
        print(json.dumps(measure(repo), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
