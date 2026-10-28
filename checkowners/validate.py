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
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from checkowners.patterns import split_escaped, strip_inline_comment

_DEFAULT_CODEOWNERS_PATH = ".github/CODEOWNERS"
SyntaxClass = Literal["negation", "brackets", "malformed_owner"]
SYNTAX_CLASSES: tuple[SyntaxClass, ...] = ("negation", "brackets", "malformed_owner")

#: GitHub logins: alphanumerics and internal hyphens (max 39 chars); team
#: owners are @org/team-slug; email owners are accepted as-is by GitHub.
_HANDLE_PATTERN = re.compile(r"^@[A-Za-z\d](?:[A-Za-z\d]|-(?=[A-Za-z\d])){0,38}(/[\w.-]+)?$")
_EMAIL_PATTERN = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")


@dataclass(frozen=True)
class ValidationFinding:
    line_number: int
    line: str
    message: str


def validate_codeowners(
    repo_root: Path,
    *,
    codeowners_path: Path | None = None,
) -> list[ValidationFinding]:
    """Validate CODEOWNERS syntax and return a list of errors."""
    target = codeowners_path or (repo_root / _DEFAULT_CODEOWNERS_PATH)
    if not target.exists():
        return [ValidationFinding(line_number=0, line="", message="CODEOWNERS file not found")]
    content = target.read_text(encoding="utf-8")
    return _validate_lines(content)


def _validate_lines(content: str) -> list[ValidationFinding]:
    """Validate each line of CODEOWNERS content."""
    errors: list[ValidationFinding] = []
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        # Strip inline comment so confidence annotations don't fail validation.
        line = strip_inline_comment(raw_line.strip()).strip()
        if not line:
            continue
        errors.extend(_validate_entry(line_number, line))
    return errors


def _validate_entry(line_number: int, line: str) -> list[ValidationFinding]:
    """Validate a single CODEOWNERS entry line."""
    errors: list[ValidationFinding] = []
    parts = split_escaped(line)
    if not parts:
        return errors

    pattern = parts[0]
    if pattern.startswith("!"):
        errors.append(
            ValidationFinding(
                line_number=line_number,
                line=line,
                message=f"GitHub CODEOWNERS does not support '!' negation: {pattern}",
            )
        )
    if "[" in pattern or "]" in pattern:
        errors.append(
            ValidationFinding(
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
                ValidationFinding(
                    line_number=line_number,
                    line=line,
                    message=f"Invalid owner format: {owner}",
                )
            )

    return errors


@dataclass(frozen=True)
class RemoteCodeownersError:
    line: int
    column: int
    kind: str
    message: str
    source: str
    path: str


@dataclass(frozen=True)
class ConformanceCase:
    repo: str
    ref: str
    default_branch: str
    private: bool
    text: str
    api_errors: tuple[RemoteCodeownersError, ...]


@dataclass(frozen=True)
class SyntaxComparison:
    source: Literal["github", "local"]
    agrees: bool
    inconclusive: bool
    local: tuple[tuple[int, SyntaxClass], ...]
    remote: tuple[tuple[int, SyntaxClass], ...]
    other_kinds: tuple[str, ...]


def syntax_classes(
    findings: Sequence[ValidationFinding],
) -> tuple[tuple[int, SyntaxClass], ...]:
    found: list[tuple[int, SyntaxClass]] = []
    for finding in findings:
        kind = _syntax_class(finding.message)
        if kind is not None:
            found.append((finding.line_number, kind))
    return tuple(found)


def compare_codeowners_syntax(
    text: str,
    api_errors: tuple[RemoteCodeownersError, ...] | None,
    *,
    private: bool,
    ref: str,
    default_branch: str,
) -> SyntaxComparison:
    """Compare local syntax findings for `text` with `api_errors`.

    `api_errors` None means the endpoint was not used. A private repository
    whose non-default branch returns no errors while local syntax findings
    exist is inconclusive.
    """
    local = syntax_classes(_validate_lines(text))
    if api_errors is None:
        return SyntaxComparison("local", True, False, local, (), ())
    remote = _remote_classes(api_errors)
    inconclusive = _inconclusive(
        private=private,
        ref=ref,
        default_branch=default_branch,
        api_errors=api_errors,
        local=local,
    )
    return SyntaxComparison(
        "github",
        set(local) == set(remote),
        inconclusive,
        local,
        remote,
        _other_kinds(api_errors),
    )


def conformance_report(cases: Sequence[ConformanceCase]) -> dict[str, object]:
    """Return the pass rate for `cases`.

    Inconclusive private-branch responses are excluded from the denominator.
    """
    compared = 0
    agreed = 0
    excluded = 0
    divergences: list[dict[str, object]] = []
    for case in cases:
        result = compare_codeowners_syntax(
            case.text,
            case.api_errors,
            private=case.private,
            ref=case.ref,
            default_branch=case.default_branch,
        )
        if result.inconclusive:
            excluded += 1
            continue
        compared += 1
        if result.agrees:
            agreed += 1
            continue
        divergences.append(
            {
                "repo": case.repo,
                "ref": case.ref,
                "local": [{"line": line, "class": kind} for line, kind in result.local],
                "remote": [{"line": line, "class": kind} for line, kind in result.remote],
                "other_kinds": list(result.other_kinds),
            }
        )
    rate = round(agreed / compared, 4) if compared else None
    return {
        "pass_rate": rate,
        "compared": compared,
        "agreed": agreed,
        "excluded": excluded,
        "syntax_classes": list(SYNTAX_CLASSES),
        "divergences": divergences,
    }


def _syntax_class(message: str) -> SyntaxClass | None:
    if "negation" in message:
        return "negation"
    if "character ranges" in message:
        return "brackets"
    if message.startswith("Invalid owner format"):
        return "malformed_owner"
    return None


def _remote_classes(
    errors: tuple[RemoteCodeownersError, ...],
) -> tuple[tuple[int, SyntaxClass], ...]:
    found: list[tuple[int, SyntaxClass]] = []
    for error in errors:
        if error.source.strip():
            found.extend(syntax_classes(_validate_entry(error.line, error.source.strip())))
            continue
        folded = f"{error.kind} {error.message}".casefold()
        if "negation" in folded:
            found.append((error.line, "negation"))
        elif "bracket" in folded or "character range" in folded:
            found.append((error.line, "brackets"))
    return tuple(found)


def _other_kinds(errors: tuple[RemoteCodeownersError, ...]) -> tuple[str, ...]:
    kinds: list[str] = []
    for error in errors:
        source = error.source.strip()
        local = syntax_classes(_validate_entry(error.line, source)) if source else ()
        if local:
            continue
        folded = error.kind.casefold()
        if "negation" in folded or "bracket" in folded:
            continue
        if error.kind:
            kinds.append(error.kind)
    return tuple(sorted(set(kinds)))


def _inconclusive(
    *,
    private: bool,
    ref: str,
    default_branch: str,
    api_errors: tuple[RemoteCodeownersError, ...],
    local: tuple[tuple[int, SyntaxClass], ...],
) -> bool:
    return bool(private and ref != default_branch and not api_errors and local)
