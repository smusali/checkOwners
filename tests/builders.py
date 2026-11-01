"""Ownership maps, drift results, and stored state a test can start from."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from checkowners import __version__
from checkowners.analyze import (
    collect_analysis,
)
from checkowners.config import (
    AnalysisConfig,
    Config,
    DriftConfig,
)
from checkowners.models import (
    ConfidenceScore,
    DecayWarning,
    DriftEntry,
    DriftMode,
    DriftResult,
    OwnerEntry,
    OwnershipFreshness,
    OwnershipMap,
    PathOwnership,
    SignalScore,
)
from checkowners.state import (
    write_state,
)
from checkowners.storage import (
    repository_identity,
    state_path,
)
from tests.gitrepo import GitRepo

NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def owner_entry(handle: str, score: float = 0.8, commits: int = 7) -> OwnerEntry:
    """Return one scored owner with every signal but review available."""
    return OwnerEntry(
        handle=handle,
        ownership_score=score,
        last_commit=NOW,
        commits=commits,
        evidence_quality=1.0,
        score_breakdown=ConfidenceScore(
            total=score,
            recency=SignalScore(available=True, score=1.0),
            frequency=SignalScore(available=True, score=1.0),
            blame=SignalScore(available=True, score=1.0),
            review=SignalScore(available=False),
        ),
    )


def ownership_map(paths: dict[str, PathOwnership]) -> OwnershipMap:
    """Return an ownership map over `paths`, pinned to the shared instant."""
    return OwnershipMap(paths=paths, last_analyzed=NOW, analysis_ref="deadbeef")


#: A two-path map with a decaying owner, shared by the report-level suites.
OWNERSHIP = ownership_map(
    {
        "src/main.py": PathOwnership(
            owners=(owner_entry("alice@example.com", 0.92), owner_entry("bob@example.com", 0.55)),
            qualified_owner_count=2,
        ),
        "src/auth.py": PathOwnership(
            owners=(owner_entry("dave@example.com", 0.34),),
            qualified_owner_count=1,
            decay_warnings=(
                DecayWarning(
                    handle="dave@example.com",
                    path="src/auth.py",
                    last_commit=NOW,
                    days_since_last_commit=289,
                    historical_confidence=0.34,
                ),
            ),
        ),
    }
)


EMPTY_OWNERSHIP = ownership_map({})


DRIFT_DETECTED = DriftResult(
    stale=(DriftEntry(path="/old.py", confidence_delta=1.0, reason="stale path"),),
    missing=(
        DriftEntry(
            path="/new.py",
            confidence_delta=0.7,
            reason="missing",
            qualified_owner_count=1,
            decay=True,
        ),
    ),
    changed=(DriftEntry(path="/changed.py", confidence_delta=0.4, reason="owner shuffle"),),
    drift_detected=True,
)


NO_DRIFT = DriftResult(stale=(), missing=(), changed=(), drift_detected=False)


RECENT = NOW - timedelta(days=10)


OLD = NOW - timedelta(days=200)


def owner_handles(result: OwnershipMap, path: str) -> list[str]:
    """Return the inferred owner handles on `path`, in scored order."""
    return [owner.handle for owner in result.paths[path].owners]


def scoring_config(lookback_days: int | None = None) -> Config:
    """Return a config that scores every contributor, optionally with a lookback."""
    analysis = AnalysisConfig(confidence_threshold=0.0, min_commits=1)
    if lookback_days is not None:
        analysis = replace(analysis, lookback_days=lookback_days)
    return Config(analysis=analysis)


def store_analysis(repo: GitRepo, config: Config) -> None:
    """Analyze `repo` and write its state file, so a later run can reuse it."""
    outcome = collect_analysis(repo.path, config, as_of=NOW, analysis_ref=repo.head_sha())
    write_state(repo.path, outcome.ownership, config=config, history=outcome.baseline)


def readable_state(payload: dict[str, object]) -> dict[str, object]:
    """Stamp `payload` with this package version, so the cache accepts it."""
    return {"package_version": __version__, **payload}


def write_raw_state(repo_root: Path, payload: object) -> None:
    """Write `payload` straight into `repo_root`'s state file, valid or not."""
    body = payload
    if isinstance(payload, dict) and "repo_id" not in payload:
        body = {**payload, "repo_id": repository_identity(repo_root)}
    target = state_path(repo_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")


def stored_ownership() -> OwnershipMap:
    """Return one fully populated ownership map, for state round trips."""
    owner = OwnerEntry(
        handle="@alice",
        ownership_score=0.85,
        last_commit=NOW,
        commits=12,
        evidence_quality=1.0,
        score_breakdown=ConfidenceScore(
            total=0.85,
            recency=SignalScore(available=True, score=0.9),
            frequency=SignalScore(available=True, score=0.7),
            blame=SignalScore(available=True, score=0.8),
            review=SignalScore(available=True, score=0.6),
        ),
        freshness=OwnershipFreshness(
            active_expertise=0.9,
            historical_expertise=1.0,
            maintenance_recency=0.8,
            status="stable",
            half_life_days=400.0,
        ),
    )
    decay = DecayWarning(
        handle="@bob",
        path="src/auth.py",
        last_commit=NOW,
        days_since_last_commit=200,
        historical_confidence=0.4,
    )
    path = PathOwnership(owners=(owner,), qualified_owner_count=1, decay_warnings=(decay,))
    return ownership_map({"src/auth.py": path})


def declared_ownership(paths: dict[str, tuple[OwnerEntry, ...]]) -> OwnershipMap:
    """Return a map whose every owner on each path counts as qualified."""
    return ownership_map(
        {
            path: PathOwnership(owners=owners, qualified_owner_count=len(owners))
            for path, owners in paths.items()
        }
    )


def drift_config(mode: DriftMode = "both", min_delta: float = 0.0) -> Config:
    """Return a config whose drift mode and delta floor are set for one case."""
    return Config(drift=DriftConfig(mode=mode, min_confidence_delta=min_delta))


def write_codeowners(root: Path, content: str) -> Path:
    """Write `content` to `root`'s CODEOWNERS and return its path."""
    target = root / ".github" / "CODEOWNERS"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target
