"""Onboarding path generator.

Walks the knowledge graph for a given starting area and emits an ordered
learning path from broad-ownership files (many qualified owners, low
risk) to deep-expertise files (few qualified owners, high concentration).
Each step nominates a reviewer and an estimated complexity tier.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from checkowners.busfactor import compute_bus_factor
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
    bus_report = compute_bus_factor(ownership, config, target=target)
    bus_by_path = {entry.path: entry.bus_factor for entry in bus_report.entries}
    scored: list[tuple[str, tuple[OwnerEntry, ...], int]] = []
    for path in matching_paths:
        po = ownership.paths[path]
        qualified = tuple(o for o in po.owners if o.confidence >= threshold)
        if not qualified:
            continue
        bus_factor = bus_by_path.get(path, len(qualified))
        scored.append((path, qualified, bus_factor))
    # Order broad -> deep: higher bus factor first, then alphabetical.
    scored.sort(key=lambda item: (-item[2], item[0]))
    if not scored:
        return OnboardingPath(target=target, steps=())
    selected = scored[:max_steps]
    used_reviewers: set[str] = set()
    steps: list[OnboardingStep] = []
    for order, (path, owners, bus_factor) in enumerate(selected, start=1):
        reviewer = _pick_reviewer(owners, used_reviewers)
        used_reviewers.add(reviewer)
        complexity = _complexity_for(order, len(selected), bus_factor)
        description = _describe(path, bus_factor, len(owners))
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


def _complexity_for(order: int, total: int, bus_factor: int) -> Complexity:
    """Tier a step's complexity; bus_factor <= 1 paths are never 'easy'."""
    third = max(1, total // 3)
    if order <= third:
        return "medium" if bus_factor <= 1 else "easy"
    if order <= 2 * third:
        return "hard" if bus_factor <= 1 else "medium"
    return "hard"


def _describe(path: str, bus_factor: int, owner_count: int) -> str:
    suffix = ""
    if bus_factor <= 1:
        suffix = " (deep expertise; bus_factor=1)"
    elif owner_count >= 3:
        suffix = " (broad ownership; many reviewers available)"
    return f"Study `{path}`{suffix}."
