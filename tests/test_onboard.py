"""Tests for checkowners.onboard module."""

from __future__ import annotations

from datetime import UTC, datetime

from checkowners.models import (
    AnalysisConfig,
    Config,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
)
from checkowners.onboard import generate_onboarding_path

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _entry(handle: str, confidence: float, commits: int = 5) -> OwnerEntry:
    return OwnerEntry(handle=handle, confidence=confidence, last_commit=_NOW, commits=commits)


def _ownership(raw: dict[str, tuple[OwnerEntry, ...]]) -> OwnershipMap:
    return OwnershipMap(
        paths={
            p: PathOwnership(
                owners=owners,
                bus_factor=sum(1 for o in owners if o.confidence >= 0.3),
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


def test_generate_onboarding_orders_broad_to_deep() -> None:
    ownership = _ownership(
        {
            "src/payments/utils.py": (
                _entry("@alice", 0.9),
                _entry("@bob", 0.7),
                _entry("@carol", 0.6),
            ),
            "src/payments/billing.py": (_entry("@alice", 0.9), _entry("@bob", 0.7)),
            "src/payments/secret.py": (_entry("@alice", 0.95),),
        }
    )
    report = generate_onboarding_path(
        ownership,
        _config(),
        target="src/payments/",
        max_steps=3,
    )
    paths = [step.path for step in report.steps]
    assert paths == [
        "src/payments/utils.py",
        "src/payments/billing.py",
        "src/payments/secret.py",
    ]


def test_generate_onboarding_complexity_increases() -> None:
    abc = (_entry("@alice", 0.9), _entry("@bob", 0.7), _entry("@carol", 0.6))
    ab = (_entry("@alice", 0.9), _entry("@bob", 0.7))
    solo = (_entry("@alice", 0.95),)
    ownership = _ownership(
        {
            "src/payments/a.py": abc,
            "src/payments/b.py": abc,
            "src/payments/c.py": ab,
            "src/payments/d.py": ab,
            "src/payments/e.py": solo,
            "src/payments/f.py": solo,
        }
    )
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
    # Every path has bus_factor 1 (deep expertise), so nothing may be 'easy'.
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
    assert "# Onboarding path for src/payments/" in md
    assert "- [ ] **Step 1**" in md


def test_to_markdown_handles_empty_path() -> None:
    ownership = _ownership({})
    report = generate_onboarding_path(ownership, _config(), target="src/")
    md = report.to_markdown()
    assert "No learning path could be built" in md
