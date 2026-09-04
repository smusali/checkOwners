# Methodology

This document outlines how `checkowners` analyzes git history to infer repository evidence, how its metrics are calculated, where they diverge from established literature, and the philosophical principles guiding the project.

## Project Principles

1. **Evidence over guesses**: We compute metrics based on observable repository data, never assumptions.
2. **Human policy remains authoritative**: Automated inference does not override explicit human decisions (e.g., in a `CODEOWNERS` file).
3. **Missing evidence is not negative evidence**: A lack of commits does not necessarily mean a lack of knowledge or authority.
4. **Local-first**: Analysis runs locally on `git` history by default.
5. **Deterministic by default**: Given the same git history and configuration, the output is identical.
6. **Explain everything**: Every score and warning must be traceable to raw data.
7. **Measure uncertainty**: Confidence bounds are first-class primitives.
8. **Compatibility matters**: Output should integrate with existing tools cleanly.
9. **No employee surveillance**: Metrics evaluate repository health, not individual performance or ranking.
10. **Correctness before feature count**: A trustworthy foundation is strictly prioritized over additional analytical features.

## Explicit Statement

**Observed expertise is evidence, not authority.** 
Git history provides a proxy for where attention has been spent. It does not definitively prove formal team ownership, architectural decision authority, organizational accountability, operational ownership, incident responsibility, compliance responsibility, security responsibility, current employment, reviewer availability, business ownership, or managerial accountability.

## Terminology Guardrails

To prevent the over-claiming of automated metrics, the following terminology mappings are enforced in all output and documentation. **Never collapse** contribution into ownership; blame into authority; recent activity into expertise; co-commit clusters into teams; inferred reviewer affinity into organizational accountability; or an uncalibrated score into a probability.

| Instead of | Use |
| :--- | :--- |
| organizational owner | repository-evidence candidate · reviewer candidate · evidence-ranked reviewer |
| expertise decay | ownership freshness · evidence freshness · continuity risk |
| backup reviewer | candidate backup reviewer |
| bus factor (as previously computed) | qualified owner count · reviewer depth |
| uncalibrated 0.92 | evidence score · ranking signal · ownership score |
| knowledge graph (as modeled) | ownership graph |
| org chart | exploratory repository topology |
| review load (from commits) | git authorship proxy |
| historical confidence | historical activity confidence |

## Formulas

### Ownership Score

The core ownership score for a given author on a path is bounded to `[0.0, 1.0]` and computed as a weighted sum of four signals:
- **Recency**: `(0.5) ^ (days_since_last_commit / half_life_days)`
- **Frequency**: `author_commits / max_commits_on_path`
- **Blame**: The fraction of lines currently attributed to the author via `git blame`.
- **Review**: The fraction of PRs touching the path reviewed by the author.

```python
score = clamp(
    (weight_recency * recency) + 
    (weight_frequency * frequency) + 
    (weight_blame * blame) + 
    (weight_review * review)
)
```

### Truck Factor & Reviewer Depth

Historically (pre-C2), our "bus factor" was simply a **qualified owner count**—it counted the number of contributors whose ownership score exceeded the configured confidence threshold, capped by the `top_n_owners` setting. 

*Divergence:* The academic literature defines truck factor (or bus factor) as a **removal simulation**: the minimal set of contributors whose departure leaves the project (or a threshold fraction of files) without a knowledgeable owner. 
*Fix:* We now clearly distinguish our metric as `reviewer depth` or `qualified owner count` to avoid conflicting with the literature's removal simulation.

## Golden-Test Cases (H3)

Our ownership philosophy asserts that active, recent contribution combined with current line attribution is the strongest proxy for candidate reviewers. Test scenarios are designed to ensure that the scoring model does not artificially elevate stale knowledge over fresh architectural changes, even if the absolute commit counts differ significantly. 

## Benchmark & Calibration Methodology (J4 & M1)

- **Benchmark Methodology (J4)**: We evaluate the performance of our inference against ground-truth `CODEOWNERS` files in large, established open-source repositories to measure precision and recall of the evidence-ranked reviewers.
- **Calibration Methodology (M1)**: Pending validation study completion, calibration involves tuning the weight coefficients to align the output confidence scores with probabilistic interpretations of reviewer availability and accuracy.

## Metric Entries

Every shipped metric is backed by git evidence or GitHub API data:
- `analyze`: Scans `git log` and `git blame` to compute the foundational ownership score.
- `bus-factor` / `qualified owner count`: Computes the number of candidate reviewers above the confidence threshold.
- `decay` / `continuity risk`: Flags paths where the most recent contribution from top candidate reviewers exceeds a freshness threshold.
- `topology`: Clusters co-commit patterns to suggest exploratory repository topology.
- `balance`: Evaluates the git authorship proxy or GitHub PR review load to highlight reviewer concentration.
- `onboard`: Navigates the ownership graph to suggest related learning paths.
- `trends`: Tracks historical activity confidence over specified periods.
