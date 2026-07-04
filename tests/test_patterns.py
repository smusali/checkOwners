"""Tests for CODEOWNERS pattern matching."""

from __future__ import annotations

import pytest

from checkowners.patterns import (
    CodeownersRule,
    match_path,
    parse_rules,
    pattern_matches,
)


class TestPatternMatches:
    """Semantics from GitHub's documented CODEOWNERS examples."""

    @pytest.mark.parametrize(
        ("pattern", "path", "expected"),
        [
            # *.ext matches anywhere in the tree
            ("*.js", "app.js", True),
            ("*.js", "src/deep/nested/app.js", True),
            ("*.js", "app.ts", False),
            # Global catch-all
            ("*", "anything.txt", True),
            ("*", "deep/nested/file.py", True),
            # Anchored directory (leading and trailing slash)
            ("/build/logs/", "build/logs/run.log", True),
            ("/build/logs/", "build/logs/deep/run.log", True),
            ("/build/logs/", "src/build/logs/run.log", False),
            ("/build/logs/", "build/logs", False),
            # dir/* is direct children only (GitHub-documented exception)
            ("docs/*", "docs/getting-started.md", True),
            ("docs/*", "docs/build-app/troubleshooting.md", False),
            # Floating directory name (trailing slash only)
            ("apps/", "apps/main.py", True),
            ("apps/", "src/apps/main.py", True),
            ("apps/", "grapps/main.py", False),
            # Anchored directory
            ("/docs/", "docs/index.md", True),
            ("/docs/", "src/docs/index.md", False),
            # Interior slash anchors even without a leading one
            ("frontend/package.json", "frontend/package.json", True),
            ("frontend/package.json", "sub/frontend/package.json", False),
            # Bare name floats and covers directory contents
            ("README.md", "README.md", True),
            ("README.md", "docs/README.md", True),
            ("models", "models/user.py", True),
            # Globstar forms
            ("**/logs", "build/logs/run.log", True),
            ("**/logs", "logs/run.log", True),
            ("src/**", "src/a/b/c.py", True),
            ("src/**", "other/a.py", False),
            ("a/**/b", "a/b/file.txt", True),
            ("a/**/b", "a/x/y/b/file.txt", True),
            ("a/**/b", "a/x/c/file.txt", False),
            # Single-char wildcard
            ("file?.txt", "file1.txt", True),
            ("file?.txt", "file12.txt", False),
            # Trailing-slash pattern needs contents, not the bare path
            ("workers/workflow/", "workers/workflow/run.js", True),
            ("workers/workflow/", "workers/workflow-v2/run.js", False),
        ],
    )
    def test_pattern_matches(self, pattern: str, path: str, expected: bool) -> None:
        assert pattern_matches(pattern, path) is expected


class TestParseRules:
    def test_parses_patterns_owners_and_line_numbers(self) -> None:
        content = "\n".join(
            [
                "# comment",
                "*.js @js-owner",
                "",
                "docs/ @octocat @hubot",
            ]
        )
        rules = parse_rules(content)
        assert rules == (
            CodeownersRule(pattern="*.js", owners=("@js-owner",), line_number=2),
            CodeownersRule(pattern="docs/", owners=("@octocat", "@hubot"), line_number=4),
        )

    def test_keeps_ownerless_rules_for_exemptions(self) -> None:
        rules = parse_rules("*.md @docs-team\ninternal/README.md\n")
        assert rules[1].owners == ()

    def test_strips_inline_comments(self) -> None:
        rules = parse_rules("*.py @dev # alice(0.92) bob(0.71)\n")
        assert rules[0].owners == ("@dev",)


class TestMatchPath:
    def test_last_matching_rule_wins(self) -> None:
        rules = parse_rules("* @global\ndocs/ @docs-team\n")
        matched = match_path(rules, "docs/index.md")
        assert matched is not None
        assert matched.owners == ("@docs-team",)

    def test_returns_none_when_nothing_matches(self) -> None:
        rules = parse_rules("docs/ @docs-team\n")
        assert match_path(rules, "src/main.py") is None

    def test_ownerless_later_rule_shadows_earlier_owner(self) -> None:
        rules = parse_rules("*.md @docs-team\ninternal/README.md\n")
        matched = match_path(rules, "internal/README.md")
        assert matched is not None
        assert matched.owners == ()


class TestEscapedSpaces:
    def test_parse_rules_unescapes_pattern_spaces(self) -> None:
        rules = parse_rules("docs/getting\\ started.md @docs-team\n")
        assert rules[0].pattern == "docs/getting started.md"
        assert rules[0].owners == ("@docs-team",)

    def test_match_path_with_escaped_space_pattern(self) -> None:
        rules = parse_rules("docs/getting\\ started.md @docs-team\n")
        matched = match_path(rules, "docs/getting started.md")
        assert matched is not None
        assert matched.owners == ("@docs-team",)
