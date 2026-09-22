#!/usr/bin/env python3
"""Build the CODEOWNERS compatibility corpus and harvest parser fixtures."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.request
from itertools import product
from pathlib import Path
from typing import TypedDict

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from checkowners.patterns import parse_rules  # noqa: E402
from tests.codeowners_reference import reference_matches  # noqa: E402

_COMPATIBILITY = _ROOT / "corpus" / "compatibility.jsonl"
_REALWORLD = _ROOT / "corpus" / "realworld.jsonl"
_HMARR_PATTERNS = "https://raw.githubusercontent.com/hmarr/codeowners/main/testdata/patterns.json"
_MATCH_TARGET = 2500
_REALWORLD_TARGET = 500
_SPDX_ALLOW = frozenset(
    {
        "MIT",
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "ISC",
        "0BSD",
        "Unlicense",
    }
)
_SEARCH_QUERIES = (
    "filename:CODEOWNERS language:CODEOWNERS size:>=200",
    "filename:CODEOWNERS language:CODEOWNERS size:<200",
)
_OWNER = re.compile(r"(?<![\w.+-])@\w[\w./-]*")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_OBFUSCATED_EMAIL = re.compile(r"[\w.+-]+\[@\][\w.-]+(?:\.[\w.-]+)+")


class MatchRow(TypedDict):
    kind: str
    category: str
    pattern: str
    path: str
    expected: bool


class RuleRow(TypedDict):
    kind: str
    category: str
    content: str
    path: str
    expected_owners: list[str] | None


class RealWorldRow(TypedDict):
    repo: str
    sha: str
    spdx: str
    path: str
    text: str
    parse_sha256: str


def _category(pattern: str, path: str) -> str:
    if "[" in pattern or "]" in pattern:
        return "bracket syntax"
    if "?" in pattern:
        return "?"
    if "**" in pattern:
        return "**"
    if "*" in pattern:
        return "*"
    if " " in pattern or " " in path:
        return "spaces"
    if any(ord(char) > 127 for char in pattern + path):
        return "unusual Unicode names"
    if pattern.startswith(".") or path.startswith(".") or "/." in pattern or "/." in path:
        return "dotfiles"
    if pattern.startswith("/"):
        return "root anchoring"
    if "/" in path or "/" in pattern:
        return "nested directories"
    if "#" in pattern or "#" in path:
        return "paths that resemble comments"
    return "nested directories"


def _match(
    pattern: str,
    path: str,
    *,
    category: str | None = None,
    expected: bool | None = None,
) -> MatchRow:
    return {
        "kind": "match",
        "category": category or _category(pattern, path),
        "pattern": pattern,
        "path": path,
        "expected": reference_matches(pattern, path) if expected is None else expected,
    }


def _github_rows() -> list[MatchRow]:
    samples = (
        ("*", "README.md", True),
        ("*", "deep/nested/file.py", True),
        ("*.js", "app.js", True),
        ("*.js", "src/deep/nested/app.js", True),
        ("*.js", "app.ts", False),
        ("/build/logs/", "build/logs/run.log", True),
        ("/build/logs/", "build/logs/deep/run.log", True),
        ("/build/logs/", "src/build/logs/run.log", False),
        ("docs/*", "docs/getting-started.md", True),
        ("docs/*", "docs/build-app/troubleshooting.md", False),
        ("apps/", "apps/main.py", True),
        ("apps/", "src/apps/main.py", True),
        ("/docs/", "docs/index.md", True),
        ("/docs/", "src/docs/index.md", False),
        ("/scripts/", "scripts/run.py", True),
        ("**/logs", "build/logs/run.log", True),
        ("**/logs", "logs/run.log", True),
        ("/apps/", "apps/github/main.py", True),
    )
    rows: list[MatchRow] = []
    for pattern, path, documented in samples:
        if reference_matches(pattern, path) is not documented:
            message = f"reference disagrees with documented example {pattern!r} {path!r}"
            raise SystemExit(message)
        rows.append(_match(pattern, path))
    return rows


def _hmarr_rows() -> list[MatchRow]:
    with urllib.request.urlopen(_HMARR_PATTERNS, timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload, list):
        raise SystemExit("hmarr patterns.json is not a list")
    rows: list[MatchRow] = []
    for case in payload:
        if not isinstance(case, dict):
            continue
        pattern = case.get("pattern")
        paths = case.get("paths")
        if not isinstance(pattern, str) or not isinstance(paths, dict):
            continue
        for path, expected in paths.items():
            if not isinstance(path, str) or not isinstance(expected, bool):
                continue
            if reference_matches(pattern, path) is not expected:
                message = f"reference disagrees with hmarr fixture {pattern!r} {path!r}"
                raise SystemExit(message)
            rows.append(_match(pattern, path, expected=expected))
    return rows


def _generated_rows() -> list[MatchRow]:
    literals = [
        "foo",
        "bar",
        "baz",
        "qux",
        "src",
        "docs",
        "app",
        "README.md",
        "file.txt",
        "a",
        "b",
    ]
    specials = ["*", "**", "?", "f*", "*o", "f?o", "*.txt", "file?.txt", r"\*", r"\?"]
    dots = [".hidden", ".github", ".gitignore"]
    spaces = ["my file", "getting started"]
    unicode_names = ["café", "文档"]
    patterns: list[str] = []
    for base in [*literals, *specials, *dots, spaces[0], unicode_names[0]]:
        patterns.extend([base, f"/{base}", f"{base}/", f"/{base}/"])
    for left, right in product(
        ["foo", "src", "docs", "*", "**", "app", ".github"],
        ["bar", "*", "**", "*.txt", "file?.txt", "baz", ".hidden", "my file", "café"],
    ):
        patterns.extend([f"{left}/{right}", f"/{left}/{right}", f"{left}/{right}/"])
    for mid in ["*", "**", "bar", "qux"]:
        patterns.extend(
            [f"foo/{mid}/baz", f"/foo/{mid}/baz", f"src/**/{mid}", f"docs/{mid}/file.txt"]
        )
    paths = [
        *literals,
        *dots,
        *spaces,
        *unicode_names,
        "notes/#draft.md",
        "apps/[param]/file.ts",
    ]
    for left, right in product(
        ["foo", "src", "docs", "app", "bar", ".github", "my file", "café"],
        ["bar", "baz", "file.txt", "README.md", ".hidden", "a", "b", "文档", "getting started"],
    ):
        paths.append(f"{left}/{right}")
    for left, mid, right in product(
        ["foo", "src", "a", "docs"],
        ["bar", "qux", "b", ".hidden"],
        ["c.txt", "qux", "file.txt", "文档"],
    ):
        paths.append(f"{left}/{mid}/{right}")
    rows: list[MatchRow] = []
    unique_patterns = list(dict.fromkeys(patterns))
    unique_paths = list(dict.fromkeys(paths))
    for path in unique_paths:
        for pattern in unique_patterns:
            rows.append(_match(pattern, path))
            if len(rows) >= _MATCH_TARGET:
                return rows
    return rows


def _manual_match_rows() -> list[MatchRow]:
    return [
        _match("/", "README.md", category="root anchoring", expected=False),
        _match(r"\#foo", "#foo", category="paths that resemble comments", expected=False),
        _match(r"\#foo", r"\#foo", category="paths that resemble comments", expected=True),
        _match("notes/#draft.md", "notes/#draft.md", category="paths that resemble comments"),
        _match("*", "#not-a-pattern", category="paths that resemble comments"),
        _match("/apps/[param]/file.ts", "apps/[param]/file.ts", category="bracket syntax"),
        _match("/apps/[param]/file.ts", "apps/param/file.ts", category="bracket syntax"),
        _match(".hidden", "src/.hidden", category="dotfiles"),
        _match("/.github/", ".github/CODEOWNERS", category="dotfiles"),
        _match("café", "docs/café", category="unusual Unicode names"),
        _match("文档/", "src/文档/readme.md", category="unusual Unicode names"),
        _match("my file", "docs/my file", category="spaces"),
        _match("/docs/my file.txt", "docs/my file.txt", category="spaces"),
    ]


def _rule_rows() -> list[RuleRow]:
    return [
        {
            "kind": "rule",
            "category": "comments",
            "content": "# comment\n*.js @js-owner\n",
            "path": "app.js",
            "expected_owners": ["@js-owner"],
        },
        {
            "kind": "rule",
            "category": "comments",
            "content": "*.py @dev # alice(0.92) bob(0.71)\n",
            "path": "main.py",
            "expected_owners": ["@dev"],
        },
        {
            "kind": "rule",
            "category": "comments",
            "content": "# only a comment\n\n",
            "path": "README.md",
            "expected_owners": None,
        },
        {
            "kind": "rule",
            "category": "escaped spaces",
            "content": "docs/getting\\ started.md @docs-team\n",
            "path": "docs/getting started.md",
            "expected_owners": ["@docs-team"],
        },
        {
            "kind": "rule",
            "category": "escaped spaces",
            "content": "my\\ file.txt @alice\n",
            "path": "pkg/my file.txt",
            "expected_owners": ["@alice"],
        },
        {
            "kind": "rule",
            "category": "line ordering and last-match-wins",
            "content": "* @global\ndocs/ @docs-team\n",
            "path": "docs/index.md",
            "expected_owners": ["@docs-team"],
        },
        {
            "kind": "rule",
            "category": "line ordering and last-match-wins",
            "content": "* @global\n/src/ @alice\n",
            "path": "src/a.py",
            "expected_owners": ["@alice"],
        },
        {
            "kind": "rule",
            "category": "multiple owners",
            "content": "*.js @alice @bob\n",
            "path": "app.js",
            "expected_owners": ["@alice", "@bob"],
        },
        {
            "kind": "rule",
            "category": "multiple owners",
            "content": "/scripts/ @doctocat @octocat\n",
            "path": "scripts/run.py",
            "expected_owners": ["@doctocat", "@octocat"],
        },
        {
            "kind": "rule",
            "category": "owner-less rules",
            "content": "*.md @docs-team\ninternal/README.md\n",
            "path": "internal/README.md",
            "expected_owners": [],
        },
        {
            "kind": "rule",
            "category": "owner-less rules",
            "content": "/apps/ @octocat\n/apps/github\n",
            "path": "apps/github/main.py",
            "expected_owners": [],
        },
        {
            "kind": "rule",
            "category": "unsupported negation",
            "content": "* @alice\n!secret @bob\n",
            "path": "!secret",
            "expected_owners": ["@alice"],
        },
        {
            "kind": "rule",
            "category": "unsupported negation",
            "content": "!foo @bob\n",
            "path": "foo",
            "expected_owners": None,
        },
        {
            "kind": "rule",
            "category": "bracket syntax",
            "content": "* @alice\nroutes/[id]/page.tsx @web\n",
            "path": "routes/[id]/page.tsx",
            "expected_owners": ["@alice"],
        },
        {
            "kind": "rule",
            "category": "bracket syntax",
            "content": "apps/[param]/file.ts @web\n",
            "path": "apps/[param]/file.ts",
            "expected_owners": None,
        },
        {
            "kind": "rule",
            "category": "paths that resemble comments",
            "content": "#not-a-pattern @ghost\n* @owner\n",
            "path": "#not-a-pattern",
            "expected_owners": ["@owner"],
        },
        {
            "kind": "rule",
            "category": "paths that resemble comments",
            "content": "notes/#draft.md @docs\n",
            "path": "notes/#draft.md",
            "expected_owners": ["@docs"],
        },
    ]


def _write_compatibility() -> None:
    seen: set[tuple[str, str]] = set()
    match_rows: list[MatchRow] = []
    for row in [*_manual_match_rows(), *_github_rows(), *_hmarr_rows(), *_generated_rows()]:
        key = (row["pattern"], row["path"])
        if key in seen:
            continue
        seen.add(key)
        match_rows.append(row)
        if len(match_rows) >= _MATCH_TARGET:
            break
    if len(match_rows) < 2000:
        raise SystemExit(f"compatibility corpus has {len(match_rows)} match rows")
    _COMPATIBILITY.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in match_rows]
    lines.extend(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in _rule_rows())
    _COMPATIBILITY.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {len(match_rows)} match rows and {len(_rule_rows())} rule rows")


def _redact(text: str) -> str:
    redacted = _OBFUSCATED_EMAIL.sub("user@example.com", text)
    redacted = _EMAIL.sub("user@example.com", redacted)
    return _OWNER.sub("@owner", redacted)


def _parse_sha256(text: str) -> str:
    payload = [
        {"pattern": rule.pattern, "owners": list(rule.owners), "line_number": rule.line_number}
        for rule in parse_rules(text)
    ]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _gh_json(args: list[str], *, stdin: str | None = None) -> object:
    last_error = ""
    for _attempt in range(6):
        completed = subprocess.run(  # noqa: S603
            ["gh", *args],  # noqa: S607
            check=False,
            capture_output=True,
            text=True,
            input=stdin,
        )
        if completed.returncode == 0:
            return json.loads(completed.stdout)
        last_error = completed.stderr.strip()
        time.sleep(8)
    raise SystemExit(last_error)


def _search_code(query: str, page: int) -> list[dict[str, object]]:
    payload = _gh_json(
        [
            "api",
            "-X",
            "GET",
            "search/code",
            "-f",
            f"q={query}",
            "-f",
            "per_page=100",
            "-f",
            f"page={page}",
        ]
    )
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    rows: list[dict[str, object]] = []
    for item in items:
        if isinstance(item, dict):
            rows.append({str(key): value for key, value in item.items()})
    return rows


def _nested(value: object, *keys: str) -> object:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _file_text(full_name: str, path: str, sha: str) -> str | None:
    try:
        payload = _gh_json(["api", f"repos/{full_name}/contents/{path}?ref={sha}"])
    except SystemExit:
        return None
    if not isinstance(payload, dict):
        return None
    encoded = payload.get("content")
    if not isinstance(encoded, str):
        return None
    try:
        return base64.b64decode(encoded).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return None


def _string_field(row: dict[str, object], key: str) -> str | None:
    value = row.get(key)
    if isinstance(value, str):
        return value
    return None


def _new_repos(
    items: list[dict[str, object]],
    seen: set[str],
) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for item in items:
        repository = item.get("repository")
        if not isinstance(repository, dict):
            continue
        full_name = repository.get("full_name")
        path = _string_field(item, "path")
        if not isinstance(full_name, str) or path is None or full_name in seen:
            continue
        if Path(path).name.lower() != "codeowners":
            continue
        seen.add(full_name)
        found.append((full_name, path))
    return found


def _graphql_batch(pairs: list[tuple[str, str]]) -> dict[str, object]:
    fields: list[str] = []
    for index, (full_name, path) in enumerate(pairs):
        owner, name = full_name.split("/", 1)
        expression = f"HEAD:{path}"
        fields.append(
            f"r{index}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) "
            "{ licenseInfo { spdxId } defaultBranchRef { target { ... on Commit { oid } } } "
            f"file: object(expression: {json.dumps(expression)}) {{ ... on Blob {{ text }} }} }}"
        )
    payload = _gh_json(
        ["api", "graphql", "--input", "-"],
        stdin=json.dumps({"query": "query {\n" + "\n".join(fields) + "\n}"}),
    )
    data = _nested(payload, "data")
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in data.items()}


def _row_from_repo(
    full_name: str,
    path: str,
    node: object,
    seen_text: set[str],
    reasons: dict[str, int],
) -> RealWorldRow | None:
    spdx = _nested(node, "licenseInfo", "spdxId")
    sha = _nested(node, "defaultBranchRef", "target", "oid")
    text = _nested(node, "file", "text")
    if not isinstance(spdx, str) or spdx not in _SPDX_ALLOW or not isinstance(sha, str):
        label = spdx if isinstance(spdx, str) else "no license"
        reasons[label] = reasons.get(label, 0) + 1
        return None
    if not isinstance(text, str):
        text = _file_text(full_name, path, sha)
    if text is None or not text.strip() or len(text) > 65_536:
        reasons["unreadable"] = reasons.get("unreadable", 0) + 1
        return None
    redacted = _redact(text)
    digest = hashlib.sha256(redacted.encode()).hexdigest()
    if digest in seen_text:
        reasons["duplicate"] = reasons.get("duplicate", 0) + 1
        return None
    seen_text.add(digest)
    return {
        "repo": full_name,
        "sha": sha,
        "spdx": spdx,
        "path": path,
        "text": redacted,
        "parse_sha256": _parse_sha256(redacted),
    }


def harvest_realworld(limit: int) -> None:
    """Write ``limit`` permissively licensed CODEOWNERS files to the corpus."""
    chosen: list[RealWorldRow] = []
    seen_repos: set[str] = set()
    seen_text: set[str] = set()
    reasons: dict[str, int] = {}
    batch_size = 15
    for query in _SEARCH_QUERIES:
        if len(chosen) >= limit:
            break
        for page in range(1, 11):
            if len(chosen) >= limit:
                break
            items = _search_code(query, page)
            if not items:
                break
            pairs = _new_repos(items, seen_repos)
            print(f"{query} page {page}: {len(pairs)} new, {len(chosen)} kept", flush=True)
            for start in range(0, len(pairs), batch_size):
                if len(chosen) >= limit:
                    break
                batch = pairs[start : start + batch_size]
                nodes = _graphql_batch(batch)
                for index, (full_name, path) in enumerate(batch):
                    if len(chosen) >= limit:
                        break
                    row = _row_from_repo(
                        full_name,
                        path,
                        nodes.get(f"r{index}"),
                        seen_text,
                        reasons,
                    )
                    if row is None:
                        continue
                    chosen.append(row)
                    print(f"{len(chosen)} {row['spdx']} {full_name}", flush=True)
            time.sleep(7)
    print(f"skip reasons: {reasons}", flush=True)
    if len(chosen) < limit:
        raise SystemExit(f"harvested {len(chosen)} files, need {limit}")
    lines = [json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in chosen]
    _REALWORLD.parent.mkdir(parents=True, exist_ok=True)
    _REALWORLD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {len(chosen)} real-world files")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("compatibility", "realworld"))
    parser.add_argument("--limit", type=int, default=_REALWORLD_TARGET)
    args = parser.parse_args()
    if args.command == "compatibility":
        _write_compatibility()
        return
    harvest_realworld(args.limit)


if __name__ == "__main__":
    main()
