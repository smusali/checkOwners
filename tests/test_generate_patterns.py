"""Generated CODEOWNERS patterns: sanitizing, merging and the broad-rule guard."""

from __future__ import annotations

import pytest

from checkowners.generate import (
    BroadPatternRecord,
    CodeownersSizeError,
    _build_codeowners_content,
    _can_explode,
    _display_pattern,
    _ensure_size_ok,
    _explode_row,
    _generated_block,
    _last_written_match,
    _merge_owners,
    _merge_same_pattern,
    _path_owner_map,
    _render_codeowners,
    _Row,
    _sanitize_pattern,
    broad_pattern_warning,
    codeowners_write_metrics,
    generate_codeowners,
)
from checkowners.models import (
    OwnerEntry,
)
from checkowners.patterns import match_path, parse_rules, pattern_matches
from tests.conftest import (
    NOW,
    GitRepo,
    analyze_regression_repo,
    regression_analysis_config,
    script_regression_repo,
)
from tests.test_generate import _entry, _make_ownership, _zero_threshold


def test_bracket_segments_become_wildcards() -> None:
    """A sibling directory makes /app/*/ lossy, so per-file wildcards are used."""
    ownership = _make_ownership(
        {
            "app/[teamId]/page.tsx": (_entry("@alice", 0.9),),
            "app/[teamId]/layout.tsx": (_entry("@alice", 0.8),),
            "app/static/index.tsx": (_entry("@bob", 0.9),),
        }
    )
    content, _expected, records = _render_codeowners(ownership, _zero_threshold())
    lines = [line for line in content.splitlines() if line and not line.startswith("#")]
    assert lines == [
        "/app/*/layout.tsx @alice",
        "/app/*/page.tsx @alice",
        "/app/static/index.tsx @bob",
    ]
    assert "[" not in content
    assert records
    assert all(not record.accepted for record in records)
    record = records[0]
    assert record.as_json() == {
        "source": record.source,
        "generated_fallback": record.generated_fallback,
        "also_matches": list(record.also_matches),
        "accepted": record.accepted,
        "owner_delta": {
            "intended": list(record.intended_owners),
            "affected": [
                {"path": path, "owners": list(owners)} for path, owners in record.affected
            ],
        },
    }
    assert ("@bob",) in {owners for record in records for _path, owners in record.affected}
    warning = broad_pattern_warning(record)
    assert warning == (
        f"WARNING: GitHub CODEOWNERS cannot precisely represent: {record.source}\n"
        f"Generated fallback: {record.generated_fallback}\n"
        f"This also matches: {', '.join(record.also_matches)}\n"
        "Affected owners differ. Refusing automatic consolidation."
    )
    listed = broad_pattern_warning(
        BroadPatternRecord(
            source="src/[id]/a.py",
            generated_fallback="src/*/a.py",
            also_matches=("src/a.py", "src/b.py"),
            intended_owners=("@alice",),
            affected=(("src/a.py", ("@bob",)), ("src/b.py", ("@carol",))),
            accepted=False,
        )
    )
    assert "This also matches: src/a.py, src/b.py\n" in listed


def test_colliding_sanitized_patterns_merge_owners() -> None:
    ownership = _make_ownership(
        {
            "app/[teamId]/page.tsx": (_entry("@alice", 0.9),),
            "app/[userId]/view.tsx": (_entry("@bob", 0.6),),
        }
    )
    config = _zero_threshold(consolidate=False)
    content = _build_codeowners_content(ownership, config)
    lines = [line for line in content.splitlines() if line and not line.startswith("#")]
    assert lines == ["/app/*/page.tsx @alice", "/app/*/view.tsx @bob"]
    refused = _make_ownership(
        {
            "app/[id]/page.tsx": (_entry("@alice", 0.9),),
            "app/other/page.tsx": (_entry("@bob", 0.6),),
        }
    )
    refused_content, _expected, records = _render_codeowners(
        refused, _zero_threshold(consolidate=False)
    )
    refused_lines = [
        line for line in refused_content.splitlines() if line and not line.startswith("#")
    ]
    assert refused_lines == ["/app/[id]/page.tsx @alice", "/app/other/page.tsx @bob"]
    assert records[0].accepted is False


def test_one_extra_path_records_a_broad_pattern() -> None:
    ownership = _make_ownership(
        {
            "app/[id]/page.tsx": (_entry("@alice", 0.9),),
            "app/other/page.tsx": (_entry("@alice", 0.8),),
        }
    )
    _content, expected, records = _render_codeowners(ownership, _zero_threshold(consolidate=False))
    assert "app/[id]/page.tsx" in expected
    assert records[0].accepted is True
    assert "app/other/page.tsx" in records[0].also_matches


def test_two_sanitized_rows_record_a_broad_pattern() -> None:
    ownership = _make_ownership(
        {
            "app/[id]/a.tsx": (_entry("@alice", 0.9),),
            "app/[name]/a.tsx": (_entry("@alice", 0.8),),
        }
    )
    _content, _expected, records = _render_codeowners(ownership, _zero_threshold(consolidate=False))
    assert records
    assert records[0].accepted is True


@pytest.mark.parametrize(
    ("second_owner", "allow_broad", "expected_lines", "accepted"),
    [
        (
            "@bob",
            False,
            [
                "/app/*/page.tsx @bob",
                "/app/[teamId]/page.tsx @alice",
                "/app/[userId]/page.tsx @bob",
            ],
            False,
        ),
        ("@alice", False, ["/app/*/page.tsx @alice"], True),
        ("@bob", True, ["/app/*/page.tsx @alice @bob"], True),
    ],
)
def test_identical_sanitized_patterns_merge_owner_sets(
    second_owner: str,
    allow_broad: bool,
    expected_lines: list[str],
    accepted: bool,
) -> None:
    ownership = _make_ownership(
        {
            "app/[teamId]/page.tsx": (_entry("@alice", 0.9),),
            "app/[userId]/page.tsx": (_entry(second_owner, 0.6),),
            "app/*/page.tsx": (_entry(second_owner, 0.6),),
        }
    )
    config = _zero_threshold(consolidate=False, allow_broad_patterns=allow_broad)
    content, _expected, records = _render_codeowners(ownership, config)
    lines = [line for line in content.splitlines() if line and not line.startswith("#")]
    assert lines == expected_lines
    assert records
    assert all(record.accepted is accepted for record in records)
    payload = records[0].as_json()
    assert payload["accepted"] is accepted
    assert payload["generated_fallback"] == "app/*/page.tsx"
    if accepted:
        return
    warning = broad_pattern_warning(records[0])
    assert "WARNING: GitHub CODEOWNERS cannot precisely represent: app/[teamId]/page.tsx" in warning
    assert "Generated fallback: app/*/page.tsx" in warning
    assert "app/[userId]/page.tsx" in warning
    assert "Affected owners differ. Refusing automatic consolidation." in warning


def test_pattern_spaces_escaped_on_write() -> None:
    ownership = _make_ownership({"docs/getting started.md": (_entry("@alice", 0.9),)})
    content = _build_codeowners_content(ownership, _zero_threshold())
    assert "/docs/getting\\ started.md @alice" in content


def test_pattern_helpers_keep_literals_and_boundaries() -> None:
    assert _sanitize_pattern("a/b") == "a/b"
    assert _sanitize_pattern("foo[bar") == "*"
    assert _sanitize_pattern("foo]bar") == "*"
    assert _display_pattern("/src/") == "src/"
    assert _display_pattern("Xsrc") == "Xsrc"
    assert _can_explode(_Row(pattern="src/", owners=(), paths=("src/a.py",))) is True
    assert _can_explode(_Row(pattern="*", owners=(), paths=("a.py",))) is True
    assert _can_explode(_Row(pattern="/a.py", owners=(), paths=("a.py",))) is False
    assert _can_explode(_Row(pattern="/a.py", owners=(), paths=("a.py", "b.py"))) is True
    mapped = _path_owner_map(_make_ownership({"/src/main.py": (_entry("@alice", 0.5),)}), 0.5)
    assert list(mapped) == ["src/main.py"]
    assert mapped["src/main.py"][0].handle == "@alice"
    mapped_x = _path_owner_map(_make_ownership({"Xsrc/a.py": (_entry("@alice", 0.5),)}), 0.5)
    assert list(mapped_x) == ["Xsrc/a.py"]
    assert _generated_block("") == ("# checkowners:generated:start\n# checkowners:generated:end\n")
    assert _generated_block("*\n") == (
        "# checkowners:generated:start\n*\n# checkowners:generated:end\n"
    )
    literal = _last_written_match(parse_rules("Xa.py @alice\n"), "Xa.py")
    assert literal is not None
    assert literal.owners == ("@alice",)
    rooted = _last_written_match(parse_rules("* @alice\n"), "/a.py")
    assert rooted is not None
    assert _last_written_match(parse_rules("src/ @alice\n"), "other.py") is None
    row = _Row(pattern="/src/", owners=(_entry("@alice", 0.9),), paths=("src/a.py", "src/b.py"))
    exploded = _explode_row(row, {"src/a.py": (_entry("@bob", 0.4),)})
    assert exploded[0].owners[0].handle == "@bob"
    assert exploded[1].owners[0].handle == "@alice"
    merged_rows = _merge_same_pattern(
        [
            _Row(pattern="/app/*/page.tsx", owners=(_entry("@alice", 0.9),), paths=("a.tsx",)),
            _Row(pattern="/app/*/page.tsx", owners=(_entry("@bob", 0.4),), paths=("b.tsx",)),
        ]
    )
    assert merged_rows[0].paths == ("a.tsx", "b.tsx")
    merged = _merge_owners(
        [
            (_entry("@alice", 0.2),),
            (
                OwnerEntry(
                    handle="@alice",
                    ownership_score=0.8,
                    last_commit=None,
                    commits=3,
                    evidence_quality=0.4,
                ),
            ),
        ]
    )
    assert merged[0].ownership_score == pytest.approx(0.5)
    assert merged[0].evidence_quality == pytest.approx(0.7)
    assert merged[0].commits == 8
    assert merged[0].last_commit == NOW
    undated = _merge_owners(
        [
            (
                OwnerEntry(
                    handle="@ada",
                    ownership_score=0.4,
                    last_commit=None,
                    commits=1,
                    evidence_quality=1.0,
                ),
            )
        ]
    )
    assert undated[0].last_commit is None
    _ensure_size_ok("abc", 3, force=False)
    with pytest.raises(CodeownersSizeError, match="4 bytes"):
        _ensure_size_ok("abcd", 3, force=False)
    assert codeowners_write_metrics("* @alice\n") == {
        "bytes_written": len(b"* @alice\n"),
        "rules_written": 1,
    }


def test_generate_sanitizes_tracked_bracket_and_space_paths(git_repo: GitRepo) -> None:
    script_regression_repo(git_repo)
    ownership = analyze_regression_repo(git_repo)
    written = generate_codeowners(git_repo.path, ownership, regression_analysis_config())
    rule_lines = [
        line for line in written.content.splitlines() if line and not line.startswith("#")
    ]
    assert rule_lines
    assert all("[" not in line and "]" not in line for line in rule_lines)
    assert any("getting\\ started.md" in line for line in rule_lines)
    assert any("notes/ünicode.py" in line for line in rule_lines)
    assert all("\\" not in line.replace("\\ ", "") for line in rule_lines)
    assert not any(len(line) >= 2 and line[1] == ":" for line in rule_lines)
    bracket = next(path for path in git_repo.ls_files() if "[" in path and "]" in path)
    winner = match_path(parse_rules(written.content), bracket)
    assert winner is not None
    assert "[" not in winner.pattern
    assert "]" not in winner.pattern
    assert pattern_matches(winner.pattern, bracket)
