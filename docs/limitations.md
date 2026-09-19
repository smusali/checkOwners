# Limitations

CheckOwners infers ownership from git history. That is a ranking signal, not a
claim that the listed people are the only experts or the assigned owners.

## Git history is incomplete evidence

- Squash merges collapse many authors into one commit, so recency and
  frequency can credit the merger more than the people who wrote the code.
- Unmapped emails stay distinct. `.mailmap` (on by default) collapses
  addresses the repository already listed; anything left unmapped is still
  two identities until handle resolution merges them.
- Rename lineage across history is not followed. `-M` and `-C` on blame
  preserve line attribution when content moves in one commit; they do not
  rebuild a file's identity across later renames.

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

## Other known gaps

- CODEOWNERS may intentionally differ from inferred expertise.
- Review history and team membership need GitHub API access.
- Historical trend scores cannot reconstruct blame or review.
- Identity resolution can miss private emails and accounts that do not
  appear in the GitHub search API.
