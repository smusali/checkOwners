# checkOwners — Current State

A complete, code-grounded snapshot of the project as it stands at `0.5.0`. Every claim below is
verified against the working tree, the packaging metadata, the composite Action, and the published
distribution surfaces. Defects are stated with the code that produces them.

---

## 1. Identity and Distribution

| Surface | Value | Status |
|---|---|---|
| Repository | `github.com/smusali/checkowners` | Canonical home. 78 commits. MIT licensed |
| Marketplace Action | `checkOwners` v0.5.0 | Categories: continuous-integration, code-review. Not GitHub-certified (normal for third-party Actions; not a security defect) |
| PyPI package | `checkowners` 0.5.0 | Released 2026-07-04. sdist 95.4 kB, wheel 54.4 kB |
| Maturity classifier | `Development Status :: 4 - Beta` | Accurate |
| Python support | `requires-python = ">=3.11"`; classifiers 3.11 / 3.12 / 3.13 | No 3.14 entry |
| Runtime dependencies | `typer>=0.9.0`, `rich>=13.0.0`, `pyyaml>=6.0` | No upper bounds, no lockfile |
| Optional extras | `graph` (`networkx>=3.0`), `github` (`pygithub>=2.0.0`), `all` | `all` duplicates the lists instead of composing `checkowners[graph,github]` |
| Supply-chain provenance | PyPI Trusted Publishing, Sigstore transparency entries, in-toto attestations, workflow permalinks | Genuinely ahead of the curve; unmentioned in the README |
| Community counters | 0 stars, 0 forks, 0 watchers, 0 open issues (before the current backlog), 0 open pull requests | No external adoption signal |
| Contributors | 2 (`@smusali`, `@claude`) | Project bus factor of 1 |

### 1.1 Stale identity references

Eight documented locations still resolve against a previous organization slug:

```
SECURITY.md:9                 security/advisories/new link
README.md:3                   CI badge
pyproject.toml:64             Homepage
pyproject.toml:65             Issues
pyproject.toml:66             Source
docs/USAGE.md:174             uses: <previous-org>/checkOwners@v0.5.0
docs/CHANGELOG.md:214-220     every compare/tag link
docs/CONTRIBUTING.md:8,87     clone URL and issues URL
```

GitHub redirects transferred repositories, so most resolve today. They stop resolving the moment a
repository named `checkowners` is created under the old organization, at which point the PyPI
Homepage link, the security disclosure link, and every `uses:` reference in every downstream
workflow break simultaneously. Separately, **Actions workflows do not follow repository renames**,
so the `uses:` line published in the project's own primary documentation points somewhere other
than where the Action is published.

`[project.urls]` also lacks `Documentation` and `Changelog` entries, and `README.md` uses relative
documentation links, which PyPI renders against the project page — producing four dead links on the
one screen where a Python developer decides whether to install.

The naming fragments across a lowercase package slug, a camel-case repository name, a previous
organization, and the current one. A first-time visitor can encounter four identities for one
product on the first screen.

---

## 2. Release History

| Version | CHANGELOG date | PyPI date | Note |
|---|---|---|---|
| 0.1.0 | 2026-05-26 | 2026-05-26 | consistent |
| 0.1.1 | 2026-05-26 | 2026-05-26 | consistent |
| 0.2.0 | 2026-05-26 | 2026-05-26 | consistent |
| 0.3.0 | 2026-05-28 | 2026-06-06 | **9 days apart** |
| 0.4.0 | 2026-06-14 | 2026-06-14 | consistent |
| 0.5.0 | 2026-07-03 | 2026-07-04 | 1 day |

Six releases in roughly six weeks, then approximately eight weeks (56 days) of silence with an empty
`Unreleased` section. Two of six changelog entries misdate their own release. Only `v0.5.0` exists
as an Action tag; there are no floating `v0` or `v0.5` tags, which is standard Marketplace practice.

`0.5.0` was a hardening release driven by dogfooding against a production monorepo of roughly 24,000
commits and 12,000 active files. It documents its own severe bugs honestly, which is a stronger
trust signal than a clean-looking history.

---

## 3. Codebase

**4,562 lines of package code across 20 modules; 4,230 lines of tests across 20 files.**

```
checkowners/
  __init__.py       3    version export (__version__ = "0.5.0")
  cli.py         1094    Typer app; 16 subcommands + --version; progress bar;
                         identity merging (emails -> @handles, bus factor recomputed)
  analyze.py      396    git log + parallel git blame -> confidence-scored OwnershipMap;
                         blames only min_commits-qualified paths; optional on_progress hook
  state.py        306    per-repo state (schema v3), handles.json cache, graph cache
  drift.py        300    pattern-aware compare -> DriftResult with notes: missing/stale/changed
  generate.py     262    OwnershipMap -> CODEOWNERS writer; directory consolidation;
                         refuses to overwrite a hand-written file without --force
  config.py       254    PyYAML loader, CODEOWNERS path auto-detection, token refusal
  github.py       246    email->@handle resolution, team resolution, review coverage
  models.py       208    dataclasses for every domain object and config section
  balance.py      201    review load balancer with source + fallback_reason provenance
  topology.py     164    team inference from commit co-occurrence
  trends.py       159    historical per-period snapshots from one git log pass
  graph.py        149    ownership graph builder (lazy networkx import; DOT-escaped)
  busfactor.py    143    per-path counts with backup-reviewer suggestions
  patterns.py     141    CODEOWNERS matcher: gitignore-style semantics, last match wins
  onboard.py      121    onboarding path generator
  notify.py       117    webhook POST with severity gating; never raises on network errors
  decay.py        111    expertise-decay detector with transfer recommendations
  validate.py      96    syntax-only CODEOWNERS validator
  expertise.py     91    per-path expertise ranking; shared path_matches_glob helper

tests/
  conftest.py      18    autouse fixture isolating CHECKOWNERS_STATE_DIR per test
  test_<module>.py       one per module; all subprocess calls mocked

action.yml        237    composite GitHub Action
```

The decomposition is genuinely good. This is not a one-file weekend script that accidentally became
a package: module boundaries are clean, the style is functional throughout, dataclasses are the only
classes, type hints are on every signature, and `mypy --strict` passes.

### 3.1 Public pattern-matching API

`patterns.py` exposes `parse_rules`, `match_path`, `pattern_matches`, `strip_inline_comment`, and
`split_escaped`, with private `_compile_pattern`, `_translate`, and `_translate_segment`. It
implements GitHub's documented gitignore-style semantics: segment-scoped `*`, segment-crossing `**`,
root anchoring on leading or interior `/`, trailing-slash directory matching, `dir/*`
direct-children-only, and last-match-wins.

---

## 4. Command Surface

Sixteen subcommands:

`analyze`, `generate`, `print`, `validate`, `drift`, `notify`, `sync`, `expertise <path>`, `decay`,
`graph`, `bus-factor`, `topology`, `balance`, `onboard <path>`, `trends`, `github-action`.

All support `--json` except `graph`, which supports `--export dot`.

Sixteen commands, zero external validation. Every command is permanent maintenance surface: a JSON
contract, a test file, a documentation section, and a backward-compatibility obligation. `topology`,
`balance`, `onboard`, `trends`, and `graph` were all built before a single external user confirmed
that `analyze` produces owners a real team agrees with.

---

## 5. The Inference Model as Implemented

### 5.1 Confidence

A weighted sum clamped to `[0.0, 1.0]`:

```text
confidence = 0.35 × recency + 0.25 × frequency + 0.25 × blame + 0.15 × review
```

- `recency = 0.5 ** (days_since_last_commit / recency_half_life_days)`, default half-life 90 days,
  evaluated against `datetime.now(UTC)`.
- `frequency = commits / max_commits_for_path`.
- `blame` = the contributor's fraction of current lines from `git blame --line-porcelain`.
- `review` = per-path review coverage, populated only when `github.api_enabled` is true, a token is
  present, and `GITHUB_REPOSITORY` is set; otherwise `0.0`. The review scan is bounded to the 200
  most recently updated closed pull requests (`REVIEW_SCAN_PR_LIMIT = 200`).

### 5.2 Configuration defaults

```yaml
analysis:
  lookback_days: 365
  min_commits: 3
  top_n_owners: 3
  confidence_threshold: 0.3
  exclude_bots: true
scoring:
  recency_half_life_days: 90
  recency_weight: 0.35
  frequency_weight: 0.25
  blame_weight: 0.25
  review_weight: 0.15
decay:
  threshold_days: 180
  alert_on_decay: true
bus_factor:
  critical_threshold: 1
  warn_threshold: 2
drift:
  mode: commit
  min_confidence_delta: 0.2
notifications:
  severity_threshold: medium
github:
  api_enabled: false
  resolve_handles: true
  resolve_teams: true
output:
  consolidate: true
  header: "# Generated by checkOwners. Do not edit manually."
  include_unowned: false
  include_confidence: false
```

### 5.3 Default exclusions

`*.lock`, `package-lock.json`, `pnpm-lock.yaml`, `dist/**`, `vendor/**`, `node_modules/**`,
`*.generated.*`, `*.min.js`, `*.min.css`, `*.map`, plus the three CODEOWNERS locations — matched
with `fnmatch.fnmatch`.

### 5.4 Severity tiers

`critical` when the qualified-owner count is at or below `bus_factor.critical_threshold` or decay is
true; `high` at confidence delta ≥ 0.7; `medium` at ≥ 0.3; `low` otherwise.

### 5.5 State layout

- `~/.checkowners/state/<repo-hash>.json` — schema v3, absolute repository path embedded and
  verified on load.
- `~/.checkowners/handles.json` — email-to-handle cache including remembered misses.
- `~/.checkowners/graph/<repo-hash>.json` — serialized graph cache.

### 5.6 Environment variables

`GITHUB_TOKEN`, `CHECKOWNERS_STATE_DIR`, `CHECKOWNERS_CONFIG`, `CHECKOWNERS_DRIFT_MODE`, plus the
runner-provided `GITHUB_REPOSITORY` and `GITHUB_OUTPUT`. Only `CHECKOWNERS_STATE_DIR` is documented
in the configuration reference; the other two appear only in an `action.yml` comment.

### 5.7 Pipeline

Configuration loads from `.github/checkowners.yml` and auto-detects the CODEOWNERS location →
`analyze.py` runs `git log --format=… --name-only --since=<N> days ago`, filters excluded paths,
filters nonexistent paths, filters bot authors, filters paths where no author reaches `min_commits`,
runs `git blame --line-porcelain` on a thread pool sized to `min(32, os.cpu_count() or 4)`, and
computes the four-factor confidence → `models.OwnershipMap` → `cli._resolve_github_owners` maps
emails to handles and merges identities → `state.py` persists per repository → downstream commands
read state and emit per-domain reports. `trends` is independent of state and runs its own single
`git log` pass.

CODEOWNERS is auto-detected at `.github/CODEOWNERS`, `CODEOWNERS` (root), or `docs/CODEOWNERS`.

---

## 6. What the Project Gets Right

These are real and must be protected in every future refactor.

1. **It attacks the real weakness of CODEOWNERS.** GitHub's mechanism is purely declarative. It can
   request reviewers and enforce code-owner approval, define matching semantics, require repository
   access, apply the final matching pattern, and skip invalid lines. What it cannot determine is
   whether the declared owner still represents the people who understand the code. The compelling
   question is not "what does CODEOWNERS say?" but **"does CODEOWNERS still correspond to reality?"**

2. **The pattern engine exists at all.** Most competitors string-compare. The `0.5.0` changelog
   admits the previous literal comparison produced near-100% false positives on real repositories.
   Fixing that is the single most important thing the project has done.

3. **Confidence-scored ownership is directionally correct.** Ownership is not
   `person_with_most_commits`. Someone may have created a component years ago, have many commits
   with no recent involvement, own most current lines, repeatedly review without authoring, or be a
   recent maintainer inheriting old code. A composite model is clearly better than a raw count.

4. **Declared-versus-observed drift is the strongest feature.** It parses the real file, evaluates
   rules against tracked files, and compares declared with inferred ownership. The analogy is exact:
   infrastructure tooling reconciles desired against actual infrastructure; this reconciles declared
   code ownership against observed engineering ownership.

5. **Correct dependency gating.** Core inference is pure git via `subprocess`. `networkx` and
   `pygithub` are optional extras, lazy-imported, degrading gracefully with a hint. GitPython was
   correctly dropped.

6. **The token refusal.** `github.token` is rejected in `.github/checkowners.yml` at load time with
   a clear error, because that file gets committed. The `${ENV_VAR}` indirection for
   `notifications.webhook_url` is the same instinct applied correctly a second time.

7. **Per-repo state isolation with schema versioning.** The `0.5.0` fix for a global state file
   leaking one repository's data into another, plus embedding and verifying the absolute repository
   path on load, plus the codified invariant that `SCHEMA_VERSION` must be bumped on change.

8. **Refusing to clobber hand-written CODEOWNERS.** `generate` and `sync` require `--force` when the
   machine-generated header is absent. Ownership files are governance artifacts, not disposable
   build products. The default is *do not destroy human intent*, not *machine inference is
   automatically authoritative*.

9. **Excluding the CODEOWNERS file itself from inference.** A `sync` commit would otherwise make
   whoever ran the tool the inferred owner of the ownership file, perturbing every subsequent run.

10. **Consolidating inferred rules into directory rules.** Emitting `/src/api/ @alice` instead of
    four per-file lines keeps the file understandable, keeps it small, and makes future files
    inherit a rule automatically.

11. **Identity resolution treated as a real problem.** The three-stage cascade — noreply parsed
    locally with zero network, then a disk cache with remembered misses, then the API — is
    well-engineered cost minimization over a genuinely messy domain.

12. **Identity merging so counts reflect people, not addresses.**

13. **Drift `notes` instead of false positives.** Emitting an explanation when comparison would be
    meaningless — raw emails versus handles, team-owned rules versus individual inference — rather
    than fabricating drift is mature product judgment.

14. **Supply-chain provenance on the package side.** Better than most projects with a hundred times
    the stars.

15. **`mypy --strict`, a non-trivial ruff rule set, and `Typing :: Typed` with `py.typed` shipped.**

16. **The composite Action's shallow-clone guard.** Failing fast with an actionable error naming
    `fetch-depth: 0`, instead of silently producing a garbage ownership map.

17. **Dogfooding against a real production monorepo.** The bracket-segment dynamic-route bug —
    GitHub ignores CODEOWNERS lines containing `[...]` ranges, so generating them silently un-owns
    those paths — is a finding you only get from real usage, and it was found and fixed.

18. **An honest changelog** that documents its own severe bugs.

19. **Local-first, pure-git, no-LLM operation.** Deterministic, auditable, offline-capable,
    air-gap-compatible, no token spend, no vendor lock-in, no hallucinated owners, no source upload.
    In the current market this is a differentiator, not a limitation.

20. **State caching at all.** The foundation for genuine incrementality already exists.

---

## 7. Defect Register

### 7.1 The bus factor is not a bus factor

`analyze._build_path_ownerships` truncates owners to `config.analysis.top_n_owners` (default 3), and
`_compute_bus_factor(top, threshold)` counts the survivors of that already-truncated tuple:

```python
top = filtered[: config.analysis.top_n_owners]
bus_factor = _compute_bus_factor(top, config.analysis.confidence_threshold)
```

```python
def _compute_bus_factor(top: tuple[OwnerEntry, ...], threshold: float) -> int:
    return sum(1 for entry in top if entry.confidence >= threshold)
```

`busfactor.compute_bus_factor` reads `po.bus_factor` directly and averages it across paths.

**Consequences.** The maximum reportable per-path value is 3 under defaults. A path maintained by
fifteen healthy contributors reports the same value as one maintained by exactly three. Raising
`top_n_owners` to 5 "improves" the entire repository's bus factor without one line of code changing
hands — the metric is a truncation artifact of a display parameter. Classification collapses to a
near-predetermined `1 → critical`, `2 → warning`, `3 → ok`. The repository-level arithmetic mean of
a value floored at 1 and capped at 3 cannot distinguish "one person knows everything" from "everyone
knows one thing."

This is not the definition of bus factor, truck factor, or lottery factor in any published
treatment. The literature defines it as a removal simulation over a knowledge distribution: the
minimal set of contributors whose departure leaves the project, or a threshold fraction of its
files, without a knowledge owner. Because bus factor is one of the headline claims in the repository
description, this is a positioning-level risk, not a cosmetic one.

*Illustration:* shares of `82/7/5/3/2/1` yield "six knowledgeable people" under a count, while
operational reality is "lose one person, catastrophic loss." Shares of `26/25/24/25` are genuinely
distributed. A count cannot separate these.

**Tracking:** #23, #24, #25, #26.

### 7.2 Confidence changes meaning depending on API availability

`analyze._score_owners` applies the four weights with **no renormalization**:

```python
total = _clamp(
    scoring.recency_weight * recency
    + scoring.frequency_weight * frequency
    + scoring.blame_weight * blame
    + scoring.review_weight * review
)
```

`review` is `0.0` whenever no review provider is injected, which is the default since
`github.api_enabled` defaults to `False`.

**Consequences.** The offline maximum is **0.85**; the API-enabled maximum is **1.00**. A score of
`0.72` has no single stable meaning — it depends on what happened to be reachable during execution.
The same checkout produces different classifications purely because one environment has a token.
`confidence_threshold: 0.3` is a different gate in each mode, and `drift.min_confidence_delta: 0.2`
compares differently-scaled quantities. This is unacceptable for a metric that drives merge-blocking
CI policy.

**Aggravating inconsistency:** `trends.py` already renormalizes over available weights when
historical blame and review cannot be reconstructed. The main analysis path does not. Both behaviors
ship in the same package.

**Tracking:** #27.

### 7.3 `min_commits: 3` deletes legitimate owners

`_filter_unqualified` drops whole paths before the blame pass, and `_build_path_ownerships` filters
authors again by `contrib.commits >= config.analysis.min_commits`.

A newly added production component created in one large commit, whose author owns 100% of its
meaningful lines, disappears entirely. This is the normal outcome — not an edge case — for new
files, young repositories, squash-merge workflows, large feature commits, low-churn infrastructure,
newly extracted services, newly moved modules, and any repository whose merge strategy produces one
commit per pull request.

**Tracking:** #28.

### 7.4 Frequency normalization inflates low-evidence paths

`_frequency_score(commits, max_commits) = commits / max_commits`. On a path where one person made
exactly three commits and nobody else touched it, that person scores `1.0`. A single-contributor
path with 3 commits and one with 300 produce identical frequency signals. There is no
absolute-evidence term, so "certainly the owner" and "the only person who happened to touch a nearly
dead file" are indistinguishable.

**Tracking:** #28.

### 7.5 Wall-clock non-determinism

`analyze_ownership` calls `datetime.now(UTC)` and threads it through every recency computation.

The same repository at the same commit produces different scores on different days; with a 90-day
half-life, scores move roughly 0.77% per day. `sync` generates diffs on unchanged code; drift results
oscillate across `min_confidence_delta: 0.2` and the severity boundaries at 0.3 and 0.7; a pull
request goes red on Tuesday and green on Wednesday with no code change; no result is reproducible
for audit. Teams experiencing flapping checks disable the check.

**Tracking:** #29.

### 7.6 A uniform 90-day half-life punishes correct stability

Mature, security-critical, protocol, or infrastructure code that legitimately has not changed in
eighteen months decays toward zero, falls below `confidence_threshold`, and generates `missing` drift
for files that have a perfectly good, perfectly available owner — exactly the class of file where
knowing the historical expert matters most.

**Tracking:** #30, #31.

### 7.7 Blame runs without any standard correction

```python
result = subprocess.run(
    ["git", "blame", "--line-porcelain", "--", path],
    capture_output=True, text=True, cwd=str(repo_root), check=True,
)
```

No `-w`, no `-M`, no `-C`, no `--ignore-revs-file`, no `--use-mailmap`. Blame carries 0.25 of the
score.

Blame currently confuses *the person who understood and wrote the code* with *the person who ran a
formatter across 8,000 files*. A single formatting sweep, license-header update, or lint autofix
migration reassigns blame for enormous fractions of a codebase; a directory reorganization destroys
attribution for every moved file. `.git-blame-ignore-revs` is the industry-standard remedy, is
honored natively by GitHub, and is already present in thousands of mature repositories. Ignoring it
produces confidently wrong answers on exactly the repositories the project most wants as reference
users.

**Tracking:** #32, #33.

### 7.8 `.mailmap` reimplemented instead of used

Neither `git log` nor `git blame` passes `--use-mailmap`. Instead `github._lookup_handle` reaches for
`client.search_users(f"{email} in:email")`. Sub-defects:

- `.mailmap` is free, offline, token-free, rate-limit-free, and already maintained in a large
  fraction of serious repositories.
- The search API sits in a separate, much stricter rate-limit bucket, and email-to-account matching
  by search is inherently fuzzy and privacy-sensitive.
- The authoritative mapping exists and is unused: fetching a single commit by an unresolved author
  returns `author.login` resolved by GitHub itself.
- `handles.json` is global across all repositories while state is per-repository. An email resolved
  while analyzing a private monorepo persists into the cache used for an unrelated public repository
  — a small but real cross-tenant leak in a local cache.
- Negative results are cached with no TTL, so a contributor who creates an account after the first
  run is permanently unresolved.

**Tracking:** #33, #37.

### 7.9 History is not path history

`_get_commit_history` runs `git log --name-only` with no rename or copy detection, and
`_aggregate_contributions` keys purely on the literal path string. A file renamed mid-history looks
brand new; five years of expertise evaporates. A **directory** rename resets ownership for every file
beneath it simultaneously. Combined with `min_commits: 3` and the 90-day half-life, a single refactor
can un-own an entire subsystem.

**Tracking:** #34.

### 7.10 Squash merges and co-authors are invisible

`_get_commit_history` parses only `%ae` and `%cI`. `Co-authored-by:` trailers are never read.
`git log --name-only` omits merge commits by default. Rebase-merge repositories attribute
differently again. The merge strategy materially changes results and is documented nowhere.

**Tracking:** #35.

### 7.11 Generated-file detection is a static blocklist

`paths.exclude` misses `yarn.lock`, `poetry.lock`, `Cargo.lock`, `go.sum`, `Gemfile.lock`,
`composer.lock`, `*.pb.go`, `*_pb2.py`, `*.g.dart`, generated GraphQL and OpenAPI clients, Terraform
lockfiles, `target/`, `build/`, `.next/`, `__generated__/`, snapshot files, and every migration
directory. `.gitattributes` `linguist-generated` and `linguist-vendored` — the mechanism GitHub
itself uses — are not read.

**Tracking:** #36.

### 7.12 Drift has a team-ownership blind spot

```python
_TEAM_NOTE = "rules owned by teams (@org/team) are not compared against inferred individuals."
```

`github.py` already exposes `create_team_resolver` and `_get_org_teams`; the capability exists and is
simply not wired into drift. Real organizational CODEOWNERS files are overwhelmingly team-shaped, so
a drift engine that cannot reconcile team declarations misses the most important enterprise use
case — and the current behavior is indistinguishable from "no drift" to a machine consumer.

**Tracking:** #39, #40.

### 7.13 Consolidation is unverified and unbounded

`generate.py` collapses a directory into one rule when every inferred file beneath it shares an owner
set, with no round-trip verification and no size guard. Consolidation plus last-match-wins ordering
is exactly the transformation where an off-by-one silently un-owns a subtree. And GitHub does not
load a CODEOWNERS file over 3 MB **at all** — code-owner information is not shown and reviews are not
requested, silently, while branch protection still appears configured.

Bracket-path sanitization, correct in intent, can also trade a syntax problem for a worse semantic
one: `routes/[id]/` and `routes/[slug]/` may both collapse toward `routes/*/`, which also matches
`routes/static/`.

**Tracking:** #41, #42.

### 7.14 The validator addresses none of the silent failures

`validate.py` is syntax-only. It correctly mirrors GitHub's documented rules — relative patterns
valid, owner-less rules valid, `!` and `[...]` rejected — and stops there. It does not detect: an
owner without write access; a team that is not visible or lacks write access; an invalid line
skipped; a file over 3 MB; base-branch placement; or last-match-wins shadowing. A syntactically
perfect file in which half the owners cannot approve anything reports zero drift forever while no
review is ever requested. GitHub has exposed CODEOWNERS syntax errors through its API since 2022;
the project hand-rolled its own instead.

**Tracking:** #44, #45, #72.

### 7.15 The review signal is a biased sample

`REVIEW_SCAN_PR_LIMIT = 200` bounds the scan by **count**, not time. On a repository merging 200 pull
requests per week the review signal covers seven days while the commit signal covers 365. The
0.15-weighted factor measures something structurally different from the other three, and its meaning
silently varies with repository velocity.

**Tracking:** #62.

### 7.16 Topology uses connected components

```python
def _cluster(adjacency: dict[str, set[str]]) -> list[set[str]]:
    """Greedy connected-component clustering over the adjacency graph."""
```

The classic chaining problem: `Alice ↔ Bob`, `Bob ↔ Carol`, `Carol ↔ Dave` yields one four-person
"team" even when Alice and Dave have never worked on overlapping code. One platform engineer who
touches many areas merges most of an organization into a single cluster. Algorithm, similarity
measure, resolution, seed, and stability are all unspecified, making the output unfalsifiable — and
the errors are socially costly.

**Tracking:** #57.

### 7.17 Trends compare a different metric

`trends.py` renormalizes over available recency and frequency weights because historical blame and
review cannot be cheaply reconstructed. The historical series is computed from a weaker, different
model than the present-day number, and charts appear directly comparable when they are not.

**Tracking:** #59.

### 7.18 Balance conflates authorship with review load

`balance._gather_counts` falls back to `_gather_from_authorship`, recording `source` and
`fallback_reason` — genuinely good provenance in the data structure. But authorship load is not
review load: a developer with many commits may perform few reviews, and a developer with few commits
may be the team's dominant reviewer. Rendering a commit-derived number as "review load" is a category
error, and it is the input to a recommendation that moves work between named people.

**Tracking:** #58.

### 7.19 State has no staleness contract, no locking, no atomicity

`state.py` writes with plain `target.write_text(...)` in three places. There is no lock, no
`analyzed_ref`, no TTL, no size bound, and no cache command. A user can `analyze`, pull 400 commits,
run `bus-factor`, and receive a stale answer with only a stderr hint. Two concurrent CI jobs on the
same runner can interleave writes. CI checkouts at different paths always re-analyze in full.

**Tracking:** #56.

### 7.20 `sync` commits directly

Commits made with the ambient token do not trigger downstream workflows, so the resulting commit runs
no checks; it collides with branch protection and required reviews; and it cannot be reviewed —
conspicuously odd for a tool about review routing.

**Tracking:** #50.

### 7.21 The webhook notifier is undefended

`notify.py` POSTs drift payloads containing contributor identities with no HMAC signing, no
documented timeout, no retry with backoff, no idempotency key, no scheme allowlist (an `http://` URL
sends identities in plaintext), and no redaction. Meanwhile the Action already emits structured
`GITHUB_OUTPUT` that any workflow can pipe to any notification Action in three lines of YAML.

**Tracking:** #65.

---

## 8. GitHub Action Defects

The Action is the primary distribution surface and carries the most severe defects.

### 8.1 The token is never passed to the CLI

`action.yml` sets exactly two environment variables on its CLI steps:

```yaml
env:
  CHECKOWNERS_CONFIG: ${{ inputs.config }}
  CHECKOWNERS_DRIFT_MODE: ${{ inputs.mode }}
```

It never sets `GITHUB_TOKEN`, and there is no `github_token` input. In GitHub Actions,
`GITHUB_TOKEN` is *not* automatically present as an environment variable inside a step; it must be
explicitly passed. The `actions/github-script` step receives one because that action requests it;
`run:` steps do not. `github.get_github_token()` therefore always fails in CI.

**Consequences:** non-noreply handle resolution cannot run; team collapsing cannot run; every
`github.api_enabled` feature cannot run; and `drift` in CI, on any repository that is not fully
squash-merged, hits `_IDENTITY_NOTE` and skips comparison. **The Action's headline feature silently
degrades to "we could not compare" on exactly the repositories that need it most.**

The documentation compounds this: the FAQ states that `${{ secrets.GITHUB_TOKEN }}` "is automatically
available" — true only in the sense that the secret can be referenced — and the `0.5.0` changelog
claims the Action "installs the `github` extra so handle resolution works in CI," when installing
`pygithub` without a token only makes the import succeed.

**Tracking:** #14.

### 8.2 Fork pull requests hard-fail

The `comment_on_pr` step defaults to `"true"`, is gated only on `github.event_name ==
'pull_request'`, calls `createComment` with the ambient token, and has no `continue-on-error` and no
try/catch. On a public repository the ambient token is read-only for fork pull requests regardless of
the `permissions:` block, so the call returns 403, the step fails, and because composite-action steps
abort the action on failure, **the entire check fails on every external contributor's pull request**
even when drift is clean. The same failure hits organizations defaulting workflow permissions to
read-only.

**Tracking:** #17.

### 8.3 The package install is unpinned

```yaml
checkowners_version:
  default: ""
```
```bash
pip install --upgrade "checkowners[graph,github]"
```

`uses: smusali/checkowners@v0.5.0` creates the impression of a pinned dependency while executing
whatever PyPI serves at that moment, inside a job granted `pull-requests: write`, with no pin, no
hash, no lockfile, and no `--only-binary`. A new release silently changes CI behavior for every
downstream user; builds are not reproducible; the Action cannot run on an air-gapped runner; and
Action `v0.5.0` will happily install a future `0.9.0` with a changed JSON shape and parse it with
`0.5.0`-era JavaScript. `install_spec` additionally accepts an arbitrary pip specification.

The irony is sharp: Trusted Publishing with Sigstore attestations on the way *out* of PyPI, all of it
discarded on the way *in* to the consumer's CI.

**Tracking:** #15.

### 8.4 Third-party actions are tag-pinned

`actions/setup-python@v5` and `actions/github-script@v7` are referenced by mutable tag — a hard
failure of the OpenSSF Scorecard pinned-dependencies check, and the exact vector behind multiple
recent Actions supply-chain incidents.

**Tracking:** #16.

### 8.5 Static output delimiter and unbounded payloads

Three steps write multi-line JSON using a literal `EOF` delimiter and `cat` the payload between
markers; `bus-factor --all --json` is written in full. A static delimiter is a latent corruption
vector the moment any path, handle, note, or reason contains a line equal to it. And a single output
is capped at 1 MB: on the 24k-commit, 12k-file monorepo the project cites as its own benchmark, the
full bus-factor payload will plausibly exceed the cap, failing by opaque truncation rather than a
clear error.

**Tracking:** #18.

### 8.6 Unpaginated comment lookup and Markdown injection

`listComments` uses `per_page: 100` with no pagination, so on a pull request with more than 100
comments the marker is not found and a new comment is created on every push — precisely what the
marker system exists to prevent. Drift paths and notes are interpolated into a Markdown table with
backtick wrapping and no escaping; backticks are legal in POSIX filenames.

**Tracking:** #19.

### 8.7 Two divergent CI implementations

`checkowners github-action` exists as a subcommand and is documented as the command that runs the
full CI flow and writes `GITHUB_OUTPUT`. `action.yml` does not call it: it invokes `drift`,
`bus-factor`, and `decay` as three separate steps and assembles output in bash heredocs. It even
contains a vestigial comment about publishing "the same as the historical key used by older
workflows" followed by a `cat` that publishes nothing additional. The documentation therefore
describes an architecture the Action does not use, `github-action` is effectively dead code, and the
Action implicitly triggers analysis up to three times.

**Tracking:** #20.

### 8.8 Three different defaults for one setting

| Location | `drift.mode` default |
|---|---|
| `models.DriftConfig` | `commit` |
| `action.yml` input | `both` |
| `docs/USAGE.md` example | `repo` |

`action.yml` also never sets `CHECKOWNERS_STATE_DIR` despite `SECURITY.md` advising exactly that for
CI — the project does not follow its own security guidance in its own Action.

**Tracking:** #21.

### 8.9 No caching, no SARIF, no annotations, no step summary

Every run pays full cost: setup-python with no `cache: pip`, a full `pip install`, `fetch-depth: 0`
checkout, and a full analysis — from scratch on every pull request, because nothing caches
`~/.checkowners`. Competitors ship a single static binary or run through a package runner. Slow
required checks get removed.

There is no SARIF output (which would place findings in the Security tab and inline on the diff
**without** needing `pull-requests: write`), no workflow annotations, no `$GITHUB_STEP_SUMMARY`
rendering, no reusable workflow, and no JUnit XML.

**Tracking:** #22, #83, #84.

---

## 9. Testing Posture

`docs/CONTRIBUTING.md` mandates that unit tests mock all subprocess calls and must not require a real
git repository, with an 85% coverage target. **The project's own changelog refutes that strategy.**

The `0.5.0` changelog documents what running the tool against **one real repository** found:

1. Drift comparison was literal string equality, so directory rules never matched inferred file paths
   and real-world files produced **near-100% false** `missing`/`stale` reports.
2. `validate` demanded every pattern start with `/` or `*`, failing perfectly valid real-world files.
3. Generated CODEOWNERS emitted `[...]` character ranges, which GitHub ignores, **silently un-owning
   those paths**.

Three catastrophic, product-invalidating bugs. All three shipped. All three passed an 85%-covered
suite. All three were caught by one real repository.

**The coverage number measures execution of Python lines while mocking away the entire problem
domain.** Every git-semantics bug, every CODEOWNERS-semantics bug, and every pattern bug is invisible
to a suite that mocks git and never renders a file GitHub would actually parse.

Additional gaps:

- `addopts = "--strict-markers"` contains no `--cov-fail-under`; the documented 85% target is
  unenforced.
- `select = ["E","F","I","N","W","UP","ANN","B","A","SIM"]` omits `S` (flake8-bandit) in a codebase
  whose entire core is `subprocess` invocation of git with user-supplied paths, and omits `PTH`
  despite `pathlib`-only being a documented human-enforced convention.
- No property-based or fuzz testing of the pattern engine — the highest-risk component, which has
  already shipped one catastrophic correctness bug.
- No differential corpus against an independent reference implementation.
- No mutation testing.
- No OS matrix; path separators, git behavior, and subprocess semantics differ on Windows.
- No git-version matrix; several flags the roadmap depends on are version-gated.

**Tracking:** #66, #67, #68, #69, #70, #71, #72.

---

## 10. Privacy and Ethics Exposure

**This is a workforce-analytics product wearing an infrastructure jacket, and it is currently
undefended.** The tool produces, about named individuals: a per-person, per-path expertise ranking
with numeric scores; a dormancy detector flagging individuals as decaying or departed and
recommending their responsibilities be transferred away; a review-load report identifying overloaded
reviewers and proposing redistribution between named people; a commit-count proxy; a graph of
contributors, files, and inferred teams; and historical trends of all of the above.

It caches contributor email addresses on disk, sends contributor emails to a third-party user-search
API, POSTs them to arbitrary webhooks, and posts derived findings into pull-request comments visible
to the whole organization.

Missing: anonymization or pseudonymization; aggregate-only reporting; retention policy or TTL on the
handle cache; a privacy document; a data-flow description; a statement of what leaves the machine;
an explicit "this is not a performance metric" disclaimer; and any way to exclude an individual on
request.

`SECURITY.md` covers token handling and state-directory hygiene well and says nothing about the fact
that the state directory is a database of who knows what about whom.

**This is the objection that blocks enterprise adoption, and it is cheap to address.**

**Tracking:** #73, #74, #75.

---

## 11. Contract Gaps

`--json` is the entire integration surface for a CI-native tool, and it has:

- no published JSON Schema;
- no `schema_version` field;
- no documented stability guarantee;
- no model or metric versioning, so every planned scoring change is a silent behavioral break for
  anyone gating CI on a threshold;
- no documented exit-code table — `validate --json` is documented to exit non-zero while `drift`'s
  exit semantics are specified nowhere, which is exactly why `action.yml` re-parses the JSON in an
  inline Python heredoc instead of checking the status;
- no policy engine, so enforcement is all-or-nothing;
- no analysis-completeness metric, so the tool degrades silently when a token, history, team data,
  or review data is unavailable;
- no baseline or ratchet, so enabling the tool on a large existing repository produces thousands of
  findings on day one.

**Tracking:** #51, #52, #53, #54, #55, #56, #49.

---

## 12. Positioning Gaps

- **The "first" claim.** `README.md:11` asserts CheckOwners is "the first CODEOWNERS tool that treats
  it as a confidence-scored spectrum and surfaces the second-order risks." Prior art exists on both
  halves: at least one generator has produced CODEOWNERS from git history for years with lookback,
  minimum-commit, and maximum-committer thresholds; prior art on bus factor is an entire research and
  tooling lineage.
- **The comparison table** in `docs/USAGE.md` compares against four alternatives and scores every one
  "no" on all seven dimensions. It omits every serious competitor and misrepresents at least one — a
  dedicated validator whose file-existence and not-owned checks overlap directly with `stale` and
  `missing`, and whose owner-validity checking is strictly *more* than `validate` does. A comparison
  table that omits every serious competitor and marks the weak ones "no" everywhere is the fastest
  way to lose a technical audience.
- **Benchmark discrepancy.** The README says a 24k-commit monorepo analyzed in under **two** minutes;
  the changelog says under **three** — two numbers for one claim in one release, with no machine
  specification, repository identity, or harness.
- **The README leads with a feature list** and contains no terminal output, screenshot, or sample
  generated file, for a tool whose entire value is its reports.
- **The strongest trust assets are invisible.** Trusted Publishing, Sigstore attestations, and the
  pure-git offline guarantee appear nowhere in the README.
- **"No LLMs" ships alongside agent artifacts.** An agent instruction file sits in the repository root
  and an AI agent appears as one of two contributors. This is not hypocrisy and there is nothing wrong
  with it — it is a presentation problem, and ambiguity is the only bad option.

**Tracking:** #87, #88.

---

## 13. Community and Governance Gaps

No Discussions. No issue or pull-request templates. No `ROADMAP.md`. No `FUNDING.yml`. No `examples/`.
No screenshots or demo. No `GOVERNANCE.md`, contributor ladder, architectural decision records, or
`CITATION.cff`. No OpenSSF Scorecard or Best Practices badge. No statement of open-core intent. The
release process works but is undocumented.

**The tool that measures bus factor has a bus factor of one.** This is not a joke to a buyer; it is
the first question a platform team asks.

**Tracking:** #95, #96.

---

## 14. Assessment Scorecards

### 14.1 Strategic

| Dimension | Rating |
|---|---:|
| Problem | 8/10 |
| Technical concept | 8/10 |
| Focus | 5/10 |
| Validation | 3/10 |
| OSS traction evidence | 3/10 |
| Developer experience | 6/10 |
| Enterprise readiness | 3/10 |
| Long-term potential | 9/10 |

### 14.2 Engineering

| Dimension | Rating | Main gap |
|---|---:|---|
| Problem selection | 9.5/10 | none |
| Product idea | 9/10 | focus |
| Differentiation | 8.5/10 | proof |
| Engineering hygiene | 8/10 | enforcement of its own standards |
| CODEOWNERS semantics | 8/10 | no differential/fuzz corpus |
| Ownership inference | 7/10 | qualification, history fidelity, calibration |
| Explainability | 6.5/10 | no explain command |
| Performance architecture | 7.5/10 | no incrementality, subprocess fan-out |
| GitHub integration | 7/10 | token never wired, teams not reconciled |
| Bus-factor methodology | 3.5/10 | metric needs redesign |
| Team-topology methodology | 5/10 | connected components too crude |
| Review-balancing methodology | 5/10 | authorship proxy conflated with review load |
| Historical/trend methodology | 5.5/10 | different metric presented as comparable |
| Validation/auditing | 5/10 | validator breadth |
| Supply-chain/reproducibility | 6/10 | Action install path unpinned |
| Packaging/distribution | 5/10 | single channel |
| Documentation | 7.5/10 | story and methodology |
| Scientific calibration | 4/10 | no benchmark, no ground truth |
| Community/adoption | 1/10 | zero visible traction |
| **Current overall** | **~6.7/10** | |
| **Realistic ceiling** | **9.5+/10** | |

### 14.3 Adversarial peer review

| Dimension | Grade |
|---|---|
| Problem selection | A |
| Core architecture | A- |
| Code discipline | A- |
| Conceptual differentiation | A- |
| CODEOWNERS semantics | B+ |
| Inference methodology | C- |
| Bus factor correctness | D |
| Testing strategy | D+ |
| GitHub Action quality | C- |
| Supply chain posture | C |
| Release engineering | D |
| Documentation depth | A- |
| Documentation accuracy | D |
| Positioning honesty | C- |
| Privacy posture | D |
| Performance architecture | C- |
| Distribution | F |
| Community readiness | D |
| Project bus factor | F |
| **Overall** | **Strong B- artifact, F- project** |
| **Alternative aggregation** | **C+ — a B+ engine wrapped in a D- product, shipped through an F- go-to-market** |

---

## 15. Risk Register

### 15.1 Correctness risks by blast radius

| # | Risk | Likelihood | Impact | Severity |
|---|---|---|---|---|
| 1 | Action never passes the token; drift degrades to "cannot compare" | Certain | Core feature non-functional in CI | **Critical** |
| 2 | Blame without `-w -M -C` and ignore-revs; formatter sweeps reassign ownership | High | Confidently wrong owners on mature repos | **Critical** |
| 3 | Fork PR comment 403 fails the whole Action | Certain on public repos | Red X on every external contribution | **Critical** |
| 4 | Unpinned install in a `pull-requests: write` job | Certain | Full supply-chain exposure downstream | **Critical** |
| 5 | Bus factor capped by `top_n_owners` | Certain | Headline metric indefensible | **High** |
| 6 | Confidence capped at 0.85 without the API | Certain | Every gate means something different per config | **High** |
| 7 | `critical` on any count ≤ 1 plus `fail_on_drift: true` | Certain on small repos | Red X on the first PR, immediate uninstall | **High** |
| 8 | Owners generated without verifying write access | High | Silently un-owned paths, false coverage | **High** |
| 9 | Stale organization references in eight locations | Certain | Broken installs on a name collision | **High** |
| 10 | Fully mocked suite over a git-semantics domain | Certain | The next severe bug also ships | **High** |
| 11 | No 3 MB guard on generated CODEOWNERS | Medium on monorepos | GitHub silently loads nothing | **Medium** |
| 12 | Unpaginated `listComments` at 100 | Medium | Duplicate comments on busy PRs | **Medium** |
| 13 | No locking or atomic writes on caches | Medium | Corrupted cache, wrong output | **Medium** |
| 14 | 90-day half-life penalizes stable code | High | False `missing` drift, alert fatigue | **Medium** |
| 15 | Global `handles.json`, negative caching without TTL | Medium | Cross-repo identity leakage, permanent misses | **Medium** |
| 16 | Consolidation ordering silently un-owns a subtree | Medium | Merge-gate bypass | **Medium** |
| 17 | Markdown injection via backticks in paths | Low | Broken or manipulated comment body | **Low** |
| 18 | `install_spec` accepts arbitrary pip specs | Low | RCE if a consumer interpolates untrusted input | **Low** |

### 15.2 Adoption and credibility risks

| Risk | Likelihood | Impact |
|---|---|---|
| Split-brain organization reference persists; every trial user bounces | **Certain today** | Fatal |
| A latest-package install changes CI behavior mid-sprint | High | Severe |
| Someone benchmarks the bus factor against the literature and publishes the critique | Medium | Severe |
| Consolidation ordering silently un-owns a subtree | Medium | Severe |
| A manager uses decay reports in a performance review; tool is org-banned | Medium | Severe |
| Data-protection objection blocks EU adoption | Medium | High |
| Daily confidence drift causes flapping CI; teams disable the check | **High** | High |
| Monorepo user hits the output cap with opaque truncation | Medium | High |
| A breaking dependency release breaks all installs | Medium | High |
| Project reads as abandoned after two quiet months | **High** | High |
| A competitor with distribution adds confidence scoring first | Low–Medium | High |

### 15.3 Structural risks

1. **The biggest risk is not competition.** It is that the project keeps adding impressive-sounding
   analytics before making its definition of ownership empirically and mathematically trustworthy —
   producing twelve commands, twenty reports, beautiful output, and uncertain answers. Correctness
   compounds: if ownership inference is trusted, everything downstream becomes valuable; if it is
   distrusted, everything downstream becomes decoration.
2. **Building for hypothetical enterprises before obtaining open-source users.**
3. **Trying to be everything simultaneously** — CODEOWNERS plus behavioral code analysis plus
   engineering-productivity analytics plus a service catalog plus a reviewer bot plus a developer
   graph plus an onboarding platform.

---

## 16. Known Limitations of the Current Model

What repository evidence **can** support: contribution frequency; recency; current-line attribution;
review participation when API data is available; concentrated activity; stale rules; changing
contribution patterns; possible continuity risk; possible reviewer affinity.

What repository evidence alone **cannot prove**: formal team ownership; architectural decision
authority; organizational accountability; operational ownership; incident responsibility; compliance
responsibility; security responsibility; current employment; reviewer availability; business
ownership; managerial accountability.

Specific distortions present today:

```text
Git history is not complete human expertise.
Squash merges hide authorship and co-authorship.
Blame over-credits formatting changes.
Renames reset ownership history.
CODEOWNERS may intentionally differ from expertise.
Team analysis requires API access and is currently skipped.
Review history availability changes what confidence means.
Historical trend scores use a different model than present-day scores.
Identity resolution can be ambiguous and is cached globally.
Analysis is not reproducible: the same commit scores differently on different days.
```

---

## 17. The Central Diagnosis

The core engineering is materially stronger than the project's adoption, methodology, distribution,
release hygiene, community trust, and evidence.

Feature velocity massively outran validation, distribution, and evidence: `topology`, `balance`,
`onboard`, `trends`, and the graph shipped before a single external user validated that `analyze`
produces owners anyone agrees with. The differentiating metrics are simultaneously the least
defensible parts of the product — "confidence scoring" is four unvalidated weights whose scale
depends on environment, and "bus factor" is a count capped by a display parameter. The primary
distribution surface has one hard functional gap (no token reaches the CLI), one guaranteed failure
mode on public repositories (fork pull-request comments), and an unpinned supply chain, in a tool
marketed to platform and security teams.

**The primary failure mode is false authority.** If teams interpret repository activity as
organizational truth, obvious recommendation errors erode confidence rapidly, and additional reports
only make a wrong answer look sophisticated.

The most instructive fact: the project ships a bus factor of one, an unenforced coverage target, and
a comparison table that omits its real competitors — while selling a tool that detects exactly those
categories of risk in other people's repositories.

The tool today answers *"what should the CODEOWNERS file say."* The distance to answering *"is anyone
actually reviewing this code, and what breaks when they leave"* is roughly one quarter of focused
work, most of it deletion and validation rather than construction.

---

## 18. Open Work

Eighty-five action items are tracked as issues #10–#96, organized into fourteen epics:

| Epic | Theme | Items | Issues |
|---|---|---|---|
| A | Identity, release, provenance | 4 | #10–13 |
| B | Action correctness and supply chain | 9 | #14–22 |
| C | Ownership metric correctness | 9 | #23–31 |
| D | Git evidence fidelity | 7 | #32–38 |
| E | Drift, generation, validation, commands | 12 | #39–50 |
| F | Contracts and policy | 6 | #51–56 |
| G | Module methodology | 7 | #57–62, #65 |
| H | Testing and quality assurance | 7 | #66–72 |
| I | Privacy, ethics, security | 3 | #73–75 |
| J | Performance | 4 | #76–79 |
| K | Distribution and integration | 7 | #80–86 |
| L | Documentation and positioning | 4 | #87–90 |
| M | Validation and proof | 4 | #91–94 |
| N | Community and governance | 2 | #95–96 |

Sixteen are release-blocking for the next corrective release: #10, #11, #12, #13, #14, #15, #16,
#17, #18, #19, #21, #23, #70, #81, #88, #95.
