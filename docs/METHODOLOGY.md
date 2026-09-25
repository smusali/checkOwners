# Methodology

How CheckOwners turns git history into an ownership ranking. This page is the
scoring contract. It is not a validation study: the score is a ranking signal,
not a calibrated probability.

## Signals

For contributor `u` on path `p`:

| Signal | Meaning | Available when |
|--------|---------|----------------|
| Recency | Exponential decay from last commit, half-life `scoring.recency_half_life_days` (default 90), aged against the analysis instant (HEAD committer time by default, never the wall clock) | The owner has a last-commit timestamp |
| Frequency | `commits(u, p) / (max_commits(p) + prior)` over the lookback window, with author emails from `git log` after `.mailmap` when `identity.mailmap` is on. `prior` is `3` when `qualification.strategy` is `adaptive`, and `0` when it is `threshold` | Commit counts exist for the path |
| Blame | Share of current lines `git blame` (porcelain, `-w`, `.mailmap` when `identity.mailmap` is on, and `-M`/`-C` when `git.detect_moves`) attributes to `u`. Commits listed in a resolved ignore-revs file, and mass-refactor commits above `git.mass_refactor_file_fraction`, are omitted so the prior author keeps the line | Blame ran and produced lines for the path |
| Review | Share of PR reviews on `p` attributed to `u` | A review provider was injected (`github.api_enabled` plus token and `GITHUB_REPOSITORY`) |

An author missing from a successful blame map, or from a provider result, scores
`0.0` with the signal still available. That is measured zero, not missing
evidence. Blame that failed or was skipped, and a run with no review provider,
mark those signals unavailable.

## What each signal proxies

| Signal | It measures | It proxies for | It does not establish |
|--------|-------------|----------------|------------------------|
| Recency | Days since the last commit, with exponential half-life decay | How fresh the contact with the path is | Expertise, availability, or current employment |
| Frequency | Share of commits on the path inside the lookback window | Repeated authorship | Ownership, or that a squash-merge author wrote the lines |
| Blame | Share of current lines attributed to the person | Who the current text is attributed to, after whitespace, move, and ignore-revs corrections | Authority to decide, or that a formatter did not touch the file |
| Review | Share of recorded pull-request reviews on the path | Review participation when a provider returned data | Organizational accountability, or that the person is the right reviewer now |

A high `ownership_score` is a ranking of that evidence. It is not a probability
that the person should own the path.

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

JSON emits `ownership_score`. `analysis.confidence_threshold` still gates this number.

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
| `ownership-v1` | `model.ownership` | The renormalized score in this page |
| `risk-v1` | `model.risk` | Inverse-Herfindahl concentration and the qualified-owner classifier |
| `topology-v1` | `model.topology` | Co-occurrence clusters |

A formula change, a classifier change, or a topology-algorithm change bumps the matching id. That bump is a breaking change for anyone gating CI on a threshold, even when the JSON shape is unchanged. A config pin must equal the id this release implements, or be omitted. Cached analyze state and the graph cache are reused only when all three ids match.

JSON carries `models.ownership`, `models.risk`, and `models.topology`. Per-repo state is schema 1. Email addresses in that state are stored as tokens.

Files without a matching `models` object, and files whose scoring config hash does not match, are ignored and replaced on the next analyze. A cached map is reused only when its commit is still `HEAD`, unless `--allow-stale` is set. Recency and the lookback window are evaluated at the analysis instant, not the wall clock. JSON payloads emit `head_sha` and `generated_at`.

`qualification.strategy: adaptive` (the default) treats commit count as evidence:
authors below `min_commits` still qualify when blame is at least
`strong_blame_override`. `strategy: threshold` keeps the previous eligibility
gate and the undamped `commits / max_commits` frequency ratio.

Weight search has not been run. See [Benchmark and calibration](#benchmark-and-calibration).

## Knowledge concentration

`qualified_owner_count` is the number of owners on a path whose
`ownership_score` is at or above `analysis.confidence_threshold`, taken from
the list already truncated to `analysis.top_n_owners` (default 3). JSON emits
`qualified_owner_count` and `qualified_owner_count_cap`. Human output states
the cap. Raising `top_n_owners` raises the maximum reportable count without
any code changing hands.

Before that rename, the same count was labeled bus factor. It was a
qualified-owner count, not a removal simulation. The count is unchanged. The
`bus_factor` config section classifies the capped count.

Concentration uses every contributor at or above
`analysis.confidence_threshold`, before `top_n_owners` truncation. The
displayed owner list is still truncated. Expertise is `ownership_score`. A
survival-weighted score is not part of this model. Shares are not a JSON key.
For each positive score on that untruncated list:

```text
p_i = score_i / sum(score)
top_owner_share = max(p_i)
hhi = sum(p_i^2)
effective_owners = 1 / hhi
shannon_entropy = -sum(p_i * ln(p_i))
truck_factor_50 = smallest k whose largest shares sum to at least q_50
truck_factor_75 = smallest k whose largest shares sum to at least q_75
truck_factor_90 = smallest k whose largest shares sum to at least q_90
```

`q_50`, `q_75`, and `q_90` are `risk.truck_factor_thresholds`, default
`[0.50, 0.75, 0.90]`, in that order. The array is repeated on the risk
object. `shannon_entropy` is in nats. `hhi` is the Herfindahl index on the
unit interval. `effective_owners` is the inverse Herfindahl index: one owner
is `1.00`, an even split of two is `2.00`, and four equal shares are `4.00`.
It is not the exponential of Shannon entropy.

A contributor is major when `p_i >= 0.05`. `major_contributor_count` is that
count. `minor_contributor_share` is the sum of the other shares. The 5% line
is fixed.

An empty or all-zero list reports `top_owner_share` `0.0`,
`effective_owners` `0.0`, `shannon_entropy` `0.0`, `hhi` `0.0`,
`minor_contributor_share` `0.0`, `major_contributor_count` `0`, and truck
factors `0`.

These truck factors are coverage counts on one path's score mass. They are
not a repository truck factor. There is no removal simulation and no
`simulate` command. Changing `top_n_owners` changes the displayed list and
`qualified_owner_count`. It does not change concentration. The `bus_factor`
config section still classifies the capped count: `critical` at or below
`critical_threshold`, `warning` at or below `warn_threshold`.

Repository output does not average that capped count. It distributes
`top_owner_share` after weighting each path by `criticality`. The first
matching glob wins. An unmatched path weighs `1.0`. An empty map uses that
weight and sets `criticality_incomplete`, because risk without a criticality
map is incomplete. Weighted percentiles of `top_owner_share` are `minimum`,
`p10`, `median`, and `p90`, taken as the first share whose cumulative weight
reaches the quantile. `critical_path_risk` is `sum(weight * top_owner_share) / sum(weight)`.
`knowledge_at_risk` is the share of total weight on paths whose
`qualified_owner_count` is at or below `critical_threshold`.

## Prior art

Published truck factor, also called bus factor or lottery factor, is the
smallest set of people whose departure leaves a project, or a threshold
fraction of its files, without a knowledgeable author.

Avelino, Passos, Hora, and Valente, "A Novel Approach for Estimating Truck
Factors" (ICPC 2016, [arXiv:1604.06766](https://arxiv.org/abs/1604.06766)),
estimate degree of authorship per file, keep authors above a normalized
threshold, then greedily remove the author who covers the most files until at
least half the files have no remaining author. Ferreira, Valente, and
Ferreira, "A Comparison of Three Algorithms for Computing Truck Factors"
(ICPC 2017), compare that algorithm with the blame-based estimate of Rigby et
al. and the file-then-project estimate of Cosentino et al.

Degree of knowledge, Fritz, Ou, Murphy, and Murphy-Hill (ICSE 2010) and Fritz,
Murphy, Murphy-Hill, Ou, and Hill, "Degree-of-Knowledge: Modeling a
Developer's Knowledge of Code" (TOSEM 2014), adds interaction (reviews,
navigation) to authorship. CheckOwners does not compute degree of authorship
or degree of knowledge, and it does not run that removal simulation.

The divergence is deliberate. The old headline number was the capped
`qualified_owner_count`. Calling it bus factor overclaimed the literature.
CheckOwners reports per-path knowledge shares, inverse-Herfindahl
`effective_owners`, and `truck_factor_50`, `truck_factor_75`, and
`truck_factor_90` over every score above the confidence threshold. Those
counts remove the highest-knowledge contributors on one path until a share of
inferred knowledge is gone. They do not remove people until files lose their
last author, which is the truck factor in Avelino et al. and Ferreira et al.

## Terminology

Use these names in human-facing output. JSON keys and command names stay as
shipped. Definitions are in [docs/GLOSSARY.md](GLOSSARY.md).

| Instead of | Use |
|---|---|
| organizational owner | repository-evidence candidate, reviewer candidate, evidence-ranked reviewer |
| expertise decay | ownership freshness, evidence freshness, continuity risk |
| backup reviewer | candidate backup reviewer |
| bus factor (as previously computed) | qualified owner count, reviewer depth |
| uncalibrated `0.92` | evidence score, ranking signal, ownership score |
| knowledge graph (as modeled) | ownership graph |
| org chart | exploratory repository topology |
| review load (from commits) | git authorship proxy |
| historical confidence | historical activity confidence |

## Never collapse these

- Contribution into ownership.
- Blame into authority.
- Recent activity into expertise.
- Co-commit clusters into teams.
- Inferred reviewer affinity into organizational accountability.
- An uncalibrated score into a probability.

## Other reported numbers

`historical_confidence` is the present `ownership_score` stored on a
continuity-risk warning: the owner's last commit on that path is older than
`decay.threshold_days`. It is not a score reconstructed at an earlier date.
Human output calls this historical activity.

`avg_top_confidence` is the mean, across paths that still have a scored owner,
of the top score at the end of one trend period. `avg_qualified_owner_count`
is the mean capped count for those same paths. Trend periods use recency and
frequency only. Blame and review are unavailable, so a trend score is not the
same number `analyze` would have produced on that day.

`confidence_delta` is the drift gap between declared CODEOWNERS owners and
inferred owners on one path: the sum of inferred scores for people who are
inferred but not declared, plus `1.0` for each declared owner who is not
inferred, clamped to `[0, 1]`. `max_confidence_delta` is the largest absolute
`confidence_delta` in the drift result, or `0.0` when there are no entries.

## Ownership beliefs

These cases are the project's stated ownership philosophy.
`tests/test_golden.py` is the judgment suite. Each history is a real git
repository with pinned author and committer dates. Analysis uses a fixed as-of
of `2026-06-01T12:00:00+00:00` (the handoff reuses one repository at three
earlier pinned instants). The expected sentence for each case is stored under
the model id, currently `ownership-v1`. A model-version bump fails until that
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
`bar.py`. Under `ownership-v1`, log aggregation keys on the literal path, so
the author of the rename commit is the scored owner of `bar.py` and the earlier
author is absent.

**Stable cadence.** Intended belief: a path with an 18-month cadence keeps its
owner. Under `ownership-v1`, a commit 18 months before the as-of falls outside
the 365-day lookback and the 90-day half-life, so the path has no scored owner.

**Co-authors.** Intended belief: a `Co-authored-by` trailer receives credit.
Under `ownership-v1`, only the commit author is scored.

When a later model makes one of those three records match the intended belief,
update the `ownership-v1` sentences, or add the new model id, in the same
change.

## Benchmark and calibration

No performance benchmark has been published. The intended measurements, when a
harness exists, are cold runtime, warm runtime, peak resident memory, git
command count, API request count, cache size, and incremental runtime, on
fixed repository shapes. Until those runs exist, do not treat a single
timing as a benchmark.

On the 0.5.0 dogfood run, a 24k-commit, 12k-file production monorepo finished
a 365-day analyze in under three minutes. That figure is one measurement on
one machine.

No calibration study has been run. The default weights
`0.35 / 0.25 / 0.25 / 0.15` are heuristics. When a study is run, ground truth
is who later maintains and reviews the code, not the CODEOWNERS file that was
already committed. Existing CODEOWNERS can be stale, which is the condition
the tool exists to detect.

The study will report ranking quality (precision and recall at small cutoffs)
against future authors and reviewers. It will not treat an `ownership_score`
as a calibrated probability until that evidence exists.

## Principles

1. **Evidence over guesses.** Every ownership recommendation must derive from inspectable evidence.
2. **Human policy remains authoritative.** Observed expertise informs CODEOWNERS. It does not silently override governance.
3. **Missing evidence is not negative evidence.** Unavailable review or team information reduces `evidence_quality`. It does not arbitrarily depress `ownership_score`.
4. **Local-first.** Core analysis works without uploading proprietary source.
5. **Deterministic by default.** The same evidence and the same model produce the same result.
6. **Explain everything.** Every score and recommendation is inspectable.
7. **Measure uncertainty.** Thin evidence is not certainty. Report `evidence_quality` and `analysis_completeness` beside the score.
8. **Compatibility matters.** GitHub CODEOWNERS semantics must be reproduced accurately where the pattern language is supported.
9. **No employee surveillance.** The project optimizes repository resilience, knowledge continuity, review routing, organizational alignment, and onboarding. It will not be positioned around who contributes least, who is really working, or which engineer is underperforming. The data is not designed for that.
10. **Correctness before feature count.** A new report waits until the number it prints is defined and bounded.

**Observed expertise is evidence, not authority.**

## Pattern semantics

CODEOWNERS rules are matched in file order. The last match wins. A pattern assigns owners only when it is a supported glob:

- `*` stays inside one path segment. `**` crosses segments.
- A leading `/` anchors the pattern to the repository root. An interior `/` does the same. A bare name floats to any directory.
- `dir/*` matches direct children of `dir`, not deeper files.
- A trailing `/` matches files inside that directory, not a sibling whose name only shares the prefix, and not the directory path itself.
- A line that starts with `!`, or a pattern that contains `[` or `]`, does not assign an owner. A backslash before `#` does not turn a comment into a pattern that matches a path beginning with `#`.

`corpus/compatibility.jsonl` locks these results. A differential test compares the supported glob language (one `**` segment, no leading `!`, no brackets, no `\#`) with an independent matcher. Repeated `**` segments such as `**/**` are outside that comparison.
