# Limitations

CheckOwners infers a ranking from repository evidence. That ranking can be
wrong. Observed history is evidence, not authority.

## Where the tool can be wrong

- Git history is not complete human expertise.
- Squash merges can hide authorship.
- Blame can over-credit formatting changes.
- CODEOWNERS may intentionally differ from repository evidence.
- GitHub team analysis requires API access.
- Review history availability affects evidence.
- Historical trend scores use different data.
- Identity resolution can be ambiguous.

## What repository evidence can support

- Contribution frequency inside the lookback window.
- Recency of the last commit on a path.
- Current-line attribution, after the blame corrections that actually ran.
- Review participation, when a review provider returned data.
- Concentrated activity, as score mass on the truncated owner list.
- Stale CODEOWNERS rules, when declared owners and inferred owners diverge.
- Changing patterns across trend periods that share the same two signals.
- Possible continuity risk, classified as `inactive`, `superseded`, or `departed`. `stable` is not a warning. A path with no commit interval still uses `decay.threshold_days`.
- Possible reviewer affinity, from commits or from recorded reviews.

## What repository evidence cannot prove

- Formal team ownership.
- Architectural decision authority.
- Organizational accountability.
- Operational ownership.
- Incident responsibility.
- Compliance responsibility.
- Security responsibility.
- Current employment.
- Reviewer availability.
- Business ownership.
- Managerial accountability.

`qualified_owner_count` is a capped count, not a truck factor. `top_owner_share`,
`effective_owners`, `truck_factor_50`, `truck_factor_75`, and `truck_factor_90`
describe score mass after the confidence threshold and before `top_n_owners`
truncation. They are not a repository removal simulation. See
[Methodology](METHODOLOGY.md).

## Git history is incomplete evidence

- Squash merges collapse many authors into one commit, so recency and
  frequency can credit the merger more than the people who wrote the code.
- Unmapped emails stay distinct. `.mailmap` (on by default) collapses
  addresses the repository already listed; anything left unmapped is still
  two identities until handle resolution merges them.
- Rename lineage across history is not followed. `-M` and `-C` on blame
  preserve line attribution when content moves in one commit; they do not
  rebuild a file's identity across later renames. `explain` on a single file
  may list prior names from `git log --follow`; that is a name list, not a
  logical file identity used for scoring.

## Blame corrections have limits

- Ignore-revs applies only when `.git-blame-ignore-revs` (or the configured
  path, or `blame.ignoreRevsFile`) exists. Repositories that never listed a
  formatter commit still get mass-refactor omission when that commit
  modified at least `git.mass_refactor_file_fraction` of tracked files.
- Mass-refactor detection is a file-count heuristic on modifications. A
  large feature that edits half the tree can be omitted from blame the same
  way a formatter is. Set the fraction to `0` to disable.
- Whitespace-only edits are ignored (`-w`). Token-changing formatters still
  need ignore-revs or the mass-refactor threshold.
- Blame can still over-credit a formatting change that is not whitespace-only
  and is below the mass-refactor fraction.

## CODEOWNERS is policy, not a measurement

CODEOWNERS may intentionally differ from inferred expertise. A security team,
a compliance owner, or an on-call rotation can be the right declared owner
when git history points at someone else. Drift reports that difference. It
does not decide which side should win.

GitHub skips any CODEOWNERS line that uses `[ ]` as a character range. Paths such
as `routes/[id]/page.tsx` therefore cannot be represented exactly. Generate
rewrites bracket segments to `*` only when every inferred path the wildcard
matches shares the same owners. When owners differ, the default is per-file
rules rather than a wildcard that assigns review to people who did not earn
those paths. Literal fallback lines with `[...]` are also skipped by GitHub;
`--allow-broad-patterns` opts into the broader `*` rule.

## GitHub data is optional and partial

GitHub team analysis requires API access. Without a token that can list the
organization, team membership is unverifiable. Offline mode does not call the
network.

Review history availability affects evidence. With no review provider, the
review signal is unavailable: it is omitted from `ownership_score` and it
lowers `evidence_quality`. A missing review history is not a score of zero
and it is not evidence that nobody reviewed the path.

## Historical trend scores use different data

`trends` reconstructs each period from `git log` only. Blame and review are
not available historically, so `avg_top_confidence` and
`avg_qualified_owner_count` use recency and frequency. Those numbers are not
comparable to a present-day `ownership_score` that included blame or review.
`historical_confidence` on a continuity-risk warning is the present score of
an owner whose freshness status is not `stable`. It is not a score from an
earlier period. `active_expertise`, `historical_expertise`, and
`maintenance_recency` are evidence scores, not a measurement of what a person
knows. `historical_expertise` does not measure which lines survived.

## Identity resolution can be ambiguous

- Private emails and accounts that do not appear in the GitHub search API
  stay unresolved.
- Two people who share an address, or one person who never listed both
  addresses in `.mailmap`, can be split or merged incorrectly.
- Noreply addresses resolve to a login locally. Other addresses need the
  API, a cache hit, or they remain raw emails.

## Completeness is reported, not implied

A run with a missing token, a shallow clone, an exhausted API or runtime
budget, unresolved identities, or a skipped team comparison still prints an
ownership table. The summary includes `analysis completeness` and one reason
per missing source. That percentage is not a finding, and a missing source is
not evidence that a person did not do the work. `--fail-on-incomplete` and
`policy.incomplete_analysis.fail` are how strict CI turns that report into
exit code 3.
