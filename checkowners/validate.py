"""Syntax-only CODEOWNERS validator. No inference, no git access.

Mirrors GitHub's documented CODEOWNERS rules rather than inventing stricter
ones: patterns may be relative (``docs/``, ``apps/*``) or anchored (``/build/``),
a rule may have zero owners (GitHub's documented way to exempt paths from a
broader rule), and escaped spaces (``docs/getting\\ started.md``) are part of
the pattern. Constructs GitHub explicitly rejects (``!`` negation and ``[...]``
character ranges) are reported as errors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_CODEOWNERS_PATH = ".github/CODEOWNERS"

#: GitHub logins: alphanumerics and internal hyphens (max 39 chars); team
#: owners are @org/team-slug; email owners are accepted as-is by GitHub.
_HANDLE_PATTERN = re.compile(r"^@[A-Za-z\d](?:[A-Za-z\d]|-(?=[A-Za-z\d])){0,38}(/[\w.-]+)?$")
_EMAIL_PATTERN = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")


@dataclass(frozen=True)
class ValidationError:
    line_number: int
    line: str
    message: str


def validate_codeowners(
    repo_root: Path,
    *,
    codeowners_path: Path | None = None,
) -> list[ValidationError]:
    """Validate CODEOWNERS syntax and return a list of errors."""
    target = codeowners_path or (repo_root / _DEFAULT_CODEOWNERS_PATH)
    if not target.exists():
        return [ValidationError(line_number=0, line="", message="CODEOWNERS file not found")]
    content = target.read_text(encoding="utf-8")
    return _validate_lines(content)


def _validate_lines(content: str) -> list[ValidationError]:
    """Validate each line of CODEOWNERS content."""
    errors: list[ValidationError] = []
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Strip inline comment so confidence annotations don't fail validation.
        line = _strip_inline_comment(stripped)
        if not line:
            continue
        errors.extend(_validate_entry(line_number, line))
    return errors


def _strip_inline_comment(line: str) -> str:
    marker = line.find(" #")
    if marker != -1:
        return line[:marker].strip()
    return line


def _split_escaped(line: str) -> list[str]:
    """Split on whitespace while honoring backslash-escaped spaces."""
    return [token.replace("\\ ", " ") for token in re.split(r"(?<!\\)\s+", line) if token]


def _validate_entry(line_number: int, line: str) -> list[ValidationError]:
    """Validate a single CODEOWNERS entry line."""
    errors: list[ValidationError] = []
    parts = _split_escaped(line)
    if not parts:
        return errors

    pattern = parts[0]
    if pattern.startswith("!"):
        errors.append(
            ValidationError(
                line_number=line_number,
                line=line,
                message=f"GitHub CODEOWNERS does not support '!' negation: {pattern}",
            )
        )
    if "[" in pattern or "]" in pattern:
        errors.append(
            ValidationError(
                line_number=line_number,
                line=line,
                message=f"GitHub CODEOWNERS does not support '[...]' character ranges: {pattern}",
            )
        )

    # Zero owners is valid: GitHub documents an owner-less rule as the way to
    # exempt paths matched by a broader earlier rule.
    for owner in parts[1:]:
        if not _HANDLE_PATTERN.match(owner) and not _EMAIL_PATTERN.match(owner):
            errors.append(
                ValidationError(
                    line_number=line_number,
                    line=line,
                    message=f"Invalid owner format: {owner}",
                )
            )

    return errors
