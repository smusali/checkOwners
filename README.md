# CheckOwners

[![CI](https://github.com/smusali/checkowners/actions/workflows/ci.yml/badge.svg)](https://github.com/smusali/checkowners/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/smusali/checkowners/graph/badge.svg)](https://codecov.io/gh/smusali/checkowners)
[![PyPI](https://img.shields.io/pypi/v/checkowners.svg)](https://pypi.org/project/checkowners/)
[![PyPI downloads](https://static.pepy.tech/badge/checkowners/month)](https://pepy.tech/project/checkowners)
[![Python versions](https://img.shields.io/pypi/pyversions/checkowners.svg)](https://pypi.org/project/checkowners/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/smusali/checkowners/blob/main/LICENSE)

Infer CODEOWNERS from git history with confidence scoring, a knowledge graph, expertise decay detection, team topology inference, review load balancing, and onboarding paths. Core inference is deterministic git analysis; no LLM is in that path. CI-native: structured JSON output, GITHUB_OUTPUT integration, composite GitHub Action.

This repository moved here from a previous GitHub organization; Sigstore attestations for 0.5.0 and earlier record that earlier publisher.

> CheckOwners treats code ownership as a confidence-scored spectrum rather than a static binary declaration.

> No other open-source tool combines git-history inference, calibrated per-path confidence, pattern-aware drift with severity tiers, and knowledge-risk reporting behind a single CI-native JSON contract.

Inference is deterministic git analysis; the scoring heuristics live in `analyze.py` and are auditable. The codebase has been built with agent assistance. Every AI-assisted change is human-reviewed, tested, and signed off.

## See it

`analyze` → `generate` → `drift` on this repository:

![checkowners analyze, generate, and drift](https://github.com/smusali/checkowners/raw/main/examples/demo.svg)

The composite Action keeps one comment on same-repo pull requests. When drift clears it looks like this (from the dogfood workflow on this repo):

![CheckOwners pull-request comment](https://github.com/smusali/checkowners/raw/main/examples/pr-comment.svg)

Trimmed JSON for the same run is in [examples/sample-output.md](https://github.com/smusali/checkowners/blob/main/examples/sample-output.md). Reference configs live under [examples/](https://github.com/smusali/checkowners/tree/main/examples).

## How it works

`checkowners analyze` reads `git log` and `git blame` (in parallel, only over paths that can actually produce owners; a 24k-commit, 12k-file production monorepo completed a 365-day analyze in under three minutes on the 0.5.0 dogfood run) into a confidence-scored ownership map cached per repo under `~/.checkowners/`. Commit emails resolve to GitHub `@handles` (noreply emails locally with no token, the rest via the GitHub API), and same-person identities merge so qualified owner counts count people, not email addresses. From that map, `generate` writes a CODEOWNERS file with uniform directories consolidated into `dir/` rules, and `drift` compares the committed file against inference using real CODEOWNERS pattern matching (directory rules, globs, last-match-wins). `qualified-owners`, `decay`, `topology`, `balance`, `onboard`, and `trends` emit their own reports. In CI, the composite GitHub Action runs the same flow, writes structured `GITHUB_OUTPUT` and a job summary, and maintains a single up-to-date PR comment on same-repo pull requests. See [docs/USAGE.md](https://github.com/smusali/checkowners/blob/main/docs/USAGE.md) for the full pipeline and a diagram.

## Installation

Requires Python 3.11 through 3.14.

```bash
pip install checkowners               # core CLI (pure git, zero API deps)
pip install "checkowners[graph]"      # + networkx-backed graph / topology / onboard
pip install "checkowners[github]"     # + GitHub API handle/team/review resolution
pip install "checkowners[all]"        # everything
```

## Quick start

```bash
# Confidence-scored ownership inference
checkowners analyze

# Write CODEOWNERS with owners ranked by expertise confidence
# (refuses to overwrite a hand-written file unless you pass --force)
checkowners generate

# Compare inferred vs current CODEOWNERS, ranked by confidence delta
checkowners drift

# Validate syntax (no git access)
checkowners validate
```

All commands accept `--json` (except `graph`, which exports DOT via `--export dot`) and persist their results per repo under `~/.checkowners/` so downstream commands can reuse the analysis.

## Commands

| Command | What it does |
|---------|--------------|
| `checkowners analyze` | Infer ownership with confidence scores, qualified owner count, decay warnings |
| `checkowners generate` | Write CODEOWNERS, ordered by confidence; optional inline annotations |
| `checkowners print` | Print inferred ownership to stdout |
| `checkowners validate` | Validate existing CODEOWNERS syntax |
| `checkowners drift` | Compare inferred vs current; severity + max confidence delta |
| `checkowners notify` | POST drift to a webhook gated by `severity_threshold` |
| `checkowners sync` | Generate CODEOWNERS and commit the result |
| `checkowners expertise <path>` | Per-path expertise ranking |
| `checkowners decay` | Detect dormant owners; recommend transfers |
| `checkowners graph [--export dot]` | Render the contributor / file / team graph |
| `checkowners qualified-owners [<path>] [--all]` | Per-path qualified owner count (capped by `top_n_owners`) with backup-reviewer suggestions. `bus-factor` is a deprecated alias pending redefinition |
| `checkowners topology` | Infer team boundaries from commit co-occurrence |
| `checkowners balance` | Detect overloaded reviewers and propose rebalancing |
| `checkowners onboard <path>` | Generate a learning path from broad-ownership to deep-expertise files |
| `checkowners trends [--periods N] [--period-days D]` | Show how ownership confidence and qualified owner count have evolved over time |
| `checkowners github-action` | Run the full CI flow and write `GITHUB_OUTPUT`; used by the composite Action |

## Documentation

- [docs/USAGE.md](https://github.com/smusali/checkowners/blob/main/docs/USAGE.md): full configuration reference, confidence scoring formula, drift severity tiers, GitHub Actions integration, comparison table.
- [docs/FAQ.md](https://github.com/smusali/checkowners/blob/main/docs/FAQ.md): identity (usernames vs emails, teams + subteams), GitHub API access, file locations, tuning, troubleshooting.
- [docs/CONTRIBUTING.md](https://github.com/smusali/checkowners/blob/main/docs/CONTRIBUTING.md): dev setup, commands, conventional commits, code conventions, PR workflow.
- [ROADMAP.md](https://github.com/smusali/checkowners/blob/main/ROADMAP.md): milestones and how to pick an issue. The [action-item register](https://github.com/smusali/checkowners/blob/main/docs/ACTION_ITEMS.md) is the full list.
- [Good first issues](https://github.com/smusali/checkowners/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22) · [Discussions](https://github.com/smusali/checkowners/discussions)
- [docs/CHANGELOG.md](https://github.com/smusali/checkowners/blob/main/docs/CHANGELOG.md): release history.

## License

MIT
