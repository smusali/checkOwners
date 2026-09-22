# Privacy

checkOwners reads git history on the machine where it runs. It writes an ownership map. Email addresses are stored as tokens. When identity lookup is enabled, those addresses may be sent to the GitHub user-search API unless `contributors.exclude` lists them.

`output.anonymize`, `output.aggregate_only`, `privacy.redact_emails` (`--redact-emails`), `identity.mode`, and `contributors.exclude` control what reports, graph exports, Action output, and pull-request comments may show. See [Privacy controls](USAGE.md#privacy-controls).

## What is stored

| Location | Contents |
|----------|----------|
| `~/.checkowners/state/<id>.json` | Inferred owners, commits, and the analyzed commit. Email addresses are stored as `email:` tokens. |
| `~/.checkowners/graph/<id>.json` | Serialized ownership graph for that analysis. Contributor emails in node ids are tokens. |
| `~/.checkowners/handles.json` | Email token to GitHub handle, including remembered misses. A file that still has plaintext addresses is rewritten on the next read. |

`checkowners cache path` prints the directory. `CHECKOWNERS_STATE_DIR` overrides it. Files are local. They are not uploaded by the analysis itself.

## What leaves the machine

With `--offline`, nothing does. The command prints:

```text
Network access: disabled
Review evidence: unavailable
Team verification: unavailable
```

Without that flag, a `GITHUB_TOKEN` can be used to resolve emails, fetch review coverage, and read team membership. That access is skipped when `--offline` is set. Noreply addresses, `.mailmap`, and an existing handle file still resolve locally.

## Deleting it

`checkowners cache clear` removes analysis state and graph files and leaves `handles.json` in place.

`checkowners cache purge` removes every file under the cache directory, including contributor emails in `handles.json`.

State and graph files are also dropped oldest-first once they exceed 256 MiB. The handle file is not part of that eviction.
