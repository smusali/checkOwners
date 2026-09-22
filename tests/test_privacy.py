"""Tests for privacy controls, labeling, and email tokens at rest."""

from __future__ import annotations

import json
import warnings
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from checkowners.cli import app
from checkowners.config import load_config
from checkowners.models import OwnerEntry, OwnershipMap, PathOwnership
from checkowners.privacy import email_token, pseudonym
from checkowners.state import (
    read_handle_cache,
    repository_identity,
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
