"""Collapsing per-file ownership into directory rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from checkowners.config import (
    AnalysisConfig,
    Config,
    GithubConfig,
    OutputConfig,
)
from checkowners.generate import (
    CodeownersVerificationError,
    _build_codeowners_content,
    _consolidate,
    _render_codeowners,
    generate_codeowners,
    verify_round_trip,
)
from checkowners.models import (
    OwnerEntry,
)
from tests.test_generate import (
    _GOLDEN_CODEOWNERS,
    _entry,
    _make_ownership,
    _zero_threshold,
)


def test_consolidates_uniform_directory() -> None:
    """Files under one dir with the same owner set collapse to a dir rule."""
    ownership = _make_ownership(
        {
            "src/a.py": (_entry("@alice", 0.9),),
            "src/b.py": (_entry("@alice", 0.7),),
        }
    )
    config = _zero_threshold()
    content = _build_codeowners_content(ownership, config)
    lines = [line for line in content.splitlines() if line and not line.startswith("#")]
    assert lines == ["/src/ @alice"]


def test_consolidation_covers_nested_dirs() -> None:
    ownership = _make_ownership(
        {
            "src/api/a.py": (_entry("@alice", 0.9),),
            "src/lib/b.py": (_entry("@alice", 0.7),),
        }
    )
    content = _build_codeowners_content(ownership, _zero_threshold())
    lines = [line for line in content.splitlines() if line and not line.startswith("#")]
    assert lines == ["/src/ @alice"]


def test_mixed_directory_keeps_file_rules() -> None:
    ownership = _make_ownership(
        {
            "src/a.py": (_entry("@alice", 0.9),),
            "src/b.py": (_entry("@bob", 0.8),),
        }
    )
    content = _build_codeowners_content(ownership, _zero_threshold())
    lines = [line for line in content.splitlines() if line and not line.startswith("#")]
    assert lines == ["/src/a.py @alice", "/src/b.py @bob"]


def test_partial_consolidation() -> None:
    """Uniform subtree consolidates even when a sibling subtree is mixed."""
    ownership = _make_ownership(
        {
            "docs/x.md": (_entry("@carol", 0.9),),
            "docs/y.md": (_entry("@carol", 0.8),),
            "src/a.py": (_entry("@alice", 0.9),),
            "src/b.py": (_entry("@bob", 0.8),),
        }
    )
    content = _build_codeowners_content(ownership, _zero_threshold())
    lines = [line for line in content.splitlines() if line and not line.startswith("#")]
    assert lines == ["/docs/ @carol", "/src/a.py @alice", "/src/b.py @bob"]


def test_whole_repo_uniform_becomes_catch_all() -> None:
    ownership = _make_ownership(
        {
            "docs/x.md": (_entry("@alice", 0.9),),
            "src/a.py": (_entry("@alice", 0.8),),
        }
    )
    content = _build_codeowners_content(ownership, _zero_threshold())
    lines = [line for line in content.splitlines() if line and not line.startswith("#")]
    assert lines == ["* @alice"]


def test_consolidation_merges_owner_stats() -> None:
    """Dir-level owners aggregate confidence (mean) and commits (sum)."""
    ownership = _make_ownership(
        {
            "src/a.py": (_entry("@alice", 0.9, commits=4),),
            "src/b.py": (_entry("@alice", 0.5, commits=6),),
        }
    )
    rows = _consolidate(
        [
            ("src/a.py", ownership.paths["src/a.py"].owners),
            ("src/b.py", ownership.paths["src/b.py"].owners),
        ]
    )
    assert len(rows) == 1
    merged = rows[0].owners[0]
    assert merged.confidence == pytest.approx(0.7)
    assert merged.commits == 10


def test_consolidation_disabled_keeps_per_file_lines() -> None:
    ownership = _make_ownership(
        {
            "src/a.py": (_entry("@alice", 0.9),),
            "src/b.py": (_entry("@alice", 0.7),),
        }
    )
    config = _zero_threshold(consolidate=False)
    content, expected, records = _render_codeowners(ownership, config)
    lines = [line for line in content.splitlines() if line and not line.startswith("#")]
    assert lines == ["/src/a.py @alice", "/src/b.py @alice"]
    assert set(expected) == {"src/a.py", "src/b.py"}
    assert records == ()


def test_single_file_dir_not_consolidated() -> None:
    ownership = _make_ownership({"src/a.py": (_entry("@alice", 0.9),)})
    content, expected, _records = _render_codeowners(ownership, _zero_threshold())
    lines = [line for line in content.splitlines() if line and not line.startswith("#")]
    assert lines == ["/src/a.py @alice"]
    assert set(expected) == {"src/a.py"}


def test_codeowners_output_is_byte_stable() -> None:
    ownership = _make_ownership(
        {
            "src/b.py": (_entry("@bob", 0.8),),
            "docs/y.md": (_entry("@carol", 0.8),),
            "src/a.py": (_entry("@alice", 0.9),),
            "docs/x.md": (_entry("@carol", 0.9),),
        }
    )
    first = _build_codeowners_content(ownership, _zero_threshold())
    second = _build_codeowners_content(ownership, _zero_threshold())
    assert first == second
    assert first == _GOLDEN_CODEOWNERS


def test_generate_round_trip_accepts_manual_winner(tmp_path: Path) -> None:
    ownership = _make_ownership({"src/main.py": (_entry("@alice", 0.9),)})
    config = Config(
        analysis=AnalysisConfig(confidence_threshold=0.0),
        output=OutputConfig(block_order="generated-first"),
    )
    target = tmp_path / ".github" / "CODEOWNERS"
    target.parent.mkdir(parents=True)
    header = "# Generated by checkOwners. Do not edit manually.\n\n"
    manual = "# checkowners:manual:start\n* @security-team\n# checkowners:manual:end\n"
    target.write_text(f"{header}{manual}", encoding="utf-8")
    result = generate_codeowners(tmp_path, ownership, config)
    assert manual in result.content
    assert result.content.index("# checkowners:generated:end") < result.content.index(manual)
    verify_round_trip(result.content, {"src/main.py": frozenset({"@alice"})})


def test_verify_round_trip_catches_later_broad_rule() -> None:
    expected = {
        "src/a.py": frozenset({"@alice"}),
        "src/b.py": frozenset({"@alice"}),
    }
    content = "/src/ @alice\n* @bob\n"
    with pytest.raises(CodeownersVerificationError) as exc_info:
        verify_round_trip(content, expected)
    assert str(exc_info.value) == (
        "Round-trip verification failed for src/a.py: "
        "intended @alice, resolved @bob, winning rule: * @bob"
    )
    with pytest.raises(CodeownersVerificationError) as missing:
        verify_round_trip("/src/ @alice\n", {"missing.py": frozenset({"@alice"})})
    assert str(missing.value) == (
        "Round-trip verification failed for missing.py: "
        "intended @alice, resolved (none), winning rule: (none)"
    )
    manual = (
        "# checkowners:manual:start\n"
        "src/ok.py @security\n"
        "# checkowners:manual:end\n"
        "src/bad.py @bob\n"
    )
    with pytest.raises(CodeownersVerificationError) as skipped:
        verify_round_trip(
            manual,
            {
                "src/ok.py": frozenset({"@someone"}),
                "src/bad.py": frozenset({"@alice"}),
            },
        )
    assert "src/bad.py" in str(skipped.value)
    with pytest.raises(CodeownersVerificationError) as later:
        verify_round_trip(
            "src/ok.py @alice\nsrc/bad.py @bob\n",
            {
                "src/ok.py": frozenset({"@alice"}),
                "src/bad.py": frozenset({"@alice"}),
            },
        )
    assert str(later.value) == (
        "Round-trip verification failed for src/bad.py: "
        "intended @alice, resolved @bob, winning rule: src/bad.py @bob"
    )
    with pytest.raises(CodeownersVerificationError) as pair:
        verify_round_trip(
            "src/a.py @bob\n",
            {"src/a.py": frozenset({"@alice", "@carol"})},
        )
    assert "intended @alice @carol, resolved @bob" in str(pair.value)
    with pytest.raises(CodeownersVerificationError) as resolved_pair:
        verify_round_trip(
            "src/a.py @bob @carol\n",
            {"src/a.py": frozenset({"@alice"})},
        )
    assert "resolved @bob @carol" in str(resolved_pair.value)
    with pytest.raises(CodeownersVerificationError) as empty:
        verify_round_trip("src/a.py @bob\n", {"src/a.py": frozenset()})
    assert "intended (none), resolved @bob" in str(empty.value)


@pytest.mark.parametrize(
    ("raw", "org"),
    [
        (
            {
                "src/a.py": (_entry("@alice", 0.9),),
                "src/b.py": (_entry("@alice", 0.7),),
            },
            "",
        ),
        (
            {
                "src/a.py": (_entry("@alice", 0.9),),
                "src/b.py": (_entry("@bob", 0.8),),
            },
            "",
        ),
        (
            {
                "a.py": (_entry("@alice", 0.9),),
                "b.py": (_entry("@alice", 0.8),),
            },
            "",
        ),
        (
            {"src/main.py": (_entry("@alice", 0.9), _entry("@bob", 0.8))},
            "acme",
        ),
        (
            {
                "app/[teamId]/page.tsx": (_entry("@alice", 0.9),),
                "app/[userId]/page.tsx": (_entry("@bob", 0.6),),
            },
            "",
        ),
        (
            {
                "app/[teamId]/page.tsx": (_entry("@alice", 0.9),),
                "app/[teamId]/layout.tsx": (_entry("@alice", 0.8),),
                "app/static/index.tsx": (_entry("@bob", 0.9),),
            },
            "",
        ),
    ],
)
def test_generated_file_round_trips_inference(
    tmp_path: Path,
    raw: dict[str, tuple[OwnerEntry, ...]],
    org: str,
) -> None:
    ownership = _make_ownership(raw)
    config = _zero_threshold()
    if org:
        config = Config(
            analysis=AnalysisConfig(confidence_threshold=0.0),
            github=GithubConfig(org=org, resolve_teams=True),
        )
    team_resolve = (lambda _owners: "@acme/backend") if org else None
    written = generate_codeowners(tmp_path, ownership, config, team_resolve=team_resolve)
    _rendered, expected, _records = _render_codeowners(
        ownership,
        config,
        team_resolve=team_resolve,
    )
    assert written.content == _rendered
    verify_round_trip(written.content, expected)
