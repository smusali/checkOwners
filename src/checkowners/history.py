"""Reading git history: commit log parsing, renames, and per-path contributions.

`get_commit_history` runs one `git log` and turns it into `CommitHistory`:
commits with their authors, co-authors, and the rename lineage of each path.
`aggregate_contributions` then reduces those commits to per-(path, author)
evidence, and the filters here drop bots, excluded paths, and the files
`.gitattributes` marks as generated.
"""

from __future__ import annotations

import hashlib
import re
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from functools import lru_cache
from itertools import pairwise
from pathlib import Path

from checkowners import git
from checkowners.config import Config
from checkowners.expertise import fnmatch_posix
from checkowners.identity import is_bot_identity, mechanical_authors
from checkowners.models import (
    ConfiguredMergeStrategy,
    LookbackDays,
    OwnerEntry,
    OwnershipMap,
    ReportedMergeStrategy,
)
from checkowners.scoring import Contribution, clamp
from checkowners.state import PathMove

_COMMIT_SENTINEL = "COMMIT_START"


BODY_START = "BODY_START"


BODY_END = "BODY_END"


_MERGE_COMMIT_MIN_PERCENT = 5


_SQUASH_SUBJECT_MIN_PERCENT = 50


_COAUTHOR_LINE = re.compile(r"(?i)^co-authored-by:\s*(.*?)\s*<([^<>\s]+@[^<>\s]+)>\s*$")


_CONTACT_EMAIL = re.compile(r"<([^<>\s]+@[^<>\s]+)>\s*$")


_SQUASH_SUBJECT = re.compile(r"\(#\d+\)\s*$")


@dataclass(frozen=True)
class RawCommit:
    """A single commit's parsed metadata."""

    author: str
    timestamp: datetime
    files: tuple[str, ...]
    coauthors: tuple[str, ...] = ()
    sha: str = ""
    subject: str = ""
    author_name: str = ""


@dataclass(frozen=True)
class ParsedCommit:
    sha: str
    author: str
    timestamp: datetime
    files: tuple[str, ...]
    contacts: tuple[tuple[str, str], ...]
    parent_count: int
    squash_subject: bool
    author_name: str = ""
    subject: str = ""
    moves: tuple[PathMove, ...] = ()


@dataclass(frozen=True)
class AuthorIndex:
    shas: dict[str, str] = field(default_factory=dict)
    names: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class CommitHistory:
    commits: list[RawCommit]
    merge_strategy: ReportedMergeStrategy
    co_author_count: int
    moves: dict[str, tuple[PathMove, ...]] = field(default_factory=dict)
    id_to_path: dict[str, str] = field(default_factory=dict)
    confidence: dict[str, float] = field(default_factory=dict)
    priors: dict[str, tuple[str, ...]] = field(default_factory=dict)
    logical_ids: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _GitAttributeRule:
    pattern: str
    relative_to: str
    values: tuple[tuple[str, bool], ...]


_LINGUIST_ATTRS = frozenset({"linguist-generated", "linguist-vendored"})


def insufficient_history(repo_root: Path, commits: list[RawCommit]) -> bool:
    if commits:
        return False
    stdout = git.git_stdout(repo_root, ["git", "rev-parse", "--verify", "HEAD"])
    if stdout is None:
        return False
    return git.FULL_SHA_RE.fullmatch(stdout.strip()) is not None


def rename_commit_shas(
    repo_root: Path,
    since: datetime | None,
    until: datetime,
    revision: str | None = None,
) -> frozenset[str]:
    argv = ["git", "log", "--diff-filter=R", "--name-status", "--pretty=format:%H"]
    if revision:
        argv.append(revision)
    if since is not None:
        argv.append(f"--since={since.isoformat()}")
    argv.append(f"--until={until.isoformat()}")
    stdout = git.git_stdout(repo_root, argv, paths=True)
    if not stdout:
        return frozenset()
    found: set[str] = set()
    current = ""
    for line in stdout.splitlines():
        stripped = line.strip()
        if git.FULL_SHA_RE.fullmatch(stripped):
            current = stripped
            continue
        if current and git.stdout_has_rename(line):
            found.add(current)
    return frozenset(found)


def window_has_renames(repo_root: Path, since: datetime | None, until: datetime) -> bool:
    argv = [
        "git",
        "log",
        "--diff-filter=R",
        "--name-status",
        "--pretty=format:",
    ]
    if since is not None:
        argv.append(f"--since={since.isoformat()}")
    argv.append(f"--until={until.isoformat()}")
    stdout = git.git_stdout(repo_root, argv, paths=True)
    if not stdout:
        return False
    return git.stdout_has_rename(stdout)


def lookback_start(lookback: LookbackDays, when: datetime) -> datetime | None:
    """Return the window start for `lookback` at `when`, or None when adaptive."""
    if lookback == "adaptive":
        return None
    return when - timedelta(days=lookback)


def coauthor_contacts(body: str) -> tuple[tuple[str, str], ...]:
    """Return unique `(name, email)` pairs from valid Co-authored-by lines in `body`."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in body.splitlines():
        match = _COAUTHOR_LINE.match(line.strip())
        if match is None:
            continue
        email = match.group(2)
        key = email.casefold()
        if key in seen:
            continue
        seen.add(key)
        found.append((match.group(1).strip(), email))
    return tuple(found)


def canonical_emails(
    repo_root: Path,
    contacts: tuple[tuple[str, str], ...],
    *,
    enabled: bool,
) -> tuple[str, ...]:
    """Return one email per contact in `contacts`. `enabled` applies `.mailmap`."""
    raw = tuple(email for _name, email in contacts)
    if not enabled or not contacts:
        return raw
    argv = ["git", "check-mailmap"]
    for name, email in contacts:
        argv.append(f"{name} <{email}>" if name else f"<{email}>")
    result = git.run_git(repo_root, argv, check=False)
    if result.returncode != 0:
        return raw
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != len(contacts):
        return raw
    parsed: list[str] = []
    for line, email in zip(lines, raw, strict=True):
        match = _CONTACT_EMAIL.search(line)
        parsed.append(match.group(1) if match is not None else email)
    return tuple(parsed)


def assign_coauthors(
    repo_root: Path,
    authors: tuple[str, ...],
    contact_lists: tuple[tuple[tuple[str, str], ...], ...],
    *,
    use_mailmap: bool,
    enabled: bool,
) -> tuple[tuple[str, ...], ...]:
    """Return credited co-author emails for each author, excluding that author."""
    empty = tuple(() for _author in authors)
    if not enabled:
        return empty
    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for contacts in contact_lists:
        for name, email in contacts:
            key = email.casefold()
            if key in seen:
                continue
            seen.add(key)
            unique.append((name, email))
    canonical = canonical_emails(repo_root, tuple(unique), enabled=use_mailmap)
    mapped = {
        email.casefold(): canonical_email
        for (_name, email), canonical_email in zip(unique, canonical, strict=True)
    }
    credited: list[tuple[str, ...]] = []
    for author, contacts in zip(authors, contact_lists, strict=True):
        author_key = author.casefold()
        people: list[str] = []
        used: set[str] = set()
        for _name, email in contacts:
            person = mapped.get(email.casefold(), email)
            key = person.casefold()
            if key == author_key or key in used:
                continue
            used.add(key)
            people.append(person)
        credited.append(tuple(people))
    return tuple(credited)


def get_commit_history(
    repo_root: Path,
    since: datetime | None,
    as_of: datetime,
    *,
    use_mailmap: bool = True,
    pathspec: tuple[str, ...] | None = None,
    count_co_authors: bool = True,
    merge_strategy: ConfiguredMergeStrategy = "auto",
    tip: str | None = None,
    follow_renames: bool = True,
    rename_similarity: int = 50,
) -> CommitHistory:
    """Return parsed commits from `since` until `as_of`. `since` None omits the lower bound."""
    records = parsed_commits(
        repo_root,
        since,
        as_of,
        use_mailmap=use_mailmap,
        name_only=not follow_renames,
        pathspec=pathspec,
        tip=tip,
        track_renames=follow_renames,
        rename_similarity=rename_similarity,
    )
    history = history_from_records(
        repo_root,
        records,
        use_mailmap=use_mailmap,
        count_co_authors=count_co_authors,
        merge_strategy=merge_strategy,
    )
    return finish_history(history, follow_renames=follow_renames)


def _history_log_format(use_mailmap: bool) -> str:
    author_fmt = "%aN%x09%aE" if use_mailmap else "%an%x09%ae"
    prefix = f"--format={_COMMIT_SENTINEL}%n%H%n{author_fmt}%n%cI%n%P%n%s"
    trailer = f"%n{BODY_START}%n%b%n{BODY_END}"
    return prefix + trailer


def parsed_commits(
    repo_root: Path,
    since: datetime | None,
    as_of: datetime,
    *,
    use_mailmap: bool,
    name_only: bool,
    pathspec: tuple[str, ...] | None = None,
    revisions: tuple[str, ...] = (),
    tip: str | None = None,
    track_renames: bool = False,
    rename_similarity: int = 50,
) -> list[ParsedCommit]:
    argv = ["git", "log", git.mailmap_flag(use_mailmap), _history_log_format(use_mailmap)]
    if track_renames and pathspec is not None and len(pathspec) == 1 and not revisions:
        argv.append("--follow")
    if track_renames:
        argv.extend(
            [
                "--name-status",
                "-M",
                "-C",
                f"--find-renames={rename_similarity}%",
                f"--find-copies={rename_similarity}%",
            ]
        )
    elif name_only:
        argv.append("--name-only")
    if revisions:
        argv.append("--no-walk")
        argv.extend(revisions)
    else:
        if tip is not None:
            argv.append(tip)
        if since is not None:
            argv.append(f"--since={since.isoformat()}")
        argv.append(f"--until={as_of.isoformat()}")
        if pathspec:
            argv.append("--")
            argv.extend(pathspec)
    result = git.run_git(repo_root, argv, check=True, paths=name_only or track_renames)
    return _parse_log(result.stdout)


def history_from_records(
    repo_root: Path,
    records: list[ParsedCommit],
    *,
    use_mailmap: bool,
    count_co_authors: bool,
    merge_strategy: ConfiguredMergeStrategy,
) -> CommitHistory:
    file_records = [record for record in records if record.files]
    credited = assign_coauthors(
        repo_root,
        tuple(record.author for record in file_records),
        tuple(record.contacts for record in file_records),
        use_mailmap=use_mailmap,
        enabled=count_co_authors,
    )
    commits = [
        RawCommit(
            author=record.author,
            timestamp=record.timestamp,
            files=record.files,
            coauthors=coauthors,
            sha=record.sha,
            subject=record.subject,
            author_name=record.author_name,
        )
        for record, coauthors in zip(file_records, credited, strict=True)
    ]
    moves = {record.sha: record.moves for record in records if record.sha and record.moves}
    return CommitHistory(
        commits=commits,
        merge_strategy=applied_strategy(merge_strategy, detect_strategy(records)),
        co_author_count=sum(len(coauthors) for coauthors in credited),
        moves=moves,
    )


def fetch_commit_files(
    repo_root: Path,
    shas: list[str],
    *,
    use_mailmap: bool,
    as_of: datetime,
    follow_renames: bool = False,
    rename_similarity: int = 50,
) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[PathMove, ...]]]:
    found: dict[str, tuple[str, ...]] = {}
    moves: dict[str, tuple[PathMove, ...]] = {}
    for start in range(0, len(shas), git.SHA_BATCH):
        batch = tuple(shas[start : start + git.SHA_BATCH])
        records = parsed_commits(
            repo_root,
            None,
            as_of,
            use_mailmap=use_mailmap,
            name_only=not follow_renames,
            revisions=batch,
            track_renames=follow_renames,
            rename_similarity=rename_similarity,
        )
        for record in records:
            found[record.sha] = record.files
            if record.moves:
                moves[record.sha] = record.moves
    return found, moves


def parse_log_output(stdout: str) -> list[RawCommit]:
    return [
        RawCommit(author=record.author, timestamp=record.timestamp, files=record.files)
        for record in _parse_log(stdout)
        if record.files
    ]


def _parse_log(stdout: str) -> list[ParsedCommit]:
    if not stdout.strip():
        return []
    records: list[ParsedCommit] = []
    for chunk in stdout.split(_COMMIT_SENTINEL):
        parsed = parse_commit_chunk(chunk)
        if parsed is not None:
            records.append(parsed)
    return records


def parse_commit_chunk(chunk: str) -> ParsedCommit | None:
    marker = f"\n{BODY_START}\n"
    end_marker = f"\n{BODY_END}"
    start = chunk.find(marker)
    if start < 0:
        return None
    header = chunk[:start].lstrip("\n")
    rest = chunk[start + len(marker) :]
    end = rest.rfind(end_marker)
    if end < 0:
        return None
    body = rest[:end]
    files_blob = rest[end + len(end_marker) :]
    lines = header.splitlines()
    if len(lines) < 4:
        return None
    sha = lines[0].strip()
    author_name, author = split_author(lines[1].strip())
    timestamp = git.parse_timestamp(lines[2].strip())
    if timestamp is None or not author or git.FULL_SHA_RE.fullmatch(sha) is None:
        return None
    subject = lines[4] if len(lines) > 4 else ""
    files, moves = parse_status_paths(files_blob)
    return ParsedCommit(
        sha=sha,
        author=author,
        timestamp=timestamp,
        files=files,
        contacts=coauthor_contacts(body),
        parent_count=len(lines[3].split()),
        squash_subject=_SQUASH_SUBJECT.search(subject) is not None,
        author_name=author_name,
        subject=subject,
        moves=moves,
    )


def split_author(line: str) -> tuple[str, str]:
    if "\t" not in line:
        return "", line
    name, email = line.split("\t", 1)
    return name.strip(), email.strip()


def author_index(commits: list[RawCommit]) -> AuthorIndex:
    shas: dict[str, str] = {}
    names: dict[str, set[str]] = {}
    for commit in commits:
        if commit.sha:
            shas.setdefault(commit.author, commit.sha)
        if commit.author_name:
            names.setdefault(commit.author, set()).add(commit.author_name)
    return AuthorIndex(
        shas=shas,
        names={author: tuple(sorted(values)) for author, values in names.items()},
    )


def cap_mechanical_authors(
    contributions: dict[str, dict[str, Contribution]],
    commits: list[RawCommit],
) -> dict[str, dict[str, Contribution]]:
    mechanical = mechanical_authors(tuple((commit.author, commit.subject) for commit in commits))
    if not mechanical:
        return contributions
    return {
        path: {
            author: replace(contrib, credit=min(contrib.credit, 1.0))
            if author in mechanical
            else contrib
            for author, contrib in authors.items()
        }
        for path, authors in contributions.items()
    }


def detect_strategy(records: list[ParsedCommit]) -> ReportedMergeStrategy:
    observed = len(records)
    if observed == 0:
        return "rebase"
    merges = sum(1 for record in records if record.parent_count >= 2)
    if _share_at_least(merges, observed, _MERGE_COMMIT_MIN_PERCENT):
        return "merge"
    rest = observed - merges
    squash_subjects = sum(
        1 for record in records if record.parent_count < 2 and record.squash_subject
    )
    if _share_at_least(squash_subjects, rest, _SQUASH_SUBJECT_MIN_PERCENT):
        return "squash"
    return "rebase"


def _share_at_least(part: int, whole: int, percent: int) -> bool:
    return whole > 0 and part * 100 >= whole * percent


def applied_strategy(
    configured: ConfiguredMergeStrategy,
    detected: ReportedMergeStrategy,
) -> ReportedMergeStrategy:
    if configured == "auto":
        return detected
    return configured


def _status_score(status: str) -> int:
    digits = "".join(char for char in status if char.isdigit())
    if not digits:
        return 100
    return max(0, min(100, int(digits)))


def parse_status_paths(blob: str) -> tuple[tuple[str, ...], tuple[PathMove, ...]]:
    files: list[str] = []
    moves: list[PathMove] = []
    for raw in blob.splitlines():
        line = raw.strip()
        if not line:
            continue
        if "\t" not in line:
            files.append(line)
            continue
        status, remainder = line.split("\t", 1)
        kind = status[:1]
        if kind in {"R", "C"} and "\t" in remainder:
            source, dest = remainder.split("\t", 1)
            files.append(dest)
            moves.append(PathMove(kind=kind, score=_status_score(status), source=source, dest=dest))
            continue
        if kind in {"A", "M", "D", "T", "U"}:
            files.append(remainder.split("\t")[-1])
            continue
        files.append(line)
    return tuple(files), tuple(moves)


def logical_id(path: str, taken: set[str], nonce: str) -> str:
    digest = hashlib.sha256(path.encode()).hexdigest()[:16]
    if digest not in taken:
        return digest
    return hashlib.sha256(f"{path}\n{nonce}".encode()).hexdigest()[:16]


def follow_path(path: str, forward: Mapping[str, str]) -> str:
    seen: set[str] = set()
    while path in forward and path not in seen:
        seen.add(path)
        path = forward[path]
    return path


def _parent_dir(path: str) -> str:
    slash = path.rfind("/")
    if slash < 0:
        return ""
    return path[:slash]


def common_directory(paths: list[str]) -> str:
    parents = [_parent_dir(path) for path in paths]
    if not parents or any(parent == "" for parent in parents):
        return ""
    common = parents[0]
    for parent in parents[1:]:
        while parent != common and not parent.startswith(f"{common}/"):
            slash = common.rfind("/")
            if slash < 0:
                return ""
            common = common[:slash]
        if not common:
            return ""
    return common


def collapse_directory_moves(
    moves: tuple[PathMove, ...],
    living: Mapping[str, str],
) -> tuple[PathMove, ...]:
    renames = [move for move in moves if move.kind == "R"]
    if len(renames) < 2:
        return moves
    old_prefix = common_directory([move.source for move in renames])
    new_prefix = common_directory([move.dest for move in renames])
    if not old_prefix or not new_prefix or old_prefix == new_prefix:
        return moves
    old_root = f"{old_prefix}/"
    new_root = f"{new_prefix}/"
    sources: set[str] = set()
    for move in renames:
        if move.source[len(old_root) :] != move.dest[len(new_root) :]:
            return moves
        sources.add(move.source)
    for path in living:
        if path.startswith(old_root) and path not in sources:
            return moves
    others = tuple(move for move in moves if move.kind != "R")
    score = min(move.score for move in renames)
    return (*others, PathMove(kind="D", score=score, source=old_prefix, dest=new_prefix))


def apply_renames(
    living: dict[str, str],
    origin: dict[str, str],
    priors: dict[str, list[str]],
    confidence: dict[str, int],
    moves: tuple[PathMove, ...],
    nonce: str,
) -> None:
    sources: dict[str, str] = {}
    for move in moves:
        if move.kind == "R" and move.source in living:
            sources[move.source] = living.pop(move.source)
    for move in moves:
        if move.kind != "R":
            continue
        logical = sources.get(move.source)
        if logical is None:
            taken = set(origin)
            logical = logical_id(move.source, taken, nonce)
            origin[logical] = move.source
            confidence[logical] = 100
        priors.setdefault(logical, []).append(move.source)
        confidence[logical] = min(confidence.get(logical, 100), move.score)
        living[move.dest] = logical


def _apply_directory_move(
    living: dict[str, str],
    priors: dict[str, list[str]],
    confidence: dict[str, int],
    move: PathMove,
) -> None:
    prefix = f"{move.source}/"
    dest_prefix = f"{move.dest}/"
    matched = [path for path in living if path.startswith(prefix)]
    for path in matched:
        logical = living.pop(path)
        dest = dest_prefix + path[len(prefix) :]
        priors.setdefault(logical, []).append(path)
        confidence[logical] = min(confidence.get(logical, 100), move.score)
        living[dest] = logical


def _apply_copy(
    living: dict[str, str],
    origin: dict[str, str],
    priors: dict[str, list[str]],
    confidence: dict[str, int],
    move: PathMove,
    nonce: str,
) -> str | None:
    source_id = living.get(move.source)
    taken = set(origin)
    new_id = logical_id(move.dest, taken, nonce)
    living[move.dest] = new_id
    origin[new_id] = move.dest
    priors.setdefault(new_id, []).append(move.source)
    confidence[new_id] = move.score
    return source_id


def with_lineage(history: CommitHistory) -> CommitHistory:
    ordered = sorted(history.commits, key=lambda commit: (commit.timestamp, commit.sha))
    living: dict[str, str] = {}
    origin: dict[str, str] = {}
    priors: dict[str, list[str]] = {}
    confidence: dict[str, int] = {}
    files_by_sha: dict[str, tuple[str, ...]] = {}
    applied: dict[str, tuple[PathMove, ...]] = {}
    for commit in ordered:
        raw_moves = history.moves.get(commit.sha, ())
        commit_moves = collapse_directory_moves(raw_moves, living)
        applied[commit.sha] = commit_moves
        apply_renames(living, origin, priors, confidence, commit_moves, commit.sha)
        for move in commit_moves:
            if move.kind == "D":
                _apply_directory_move(living, priors, confidence, move)
        copied: list[tuple[str, str]] = []
        for move in commit_moves:
            if move.kind != "C":
                continue
            source_id = _apply_copy(living, origin, priors, confidence, move, commit.sha)
            new_id = living.get(move.dest)
            if source_id is not None and new_id is not None:
                copied.append((new_id, source_id))
        identities: list[str] = []
        for path in commit.files:
            if path not in living:
                logical = logical_id(path, set(origin), commit.sha)
                living[path] = logical
                origin[logical] = path
                confidence.setdefault(logical, 100)
            identities.append(living[path])
        files_by_sha[commit.sha] = tuple(dict.fromkeys(identities))
        for new_id, source_id in copied:
            for sha, file_ids in files_by_sha.items():
                if source_id in file_ids and new_id not in file_ids:
                    files_by_sha[sha] = (*file_ids, new_id)
    id_to_path = {logical: path for path, logical in living.items()}
    rewritten: list[RawCommit] = []
    for commit in history.commits:
        rewritten.append(replace(commit, files=files_by_sha.get(commit.sha, ())))
    path_confidence = {path: confidence.get(logical, 100) / 100 for path, logical in living.items()}
    path_priors = {
        path: bounded_priors(priors.get(logical, []), path) for path, logical in living.items()
    }
    path_ids = dict(living)
    return replace(
        history,
        commits=rewritten,
        moves={sha: moves for sha, moves in applied.items() if moves},
        id_to_path=id_to_path,
        confidence=path_confidence,
        priors=path_priors,
        logical_ids=path_ids,
    )


def bounded_priors(previous: list[str], current: str) -> tuple[str, ...]:
    seen: set[str] = set()
    names: list[str] = []
    for item in previous:
        if item == current or item in seen:
            break
        seen.add(item)
        names.append(item)
    return tuple(names)


def finish_history(history: CommitHistory, *, follow_renames: bool) -> CommitHistory:
    if not follow_renames:
        return history
    return with_lineage(history)


def current_commits(history: CommitHistory) -> list[RawCommit]:
    if not history.id_to_path:
        return history.commits
    mapped: list[RawCommit] = []
    for commit in history.commits:
        paths = tuple(
            dict.fromkeys(
                history.id_to_path[item] for item in commit.files if item in history.id_to_path
            )
        )
        mapped.append(replace(commit, files=paths))
    return mapped


def remap_contributions(
    contributions: dict[str, dict[str, Contribution]],
    id_to_path: Mapping[str, str],
) -> dict[str, dict[str, Contribution]]:
    if not id_to_path:
        return contributions
    remapped: dict[str, dict[str, Contribution]] = {}
    for logical, authors in contributions.items():
        current = id_to_path.get(logical)
        if current is None:
            continue
        remapped[current] = authors
    return remapped


def path_lineage(
    repo_root: Path,
    target: str,
    config: Config,
    as_of: datetime,
) -> tuple[tuple[str, ...], str, float]:
    """Return prior names, logical id, and rename confidence for `target`."""
    history = get_commit_history(
        repo_root,
        lookback_start(config.analysis.lookback_days, as_of),
        as_of,
        use_mailmap=config.git.use_mailmap,
        pathspec=(target,),
        count_co_authors=config.git.count_co_authors,
        merge_strategy=config.git.merge_strategy,
        follow_renames=True,
        rename_similarity=config.analysis.rename_similarity,
    )
    return (
        history.priors.get(target, ()),
        history.logical_ids.get(target, ""),
        history.confidence.get(target, 1.0),
    )


def scale_lineage_confidence(
    ownership: OwnershipMap,
    confidence: Mapping[str, float],
) -> OwnershipMap:
    factors = {path: value for path, value in confidence.items() if value < 1.0}
    if not factors:
        return ownership

    def scale(path: str, owner: OwnerEntry) -> OwnerEntry:
        factor = factors.get(path)
        if factor is None:
            return owner
        return replace(owner, evidence_quality=clamp(owner.evidence_quality * factor))

    paths = {
        path: replace(
            path_ownership,
            owners=tuple(scale(path, owner) for owner in path_ownership.owners),
            candidates=tuple(scale(path, owner) for owner in path_ownership.candidates),
            scored_owners=tuple(scale(path, owner) for owner in path_ownership.scored_owners),
        )
        for path, path_ownership in ownership.paths.items()
    }
    return replace(ownership, paths=paths)


def aggregate_contributions(
    commits: list[RawCommit],
    *,
    co_author_weight: float = 1.0,
) -> dict[str, dict[str, Contribution]]:
    """Aggregate per-(path, author) counts, credit, cadence, and later commits."""
    events: dict[str, list[tuple[str, datetime, float, str]]] = {}
    for commit in commits:
        people = [
            (commit.author, 1.0),
            *((email, co_author_weight) for email in commit.coauthors),
        ]
        for file_path in commit.files:
            bucket = events.setdefault(file_path, [])
            for author, weight in people:
                bucket.append((author, commit.timestamp, weight, commit.sha))
    result: dict[str, dict[str, Contribution]] = {}
    for path in sorted(events):
        path_events = events[path]
        counts: dict[str, int] = {}
        credits_by_author: dict[str, float] = {}
        latest: dict[str, datetime] = {}
        latest_sha: dict[str, str] = {}
        for author, timestamp, weight, sha in path_events:
            counts[author] = counts.get(author, 0) + 1
            credits_by_author[author] = credits_by_author.get(author, 0.0) + weight
            prior = latest.get(author)
            if prior is None or timestamp > prior:
                latest[author] = timestamp
                latest_sha[author] = sha
        cadence = cadence_days([timestamp for _author, timestamp, _weight, _sha in path_events])
        result[path] = {
            author: Contribution(
                commits=commits_n,
                last_commit=latest[author],
                later_commits=sum(
                    1
                    for other, timestamp, _weight, _sha in path_events
                    if other != author and timestamp > latest[author]
                ),
                cadence_days=cadence,
                credit=credits_by_author[author],
                last_sha=latest_sha[author],
            )
            for author, commits_n in sorted(counts.items())
        }
    return result


_COCHANGE_COMMIT_CAP = 20


def path_activity(
    commits: list[RawCommit],
    kept: frozenset[str],
) -> dict[str, tuple[int, float | None, float | None]]:
    """Return `(change_events, cadence_days, cochange_degree)` for each path in `kept`."""
    counts = dict.fromkeys(kept, 0)
    timestamps: dict[str, list[datetime]] = {path: [] for path in kept}
    partners: dict[str, set[str]] = {path: set() for path in kept}
    for commit in commits:
        seen: list[str] = []
        seen_set: set[str] = set()
        for file_path in commit.files:
            if file_path in kept and file_path not in seen_set:
                seen.append(file_path)
                seen_set.add(file_path)
        for file_path in seen:
            counts[file_path] += 1
            timestamps[file_path].append(commit.timestamp)
        if len(set(commit.files)) > _COCHANGE_COMMIT_CAP or len(seen) < 2:
            continue
        for file_path in seen:
            partners[file_path].update(other for other in seen if other != file_path)
    degrees = {path: len(partners[path]) for path in kept}
    max_degree = max(degrees.values(), default=0)
    activity: dict[str, tuple[int, float | None, float | None]] = {}
    for path in kept:
        degree = degrees[path]
        coupling = degree / max_degree if max_degree > 0 and degree > 0 else None
        activity[path] = (counts[path], cadence_days(timestamps[path]), coupling)
    return activity


def cadence_days(timestamps: list[datetime]) -> float | None:
    unique = sorted(set(timestamps))
    if len(unique) < 2:
        return None
    gaps = [(later - earlier).total_seconds() / 86400.0 for earlier, later in pairwise(unique)]
    return statistics.median(gaps)


def filter_excluded(
    contributions: dict[str, dict[str, Contribution]],
    exclude_patterns: tuple[str, ...],
) -> dict[str, dict[str, Contribution]]:
    return {
        path: authors
        for path, authors in contributions.items()
        if not path_is_excluded(path, exclude_patterns)
    }


def filter_nonexistent(
    contributions: dict[str, dict[str, Contribution]],
    repo_root: Path,
) -> dict[str, dict[str, Contribution]]:
    return {path: authors for path, authors in contributions.items() if (repo_root / path).exists()}


def filter_bot_authors(
    contributions: dict[str, dict[str, Contribution]],
    extra: Sequence[str] = (),
) -> dict[str, dict[str, Contribution]]:
    result: dict[str, dict[str, Contribution]] = {}
    for path, authors in contributions.items():
        humans = {a: c for a, c in authors.items() if not is_bot_identity(a, extra)}
        if humans:
            result[path] = humans
    return result


def filter_unqualified(
    contributions: dict[str, dict[str, Contribution]],
    min_commits: int,
) -> dict[str, dict[str, Contribution]]:
    return {
        path: authors
        for path, authors in contributions.items()
        if any(contrib.commits >= min_commits for contrib in authors.values())
    }


def path_is_excluded(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch_posix(path, pattern) for pattern in patterns)


def _parse_attribute_token(token: str) -> tuple[str, bool] | None:
    if token.startswith("-"):
        name = token[1:]
        return (name, False) if name in _LINGUIST_ATTRS else None
    if "=" in token:
        name, _, value = token.partition("=")
        if name not in _LINGUIST_ATTRS:
            return None
        return (name, value.lower() != "false")
    if token in _LINGUIST_ATTRS:
        return (token, True)
    return None


def parse_gitattributes(content: str, relative_to: str) -> tuple[_GitAttributeRule, ...]:
    rules: list[_GitAttributeRule] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        values: list[tuple[str, bool]] = []
        for token in parts[1:]:
            parsed = _parse_attribute_token(token)
            if parsed is not None:
                values.append(parsed)
        if values:
            rules.append(
                _GitAttributeRule(
                    pattern=parts[0],
                    relative_to=relative_to,
                    values=tuple(values),
                )
            )
    return tuple(rules)


def _translate_gitattributes_segment(segment: str) -> str:
    out: list[str] = []
    for char in segment:
        if char == "*":
            out.append(r"[^/]*")
        elif char == "?":
            out.append(r"[^/]")
        else:
            out.append(re.escape(char))
    return "".join(out)


def translate_gitattributes_pattern(pattern: str) -> str:
    p = pattern.rstrip("/") if pattern.endswith("/") else pattern
    anchored = p.startswith("/")
    p = p.lstrip("/")
    if "/" in p:
        anchored = True
    if not p:
        return r"\A\Z"
    segments = p.split("/")
    parts: list[str] = []
    for index, segment in enumerate(segments):
        if segment == "**":
            parts.append(r"(?:[^/]+/)*" if index < len(segments) - 1 else r".*")
            continue
        escaped = _translate_gitattributes_segment(segment)
        parts.append(escaped + ("/" if index < len(segments) - 1 else ""))
    body = "".join(parts)
    prefix = r"" if anchored else r"(?:[^/]+/)*"
    return prefix + body + r"\Z"


@lru_cache(maxsize=4096)
def _compile_gitattributes_pattern(pattern: str) -> re.Pattern[str]:
    return re.compile(translate_gitattributes_pattern(pattern))


def gitattributes_pattern_matches(pattern: str, path: str, relative_to: str) -> bool:
    if relative_to:
        prefix = f"{relative_to}/"
        if not path.startswith(prefix):
            return False
        local = path[len(prefix) :]
    else:
        local = path
    if not local:
        return False
    return _compile_gitattributes_pattern(pattern).match(local) is not None


def _ancestor_dirs(path: str) -> tuple[str, ...]:
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    if not parent:
        return ("",)
    dirs = [""]
    current = ""
    for part in parent.split("/"):
        current = f"{current}/{part}" if current else part
        dirs.append(current)
    return tuple(dirs)


def _gitattributes_rules_for(
    repo_root: Path,
    rel_dir: str,
    cache: dict[str, tuple[_GitAttributeRule, ...]],
) -> tuple[_GitAttributeRule, ...]:
    if rel_dir in cache:
        return cache[rel_dir]
    attrs_path = repo_root / rel_dir / ".gitattributes" if rel_dir else repo_root / ".gitattributes"
    try:
        rules = (
            parse_gitattributes(attrs_path.read_text(encoding="utf-8"), rel_dir)
            if attrs_path.is_file()
            else ()
        )
    except OSError:
        rules = ()
    cache[rel_dir] = rules
    return rules


def path_has_linguist_attr(
    path: str,
    repo_root: Path,
    cache: dict[str, tuple[_GitAttributeRule, ...]],
) -> bool:
    flags: dict[str, bool] = {}
    for rel_dir in _ancestor_dirs(path):
        for rule in _gitattributes_rules_for(repo_root, rel_dir, cache):
            if not gitattributes_pattern_matches(rule.pattern, path, rule.relative_to):
                continue
            for name, is_set in rule.values:
                flags[name] = is_set
    return flags.get("linguist-generated") is True or flags.get("linguist-vendored") is True


def linguist_excluded_paths(repo_root: Path, paths: Iterable[str]) -> frozenset[str]:
    pending = tuple(paths)
    if not pending:
        return frozenset()
    cache: dict[str, tuple[_GitAttributeRule, ...]] = {}
    return frozenset(path for path in pending if path_has_linguist_attr(path, repo_root, cache))
