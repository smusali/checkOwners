# CODEOWNERS compatibility corpus

Machine-readable cases for GitHub CODEOWNERS matching. Each file is JSONL: one JSON object per line, UTF-8.

Other tools can load the files without installing CheckOwners. The source distribution includes this directory. The wheel does not.

## `compatibility.jsonl`

A match row checks one pattern against one repo-relative path:

```json
{"kind":"match","category":"**","pattern":"/foo/**","path":"foo/bar/a.py","expected":true}
```

A rule row checks a whole file. `expected_owners` is the winning rule's owners. `null` means no rule wins. `[]` means an owner-less rule wins:

```json
{"kind":"rule","category":"line ordering and last-match-wins","content":"* @a\ndocs/ @b\n","path":"docs/x.md","expected_owners":["@b"]}
```

Categories:

- root anchoring
- nested directories
- `**`
- `*`
- `?`
- spaces
- escaped spaces
- comments
- line ordering and last-match-wins
- unsupported negation
- bracket syntax
- unusual Unicode names
- dotfiles
- paths that resemble comments
- multiple owners
- owner-less rules

There are at least 2,000 match rows.

Load every line:

```python
import json
from pathlib import Path

rows = [
    json.loads(line)
    for line in Path("corpus/compatibility.jsonl").read_text(encoding="utf-8").splitlines()
    if line
]
```

### How the rows differ from one reference

Match rows are labeled by a port of the matcher in [hmarr/codeowners](https://github.com/hmarr/codeowners) (`testdata/patterns.json` and `match.go`, MIT, Copyright (c) 2020 Harry Marr), plus the examples in GitHub's CODEOWNERS syntax documentation.

Two cases are fixed by GitHub's syntax rules instead of that port:

- A line that starts with `!`, or a pattern that contains `[` or `]`, does not assign an owner. `pattern_matches` still treats brackets as literal characters, so a match row for `apps/[param]/file.ts` can expect `true`.
- `\#foo` does not match a path `#foo`. The backslash does not make a leading `#` into a pattern.

The differential fuzz compares patterns that stay in the supported glob language: no leading `!`, no brackets, no `\#`, and at most one `**` segment. A pattern made only of repeated `**` segments, such as `**/**`, is outside that comparison.

## `realworld.jsonl`

Parser fixtures harvested from public repositories. Each row:

| Field | Meaning |
| --- | --- |
| `repo` | `owner/name` |
| `sha` | commit of the default branch at harvest time |
| `spdx` | `MIT`, `Apache-2.0`, `BSD-2-Clause`, `BSD-3-Clause`, `ISC`, `0BSD`, or `Unlicense` |
| `path` | path of the file in that repository |
| `text` | file text, with owner tokens replaced by `@owner` or `user@example.com` |
| `parse_sha256` | SHA-256 of the parsed rules |

`parse_sha256` is the hex digest of the UTF-8 JSON array of objects `{"pattern", "owners", "line_number"}`, with no extra whitespace. Pattern text, comments, and escapes are unchanged. The corpus has at least 500 files.

## Regenerating

```bash
python tools/build_codeowners_corpus.py compatibility
python tools/build_codeowners_corpus.py realworld
```

`realworld` needs the GitHub CLI logged in. It searches for files named CODEOWNERS, keeps the SPDX allowlist above, and rewrites the file.
