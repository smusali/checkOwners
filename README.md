# checkowners

[![CI](https://github.com/fortyOneTech/checkowners/actions/workflows/ci.yml/badge.svg)](https://github.com/fortyOneTech/checkowners/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/checkowners.svg)](https://pypi.org/project/checkowners/)
[![PyPI downloads](https://img.shields.io/pypi/dm/checkowners.svg)](https://pypi.org/project/checkowners/)
[![Python versions](https://img.shields.io/pypi/pyversions/checkowners.svg)](https://pypi.org/project/checkowners/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/smusali/checkowners/blob/main/LICENSE)

Infer CODEOWNERS from git history with confidence scoring, a knowledge graph, expertise decay detection, bus factor analysis, team topology inference, review load balancing, and onboarding paths. Pure git, no LLMs. CI-native: structured JSON output, GITHUB_OUTPUT integration, composite GitHub Action.

> Ownership is not binary. checkOwners is the first CODEOWNERS tool that treats it as a confidence-scored spectrum and surfaces the second-order risks (bus factor, expertise decay, team topology) that come with it.

## How it works

`checkowners analyze` reads `git log` and `git blame` (in parallel, only over paths that can actually produce owners; a 24k-commit monorepo analyzes in under two minutes) into a confidence-scored ownership map cached per repo under `~/.checkowners/`. Commit emails resolve to GitHub `@handles` (noreply emails locally with no token, the rest via the GitHub API), and same-person identities merge so bus factors count people, not email addresses. From that map, `generate` writes a CODEOWNERS file with uniform directories consolidated into `dir/` rules, and `drift` compares the committed file against inference using real CODEOWNERS pattern matching (directory rules, globs, last-match-wins). `bus-factor`, `decay`, `topology`, `balance`, `onboard`, and `trends` emit their own reports. In CI, the composite GitHub Action runs the same flow, writes structured `GITHUB_OUTPUT`, and maintains a single up-to-date PR comment. See [docs/USAGE.md](https://github.com/smusali/checkowners/blob/main/docs/USAGE.md) for the full pipeline and a diagram.

## Installation

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
| `checkowners analyze` | Infer ownership with confidence scores, bus factor, decay warnings |
| `checkowners generate` | Write CODEOWNERS, ordered by confidence; optional inline annotations |
| `checkowners print` | Print inferred ownership to stdout |
| `checkowners validate` | Validate existing CODEOWNERS syntax |
| `checkowners drift` | Compare inferred vs current; severity + max confidence delta |
| `checkowners notify` | POST drift to a webhook gated by `severity_threshold` |
| `checkowners sync` | Generate CODEOWNERS and commit the result |
| `checkowners expertise <path>` | Per-path expertise ranking |
| `checkowners decay` | Detect dormant owners; recommend transfers |
| `checkowners graph [--export dot]` | Render the contributor / file / team graph |
| `checkowners bus-factor [<path>] [--all]` | Per-path bus factor with backup-reviewer suggestions |
| `checkowners topology` | Infer team boundaries from commit co-occurrence |
| `checkowners balance` | Detect overloaded reviewers and propose rebalancing |
| `checkowners onboard <path>` | Generate a learning path from broad-ownership to deep-expertise files |
| `checkowners trends [--periods N] [--period-days D]` | Show how ownership confidence and bus factor have evolved over time |
| `checkowners github-action` | Run the full CI flow and write `GITHUB_OUTPUT`; used by the composite Action |

## Documentation

- [docs/USAGE.md](https://github.com/smusali/checkowners/blob/main/docs/USAGE.md): full configuration reference, confidence scoring formula, drift severity tiers, GitHub Actions integration, comparison table.
- [docs/FAQ.md](https://github.com/smusali/checkowners/blob/main/docs/FAQ.md): identity (usernames vs emails, teams + subteams), GitHub API access, file locations, tuning, troubleshooting.
- [docs/CONTRIBUTING.md](https://github.com/smusali/checkowners/blob/main/docs/CONTRIBUTING.md): dev setup, commands, conventional commits, code conventions, PR workflow.
- [docs/CHANGELOG.md](https://github.com/smusali/checkowners/blob/main/docs/CHANGELOG.md): release history.

## License

MIT
