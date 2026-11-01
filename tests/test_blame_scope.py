"""Which paths blame runs on: gitattributes, the exclude list, and a pathspec."""

from __future__ import annotations

from pathlib import Path

from checkowners.analyze import (
    analyze_ownership,
)
from checkowners.config import (
    AnalysisConfig,
    Config,
    QualificationConfig,
    ScoringConfig,
)
from checkowners.history import (
    filter_excluded,
    filter_nonexistent,
    gitattributes_pattern_matches,
    linguist_excluded_paths,
    parse_gitattributes,
    path_is_excluded,
)
from checkowners.scoring import (
    Contribution,
)
from tests.conftest import (
    FIRST_DATE,
    NOW,
    RECENT,
    GitRepo,
)

_NO_BLAME = ScoringConfig(blame_weight=0.0, change_survival_weight=0.0)


def test_unqualified_paths_not_blamed(git_repo: GitRepo) -> None:
    git_repo.seed_history(
        [
            ("alice@example.com", RECENT, ["hot.py"]),
            ("alice@example.com", RECENT, ["hot.py"]),
            ("alice@example.com", RECENT, ["hot.py"]),
            ("bob@example.com", RECENT, ["cold.py"]),
        ]
    )
    config = Config(
        analysis=AnalysisConfig(min_commits=3, confidence_threshold=0.0),
        qualification=QualificationConfig(strategy="threshold", min_commits=3),
        scoring=_NO_BLAME,
    )
    result = analyze_ownership(git_repo.path, config, as_of=NOW)
    assert set(result.paths) == {"hot.py"}


def test_adaptive_keeps_low_commit_paths(git_repo: GitRepo) -> None:
    git_repo.seed_history(
        [
            ("alice@example.com", RECENT, ["hot.py"]),
            ("alice@example.com", RECENT, ["hot.py"]),
            ("alice@example.com", RECENT, ["hot.py"]),
            ("bob@example.com", RECENT, ["cold.py"]),
        ]
    )
    config = Config(
        analysis=AnalysisConfig(min_commits=3, confidence_threshold=0.0),
        qualification=QualificationConfig(strategy="adaptive", min_commits=3),
        scoring=_NO_BLAME,
    )
    result = analyze_ownership(git_repo.path, config, as_of=NOW)
    assert {"hot.py", "cold.py"} <= set(result.paths)


def test_gitattributes_generated_directory_excluded_before_blame(git_repo: GitRepo) -> None:
    git_repo.commit_files(
        {
            "generated/out.py": "x = 1\n",
            "src/main.py": "y = 2\n",
            ".gitattributes": "generated/** linguist-generated\n",
        },
        "init",
        date=FIRST_DATE,
    )
    config = Config(analysis=AnalysisConfig(min_commits=1, confidence_threshold=0.0))
    result = analyze_ownership(git_repo.path, config, as_of=NOW)
    assert "generated/out.py" not in result.paths
    assert "src/main.py" in result.paths
    assert result.analysis_completeness.excluded_gitattributes == 1
    disabled = Config(
        analysis=AnalysisConfig(
            min_commits=1,
            confidence_threshold=0.0,
            respect_gitattributes=False,
        )
    )
    off = analyze_ownership(git_repo.path, disabled, as_of=NOW)
    assert "generated/out.py" in off.paths
    assert off.analysis_completeness.excluded_gitattributes == 0


def test_gitattributes_and_static_exclusions_are_not_double_counted(git_repo: GitRepo) -> None:
    git_repo.commit_files(
        {
            "src/main.py": "x = 1\n",
            "vendor/lib.go": "package v\n",
            "dist/bundle.js": "ok\n",
            "generated/out.py": "x = 2\n",
            ".gitattributes": ("vendor/** linguist-vendored\ngenerated/** linguist-generated\n"),
        },
        "init",
        date=FIRST_DATE,
    )
    result = analyze_ownership(
        git_repo.path,
        Config(analysis=AnalysisConfig(min_commits=1, confidence_threshold=0.0)),
        as_of=NOW,
    )
    completeness = result.analysis_completeness
    assert "src/main.py" in result.paths
    assert "vendor/lib.go" not in result.paths
    assert "dist/bundle.js" not in result.paths
    assert "generated/out.py" not in result.paths
    assert completeness.excluded_gitattributes == 2
    assert completeness.excluded_static == 1
    assert completeness.excluded_gitattributes + completeness.excluded_static == 3
    assert any(
        gap.reason == "Excluded files: 2 gitattributes, 1 static." for gap in completeness.gaps
    )


def test_pathspec_limits_blame_to_requested_path(git_repo: GitRepo) -> None:
    git_repo.seed_history(
        [
            ("alice@example.com", RECENT, ["keep.py", "other.py"]),
            ("bob@example.com", RECENT, ["other.py"]),
        ]
    )
    result = analyze_ownership(
        git_repo.path,
        Config(
            analysis=AnalysisConfig(min_commits=1, confidence_threshold=0.0),
            scoring=_NO_BLAME,
        ),
        as_of=NOW,
        pathspec=("keep.py",),
    )
    assert set(result.paths) == {"keep.py"}


def test_pathspec_directory_blames_only_matching_files(git_repo: GitRepo) -> None:
    git_repo.seed_history(
        [("alice@example.com", RECENT, ["src/a.py", "src/b.py", "docs/readme.md"])]
    )
    result = analyze_ownership(
        git_repo.path,
        Config(
            analysis=AnalysisConfig(min_commits=1, confidence_threshold=0.0),
            scoring=_NO_BLAME,
        ),
        as_of=NOW,
        pathspec=("src/",),
    )
    assert set(result.paths) == {"src/a.py", "src/b.py"}


def test_is_excluded_patterns() -> None:
    assert path_is_excluded("yarn.lock", ("*.lock",))
    assert path_is_excluded("package-lock.json", ("*.lock",)) is False
    assert path_is_excluded("dist/bundle.js", ("dist/**",))
    assert path_is_excluded("dist/sub/file.js", ("dist/**",))
    assert path_is_excluded("vendor/a/b.go", ("vendor/**",))
    assert path_is_excluded("src/main.py", ("*.lock", "dist/**", "vendor/**")) is False


def test_gitattributes_parsing_comments_negation_and_scoping() -> None:
    rules = parse_gitattributes(
        "# comment\n"
        "\n"
        "generated/** linguist-generated\n"
        "generated/keep.py -linguist-generated\n"
        "!skip.py linguist-generated\n"
        "vendor/** linguist-vendored linguist-generated\n"
        "pattern-only\n"
        "other.py text -diff eol=lf\n"
        "hist.py linguist-generated=true\n"
        "off.py linguist-generated=false -text\n"
        "mix.py -text linguist-vendored\n",
        "",
    )
    by_pattern = {rule.pattern: rule.values for rule in rules}
    assert set(by_pattern) == {
        "generated/**",
        "generated/keep.py",
        "vendor/**",
        "hist.py",
        "off.py",
        "mix.py",
    }
    assert by_pattern["generated/keep.py"] == (("linguist-generated", False),)
    assert by_pattern["hist.py"] == (("linguist-generated", True),)
    assert by_pattern["off.py"] == (("linguist-generated", False),)
    assert by_pattern["mix.py"] == (("linguist-vendored", True),)
    assert gitattributes_pattern_matches("generated/*", "generated/foo.py", "")
    assert gitattributes_pattern_matches("generated/*", "generated/nested/foo.py", "") is False
    assert gitattributes_pattern_matches("generated/**", "generated/nested/foo.py", "")
    assert gitattributes_pattern_matches("*.pb.go", "src/foo.pb.go", "src")
    assert gitattributes_pattern_matches("*.pb.go", "other/foo.pb.go", "src") is False
    assert gitattributes_pattern_matches("f?.py", "fa.py", "")
    assert gitattributes_pattern_matches("f?.py", "fab.py", "") is False
    assert gitattributes_pattern_matches("a/**/b.py", "a/x/b.py", "")
    assert gitattributes_pattern_matches("/rooted.py", "rooted.py", "")
    assert gitattributes_pattern_matches("/rooted.py", "sub/rooted.py", "") is False
    assert gitattributes_pattern_matches("generated/", "generated/foo.py", "") is False
    assert gitattributes_pattern_matches("/", "anything.py", "") is False
    assert gitattributes_pattern_matches("*", "src/", "src") is False


def test_linguist_nested_gitattributes_and_negation(tmp_path: Path) -> None:
    (tmp_path / ".gitattributes").write_text("*.pb.go linguist-generated\n", encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / ".gitattributes").write_text("keep.pb.go -linguist-generated\n", encoding="utf-8")
    excluded = linguist_excluded_paths(
        tmp_path,
        ("src/foo.pb.go", "src/keep.pb.go", "src/main.py"),
    )
    assert excluded == frozenset({"src/foo.pb.go"})
    assert linguist_excluded_paths(tmp_path, ()) == frozenset()


def test_filter_excluded_removes_paths() -> None:
    contribs: dict[str, dict[str, Contribution]] = {
        "src/main.py": {"alice": Contribution(3, NOW, credit=3.0)},
        "yarn.lock": {"alice": Contribution(5, NOW, credit=5.0)},
        "dist/out.js": {"bob": Contribution(2, NOW, credit=2.0)},
    }
    patterns = ("*.lock", "dist/**")
    filtered = filter_excluded(contribs, patterns)
    assert "src/main.py" in filtered
    assert "yarn.lock" not in filtered
    assert "dist/out.js" not in filtered


def test_filter_nonexistent(tmp_path: Path) -> None:
    (tmp_path / "exists.py").write_text("x", encoding="utf-8")
    contribs: dict[str, dict[str, Contribution]] = {
        "exists.py": {"alice": Contribution(3, NOW, credit=3.0)},
        "deleted.py": {"bob": Contribution(5, NOW, credit=5.0)},
    }
    result = filter_nonexistent(contribs, tmp_path)
    assert "exists.py" in result
    assert "deleted.py" not in result
