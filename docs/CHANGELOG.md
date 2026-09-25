# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Each dated heading is the UTC calendar day that version was published to PyPI (`YYYY-MM-DD`). `Unreleased` has no date.

## [Unreleased]

Next cycle is `0.7.0` (ownership risk and scale). See the [public roadmap](../ROADMAP.md).

### Added
- Recency uses each path's median commit gap as its half-life, clamped to a floor and ceiling, with `scoring.recency_strategy: fixed` keeping one half-life for every path, `analysis.lookback_days: adaptive` reading history until the analysis instant, and each owner reporting `active_expertise`, `historical_expertise`, `maintenance_recency`, and a continuity-risk status of `stable`, `inactive`, `superseded`, or `departed`.
- `checkowners simulate --remove` reports files that lose their only author, files left with one author, fully orphaned directories, the repository truck factor before and after, and candidate backup reviewers, using the cached map only.
- `qualified-owners` reports a criticality-weighted distribution of `top_owner_share` (`minimum`, `p10`, `median`, `p90`, `critical_path_risk`, and `knowledge_at_risk`), with `criticality` mapping path globs to weights in `(0, 1]` and an empty map setting `criticality_incomplete`.
- Per-path `risk` reports inverse-Herfindahl `effective_owners`, Shannon entropy, HHI, `truck_factor_50`, `truck_factor_75`, `truck_factor_90`, minor-contributor share, and major-contributor count from every `ownership_score` above the confidence threshold, with `risk.truck_factor_thresholds` defaulting to `[0.50, 0.75, 0.90]` and echoed on the risk object, while `top_n_owners` still caps only the displayed owner list and `qualified_owner_count`.
- The composite Action caches pip and `~/.checkowners`, with the `cache` input defaulting to `"true"` and a restored state file reused only when its commit is `HEAD` and the config hash and model versions match.

## [0.6.0] - 2026-09-24

Correctness and trust: scored ownership, schema 1, privacy controls, and supply-chain checks. Inference stays local git.

### Added
- [docs/METHODOLOGY.md](METHODOLOGY.md) states the formulas, truck-factor divergence, terminology, golden beliefs, and principles, [docs/limitations.md](limitations.md) states when the tool can be wrong, and CI fails if a shipped metric key is missing from the methodology page.
- The README leads with a drift example, install, run, CI, the offline guarantee, Trusted Publishing, and Sigstore, and adds `examples/demo.gif`, `examples/pr-comment.png`, and `examples/sample-CODEOWNERS`.
- `.pre-commit-hooks.yaml` exposes `checkowners-validate` and `checkowners-drift`, this repository runs both, and the hooks are documented in [docs/USAGE.md](USAGE.md#pre-commit).
- CI runs CodeQL, dependency review, pip-audit, and zizmor, Dependabot updates Python dependencies, and each GitHub Release carries a CycloneDX SBOM and a signed provenance attestation.
- Human reports, the job summary, and pull-request comments state that CheckOwners is a knowledge-risk tool, not a performance-measurement tool, and [docs/PRIVACY.md](PRIVACY.md) records the data flow, cache, `cache purge`, and threat model.
- A public CODEOWNERS compatibility corpus (`corpus/compatibility.jsonl`) and a license-checked parser corpus (`corpus/realworld.jsonl`) ship under `corpus/README.md`.
- Privacy controls add `output.anonymize`, `output.aggregate_only`, `privacy.redact_emails` with `--redact-emails`, `identity.mode` (`handle`, `email`, or `hashed`), and `contributors.exclude`, and the Action input `include_balance` defaults to false.
- On-disk state is schema 1 with tokenized emails, atomic locked writes, reuse only when HEAD and the scoring config still match, `cache path|info|clear|purge`, a 256 MiB cap, and `--offline` with no network calls.
- Analysis completeness is a run-level score with `analysis_gaps` that scales evidence quality only, and `--fail-on-incomplete` or `policy.incomplete_analysis.fail` exits 3 when a runtime, worker, API, or rate-limit bound leaves the run incomplete.
- `--json` uses schema `1` ([docs/schemas/commands-v1.json](schemas/commands-v1.json)) with `schema_version`, `checkowners_version`, `models`, `repository`, `head_sha`, `generated_at`, and `analysis_completeness`, plus `identity`, flat `signals`, per-path `risk`, nested drift `recommendation`, optional GitHub evidence stamps, and Action summaries at integer `schema_version: 1`.
- Configuration requires `version: 1` and pins `ownership-v1`, `risk-v1`, and `topology-v1`, refuses any other version or unimplemented keys such as `criticality`, and ignores caches whose `models` do not match.
- `checkowners baseline create` writes accepted findings identified by rule, path, and owner set, and `--baseline`, `drift.baseline_file`, `CHECKOWNERS_BASELINE`, the Action `baseline` input, and reasoned `suppressions` keep CI failing only on new findings.
- `checkowners explain PATH` decomposes one path's inferred ownership, `checkowners owners PATH` (`who`) prints a ranked list, and both accept `--json` with `schema_version: "1"` without reading or writing the cache.
- Analyze excludes `linguist-generated` and `linguist-vendored` paths from `.gitattributes` when `analysis.respect_gitattributes` is true, reports `excluded_gitattributes` and `excluded_static`, and falls back to `paths.exclude`.
- Blame honors `.git-blame-ignore-revs`, ignores whitespace, detects moves and copies, omits mass-refactor commits above `git.mass_refactor_file_fraction` (default `0.5`), and requires Git 2.23 or newer.
- `.mailmap` is applied first when `identity.mailmap` is true, and analyze reports whether a mapping file was found.
- `generate` and `sync` fail when re-resolved CODEOWNERS owners differ from the intended set, unless `output.verify_round_trip` is false.
- `generate` warns at 2 MB and refuses above `output.max_bytes` (default 2_500_000) without `--force`, and `--json` includes `bytes_written` and `rules_written`.
- `checkowners explain-path <path>` shows the winning CODEOWNERS rule and the full match chain.
- `generate` and `sync` refuse sanitized `*` rules that would assign a different owner set, unless `--allow-broad-patterns` or `output.allow_broad_patterns` is set, and `--json` records `broad_patterns`.
- Golden ownership tests in `tests/test_golden.py` record what `ownership-v1` concludes for pinned git histories under a fixed as-of.

### Changed
- Action output `checkowners_drift` is now `drift_summary` in `github-action --json` and `GITHUB_OUTPUT`, and command JSON and Action summaries both use schema `1`.
- Commands share exit codes 0 clean, 1 internal, 2 usage, 3 findings, and 4 integration failure, with `--exit-zero` clearing only code 3 and `--fail-on-incomplete` exiting 3 when completeness is below 1.
- The composite Action runs `checkowners github-action` once for drift, qualified owners, decay, the job summary, and `GITHUB_OUTPUT`.
- Recency, decay, and lookback age against `--as-of`, else `SOURCE_DATE_EPOCH`, else the HEAD committer time, JSON emits `head_sha` and `generated_at`, and `drift.hysteresis_runs` holds severity flips.
- Ownership scoring renormalizes over available signals into `ownership_score` on `[0, 1]` with a separate `evidence_quality`, stamps `models.ownership: ownership-v1`, and no longer caps offline scores at `0.85`.
- The default `qualification.strategy` is `adaptive`, so commit count is evidence with Bayesian shrinkage rather than an eligibility gate, while `threshold` keeps a commit-count gate and undamped frequency.

### Fixed
- `requirements-dev.lock` pins `pyyaml-ft` to `python_version >= "3.13"` so hashed CI installs skip libcst's 3.13-only YAML backend on 3.11 and 3.12.

## [0.5.1] - 2026-09-15

Corrective release: identity, Action supply chain, metric naming, and the
contributor on-ramp. No new inference behavior.

### Added
- Issue forms (bug, false-positive, feature), a pull-request template,
  Discussions, `ROADMAP.md`, the action-item register, `.github/FUNDING.yml`,
  two example configs, and demo assets (terminal recording, PR comment,
  sample `analyze` / `drift` JSON). Listing PRs:
  [awesome-actions](https://github.com/sdras/awesome-actions/pull/953),
  [awesome-devops](https://github.com/wmariuss/awesome-devops/pull/555),
  [static-analysis](https://github.com/analysis-tools-dev/static-analysis/pull/1895).
- A `dev` extra bootstraps tests and linters via `pip install -e ".[dev]"`.
- Python 3.14 classifier and CI matrix entry. `checkowners/py.typed` ships in
  the wheel so the `Typing :: Typed` classifier is accurate.
- `requirements-dev.lock` pins CI test and lint installs with hashes.
- pytest `--cov-fail-under=85` so CI fails below the documented coverage
  floor. Branch coverage is collected repo-wide and reported for
  `patterns.py`, `analyze.py`, `drift.py`, and `generate.py`.
- Ruff enables `S` (bandit), `PTH` (pathlib), `RUF`, `C4`, `PL`, `ARG`,
  `TID`, and `ERA`.
- Coverage badge on the README pointing at `smusali/checkowners`.
- Composite Action input `max_output_entries` (default 50) caps each list
  in the `GITHUB_OUTPUT` summaries. Full `drift.json`, `bus_factor.json`,
  and `decay.json` payloads are uploaded as the `checkowners-reports`
  artifact, exposed via the `artifact_name` output.
- The composite Action always writes the drift, bus-factor, and decay
  report to the job summary, including when the drift step fails.
- Third-party GitHub Actions are pinned to full commit SHAs. Dependabot
  watches the `github-actions` ecosystem, and CI fails on any unpinned
  `uses:` reference.
- The publish workflow rejects a GitHub Release whose tag has no dated changelog heading.
- The composite Action installs the `checkowners` version that matches its
  own tag from a committed wheel and a hashed `requirements.lock`. New
  `index_url` and `offline` inputs support internal mirrors and air-gapped
  runners. `install_spec` is limited to a local extras allowlist.
- CI and the publish job fail when the Action pin, package version, wheel,
  or lockfile disagree with each other (or with the release tag).

### Fixed
- Codecov uploads authenticate with `CODECOV_TOKEN` and fail the job if
  the report is rejected, so the README coverage badge can receive data.
- README monthly-download badge uses pepy.tech instead of shields.io
  `pypi/dm`, which was rendering "rate limited by upstream service".
- CI pip installs retry with a longer timeout on transient PyPI index
  failures.
- The Action PR-comment lookup paginates past 100 comments, so the
  managed comment is updated instead of duplicated. Interpolated
  paths, notes, and reasons are escaped, long paths are truncated,
  and the report is capped with an overflow line pointing at the
  uploaded artifact.
- Composite Action multiline `GITHUB_OUTPUT` writes use a random
  `ghadelim_` delimiter instead of a static `EOF`, so a path, note, or
  reason equal to `EOF` can no longer truncate or inject outputs.
- The composite Action no longer installs an unpinned latest package from
  PyPI. `uses: smusali/checkowners@vX.Y.Z` now installs `checkowners==X.Y.Z`.
- `install_spec` dogfood installs use pip's isolated build env so hatchling
  is available. `git blame` no longer crashes on non-UTF-8 files, and `*.whl`
  is excluded from inference.
- README documentation and license links are now absolute GitHub URLs so they
  resolve on the PyPI project page.
- The composite Action exports `GITHUB_TOKEN` on every CLI step via a
  `github_token` input (default `${{ github.token }}`). The 0.5.0 claim that
  installing the `github` extra made handle resolution work in CI was
  incomplete: the extra was installed, but the token was never passed, so
  only local noreply parsing worked. When `github.api_enabled` is true and
  no token is reachable, the CLI now prints a warning naming `GITHUB_TOKEN`
  instead of degrading silently. The same input is now passed to the PR
  comment step, so a PAT or App token takes precedence over `github.token`.
- PR comments no longer fail the job on fork pull requests or read-only
  workflow permissions. Forks skip the comment with a notice; a 403
  becomes a warning that names `pull-requests: write` and
  `comment_on_pr: false`. The comment body is a short sectioned report
  rather than a details-block dump.
- Solo-maintainer repos no longer get a per-file knowledge-risk comment
  for `bus_factor = 1`. The Action only lists single-owner paths when
  more than one human has qualified ownership, and names the owner and
  suggested backups instead of saying "bus factor 1".
- The composite Action sets `CHECKOWNERS_STATE_DIR` to
  `${{ runner.temp }}/checkowners-state` on every CLI step, so CI state
  no longer lands in the runner home directory. Environment variables
  (`CHECKOWNERS_CONFIG`, `CHECKOWNERS_DRIFT_MODE`, `CHECKOWNERS_STATE_DIR`,
  `GITHUB_TOKEN`, plus runner-provided `GITHUB_REPOSITORY` and
  `GITHUB_OUTPUT`) are documented with their precedence over the config file.
- Composite Action `description` is under 125 characters so GitHub Marketplace
  will accept the listing.

### Changed
- The `all` extra composes `checkowners[graph,github]` instead of duplicating
  those dependency lists. Runtime dependencies are capped at the next
  untested major. Package version is read from
  `checkowners/__init__.py` only.
- Test and lint CI install from `requirements-dev.lock` with hashes instead
  of resolving a hatch environment unpinned.
- The truncated owner count is now named `qualified_owner_count`. Human
  output always states the `top_n_owners` cap. JSON emits both
  `qualified_owner_count` and the deprecated `bus_factor` alias for one
  minor cycle, plus `qualified_owner_count_cap` and `deprecated_keys`.
  `checkowners qualified-owners` is the canonical command; `bus-factor`
  remains as a deprecated alias pending redefinition. Persisted state is
  schema v4; Action summaries are `schema_version` 2. README, PyPI, and
  Action copy no longer advertise an unqualified bus factor.
- Composite Action outputs `checkowners_drift`, `bus_factor_summary`, and
  `decay_summary` are now bounded summaries (`schema_version: 1`) with
  counts and a `truncated` flag, not the full CLI payloads. Bus-factor
  `entries` are omitted from `GITHUB_OUTPUT`. `fromJson` gates on
  `drift_detected`, `severity`, and `critical_paths[0]` are unchanged.
- Project URLs, documentation clone and issue links, the Actions example,
  and the security advisory link now point at this repository.
- Changelog version dates are the PyPI publication day; the 0.3.0 and 0.5.0
  headings now match those uploads.
- Composite Action `mode` default is now `commit`, matching `DriftConfig`
  and the configuration reference. Callers who omitted `mode` previously
  got `both` (including stale-rule detection) and must pass `mode: both`
  to keep that behavior.
- Positioning copy no longer claims priority. The comparison table credits
  competitors, including where they exceed CheckOwners (owner-existence
  checks, formal truck factor, compile-time `check` mode). The runtime
  sentence now matches the 0.5.0 dogfood figure of under three minutes
  on a 24k-commit, 12k-file monorepo. The README and FAQ state the
  AI-assistance posture explicitly: inference is deterministic git
  analysis, and the codebase has been built with reviewed agent
  assistance. Unused `CLAUDE.md` agent instructions are removed.

## [0.5.0] - 2026-07-04

Hardening release driven by testing every command against a real production
monorepo (24k commits, 12k active files). Focus: correct CODEOWNERS
semantics, analyze performance, per-repo state isolation, and identity
resolution.

### Added
- CODEOWNERS pattern matching engine (`checkowners/patterns.py`) with
  GitHub's documented gitignore-style semantics: `*` stays within a path
  segment, `**` crosses segments, leading or interior `/` anchors to the repo
  root, trailing `/` matches directory contents, `dir/*` matches direct
  children only, and the last matching rule wins.
- `generate` consolidates per-file inference into directory-level rules when
  every inferred file under a directory shares one owner set (`/src/ @alice`
  instead of hundreds of per-file lines). New files under a consolidated
  directory now match a rule. Disable with `output.consolidate: false`.
- `generate` and `sync` refuse to overwrite a hand-written CODEOWNERS (one
  without the machine-generated header) unless `--force` is passed.
- `--version` flag; running bare `checkowners` now prints help instead of a
  usage error.
- Progress bar (stderr, TTY-only) during the analyze blame pass.
- Local GitHub-noreply email resolution: `12345+login@users.noreply.github.com`
  and `login@users.noreply.github.com` map to `@login` with zero API calls
  and no token.
- Persistent email-to-handle cache at `<state-dir>/handles.json`, including
  remembered misses, so the rate-limited user-search API is only queried once
  per new email.
- Identity merging: when several commit emails resolve to one GitHub handle,
  their owner entries merge and per-path bus factor is recomputed over
  distinct people, so one person with two emails no longer reads as a bus
  factor of two. Decay warnings are remapped to the resolved handle.
- Drift results carry `notes` explaining skipped comparisons (raw emails vs
  @handles, team-owned rules) instead of emitting false drift.
- `balance` reports `fallback_reason` when the GitHub API path was abandoned,
  and labels the load column "Commits (proxy)" when counting authorship.

### Changed
- **Drift detection is now pattern-aware.** Inferred files are matched
  against CODEOWNERS rules with real matching semantics; previously the
  comparison was literal string equality, so directory rules like
  `frontend/` never matched inferred file paths and real-world files
  produced near-100% false "missing"/"stale" reports. New categories:
  `missing` = file no rule covers, `stale` = rule matching no tracked file
  (via `git ls-files`), `changed` = per-rule owner divergence, aggregated and
  ranked by worst per-file delta. Owner comparison is case-insensitive.
- **State is keyed per repo** (schema v3) at
  `~/.checkowners/state/<repo-hash>.json` with the absolute repo path
  embedded and verified on load. Previously a single global `state.json`
  meant analyzing repo A then running `decay` in repo B silently reused repo
  A's data.
- **Analyze is parallel and skips unowned files.** git blame now runs on a
  thread pool sized to the CPU count and only on paths where at least one
  author reaches `min_commits` (4-5x fewer files on a real monorepo).
  Combined effect on a 24k-commit production repo: 80+ minutes to under 3
  minutes.
- `validate` follows GitHub's actual CODEOWNERS rules: relative patterns
  (`docs/`, `apps/*`, `frontend/package.json`) are valid, owner-less rules
  (GitHub's documented exemption mechanism) are valid, escaped spaces are
  parsed, and `!` negation / `[...]` character ranges are correctly rejected.
  It previously demanded every pattern start with `/` or `*`, failing
  perfectly valid real-world files. Handle validation now matches GitHub's
  login rules (no dots, max 39 chars).
- `validate --json` exits non-zero on an invalid file, matching the
  human-readable mode.
- `sync` is a no-op success when the generated file matches the committed one
  ("already in sync"); it previously failed with an empty error because git
  prints "nothing to commit" on stdout.
- Downstream commands print a stderr hint when reusing cached state.
- `notifications.include_unchanged` now means "also notify when no drift was
  detected"; without it, no-drift runs no longer fire webhooks.
- Severity's critical signal honors `bus_factor.critical_threshold` instead
  of a hardcoded 1.
- Review-coverage and balance GitHub scans are bounded to the 200 most
  recently updated closed PRs and scoped to `GITHUB_REPOSITORY`; previously
  unbounded scans exhausted the API rate limit on mature repos.
- `trends` counts distinct commits per period; a commit touching 12 files
  previously counted 12 times.
- `bus-factor`, `expertise`, and `onboard` share one glob semantic
  (previously the same pattern matched different path sets per command).
- Backup-reviewer suggestions fall back to repo-wide top owners for
  root-level files.
- Onboarding steps never label a `bus_factor<=1` path "easy".
- Default exclusions now also cover `package-lock.json`, `pnpm-lock.yaml`,
  `*.min.js`, `*.min.css`, `*.map`, and the CODEOWNERS file itself (a sync
  commit would otherwise make whoever runs the tool its inferred owner,
  perturbing every subsequent run).
- Composite Action: fails fast with a clear error on shallow clones
  (`fetch-depth: 0` guidance), installs the `github` extra so handle
  resolution works in CI, and the PR comment is updated in place (one
  managed comment per PR, marked resolved when drift clears) instead of
  posting a new comment on every push.
- PyGithub moved from a hard dependency to the `github` extra (with an
  `all` convenience extra); core inference is pure git. Unused GitPython
  dependency dropped.
- graph DOT export escapes quotes and backslashes in node IDs and labels.

### Removed
- `drift.compare_to` config option: it was parsed and documented but never
  read by any logic. Configs still containing it are ignored, not rejected.
- Dead internal API surface orphaned by the identity-merge rework:
  `OwnershipMap.handles_only()` and `github.map_owners()`.

### Fixed
- Generated CODEOWNERS never emits `[...]` character ranges: bracket-bearing
  path segments (Next.js dynamic routes like `[companyId]`) become the valid
  `*` wildcard, colliding patterns merge their owners, and literal spaces in
  patterns are backslash-escaped. GitHub ignores lines with `[...]`, which
  silently un-owned those paths. Found by dogfooding against a production
  Next.js monorepo.
- Terminal output renders paths like `[companyId]` verbatim: user-derived
  text (paths, reasons, handles) is markup-escaped so Rich no longer swallows
  bracket segments as style tags.
- Webhook notifications no longer crash the CLI on HTTP or network errors;
  failures log a warning and `notify` reports `sent: false`.
- Rebalance suggestions can no longer propose shifting reviews onto another
  overloaded reviewer.
- Topology reports one mismatch line per overlapping declared team instead of
  only the first.
- Tests never touch the developer's real `~/.checkowners` (isolated state
  dir fixture).

## [0.4.0] - 2026-06-14

### Added
- `checkowners github-action`: runs the full CI flow (`analyze` -> `drift` -> `bus-factor` -> `decay`) in one command, writes the `checkowners_drift`, `bus_factor_summary`, and `decay_summary` keys to `GITHUB_OUTPUT`, and exits non-zero on drift by default (`--no-fail-on-drift` to override).
- `checkowners trends [--periods N] [--period-days D]`: reconstructs the ownership snapshot at the end of each of the last N periods from a single `git log` pass and reports commits, active contributors, tracked paths, average top-owner confidence, and average bus factor over time.
- Review-activity factor of the confidence score is now populated (it was previously always 0.0). When `github.api_enabled`, a token resolves, and `GITHUB_REPOSITORY` is set, closed-PR reviews are aggregated per changed file and folded into the score; the factor stays 0.0 otherwise.
- Serialized knowledge-graph cache at `~/.checkowners/graph/<repo-hash>.json`, keyed by repo and invalidated by the analysis timestamp; the `graph` command reuses a fresh cache.
- Composite Action posts a built-in drift + bus-factor PR comment on pull requests (`comment_on_pr` input, default `true`).
- `docs/` directory housing detailed reference: `USAGE.md`, `FAQ.md`, `CONTRIBUTING.md`, this `CHANGELOG.md`, and the project `CODEOWNERS` (moved from `.github/CODEOWNERS`).

### Changed
- README drops its Mermaid pipeline diagram (PyPI does not render Mermaid) in favor of a prose summary; the diagram now lives in `docs/USAGE.md`.
- `paths.exclude` default now includes `*.generated.*`.
- Composite Action honors its `config` and `mode` inputs via the `CHECKOWNERS_CONFIG` and `CHECKOWNERS_DRIFT_MODE` environment variables, which `load_config` now reads.
- `BusFactorReport` tiers (`tier_for` / `critical_paths`) respect the repo's configured `bus_factor` thresholds instead of hardcoded defaults.
- README slimmed to intro, install, quick start, command table, and links to the new `docs/`.
- Dogfood config sets `output.include_confidence: false` so the committed `CODEOWNERS` no longer publishes a per-file confidence/bus-factor map.
- All Markdown across the repo follows a tightened style: no em dashes, no typographic `--` separators, multi-entry bullets only.

### Fixed
- CI smoke job: `pip install "dist/checkowners-"*"-py3-none-any.whl[graph]"` failed because bash treated `[graph]` as a glob character class and never expanded the wildcard. The wheel path is now resolved via `ls` before installation.
- Removed dead `generate._owners_for_path` helper.

### Security
- `notifications.webhook_url` accepts a `${ENV_VAR}` reference (e.g. `${CHECKOWNERS_WEBHOOK_URL}`) so a committed config can point at a secret/internal endpoint without storing it; an unset variable resolves to "".
- `.checkowners/` is git-ignored so a state or graph cache (contributor emails + ownership map) cannot be committed if `CHECKOWNERS_STATE_DIR` points inside a repo.
- `github.token` remains refused inside `.github/checkowners.yml`; the only supported way to provide a token is the `GITHUB_TOKEN` environment variable.

## [0.3.0] - 2026-06-06

### Added
- Confidence scoring on every path-owner pair. Score is a weighted blend of four signals: commit recency (exponential decay), commit frequency, blame coverage, and PR review activity (last one only when `github.api_enabled`).
- Bus factor calculation per path with backup-reviewer suggestions, plus `checkowners bus-factor [<path>] [--all]`.
- Expertise decay detection that distinguishes dormant from departed owners, recommends transfer targets, and exposes them through `checkowners decay`.
- Knowledge graph builder backed by an optional `networkx` extra: `pip install "checkowners[graph]"`. Render in the terminal or export to DOT via `checkowners graph [--export dot]`.
- Per-path expertise ranking via `checkowners expertise <path>`.
- Team topology inference from commit co-occurrence, with reconciliation against declared GitHub teams when `api_enabled`. Exposed as `checkowners topology`.
- PR review load balancer that detects overloaded reviewers and suggests redistribution. Exposed as `checkowners balance`.
- Onboarding path generator that walks the knowledge graph from broad-ownership files to deep-expertise files and emits a Markdown checklist via `checkowners onboard <path>`.
- Persistent state cache at `~/.checkowners/state.json` (schema v2), with `CHECKOWNERS_STATE_DIR` override for CI and tests.
- Composite GitHub Action (`action.yml`) exposing `checkowners_drift`, `bus_factor_summary`, and `decay_summary` outputs; example workflow at `.github/workflows/checkowners-example.yml`.
- Drift severity tiers (`low` / `medium` / `high` / `critical`) computed from the max confidence delta plus bus-factor and decay signals; `notifications.severity_threshold` gates webhook delivery.
- Config sections `scoring`, `decay`, `bus_factor` and new fields on existing sections (`confidence_threshold`, `min_confidence_delta`, `include_confidence`, `severity_threshold`, `github.api_enabled`).

### Changed
- `analysis.lookback_days` default lifted from 180 to 365.
- `analysis.top_n_owners` default lifted from 2 to 3.
- `paths.exclude` default now includes `node_modules/**`.
- `OwnershipMap` reshaped to carry `PathOwnership` entries (confidence-scored owners, bus factor, decay warnings).
- `DriftResult` now carries `DriftEntry` tuples with per-entry confidence delta and reason.
- `notify` payload includes severity, max delta, and per-entry bus factor / decay flags.

### Fixed
`validate` strips inline confidence comments so `output.include_confidence: true` does not fail the validator. Caught while dogfooding.

### Security
`github.token` is now refused inside `.github/checkowners.yml`. `load_config` raises a clear error if the field is present, since that file gets pushed to GitHub. The only supported way to provide a token is the `GITHUB_TOKEN` environment variable.

## [0.2.0] - 2026-05-26

### Added
- GitHub `@handle` mapping: commit emails are looked up against the GitHub user-search API and rewritten to `@username` when a match is found.
- Team and subteam resolution: owner sets whose handles are a subset of an org team collapse to `@org/team-slug`, with the most deeply-nested matching team winning.
- CODEOWNERS path auto-detection across `.github/CODEOWNERS`, root `CODEOWNERS`, and `docs/CODEOWNERS` in priority order.

## [0.1.1] - 2026-05-26

### Fixed
Deleted files are no longer carried into the generated CODEOWNERS; `analyze` filters out paths that no longer exist on disk.

### Changed
Repo now dogfoods its own generated CODEOWNERS.

## [0.1.0] - 2026-05-26

### Added
- Initial CLI: `analyze`, `generate`, `print`, `validate`, `drift`, `notify`, `sync`.
- Drift detection with three modes (`commit`, `repo`, `both`) and GITHUB_OUTPUT integration.
- Webhook notifications on drift events.
- Syntax-only CODEOWNERS validator.
- Packaging via hatch; published to PyPI under `checkowners`.
- CI workflow running tests and lint across Python 3.11, 3.12, 3.13.

[Unreleased]: https://github.com/smusali/checkowners/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/smusali/checkowners/compare/v0.5.1...v0.6.0
[0.5.1]: https://github.com/smusali/checkowners/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/smusali/checkowners/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/smusali/checkowners/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/smusali/checkowners/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/smusali/checkowners/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/smusali/checkowners/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/smusali/checkowners/releases/tag/v0.1.0
