# FAQ

Common questions about configuring and operating checkowners. For the full configuration reference see [docs/USAGE.md](USAGE.md).

## Ownership identity

### Will the generated CODEOWNERS show GitHub usernames or commit email addresses?

GitHub usernames whenever they can be resolved. Start with a `.mailmap` at the repo root. That is the cheapest accuracy fix and the first identity stage (`identity.mailmap: true` by default).

Git collapses one person's addresses first. CheckOwners then parses GitHub noreply emails (`12345+login@users.noreply.github.com`) to `@login` locally, reads the on-disk cache (`~/.checkowners/handles.json`, misses remembered), or calls the GitHub user-search API when `GITHUB_TOKEN` is set. When resolution misses (private email, no GitHub account, API unavailable) the entry falls back to the raw email so the output stays usable.

On squash-merge repos most contributors have noreply author emails, so usernames appear even without a token. When two emails resolve to the same username they merge into one owner and the path's qualified owner count is recomputed over distinct people. Set `identity.mailmap: false` to keep raw commit addresses.

```yaml
identity:
  mailmap: true          # default; set false for raw emails
github:
  resolve_handles: true  # default
```

### Does it handle GitHub teams and subteams?

Yes. When `github.org` is set and a token is available, `checkowners generate` collects every team in the org (including nested subteams), and any owner set whose handles are a subset of a team's membership is collapsed to that team. The most deeply-nested matching team wins ties, so subteams are preferred over their parents.

```yaml
github:
  org: my-org
  resolve_teams: true    # default; emits @my-org/platform/backend etc.
```

Disable `resolve_teams` if you want raw `@username` entries even when a team would match.

### Can a report omit people's names?

Yes. `output.anonymize: true` replaces each person with a stable `person-` token for that repository. `output.aggregate_only: true` drops per-person fields and keeps repository metrics. `identity.mode` is `handle` (default), `email`, or `hashed`. `privacy.redact_emails: true` or `--redact-emails` replaces addresses in output with tokens. `contributors.exclude` removes those people from every command, including graph export.

`generate` and `sync` exit 2 under anonymize, aggregate-only, or hashed mode. On disk, state and `handles.json` store email tokens rather than raw addresses. See [Privacy controls](USAGE.md#privacy-controls).

## GitHub API access

### Does checkowners require a GitHub token?

No. The core inference is pure git and runs offline. A token is only needed for three optional features:

| Feature | Config gate | Why a token is needed |
|---------|-------------|-----------------------|
| Email to `@username` resolution (non-noreply emails) | `github.resolve_handles` | GitHub user-search API |
| Team / subteam resolution | `github.resolve_teams` + `github.org` | List org teams + members |
| Review history and team reconciliation | `github.api_enabled` | PR review API + team membership |

Noreply emails (`login@users.noreply.github.com`) resolve to `@login` without any token. The API-backed features also need the `github` extra (`pip install "checkowners[github]"`). Without it they degrade gracefully with a log hint.

Without a token you still get scored ownership, drift detection, qualified owner counts, ownership freshness, and onboarding paths. They operate on email handles and skip the review-activity signal. Remaining weights are renormalized so the score stays on `[0, 1]`. What that score is, and what it is not, is in [Methodology](METHODOLOGY.md). When it can be wrong is in [Limitations](limitations.md).

### What environment variable holds the token?

`GITHUB_TOKEN` (not `GITHUB_API_KEY`). This is the **only** supported way to provide a token. `github.token` is intentionally **not** accepted in `checkowners.yml` because that file gets committed to git and storing a secret there would publish it to GitHub.

`load_config` refuses to load a config that contains `github.token` so a misconfigured repo fails fast instead of silently leaking. The full environment-variable list and precedence live in [docs/USAGE.md](USAGE.md#environment-variables).

```bash
export GITHUB_TOKEN=ghp_...
checkowners generate
```

In GitHub Actions the job token exists as `${{ secrets.GITHUB_TOKEN }}` / `${{ github.token }}`. A `run:` step only sees `GITHUB_TOKEN` if the workflow exports it. The composite action does this for you: `github_token` defaults to `${{ github.token }}` and is exported on the `github-action` step and the PR comment step. Pass a PAT or App token only when the default job token is not enough (org team listing or commenting). See [docs/USAGE.md](USAGE.md#github-actions).

```yaml
- uses: smusali/checkowners@v0
  # github_token defaults to github.token; override only when you need a PAT.
  # @v0 tracks the latest 0.x; @v0.6 tracks 0.6.x; @v0.5 stays on 0.5.x; @v0.6.0 is the pin.
  with:
    github_token: ${{ secrets.GITHUB_TOKEN }}
```

If you invoke the CLI yourself in a `run:` step, export the token:

```yaml
- run: checkowners drift --json
  env:
    GITHUB_TOKEN: ${{ github.token }}
```

### What token scopes are needed?

| Capability | Job token (`permissions`) | Classic PAT | Fine-grained PAT |
|---|---|---|---|
| Email to `@username` (user search) | Default job token is enough (`contents: read` is already required for checkout) | Authenticated token; `read:user` if you want the user-search scope stated explicitly | No extra repository permission |
| Team / subteam resolution | Not available: the default `GITHUB_TOKEN` is repo-scoped and cannot list org teams. Pass a PAT or App token via `github_token` | `read:org` | Organization members: Read |
| Review coverage and review-load balance (`github.api_enabled`) | `pull-requests: read` | `repo` (private) or `public_repo` | Pull requests: Read |
| PR comment (Action `comment_on_pr`) | `pull-requests: write` | `repo` | Pull requests: Write |

Fork `pull_request` runs cannot comment with the job token: GitHub makes that token read-only regardless of the `permissions:` block. The action skips the comment, writes the report to the job summary, and does not fail the job. The same warning-not-failure path applies when the workflow itself is read-only; set `comment_on_pr: false` to skip the attempt.

A fine-grained PAT scoped to the target org with the minimums above is the recommended setup when the job token cannot reach org teams.

## File locations

### Can the CODEOWNERS file live in the repo root instead of `.github/`?

Yes. `checkowners` auto-detects the file at any of the three locations GitHub itself supports, in priority order:

1. `.github/CODEOWNERS`
2. `CODEOWNERS` (repo root)
3. `docs/CODEOWNERS`

The first one that exists wins for `analyze`, `drift`, `validate`, and `sync`. If none exists, `generate` creates `.github/CODEOWNERS` by default; move the file manually if you want a different layout and checkowners will keep using it there.

Note that `generate` and `sync` refuse to overwrite a CODEOWNERS that was not generated by checkOwners (it looks for the machine-generated header). Pass `--force` to replace a hand-written file, or to write a file larger than `output.max_bytes`.

### Where does the config file live?

`.github/checkowners.yml`. There's no auto-detection for the config; it has to live there.

### Where is the state cache?

`~/.checkowners/` (schema 1). `checkowners cache path` prints that directory. State and graph files are named from the normalized `origin` URL, so two checkouts of the same remote share a cache. A repo with no `origin` is keyed by its absolute path, and the stored identity is checked on load, so one repo cannot read another's file. Downstream commands (`qualified-owners`, `decay`, `topology`, `balance`, `onboard`, `expertise`, `graph`) reuse the file when its commit is still `HEAD` and the scoring config hash matches. If not, they re-analyze and say so on stderr. `--allow-stale` reuses a map from an older commit. `--max-age SECONDS` refuses a map older than that (`0` expires immediately). `--no-cache` skips the cache for one run. `cache clear` drops analysis files and keeps `handles.json`. `cache purge` deletes the directory contents, including handle tokens. State and graph files are capped at 256 MiB and the oldest files are removed past that cap. `explain` and `owners` do not read this cache. Override the directory with `CHECKOWNERS_STATE_DIR` for CI or tests. The composite Action uses `~/.checkowners` when `cache` is `"true"`, and `${{ runner.temp }}/checkowners-state` when `cache` is `"false"`. See [docs/USAGE.md](USAGE.md#pipeline).

## Inference behavior

### How is the ownership score computed?

A weighted mean over **available** signals, each in `[0.0, 1.0]`:

- **Recency**: `exp(-ln 2 × days_since_last_commit / half_life)`. Default half-life is 90 days.
- **Frequency**: contributor's commits on the path divided by the path's max contributor.
- **Blame coverage**: fraction of current lines `git blame` attributes to the contributor, after whitespace ignore, optional move/copy detection, and ignore-revs.
- **Review activity**: PR reviews on the path divided by total reviews. Unavailable unless `github.api_enabled` is true; missing review is skipped, not scored as `0.0`.

```text
ownership_score  = Σ(wᵢ × aᵢ × sᵢ) / Σ(wᵢ × aᵢ)
evidence_quality = Σ(wᵢ × aᵢ × rᵢ) / Σ(wᵢ)
```

Weights and per-signal `*_reliability` defaults live under `scoring`. The score is always on `[0.0, 1.0]`; owners below `analysis.confidence_threshold` are dropped from the generated CODEOWNERS. JSON emits `ownership_score`. This is a ranking signal, not a calibrated probability. The formulas, what each signal proxies, the truck-factor divergence, and the project principles are in [Methodology](METHODOLOGY.md). When the tool can be wrong is in [Limitations](limitations.md). If you gate CI on a threshold, re-check it after upgrading: offline scores are no longer capped at `0.85`.

### Can I tune the inference for a high-turnover team?

Yes. Common tunings:

```yaml
scoring:
  recency_half_life_days: 45   # shorten ownership freshness
  recency_weight: 0.5          # weigh "what did you touch last month" higher

decay:
  threshold_days: 90           # flag dormant owners after 3 months

analysis:
  confidence_threshold: 0.4    # stricter cutoff
```

### Why didn't a formatter commit take ownership?

Blame ignores whitespace-only edits (`-w`), follows moved and copied lines when `git.detect_moves` is true, and omits commits listed in `.git-blame-ignore-revs` (or `blame.ignoreRevsFile`). Commits that modify at least `git.mass_refactor_file_fraction` of tracked files (default half the repository) are omitted the same way, so a `black .` or license-header sweep does not become the owner. Add the formatting SHA to `.git-blame-ignore-revs` for an explicit record. Analyze JSON reports whether an ignore-revs file was found, and whether a `.mailmap` was found and applied, under `analysis`.

### Why are some paths missing from `checkowners analyze`?

Four filters can drop a path: `.gitattributes` marks it `linguist-generated` or `linguist-vendored` (`analysis.respect_gitattributes`, on by default), it matches a `paths.exclude` pattern (the fallback list), it no longer exists on disk (deleted files are filtered automatically so CODEOWNERS doesn't pin removed paths), or its only contributors are bots (`analysis.exclude_bots`, on by default). With `qualification.strategy: threshold`, a fifth filter still drops paths where no contributor reaches `analysis.min_commits` within the lookback window. The default `adaptive` strategy keeps those sparse paths and scores them; a one-commit author of a new file can appear as an owner. Analyze JSON reports `analysis.excluded_gitattributes` and `analysis.excluded_static` so a path counted under gitattributes is not counted again under the static list.

## Drift, severity, and CI

`drift.mode` defaults to `commit` (missing + changed). Set `repo` or `both` to also flag stale rules.

### What does drift "severity" mean in CI?

Drift severity maps the max confidence delta plus qualified-owner / decay flags to a tier:

| Severity | Trigger |
|----------|---------|
| `critical` | Any drift entry has `qualified_owner_count <= 1` or is `decay = true` |
| `high` | `max_confidence_delta >= 0.7` |
| `medium` | `max_confidence_delta >= 0.3` |
| `low` | otherwise |

`--json` and the Action output `drift_summary` include the severity field so a workflow can branch on it.

### How do I turn checkOwners on without failing on every existing finding?

`checkowners baseline create` writes `.checkowners-baseline.json` with today's findings. Pass that file with `--baseline`, `drift.baseline_file`, or the Action `baseline` input. Subsequent runs fail only on findings whose identity is not in the file. Identity is `rule` plus `path` plus owner set, so a CODEOWNERS reorder does not invalidate the snapshot. `checkowners --exit-zero drift` is the other adoption path: findings are still printed, and the process exits 0. See [Turning checkOwners on for an existing large repository](USAGE.md#turning-checkowners-on-for-an-existing-large-repository).

### What is the difference between a baseline and a suppression?

The baseline is a bulk snapshot of current debt so CI can start green. A suppression is a named exception with a mandatory reason and an optional expiry. Use a suppression when you can explain one rule on one path; use the baseline when you need to adopt on a large existing tree. Both counts appear in every `drift` and Action summary.

### Why does a suppression require a reason?

A reasonless ignore becomes invisible debt. `load_config` rejects a suppression that omits `reason` or leaves it blank. Expiry is optional. When `expires` is set and the analysis date is after that day, the command fails and prints the path, rule, date, and reason. It does not keep suppressing.

### What happens when I fix a finding that is still in the baseline?

The leftover baseline row is reported as stale (`stale_baseline`). That does not fail the run. Re-run `checkowners baseline create` to drop rows that no longer exist.

### How do I fail a PR only on critical drift?

The example workflow in `.github/workflows/checkowners-example.yml` does this with `fromJson(steps.checkowners.outputs.drift_summary).severity == 'critical'`. The composite action also accepts `fail_on_drift: "false"` if you want the job summary and optional PR comment without blocking.

### Why is analysis completeness below 100% on a normal repository?

The run score counts every missing evidence source, not only the four scoring signals. A repository without `.mailmap`, without `.git-blame-ignore-revs`, or without review history is incomplete even when the owners that were scored look confident. The summary lists each reason. Missing evidence lowers evidence quality. It does not lower the ownership score, and it is not reported as drift.

`--fail-on-incomplete`, or `policy.incomplete_analysis.fail: true`, exits 3 when the score is below 1. Leave it off until the repository can satisfy the sources you care about. `--exit-zero` still turns that 3 into 0.

### What exit code should a script check?

| Code | Meaning |
|------|---------|
| 0 | Clean, or findings hidden by `--exit-zero` |
| 1 | Internal error |
| 2 | Configuration or usage error |
| 3 | Findings (`validate`, `drift`, expired suppressions, incomplete analysis) |
| 4 | Git or GitHub integration failure |

Any non-zero status fails the step. The Action fails from that process status when `fail_on_drift` is true (the default): drift left after the baseline ratchet exits 3. `--exit-zero` and `fail_on_drift: "false"` hide findings only. The full table is in [Exit codes](USAGE.md#exit-codes).

## Positioning

### Is this a performance-measurement tool?

No. This is a knowledge-risk tool, not a performance-measurement tool. Using it for individual evaluation is unsupported and harmful. The project will not be positioned around who contributes least, who is really working, or which engineer is underperforming. Position it around repository resilience, knowledge continuity, review routing, organizational alignment, and onboarding. The ten principles are in [Methodology](METHODOLOGY.md#principles). What repository evidence cannot prove is in [Limitations](limitations.md). What is read, what leaves the machine, and how to delete the cache are in [Privacy](PRIVACY.md).

### How does CheckOwners compare to other CODEOWNERS tools?

CheckOwners treats code ownership as a scored spectrum rather than a static binary declaration. No other open-source tool combines git-history inference, per-path ownership scores with evidence quality, pattern-aware drift with severity tiers, and knowledge-risk reporting behind a single CI-native JSON contract.

Dedicated validators still go further on owner validity: they verify that accounts exist and that users and teams belong to the organization. Formal bus/truck-factor research tools run a removal simulation over a knowledge distribution. CheckOwners `qualified_owner_count` is a capped count of owners above the confidence threshold; it is not truck factor, bus factor, or lottery factor. See [Qualified owner count](USAGE.md#qualified-owner-count) and [Knowledge concentration](METHODOLOGY.md#knowledge-concentration).

The full matrix and what each category does well live in [How checkowners compares](USAGE.md#how-checkowners-compares).

### Why did scores used to change overnight with no commits?

Recency used to age against the wall clock, so the same commit scored differently each day. Analysis now uses one instant: `--as-of`, else `SOURCE_DATE_EPOCH`, else the HEAD committer timestamp. The same repository, commit, and config produce the same JSON. Pass `--as-of 2026-01-15T00:00:00+00:00` to pin an audit instant. `--deterministic` is the documented name for that guarantee.

### Does CheckOwners use LLMs?

No LLM is in the inference path. Core analysis is deterministic git work (`git log`, `git blame`) plus optional GitHub API lookups. The scoring heuristics live in `analyze.py` and are auditable. The codebase has been built with agent assistance; every AI-assisted change is human-reviewed, tested, and signed off.

## Reporting

### How do I report a wrong owner?

Open a [false-positive report](https://github.com/smusali/checkowners/issues/new?template=false-positive.yml). The form asks for the path, inferred owner, expected owner, command, configuration, repository shape, and merge strategy. Those reports feed the public false-positive ledger. Crashes and CLI defects use the [bug form](https://github.com/smusali/checkowners/issues/new?template=bug.yml). Usage questions go to [Discussions](https://github.com/smusali/checkowners/discussions/categories/q-a).

## Troubleshooting

### Why did generate warn about `routes/[id]/` and emit per-file rules?

GitHub skips CODEOWNERS lines that contain `[...]` character ranges, so Next.js and Remix dynamic segments cannot be written as-is. Generate rewrites those segments to `*`. When that wildcard would also own paths that were inferred for someone else (`routes/[slug]/`, `routes/static/`), the default is to refuse the broad rule, warn, and emit per-file rules. Those per-file lines stay GitHub-valid when a narrower `*` is unique; otherwise they keep the literal `[id]` path, which GitHub will ignore. Pass `--allow-broad-patterns` (or set `output.allow_broad_patterns: true`) only when you accept that over-ownership. `--force` does not opt in. `generate --json` records the affected paths and owner delta under `broad_patterns`.

### `generate` failed with a round-trip verification error.

The generated rules did not resolve to the owners generation assigned. The message names the path, the intended owners, the resolved owners, and the winning rule. Last-match-wins ordering is the usual cause: a later broader rule (for example `*`) un-owns a subtree that a directory rule was meant to cover. Fix the ordering, or set `output.consolidate: false` and regenerate. `--force` does not skip this check. `checkowners explain-path <path>` shows the match chain for a single path.

### `generate` refused because the file is too large.

GitHub does not load a CODEOWNERS file over 3 MB. Generate warns at 2 MB and refuses above `output.max_bytes` (default 2_500_000) unless you pass `--force`. Consolidation (`output.consolidate: true`) is the usual way to stay under the ceiling.

### How do I see which CODEOWNERS rule owns a path?

```bash
checkowners explain-path src/main.py
checkowners explain-path src/main.py --json
```

The command reads the existing CODEOWNERS file and lists every matching rule in file order. The last match wins. It does not re-run analysis.

### How do I see why someone was inferred (or not) for a path?

`checkowners explain-path` answers the declared-rule question. `checkowners explain PATH` answers the inference question: per-signal scores, availability, named commits, and `--why-not @handle`. `checkowners owners PATH` (or `who`) prints only the ranked list. Those two commands analyze the requested path only and do not use the cached state.

### `networkx` is not installed but I want `checkowners graph`.

Install the extra: `pip install "checkowners[graph]"`. The error message points to this too.

### `checkowners drift` complains about lines with `# alice(0.92)`.

You're on an older version that predates the inline-comment fix. Upgrade to v0.3.0+ or strip the annotations by setting `output.include_confidence: false` and regenerating.

### The qualified-owners report says `repo_average: 1.0`. Is that right?

For a solo-maintainer repo, yes. `qualified_owner_count` is the number of selected owners with confidence at or above `analysis.confidence_threshold`, after the list has already been truncated to `analysis.top_n_owners` (default 3). A single committer is 1 per path. The value cannot exceed `top_n_owners`, so fifteen healthy contributors report the same count as three. This is not truck factor, bus factor, or lottery factor: there is no removal simulation and no repo-level truck factor. Per-path JSON reports score-mass concentration, computed before that cap, as `risk.top_owner_share`, `risk.effective_owners` (`1 / sum(p_i^2)`), `risk.truck_factor_50`, `risk.truck_factor_75`, and `risk.truck_factor_90`. Changing `top_n_owners` does not change those numbers. See [Qualified owner count](USAGE.md#qualified-owner-count). The composite Action does not open a knowledge-risk PR comment for the solo-maintainer case: single-owner paths are expected when only one human has qualified ownership.
