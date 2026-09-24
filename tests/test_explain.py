"""Tests for checkowners.explain."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from checkowners.analyze import _BlamePass
from checkowners.explain import (
    ExplainedOwner,
    PathExplanation,
    SignalEvidence,
    _assess,
    _blame_detail,
    _days_ago,
    _exclusion_reasons,
    _explained_payload,
    _fallback_quality,
    _lookup_shas,
    _outsider_blame,
    _parse_sha_log,
    _review_available,
    _review_detail,
    _sha_detail,
    _short_sha,
    _signals_for,
    _target_kind,
    _team_resolution,
    _threshold_reason,
    blame_shares,
    build_explanation,
    commit_shas,
    explain_why_not,
    explanation_payload,
    identities_match,
    last_contribution,
    matching_files,
    owners_payload,
    path_knobs,
    ranked_owners,
    rename_lineage,
    signal_tuples,
)
from checkowners.models import (
    COMMAND_SCHEMA_VERSION,
    OWNERSHIP_MODEL_VERSION,
    AnalysisConfig,
    ConfidenceScore,
    Config,
    GitConfig,
    GithubConfig,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
    ScoringConfig,
    SignalScore,
    models_payload,
)

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)
_ISO = "2026-05-28T12:00:00+00:00"


def _proc(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


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


def test_identities_match_variants() -> None:
    assert identities_match("@alice", "alice")
    assert identities_match("alice@example.com", "alice")
    assert identities_match("alice", "alice@example.com")
    assert not identities_match("", "@alice")
    assert not identities_match("@", "@")
    assert not identities_match("@alice", "@bob")


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


def test_build_explanation_filters_owner_and_collects_evidence(tmp_path: Path) -> None:
    alice = _entry("@alice", 0.86)
    bob = _entry("@bob", 0.54)
    ownership = _single("src/a.py", (alice, bob), (alice, bob))
    with (
        patch(
            "checkowners.explain.commit_shas",
            return_value={"alice@example.com": ("deadbeefabcd",)},
        ),
        patch("checkowners.explain.rename_lineage", return_value=("src/old.py",)),
    ):
        explanation = build_explanation(
            ownership,
            "src/a.py",
            Config(),
            tmp_path,
            as_of=_NOW,
            owner="@alice",
        )
    assert explanation.kind == "file"
    assert [item.entry.handle for item in explanation.inferred] == ["@alice"]
    assert explanation.lineage == ("src/old.py",)
    recency = next(s for s in explanation.inferred[0].signals if s.name == "recency")
    assert recency.commits == ("deadbeefabcd",)
    assert recency.detail == "deadbeefabcd"


def test_build_explanation_directory_kind_and_empty_inferred(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    carol = _entry("@carol", 0.12, quality=0.41)
    ownership = _single("pkg/a.py", (), (carol,))
    with (
        patch("checkowners.explain.commit_shas", return_value={}),
        patch("checkowners.explain.rename_lineage", return_value=("ignored.py",)),
    ):
        empty = build_explanation(_map({}), "pkg", Config(), tmp_path, as_of=_NOW)
        fallback = build_explanation(ownership, "pkg/", Config(), tmp_path, as_of=_NOW)
    assert empty.kind == "directory"
    assert empty.evidence_quality == 0.0
    assert empty.inferred == ()
    assert empty.lineage == ()
    assert fallback.kind == "directory"
    assert fallback.evidence_quality == 0.41
    assert fallback.lineage == ()


def test_build_explanation_why_not_and_team_assessment(tmp_path: Path) -> None:
    alice = _entry("@alice", 0.86)
    ownership = _single("src/a.py", (alice,), (alice,))
    with (
        patch("checkowners.explain.commit_shas", return_value={}),
        patch("checkowners.explain.rename_lineage", return_value=()),
        patch("checkowners.explain.last_contribution", return_value=None),
        patch("checkowners.explain.blame_shares", return_value={}),
    ):
        explanation = build_explanation(
            ownership,
            "src/a.py",
            Config(),
            tmp_path,
            as_of=_NOW,
            declared=("@org/platform",),
            team_members={"platform": frozenset({"@alice"})},
            why_not="@dave",
        )
    assert explanation.assessment == "aligned"
    assert explanation.team_resolution == ("@alice ∈ @platform",)
    assert explanation.why_not is not None
    assert explanation.why_not.kind == "never_contributed"
    assert explanation.knobs == explanation.why_not.knobs


def test_explain_why_not_already_inferred() -> None:
    alice = _explained("@alice", 0.86)
    result = explain_why_not(
        "alice",
        _single("src/a.py", (alice.entry,), (alice.entry,)),
        "src/a.py",
        Config(),
        Path(),
        as_of=_NOW,
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
        as_of=_NOW,
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
        as_of=_NOW,
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
        as_of=_NOW,
        inferred=(alice,),
        candidates=(alice, carol),
    )
    assert result.kind == "below_threshold"
    assert result.ownership_score == 0.40


def test_explain_why_not_outsider_last_commit() -> None:
    alice = _entry("@alice", 0.86, review=0.4)
    when = _NOW - timedelta(days=40)
    with (
        patch("checkowners.explain.last_contribution", return_value=("eve@example.com", when)),
        patch(
            "checkowners.explain.blame_shares",
            return_value={"src/a.py": {"eve@example.com": 0.08}},
        ),
    ):
        result = explain_why_not(
            "eve",
            _single("src/a.py", (alice,), (alice,)),
            "src/a.py",
            Config(),
            Path(),
            as_of=_NOW,
            inferred=(_explained("@alice", 0.86),),
            candidates=(_explained("@alice", 0.86),),
        )
    assert result.kind == "outside_window"
    assert result.handle == "eve@example.com"
    assert any("40 days ago" in reason for reason in result.reasons)
    assert any("lookback_days" in knob for knob in result.knobs)


def test_explain_why_not_outsider_blame_only() -> None:
    with (
        patch("checkowners.explain.last_contribution", return_value=None),
        patch(
            "checkowners.explain.blame_shares",
            return_value={"src/a.py": {"eve@example.com": 0.55}},
        ),
    ):
        result = explain_why_not(
            "eve",
            _map({}),
            "src/a.py",
            Config(),
            Path(),
            as_of=_NOW,
            inferred=(),
            candidates=(),
        )
    assert result.kind == "outside_window"
    assert result.handle == "eve@example.com"
    assert "no commits in the observation window" in result.reasons


def test_explain_why_not_outsider_without_score_breakdown() -> None:
    bare = OwnerEntry(handle="eve@example.com", ownership_score=0.11, last_commit=_NOW, commits=0)
    with (
        patch(
            "checkowners.explain.last_contribution",
            return_value=("eve@example.com", _NOW - timedelta(days=1)),
        ),
        patch(
            "checkowners.explain.blame_shares",
            return_value={"src/a.py": {"eve@example.com": 0.2}},
        ),
        patch("checkowners.explain.score_owners", return_value=[bare]),
    ):
        result = explain_why_not(
            "eve",
            _map({}),
            "src/a.py",
            Config(),
            Path(),
            as_of=_NOW,
            inferred=(),
            candidates=(),
        )
    assert result.kind == "outside_window"
    assert "blame unavailable" in result.reasons
    assert any("1 day ago" in reason for reason in result.reasons)


def test_commit_shas_parses_and_respects_mailmap() -> None:
    stdout = "\n".join(
        (
            "aaaaaaaaaaaaxxxx",
            "alice@example.com",
            _ISO,
            "bbbbbbbbbbbbxxxx",
            "alice@example.com",
            "2026-05-27T12:00:00+00:00",
        )
    )
    with patch("checkowners.explain.subprocess.run", return_value=_proc(stdout)) as mocked:
        shas = commit_shas(Path(), "src/a.py", Config(), _NOW)
    assert shas["alice@example.com"] == ("aaaaaaaaaaaa", "bbbbbbbbbbbb")
    argv = mocked.call_args.args[0]
    assert "--use-mailmap" in argv
    assert any("%aE" in part for part in argv)

    config = Config(git=GitConfig(use_mailmap=False))
    with patch("checkowners.explain.subprocess.run", return_value=_proc("")) as mocked:
        assert commit_shas(Path(), "src/a.py", config, _NOW) == {}
    argv = mocked.call_args.args[0]
    assert "--no-use-mailmap" in argv
    assert any("%ae" in part for part in argv)


def test_rename_lineage_keeps_unseen_old_names() -> None:
    stdout = "\n".join(
        (
            "M\tsrc/a.py",
            "R100\told/a.py\tsrc/a.py",
            "R",
            "R050\told/a.py",
            "R080\tfirst/a.py\told/a.py",
            "R090\told/a.py\tsrc/a.py",
        )
    )
    with patch("checkowners.explain.subprocess.run", return_value=_proc(stdout)):
        assert rename_lineage(Path(), "src/a.py") == ("old/a.py", "first/a.py")


def test_last_contribution_matches_identity() -> None:
    stdout = "\n".join(
        ("ccccccccccccxxxx", "eve@example.com", _ISO, "ddddddddddddxxxx", "bob", _ISO)
    )
    with patch("checkowners.explain.subprocess.run", return_value=_proc(stdout)) as mocked:
        found = last_contribution(Path(), "src/a.py", "eve", use_mailmap=True)
        missing = last_contribution(Path(), "src/a.py", "carol", use_mailmap=False)
    assert found == ("eve@example.com", datetime.fromisoformat(_ISO))
    assert missing is None
    first_argv = mocked.call_args_list[0].args[0]
    second_argv = mocked.call_args_list[1].args[0]
    assert "--use-mailmap" in first_argv
    assert "--no-use-mailmap" in second_argv


def test_blame_shares_empty_and_delegated() -> None:
    assert blame_shares(Path(), (), Config()) == {}
    coverage = {"src/a.py": {"alice@example.com": 0.6}}
    with patch(
        "checkowners.explain.gather_blame_coverage",
        return_value=_BlamePass(coverage=coverage),
    ) as mocked:
        assert blame_shares(Path(), ("src/a.py",), Config()) == coverage
    mocked.assert_called_once()


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
    listed = owners_payload((alice.entry,), "src/a.py", ownership)
    assert listed["schema_version"] == COMMAND_SCHEMA_VERSION
    assert listed["models"]["ownership"] == OWNERSHIP_MODEL_VERSION
    assert listed["models"] == models_payload()
    listed_owner = listed["owners"][0]
    assert listed_owner["identity"] == "@alice"
    assert listed_owner["ownership_score"] == 0.86
    assert listed_owner["last_commit"] is None
    assert "review" not in listed_owner["signals"]

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
    first = owners[0]
    assert isinstance(first, dict)
    assert first["last_commit"] is None
    assert payload["why_not"]["kind"] == "below_threshold"
    review = first["signals"]["review"]
    assert review["available"] is False
    assert "score" not in review


def test_signal_tuples_with_and_without_breakdown() -> None:
    bare = _entry("@alice", 0.5, include_breakdown=False)
    assert signal_tuples(bare) == {
        "recency": (0.0, True),
        "frequency": (0.0, True),
        "blame": (0.0, False),
        "review": (0.0, False),
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


def test_exclusion_reasons_without_signal_rows() -> None:
    item = ExplainedOwner(
        entry=_entry("@carol", 0.12, last_commit=None),
        signals=(),
        source_path="x",
    )
    reasons = _exclusion_reasons(item, Config(), _NOW, "below_threshold")
    assert reasons[0] == "last contribution: unknown"
    assert reasons[1] == "blame unavailable"
    assert reasons[2] == "no reviews in observation window"


def test_team_resolution_and_assessment_branches() -> None:
    alice = _explained("@alice", 0.8)
    bob = _explained("@bob", 0.7)
    assert _team_resolution((), ("@org/platform",), {}) == ()
    assert _team_resolution((alice,), (), {"x": frozenset({"@alice"})}) == ()
    assert _team_resolution((alice,), ("@t",), None) == ()
    assert _team_resolution(
        (alice,),
        ("@t",),
        {"org/platform": frozenset({"@alice"}), "other": frozenset({"@alice"})},
    ) == ("@alice ∈ @org/platform",)
    assert _team_resolution((alice,), ("@t",), {"@org/platform": frozenset({"@alice"})}) == (
        "@alice ∈ @org/platform",
    )

    assert _assess((alice,), (), None) == "unverifiable"
    assert _assess((alice,), ("@alice",), None) == "aligned"
    assert _assess((alice,), ("@org/platform",), None) == "unverifiable"
    assert (
        _assess((alice,), ("@org/platform",), {"@org/platform": frozenset({"@alice"})}) == "aligned"
    )
    assert (
        _assess((alice, bob), ("@org/platform",), {"@org/platform": frozenset({"@alice"})})
        == "aligned"
    )
    assert _assess((alice,), ("@org/platform",), {"@org/platform": frozenset({"@carol"})}) == (
        "diverged"
    )
    assert _assess((alice,), ("@carol",), None) == "diverged"
    acme = {"@acme/platform": frozenset({"@alice"})}
    other = {"@other/docs": frozenset({"@alice"})}
    assert _assess((alice,), ("@org/platform",), acme) == "aligned"
    assert _assess((alice,), ("@org/platform",), other) == "diverged"


def test_target_kind_and_quality_helpers(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    assert _target_kind(tmp_path, "src/", ()) == "directory"
    assert _target_kind(tmp_path, "src", ("a.py", "b.py")) == "directory"
    assert _target_kind(tmp_path, "pkg", ()) == "directory"
    assert _target_kind(tmp_path, "missing.py", ()) == "file"
    assert _fallback_quality(()) == 0.0
    assert (
        _fallback_quality(
            (_explained("@carol", 0.1, quality=0.2), _explained("@dave", 0.1, quality=0.6))
        )
        == 0.6
    )


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


def test_lookup_and_parse_sha_log() -> None:
    assert _lookup_shas("@alice", {"@alice": ("aaa",)}) == ("aaa",)
    assert _lookup_shas("@alice", {"alice@example.com": ("bbb",)}) == ("bbb",)
    assert _lookup_shas("@alice", {"@bob": ("ccc",)}) == ()
    stdout = "\n".join(
        (
            "111111111111xxxx",
            "alice",
            _ISO,
            "111111111111xxxx",
            "alice",
            _ISO,
            "222222222222xxxx",
            "alice",
            _ISO,
            "333333333333xxxx",
            "alice",
            _ISO,
            "444444444444xxxx",
            "alice",
            _ISO,
            "555555555555xxxx",
            "alice",
            _ISO,
            "666666666666xxxx",
            "alice",
            _ISO,
            "not-a-sha",
            "bob",
            "not-a-date",
            "777777777777xxxx",
            "bob",
            _ISO,
        )
    )
    parsed = _parse_sha_log(stdout)
    assert parsed["alice"] == (
        "111111111111",
        "222222222222",
        "333333333333",
        "444444444444",
        "555555555555",
    )
    assert parsed["bob"] == ("777777777777",)
    assert _short_sha("abc") == "abc"
    assert _short_sha("abcdefghijklm") == "abcdefghijkl"


def test_detail_and_day_helpers() -> None:
    assert _blame_detail(SignalScore(available=False)) == "blame unavailable"
    assert _blame_detail(SignalScore(available=True, score=0.68)) == "68% current blame"
    assert _review_detail(SignalScore(available=False)) == "reviews unavailable"
    assert (
        _review_detail(SignalScore(available=True, score=0.0)) == "no reviews in observation window"
    )
    assert _review_detail(SignalScore(available=True, score=0.74)) == "review share 0.74"
    assert _sha_detail(()) == "no commit in window"
    assert _sha_detail(("abcd",)) == "abcd"
    assert _days_ago(None, _NOW) == "unknown"
    assert _days_ago(_NOW, _NOW) == "today"
    assert _days_ago(_NOW - timedelta(days=1), _NOW) == "1 day ago"
    assert _days_ago(_NOW - timedelta(days=9), _NOW) == "9 days ago"
    assert _threshold_reason(0.12, 0.3) == "confidence: 0.12 < threshold 0.30"
    assert _threshold_reason(0.40, 0.3) == "confidence: 0.40"


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
