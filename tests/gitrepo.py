"""Building a real git repository for a test to analyze."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from checkowners.analyze import (
    analyze_ownership,
)
from checkowners.config import (
    AnalysisConfig,
    Config,
)
from checkowners.git import (
    MIN_GIT_VERSION,
    parse_git_version,
)
from checkowners.models import (
    OwnershipMap,
)

#: Noreply addresses so identity resolves to @handles locally, with no API call.
ALICE = "1+alice@users.noreply.github.com"
BOB = "2+bob@users.noreply.github.com"
REGRESSION_AS_OF = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)


DEFAULT_COMMIT_DATE = "2026-05-01T12:00:00+00:00"


def _git_env(path: Path) -> dict[str, str]:
    return {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(path),
        "XDG_CONFIG_HOME": str(path),
    }


def _require_git() -> None:
    result = subprocess.run(
        ["git", "version"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    try:
        found = parse_git_version(result.stdout)
    except ValueError as exc:
        pytest.fail(str(exc))
    if found < MIN_GIT_VERSION:
        found_text = ".".join(str(part) for part in found)
        required = ".".join(str(part) for part in MIN_GIT_VERSION)
        pytest.fail(f"checkOwners requires Git {required} or newer; found {found_text}")


def _git(
    path: Path,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        args,
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env if env is not None else _git_env(path),
    )


def init_git_repo(path: Path) -> Path:
    """Create a local git repo at ``path`` with signing disabled."""
    _require_git()
    path.mkdir(parents=True, exist_ok=True)
    env = _git_env(path)
    _git(path, ["git", "init"], env=env)
    _git(path, ["git", "symbolic-ref", "HEAD", "refs/heads/main"], env=env)
    for key, value in (
        ("user.email", "test@example.com"),
        ("user.name", "Test"),
        ("commit.gpgsign", "false"),
        ("core.autocrlf", "false"),
        ("core.quotePath", "false"),
        ("core.precomposeunicode", "true"),
        ("core.longpaths", "true"),
    ):
        _git(path, ["git", "config", key, value], env=env)
    return path


def git_commit(
    path: Path,
    message: str,
    *,
    author: str,
    email: str,
    date: str,
) -> str:
    """Stage all changes and commit with pinned author and committer identity."""
    env = {
        **_git_env(path),
        "GIT_AUTHOR_NAME": author,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_NAME": author,
        "GIT_COMMITTER_EMAIL": email,
        "GIT_COMMITTER_DATE": date,
    }
    _git(path, ["git", "add", "-A"], env=env)
    _git(path, ["git", "commit", "-m", message], env=env)
    return _git(path, ["git", "rev-parse", "HEAD"], env=env).stdout.strip()


@dataclass
class GitRepo:
    path: Path

    @classmethod
    def create(cls, path: Path) -> GitRepo:
        return cls(init_git_repo(path))

    def write(self, rel: str, text: str) -> None:
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(
        self,
        message: str,
        *,
        author: str = "Alice",
        email: str = "alice@example.com",
        date: str = DEFAULT_COMMIT_DATE,
    ) -> str:
        return git_commit(self.path, message, author=author, email=email, date=date)

    def commit_files(
        self,
        files: dict[str, str],
        message: str,
        *,
        author: str = "Alice",
        email: str = "alice@example.com",
        date: str = DEFAULT_COMMIT_DATE,
    ) -> str:
        for rel in sorted(files):
            self.write(rel, files[rel])
        return self.commit(message, author=author, email=email, date=date)

    def commit_paths(self, *paths: str, body: str = "x\n") -> None:
        """Commit `paths` with the same contents so git ls-files lists them."""
        self.commit_files(dict.fromkeys(paths, body), "seed")

    def seed_history(
        self,
        commits: list[tuple[str, datetime, list[str]]],
        *,
        body: str = "x\n",
    ) -> None:
        """Replay `commits` as (email, instant, paths) with pinned dates."""
        contents: dict[str, str] = {}
        for index, (email, when, paths) in enumerate(commits):
            for path in paths:
                contents[path] = contents.get(path, "") + f"{index}:{body}"
            self.commit_files(
                {path: contents[path] for path in paths},
                f"c{index}",
                author=email.split("@", 1)[0],
                email=email,
                date=when.isoformat(),
            )

    def rename(self, src: str, dst: str) -> None:
        (self.path / dst).parent.mkdir(parents=True, exist_ok=True)
        _git(self.path, ["git", "mv", src, dst])

    def copy(self, src: str, dst: str) -> None:
        target = self.path / dst
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((self.path / src).read_bytes())

    def format_sweep(self, rel: str) -> None:
        target = self.path / rel
        text = target.read_text(encoding="utf-8")
        target.write_text(text.rstrip("\n") + "\n\n", encoding="utf-8")

    def coauthored_commit(
        self,
        message: str,
        *,
        author: str,
        email: str,
        date: str,
        coauthor: str,
        coemail: str,
    ) -> str:
        trailer = f"{message}\n\nCo-authored-by: {coauthor} <{coemail}>"
        return self.commit(trailer, author=author, email=email, date=date)

    def write_mailmap(self, text: str) -> None:
        self.write(".mailmap", text)

    def write_ignore_revs(self, shas: tuple[str, ...]) -> None:
        self.write(".git-blame-ignore-revs", "".join(f"{sha}\n" for sha in shas))

    def write_gitattributes(self, text: str) -> None:
        self.write(".gitattributes", text)

    def ls_files(self) -> tuple[str, ...]:
        result = _git(self.path, ["git", "ls-files"])
        return tuple(line for line in result.stdout.splitlines() if line)

    def set_origin(self, url: str) -> None:
        _git(self.path, ["git", "remote", "add", "origin", url])

    def head_sha(self) -> str:
        return _git(self.path, ["git", "rev-parse", "HEAD"]).stdout.strip()


def regression_analysis_config() -> Config:
    return Config(analysis=AnalysisConfig(confidence_threshold=0.0, min_commits=1))


def analyze_regression_repo(repo: GitRepo) -> OwnershipMap:
    return analyze_ownership(
        repo.path,
        regression_analysis_config(),
        as_of=REGRESSION_AS_OF,
    )


def script_regression_repo(repo: GitRepo) -> None:
    long_path = f"deep/{'a' * 200}.py"
    repo.commit_files(
        {
            "src/main.py": "def total(items):\n    return sum(items)\n",
            "docs/getting started.md": "# Guide\n",
            "app/[companyId]/page.tsx": "export const page = 1\n",
            "notes/ünicode.py": "value = 1\n",
            "generated/out.py": "x = 1\n",
            "Dir/File.py": "name = 'File'\n",
            long_path: "long_path = True\n",
        },
        "squash initial tree",
        author="Alice",
        email=ALICE,
        date="2026-05-01T12:00:00+00:00",
    )
    repo.format_sweep("src/main.py")
    format_sha = repo.commit(
        "format sweep",
        author="Bob",
        email="bob@example.com",
        date="2026-05-02T12:00:00+00:00",
    )
    repo.rename("src/main.py", "src/app.py")
    repo.commit(
        "rename main",
        author="Alice",
        email="alice@example.com",
        date="2026-05-03T12:00:00+00:00",
    )
    repo.copy("src/app.py", "src/app_copy.py")
    repo.coauthored_commit(
        "copy app",
        author="Bob",
        email="bob@home.example",
        date="2026-05-04T12:00:00+00:00",
        coauthor="Alice",
        coemail="alice@example.com",
    )
    repo.write_mailmap("Bob Home <bob@example.com> <bob@home.example>\n")
    repo.write_ignore_revs((format_sha,))
    repo.write_gitattributes("generated/** linguist-generated\n")
    repo.commit(
        "repo metadata",
        author="Alice",
        email="alice@example.com",
        date="2026-05-05T12:00:00+00:00",
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> GitRepo:
    return GitRepo.create(tmp_path / "repo")


#: Pinned commit dates, so a scripted history scores the same on every machine.
FIRST_DATE = "2026-05-01T12:00:00+00:00"


SECOND_DATE = "2026-05-02T12:00:00+00:00"


THIRD_DATE = "2026-05-03T12:00:00+00:00"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Return an empty directory that stands in for a repository root."""
    root = tmp_path / "repo"
    root.mkdir()
    return root
