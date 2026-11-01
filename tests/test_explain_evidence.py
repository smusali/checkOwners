"""Reading one path's git evidence: commits, blame shares, and rename lineage."""

from __future__ import annotations

import subprocess
from pathlib import Path

from checkowners.analyze import (
    analyze_ownership,
)
from checkowners.config import (
    AnalysisConfig,
    Config,
)
from checkowners.explain import (
    _lookup_shas,
    _parse_sha_log,
    _short_sha,
    build_explanation,
    explanation_payload,
    identities_match,
)
from checkowners.history import (
    BODY_END,
    BODY_START,
)
from tests.conftest import GitRepo
from tests.test_explain import (
    _ISO,
    _NOW,
)


def _proc(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _sha_commit(sha: str, author: str, when: str) -> tuple[str, ...]:
    return (sha, author, when, BODY_START, BODY_END)


def test_identities_match_variants() -> None:
    assert identities_match("@alice", "alice")
    assert identities_match("alice@example.com", "alice")
    assert identities_match("alice", "alice@example.com")
    assert not identities_match("", "@alice")
    assert not identities_match("@", "@")
    assert not identities_match("@alice", "@bob")


def test_explain_shows_scoring_lineage_after_a_rename(git_repo: GitRepo) -> None:
    git_repo.commit_files(
        {"src/old.py": "value = 1\n"},
        "create",
        author="Alice",
        email="alice@example.com",
        date="2026-05-01T12:00:00+00:00",
    )
    git_repo.rename("src/old.py", "src/new.py")
    git_repo.commit(
        "rename",
        author="Bob",
        email="bob@example.com",
        date="2026-05-02T12:00:00+00:00",
    )
    config = Config(analysis=AnalysisConfig(min_commits=1, confidence_threshold=0.0))
    ownership = analyze_ownership(
        git_repo.path,
        config,
        as_of=_NOW,
        analysis_ref=git_repo.head_sha(),
    )
    explanation = build_explanation(ownership, "src/new.py", config, git_repo.path, as_of=_NOW)
    assert explanation.lineage == ("src/old.py",)
    assert explanation.logical_file_id
    assert explanation.rename_confidence == 1.0
    payload = explanation_payload(explanation)
    assert payload["logical_file_id"] == explanation.logical_file_id
    assert payload["rename_confidence"] == 1.0


def test_lookup_and_parse_sha_log() -> None:
    assert _lookup_shas("@alice", {"@alice": ("aaa",)}) == ("aaa",)
    assert _lookup_shas("@alice", {"alice@example.com": ("bbb",)}) == ("bbb",)
    assert _lookup_shas("@alice", {"@bob": ("ccc",)}) == ()
    stdout = "\n".join(
        (
            *_sha_commit("111111111111xxxx", "alice", _ISO),
            *_sha_commit("111111111111xxxx", "alice", _ISO),
            *_sha_commit("222222222222xxxx", "alice", _ISO),
            *_sha_commit("333333333333xxxx", "alice", _ISO),
            *_sha_commit("444444444444xxxx", "alice", _ISO),
            *_sha_commit("555555555555xxxx", "alice", _ISO),
            *_sha_commit("666666666666xxxx", "alice", _ISO),
            "not-a-sha",
            "bob",
            "not-a-date",
            *_sha_commit("777777777777xxxx", "bob", _ISO),
        )
    )
    parsed = _parse_sha_log(stdout, Path(), use_mailmap=False, count_co_authors=True)
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
    trailer = "\n".join(
        (
            "aaaaaaaaaaaa",
            "alice@example.com",
            _ISO,
            BODY_START,
            "Co-authored-by: Bob <bob@example.com>",
            BODY_END,
            "bbbbbbbbbbbb",
            "carol@example.com",
            _ISO,
            "not-a-body",
            "eeeeeeeeeeee",
            "erin@example.com",
            _ISO,
        )
    )
    credited = _parse_sha_log(trailer, Path(), use_mailmap=False, count_co_authors=True)
    assert credited["alice@example.com"] == ("aaaaaaaaaaaa",)
    assert credited["bob@example.com"] == ("aaaaaaaaaaaa",)
    assert "carol@example.com" not in credited
    assert "erin@example.com" not in credited
    ignored = _parse_sha_log(trailer, Path(), use_mailmap=False, count_co_authors=False)
    assert ignored["alice@example.com"] == ("aaaaaaaaaaaa",)
    assert "bob@example.com" not in ignored
    truncated = "\n".join(
        (
            "cccccccccccc",
            "dave@example.com",
            _ISO,
            BODY_START,
            "Co-authored-by: Eve <eve@example.com>",
        )
    )
    assert _parse_sha_log(truncated, Path(), use_mailmap=False, count_co_authors=True) == {}
