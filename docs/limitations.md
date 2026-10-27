# Limitations

CheckOwners infers a ranking from repository evidence. That ranking can be
wrong. Observed history is evidence, not authority.

## Where the tool can be wrong

- Git history is not complete human expertise.
- Squash merges hide authors who are absent from the commit and its `Co-authored-by` trailers.
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

Git attribution is not a complete engineering contribution history. Analyze
reports `merge_strategy` and `co_author_count` so that gap is visible.
`git.merge_strategy: auto` detects the dominant style in the lookback window.
An explicit `squash`, `merge`, or `rebase` is reported as given. Detection
does not change which commits are read. `git log` walks every parent. It does
not pass `-m` or `--first-parent`: `-m` would count a merge's files twice, and
`--first-parent` would hide the authors on the merged branch.

- **auto.** If at least 5% of commits in the window have two or more parents,
  the strategy is `merge`. Otherwise, if at least half of the remaining
  commits have a subject ending in `(#<number>)`, the strategy is `squash`.
  Otherwise, including an empty window, it is `rebase`.
- **squash.** One commit stands in for the change. Other authors are recovered
  only from `Co-authored-by` trailers when `git.count_co_authors` is true.
  Each credited person is scored on every file in that commit, with
  `git.co_author_weight` relative to the author's `1`. Blame still names only
  the commit author. Reviewers, and authors left off the trailer, are not
  recovered. Pull-request participant lists are not read.
- **merge.** Commits that were merged keep their own files and authors. The
  merge commit itself adds no file credit. A trailer on a merged commit still
  credits that co-author.
- **rebase.** Each replayed commit keeps its author. A trailer credits an
  extra person on that commit. It does not stand in for commits the rebase
  dropped.

`git.count_co_authors: false` scores only the commit author and reports
`co_author_count: 0`.

A `Co-authored-by` line counts only in the form `Name <email>`. The key is
case-insensitive. Any other trailer is ignored and does not fail the run. The
same email twice on one commit counts once. A trailer that is the author, or
that `.mailmap` maps to the author, is not a second person. Co-author emails
use the same `.mailmap` mapping, bot filter, and handle resolution as authors.
Blame does not split lines across co-authors.

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

`suggest` labels that gap as `load_signal: git_authorship_proxy`. The JSON
field `reviews` then holds commit totals. Those totals are not review load,
and the command does not recommend a person from them. When the API sample
exists, the label is `github_completed_reviews`. The ownership review factor
uses pulls closed inside `analysis.review_lookback_days`, which follows
`analysis.lookback_days` when omitted. Two repositories with the same
in-window reviewers and paths get the same review shares even when one ships
faster. `reviews_sampled` is how many closed pulls were included.
`reviews_window_days` is the span back to the oldest of those pulls. A young
repository can show a shorter span with no warning, because the pull list
ended. A recently updated pull that closed before the window is not part of
the signal, but walking those updates can spend `analysis.max_api_requests`
before the window is filled. That stop is a gap whose reason starts with
`Review window truncated:`, and it lowers `analysis_completeness`. It is not
a silent short window. Suggest still reads the 200 most recently updated
closed pull requests, and open pull requests, also capped at 200, are only
the current waiting queue. `median_response_time_hours`
is pull open to first submitted review, not the time from the review request.
`pr_complexity` is the median number of changed lines, not a calibrated
complexity score. `review_concentration` is per analyzed path, and only when
recorded reviews exist.

## Historical trend scores use different data

`trends` reconstructs each period from `git log` only. The series is
`historical_activity_confidence` (`model_version` `historical-v1`, signals
recency and frequency). `avg_qualified_owner_count` uses those same scores.
`ownership_score` is `ownership-v1` and can include blame and review. The two
numbers are not the same metric. The command prints that limitation. Naming
`ownership_score` in the same report requires `--compare-ownership` and
`--acknowledge-different-metrics`, and still does not plot `ownership_score`
values on the historical series.

Historical blame is not reconstructed. It stays unavailable until a published
benchmark shows the cost is acceptable.

`historical_confidence` on a continuity-risk warning is the present
`ownership_score` of an owner whose freshness status is not `stable`. It is
not `historical_activity_confidence` and it is not a score from an earlier
period. `active_expertise`, `historical_expertise`, and `maintenance_recency`
are evidence scores, not a measurement of what a person knows.
`historical_expertise` does not measure which lines survived.

## Identity resolution can be ambiguous

- Private emails and accounts that do not appear in the GitHub search API
  stay unresolved.
- User search matches a public profile email and can attach the wrong account.
  A search hit may relabel one email. It does not collapse two owners.
- Two people who share an address, or one person who never listed both
  addresses in `.mailmap` or `identities`, can be split or merged incorrectly.
  `identity.do_not_merge` and `identity unmerge` keep a named pair apart.
- Noreply addresses resolve to a login locally. A commit linked to an account
  resolves from `author.login`. Other addresses need a configured identity, a
  cache hit, search, or they remain raw emails.
- `former` is roster-only. A person who is not on the roster and is absent
  from the org is `external` when the member list was fetched, not `former`.
  Commit dates never assign `former`.
- A login ending in `[bot]`, an account type of `Bot`, a known dependency
  address, `bots.additional`, or `identities.*.service_account` is a bot.
  The substring `bot` in a human name is not. A long run of identical subjects
  caps frequency credit and does not delete the author.

## Completeness is reported, not implied

A run with a missing token, a shallow clone, an exhausted API or runtime
budget, unresolved identities, or a skipped team comparison still prints an
ownership table. The summary includes `analysis completeness` and one reason
per missing source. That percentage is not a finding, and a missing source is
not evidence that a person did not do the work. `--fail-on-incomplete` and
`policy.incomplete_analysis.fail` are how strict CI turns that report into
exit code 3.
