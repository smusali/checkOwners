# Limitations of Repository Evidence Inference

While `checkowners` provides powerful, deterministic signals from repository data, its underlying models have fundamental limitations. An open-source intelligence tool earns trust by explaining exactly when and how it can be wrong.

## What Git History Cannot Capture

- **Git history is not complete human expertise:** Mentorship, architectural planning, pair programming (when uncredited), and off-repo discussions build expertise that leaves no git footprint.
- **Squash merges can hide authorship:** When PRs are squash-merged without preserving co-authors, the primary author absorbs all commit frequency credit, obscuring secondary contributors.
- **Blame can over-credit formatting changes:** Mass formatting, variable renaming, or linting fixes can rewrite large portions of `git blame`. While our confidence score balances blame against frequency and recency, severe refactors still skew the blame fraction.
- **CODEOWNERS may intentionally differ from expertise:** A team may route reviews to junior members for training, or to a specific tech lead for compliance, regardless of who writes the most code in that path.

## System & Data Constraints

- **GitHub team analysis requires API access:** Attempting to infer team topology natively without the optional GitHub API relies entirely on co-commit clusters, which can conflate functional groups with organizational teams.
- **Review history availability affects evidence:** Pure-git inference (without the GitHub API) has no visibility into review activity. In this mode, the "review" component of the ownership score is effectively zero.
- **Historical trend scores use different data:** The historical confidence series might be computed from a weaker, different model than the present-day number, meaning trend charts must be interpreted with caution.
- **Identity resolution can be ambiguous:** While we attempt to merge disparate emails into unified GitHub handles, users who omit `.mailmap` entries or use multiple unrecognized emails may be erroneously split into distinct individuals, inflating the reviewer depth count.

## The Evidence Boundary (Can vs. Cannot Prove)

To maintain precise terminology, we strictly separate what repository evidence supports from what it cannot prove.

### What Repository Evidence CAN Support
- Contribution frequency
- Recency of changes
- Current-line attribution (`git blame`)
- Review participation (with API)
- Concentrated activity
- Stale rules
- Changing patterns
- Possible continuity risk
- Possible reviewer affinity

### What Repository Evidence CANNOT Prove
- Formal team ownership
- Architectural decision authority
- Organizational accountability
- Operational ownership
- Incident responsibility
- Compliance responsibility
- Security responsibility
- Current employment
- Reviewer availability
- Business ownership
- Managerial accountability
