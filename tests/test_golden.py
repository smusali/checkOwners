"""Judgment checks for ownership-v1 on pinned git histories."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from checkowners.analyze import ReviewProvider, analyze_ownership
from checkowners.models import OWNERSHIP_MODEL_VERSION, Config, OwnerEntry, OwnershipMap
from tests.conftest import GitRepo

_AS_OF = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
_STRONG_SCORE = 0.5
_MEDIUM_QUALITY_FLOOR = 0.4
_ALICE = "alice@example.com"
_BOB = "bob@example.com"
_WORK = "alice@work.example"
_HOME = "alice@home.example"
_OLD = "alice@old.example"

_GOLDEN: dict[str, dict[str, str]] = {
    "ownership-v1": {
        "formatting": (
            "Alice remains the primary expert on foo.py; "
            "Bob's formatting commit has no blame and a lower score."
        ),
        "handoff": (
            "Ownership moves from Alice to Bob across the year; "
            "Alice's score falls at each later date and stays above zero."
        ),
        "creating_commit": (
            "The author of a single creating commit is the sole primary owner, "
            "with a strong score and medium evidence quality."
        ),
        "rewrite": (
            "After a full rewrite, the new author is primary "
            "and the original author's blame share is zero."
        ),
        "mailmap": "Addresses merged by .mailmap count as one person.",
        "missing_signals": (
            "When blame and review are unavailable, the ownership score stays inside (0, 1]."
        ),
        "rename": "The author of the rename commit is the scored owner of the new path.",
        "cadence": "A path last touched 18 months earlier has no scored owner.",
        "coauthor": (
            "A Co-authored-by trailer adds a scored owner. Blame stays with the commit author."
        ),
    },
}


def _belief(case: str) -> str:
    record = _GOLDEN.get(OWNERSHIP_MODEL_VERSION)
    if record is None:
        pytest.fail(
            f"{OWNERSHIP_MODEL_VERSION}: no golden record. Add this model version to the record."
        )
    message = record.get(case)
    if message is None:
        pytest.fail(f"{OWNERSHIP_MODEL_VERSION}: no golden record for {case}.")
    return message


def _check(case: str, holds: bool) -> None:
    if not holds:
        pytest.fail(f"{OWNERSHIP_MODEL_VERSION} {case}: {_belief(case)}")


def _when(days_before: int) -> datetime:
    return _AS_OF - timedelta(days=days_before)


def _stamp(days_before: int) -> str:
    return _when(days_before).isoformat()


def _commit(
    repo: GitRepo,
    files: dict[str, str],
    message: str,
    *,
    author: str,
    email: str,
    days_before: int,
) -> None:
    repo.commit_files(
        files,
        message,
        author=author,
        email=email,
        date=_stamp(days_before),
    )


def _entry_key(entry: OwnerEntry) -> tuple[str, float, float, bool, float]:
    breakdown = entry.score_breakdown
    if breakdown is None:
        return (entry.handle, entry.ownership_score, entry.evidence_quality, False, 0.0)
    return (
        entry.handle,
        entry.ownership_score,
        entry.evidence_quality,
        breakdown.blame.available,
        breakdown.blame.score,
    )


def _same(left: OwnershipMap, right: OwnershipMap) -> bool:
    if tuple(left.paths) != tuple(right.paths):
        return False
    for path, path_ownership in left.paths.items():
        other = right.paths[path]
        if tuple(_entry_key(entry) for entry in path_ownership.owners) != tuple(
            _entry_key(entry) for entry in other.owners
        ):
            return False
        if tuple(_entry_key(entry) for entry in path_ownership.candidates) != tuple(
            _entry_key(entry) for entry in other.candidates
        ):
            return False
    return True


def _analyze(
    repo: GitRepo,
    case: str,
    when: datetime,
    *,
    review_provider: ReviewProvider | None = None,
    retain_all: bool = False,
) -> OwnershipMap:
    first = analyze_ownership(
        repo.path,
        Config(),
        as_of=when,
        review_provider=review_provider,
        retain_all=retain_all,
    )
    second = analyze_ownership(
        repo.path,
        Config(),
        as_of=when,
        review_provider=review_provider,
        retain_all=retain_all,
    )
    if not _same(first, second):
        pytest.fail(
            f"{OWNERSHIP_MODEL_VERSION} {case}: repeated analysis with the same as-of diverged."
        )
    return first


def _ranked(ownership: OwnershipMap, path: str) -> tuple[OwnerEntry, ...]:
    found = ownership.paths.get(path)
    if found is None:
        return ()
    if found.candidates:
        return found.candidates
    return found.owners


def _person(ownership: OwnershipMap, path: str, handle: str) -> OwnerEntry | None:
    for entry in _ranked(ownership, path):
        if entry.handle == handle:
            return entry
    return None


def _score(ownership: OwnershipMap, path: str, handle: str) -> float:
    entry = _person(ownership, path, handle)
    if entry is None:
        return 0.0
    return entry.ownership_score


def _primary(ownership: OwnershipMap, path: str) -> str | None:
    found = ownership.paths.get(path)
    if found is None or not found.owners:
        return None
    return found.owners[0].handle


def _blame_share(entry: OwnerEntry) -> float:
    breakdown = entry.score_breakdown
    if breakdown is None or not breakdown.blame.available:
        return 0.0
    return breakdown.blame.score


def _reindent(repo: GitRepo, rel: str) -> None:
    text = (repo.path / rel).read_text(encoding="utf-8")
    widened = [f"    {line}" if line.startswith("    ") else line for line in text.splitlines()]
    repo.write(rel, "\n".join(widened) + "\n")


def _overlay(text: str, start: int, end: int, label: str) -> str:
    rows = text.splitlines()
    for index in range(start, end):
        rows[index] = f"{label}_{index} = {index * 10}"
    return "\n".join(rows) + "\n"


def _alice_reviewed(_emails: set[str]) -> dict[str, dict[str, float]]:
    return {"foo.py": {_ALICE: 1.0}}


def _keepers() -> dict[str, str]:
    return {"keep.py": "kept = 1\n", "notes.py": "note = 1\n"}


@pytest.mark.integration
def test_golden_formatting(git_repo: GitRepo) -> None:
    _belief("formatting")
    _commit(
        git_repo,
        {
            "foo.py": "def total(items):\n    return sum(items)\n",
            **_keepers(),
        },
        "create foo",
        author="Alice",
        email=_ALICE,
        days_before=20,
    )
    _commit(
        git_repo,
        {
            "foo.py": (
                "def total(items):\n"
                "    return sum(items)\n"
                "\n"
                "def scaled(items, factor):\n"
                "    return sum(item * factor for item in items)\n"
            ),
        },
        "edit foo",
        author="Alice",
        email=_ALICE,
        days_before=12,
    )
    _reindent(git_repo, "foo.py")
    git_repo.commit(
        "reformat foo",
        author="Bob",
        email=_BOB,
        date=_stamp(6),
    )
    ownership = _analyze(
        git_repo,
        "formatting",
        _AS_OF,
        review_provider=_alice_reviewed,
        retain_all=True,
    )
    alice = _person(ownership, "foo.py", _ALICE)
    bob = _person(ownership, "foo.py", _BOB)
    _check("formatting", alice is not None and bob is not None)
    assert alice is not None
    assert bob is not None
    _check("formatting", _primary(ownership, "foo.py") == _ALICE)
    _check("formatting", _blame_share(bob) == 0.0)
    _check("formatting", bob.ownership_score < alice.ownership_score)


@pytest.mark.integration
def test_golden_handoff(git_repo: GitRepo) -> None:
    _belief("handoff")
    original = "".join(f"alpha_{index} = {index}\n" for index in range(8))
    _commit(
        git_repo,
        {"component.py": original, **_keepers()},
        "alice starts the component",
        author="Alice",
        email=_ALICE,
        days_before=340,
    )
    first = _analyze(git_repo, "handoff", _when(310), retain_all=True)
    _commit(
        git_repo,
        {"component.py": _overlay(original, 0, 2, "beta")},
        "bob maintains",
        author="Bob",
        email=_BOB,
        days_before=240,
    )
    current = (git_repo.path / "component.py").read_text(encoding="utf-8")
    _commit(
        git_repo,
        {"component.py": _overlay(current, 2, 5, "beta")},
        "bob maintains further",
        author="Bob",
        email=_BOB,
        days_before=160,
    )
    second = _analyze(git_repo, "handoff", _when(150), retain_all=True)
    current = (git_repo.path / "component.py").read_text(encoding="utf-8")
    _commit(
        git_repo,
        {"component.py": _overlay(current, 5, 8, "beta")},
        "bob finishes the year",
        author="Bob",
        email=_BOB,
        days_before=50,
    )
    third = _analyze(git_repo, "handoff", _when(20), retain_all=True)
    snapshots = (first, second, third)
    alice_scores = tuple(_score(snapshot, "component.py", _ALICE) for snapshot in snapshots)
    bob_scores = tuple(_score(snapshot, "component.py", _BOB) for snapshot in snapshots)
    _check("handoff", _primary(first, "component.py") == _ALICE)
    _check("handoff", alice_scores[0] > alice_scores[1] > alice_scores[2] > 0.0)
    _check("handoff", bob_scores[0] < bob_scores[1] < bob_scores[2])
    _check("handoff", _primary(third, "component.py") == _BOB)


@pytest.mark.integration
def test_golden_creating_commit(git_repo: GitRepo) -> None:
    _belief("creating_commit")
    _commit(
        git_repo,
        {"created.py": "def created():\n    return 1\n"},
        "create the file",
        author="Alice",
        email=_ALICE,
        days_before=4,
    )
    ownership = _analyze(git_repo, "creating_commit", _AS_OF)
    found = ownership.paths.get("created.py")
    _check("creating_commit", found is not None and len(found.owners) == 1)
    assert found is not None
    owner = found.owners[0]
    _check("creating_commit", owner.handle == _ALICE)
    _check("creating_commit", owner.ownership_score >= _STRONG_SCORE)
    _check(
        "creating_commit",
        _MEDIUM_QUALITY_FLOOR <= owner.evidence_quality < 1.0,
    )


@pytest.mark.integration
def test_golden_rewrite(git_repo: GitRepo) -> None:
    _belief("rewrite")
    _commit(
        git_repo,
        {
            "rewritten.py": "".join(f"alpha_{index} = {index}\n" for index in range(6)),
            **_keepers(),
        },
        "alice writes the file",
        author="Alice",
        email=_ALICE,
        days_before=15,
    )
    _commit(
        git_repo,
        {"rewritten.py": "".join(f"beta_{index} = {index * 10}\n" for index in range(6))},
        "bob rewrites the file",
        author="Bob",
        email=_BOB,
        days_before=5,
    )
    ownership = _analyze(git_repo, "rewrite", _AS_OF, retain_all=True)
    alice = _person(ownership, "rewritten.py", _ALICE)
    _check("rewrite", _primary(ownership, "rewritten.py") == _BOB)
    _check("rewrite", alice is not None and _blame_share(alice) == 0.0)


@pytest.mark.integration
def test_golden_mailmap(git_repo: GitRepo) -> None:
    _belief("mailmap")
    for days_before, email, body in (
        (18, _WORK, "v1\n"),
        (12, _HOME, "v2\n"),
        (6, _OLD, "v3\n"),
    ):
        _commit(
            git_repo,
            {"owned.py": body},
            "edit owned",
            author="Alice",
            email=email,
            days_before=days_before,
        )
    git_repo.write_mailmap(
        "Alice Example <alice@example.com> <alice@work.example>\n"
        "Alice Example <alice@example.com> <alice@home.example>\n"
        "Alice Example <alice@example.com> <alice@old.example>\n"
    )
    ownership = _analyze(git_repo, "mailmap", _AS_OF)
    found = ownership.paths.get("owned.py")
    _check("mailmap", found is not None and found.qualified_owner_count == 1)
    assert found is not None
    _check("mailmap", tuple(owner.handle for owner in found.owners) == (_ALICE,))


@pytest.mark.integration
def test_golden_missing_signals(git_repo: GitRepo) -> None:
    _belief("missing_signals")
    _commit(
        git_repo,
        {"empty.py": ""},
        "track an empty file",
        author="Alice",
        email=_ALICE,
        days_before=3,
    )
    ownership = _analyze(git_repo, "missing_signals", _AS_OF)
    owner = _person(ownership, "empty.py", _ALICE)
    _check("missing_signals", owner is not None)
    assert owner is not None
    breakdown = owner.score_breakdown
    _check("missing_signals", breakdown is not None)
    assert breakdown is not None
    _check("missing_signals", not breakdown.blame.available)
    _check("missing_signals", not breakdown.review.available)
    _check("missing_signals", 0.0 < owner.ownership_score <= 1.0)


@pytest.mark.integration
def test_golden_rename(git_repo: GitRepo) -> None:
    _belief("rename")
    _commit(
        git_repo,
        {"foo.py": "value = 1\n"},
        "alice writes foo",
        author="Alice",
        email=_ALICE,
        days_before=10,
    )
    git_repo.rename("foo.py", "bar.py")
    git_repo.commit(
        "rename foo to bar",
        author="Bob",
        email=_BOB,
        date=_stamp(4),
    )
    ownership = _analyze(git_repo, "rename", _AS_OF)
    handles = tuple(owner.handle for owner in _ranked(ownership, "bar.py"))
    _check("rename", _primary(ownership, "bar.py") == _BOB)
    _check("rename", _ALICE not in handles)
    _check("rename", "foo.py" not in ownership.paths)


@pytest.mark.integration
def test_golden_cadence(git_repo: GitRepo) -> None:
    _belief("cadence")
    git_repo.commit_files(
        {"stable.py": "value = 1\n"},
        "stable cadence",
        author="Alice",
        email=_ALICE,
        date=datetime(2024, 12, 1, 12, 0, tzinfo=UTC).isoformat(),
    )
    ownership = _analyze(git_repo, "cadence", _AS_OF)
    _check("cadence", "stable.py" not in ownership.paths)


@pytest.mark.integration
def test_golden_coauthor(git_repo: GitRepo) -> None:
    _belief("coauthor")
    git_repo.write("keep.py", "value = 0\n")
    git_repo.write("extra.py", "value = 2\n")
    git_repo.commit("seed", author="Alice", email=_ALICE, date=_stamp(6))
    git_repo.write("pair.py", "value = 1\n")
    git_repo.coauthored_commit(
        "add pair",
        author="Alice",
        email=_ALICE,
        date=_stamp(5),
        coauthor="Bob",
        coemail=_BOB,
    )
    ownership = _analyze(git_repo, "coauthor", _AS_OF)
    handles = tuple(owner.handle for owner in _ranked(ownership, "pair.py"))
    _check("coauthor", _ALICE in handles and _BOB in handles)
    alice = _person(ownership, "pair.py", _ALICE)
    bob = _person(ownership, "pair.py", _BOB)
    _check("coauthor", alice is not None and _blame_share(alice) > 0)
    _check("coauthor", bob is not None and _blame_share(bob) == 0.0)
