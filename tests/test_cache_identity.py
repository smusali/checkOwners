"""Where the cache lives, and how a repository is identified."""

from __future__ import annotations

import builtins
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType

import pytest

from checkowners.config import (
    Config,
)
from checkowners.state import (
    load_ownership,
    write_state,
)
from checkowners.storage import (
    repository_identity,
    state_path,
)
from tests.conftest import (
    init_git_repo,
    stored_ownership,
)


def test_atomic_write_removes_temp_on_failure(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = Path.write_text

    def fail_temp(
        self: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        if self.name.endswith(".tmp"):
            raise OSError("disk full")
        return real(self, data, encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr(Path, "write_text", fail_temp)
    with pytest.raises(OSError, match="disk full"):
        write_state(repo, stored_ownership())
    assert list(state_path(repo).parent.glob("*.tmp")) == []


def test_state_write_succeeds_without_fcntl(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def guarded(
        name: str,
        globals_: Mapping[str, object] | None = None,
        locals_: Mapping[str, object] | None = None,
        fromlist: Sequence[str] = (),
        level: int = 0,
    ) -> ModuleType:
        if name == "fcntl":
            raise ImportError
        return real_import(name, globals_, locals_, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded)
    target = write_state(repo, stored_ownership())
    assert target.is_file()
    assert load_ownership(repo) is not None


def test_windows_lock_uses_msvcrt_when_fcntl_is_missing(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_import = builtins.__import__

    def guarded(
        name: str,
        globals_: Mapping[str, object] | None = None,
        locals_: Mapping[str, object] | None = None,
        fromlist: Sequence[str] = (),
        level: int = 0,
    ) -> ModuleType:
        if name == "fcntl":
            raise ImportError
        return real_import(name, globals_, locals_, fromlist, level)

    class Msvcrt:
        LK_UNLCK = 0
        LK_LOCK = 1

        def __init__(self) -> None:
            self.calls: list[tuple[int, int, int]] = []

        def locking(self, fd: int, mode: int, nbytes: int) -> None:
            self.calls.append((fd, mode, nbytes))

    fake = Msvcrt()
    monkeypatch.setattr(builtins, "__import__", guarded)
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setattr("checkowners.storage._WINDOWS", True)
    write_state(repo, stored_ownership())
    write_state(repo, stored_ownership())
    assert [mode for _fd, mode, _nbytes in fake.calls] == [
        Msvcrt.LK_LOCK,
        Msvcrt.LK_UNLCK,
        Msvcrt.LK_LOCK,
        Msvcrt.LK_UNLCK,
    ]
    lock = state_path(repo).with_name(f"{state_path(repo).name}.lock")
    assert lock.read_bytes() == b"\0"


def test_repository_identity_ignores_unusable_origin(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = repo / ".git" / "config"
    config.parent.mkdir()
    expected = f"path:{repo.resolve()}"
    for body in (
        '[core]\n\tbare = false\n[remote "origin"]\n\turl =\n',
        '[remote "origin"]\n\turl = https://github.com\n',
        '[remote "origin"]\n\turl = not-a-url\n',
        '[remote "origin"]\n\turl = github.com:/\n',
    ):
        config.write_text(body, encoding="utf-8")
        assert repository_identity(repo) == expected
    config.write_text(
        '[remote "origin"]\n\tfetch = +refs/heads/*\n\turl = https://github.com/acme/widget\n',
        encoding="utf-8",
    )
    assert repository_identity(repo) == "origin:github.com/acme/widget"


def test_repository_identity_follows_worktree_gitdir(tmp_path: Path) -> None:
    common = tmp_path / "common"
    common.mkdir()
    (common / "config").write_text(
        '[remote "origin"]\n\turl = https://user:token@GitHub.com/acme/widget.git\n',
        encoding="utf-8",
    )
    git_dir = tmp_path / "wt.git"
    git_dir.mkdir()
    (git_dir / "commondir").write_text("../common\n", encoding="utf-8")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").write_text("gitdir: ../wt.git\n", encoding="utf-8")
    assert repository_identity(checkout) == "origin:github.com/acme/widget"

    (git_dir / "commondir").write_text(f"{common}\n", encoding="utf-8")
    (checkout / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    assert repository_identity(checkout) == "origin:github.com/acme/widget"

    (git_dir / "commondir").unlink()
    (git_dir / "config").write_text(
        '[remote "origin"]\n\turl = git@github.com:acme/other\n',
        encoding="utf-8",
    )
    assert repository_identity(checkout) == "origin:github.com/acme/other"

    (checkout / ".git").write_text("ref: refs/heads/main\n", encoding="utf-8")
    assert repository_identity(checkout) == f"path:{checkout.resolve()}"


def test_origin_url_ignores_unreadable_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_git_repo(tmp_path / "repo")
    config = repo / ".git" / "config"
    real = Path.read_text

    def flaky(
        self: Path,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> str:
        if self == config:
            raise OSError("denied")
        return real(self, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", flaky)
    assert repository_identity(repo).startswith("path:")


def test_same_origin_shares_state(tmp_path: Path) -> None:
    first = init_git_repo(tmp_path / "first")
    second = init_git_repo(tmp_path / "second")
    for root, url in (
        (first, "git@github.com:acme/widget.git"),
        (second, "https://github.com/acme/widget.git"),
    ):
        subprocess.run(  # noqa: S603
            ["git", "remote", "add", "origin", url],  # noqa: S607
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    write_state(first, stored_ownership(), config=Config())
    loaded = load_ownership(second)
    assert loaded is not None
    assert loaded.analysis_ref == "deadbeef"
    assert repository_identity(first) == repository_identity(second)


def test_state_path_is_per_repo(tmp_path: Path, repo: Path) -> None:
    other = tmp_path / "other-repo"
    other.mkdir()
    assert state_path(repo) != state_path(other)
    assert state_path(repo).parent.name == "state"


def test_state_isolated_between_repos(tmp_path: Path, repo: Path) -> None:
    """Analyzing repo A must never leak ownership into repo B."""
    other = tmp_path / "other-repo"
    other.mkdir()
    write_state(repo, stored_ownership())
    assert load_ownership(other) is None
    assert load_ownership(repo) is not None
