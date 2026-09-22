# Methodology

How CheckOwners turns git history into an ownership ranking. This page is the
scoring contract. It is not a validation study: the score is a ranking signal,
not a calibrated probability.

## Signals

For contributor `u` on path `p`:

| Signal | Meaning | Available when |
|--------|---------|----------------|
| Recency | Exponential decay from last commit, half-life `scoring.recency_half_life_days` (default 90), aged against `analysis_epoch` (HEAD committer time by default, never the wall clock) | The owner has a last-commit timestamp |
| Frequency | `commits(u, p) / (max_commits(p) + prior)` over the lookback window, with author emails from `git log` after `.mailmap` when `identity.mailmap` is on. `prior` is `3` when `qualification.strategy` is `adaptive`, and `0` when it is `threshold` | Commit counts exist for the path |
| Blame | Share of current lines `git blame` (porcelain, `-w`, `.mailmap` when `identity.mailmap` is on, and `-M`/`-C` when `git.detect_moves`) attributes to `u`. Commits listed in a resolved ignore-revs file, and mass-refactor commits above `git.mass_refactor_file_fraction`, are omitted so the prior author keeps the line | Blame ran and produced lines for the path |
| Review | Share of PR reviews on `p` attributed to `u` | A review provider was injected (`github.api_enabled` plus token and `GITHUB_REPOSITORY`) |

An author missing from a successful blame map, or from a provider result, scores
`0.0` with the signal still available. That is measured zero, not missing
evidence. Blame that failed or was skipped, and a run with no review provider,
mark those signals unavailable.

## Ownership score

Missing evidence is not negative evidence. Unavailable signals are omitted and
the remaining weights are renormalized so the attainable range is `[0, 1]` in
every configuration:

```text
ownership_score = Σ(wᵢ × aᵢ × sᵢ) / Σ(wᵢ × aᵢ)
```

`wᵢ` is the configured weight, `aᵢ ∈ {0, 1}` is availability, and `sᵢ` is the
per-signal score. If no signal is available the score is `0.0`. Default weights
are `0.35 / 0.25 / 0.25 / 0.15` for recency, frequency, blame, and review.

`analyze` and `trends` share one helper. Trends cannot reconstruct historical
blame or review, so those two signals are unavailable and the same formula
renormalizes over recency and frequency.

JSON emits `ownership_score` as the primary key and `confidence` as a deprecated
alias for one cycle. `analysis.confidence_threshold` still gates this number.

## Evidence quality

Evidence quality is a separate quantity and is never folded into the score:

```text
signal_quality = Σ(wᵢ × aᵢ × rᵢ) / Σ(wᵢ)
evidence_quality = signal_quality × analysis_completeness
```

`rᵢ` is `scoring.*_reliability` (default `1.0` for every signal). With those
defaults, `signal_quality` equals the fraction of configured weight that was
observed. Unavailable review therefore lowers quality (by `0.15` by default)
without pulling the owner's score toward zero. `analysis_completeness` is the
fraction of the evidence catalog that was collected (shallow history, token,
review history, rate limit, budgets, team membership, identity, exclusions,
renames, mailmap, ignore-revs). It is recomputed from the signal breakdown
whenever gaps change, so the run score is applied once. A missing signal stays
unavailable. A measured zero stays zero. The ownership score is not multiplied.

Human output shows both values as `handle (score/quality)`, for example
`@alice 0.91/0.93` versus `@bob 0.79/0.31`.

`checkowners explain PATH` prints the same C5 weighted mean as `analyze`. Each
row shows the four signals with their configured weight and availability. A
measured zero (author absent from a successful blame or review map) is a
number. An unavailable signal is omitted from the mean and shown as `n/a`.
The printed total equals `Σ(wᵢ × aᵢ × sᵢ) / Σ(wᵢ × aᵢ)` on the displayed
tuples. `--why-not` uses that same total against
`analysis.confidence_threshold`.

## Versioning

Three model ids are stamped on JSON output and on human reports:

| Id | Config key | What it names |
| --- | --- | --- |
| `ownership-v3` | `model.ownership` | The renormalized score in this page |
| `risk-v1` | `model.risk` | Qualified-owner count and its critical/warn thresholds |
| `topology-v1` | `model.topology` | Co-occurrence clusters |

A formula change, a classifier change, or a topology-algorithm change bumps the matching id. That bump is a breaking change for anyone gating CI on a threshold, even when the JSON shape is unchanged. A config pin must equal the id this release implements, or be omitted. Cached analyze state and the graph cache are reused only when all three ids match.

Analyze JSON still includes `model_version: ownership-v3` for one minor cycle. Per-repo state is schema v7. Files without a matching `models` object, files whose `model_version` is not `ownership-v3`, and files whose scoring config hash does not match are ignored and replaced on the next analyze. A cached map is reused only when its `analysis_ref` is still `HEAD`, unless `--allow-stale` is set. Recency and the lookback window are evaluated at `analysis_epoch`, not the wall clock. JSON payloads emit `analysis_ref` (HEAD SHA) and `analysis_epoch`.

`qualification.strategy: adaptive` (the default) treats commit count as evidence:
authors below `min_commits` still qualify when blame is at least
`strong_blame_override`. `strategy: threshold` keeps the previous eligibility
gate and the undamped `commits / max_commits` frequency ratio.

Calibration against ground truth, and weight search, are out of scope here.
