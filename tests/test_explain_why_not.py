"""The explain command's why-not answer for a person who was not inferred."""

from __future__ import annotations

from pathlib import Path

from checkowners.config import (
    AnalysisConfig,
    Config,
)
from checkowners.explain import (
    ExplainedOwner,
    _exclusion_reasons,
    _outsider_blame,
    _review_available,
    explain_why_not,
)
from tests.conftest import NOW
from tests.test_explain import _entry, _explained, _map, _single


def test_explain_why_not_already_inferred() -> None:
    alice = _explained("@alice", 0.86)
    result = explain_why_not(
        "alice",
        _single("src/a.py", (alice.entry,), (alice.entry,)),
        "src/a.py",
        Config(),
        Path(),
        as_of=NOW,
        inferred=(alice,),
        candidates=(alice,),
    )
    assert result.kind == "below_threshold"
    assert result.knobs == ()
    assert "is an inferred owner of src/a.py" in result.reasons[0]


def test_explain_why_not_below_threshold() -> None:
    alice = _explained("@alice", 0.86)
    carol = _explained("@carol", 0.12, commits=1, recency=0.05, frequency=0.1, blame=0.03)
    result = explain_why_not(
        "@carol",
        _single("src/a.py", (alice.entry,), (alice.entry, carol.entry)),
        "src/a.py",
        Config(),
        Path(),
        as_of=NOW,
        inferred=(alice,),
        candidates=(alice, carol),
    )
    assert result.kind == "below_threshold"
    assert result.ownership_score == 0.12
    assert any("0.12 < threshold 0.30" in reason for reason in result.reasons)


def test_explain_why_not_outside_top_n() -> None:
    config = Config(analysis=AnalysisConfig(top_n_owners=2))
    inferred = (_explained("@alice", 0.90), _explained("@bob", 0.80))
    dave = _explained("@dave", 0.40)
    result = explain_why_not(
        "@dave",
        _single("src/a.py", tuple(item.entry for item in inferred), (dave.entry,)),
        "src/a.py",
        config,
        Path(),
        as_of=NOW,
        inferred=inferred,
        candidates=(*inferred, dave),
    )
    assert result.kind == "outside_top_n"
    assert any("top_n_owners (2)" in reason for reason in result.reasons)
    assert any("top_n_owners is 2" in knob for knob in result.knobs)
    assert any(reason == "confidence: 0.40" for reason in result.reasons)


def test_explain_why_not_above_threshold_but_not_inferred() -> None:
    alice = _explained("@alice", 0.86)
    carol = _explained("@carol", 0.40)
    result = explain_why_not(
        "@carol",
        _single("src/a.py", (alice.entry,), (alice.entry, carol.entry)),
        "src/a.py",
        Config(),
        Path(),
        as_of=NOW,
        inferred=(alice,),
        candidates=(alice, carol),
    )
    assert result.kind == "below_threshold"
    assert result.ownership_score == 0.40


def test_review_available_and_outsider_blame() -> None:
    none = _single("src/a.py", (_entry("@alice", 0.8),), (_entry("@alice", 0.8),))
    reviewed = _single("src/a.py", (_entry("@alice", 0.8, review=0.4),))
    candidate_only = _single(
        "src/a.py",
        (_entry("@alice", 0.8),),
        (_entry("@bob", 0.2, review=0.1),),
    )
    assert _review_available(_map({})) is False
    assert _review_available(none) is False
    assert _review_available(reviewed) is True
    assert _review_available(candidate_only) is True
    assert _outsider_blame("x", {}) == (None, 0.0, False)
    shares = {"a": {"eve@example.com": 0.2}, "b": {"eve@example.com": 0.5}}
    assert _outsider_blame("eve", shares) == ("eve@example.com", 0.5, True)
    assert _outsider_blame("nobody", {"a": {"eve@example.com": 0.2}}) == (None, 0.0, True)


def test_exclusion_reasons_without_signal_rows() -> None:
    item = ExplainedOwner(
        entry=_entry("@carol", 0.12, last_commit=None),
        signals=(),
        source_path="x",
    )
    reasons = _exclusion_reasons(item, Config(), NOW, "below_threshold")
    assert reasons[0] == "last contribution: unknown"
    assert reasons[1] == "blame unavailable"
    assert reasons[2] == "no reviews in observation window"
