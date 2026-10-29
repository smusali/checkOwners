"""CODEOWNERS pattern matching with gitignore-style semantics.

GitHub CODEOWNERS patterns follow most gitignore rules: `*` stays within a
path segment, `**` crosses segments, `?` matches a single character, a
leading `/` (or any interior `/`) anchors the pattern to the repo root, and a
trailing `/` restricts the match to a directory's contents. Negation (`!`)
and character ranges (`[...]`) are not supported by GitHub and are rejected
by the validator rather than silently mismatched here.

The last matching rule wins, mirroring GitHub's evaluation order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Literal

RuleSource = Literal["manual", "generated", "unmarked"]
_MarkerKind = Literal["manual", "generated"]
_MarkerEdge = Literal["start", "end"]

MANUAL_START = "# checkowners:manual:start"
MANUAL_END = "# checkowners:manual:end"
GENERATED_START = "# checkowners:generated:start"
GENERATED_END = "# checkowners:generated:end"

_MARKERS: dict[str, tuple[_MarkerKind, _MarkerEdge]] = {
    MANUAL_START: ("manual", "start"),
    MANUAL_END: ("manual", "end"),
    GENERATED_START: ("generated", "start"),
    GENERATED_END: ("generated", "end"),
}
_PARTNER = {
    MANUAL_START: MANUAL_END,
    MANUAL_END: MANUAL_START,
    GENERATED_START: GENERATED_END,
    GENERATED_END: GENERATED_START,
}


class CodeownersMarkerError(Exception):
    pass


@dataclass(frozen=True)
class MarkerLayout:
    manual: str
    sources: dict[int, RuleSource]
    marked: bool


@dataclass(frozen=True)
class CodeownersRule:
    """One parsed CODEOWNERS line: a pattern plus its owner handles."""

    pattern: str
    owners: tuple[str, ...]
    line_number: int
    source: RuleSource = "unmarked"
    section: str = ""
    approvals: int = 1
    optional: bool = False


_SECTION_HEADER = re.compile(r"(\^)?\[([^\]]+)\](?:\[(\d+)\])?")


def gitlab_section(line: str) -> tuple[str, int, bool] | None:
    """Return `(name, approvals, optional)` when `line` is a GitLab section header."""
    parts = split_escaped(line)
    if not parts:
        return None
    match = _SECTION_HEADER.fullmatch(parts[0])
    if match is None or "/" in match.group(2):
        return None
    count = int(match.group(3)) if match.group(3) else 1
    return match.group(2), count if count >= 1 else 1, match.group(1) == "^"


def parse_rules(content: str, *, dialect: str = "github") -> tuple[CodeownersRule, ...]:
    """Parse CODEOWNERS text into ordered rules (comments and blanks skipped).

    A pattern with no owners is kept: GitHub uses owner-less rules to exempt
    paths from a broader earlier rule.
    """
    rules: list[CodeownersRule] = []
    section = ""
    approvals = 1
    optional = False
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        line = strip_inline_comment(raw_line).strip()
        if not line:
            continue
        if dialect == "gitlab":
            header = gitlab_section(line)
            if header is not None:
                section, approvals, optional = header
                continue
        parts = split_escaped(line)
        rules.append(
            CodeownersRule(
                pattern=parts[0],
                owners=tuple(parts[1:]),
                line_number=line_number,
                section=section,
                approvals=approvals,
                optional=optional,
            )
        )
    return tuple(rules)


def marker_layout(content: str) -> MarkerLayout:
    """Return the manual slice and per-line sources for `content`."""
    spans: list[tuple[int, int]] = []
    offset = 0
    for piece in content.splitlines(keepends=True):
        spans.append((offset, offset + len(piece)))
        offset += len(piece)

    open_kind: _MarkerKind | None = None
    open_line = 0
    open_offset = 0
    manual = ""
    manual_done = False
    generated_done = False
    sources: dict[int, RuleSource] = {}

    for line_number, (start, end) in enumerate(spans, start=1):
        token = content[start:end].strip()
        found = _MARKERS.get(token)
        if found is None:
            if open_kind is not None:
                sources[line_number] = open_kind
            continue
        kind, edge = found
        if open_kind is None:
            if edge == "end":
                raise _marker_error(line_number, token)
            if (kind == "manual" and manual_done) or (kind == "generated" and generated_done):
                raise _marker_error(line_number, token, f"repeats a {kind} block")
            open_kind = kind
            open_line = line_number
            open_offset = start
            continue
        if kind != open_kind:
            raise _marker_error(
                line_number,
                token,
                f"is inside the {open_kind} block that started at line {open_line}",
            )
        if edge == "start":
            raise _marker_error(line_number, token, f"repeats an open {kind} block")
        if kind == "manual":
            manual = content[open_offset:end]
            manual_done = True
        else:
            generated_done = True
        open_kind = None

    if open_kind is not None:
        token = MANUAL_START if open_kind == "manual" else GENERATED_START
        raise _marker_error(open_line, token)
    return MarkerLayout(
        manual=manual,
        sources=sources,
        marked=manual_done or generated_done,
    )


def rules_with_source(content: str, *, dialect: str = "github") -> tuple[CodeownersRule, ...]:
    """Return rules parsed from `content`, tagged by marker source."""
    rules = parse_rules(content, dialect=dialect)
    sources = marker_layout(content).sources
    if not sources:
        return rules
    return tuple(
        replace(rule, source=sources[rule.line_number]) if rule.line_number in sources else rule
        for rule in rules
    )


def _marker_error(line_number: int, marker: str, problem: str = "") -> CodeownersMarkerError:
    if problem:
        return CodeownersMarkerError(f"CODEOWNERS marker {marker} at line {line_number} {problem}.")
    return CodeownersMarkerError(
        f"CODEOWNERS marker {marker} at line {line_number} has no {_PARTNER[marker]}."
    )


def strip_inline_comment(line: str) -> str:
    """Drop an inline comment; a `#` only counts when at start or after space."""
    if line.lstrip().startswith("#"):
        return ""
    marker = line.find(" #")
    if marker != -1:
        return line[:marker]
    return line


def split_escaped(line: str) -> list[str]:
    """Split on whitespace while honoring backslash-escaped spaces."""
    return [token.replace("\\ ", " ") for token in re.split(r"(?<!\\)\s+", line) if token]


def matching_rules(rules: tuple[CodeownersRule, ...], path: str) -> tuple[CodeownersRule, ...]:
    """Rules that match ``path``, in file order. The last element is the winner.

    ``path`` is a repo-relative file path without a leading slash.
    Patterns GitHub skips (a leading ``!``, or ``[`` / ``]``) never match.
    """
    normalized = path.lstrip("/")
    return tuple(
        rule
        for rule in rules
        if _supported_pattern(rule.pattern) and pattern_matches(rule.pattern, normalized)
    )


def _supported_pattern(pattern: str) -> bool:
    """Return whether ``pattern`` may assign an owner."""
    return not pattern.startswith("!") and "[" not in pattern and "]" not in pattern


def _degenerate_globstar(pattern: str) -> bool:
    """Return whether ``pattern`` contains only ``**`` segments.

    ``**`` and ``/**`` are excluded: those match every relative path. A trailing
    slash is an empty segment, so ``**/`` and ``**/**`` are included.
    """
    body = pattern[1:] if pattern.startswith("/") else pattern
    if body in {"", "**"}:
        return False
    parts = body.split("/")
    return all(part in {"**", ""} for part in parts)


def match_path(rules: tuple[CodeownersRule, ...], path: str) -> CodeownersRule | None:
    """Return the last rule whose pattern matches ``path``, or None.

    ``path`` is a repo-relative file path without a leading slash.
    """
    matches = matching_rules(rules, path)
    return matches[-1] if matches else None


def pattern_matches(pattern: str, path: str) -> bool:
    """True when a CODEOWNERS ``pattern`` covers the repo-relative ``path``."""
    if not pattern or "***" in pattern or _degenerate_globstar(pattern):
        return False
    regex = _compile_pattern(pattern)
    return regex.match(path) is not None


@lru_cache(maxsize=4096)
def _compile_pattern(pattern: str) -> re.Pattern[str]:
    return re.compile(_translate(pattern))


def _translate(pattern: str) -> str:
    """Translate a CODEOWNERS pattern into a regex over relative file paths."""
    p = pattern
    dir_only = p.endswith("/")
    if dir_only:
        p = p.rstrip("/")
    anchored = p.startswith("/")
    p = p.lstrip("/")
    # An interior separator anchors the pattern to the repo root, same as
    # gitignore; a bare name or trailing-slash-only pattern floats.
    if "/" in p:
        anchored = True
    if not p:
        return r"(?!)"
    segments = p.split("/")
    parts: list[str] = []
    for index, segment in enumerate(segments):
        if segment == "**":
            parts.append(r"(?:[^/]+/)*" if index < len(segments) - 1 else r".*")
            continue
        if segment == "*":
            parts.append(r"[^/]+" + ("/" if index < len(segments) - 1 else ""))
            continue
        escaped = _translate_segment(segment)
        parts.append(escaped + ("/" if index < len(segments) - 1 else ""))
    body = "".join(parts)
    if body.endswith(".*"):
        suffix = r"\Z"
    elif dir_only:
        # Trailing slash: the match must be a file inside the directory.
        suffix = r"/.+\Z"
    elif segments[-1] == "*":
        # GitHub documents `dir/*` as direct children only, unlike gitignore.
        suffix = r"\Z"
    else:
        # A file match, or a directory whose contents are all covered.
        suffix = r"(?:/.*)?\Z"
    prefix = r"" if anchored else r"(?:[^/]+/)*"
    return prefix + body + suffix


def _translate_segment(segment: str) -> str:
    """Translate one path segment into a regex fragment.

    ``*`` and ``?`` stay inside the segment. A backslash before ``*`` or ``?``
    makes that wildcard literal. A leading ``\\#`` is not turned into ``#``.
    """
    out: list[str] = []
    escaped = False
    for char in segment:
        if escaped:
            if char == "#":
                out.append(re.escape("\\") + re.escape(char))
            else:
                out.append(re.escape(char))
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "*":
            out.append(r"[^/]*")
        elif char == "?":
            out.append(r"[^/]")
        else:
            out.append(re.escape(char))
    return "".join(out)
