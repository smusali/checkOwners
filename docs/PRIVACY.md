# Privacy

checkOwners reads git history on the machine where it runs. It writes an ownership map and, when identity lookup is enabled, contributor email addresses.

## What is stored

| Location | Contents |
|----------|----------|
| `~/.checkowners/state/<id>.json` | Inferred owners, commits, and the analyzed commit |
| `~/.checkowners/graph/<id>.json` | Serialized ownership graph for that analysis |
| `~/.checkowners/handles.json` | Email to GitHub handle, including remembered misses |

`checkowners cache path` prints the directory. `CHECKOWNERS_STATE_DIR` overrides it. Files are local. They are not uploaded by the analysis itself.

## What leaves the machine

With `--offline`, nothing does. The command prints:

```text
Network access: disabled
Review evidence: unavailable
Team verification: unavailable
```

Without that flag, a `GITHUB_TOKEN` can be used to resolve emails, fetch review coverage, and read team membership. `notify` can POST a drift payload to `notifications.webhook_url`. Both are skipped when `--offline` is set. Noreply addresses, `.mailmap`, and an existing handle file still resolve locally.

## Deleting it

`checkowners cache clear` removes analysis state and graph files and leaves `handles.json` in place.

`checkowners cache purge` removes every file under the cache directory, including contributor emails in `handles.json`.

State and graph files are also dropped oldest-first once they exceed 256 MiB. The handle file is not part of that eviction.
