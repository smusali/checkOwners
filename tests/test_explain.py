"""Tests for checkowners.explain."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from checkowners.config import (
    Config,
    GithubConfig,
    ScoringConfig,
)
from checkowners.explain import (
    ExplainedOwner,
    PathExplanation,
    SignalEvidence,
    _explained_payload,
    _signals_for,
    explain_why_not,
    explanation_payload,
    matching_files,
    owners_payload,
    path_knobs,
    ranked_owners,
    signal_tuples,
)
from checkowners.models import (
    ConfidenceScore,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
    SignalScore,
)
from tests.conftest import Payload

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)
_ISO = "2026-05-28T12:00:00+00:00"


def _breakdown(
    score: float,
    *,
    recency: float = 0.9,
    frequency: float = 0.8,
    blame: float = 0.7,
    review: float | None = None,
) -> ConfidenceScore:
    return ConfidenceScore(
        total=score,
        recency=SignalScore(available=True, score=recency),
        frequency=SignalScore(available=True, score=frequency),
        blame=SignalScore(available=True, score=blame),
        review=SignalScore(available=review is not None, score=review or 0.0),
    )


def _entry(
    handle: str,
    score: float,
    *,
    commits: int = 4,
    last_commit: datetime | None = _NOW,
    quality: float = 0.85,
    include_breakdown: bool = True,
    recency: float = 0.9,
    frequency: float = 0.8,
    blame: float = 0.7,
    review: float | None = None,
) -> OwnerEntry:
    return OwnerEntry(
        handle=handle,
        ownership_score=score,
        last_commit=last_commit,
        commits=commits,
        evidence_quality=quality,
        score_breakdown=_breakdown(
            score, recency=recency, frequency=frequency, blame=blame, review=review
        )
        if include_breakdown
        else None,
    )


def _explained(
    handle: str,
    score: float,
    *,
    source_path: str = "src/a.py",
    signals: tuple[SignalEvidence, ...] | None = None,
    commits: int = 4,
    last_commit: datetime | None = _NOW,
    quality: float = 0.85,
    include_breakdown: bool = True,
    recency: float = 0.9,
    frequency: float = 0.8,
    blame: float = 0.7,
    review: float | None = None,
) -> ExplainedOwner:
    entry = _entry(
        handle,
        score,
        commits=commits,
        last_commit=last_commit,
        quality=quality,
        include_breakdown=include_breakdown,
        recency=recency,
        frequency=frequency,
        blame=blame,
        review=review,
    )
    return ExplainedOwner(
        entry=entry,
        signals=signals if signals is not None else _signals_for(entry, ScoringConfig(), ()),
        source_path=source_path,
    )


def _map(
    paths: dict[str, PathOwnership],
    *,
    ref: str = "deadbeef",
) -> OwnershipMap:
    return OwnershipMap(paths=paths, last_analyzed=_NOW, analysis_ref=ref)


def _single(
    path: str,
    owners: tuple[OwnerEntry, ...],
    candidates: tuple[OwnerEntry, ...] = (),
) -> OwnershipMap:
    return _map(
        {
            path: PathOwnership(
                owners=owners,
                qualified_owner_count=len(owners),
                candidates=candidates,
            )
        }
    )


def test_matching_files_and_ranked_owners_aggregate_directories() -> None:
    alice_main = _entry("@alice", 0.80, commits=2, last_commit=_NOW - timedelta(days=3))
    alice_util = _entry("@alice", 0.60, commits=5, last_commit=None)
    bob = _entry("@bob", 0.40, commits=1, last_commit=None)
    ownership = _map(
        {
            "src/main.py": PathOwnership(
                owners=(alice_main,),
                qualified_owner_count=1,
                candidates=(alice_main, bob),
            ),
            "src/util.py": PathOwnership(
                owners=(alice_util,),
                qualified_owner_count=1,
                candidates=(alice_util,),
            ),
            "docs/readme.md": PathOwnership(owners=(bob,), qualified_owner_count=1),
        }
    )
    assert matching_files(ownership, "src/") == ("src/main.py", "src/util.py")
    ranked = ranked_owners(ownership, "src/", Config())
    assert [item.handle for item in ranked] == ["@alice"]
    assert ranked[0].ownership_score == 0.80
    assert ranked[0].commits == 7
    assert ranked[0].last_commit == _NOW - timedelta(days=3)


def test_ranked_owners_uses_owners_when_candidates_missing() -> None:
    alice = _entry("@alice", 0.70, last_commit=None)
    ownership = _single("src/a.py", (alice,))
    ranked = ranked_owners(ownership, "src/a.py", Config())
    assert ranked[0].handle == "@alice"
    assert ranked[0].last_commit is None


def test_path_knobs_cover_threshold_top_n_and_review() -> None:
    inferred = (_explained("@alice", 0.86),)
    extra = (
        _explained("@alice", 0.86),
        _explained("@bob", 0.54),
        _explained("@carol", 0.12),
        _explained("@dave", 0.10),
    )
    knobs = path_knobs(Config(), inferred, extra)
    joined = " ".join(knobs)
    assert "confidence_threshold is 0.30" in joined
    assert "@carol scores 0.12" in joined
    assert "top_n_owners is 3" in joined
    assert "github.api_enabled is false" in joined
    assert "recency_half_life_days is 90" in joined

    enabled = Config(github=GithubConfig(api_enabled=True))
    skipped = path_knobs(enabled, inferred, extra)
    assert not any("github.api_enabled" in knob for knob in skipped)

    no_review_gap = path_knobs(Config(), inferred, inferred)
    assert not any("confidence_threshold" in knob for knob in no_review_gap)
    assert not any("top_n_owners" in knob for knob in no_review_gap)


def test_payloads_include_why_not_and_null_last_commit() -> None:
    alice = _explained("@alice", 0.86, last_commit=None)
    why = explain_why_not(
        "@alice",
        _single("src/a.py", (alice.entry,), (alice.entry,)),
        "src/a.py",
        Config(),
        Path(),
        as_of=_NOW,
        inferred=(alice,),
        candidates=(alice,),
    )
    ownership = _single("src/a.py", (alice.entry,), (alice.entry,))
    listed = Payload(owners_payload((alice.entry,), "src/a.py", ownership))
    listed_owner = listed.rows("owners")[0]
    assert listed_owner.text("identity") == "@alice"
    assert listed_owner.number("ownership_score") == 0.86
    assert listed_owner["last_commit"] is None
    assert "review" not in listed_owner.child("signals")
    low = _entry("@alice", 0.2)
    high = _entry("@alice", 0.9)
    bob = _entry("@bob", 0.4)
    merged = owners_payload(
        (high,),
        "src",
        _map(
            {
                "src/a.py": PathOwnership(
                    owners=(high,),
                    qualified_owner_count=1,
                    scored_owners=(high, bob),
                ),
                "src/b.py": PathOwnership(
                    owners=(high,),
                    qualified_owner_count=1,
                    scored_owners=(low,),
                ),
            }
        ),
    )
    assert Payload(merged).child("risk").number("top_owner_share") == round(0.9 / 1.3, 4)
    unmatched = Payload(owners_payload((bob,), "missing.py", ownership))
    assert unmatched.child("risk").number("top_owner_share") == 1.0

    explanation = PathExplanation(
        target="src/a.py",
        kind="file",
        files=("src/a.py",),
        inferred=(alice,),
        candidates=(alice,),
        evidence_quality=0.85,
        declared=(),
        team_resolution=(),
        assessment="unverifiable",
        lineage=(),
        knobs=(),
        weights={"recency": 0.35},
        why_not=why,
    )
    payload = explanation_payload(explanation)
    owners = payload["owners"]
    assert isinstance(owners, list)
    first = Payload(owners[0]) if isinstance(owners[0], dict) else None
    assert first is not None
    assert first["last_commit"] is None
    assert Payload(payload).child("why_not").text("kind") == "below_threshold"
    review = first.child("signals").child("review")
    assert review.flag("available") is False
    assert "score" not in review


def test_signal_tuples_with_and_without_breakdown() -> None:
    bare = _entry("@alice", 0.5, include_breakdown=False)
    assert signal_tuples(bare) == {
        "recency": (0.0, True),
        "frequency": (0.0, True),
        "blame": (0.0, False),
        "review": (0.0, False),
        "change_survival": (0.0, False),
    }
    scored = _entry("@alice", 0.8, review=0.2)
    assert signal_tuples(scored)["review"] == (0.2, True)
    assert signal_tuples(scored)["blame"] == (0.7, True)


def test_signals_for_without_breakdown_and_without_shas() -> None:
    signals = _signals_for(
        _entry("@alice", 0.5, include_breakdown=False, commits=2), ScoringConfig(), ()
    )
    by_name = {item.name: item for item in signals}
    assert by_name["recency"].detail == "no commit in window"
    assert by_name["frequency"].detail == "2 commits"
    assert by_name["blame"].detail == "blame unavailable"
    assert by_name["review"].detail == "reviews unavailable"


def test_explained_payload_omits_unavailable_score() -> None:
    item = _explained("@alice", 0.5, last_commit=None, review=None)
    payload = _explained_payload(item)
    assert payload["last_commit"] is None
    signals = payload["signals"]
    assert isinstance(signals, dict)
    review = signals["review"]
    assert isinstance(review, dict)
    assert review["available"] is False
    assert "score" not in review
