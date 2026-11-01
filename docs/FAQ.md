# FAQ

## Which Action tag should I pin?

`@v0` tracks the latest 0.x. `@v0.7` tracks 0.7.x. `@v0.6` stays on 0.6.x. `@v0.7.0` is the pin.

## Does checkOwners need a GitHub token?

No. The inference is pure git and runs offline. A token only adds three
features: resolving non-noreply emails to `@handles`, resolving teams and
subteams, and the review signal with team reconciliation. Without one you still
get scored ownership, drift, qualified owner counts, freshness, and onboarding,
and the remaining signal weights are renormalized so the score stays on
`[0, 1]`.

Noreply addresses such as `login@users.noreply.github.com` resolve to `@login`
with no token at all.

## Where does the token go?

In the `GITHUB_TOKEN` environment variable, and nowhere else.
`github.token` in `checkowners.yml` is refused, because that file is committed
and a secret there would be published. In Actions the composite Action exports
the job token for you; pass `github_token` only when you need a PAT or App
token.

## What permissions does each feature need?

| Capability | Job token | Fine-grained PAT |
| --- | --- | --- |
| Email to `@handle` | the default token is enough | no extra repository permission |
| Teams and subteams | not available; the job token cannot list org teams | Organization members: Read |
| Review coverage, `suggest` | `pull-requests: read` | Pull requests: Read |
| Pull-request comment | `pull-requests: write` | Pull requests: Write |
| Code scanning upload | `security-events: write` | Code scanning alerts: Read and write |
| Check run | `checks: write` | Checks: Read and write |
| `sync` pull request | `contents: write` and `pull-requests: write` | Contents and Pull requests: Read and write |

A fork pull request cannot comment or upload code scanning results: GitHub makes
that token read-only whatever the `permissions:` block says. The Action skips
both, writes the report and the SARIF artifact, and does not fail the job.

## Will the generated CODEOWNERS use handles or emails?

Handles whenever one was resolved. A path whose owners are still raw addresses
is skipped rather than written with an address, because GitHub would not
understand it. Writing a `.mailmap` is the cheapest way to improve this.

## Does it handle teams and subteams?

With a token, `github.org`, and `github.resolve_teams`, yes. checkOwners loads
the organization team list once, follows child teams, and reports a team rule as
aligned, partially drifted, unverifiable, invisible, empty, or nested.
`team_coverage` is the share of inferred handles who are members.

## Can a report leave out people's names?

Yes. `output.anonymize` replaces each person with a stable `person-` token,
`output.aggregate_only` drops per-person fields and keeps path and repository
metrics, and `privacy.redact_emails` replaces addresses with tokens.
`generate` and `sync` exit 2 under those settings, because a CODEOWNERS file
has to name accounts.

## Can CODEOWNERS live in the repository root?

Yes. checkOwners reads `.github/CODEOWNERS`, `CODEOWNERS`, and
`docs/CODEOWNERS`, in that order, and writes back to whichever it found.

## Where is the cache?

`~/.checkowners`, or `%USERPROFILE%\.checkowners` on Windows. Set
`CHECKOWNERS_STATE_DIR` to move it, `checkowners cache path` to print it, and
`checkowners cache purge` to delete everything in it.

## How do I turn checkOwners on without failing on everything?

Run `checkowners baseline create` and commit the file. Later runs then fail only
on findings that are not in it. `--exit-zero` is the other way: it turns the
findings exit code into 0 after printing the report.

## What is the difference between a baseline and a suppression?

A baseline is a snapshot of what was already there, written by a command, and it
ratchets: a finding you fix cannot come back silently. A suppression is a
hand-written rule in the config that always applies, and it requires a reason so
the next reader knows why.

## How do I fail only on critical drift?

Leave `policy.drift.fail` false and branch on the Action's `drift_summary`,
which carries the severity. A workflow step that fails when
`severity == "critical"` is three lines of `if:`.

## Why is analysis completeness below 100% on a healthy repository?

Completeness counts the evidence catalog, not the health of the repository.
Without a token the review signal is missing; without a `.mailmap` or a
`.git-blame-ignore-revs` file those corrections did not run. Each of those is a
gap, so a normal run sits below 1 and still exits 0.

## Why did a formatter commit not take ownership?

Commits that touch at least `git.mass_refactor_file_fraction` of tracked files
are left out of blame, and revisions in `.git-blame-ignore-revs` are ignored.
Both exist so a sweeping reformat does not become ownership.

## Why are some paths missing from `analyze`?

They were excluded: `linguist-generated` or `linguist-vendored` in
`.gitattributes`, a `paths.exclude` pattern, a deleted file, or a path whose only
authors are bots.

## Can I keep a hand-written rule when I generate CODEOWNERS?

Yes. Wrap it in `# checkowners:manual:start` and `# checkowners:manual:end`.
`generate` rewrites only the generated block. `output.block_order` decides which
block wins an overlap, and the later block is the winner.

## `generate` stopped on a marker error and left the file alone.

The manual and generated markers are unbalanced or interleaved. The message
names the line. Nothing is written until they pair up, so no manual rule is ever
lost to a half-written file.

## `generate` refused because the file is too large.

`output.max_bytes` defaults to 2.5 MB because GitHub ignores a CODEOWNERS file
over 3 MB. Raise the limit, or let `output.consolidate` collapse uniform
directories, or pass `--force`.

## `networkx` is not installed but I want `graph`.

`pip install "checkowners[graph]"`. The graph, topology, and onboarding commands
need it; nothing else does.

## Is this a performance-measurement tool?

No. It measures where knowledge is concentrated, which is a property of a
repository, not of a person. Using it to evaluate individuals is unsupported and
harmful.

## Does it use an LLM?

No. Inference is deterministic git analysis, and the scoring code is in the
repository to read.
