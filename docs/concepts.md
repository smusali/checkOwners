# Concepts

Short definitions. Formulas live in [Methodology](METHODOLOGY.md). One-line report terms live in the [Glossary](GLOSSARY.md).

## Declared vs observed

CODEOWNERS is the declared policy. Git history and, when a provider is configured, reviews are the observed evidence. Drift is the comparison of those two layers. `RepositoryAnalyzer.reconcile()` also reports declared-local owners from the nearest `OWNERS` file and nested CODEOWNERS, declared-global owners from the repository CODEOWNERS match, and observed owners at file, directory, package, service, and repository scope.

## Ownership score

`ownership_score` is a ranking from 0.0 to 1.0 built from the signals that were available for that path. It is not a probability that the person should own the path. The signals and the formula are in [Ownership score](METHODOLOGY.md#ownership-score).

## Evidence quality

`evidence_quality` is separate from the score. It measures how much of the configured signal weight was observed, scaled by analysis completeness. Two owners can share a score and differ in quality. The definition is in the [Glossary](GLOSSARY.md).

## Drift

Drift reports where inferred owners and the committed CODEOWNERS rules disagree, with a severity and a maximum score gap. Severity tiers, baselines, and policy are in [Drift and severity](USAGE.md#drift-and-severity).

## Knowledge risk

`qualified_owner_count` is the number of owners at or above the confidence threshold after the displayed list is capped. It is not a truck factor. Truck factor is the separate departure measure from `simulate`. Concentration, decay, and the repository risk report are in [Knowledge concentration](METHODOLOGY.md#knowledge-concentration).

## Identity resolution

Resolution prefers a configured identity, then `.mailmap`, a GitHub noreply address, this repository's handle cache, `author.login` on a local commit, and user search last. Search relabels one email and does not merge two owners. The stages are in [Identity resolution](USAGE.md#identity-resolution).
