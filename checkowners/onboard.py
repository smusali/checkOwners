"""Experimental onboarding suggestions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from checkowners.expertise import path_matches_glob
from checkowners.graph import python_dependencies
from checkowners.models import Config, OwnerEntry, OwnershipMap
from checkowners.privacy import KNOWLEDGE_RISK_NOTICE

Complexity = Literal["easy", "medium", "hard"]
_SKIPPED_MENTORS = frozenset({"former", "inactive", "bot"})
_ALWAYS_UNAVAILABLE = ("code complexity", "test coverage", "documentation coverage")
_MENTOR_LIMIT = 3


@dataclass(frozen=True)
class OnboardingStep:
    order: int
    path: str
    reviewer: str
    complexity: Complexity
    description: str


@dataclass(frozen=True)
class OnboardingPath:
    target: str
    steps: tuple[OnboardingStep, ...]
    experimental: bool = True
    unavailable_signals: tuple[str, ...] = ()
    mentor_candidates: tuple[str, ...] = ()
    cycles: tuple[str, ...] = ()

    def to_markdown(self) -> str:
        heading = f"# Experimental onboarding suggestions for {self.target}"
        if not self.steps:
            return f"{heading}\n\nNo learning path could be built.\n\n{KNOWLEDGE_RISK_NOTICE}\n"
        lines = [heading, "", "These steps are inferred suggestions.", ""]
        if self.unavailable_signals:
            lines.append("Unavailable signals: " + ", ".join(self.unavailable_signals))
            lines.append("")
        if self.cycles:
            names = ", ".join(f"`{path}`" for path in self.cycles)
            lines.append(f"Cycle: {names}. Those steps use path order.")
            lines.append("")
        if self.mentor_candidates:
            lines.append("Mentor candidates: " + ", ".join(self.mentor_candidates))
            lines.append("")
        for step in self.steps:
            lines.append(
                f"- [ ] **Step {step.order}** ({step.complexity}) `{step.path}`: "
                f"review with {step.reviewer}. {step.description}"
            )
        lines.append("")
        lines.append(KNOWLEDGE_RISK_NOTICE)
        lines.append("")
        return "\n".join(lines)


def generate_onboarding_path(
    ownership: OwnershipMap,
    config: Config,
    *,
    target: str,
    max_steps: int = 15,
    repo_root: Path | None = None,
    dependency_edges: tuple[tuple[str, str], ...] | None = None,
) -> OnboardingPath:
    threshold = config.analysis.confidence_threshold
    qualified = _qualified(ownership, target, threshold)
    if not qualified:
        return _report(target, ())
    edges = _edges(tuple(qualified), repo_root, dependency_edges)
    rank, cycles = _ranks(tuple(qualified), edges)
    ordered = _order(qualified, rank, set(cycles), ownership, config)
    selected = ordered[:max_steps]
    selected_paths = {path for path, _owners in selected}
    prerequisites = _prerequisites(selected_paths, edges)
    used_reviewers: set[str] = set()
    steps: list[OnboardingStep] = []
    cyclic = set(cycles)
    for order, (path, owners) in enumerate(selected, start=1):
        reviewer = _pick_reviewer(owners, used_reviewers)
        used_reviewers.add(reviewer)
        owner_count = len(owners)
        steps.append(
            OnboardingStep(
                order=order,
                path=path,
                reviewer=reviewer,
                complexity=_complexity_for(order, len(selected), owner_count),
                description=_describe(owner_count, prerequisites.get(path, ()), path in cyclic),
            )
        )
    matched = any(_criticality(path, config) is not None for path, _owners in selected)
    return _report(
        target,
        tuple(steps),
        edges_found=bool(edges),
        criticality_matched=matched,
        mentors=_mentors(selected),
        cycles=cycles,
    )


def _report(
    target: str,
    steps: tuple[OnboardingStep, ...],
    *,
    edges_found: bool = False,
    criticality_matched: bool = False,
    mentors: tuple[str, ...] = (),
    cycles: tuple[str, ...] = (),
) -> OnboardingPath:
    return OnboardingPath(
        target=target,
        steps=steps,
        unavailable_signals=_unavailable(edges_found, criticality_matched),
        mentor_candidates=mentors,
        cycles=cycles,
    )


def _unavailable(edges_found: bool, criticality_matched: bool) -> tuple[str, ...]:
    names = list(_ALWAYS_UNAVAILABLE)
    if not edges_found:
        names.append("dependency order")
    if not criticality_matched:
        names.append("domain criticality")
    return tuple(names)


def _qualified(
    ownership: OwnershipMap,
    target: str,
    threshold: float,
) -> dict[str, tuple[OwnerEntry, ...]]:
    found: dict[str, tuple[OwnerEntry, ...]] = {}
    for path, path_ownership in ownership.paths.items():
        if not path_matches_glob(path, target):
            continue
        owners = tuple(owner for owner in path_ownership.owners if owner.confidence >= threshold)
        if owners:
            found[path] = owners
    return found


def _edges(
    paths: tuple[str, ...],
    repo_root: Path | None,
    explicit: tuple[tuple[str, str], ...] | None,
) -> tuple[tuple[str, str], ...]:
    known = set(paths)
    raw = explicit
    if raw is None and repo_root is not None:
        raw = python_dependencies(repo_root, paths)
    if raw is None:
        return ()
    return tuple(
        sorted(
            {
                (source, target)
                for source, target in raw
                if source in known and target in known and source != target
            }
        )
    )


def _ranks(
    paths: tuple[str, ...],
    edges: tuple[tuple[str, str], ...],
) -> tuple[dict[str, int], tuple[str, ...]]:
    incoming: dict[str, set[str]] = {path: set() for path in paths}
    outgoing: dict[str, set[str]] = {path: set() for path in paths}
    for source, target in edges:
        outgoing[target].add(source)
        incoming[source].add(target)
    rank: dict[str, int] = {}
    ready = sorted(path for path in paths if not incoming[path])
    level = 0
    while ready:
        nxt: list[str] = []
        for path in ready:
            rank[path] = level
            for dependent in sorted(outgoing[path]):
                incoming[dependent].discard(path)
                if not incoming[dependent] and dependent not in rank:
                    nxt.append(dependent)
        ready = sorted(set(nxt))
        level += 1
    cyclic = tuple(
        sorted(path for path in paths if path not in rank and _reaches_self(path, outgoing))
    )
    for path in paths:
        if path not in rank:
            rank[path] = level
    return rank, cyclic


def _reaches_self(start: str, outgoing: dict[str, set[str]]) -> bool:
    seen: set[str] = set()
    stack = sorted(outgoing.get(start, ()))
    while stack:
        node = stack.pop()
        if node == start:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(sorted(outgoing.get(node, ())))
    return False


def _order(
    qualified: dict[str, tuple[OwnerEntry, ...]],
    rank: dict[str, int],
    cyclic: set[str],
    ownership: OwnershipMap,
    config: Config,
) -> list[tuple[str, tuple[OwnerEntry, ...]]]:
    grouped: dict[int, list[str]] = {}
    for path in qualified:
        grouped.setdefault(rank[path], []).append(path)
    ordered: list[tuple[str, tuple[OwnerEntry, ...]]] = []
    for level in sorted(grouped):
        group = grouped[level]
        cyclic_paths = sorted(path for path in group if path in cyclic)
        rest = [path for path in group if path not in cyclic]
        rest.sort(key=lambda path: _signal_key(path, qualified[path], ownership, config))
        ordered.extend((path, qualified[path]) for path in (*cyclic_paths, *rest))
    return ordered


def _signal_key(
    path: str,
    owners: tuple[OwnerEntry, ...],
    ownership: OwnershipMap,
    config: Config,
) -> tuple[int, tuple[int, int], tuple[int, float], str]:
    events = ownership.paths[path].change_events
    weight = _criticality(path, config)
    event_key = (0, events) if events is not None else (1, 0)
    criticality_key = (0, weight) if weight is not None else (1, 0.0)
    return (len(owners), event_key, criticality_key, path)


def _criticality(path: str, config: Config) -> float | None:
    for pattern, weight in config.criticality:
        if path_matches_glob(path, pattern):
            return weight
    return None


def _prerequisites(
    paths: set[str],
    edges: tuple[tuple[str, str], ...],
) -> dict[str, tuple[str, ...]]:
    found: dict[str, set[str]] = {path: set() for path in paths}
    for source, target in edges:
        if source in paths and target in paths:
            found[source].add(target)
    return {path: tuple(sorted(items)) for path, items in found.items()}


def _describe(owner_count: int, prerequisites: tuple[str, ...], cyclic: bool) -> str:
    facts = [f"{owner_count} qualified owners"]
    if prerequisites:
        facts.insert(0, "depends on " + ", ".join(prerequisites))
    if cyclic:
        facts.append("cycle; path order")
    return ", ".join(facts)


def _mentors(
    selected: list[tuple[str, tuple[OwnerEntry, ...]]],
) -> tuple[str, ...]:
    shared: dict[str, int] = {}
    commits: dict[str, int] = {}
    for _path, owners in selected:
        for owner in owners:
            if owner.contributor_status in _SKIPPED_MENTORS:
                continue
            shared.setdefault(owner.handle, 0)
            commits[owner.handle] = commits.get(owner.handle, 0) + owner.commits
            if any(item.handle != owner.handle for item in owners):
                shared[owner.handle] += 1
    ranked = sorted(shared, key=lambda handle: (-shared[handle], commits[handle], handle))
    return tuple(ranked[:_MENTOR_LIMIT])


def _pick_reviewer(owners: tuple[OwnerEntry, ...], used: set[str]) -> str:
    for owner in owners:
        if owner.handle not in used:
            return owner.handle
    return owners[0].handle


def _complexity_for(order: int, total: int, qualified_owner_count: int) -> Complexity:
    third = max(1, total // 3)
    if order <= third:
        return "medium" if qualified_owner_count <= 1 else "easy"
    if order <= 2 * third:
        return "hard" if qualified_owner_count <= 1 else "medium"
    return "hard"
