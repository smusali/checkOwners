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

## Ownership beliefs

`tests/test_golden.py` is the judgment suite. Each history is a real git
repository with pinned author and committer dates. Analysis uses a fixed as-of
of `2026-06-01T12:00:00+00:00` (the handoff reuses one repository at three
earlier pinned instants). The expected sentence for each case is stored under
the model id, currently `ownership-v3`. A model-version bump fails until that
record gains the new id. A run that breaks a belief fails with the sentence.

Strong ownership in that suite means a score of at least `0.5`. Medium evidence
quality means a quality of at least `0.4` and below `1`.

**Formatting does not confer ownership.** Alice creates `foo.py` (alongside
other tracked files, so the later edit stays under the mass-refactor fraction)
and edits it. Bob changes only whitespace. Alice reviews that change. Alice
stays the primary expert. Bob's blame share is zero, and his score is below
Alice's.

**Ownership moves gradually.** Alice's last commit stays inside the 365-day
lookback. Bob then replaces the file in a few commits across that year. Alice
is primary at the first as-of. Her score is lower at each later as-of and still
above zero at the last one. Bob's score rises, and Bob is primary at the end.

**A single creating commit is strong, with medium evidence.** One commit that
adds a file makes that author the sole primary owner. The score is strong.
Evidence quality is medium, because review and other catalog gaps are missing.

**A full rewrite leaves no current lines for the original author.** Bob
replaces every line Alice wrote. Bob is primary. Alice's blame share is zero.
That share is how this model shows superseded work.

**One person, one owner.** Three commit addresses that `.mailmap` maps to one
canonical email score as a single owner.

**Missing blame and review stay on the unit interval.** An empty tracked file
produces no blame lines, and no review provider is configured, so those two
signals are unavailable. The score stays inside `(0, 1]`. A commit still
supplies frequency, so the case scores the signals that were observed.

**Rename.** Intended belief: ownership survives a rename from `foo.py` to
`bar.py`. Under `ownership-v3`, log aggregation keys on the literal path, so
the author of the rename commit is the scored owner of `bar.py` and the earlier
author is absent.

**Stable cadence.** Intended belief: a path with an 18-month cadence keeps its
owner. Under `ownership-v3`, a commit 18 months before the as-of falls outside
the 365-day lookback and the 90-day half-life, so the path has no scored owner.

**Co-authors.** Intended belief: a `Co-authored-by` trailer receives credit.
Under `ownership-v3`, only the commit author is scored.

When a later model makes one of those three records match the intended belief,
update the `ownership-v3` sentences, or add the new model id, in the same
change.

## Pattern semantics

CODEOWNERS rules are matched in file order. The last match wins. A pattern assigns owners only when it is a supported glob:

- `*` stays inside one path segment. `**` crosses segments.
- A leading `/` anchors the pattern to the repository root. An interior `/` does the same. A bare name floats to any directory.
- `dir/*` matches direct children of `dir`, not deeper files.
- A trailing `/` matches files inside that directory, not a sibling whose name only shares the prefix, and not the directory path itself.
- A line that starts with `!`, or a pattern that contains `[` or `]`, does not assign an owner. A backslash before `#` does not turn a comment into a pattern that matches a path beginning with `#`.

`corpus/compatibility.jsonl` locks these results. A differential test compares the supported glob language (one `**` segment, no leading `!`, no brackets, no `\#`) with an independent matcher. Repeated `**` segments such as `**/**` are outside that comparison.
