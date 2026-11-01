# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Each dated heading is the UTC calendar day that version was published to PyPI. `Unreleased` has no date.

## [Unreleased]

### Added

- A documentation site is published from this repository, with search and a version per release.
- A bare `checkowners` runs `audit`, and `checkowners --help` still prints help.
- `checkowners doctor` reports configuration rows and `--env` adds the environment, where a missing token is `unverifiable` and not a pass.
- `checkowners report --html FILE` writes one offline HTML report: an overview, a treemap, an ownership map, a timeline, and a drill-down.
- `checkowners diff SPEC` reports the ownership impact of a diff, and `suggest --diff SPEC` or `suggest --pr N` routes a reviewer for it.
- `--diff-only SPEC` limits the read-only analysis commands to the paths a diff touched without replacing the cached map.
- `checkowners simulate --remove @alice` reports the paths that lose coverage and the truck factor before and after, from the cached map.
- `simulate --remove-file` takes a list of identities, and `--remove-status` takes every cached owner with one contributor status.
- `checkowners graph --export` writes DOT, GraphML, GEXF, or JSON, and reports articulation points, orphan files, and a transfer path.
- `checkowners topology` reports weighted Louvain clusters with modularity and cross-seed stability, and compares them with declared teams.
- `checkowners onboard PATH` lists inferred onboarding steps, following resolved imports where they exist and naming the missing signals.
- `checkowners suggest` recommends one reviewer from expertise and current load, and names the load signal it measured.
- `checkowners sync` opens or updates one reconciliation pull request; `--commit` commits locally and `--dry-run` prints the diff.
- `identity merge`, `identity unmerge`, and `identity audit` record or report identity changes, and `contributors.roster` sets each status.
- `drift`, `risk`, and `validate` accept `--format text|json|sarif|junit`, and the Action uploads the SARIF and JUnit files.
- The Action `upload_sarif` input uploads that SARIF, publishes workflow annotations, and completes a check run named checkOwners.
- `.github/workflows/checkowners.yml` is a reusable workflow wrapping the Action with the same inputs.
- `policy` chooses which findings block: drift, a single expert, unowned paths, and incomplete analysis warn, and an invalid owner fails.
- `generate` keeps a `# checkowners:manual` block byte for byte, and `output.block_order` decides which block wins an overlapping path.
- `generate` and `validate` take `--dialect github|gitlab`, and GitLab writes one section per top-level directory.
- Incremental analysis replays a usable baseline by default, and `--no-incremental` scores the full lookback again.
- Blame results are cached between runs, `--verbose` prints hit and miss counts, and `--no-cache` skips the cache.
- History follows renames and copies by default, under `analysis.follow_renames` and `analysis.rename_similarity`.
- Recency uses each path's median commit gap as its half-life, and each owner reports expertise and a continuity status.
- Change survival is a scored signal: current blame lines over that person's lines at their last commit.
- `Co-authored-by` trailers credit co-authors when `git.count_co_authors` is true, and analyze reports the merge strategy it saw.
- Drift compares a team rule with the expanded member set and says whether the inferred owners are inside it.
- Drift severity scores owner overlap, confidence delta, path criticality, decay, and a single qualified owner, with configurable cutoffs.
- `qualified-owners` reports a criticality-weighted distribution of top-owner share, and an empty `criticality` map says so.
- Per-path `risk` measures concentration over every score above the threshold, while `top_n_owners` caps only the displayed list.
- `risk` reports `former_knowledge_share`, the share of score held by owners the roster marks as former.
- `cache info` reports this repository's id and state file, exiting 1 with no state file and 2 outside a repository.
- `.checkowners-baseline.json` is excluded by default, so refreshing the accepted-findings file is not itself a finding.
- `RepositoryAnalyzer` is the library entry point, and its stored observations can be scored again with no second git pass.
- The Action caches pip downloads and `~/.checkowners`, reusing a restored map only when its commit and config hash match.

### Changed

- Every cache is keyed on the package version and the configuration hash, so a release or a config change refreshes it.
- `trends` reports `historical_activity_confidence` instead of `avg_top_confidence`, because the two are not the same measurement.
- The review signal reads closed pulls in batched queries, and a window cut short by the request budget lowers analysis completeness.
- The Action `fail_on_drift` input defaults to empty, so a run follows `policy.drift.fail` and blocks only on a novel finding.
- The GitHub API features need only a token; there is no separate extra to install.

### Removed

- The `version:` key and the `model.ownership`, `model.risk`, and `model.topology` pins are gone, and each now exits 2 naming the line.
- `schema_version` and `models` are gone from every JSON payload, which now carries `checkowners_version` alone.
- The `github` extra is gone; `checkowners[all]` is now `checkowners[graph]`.
- The command aliases `who`, `bus-factor`, `decay`, and `balance` are gone; use `owners`, `qualified-owners`, `risk`, and `suggest`.
- `analysis.max_owners`, `analysis.exclude_bots`, and the flat `scoring:` section are gone, replaced by one key each.

### Fixed

- `expertise --json` validates against the published schema, which did not accept the confidence field it emits.
- Path globs and `paths.exclude` treat `\` and `/` alike and stay case-sensitive on every operating system.
- Git path output is read as UTF-8 with quoting disabled, and Git older than 2.23.0 exits 4 naming the version to install.
- Concurrent handle-cache writes take a lock, so two commands on one repository cannot corrupt the cache.

### Security

- `certifi` is pinned to 2026.10.19, which drops the compromised `emSign ECC Root CA - G3` root (GHSA-6q48-6c7w-9gw2).
- `PyJWT` and `urllib3` are pinned past the advisories that the previous pins carried.

## [0.6.0] - 2026-09-24

Correctness and trust: scored ownership, privacy controls, and supply-chain checks. Inference stays local git.

### Added

- Ownership scoring renormalizes over the available signals into `ownership_score` on `[0, 1]`, with `evidence_quality` reported separately.
- Analysis completeness is a run-level score with `analysis_gaps`, and `--fail-on-incomplete` exits 3 when a bound left the run short.
- `--json` emits a documented schema with `checkowners_version`, `repository`, `head_sha`, `generated_at`, and completeness.
- Privacy controls add `output.anonymize`, `output.aggregate_only`, `privacy.redact_emails`, `identity.mode`, and `contributors.exclude`.
- On-disk state uses tokenized emails and locked atomic writes, with `cache path|info|clear|purge` and a 256 MiB cap.
- `checkowners baseline create` writes accepted findings, so `--baseline` keeps CI failing only on findings outside that file.
- `suppressions` accepts explicit, reason-required exceptions, which `drift` and `audit` honor alongside the baseline.
- `checkowners explain PATH` decomposes one path's inferred ownership and `checkowners owners PATH` prints the ranked list.
- `checkowners explain-path PATH` shows the winning CODEOWNERS rule and the whole match chain.
- Analyze excludes `linguist-generated` and `linguist-vendored` paths and reports how many each mechanism dropped.
- Blame honors `.git-blame-ignore-revs`, ignores whitespace, detects moves, and omits commits above `git.mass_refactor_file_fraction`.
- `.mailmap` is applied first when `identity.mailmap` is true, and analyze reports whether a mapping file was found.
- `generate` and `sync` fail when the re-resolved owners differ from the intended set, unless `output.verify_round_trip` is false.
- `generate` warns at 2 MB, refuses above `output.max_bytes` without `--force`, and reports the bytes and rules it wrote.
- `generate` and `sync` refuse a broad `*` rule that would assign different owners, unless `output.allow_broad_patterns` is set.
- `.pre-commit-hooks.yaml` exposes `checkowners-validate` and `checkowners-drift`.
- Human reports, the job summary, and comments state that this is a knowledge-risk tool and not a performance measure.
- Each GitHub Release carries a CycloneDX SBOM and a signed build-provenance attestation.

### Changed

- Every command shares the exit codes 0 clean, 1 internal, 2 usage, 3 findings, 4 integration, and `--exit-zero` clears only 3.
- The Action output `checkowners_drift` is now `drift_summary`, in both `github-action --json` and `GITHUB_OUTPUT`.
- The composite Action runs `checkowners github-action` once for drift, qualified owners, decay, the job summary, and `GITHUB_OUTPUT`.
- Recency, decay, and lookback age against `--as-of`, else `SOURCE_DATE_EPOCH`, else the HEAD committer time, never the wall clock.
- An offline score is no longer capped at `0.85`, so a value means the same thing with or without a token.
- The default `qualification.strategy` is `adaptive`, so commit count is shrunk evidence; `threshold` keeps the old eligibility gate.

## [0.5.1] - 2026-09-15

Corrective release: identity, Action supply chain, and metric naming. No new inference behavior.

### Added

- The Action input `max_output_entries` caps each list in the output summaries, and the full payloads are always uploaded as an artifact.
- The Action installs the `checkowners` version matching its own tag, and a release whose pins disagree is rejected.
- Third-party Actions are pinned to full commit SHAs.
- `checkowners/py.typed` ships in the wheel, so type checkers see the annotations.

### Changed

- The truncated owner count is named `qualified_owner_count`, because the old name claimed a simulation it never performed.
- The Action `mode` input defaults to `commit`, matching the configuration default.
- The `all` extra composes the other extras instead of duplicating their pins.

### Fixed

- The Action comment lookup paginates past 100 comments, so it updates its own comment instead of posting a new one.
- The Action no longer fails on a fork pull request or a read-only token; it writes the report to the job summary.
- The Action exports `GITHUB_TOKEN` on every CLI step, and sets `CHECKOWNERS_STATE_DIR` to a writable path.
- A solo-maintainer repository no longer receives a per-file knowledge-risk comment that names every path.
- Multiline Action outputs use a random delimiter, so report text cannot break `GITHUB_OUTPUT`.

## [0.5.0] - 2026-07-04

Pattern-aware drift, per-repository state, and local identity resolution.

### Added

- A real CODEOWNERS pattern engine: `*` within a segment, `**` across segments, a leading `/` anchored, last match wins.
- `generate` consolidates per-file inference into directory rules when every file under a directory agrees.
- `generate` and `sync` refuse to overwrite a hand-written CODEOWNERS unless `--force` is passed.
- `--version` prints the version, and a bare `checkowners` prints help.
- GitHub noreply addresses resolve to `@handles` with no token and no network.
- A persistent email-to-handle cache remembers both hits and misses, so a repeat run does not re-query.
- Several commit emails that resolve to one handle become one owner, with the commits summed.
- Drift results carry notes explaining a comparison it skipped, such as raw emails against `@handle` rules.
- A progress bar on stderr during the blame pass, on a terminal only.

### Changed

- Drift matches inferred files against real patterns, so a directory rule no longer marks the files under it missing.
- State is keyed per repository, so two checkouts of one remote share a map and unrelated repositories never do.
- Analyze runs blame in parallel and skips unowned files.
- `validate` follows GitHub's actual rules, including relative patterns and inline comments.
- `validate --json` exits non-zero on an invalid file, matching the text output.
- `sync` is a no-op success when the generated file already matches the committed one.
- Review-coverage and review-load scans are bounded to the 200 most recent pulls.
- `trends` counts distinct commits per period, so one commit touching twelve files counts once.
- The default exclusions cover `package-lock.json`, `pnpm-lock.yaml`, and the generated CODEOWNERS itself.
- The Action fails fast with a clear message on a shallow clone.
- PyGithub moved from a hard dependency into an extra.

### Removed

- The `drift.compare_to` option is gone; it was parsed and documented but never read.

### Fixed

- Generated CODEOWNERS never emits a `[...]` character range, which GitHub does not support.
- Terminal output renders a path such as `[companyId]` verbatim instead of interpreting it as markup.
- A review-load suggestion can no longer propose shifting reviews onto someone who is already overloaded.
- Graph DOT export escapes quotes and backslashes in node ids and labels.

## [0.4.0] - 2026-06-14

### Added

- `checkowners github-action` runs the whole CI flow and writes the drift, qualified-owner, and decay summaries to `GITHUB_OUTPUT`.
- `checkowners trends [--periods N] [--period-days D]` reconstructs one snapshot per period from a single `git log` pass.
- The review-activity signal is populated when `github.api_enabled` is set and a token resolves.
- The Action posts a drift and qualified-owner comment on pull requests, which `comment_on_pr` controls.
- A serialized graph cache per repository, invalidated by the analysis timestamp.

### Changed

- `paths.exclude` adds `*.generated.*` by default.
- The Action honors its `config` and `mode` inputs through `CHECKOWNERS_CONFIG` and `CHECKOWNERS_DRIFT_MODE`.
- Qualified-owner tiers respect the configured `bus_factor` thresholds instead of hardcoded ones.

### Security

- `.checkowners/` is git-ignored, so a cache of contributor emails cannot be committed when the state directory is inside a repository.

## [0.3.0] - 2026-06-06

### Added

- Confidence scoring on every path and owner pair, from commit recency, commit frequency, blame coverage, and review activity.
- A per-path qualified owner count with candidate backup reviewers, as `checkowners qualified-owners [PATH] [--all]`.
- Continuity-risk detection that separates a dormant owner from a departed one and names a transfer target.
- An ownership graph behind the `graph` extra, rendered in the terminal or exported to DOT.
- `checkowners expertise PATH` ranks the evidence for one path.
- `checkowners topology` infers collaboration clusters and reconciles them with declared GitHub teams.
- `checkowners suggest` detects an overloaded reviewer and proposes a redistribution.
- `checkowners onboard PATH` walks from broad ownership to deep expertise and emits a checklist.
- A persistent state cache, with `CHECKOWNERS_STATE_DIR` to move it.
- A composite GitHub Action with the drift, qualified-owner, and decay outputs, plus an example workflow.
- Drift severity tiers from the worst confidence delta plus the concentration and decay signals.
- The configuration sections `scoring`, `decay`, and `bus_factor`, plus `confidence_threshold` and `github.api_enabled`.

### Changed

- `analysis.lookback_days` defaults to 365, and `analysis.top_n_owners` to 3.
- `paths.exclude` adds `node_modules/**` by default.

## [0.2.0] - 2026-05-26

### Added

- `checkowners drift` compares inferred ownership with the committed CODEOWNERS and reports what disagrees.
- `checkowners sync` writes the inferred file, and `checkowners validate` checks the committed one.

## [0.1.1] - 2026-05-26

### Fixed

- The package metadata and entry point work from a clean install.

## [0.1.0] - 2026-05-26

### Added

- `checkowners analyze`, `checkowners print`, and `checkowners generate` infer ownership from git history and write a CODEOWNERS file.

[Unreleased]: https://github.com/smusali/checkowners/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/smusali/checkowners/compare/v0.5.1...v0.6.0
[0.5.1]: https://github.com/smusali/checkowners/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/smusali/checkowners/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/smusali/checkowners/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/smusali/checkowners/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/smusali/checkowners/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/smusali/checkowners/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/smusali/checkowners/releases/tag/v0.1.0
