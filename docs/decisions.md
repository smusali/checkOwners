# Decisions

## Confidence model

Status: accepted.

Context: a single number was being read as a probability that a person should own a path, and as a measure of how much evidence the run collected.

Decision: `ownership_score` is a ranking from 0.0 to 1.0 of the signals that were available. `evidence_quality` stays separate. The score is not a calibrated probability. A measured zero stays a measured zero. A signal that did not run is unavailable, not zero.

Consequences: methodology is the scoring contract. A formula change bumps `models.ownership` even when the JSON types stay the same. Reports show score and quality side by side.

## CODEOWNERS pattern engine

Status: accepted.

Context: directory rules and GitHub's last-match-wins behavior were being compared as literal strings, which marked real files missing.

Decision: pattern matching follows GitHub CODEOWNERS semantics in `checkowners.patterns`. The last matching pattern in file order assigns the owners. GitLab sections are a dialect on that engine, not a second matcher. Bitbucket and Gerrit names are recognized and are not rendered.

Consequences: `generate` and `validate` share the engine. A new forge adds a writer. It does not fork the matcher.

## Identity resolution

Status: accepted.

Context: one person shows up as several emails, and a remote search that merges those emails can collapse two people.

Decision: resolution order is a configured `identities` entry, `.mailmap` when `identity.mailmap` is on, a GitHub noreply address, this repository's handle cache, `author.login` on one local commit, and user search last. Search relabels that one email. It does not merge two owners. `identity.do_not_merge` and `identity unmerge` keep a pair apart. A miss expires after `identity.negative_cache_ttl_days`.

Consequences: `.mailmap` is the cheapest accuracy fix. Search stays last and local. `contributors.roster` sets `contributor_status`. Commit dates do not invent `former`.

## Knowledge concentration

Status: accepted.

Context: the capped count of owners above the confidence threshold was labeled bus factor. That name claims a removal simulation the count does not perform.

Decision: the count is `qualified_owner_count`, taken from the list already truncated to `analysis.top_n_owners`. The `bus_factor` config section classifies that capped count. Truck factor stays the departure measure: on one path, the smallest number of highest-share contributors whose scores reach a configured fraction, and in `simulate`, the number of authors whose departure drops file coverage below the first threshold.

Consequences: raising `top_n_owners` raises the maximum reportable count without any code changing hands. Concentration metrics use every score at or above the threshold, before that truncation. The displayed owner list stays truncated.

## Notifier

Status: accepted.

Context: a built-in webhook posts repository findings to a URL the tool chooses, and a network error in that post becomes a failure of the audit.

Decision: CheckOwners does not send webhooks. The Action writes bounded summaries to its outputs. The caller posts those summaries. A Slack step that reads `drift_summary` is the caller's workflow, not a client inside the tool.

Consequences: there is no notification setting in the config file. Delivery, routing, and retries belong to the workflow that already has the secret.

## Blame architecture

Status: accepted.

Context: one `git blame` process per file spends time on process startup. Three replacements were timed: `git blame --incremental`, one `git log --numstat` pass, and in-process blame through pygit2. Production attribution had to stay identical to `git blame --line-porcelain`.

Decision: production blame stays `git blame --line-porcelain`, one process per path, on a thread pool. On the recorded run, incremental blame was not faster (0.3498 seconds versus 0.3487 on the `ci` tier, and 8.8745 versus 8.722 on 2000 files) and numstat was faster (0.0223 and 0.0233 seconds) without matching porcelain attribution. pygit2 stays a benchmark probe. It is not the analyze path. `owners`, `explain`, and `change` blame only the path they were asked about. Unchanged blobs hit the blame cache.

Consequences: pure git remains the runtime dependency. A later replacement has to match porcelain line attribution or document a model change. The recorded choice is porcelain.

## Forge abstraction

Status: accepted.

Context: ownership evidence is not GitHub-specific, and folding every forge into the scorer would put host-specific conditionals in the scoring path.

Decision: `--dialect` accepts `github`, `gitlab`, `bitbucket`, and `gerrit`. GitHub and GitLab render. Bitbucket and Gerrit exit 2 until a writer exists. `RepositoryAnalyzer` is the library entry. `analyze()` returns scored ownership with the commit, blame, and review observations stored on each path. Scoring can run again from those stored observations. GitHub remains the best-supported host. New forges wait on demand.

Consequences: a model change can be evaluated against stored evidence without another git or API pass. Adding a forge is a writer and a client behind the dialect seam, not an edit to the score formula.
