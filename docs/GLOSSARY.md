# Glossary

One sentence for each term used in reports. Naming rules are in [Terminology](METHODOLOGY.md#terminology).

- **analysis completeness**: the fraction of the evidence catalog that a run actually collected.
- **baseline**: an accepted-findings file that later runs compare against, so they fail only on new findings.
- **candidate backup reviewer**: another scored person on a path, listed after the primary owners as someone who could review it.
- **CODEOWNERS**: the committed file that names who should review each path.
- **confidence delta**: the clamped gap on one path between the owners declared in CODEOWNERS and the owners inferred from history.
- **continuity risk**: a warning that a scored owner's last commit on a path is older than the decay threshold.
- **drift**: a comparison of inferred owners with the committed CODEOWNERS rules, reported with a severity and a maximum score gap.
- **effective owners**: the inverse Herfindahl index of ownership-score shares on a path, `1 / sum(p_i^2)`, computed before `top_n_owners` truncation.
- **evidence quality**: a separate measure of how much configured signal weight was observed, multiplied by analysis completeness, and never folded into the ownership score.
- **exploratory repository topology**: a clustering of contributors from commit co-occurrence, not an org chart.
- **git authorship proxy**: a review-load stand-in computed from commits when recorded reviews are unavailable.
- **HHI**: the Herfindahl index of ownership-score shares on a path, the sum of the squared shares.
- **historical activity confidence**: the present ownership score stored on a continuity-risk warning, not a score reconstructed at an earlier date.
- **last match wins**: the CODEOWNERS rule that the last matching pattern in file order assigns the owners.
- **major contributor**: a contributor whose ownership-score share on a path is at least 0.05.
- **minor contributor share**: the combined ownership-score share of contributors below 0.05 on a path.
- **ownership graph**: the graph of inferred owners and the paths they score on.
- **ownership score**: a ranking from 0.0 to 1.0 built from the signals that were available, not a calibrated probability.
- **qualified owner count**: the number of owners on a path at or above the confidence threshold after the list is truncated to `top_n_owners`, which is a capped count and not a truck factor.
- **Shannon entropy**: the entropy, in nats, of the ownership-score shares on a path.
- **top owner share**: the largest ownership-score share on a path.
- **truck factor**: the smallest number of highest-share contributors whose scores sum to a configured fraction of inferred knowledge on one path.
