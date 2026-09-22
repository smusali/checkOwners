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
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class CodeownersRule:
    """One parsed CODEOWNERS line: a pattern plus its owner handles."""

    pattern: str
    owners: tuple[str, ...]
    line_number: int


def parse_rules(content: str) -> tuple[CodeownersRule, ...]:
    """Parse CODEOWNERS text into ordered rules (comments and blanks skipped).

    A pattern with no owners is kept: GitHub uses owner-less rules to exempt
    paths from a broader earlier rule.
    """
    rules: list[CodeownersRule] = []
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        line = strip_inline_comment(raw_line).strip()
        if not line:
            continue
        parts = split_escaped(line)
        rules.append(
            CodeownersRule(
                pattern=parts[0],
                owners=tuple(parts[1:]),
                line_number=line_number,
            )
        )
    return tuple(rules)


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
