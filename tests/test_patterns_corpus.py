"""The published compatibility corpus and the Windows path corpus."""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from checkowners.history import (
    path_is_excluded,
)
from checkowners.patterns import (
    match_path,
    parse_rules,
    pattern_matches,
)
from checkowners.validate import validate_codeowners
from tests.codeowners_reference import reference_matches
from tests.conftest import GitRepo, script_regression_repo
from tests.test_patterns import (
    _CATEGORIES,
    _LITERAL,
    _MATCHES,
    _MIN_MATCHES,
    _RULES,
    _UNSUPPORTED,
    _WILD,
    _WINDOWS_PATH_IDS,
    _WINDOWS_PATHS,
    _WINDOWS_RULE_IDS,
    _WINDOWS_RULES,
    MatchCase,
    RuleCase,
    WindowsPathCase,
    WindowsRuleCase,
)


def _ids(cases: list[MatchCase] | list[RuleCase]) -> list[str]:
    return [f"{index}-{case['category']}" for index, case in enumerate(cases)]


def test_compatibility_corpus_covers_required_categories() -> None:
    categories = {case["category"] for case in _MATCHES} | {case["category"] for case in _RULES}
    assert len(_MATCHES) >= _MIN_MATCHES
    assert categories >= _CATEGORIES


@pytest.mark.parametrize("case", _MATCHES, ids=_ids(_MATCHES))
def test_compatibility_match(case: MatchCase) -> None:
    assert pattern_matches(case["pattern"], case["path"]) is case["expected"]


@pytest.mark.parametrize("case", _RULES, ids=_ids(_RULES))
def test_compatibility_rule(case: RuleCase, tmp_path: Path) -> None:
    matched = match_path(parse_rules(case["content"]), case["path"])
    if case["expected_owners"] is None:
        assert matched is None
    else:
        assert matched is not None
        assert list(matched.owners) == case["expected_owners"]
    if case["category"] not in _UNSUPPORTED:
        return
    target = tmp_path / "CODEOWNERS"
    target.write_text(case["content"], encoding="utf-8")
    assert validate_codeowners(tmp_path, codeowners_path=target)


@pytest.mark.parametrize("case", _WINDOWS_PATHS, ids=_WINDOWS_PATH_IDS)
def test_windows_path_corpus(case: WindowsPathCase) -> None:
    assert pattern_matches(case["pattern"], case["path"]) is case["match"]
    assert path_is_excluded(case["path"], (case["pattern"],)) is case["exclude"]


@pytest.mark.parametrize("case", _WINDOWS_RULES, ids=_WINDOWS_RULE_IDS)
def test_windows_path_owners(case: WindowsRuleCase) -> None:
    matched = match_path(parse_rules(case["content"]), case["path"])
    if case["owners"] is None:
        assert matched is None
    else:
        assert matched is not None
        assert matched.owners == case["owners"]


@st.composite
def _patterns(draw: st.DrawFn) -> str:
    parts = draw(st.lists(st.one_of(_LITERAL, _WILD), min_size=1, max_size=3))
    if draw(st.booleans()):
        parts.insert(draw(st.integers(min_value=0, max_value=len(parts))), "**")
    pattern = "/".join(parts)
    if draw(st.booleans()):
        pattern = f"/{pattern}"
    if draw(st.booleans()):
        pattern = f"{pattern}/"
    return pattern


@st.composite
def _paths(draw: st.DrawFn) -> str:
    parts = draw(st.lists(_LITERAL, min_size=1, max_size=4))
    return "/".join(parts)


@given(pattern=_patterns(), path=_paths())
def test_pattern_matches_reference(pattern: str, path: str) -> None:
    assert pattern_matches(pattern, path) is reference_matches(pattern, path)


def test_patterns_match_paths_from_git_ls_files(git_repo: GitRepo) -> None:
    script_regression_repo(git_repo)
    tracked = git_repo.ls_files()
    bracket = next(path for path in tracked if "[" in path and "]" in path)
    spaced = next(path for path in tracked if " " in path)
    unicode_path = next(path for path in tracked if path.startswith("notes/"))
    renamed = next(path for path in tracked if path == "src/app.py")
    escaped = parse_rules(spaced.replace(" ", "\\ ") + " @alice\n")
    assert pattern_matches("src/", renamed)
    assert pattern_matches(escaped[0].pattern, spaced)
    assert pattern_matches("app/*/page.tsx", bracket)
    assert pattern_matches("notes/", unicode_path)
    assert pattern_matches(unicode_path, unicode_path)
    cased = next(path for path in tracked if path == "Dir/File.py")
    long_path = next(path for path in tracked if path.startswith("deep/"))
    for path in tracked:
        assert "\\" not in path
        assert not path.startswith("/")
        assert not (len(path) >= 2 and path[1] == ":")
    assert pattern_matches("dir/file.py", cased) is False
    assert pattern_matches(cased, cased)
    assert pattern_matches(long_path, long_path)
