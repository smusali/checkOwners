"""Tests for privacy controls, labeling, and email tokens at rest."""

from __future__ import annotations

import json
import warnings
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from checkowners.cli import (
    _ACTIVE_CONFIG,
    _aggregate,
    _codeowners_ownership,
    _person,
    _public_payload,
    _review_provider,
    app,
)
from checkowners.config import load_config
from checkowners.graph import for_display
from checkowners.models import (
    Config,
    DecayWarning,
    GithubConfig,
    OutputConfig,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
    PrivacyConfig,
)
from checkowners.privacy import (
    email_token,
    exclude_contributors,
    is_excluded,
    label,
    named_codeowners_allowed,
    pseudonym,
    rekey_handle_cache,
    sanitize,
    scrub_text,
    without_emails,
)
from checkowners.state import (
    read_handle_cache,
    repository_identity,
    write_graph_cache,
    write_handle_cache,
    write_state,
)
from tests.conftest import GitRepo

runner = CliRunner()
_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)
_ALICE = "alice@example.com"
_BOB = "bob@example.com"
_AS_OF = "2026-06-15T00:00:00+00:00"
_COMMIT_DATE = "2025-12-01T00:00:00Z"
_PRIVACY_COMMANDS = (
    ["--offline", "--as-of", _AS_OF, "analyze", "--json"],
    ["--offline", "--as-of", _AS_OF, "decay", "--json"],
    ["--offline", "--as-of", _AS_OF, "bus-factor", "--all", "--json"],
    ["--offline", "--as-of", _AS_OF, "topology", "--json"],
    ["--offline", "--as-of", _AS_OF, "balance", "--json"],
    ["--offline", "--as-of", _AS_OF, "expertise", "foo.py", "--json"],
    ["--offline", "--as-of", _AS_OF, "graph"],
    ["--offline", "--as-of", _AS_OF, "graph", "--export", "dot"],
    ["--offline", "--as-of", _AS_OF, "drift", "--json"],
    ["--offline", "--as-of", _AS_OF, "trends", "--json"],
    ["--offline", "--as-of", _AS_OF, "github-action", "--json", "--no-fail-on-drift"],
)


def _write_config(tmp_path: Path, content: str) -> Path:
    config_dir = tmp_path / ".github"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "checkowners.yml").write_text(content, encoding="utf-8")
    return tmp_path


def _privacy_repo(tmp_path: Path) -> GitRepo:
    repo = GitRepo.create(tmp_path / "repo")
    repo.commit_files(
        {"foo.py": "print(1)\n"},
        "add foo",
        author="Dev",
        email=_ALICE,
        date=_COMMIT_DATE,
    )
    return repo


def test_privacy_controls_load(tmp_path: Path) -> None:
    root = _write_config(
        tmp_path,
        """
version: 2
output:
  anonymize: true
  aggregate_only: true
privacy:
  redact_emails: true
identity:
  mode: hashed
contributors:
  exclude:
    - alice@example.com
    - "@bob"
""",
    )
    loaded = load_config(repo_root=root)
    assert loaded.output.anonymize is True
    assert loaded.output.aggregate_only is True
    assert loaded.privacy.redact_emails is True
    assert loaded.identity_mode == "hashed"
    assert loaded.contributors_exclude == ("alice@example.com", "@bob")


def test_identity_mode_is_rejected(tmp_path: Path) -> None:
    root = _write_config(tmp_path, "version: 2\nidentity:\n  mode: name\n")
    with pytest.raises(ValueError, match=r"identity\.mode"):
        load_config(repo_root=root)


def test_v1_ignores_privacy_keys(tmp_path: Path) -> None:
    root = _write_config(
        tmp_path,
        "output:\n  anonymize: true\nprivacy:\n  redact_emails: true\nidentity:\n  mode: hashed\n",
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        loaded = load_config(repo_root=root)
    assert loaded.output.anonymize is False
    assert loaded.privacy.redact_emails is False
    assert loaded.identity_mode == "handle"


def test_state_stores_email_tokens(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    owner = OwnerEntry(
        handle="alice@example.com",
        ownership_score=0.5,
        last_commit=_NOW,
        commits=1,
    )
    ownership = OwnershipMap(
        paths={"a.py": PathOwnership(owners=(owner,), qualified_owner_count=1)},
        last_analyzed=_NOW,
        analysis_ref="abc",
    )
    target = write_state(repo, ownership)
    text = target.read_text(encoding="utf-8")
    assert "alice@example.com" not in text
    assert email_token("alice@example.com") in text


def test_handle_cache_rewrites_plaintext_keys() -> None:
    target = write_handle_cache({})
    target.write_text(json.dumps({"alice@example.com": "@alice"}), encoding="utf-8")
    cache = read_handle_cache()
    assert "alice@example.com" not in target.read_text(encoding="utf-8")
    assert cache[email_token("alice@example.com")] == "@alice"


@pytest.mark.parametrize("mode", ["anonymize", "aggregate_only"])
@pytest.mark.parametrize("args", _PRIVACY_COMMANDS)
def test_privacy_modes_hide_identities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    args: list[str],
) -> None:
    repo = _privacy_repo(tmp_path)
    flag = "anonymize: true" if mode == "anonymize" else "aggregate_only: true"
    config_path = tmp_path / "checkowners.yml"
    config_path.write_text(f"version: 2\noutput:\n  {flag}\n", encoding="utf-8")
    monkeypatch.chdir(repo.path)
    monkeypatch.setenv("CHECKOWNERS_CONFIG", str(config_path))
    result = runner.invoke(app, args)
    assert result.exit_code in {0, 3}, result.output
    combined = result.stdout + result.stderr
    assert _ALICE not in combined
    assert "@alice" not in combined
    if mode == "aggregate_only" and "analyze" in args and "--json" in args:
        payload = json.loads(result.stdout)
        inferred = payload["inferred"]
        assert any("qualified_owner_count" in row for row in inferred.values())


def test_pseudonyms_are_stable_and_repo_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    left = pseudonym(_ALICE, "origin:one")
    assert left == pseudonym(_ALICE, "origin:one")
    assert left != pseudonym(_ALICE, "origin:two")
    assert left.startswith("person-")
    repo = _privacy_repo(tmp_path)
    config_path = tmp_path / "checkowners.yml"
    config_path.write_text("version: 2\noutput:\n  anonymize: true\n", encoding="utf-8")
    monkeypatch.chdir(repo.path)
    monkeypatch.setenv("CHECKOWNERS_CONFIG", str(config_path))
    first = runner.invoke(app, ["--offline", "--as-of", _AS_OF, "--no-cache", "analyze", "--json"])
    second = runner.invoke(app, ["--offline", "--as-of", _AS_OF, "--no-cache", "analyze", "--json"])
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert first.stdout == second.stdout
    assert pseudonym(_ALICE, repository_identity(repo.path)) in first.stdout


def test_excluded_contributor_is_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _privacy_repo(tmp_path)
    repo.commit_files(
        {"bar.py": "print(2)\n"},
        "add bar",
        author="Other",
        email=_BOB,
        date=_COMMIT_DATE,
    )
    config_path = tmp_path / "checkowners.yml"
    config_path.write_text(
        f'version: 2\ncontributors:\n  exclude:\n    - "{_ALICE}"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(repo.path)
    monkeypatch.setenv("CHECKOWNERS_CONFIG", str(config_path))
    analyzed = runner.invoke(app, ["--offline", "--as-of", _AS_OF, "analyze", "--json"])
    exported = runner.invoke(
        app, ["--offline", "--as-of", _AS_OF, "--no-cache", "graph", "--export", "dot"]
    )
    assert analyzed.exit_code == 0, analyzed.output
    assert exported.exit_code == 0, exported.output
    assert _ALICE not in analyzed.stdout
    assert _ALICE not in exported.stdout
    assert _BOB in analyzed.stdout
    assert _BOB in exported.stdout


def _owner(handle: str, score: float = 0.8) -> OwnerEntry:
    return OwnerEntry(handle=handle, ownership_score=score, last_commit=_NOW, commits=2)


def _map_with(*owners: OwnerEntry, candidates: tuple[OwnerEntry, ...] = ()) -> OwnershipMap:
    warning = DecayWarning(
        handle=owners[0].handle,
        path="a.py",
        last_commit=_NOW,
        days_since_last_commit=10,
        historical_confidence=0.4,
    )
    return OwnershipMap(
        paths={
            "a.py": PathOwnership(
                owners=owners,
                qualified_owner_count=len(owners),
                candidates=candidates,
                decay_warnings=(warning,),
            )
        },
        last_analyzed=_NOW,
        analysis_ref="abc",
    )


def test_email_token_keeps_an_existing_token() -> None:
    token = email_token(_ALICE)
    assert email_token(token) == token


def test_rekey_keeps_a_resolved_handle_over_a_miss() -> None:
    token = email_token(_ALICE)
    promoted = rekey_handle_cache({_ALICE: "", token: "@alice"})
    assert promoted == {token: "@alice"}
    kept = rekey_handle_cache({token: "@alice", _ALICE: ""})
    assert kept == {token: "@alice"}
    blank = rekey_handle_cache({token: "", _ALICE: ""})
    assert blank == {token: ""}


def test_label_hashes_emails_and_keeps_handles() -> None:
    hashed = Config(identity_mode="hashed")
    redacted = Config(privacy=PrivacyConfig(redact_emails=True))
    assert label(_ALICE, hashed, "repo") == email_token(_ALICE)
    assert label(_ALICE, redacted, "repo") == email_token(_ALICE)
    assert label("@alice", hashed, "repo") == "@alice"
    assert label(_ALICE, Config(identity_mode="email"), "repo") == _ALICE


def test_named_codeowners_refuses_anonymous_modes() -> None:
    assert named_codeowners_allowed(Config()) is True
    assert named_codeowners_allowed(Config(output=OutputConfig(anonymize=True))) is False
    assert named_codeowners_allowed(Config(output=OutputConfig(aggregate_only=True))) is False
    assert named_codeowners_allowed(Config(identity_mode="hashed")) is False


def test_exclude_matches_handle_email_and_blank_entries() -> None:
    excluded = ("  ", "@Alice", "bob")
    assert is_excluded("@alice", excluded) is True
    assert is_excluded("alice", excluded) is True
    assert is_excluded("@bob", excluded) is True
    assert is_excluded(_BOB, ()) is False
    ownership = _map_with(_owner(_ALICE, 0.9), _owner("@carol", 0.2), candidates=(_owner(_BOB),))
    same = exclude_contributors(ownership, (), confidence_threshold=0.3)
    assert same is ownership
    dropped = exclude_contributors(ownership, (_ALICE,), confidence_threshold=0.3)
    path = dropped.paths["a.py"]
    assert [owner.handle for owner in path.owners] == ["@carol"]
    assert path.qualified_owner_count == 0
    assert path.decay_warnings == ()
    assert path.candidates[0].handle == _BOB


def test_without_emails_drops_addresses_and_recounts() -> None:
    ownership = _map_with(
        _owner(_ALICE, 0.9),
        _owner("@bob", 0.2),
        candidates=(_owner(_BOB, 0.4), _owner("@cara", 0.4)),
    )
    kept = without_emails(ownership, confidence_threshold=0.3)
    path = kept.paths["a.py"]
    assert [owner.handle for owner in path.owners] == ["@bob"]
    assert [owner.handle for owner in path.candidates] == ["@cara"]
    assert path.decay_warnings == ()
    assert path.qualified_owner_count == 0


def test_sanitize_drops_people_and_labels_text() -> None:
    payload: dict[object, object] = {
        1: "ignored",
        "handle": _ALICE,
        "note": f"ask {_ALICE} or @alice",
        "count": 2,
        "nested": [_ALICE],
    }
    aggregate = Config(output=OutputConfig(aggregate_only=True))
    hidden = sanitize(payload, aggregate, "repo")
    assert "handle" not in hidden
    assert _ALICE not in json.dumps(hidden)
    assert "@alice" not in json.dumps(hidden)
    assert hidden["count"] == 2
    anonymized = sanitize(payload, Config(output=OutputConfig(anonymize=True)), "repo")
    text = json.dumps(anonymized)
    assert _ALICE not in text
    assert "@alice" not in text
    assert "person-" in text
    hashed = scrub_text(f"{_ALICE} @alice", Config(identity_mode="hashed"), "repo")
    assert hashed == f"{email_token(_ALICE)} @alice"


def test_graph_display_relabels_contributors_and_can_drop_them() -> None:
    networkx = pytest.importorskip("networkx")
    graph = networkx.Graph()
    graph.add_node("path::a.py", kind="path")
    graph.add_node("contrib::alice@example.com", kind="contributor")
    graph.add_node("not-a-contributor-id", kind="contributor")
    graph.add_node(7, kind="contributor")
    shown = for_display(graph, Config(output=OutputConfig(anonymize=True)), "repo")
    labels = {node for node in shown.nodes if isinstance(node, str)}
    assert any(node.startswith("contrib::person-") for node in labels)
    assert "contrib::alice@example.com" not in labels
    paths_only = networkx.Graph()
    paths_only.add_node("path::a.py", kind="path")
    assert for_display(paths_only, Config(), "repo") is paths_only
    dropped = for_display(graph, Config(output=OutputConfig(aggregate_only=True)), "repo")
    assert list(dropped.nodes) == ["path::a.py"]


def test_graph_cache_skips_malformed_entries(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = write_graph_cache(
        repo,
        _NOW,
        {
            "nodes": ["skip", {"id": "contrib::alice@example.com"}, {"id": 3}],
            "edges": ["skip", {"source": "contrib::bob@example.com", "target": "path::a"}],
        },
    )
    text = target.read_text(encoding="utf-8")
    assert _ALICE not in text
    assert _BOB not in text
    assert email_token(_ALICE) in text
    assert email_token(_BOB) in text
    empty = write_graph_cache(repo, _NOW, {"nodes": "nope", "edges": None})
    stored = json.loads(empty.read_text(encoding="utf-8"))
    assert stored["graph"]["nodes"] == []
    assert stored["graph"]["edges"] == []


@pytest.mark.parametrize(
    ("content", "match"),
    [
        ('version: 2\noutput:\n  anonymize: "yes"\n', "output.anonymize"),
        ("version: 2\noutput:\n  aggregate_only: 1\n", "output.aggregate_only"),
        ('version: 2\nprivacy:\n  redact_emails: "yes"\n', "privacy.redact_emails"),
        ("version: 2\nidentity:\n  mode: 1\n", "identity.mode"),
        ("version: 2\ncontributors: []\n", "contributors must be a mapping"),
        ("version: 2\ncontributors:\n  exclude: alice\n", "contributors.exclude must be a list"),
        ("version: 2\ncontributors:\n  exclude:\n    - '  '\n", "non-empty strings"),
        ("version: 2\ncontributors:\n  exclude:\n    - 1\n", "non-empty strings"),
    ],
)
def test_privacy_config_values_are_rejected(tmp_path: Path, content: str, match: str) -> None:
    root = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match=match):
        load_config(repo_root=root)


def test_contributors_section_without_exclude_is_empty(tmp_path: Path) -> None:
    root = _write_config(tmp_path, "version: 2\ncontributors: {}\n")
    assert load_config(repo_root=root).contributors_exclude == ()


def test_v1_ignores_non_mapping_privacy_sections(tmp_path: Path) -> None:
    root = _write_config(
        tmp_path,
        "identity: true\noutput: []\nprivacy: true\ncontributors: true\n",
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        loaded = load_config(repo_root=root)
    assert loaded.identity_mode == "handle"
    assert loaded.output.anonymize is False
    assert loaded.privacy.redact_emails is False


def test_helpers_without_an_active_config() -> None:
    token = _ACTIVE_CONFIG.set(None)
    try:
        assert _aggregate() is False
        assert _person("@alice") == "@alice"
        assert _public_payload({"handle": "@alice"}) == {"handle": "@alice"}
    finally:
        _ACTIVE_CONFIG.reset(token)


def test_codeowners_ownership_skips_raw_emails_when_redacted() -> None:
    config = Config(privacy=PrivacyConfig(redact_emails=True))
    ownership = _map_with(_owner(_ALICE), _owner("@bob"))
    kept = _codeowners_ownership(ownership, config)
    assert [owner.handle for owner in kept.paths["a.py"].owners] == ["@bob"]


def test_review_provider_skips_excluded_emails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/app")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test")
    config = Config(
        github=GithubConfig(api_enabled=True),
        contributors_exclude=("skip@example.com",),
    )
    with patch("checkowners.cli.build_review_coverage", return_value={}) as coverage:
        provider = _review_provider(config)
        assert provider is not None
        assert provider({"skip@example.com", "keep@example.com"}) == {}
    assert coverage.call_args is not None
    assert coverage.call_args.args[2] == {"keep@example.com"}
    monkeypatch.delenv("GITHUB_REPOSITORY")
    assert _review_provider(config) is None
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/app")
    monkeypatch.delenv("GITHUB_TOKEN")
    assert _review_provider(config) is None


def test_aggregate_person_is_blank() -> None:
    config = replace(load_config(), output=OutputConfig(aggregate_only=True))
    token = _ACTIVE_CONFIG.set(config)
    try:
        assert _person(_ALICE) == ""
        assert _aggregate() is True
    finally:
        _ACTIVE_CONFIG.reset(token)
