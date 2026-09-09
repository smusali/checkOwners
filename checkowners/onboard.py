"""Onboarding path generator.

Walks the knowledge graph for a given starting area and emits an ordered
learning path from broad-ownership files (many qualified owners, low
risk) to deep-expertise files (few qualified owners, high concentration).
Each step nominates a reviewer and an estimated complexity tier.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from checkowners.busfactor import compute_qualified_owners, format_qualified_owner_count
from checkowners.expertise import path_matches_glob
from checkowners.models import Config, OwnerEntry, OwnershipMap

Complexity = Literal["easy", "medium", "hard"]


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

    def to_markdown(self) -> str:
        if not self.steps:
            return f"# Onboarding path for {self.target}\n\nNo learning path could be built.\n"
        lines = [f"# Onboarding path for {self.target}", ""]
        for step in self.steps:
            lines.append(
                f"- [ ] **Step {step.order}** ({step.complexity}) `{step.path}`: "
                f"review with {step.reviewer}. {step.description}"
            )
        lines.append("")
        return "\n".join(lines)


def generate_onboarding_path(
    ownership: OwnershipMap,
    config: Config,
    *,
    target: str,
    max_steps: int = 15,
) -> OnboardingPath:
    """Build an ordered learning path for `target`."""
    threshold = config.analysis.confidence_threshold
    matching_paths = _matching_paths(ownership, target)
    if not matching_paths:
        return OnboardingPath(target=target, steps=())
    owners_report = compute_qualified_owners(ownership, config, target=target)
    count_by_path = {entry.path: entry.qualified_owner_count for entry in owners_report.entries}
    cap = config.analysis.top_n_owners
    scored: list[tuple[str, tuple[OwnerEntry, ...], int]] = []
    for path in matching_paths:
        po = ownership.paths[path]
        qualified = tuple(o for o in po.owners if o.confidence >= threshold)
        if not qualified:
            continue
        owner_count = count_by_path.get(path, len(qualified))
        scored.append((path, qualified, owner_count))
    scored.sort(key=lambda item: (-item[2], item[0]))
    if not scored:
        return OnboardingPath(target=target, steps=())
    selected = scored[:max_steps]
    used_reviewers: set[str] = set()
    steps: list[OnboardingStep] = []
    for order, (path, owners, owner_count) in enumerate(selected, start=1):
        reviewer = _pick_reviewer(owners, used_reviewers)
        used_reviewers.add(reviewer)
        complexity = _complexity_for(order, len(selected), owner_count)
        description = _describe(path, owner_count, len(owners), cap)
        steps.append(
            OnboardingStep(
                order=order,
                path=path,
                reviewer=reviewer,
                complexity=complexity,
                description=description,
            )
        )
    return OnboardingPath(target=target, steps=tuple(steps))


def _matching_paths(ownership: OwnershipMap, target: str) -> list[str]:
    return [path for path in ownership.paths if path_matches_glob(path, target)]


def _pick_reviewer(owners: tuple[OwnerEntry, ...], used: set[str]) -> str:
    for owner in owners:
        if owner.handle not in used:
            return owner.handle
    return owners[0].handle


def _complexity_for(order: int, total: int, qualified_owner_count: int) -> Complexity:
    """Tier a step's complexity; single-owner paths are never 'easy'."""
    third = max(1, total // 3)
    if order <= third:
        return "medium" if qualified_owner_count <= 1 else "easy"
    if order <= 2 * third:
        return "hard" if qualified_owner_count <= 1 else "medium"
    return "hard"


def _describe(path: str, qualified_owner_count: int, owner_count: int, cap: int) -> str:
    suffix = ""
    if qualified_owner_count <= 1:
        suffix = f" (deep expertise; {format_qualified_owner_count(qualified_owner_count, cap)})"
    elif owner_count >= 3:
        suffix = " (broad ownership; many reviewers available)"
    return f"Study `{path}`{suffix}."
