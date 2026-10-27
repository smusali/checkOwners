# Glossary

One sentence for each term used in reports. Naming rules are in [Terminology](METHODOLOGY.md#terminology).

- **analysis completeness**: the fraction of the evidence catalog that a run actually collected.
- **baseline**: an accepted-findings file that later runs compare against, so they fail only on new findings.
- **candidate backup reviewer**: another scored person on a path, listed after the primary owners as someone who could review it.
- **CODEOWNERS**: the committed file that names who should review each path.
- **confidence delta**: the clamped gap on one path between the owners declared in CODEOWNERS and the owners inferred from history.
- **continuity risk**: a freshness status of `inactive`, `superseded`, or `departed` for a scored owner. `stable` means the gap fits the path and is not a warning.
- **critical-path risk**: the criticality-weighted mean of top-owner share across the reported paths.
- **criticality**: a configured weight in `(0, 1]` for a path glob, used when aggregating repository risk.
- **drift**: a comparison of inferred owners with the committed CODEOWNERS rules, reported with a severity and a maximum score gap.
- **effective owners**: the inverse Herfindahl index of ownership-score shares on a path, `1 / sum(p_i^2)`, computed before `top_n_owners` truncation.
- **evidence quality**: a separate measure of how much configured signal weight was observed, multiplied by analysis completeness, and never folded into the ownership score.
- **exploratory repository topology**: an experimental Louvain clustering of contributors from weighted co-ownership, not an org chart.
- **git authorship proxy**: commit totals reported when recorded reviews are unavailable. They are not review load and they do not recommend a person.
- **load signal**: the label on a balance report: `github_completed_reviews`, `git_authorship_proxy`, or `external`.
- **review concentration**: one person's share of reviews on a single path, reported when that share is at least 0.72 and at least two owners meet the confidence threshold.
- **HHI**: the Herfindahl index of ownership-score shares on a path, the sum of the squared shares.
- **historical activity confidence**: the trends series `historical_activity_confidence`, the mean top score at a period end from recency and frequency only (`historical-v1`). It is not `ownership_score`.
- **historical confidence**: the present `ownership_score` stored on a continuity-risk warning (`historical_confidence`). It is not the trends series.
- **knowledge at risk**: the share of criticality weight on paths whose qualified owner count is at or below the critical threshold.
- **last match wins**: the CODEOWNERS rule that the last matching pattern in file order assigns the owners.
- **major contributor**: a contributor whose ownership-score share on a path is at least 0.05.
- **minor contributor share**: the combined ownership-score share of contributors below 0.05 on a path.
- **ownership graph**: contributors, files, directories, the repository, teams, and CODEOWNERS rules, linked by the relations current evidence can support.
- **ownership score**: a ranking from 0.0 to 1.0 built from the signals that were available, not a calibrated probability.
- **qualified owner count**: the number of owners on a path at or above the confidence threshold after the list is truncated to `top_n_owners`, which is a capped count and not a truck factor.
- **Shannon entropy**: the entropy, in nats, of the ownership-score shares on a path.
- **top owner share**: the largest ownership-score share on a path.
- **truck factor**: on one path, the smallest number of highest-share contributors whose scores sum to a configured fraction of inferred knowledge. The repository truck factor, from `simulate`, is the number of authors whose departure drops file coverage below the first truck-factor threshold.
