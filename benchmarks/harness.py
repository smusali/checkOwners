"""Shell out to checkowners analyze and compare runtimes."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TypedDict

_ROOT = Path(__file__).resolve().parent
_MANIFEST = _ROOT / "manifest.json"
_SHA = re.compile(r"[0-9a-f]{40}")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_CLI = (
    "import sys\n"
    "from checkowners.cli import main\n"
    "sys.argv = ['checkowners', *sys.argv[1:]]\n"
    "raise SystemExit(main())\n"
)
_WORKERS = 4
_CONFIG = f"""\
version: 1
analysis:
  confidence_threshold: 0.0
  min_commits: 1
  max_git_workers: {_WORKERS}
github:
  api_enabled: false
"""
_COUNT_KEYS = (
    "git_commands",
    "git_commands_warm",
    "git_commands_incremental",
    "api_requests",
)
_WALL_KEYS = ("cold_seconds", "warm_seconds", "incremental_seconds")


class BenchEnvironment(TypedDict):
    machine: str
    cpu_count: int
    python: str
    git: str
    max_git_workers: int


class BenchResult(TypedDict):
    tier: str
    cold_seconds: float
    warm_seconds: float
    incremental_seconds: float
    peak_rss_bytes: int
    git_commands: int
    git_commands_warm: int
    git_commands_incremental: int
    api_requests: int
    cache_bytes: int
    files: int
    commits: int
    environment: BenchEnvironment


def _read_object(path: Path) -> dict[str, object]:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        msg = f"{path} is not an object"
        raise SystemExit(msg)
    payload: dict[str, object] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            msg = f"{path} has a non-string key"
            raise SystemExit(msg)
        payload[key] = value
    return payload


def _require_float(payload: dict[str, object], key: str) -> float:
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"{key} is not a number"
        raise SystemExit(msg)
    return float(value)


def _require_int(payload: dict[str, object], key: str) -> int:
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{key} is not an integer"
        raise SystemExit(msg)
    return value


def _limits(manifest: dict[str, object]) -> tuple[float, float]:
    return (
        _require_float(manifest, "variance"),
        _require_float(manifest, "regression_factor"),
    )


def _tiers(manifest: dict[str, object]) -> dict[str, dict[str, object]]:
    raw = manifest["tiers"]
    if not isinstance(raw, dict):
        msg = "tiers is not an object"
        raise SystemExit(msg)
    tiers: dict[str, dict[str, object]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            msg = "a tier entry is malformed"
            raise SystemExit(msg)
        tier: dict[str, object] = {}
        for field, item in value.items():
            if not isinstance(field, str):
                msg = "a tier field is not a string"
                raise SystemExit(msg)
            tier[field] = item
        tiers[key] = tier
    return tiers


def peak_rss(stderr: str) -> int:
    mac = re.search(r"^\s*(\d+)\s+maximum resident set size\s*$", stderr, re.MULTILINE)
    if mac:
        return int(mac.group(1))
    linux = re.search(r"Maximum resident set size \(kbytes\):\s*(\d+)", stderr)
    if linux:
        return int(linux.group(1)) * 1024
    msg = "peak RSS was not reported"
    raise ValueError(msg)


def variance_failures(
    left: dict[str, object],
    right: dict[str, object],
    *,
    variance: float,
) -> list[str]:
    failures: list[str] = []
    for key in _COUNT_KEYS:
        if _require_int(left, key) != _require_int(right, key):
            failures.append(f"{key} differs")
    for key in _WALL_KEYS:
        first = _require_float(left, key)
        second = _require_float(right, key)
        scale = max(first, second, 1e-9)
        if abs(first - second) / scale > variance:
            failures.append(f"{key} differs by more than {variance:.0%}")
    return failures


def regression_failures(
    sample: dict[str, object],
    baseline: dict[str, object],
    *,
    factor: float,
) -> list[str]:
    recorded = baseline.get("ci")
    if not isinstance(recorded, dict):
        msg = "baseline has no ci object"
        raise SystemExit(msg)
    ci: dict[str, object] = {}
    for key, value in recorded.items():
        if isinstance(key, str):
            ci[key] = value
    limit = _require_float(ci, "cold_seconds") * factor
    if _require_float(sample, "cold_seconds") > limit:
        return [f"cold_seconds exceeds baseline * {factor}"]
    return []


def _git_bin() -> str:
    found = shutil.which("git")
    if found is None:
        msg = "git is not installed"
        raise SystemExit(msg)
    return found


def _git(repo: Path, args: list[str], env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(  # noqa: S603
        [_git_bin(), *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _commit(repo: Path, message: str, date: str) -> None:
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = date
    env["GIT_COMMITTER_DATE"] = date
    for args in (
        ["-c", "user.name=Alice", "-c", "user.email=alice@example.com", "add", "-A"],
        ["-c", "user.name=Alice", "-c", "user.email=alice@example.com", "commit", "-m", message],
    ):
        _git(repo, args, env)


def generate_ci_repo(root: Path, *, files: int, commits: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, ["init"])
    config = root / ".github"
    config.mkdir()
    (config / "checkowners.yml").write_text(_CONFIG, encoding="utf-8")
    per_commit = max(1, files // commits)
    written = 0
    for index in range(commits):
        batch = per_commit if index < commits - 1 else files - written
        for offset in range(batch):
            number = written + offset
            path = root / "src" / f"file_{number}.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"def value_{number}():\n    return {number}\n",
                encoding="utf-8",
            )
        written += batch
        day = index + 1
        _commit(root, f"add batch {index}", f"2026-10-{day:02d}T12:00:00+00:00")


def _environment(workers: int) -> BenchEnvironment:
    git = _git(Path.cwd(), ["--version"]).strip()
    cpu = os.cpu_count() or 1
    return {
        "machine": platform.platform(),
        "cpu_count": cpu,
        "python": platform.python_version(),
        "git": git,
        "max_git_workers": workers,
    }


def _time_prefix() -> list[str]:
    if sys.platform == "darwin":
        return ["/usr/bin/time", "-l"]
    if sys.platform == "linux":
        return ["/usr/bin/time", "-v"]
    msg = f"no peak RSS timer for {sys.platform}"
    raise SystemExit(msg)


def _count_metrics(path: Path) -> tuple[int, int]:
    git_count = 0
    api_count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if line == "git":
            git_count += 1
        elif line == "api":
            api_count += 1
    return git_count, api_count


def _cache_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


def _analyze(repo: Path, state: Path, metrics: Path) -> tuple[float, int, int, int]:
    metrics.parent.mkdir(parents=True, exist_ok=True)
    metrics.write_text("", encoding="utf-8")
    env = os.environ.copy()
    for name in ("SOURCE_DATE_EPOCH", "CHECKOWNERS_CONFIG", "GITHUB_TOKEN", "GH_TOKEN"):
        env.pop(name, None)
    env["CHECKOWNERS_STATE_DIR"] = str(state)
    env["CHECKOWNERS_METRICS_PATH"] = str(metrics)
    started = time.perf_counter()
    completed = subprocess.run(  # noqa: S603
        [*_time_prefix(), sys.executable, "-c", _CLI, "analyze", "--json"],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    elapsed = time.perf_counter() - started
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        msg = f"analyze exited {completed.returncode}: {detail}"
        raise SystemExit(msg)
    git_count, api_count = _count_metrics(metrics)
    return elapsed, git_count, api_count, peak_rss(completed.stderr)


def _measure_repo(repo: Path, tier: str, *, files: int, commits: int) -> BenchResult:
    with tempfile.TemporaryDirectory(prefix="checkowners-bench-") as tmp:
        work = Path(tmp)
        state = work / "state"
        cold, git_cold, api_cold, rss = _analyze(repo, state, work / "cold.txt")
        warm, git_warm, _api_warm, _rss_warm = _analyze(repo, state, work / "warm.txt")
        extra = repo / "src" / "extra.py"
        extra.write_text("def extra():\n    return 1\n", encoding="utf-8")
        _commit(repo, "add extra", "2026-10-20T12:00:00+00:00")
        incremental, git_incremental, _api_incremental, _rss_incremental = _analyze(
            repo,
            state,
            work / "incremental.txt",
        )
        cache = _cache_bytes(state)
    return {
        "tier": tier,
        "cold_seconds": round(cold, 4),
        "warm_seconds": round(warm, 4),
        "incremental_seconds": round(incremental, 4),
        "peak_rss_bytes": rss,
        "git_commands": git_cold,
        "git_commands_warm": git_warm,
        "git_commands_incremental": git_incremental,
        "api_requests": api_cold,
        "cache_bytes": cache,
        "files": files,
        "commits": commits,
        "environment": _environment(_WORKERS),
    }


def run_tier(tier: str, output: Path) -> BenchResult:
    manifest = _read_object(_MANIFEST)
    spec = _tiers(manifest).get(tier)
    if spec is None:
        msg = f"unknown tier {tier}"
        raise SystemExit(msg)
    if spec.get("kind") != "synthetic":
        msg = f"{tier} is not a local tier"
        raise SystemExit(msg)
    files = _require_int(spec, "files")
    commits = _require_int(spec, "commits")
    with tempfile.TemporaryDirectory(prefix="checkowners-ci-repo-") as tmp:
        repo = Path(tmp) / "repo"
        generate_ci_repo(repo, files=files, commits=commits)
        result = _measure_repo(repo, tier, files=files, commits=commits)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def _public_record(name: str, spec: dict[str, object], work: Path) -> dict[str, object]:
    repository = spec.get("repository")
    sha = spec.get("sha")
    if not isinstance(repository, str) or not isinstance(sha, str):
        return {"tier": name, "skipped": "unpinned"}
    if _REPO.fullmatch(repository) is None or _SHA.fullmatch(sha) is None:
        return {"tier": name, "skipped": "unpinned"}
    dest = work / name
    git_bin = _git_bin()
    subprocess.run(  # noqa: S603
        [git_bin, "clone", f"https://github.com/{repository}.git", str(dest)],
        check=True,
    )
    subprocess.run([git_bin, "checkout", sha], cwd=dest, check=True)  # noqa: S603
    files = len(_git(dest, ["ls-files"]).splitlines())
    commits = int(_git(dest, ["rev-list", "--count", "HEAD"]).strip())
    measured = _measure_repo(dest, name, files=files, commits=commits)
    return dict(measured)


def run_public(output: Path) -> None:
    manifest = _read_object(_MANIFEST)
    runs: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="checkowners-public-") as tmp:
        work = Path(tmp)
        for name, spec in _tiers(manifest).items():
            if spec.get("kind") != "public":
                continue
            runs.append(_public_record(name, spec, work))
    payload = {"runs": runs}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def self_check() -> int:
    manifest = _read_object(_MANIFEST)
    _variance, factor = _limits(manifest)
    baseline: dict[str, object] = {"ci": {"cold_seconds": 1.0}}
    accepted = regression_failures({"cold_seconds": 1.0}, baseline, factor=factor)
    slowed = regression_failures({"cold_seconds": 1.0 * factor + 0.01}, baseline, factor=factor)
    if accepted or not slowed:
        print("slowed sample was accepted", file=sys.stderr)
        return 1
    return 0


def _compare(args: argparse.Namespace) -> int:
    manifest = _read_object(_MANIFEST)
    variance, factor = _limits(manifest)
    failures: list[str] = []
    if args.left is not None or args.right is not None:
        if args.left is None or args.right is None:
            print("compare needs both --left and --right", file=sys.stderr)
            return 2
        failures.extend(
            variance_failures(
                _read_object(args.left),
                _read_object(args.right),
                variance=variance,
            )
        )
    if args.baseline is not None or args.sample is not None:
        if args.baseline is None or args.sample is None:
            print("compare needs both --baseline and --sample", file=sys.stderr)
            return 2
        failures.extend(
            regression_failures(
                _read_object(args.sample),
                _read_object(args.baseline),
                factor=factor,
            )
        )
    if args.left is None and args.baseline is None:
        print("compare needs two runs or a baseline and a sample", file=sys.stderr)
        return 2
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmarks/harness.py")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--tier", required=True)
    run.add_argument("--output", required=True, type=Path)
    compare = commands.add_parser("compare")
    compare.add_argument("--left", type=Path)
    compare.add_argument("--right", type=Path)
    compare.add_argument("--baseline", type=Path)
    compare.add_argument("--sample", type=Path)
    commands.add_parser("self-check")
    public = commands.add_parser("public")
    public.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "run":
        run_tier(args.tier, args.output)
        return 0
    if args.command == "compare":
        return _compare(args)
    if args.command == "self-check":
        return self_check()
    if args.command == "public":
        run_public(args.output)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
