# Benchmarks

`benchmarks/harness.py` shells out to `checkowners analyze`. It does not import the scorers.

## What a run records

Each result has cold runtime, warm runtime, peak RSS, git command count, GitHub API request count, cache size, and incremental runtime. Incremental runtime is another analyze after one new commit. Peak RSS comes from `time -l` on macOS and `time -v` on Linux. The environment block records the machine, CPU count, Python, git version, and `max_git_workers`.

`CHECKOWNERS_METRICS_PATH` makes each `git` command and each GitHub API request append one line. A normal analyze does not open that file.

## Variance

Two runs on the same commit must match git command count exactly, and wall time within 15%. `compare --baseline` fails when cold time exceeds the recorded `ci` baseline times 1.15.

```text
python benchmarks/harness.py run --tier ci --output /tmp/bench-a.json
python benchmarks/harness.py run --tier ci --output /tmp/bench-b.json
python benchmarks/harness.py compare --left /tmp/bench-a.json --right /tmp/bench-b.json
python benchmarks/harness.py compare --baseline benchmarks/baseline.json --sample /tmp/bench-a.json
python benchmarks/harness.py self-check
```

`self-check` feeds the comparator a slowed sample and requires that sample to fail.

## Tiers

`ci` is a synthetic repository the harness generates (80 Python files across 8 commits). Pull-request CI runs that tier twice, compares the runs, and runs the self-check. It does not clone public repositories.

`1k`, `10k`, `100k`, `monorepo`, and `deep` are candidates: a small library, `django/django`, `torvalds/linux`, `kubernetes/kubernetes`, and `git/git`. A tier stays unpinned until a run confirms its file count and commit count. The manifest records that sha only then. Unpinned tiers are skipped. The manual workflow writes `benchmarks/results/<git-sha>.json`.

## Recorded figures

J1, measured before this harness on 2026-10-24, used 600 one-line Python files across 20 commits and one later commit that changed two files. The cold pass took 1.99 seconds and blamed all 600 files. The warm pass took 0.34 seconds and blamed the two changed files.

The `ci` object in `baseline.json` is this harness's first `ci` run. The `j2` object is the same tier after the blame cache. The `j3` object is the blame-strategy timing. Production blame stayed on `git blame --line-porcelain`. The `c9` object is the same tier after change survival. Historical blame of an unchanged blob hits the blame cache.
