"""Tests for checkowners.onboard module."""

from __future__ import annotations

from datetime import UTC, datetime

from checkowners.models import (
    AnalysisConfig,
    Config,
    ContributorStatus,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
)
from checkowners.onboard import generate_onboarding_path
from checkowners.privacy import KNOWLEDGE_RISK_NOTICE

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _entry(
    handle: str,
    confidence: float,
    commits: int = 5,
    *,
    contributor_status: ContributorStatus = "unknown",
) -> OwnerEntry:
    return OwnerEntry(
        handle=handle,
        ownership_score=confidence,
        last_commit=_NOW,
        commits=commits,
        contributor_status=contributor_status,
    )


def _ownership(raw: dict[str, tuple[OwnerEntry, ...]]) -> OwnershipMap:
    return OwnershipMap(
        paths={
            p: PathOwnership(
                owners=owners,
                qualified_owner_count=sum(1 for o in owners if o.confidence >= 0.3),
            )
            for p, owners in raw.items()
        },
        last_analyzed=_NOW,
    )


def _config() -> Config:
    return Config(analysis=AnalysisConfig(confidence_threshold=0.3))


def test_generate_onboarding_no_match_returns_empty_path() -> None:
    ownership = _ownership({"src/api.py": (_entry("@alice", 0.9),)})
    report = generate_onboarding_path(ownership, _config(), target="src/payments/")
    assert report.steps == ()


def test_generate_onboarding_orders_by_dependency() -> None:
    ownership = _ownership(
        {
            "src/payments/secret.py": (
                _entry("@alice", 0.9),
                _entry("@bob", 0.7),
                _entry("@carol", 0.6),
            ),
            "src/payments/billing.py": (_entry("@alice", 0.9), _entry("@bob", 0.7)),
            "src/payments/utils.py": (_entry("@alice", 0.95),),
        }
    )
    report = generate_onboarding_path(
        ownership,
        _config(),
        target="src/payments/",
        max_steps=3,
        dependency_edges=(
            ("src/payments/billing.py", "src/payments/utils.py"),
            ("src/payments/secret.py", "src/payments/billing.py"),
        ),
    )
    paths = [step.path for step in report.steps]
    assert paths == [
        "src/payments/utils.py",
        "src/payments/billing.py",
        "src/payments/secret.py",
    ]
    assert "depends on src/payments/utils.py" in report.steps[1].description
    assert "3 qualified owners" in report.steps[2].description
    assert "dependency order" not in report.unavailable_signals


def test_onboarding_declares_unavailable_signals_and_skips_former_mentors() -> None:
    ownership = _ownership(
        {
            "src/payments/a.py": (
                _entry("@alice", 0.9, commits=2, contributor_status="active"),
                _entry("@bob", 0.8, commits=9, contributor_status="active"),
                _entry("@carol", 0.95, commits=1, contributor_status="former"),
            ),
            "src/payments/b.py": (
                _entry("@alice", 0.9, commits=2, contributor_status="active"),
                _entry("@bob", 0.8, commits=9, contributor_status="active"),
                _entry("@carol", 0.95, commits=1, contributor_status="former"),
            ),
        }
    )
    report = generate_onboarding_path(ownership, _config(), target="src/payments/")
    assert report.experimental is True
    assert report.unavailable_signals == (
        "code complexity",
        "test coverage",
        "documentation coverage",
        "dependency order",
        "domain criticality",
    )
    assert report.mentor_candidates == ("@alice", "@bob")
    assert "@carol" not in report.mentor_candidates


def test_cycle_falls_back_to_path_order() -> None:
    owners = (_entry("@alice", 0.9), _entry("@bob", 0.7))
    ownership = _ownership(
        {
            "src/payments/b.py": owners,
            "src/payments/a.py": owners,
        }
    )
    report = generate_onboarding_path(
        ownership,
        _config(),
        target="src/payments/",
        dependency_edges=(
            ("src/payments/a.py", "src/payments/b.py"),
            ("src/payments/b.py", "src/payments/a.py"),
        ),
    )
    assert [step.path for step in report.steps] == [
        "src/payments/a.py",
        "src/payments/b.py",
    ]
    assert "cycle; path order" in report.steps[0].description
    assert "Cycle:" in report.to_markdown()


def test_generate_onboarding_complexity_increases() -> None:
    owners = (_entry("@alice", 0.9), _entry("@bob", 0.7))
    ownership = _ownership({f"src/payments/{name}.py": owners for name in "abcdef"})
    report = generate_onboarding_path(
        ownership,
        _config(),
        target="src/payments/",
        max_steps=6,
    )
    complexities = [step.complexity for step in report.steps]
    # First third easy, last third hard
    assert complexities[0] == "easy"
    assert complexities[-1] == "hard"


def test_generate_onboarding_bus_factor_one_never_easy() -> None:
    solo = (_entry("@alice", 0.95),)
    ownership = _ownership(
        {
            "src/payments/a.py": solo,
            "src/payments/b.py": solo,
            "src/payments/c.py": solo,
            "src/payments/d.py": solo,
            "src/payments/e.py": solo,
            "src/payments/f.py": solo,
        }
    )
    report = generate_onboarding_path(ownership, _config(), target="src/payments/")
    assert report.steps
    # Every path has qualified_owner_count 1 (deep expertise), so nothing may be 'easy'.
    for step in report.steps:
        assert step.complexity != "easy"
    # The first third is still the gentlest tier available: medium.
    assert report.steps[0].complexity == "medium"


def test_generate_onboarding_caps_at_max_steps() -> None:
    owners = (_entry("@alice", 0.9), _entry("@bob", 0.7))
    ownership = _ownership({f"src/payments/mod_{i:02d}.py": owners for i in range(20)})
    report = generate_onboarding_path(ownership, _config(), target="src/payments/", max_steps=4)
    assert len(report.steps) == 4


def test_generate_onboarding_rotates_reviewers() -> None:
    ownership = _ownership(
        {
            "src/payments/a.py": (_entry("@alice", 0.9), _entry("@bob", 0.85)),
            "src/payments/b.py": (_entry("@alice", 0.9), _entry("@bob", 0.85)),
            "src/payments/c.py": (_entry("@alice", 0.9), _entry("@bob", 0.85)),
        }
    )
    report = generate_onboarding_path(
        ownership,
        _config(),
        target="src/payments/",
        max_steps=3,
    )
    reviewers = [step.reviewer for step in report.steps]
    assert "@alice" in reviewers
    assert "@bob" in reviewers


def test_to_markdown_emits_checklist() -> None:
    ownership = _ownership(
        {
            "src/payments/a.py": (_entry("@alice", 0.9), _entry("@bob", 0.85)),
        }
    )
    report = generate_onboarding_path(
        ownership,
        _config(),
        target="src/payments/",
        max_steps=1,
    )
    md = report.to_markdown()
    assert "# Experimental onboarding suggestions for src/payments/" in md
    assert "- [ ] **Step 1**" in md
    assert KNOWLEDGE_RISK_NOTICE in md


def test_to_markdown_handles_empty_path() -> None:
    ownership = _ownership({})
    report = generate_onboarding_path(ownership, _config(), target="src/")
    md = report.to_markdown()
    assert "No learning path could be built" in md
    assert KNOWLEDGE_RISK_NOTICE in md
