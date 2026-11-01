"""Tests for CODEOWNERS pattern matching."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TypedDict

import pytest
from hypothesis import given
from hypothesis import strategies as st

from checkowners.patterns import (
    CodeownersMarkerError,
    gitlab_section,
    marker_layout,
    match_path,
    parse_rules,
    pattern_matches,
    rules_with_source,
    strip_inline_comment,
)

_COMPATIBILITY = Path(__file__).parent / "fixtures" / "compatibility.jsonl"
_MIN_MATCHES = 2000
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
_CATEGORIES = frozenset(
    {
        "root anchoring",
        "nested directories",
        "**",
        "*",
        "?",
        "spaces",
        "escaped spaces",
        "comments",
        "line ordering and last-match-wins",
        "unsupported negation",
        "bracket syntax",
        "unusual Unicode names",
        "dotfiles",
        "paths that resemble comments",
        "multiple owners",
        "owner-less rules",
    }
)
_UNSUPPORTED = frozenset({"unsupported negation", "bracket syntax"})
_LITERAL = st.from_regex(r"[a-z0-9.]{1,8}", fullmatch=True)
_WILD = st.sampled_from(["*", "?", "f*", "*o", "*.txt", "file?.txt", r"\*", r"\?"])
_NAME = st.from_regex(r"[a-z]{1,8}", fullmatch=True)
_HANDLE = st.from_regex(r"[a-z]{1,12}", fullmatch=True)


class MatchCase(TypedDict):
    kind: str
    category: str
    pattern: str
    path: str
    expected: bool


class RuleCase(TypedDict):
    kind: str
    category: str
    content: str
    path: str
    expected_owners: list[str] | None


class RealWorldCase(TypedDict):
    repo: str
    sha: str
    spdx: str
    path: str
    text: str
    parse_sha256: str


class WindowsPathCase(TypedDict):
    pattern: str
    path: str
    match: bool
    exclude: bool


class WindowsRuleCase(TypedDict):
    content: str
    path: str
    owners: tuple[str, ...] | None


def _json_object(line: str) -> dict[str, object]:
    parsed: object = json.loads(line)
    if not isinstance(parsed, dict):
        raise ValueError("corpus row is not an object")
    row: dict[str, object] = {}
    for key, value in parsed.items():
        if not isinstance(key, str):
            raise ValueError("corpus key is not a string")
        row[key] = value
    return row


def _text(row: dict[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} is not a string")
    return value


def _owners(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("expected_owners is not a list")
    owners: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("owner is not a string")
        owners.append(item)
    return owners


def _match_case(row: dict[str, object]) -> MatchCase:
    expected = row.get("expected")
    if not isinstance(expected, bool):
        raise ValueError("expected is not a bool")
    return {
        "kind": "match",
        "category": _text(row, "category"),
        "pattern": _text(row, "pattern"),
        "path": _text(row, "path"),
        "expected": expected,
    }


def _rule_case(row: dict[str, object]) -> RuleCase:
    if "expected_owners" not in row:
        raise ValueError("rule row missing expected_owners")
    return {
        "kind": "rule",
        "category": _text(row, "category"),
        "content": _text(row, "content"),
        "path": _text(row, "path"),
        "expected_owners": _owners(row.get("expected_owners")),
    }


def _load_compatibility() -> tuple[list[MatchCase], list[RuleCase]]:
    matches: list[MatchCase] = []
    rules: list[RuleCase] = []
    for line in _COMPATIBILITY.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        row = _json_object(line)
        kind = row.get("kind")
        if kind == "match":
            matches.append(_match_case(row))
        elif kind == "rule":
            rules.append(_rule_case(row))
        else:
            raise ValueError("unknown corpus kind")
    return matches, rules


def _parse_sha256(text: str) -> str:
    payload = [
        {"pattern": rule.pattern, "owners": list(rule.owners), "line_number": rule.line_number}
        for rule in parse_rules(text)
    ]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


_MATCHES, _RULES = _load_compatibility()


_WINDOWS_PATHS: tuple[WindowsPathCase, ...] = (
    {
        "pattern": "src/*",
        "path": "src/file.py",
        "match": True,
        "exclude": True,
    },
    {
        "pattern": "src/*",
        "path": "src/a/b.py",
        "match": False,
        "exclude": True,
    },
    {
        "pattern": "src/*",
        "path": "src\\file.py",
        "match": False,
        "exclude": True,
    },
    {
        "pattern": "src/*",
        "path": "src/sub\\file.py",
        "match": True,
        "exclude": True,
    },
    {
        "pattern": "*.py",
        "path": "src\\file.py",
        "match": True,
        "exclude": True,
    },
    {
        "pattern": "*.py",
        "path": "C:/src/file.py",
        "match": True,
        "exclude": True,
    },
    {
        "pattern": "*.py",
        "path": "C:\\src\\file.py",
        "match": True,
        "exclude": True,
    },
    {
        "pattern": "src/*",
        "path": "C:\\src\\file.py",
        "match": False,
        "exclude": False,
    },
    {
        "pattern": "src/*",
        "path": "C:/src/file.py",
        "match": False,
        "exclude": False,
    },
    {
        "pattern": "*.py",
        "path": "\\\\server\\share\\file.py",
        "match": True,
        "exclude": True,
    },
    {
        "pattern": "src/*",
        "path": "\\\\server\\share\\file.py",
        "match": False,
        "exclude": False,
    },
    {
        "pattern": "dir/file.py",
        "path": "Dir/File.py",
        "match": False,
        "exclude": False,
    },
    {
        "pattern": "*.py",
        "path": "Dir/File.PY",
        "match": False,
        "exclude": False,
    },
)


_WINDOWS_RULES: tuple[WindowsRuleCase, ...] = (
    {
        "content": "* @alice\n",
        "path": "C:\\src\\a.py",
        "owners": ("@alice",),
    },
    {
        "content": "* @alice\n",
        "path": "\\\\server\\share\\a.py",
        "owners": ("@alice",),
    },
    {
        "content": "src/* @alice\n",
        "path": "src\\file.py",
        "owners": None,
    },
    {
        "content": "*.py @alice\n",
        "path": "Dir/File.PY",
        "owners": None,
    },
)


_WINDOWS_PATH_IDS = (
    "forward-slash-child",
    "forward-slash-nested",
    "backslash",
    "mixed",
    "backslash-star",
    "drive-forward",
    "drive-backslash",
    "drive-backslash-rooted",
    "drive-forward-rooted",
    "unc-star",
    "unc-rooted",
    "case-path",
    "case-extension",
)

_WINDOWS_RULE_IDS = (
    "star-drive",
    "star-unc",
    "rooted-backslash",
    "case-extension",
)


def test_pattern_matches_rejects_degenerate_forms_and_keeps_literals() -> None:
    assert pattern_matches("***", "file") is False
    assert pattern_matches("X", "X") is True
    assert pattern_matches("docsX/", "docsX/a") is True
    assert pattern_matches("a/**/c", "a/xc") is False
    assert pattern_matches("a/*/c", "a//c") is False
    assert pattern_matches(r"\**", "*a") is True


def test_bracket_patterns_and_slash_stripping_do_not_invent_owners() -> None:
    assert match_path(parse_rules("foo[ @alice\n"), "foo[") is None
    assert match_path(parse_rules("foo] @alice\n"), "foo]") is None
    rooted = match_path(parse_rules("* @alice\n"), "/foo")
    assert rooted is not None
    assert rooted.owners == ("@alice",)
    literal = match_path(parse_rules("Xfoo @alice\n"), "Xfoo")
    assert literal is not None
    assert literal.owners == ("@alice",)


def test_gitlab_section_ignores_a_blank_line() -> None:
    assert gitlab_section("") is None
    assert gitlab_section("   ") is None


def test_strip_inline_comment_cuts_at_the_first_spaced_hash() -> None:
    assert strip_inline_comment("  # comment") == ""
    assert strip_inline_comment("a # b # c") == "a"
    assert strip_inline_comment("keep") == "keep"


def test_marker_layout_reports_manual_text_and_marker_locations() -> None:
    unmarked = marker_layout("* @alice\n")
    assert unmarked.manual == ""
    assert unmarked.marked is False

    content = "preamble\n# checkowners:manual:start\nkeep @alice\n# checkowners:manual:end\n"
    layout = marker_layout(content)
    assert layout.marked is True
    assert layout.manual.startswith("# checkowners:manual:start")
    assert "preamble" not in layout.manual
    assert "keep @alice" in layout.manual

    with pytest.raises(CodeownersMarkerError, match=r"manual:end at line 1"):
        marker_layout("# checkowners:manual:end\n")
    with pytest.raises(CodeownersMarkerError, match=r"manual:start at line 3"):
        marker_layout(
            "# checkowners:manual:start\n# checkowners:manual:end\n# checkowners:manual:start\n"
        )
    with pytest.raises(CodeownersMarkerError, match=r"generated:start at line 2"):
        marker_layout("# checkowners:manual:start\n# checkowners:generated:start\n")
    with pytest.raises(CodeownersMarkerError, match=r"manual:start at line 2"):
        marker_layout("# checkowners:manual:start\n# checkowners:manual:start\n")
    with pytest.raises(CodeownersMarkerError, match=r"manual:start at line 1"):
        marker_layout("# checkowners:manual:start\n* @alice\n")


def test_rules_outside_a_marker_block_stay_unmarked() -> None:
    content = "# checkowners:manual:start\nkeep @alice\n# checkowners:manual:end\n* @bob\n"
    sources = {rule.pattern: rule.source for rule in rules_with_source(content)}
    assert sources == {"keep": "manual", "*": "unmarked"}


@given(mid=_NAME)
def test_globstar_crosses_segments_and_star_does_not(mid: str) -> None:
    assert pattern_matches("a/**/c", f"a/{mid}/c")
    assert pattern_matches("a/**/c", f"a/{mid}/x/c")
    assert pattern_matches("a/*/c", f"a/{mid}/c")
    assert pattern_matches("a/*/c", f"a/{mid}/x/c") is False


@given(name=_NAME)
def test_leading_slash_anchors_at_root(name: str) -> None:
    assert pattern_matches(f"/{name}", name)
    assert pattern_matches(f"/{name}", f"sub/{name}") is False
    assert pattern_matches(name, name)
    assert pattern_matches(name, f"sub/{name}")


@given(first=_HANDLE, second=_HANDLE)
def test_last_matching_rule_wins(first: str, second: str) -> None:
    rules = parse_rules(f"* @{first}\n*.py @{second}\n")
    matched = match_path(rules, "a.py")
    assert matched is not None
    assert matched.owners == (f"@{second}",)


@given(child=_NAME, nested=_NAME)
def test_dir_star_matches_direct_children_only(child: str, nested: str) -> None:
    assert pattern_matches("dir/*", f"dir/{child}")
    assert pattern_matches("dir/*", f"dir/{child}/{nested}") is False


@given(child=_NAME, nested=_NAME)
def test_trailing_slash_matches_directory_contents(child: str, nested: str) -> None:
    assert pattern_matches("dir/", f"dir/{child}")
    assert pattern_matches("dir/", f"dir/{child}/{nested}")
    assert pattern_matches("dir/", f"dir2/{child}") is False
    assert pattern_matches("dir/", "dir") is False
