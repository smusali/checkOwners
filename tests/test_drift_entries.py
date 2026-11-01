"""What one drift entry reports: the delta, the payload, and the fix plan."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from checkowners.config import (
    BusFactorConfig,
    Config,
)
from checkowners.drift import (
    detect_drift,
    drift_entry_payload,
    evidence_gaps,
    format_fix_plan,
    owner_overlap,
    reconciliation_body,
    write_github_output,
)
from checkowners.github import clear_api_evidence, note_api_call
from checkowners.models import (
    DriftEntry,
    DriftResult,
    OwnershipMap,
)
from checkowners.payloads import (
    AnalysisGapJson,
)
from tests.conftest import (
    NOW,
    GitRepo,
    declared_ownership,
    drift_config,
    owner_entry,
    write_codeowners,
)


def test_owner_overlap_rounds_the_jaccard_ratio() -> None:
    assert owner_overlap((), ()) == 0.0
    assert owner_overlap(("@A",), ("@a", "@b", "@c")) == 0.3333
    assert owner_overlap(("@a", "@b"), ("@b",)) == 0.5


def test_disjoint_owners_are_a_complete_replacement(git_repo: GitRepo) -> None:
    write_codeowners(git_repo.path, "src/ @alice\n")
    ownership = declared_ownership({"src/main.py": (owner_entry("@bob", 0.4),)})
    git_repo.commit_paths("src/main.py")
    result = detect_drift(git_repo.path, ownership, drift_config())
    assert result.changed[0].drift_type == "complete_ownership_replacement"
    assert result.changed[0].confidence_delta == 1.0


def test_added_owner_keeps_its_confidence_delta(git_repo: GitRepo) -> None:
    write_codeowners(git_repo.path, "src/ @alice\n")
    ownership = declared_ownership(
        {"src/main.py": (owner_entry("@alice", 0.4), owner_entry("@bob", 0.5))}
    )
    git_repo.commit_paths("src/main.py")
    result = detect_drift(git_repo.path, ownership, drift_config(min_delta=0.5))
    assert len(result.changed) == 1
    assert result.changed[0].confidence_delta == 0.5
    assert result.changed[0].drift_type == "owner_mismatch"


def test_two_diverging_files_count_both(git_repo: GitRepo) -> None:
    write_codeowners(git_repo.path, "src/ @alice\n")
    ownership = declared_ownership(
        {
            "src/a.py": (owner_entry("@alice", 0.3), owner_entry("@bob", 0.2)),
            "src/b.py": (owner_entry("@alice", 0.3), owner_entry("@carol", 0.6)),
        }
    )
    git_repo.commit_paths("src/a.py", "src/b.py")
    result = detect_drift(git_repo.path, ownership, drift_config())
    assert result.changed[0].reason == "owners diverge on 2 of 2 covered path(s) (line 1)"
    assert result.changed[0].qualified_owner_count == 2
    assert result.changed[0].observed_owners == ("@alice", "@carol")


def test_a_matching_file_does_not_hide_a_later_mismatch(git_repo: GitRepo) -> None:
    write_codeowners(git_repo.path, "src/ @alice\n")
    ownership = declared_ownership(
        {
            "src/ok.py": (owner_entry("@alice"),),
            "src/bad.py": (owner_entry("@bob", 0.4),),
        }
    )
    git_repo.commit_paths("src/ok.py", "src/bad.py")
    result = detect_drift(git_repo.path, ownership, drift_config())
    assert result.changed[0].observed_owners == ("@bob",)


def test_equal_deltas_keep_the_first_file(git_repo: GitRepo) -> None:
    write_codeowners(git_repo.path, "src/ @alice\n")
    ownership = declared_ownership(
        {
            "src/a.py": (owner_entry("@alice", 0.3), owner_entry("@bob", 0.4)),
            "src/b.py": (owner_entry("@alice", 0.3), owner_entry("@carol", 0.4)),
        }
    )
    git_repo.commit_paths("src/a.py", "src/b.py")
    result = detect_drift(git_repo.path, ownership, drift_config())
    assert result.changed[0].observed_owners == ("@alice", "@bob")


def test_zero_confidence_addition_keeps_the_initial_measurement(git_repo: GitRepo) -> None:
    write_codeowners(git_repo.path, "src/ @alice\n")
    ownership = declared_ownership(
        {"src/main.py": (owner_entry("@alice", 0.5), owner_entry("@zero", 0.0))}
    )
    git_repo.commit_paths("src/main.py")
    result = detect_drift(git_repo.path, ownership, drift_config())
    entry = result.changed[0]
    assert entry.decay is False
    assert entry.observed_owners == ()
    assert entry.qualified_owner_count is None


def test_an_agreeing_rule_does_not_hide_a_later_mismatch(git_repo: GitRepo) -> None:
    write_codeowners(git_repo.path, "docs/ @alice\nsrc/ @bob\n")
    ownership = declared_ownership(
        {
            "docs/a.py": (owner_entry("@alice"),),
            "src/a.py": (owner_entry("@carol"),),
        }
    )
    git_repo.commit_paths("docs/a.py", "src/a.py")
    result = detect_drift(git_repo.path, ownership, drift_config())
    assert [entry.path for entry in result.changed] == ["src/"]
    assert result.changed[0].owners == ("@bob",)


def test_unowned_inference_has_no_confidence_delta(git_repo: GitRepo) -> None:
    ownership = declared_ownership({"src/empty.py": ()})
    git_repo.commit_paths("src/empty.py")
    result = detect_drift(git_repo.path, ownership, drift_config())
    assert result.missing[0].confidence_delta == 0.0
    assert result.notes == ()


def test_email_inference_without_rules_is_not_an_identity_gap(git_repo: GitRepo) -> None:
    ownership = declared_ownership({"src/main.py": (owner_entry("ada@example.com"),)})
    git_repo.commit_paths("src/main.py")
    result = detect_drift(git_repo.path, ownership, drift_config())
    assert result.notes == ()
    assert evidence_gaps(result) == ()


def test_fix_plan_names_a_retained_owner_score() -> None:
    entry = DriftEntry(
        path="src/a.py",
        confidence_delta=0.5,
        owners=("@alice", "@carol"),
        observed_owners=("@alice", "@carol", "@bob"),
    )
    plan = format_fix_plan(
        DriftResult(stale=(), missing=(), changed=(entry,), drift_detected=True),
        declared_ownership({"src/a.py": (owner_entry("@bob", 0.42),)}),
    )
    assert plan == (
        "Consider adding @bob while retaining @alice @carol. "
        "@bob has 0.42 observed expertise and is not currently represented.\n"
    )


def test_drift_entry_payload_uses_stable_keys() -> None:
    entry = DriftEntry(
        path="src/",
        confidence_delta=1 / 3,
        reason="owners diverge",
        qualified_owner_count=2,
        owners=("@alice", "@bob"),
        observed_owners=("@carol",),
        observed_teams=("@acme/web",),
        drift_type="owner_mismatch",
    )
    payload = drift_entry_payload(
        entry,
        3,
        Config(bus_factor=BusFactorConfig(critical_threshold=2)),
    )
    assert payload == {
        "path": "src/",
        "declared": {"owners": ["@alice", "@bob"]},
        "observed": {"owners": ["@carol"], "teams": ["@acme/web"]},
        "drift": {
            "severity": "medium",
            "type": "owner_mismatch",
            "declared_observed_overlap": 0.0,
        },
        "recommendation": {
            "action": "review_codeowners_rule",
            "suggested_team": "@acme/web",
        },
        "confidence_delta": round(1 / 3, 4),
        "reason": "owners diverge",
        "qualified_owner_count": 2,
        "qualified_owner_count_cap": 3,
    }


def test_github_output_writes_the_sorted_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_api_evidence()
    output_file = tmp_path / "gh_output.txt"
    result = DriftResult(
        stale=(
            DriftEntry(
                path="/old/",
                confidence_delta=0.2,
                reason="gone",
                owners=("@alice",),
                qualified_owner_count=2,
                drift_type="stale_rule",
            ),
        ),
        missing=(
            DriftEntry(
                path="src/a.py",
                confidence_delta=0.4,
                reason="absent",
                observed_owners=("@bob",),
                observed_teams=("@acme/backend",),
                qualified_owner_count=2,
                drift_type="missing_observed_expert",
            ),
        ),
        changed=(
            DriftEntry(
                path="src/",
                confidence_delta=0.6,
                reason="diverged",
                owners=("@carol",),
                observed_owners=("@dave",),
                qualified_owner_count=2,
                drift_type="owner_mismatch",
            ),
        ),
        drift_detected=True,
        notes=("watch the emails",),
    )
    config = Config(bus_factor=BusFactorConfig(critical_threshold=2))
    gaps: list[AnalysisGapJson] = [{"code": "team_membership", "reason": "because"}]
    try:
        monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
        monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widgets")
        monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
        note_api_call()
        write_github_output(
            result,
            3,
            analysis_ref="abc123",
            analysis_epoch="2020-01-02T00:00:00+00:00",
            config=config,
            analysis_completeness=0.25,
            analysis_gaps=gaps,
        )
        content = output_file.read_text(encoding="utf-8")
        payload = json.loads(content.removeprefix("drift_summary=").strip())
        assert content.startswith("drift_summary=")
        assert content.endswith("\n")
        assert list(payload) == sorted(payload)
        assert payload["drift_detected"] is True
        assert payload["max_confidence_delta"] == 0.6
        assert payload["notes"] == ["watch the emails"]
        assert payload["stale"][0]["path"] == "/old/"
        assert payload["stale"][0]["qualified_owner_count_cap"] == 3
        assert payload["stale"][0]["drift"]["severity"] == "medium"
        assert payload["changed"][0]["qualified_owner_count_cap"] == 3
        assert payload["changed"][0]["drift"]["severity"] == "high"
        assert payload["missing"][0]["path"] == "src/a.py"
        assert payload["missing"][0]["qualified_owner_count_cap"] == 3
        assert payload["missing"][0]["drift"]["severity"] == "medium"
        assert payload["changed"][0]["path"] == "src/"
        assert payload["head_sha"] == "abc123"
        assert payload["generated_at"] == "2020-01-02T00:00:00+00:00"
        assert payload["analysis_completeness"] == 0.25
        assert payload["repository"] == "acme/widgets"
        assert payload["analysis_gaps"] == gaps
        assert payload["repository_head"] == "abc123"
        assert (
            payload["github_evidence_collected_at"]
            == datetime.fromtimestamp(1700000000, tz=UTC).isoformat()
        )
        output_file.unlink()
        write_github_output(result, 3)
        omitted = json.loads(output_file.read_text(encoding="utf-8").removeprefix("drift_summary="))
        assert omitted["head_sha"] == ""
        assert omitted["generated_at"] == ""
    finally:
        clear_api_evidence()


def test_former_owners_appear_in_drift_payload_and_reconciliation() -> None:
    entry = DriftEntry(
        path="src/a.py",
        confidence_delta=0.4,
        reason="owners changed",
        owners=("@alice",),
        observed_owners=("@bob",),
        former_owners=("@carol",),
    )
    payload = drift_entry_payload(entry, 3, Config())
    assert payload["former_owners"] == ["@carol"]
    body = reconciliation_body(
        DriftResult(stale=(), missing=(), changed=(entry,), drift_detected=True),
        OwnershipMap(paths={}, last_analyzed=NOW),
        Config(),
    )
    assert "Former contributors: @carol" in body
