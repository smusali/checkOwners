"""Drift against team rules: alignment, membership gaps and team visibility."""

from __future__ import annotations

from checkowners.config import (
    Config,
)
from checkowners.drift import (
    compute_severity,
    detect_drift,
    drift_entry_payload,
    evidence_gaps,
    format_fix_plan,
    format_team_line,
    team_comparison_payload,
)
from checkowners.github import TeamDirectory, TeamRecord
from checkowners.models import (
    TEAM_MEMBERSHIP_REASON,
    DecayWarning,
    DriftEntry,
    DriftResult,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
    TeamComparison,
)
from checkowners.patterns import match_path, rules_with_source
from tests.conftest import (
    NOW,
    GitRepo,
    declared_ownership,
    drift_config,
    owner_entry,
    write_codeowners,
)


def _directory(*records: TeamRecord, org: str = "org") -> TeamDirectory:
    return TeamDirectory(org=org, fetched_at="2026-01-01T00:00:00+00:00", teams=records)


def test_aligned_team_is_not_drift(git_repo: GitRepo) -> None:
    write_codeowners(git_repo.path, "/payments/ @org/payments\n")
    ownership = declared_ownership({"payments/a.py": (owner_entry("@alice"), owner_entry("@bob"))})
    directory = _directory(
        TeamRecord(
            slug="payments",
            members=frozenset({"alice", "bob", "carol"}),
            parent="",
            children=(),
            visible=True,
        )
    )
    git_repo.commit_paths("payments/a.py")
    result = detect_drift(git_repo.path, ownership, drift_config(), teams=directory)
    assert result.changed == ()
    assert result.drift_detected is False
    row = result.teams[0]
    assert row.status == "aligned"
    assert row.team_coverage == 1.0
    assert row.inactive_members == ("@carol",)
    assert row.observed_outside_team == ()
    assert row.team_snapshot == "2026-01-01T00:00:00+00:00"
    assert evidence_gaps(result) == ()


def test_partial_team_drift_names_outsiders(git_repo: GitRepo) -> None:
    write_codeowners(git_repo.path, "/payments/ @org/payments\n")
    ownership = declared_ownership({"payments/a.py": (owner_entry("@alice"), owner_entry("@dave"))})
    directory = _directory(
        TeamRecord(
            slug="payments",
            members=frozenset({"alice", "bob", "carol"}),
            parent="",
            children=(),
            visible=True,
        )
    )
    git_repo.commit_paths("payments/a.py")
    result = detect_drift(git_repo.path, ownership, drift_config(), teams=directory)
    assert result.drift_detected is True
    assert result.changed[0].drift_type == "organizational_mismatch"
    row = result.teams[0]
    assert row.status == "partial-drift"
    assert row.team_coverage == 0.5
    assert row.observed_outside_team == ("@dave",)
    payload = drift_entry_payload(result.changed[0], 3)
    assert payload["team_coverage"] == 0.5
    assert payload["observed_outside_team"] == ["@dave"]
    assert payload["inactive_members"] == ["@bob", "@carol"]


def test_invisible_empty_and_nested_teams_are_distinct(git_repo: GitRepo) -> None:
    write_codeowners(
        git_repo.path,
        "/secret/ @org/secret\n/empty/ @org/empty\n/parent/ @org/parent\n",
    )
    ownership = declared_ownership(
        {
            "secret/a.py": (owner_entry("@alice"),),
            "empty/a.py": (owner_entry("@alice"),),
            "parent/a.py": (owner_entry("@dave"),),
        }
    )
    directory = _directory(
        TeamRecord(
            slug="secret",
            members=frozenset(),
            parent="",
            children=(),
            visible=False,
        ),
        TeamRecord(
            slug="empty",
            members=frozenset(),
            parent="",
            children=(),
            visible=True,
        ),
        TeamRecord(
            slug="parent",
            members=frozenset({"alice"}),
            parent="",
            children=("child",),
            visible=True,
        ),
        TeamRecord(
            slug="child",
            members=frozenset({"dave"}),
            parent="parent",
            children=(),
            visible=True,
        ),
    )
    git_repo.commit_paths("secret/a.py", "empty/a.py", "parent/a.py")
    result = detect_drift(git_repo.path, ownership, drift_config(), teams=directory)
    statuses = {row.team: row.status for row in result.teams}
    assert statuses == {
        "@org/secret": "invisible",
        "@org/empty": "empty",
        "@org/parent": "nested",
    }
    nested = result.teams[2]
    assert nested.team_coverage == 1.0
    assert nested.observed_outside_team == ()
    assert result.changed == ()
    assert result.drift_detected is False


def test_individual_rules_ignore_the_team_directory(git_repo: GitRepo) -> None:
    write_codeowners(git_repo.path, "src/ @alice\n")
    ownership = declared_ownership({"src/main.py": (owner_entry("@bob", 0.4),)})
    directory = _directory(
        TeamRecord(
            slug="payments",
            members=frozenset({"alice", "bob"}),
            parent="",
            children=(),
            visible=True,
        )
    )
    git_repo.commit_paths("src/main.py")
    result = detect_drift(git_repo.path, ownership, drift_config(), teams=directory)
    assert result.teams == ()
    assert result.changed[0].drift_type == "complete_ownership_replacement"
    assert result.changed[0].owners == ("@alice",)


def test_team_rules_record_the_membership_gap(git_repo: GitRepo) -> None:
    write_codeowners(git_repo.path, "src/ @acme/backend\nlib/ @bob\n")
    ownership = declared_ownership(
        {
            "src/main.py": (owner_entry("@alice"),),
            "lib/a.py": (owner_entry("@alice"),),
        }
    )
    git_repo.commit_paths("src/main.py", "lib/a.py")
    result = detect_drift(git_repo.path, ownership, drift_config())
    assert [entry.path for entry in result.changed] == ["lib/"]
    assert result.changed[0].owners == ("@bob",)
    assert result.notes == ()
    assert result.teams[0].status == "unverifiable"
    assert result.teams[0].team == "@acme/backend"
    gaps = evidence_gaps(result)
    assert len(gaps) == 1
    assert gaps[0].code == "team_membership"
    assert gaps[0].reason == TEAM_MEMBERSHIP_REASON


def test_team_rules_are_unverifiable_without_a_directory(git_repo: GitRepo) -> None:
    write_codeowners(git_repo.path, "src/ @org/backend-team\n")
    ownership = declared_ownership({"src/main.py": (owner_entry("@alice"),)})
    git_repo.commit_paths("src/main.py")
    result = detect_drift(git_repo.path, ownership, drift_config())
    assert result.changed == ()
    assert result.drift_detected is False
    assert result.teams[0].status == "unverifiable"
    assert result.teams[0].reason == "team_membership_requires_github_api"
    assert any(gap.code == "team_membership" for gap in evidence_gaps(result))


def test_drift_entry_suggests_resolved_team() -> None:
    entry = DriftEntry(
        path="src/",
        confidence_delta=0.4,
        reason="owners diverge",
        owners=("@alice",),
        observed_owners=("@bob",),
        observed_teams=("@org/backend", "@org/other"),
        drift_type="owner_mismatch",
        qualified_owner_count=1,
    )
    payload = drift_entry_payload(entry, 3)
    assert payload["recommendation"]["suggested_team"] == "@org/backend"
    assert payload["qualified_owner_count"] == 1


def test_manual_team_edges_and_severity_weights(git_repo: GitRepo) -> None:
    write_codeowners(
        git_repo.path,
        "# checkowners:manual:start\n"
        "/pay/ @org/pay @alice\n"
        "/at/ @org/at\n"
        "# checkowners:manual:end\n",
    )
    former = OwnerEntry(
        handle="@alice",
        ownership_score=0.4,
        last_commit=NOW,
        commits=1,
        contributor_status="former",
    )
    ownership = OwnershipMap(
        paths={
            "pay/a.py": PathOwnership(owners=(owner_entry("@dave"),), qualified_owner_count=1),
            "at/a.py": PathOwnership(owners=(former,), qualified_owner_count=2),
            "at/b.py": PathOwnership(owners=(owner_entry("@alice", 0.9),), qualified_owner_count=1),
            "at/c.py": PathOwnership(owners=(owner_entry("@alice", 0.2),), qualified_owner_count=1),
        },
        last_analyzed=NOW,
    )
    directory = _directory(
        TeamRecord(slug="pay", members=frozenset({"bob"}), parent="", children=(), visible=True),
        TeamRecord(slug="at", members=frozenset({"@alice"}), parent="", children=(), visible=True),
    )
    git_repo.commit_paths("pay/a.py", "at/a.py", "at/b.py", "at/c.py")
    result = detect_drift(git_repo.path, ownership, drift_config(), teams=directory)
    assert result.changed == ()
    at = next(entry for entry in result.reviews if entry.path == "/at/")
    assert at.confidence_delta == 0.0
    assert at.former_owners == ("@alice",)
    pay = next(row for row in result.teams if row.team == "@org/pay")
    assert pay.observed_outside_team == ("@dave",)
    assert (
        format_team_line(
            TeamComparison(team="@org/empty", pattern="*", line_number=1, status="empty")
        )
        == "@org/empty empty"
    )
    assert "outside @dave" in format_team_line(pay)
    assert "team_membership_requires_github_api" in format_team_line(
        TeamComparison(
            team="@org/x",
            pattern="*",
            line_number=2,
            status="unverifiable",
            reason="team_membership_requires_github_api",
        )
    )
    assert team_comparison_payload(pay)["status"] == "partial-drift"
    scored = compute_severity(
        DriftResult(
            stale=(),
            missing=(),
            changed=(
                DriftEntry(path="quiet", confidence_delta=0.2),
                DriftEntry(
                    path="src/a.py",
                    confidence_delta=0.2,
                    owners=("@a",),
                    observed_owners=("@b",),
                    team_coverage=0.0,
                ),
            ),
            drift_detected=True,
        ),
        Config(criticality=(("src/", 2.0), ("docs/", 0.2))),
    )
    assert scored == "high"


def test_manual_rule_shadow_is_a_review_note(git_repo: GitRepo) -> None:
    content = (
        "# checkowners:generated:start\n"
        "/payments/** @payments\n"
        "# checkowners:generated:end\n"
        "\n"
        "# checkowners:manual:start\n"
        "/payments/** @security-team\n"
        "# checkowners:manual:end\n"
    )
    write_codeowners(git_repo.path, content)
    ownership = declared_ownership({"payments/pay.py": (owner_entry("@payments", 0.91),)})
    git_repo.commit_paths("payments/pay.py")
    result = detect_drift(git_repo.path, ownership, drift_config())
    rules = rules_with_source(content)
    winner = match_path(rules, "payments/pay.py")
    assert winner is not None
    assert winner.source == "manual"
    assert winner.owners == ("@security-team",)
    assert result.changed == ()
    assert result.drift_detected is False
    note = (
        "declared ownership without observed expertise: "
        "/payments/** @security-team wins over /payments/** @payments (line 6). "
        "Review this manual rule."
    )
    assert result.notes == (note,)
    plan = format_fix_plan(result, ownership)
    assert plan == f"{note}\n"
    assert "-/payments/**" not in plan
    edged = format_fix_plan(
        DriftResult(
            stale=(),
            missing=(),
            changed=(
                DriftEntry(
                    path="*",
                    confidence_delta=1.0,
                    owners=("@old", "@older"),
                    observed_owners=("@ghost", "@spirit"),
                ),
                DriftEntry(
                    path="/src/**",
                    confidence_delta=0.5,
                    owners=("@platform",),
                    observed_owners=("@platform", "@ghost"),
                ),
                DriftEntry(
                    path="docs/",
                    confidence_delta=1.0,
                    owners=(),
                    observed_owners=("@ghost",),
                ),
            ),
            drift_detected=True,
        ),
        ownership,
    )
    assert edged == (
        "-* @old @older\n"
        "+* @ghost @spirit\n"
        "\n"
        "Consider adding @ghost while retaining @platform. "
        "@ghost has 0.00 observed expertise and is not currently represented.\n"
        "\n"
        "-docs/\n"
        "+docs/ @ghost\n"
    )


def test_manual_rule_without_generated_shadow_is_still_a_review(git_repo: GitRepo) -> None:
    write_codeowners(
        git_repo.path,
        "# checkowners:manual:start\n/src/** @alice @bob\n# checkowners:manual:end\n",
    )
    warning = DecayWarning(
        handle="@alice",
        path="src/main.py",
        last_commit=NOW,
        days_since_last_commit=400,
        historical_confidence=0.8,
    )
    ownership = OwnershipMap(
        paths={
            "src/main.py": PathOwnership(
                owners=(owner_entry("@alice", 0.4), owner_entry("@carol", 0.4)),
                qualified_owner_count=2,
                decay_warnings=(warning,),
            )
        },
        last_analyzed=NOW,
    )
    git_repo.commit_paths("src/main.py")
    result = detect_drift(git_repo.path, ownership, drift_config())
    assert result.changed == ()
    review = result.reviews[0]
    assert review.path == "/src/**"
    assert review.owners == ("@alice", "@bob")
    assert review.observed_owners == ("@alice", "@carol")
    assert review.decay is True
    assert review.line_number == 2
    assert review.qualified_owner_count == 2
    assert review.drift_type == "stale_declared_owner"
    assert result.notes == (
        (
            "declared ownership without observed expertise: "
            "/src/** @alice @bob (line 2). Review this manual rule."
        ),
    )


def test_a_manual_rule_does_not_hide_a_later_generated_mismatch(git_repo: GitRepo) -> None:
    write_codeowners(
        git_repo.path,
        "# checkowners:generated:start\n"
        "lib/ @bob\n"
        "# checkowners:generated:end\n"
        "# checkowners:manual:start\n"
        "docs/ @security\n"
        "# checkowners:manual:end\n",
    )
    ownership = declared_ownership(
        {
            "docs/a.py": (owner_entry("@alice"),),
            "lib/a.py": (owner_entry("@alice"),),
        }
    )
    git_repo.commit_paths("docs/a.py", "lib/a.py")
    result = detect_drift(git_repo.path, ownership, drift_config())
    assert [entry.path for entry in result.changed] == ["lib/"]
    assert result.reviews[0].path == "docs/"
