"""Persistent state at ~/.checkowners/state/<repo-hash>.json.

The state file is the cache of the most recent analyze run for a repo.
Downstream commands (drift, decay, qualified-owners, topology, balance, onboard)
read from it to avoid re-running git log on every invocation. State is keyed
per repo (schema v6): each repo gets its own file, and the payload embeds the
absolute repo path so state from one repo can never leak into another.

Schema is versioned. Older state files are not auto-migrated; they are
ignored and a fresh state replaces them on the next analyze.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from checkowners.busfactor import DEPRECATED_COUNT_KEY, qualified_owner_count_fields
from checkowners.models import (
    DEPRECATED_SCORE_KEY,
    OWNERSHIP_MODEL_VERSION,
    AnalysisCompleteness,
    BusFactor,
    ConfidenceScore,
    DecayWarning,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
    Severity,
    SignalScore,
    TeamCluster,
)

SCHEMA_VERSION: int = 6
_STATE_DIR = Path.home() / ".checkowners"
_STATE_SUBDIR = "state"
_GRAPH_CACHE_SUBDIR = "graph"
_HANDLE_CACHE_FILENAME = "handles.json"


def _base_dir() -> Path:
    """Resolve the checkowners state directory, honoring CHECKOWNERS_STATE_DIR."""
    override = os.environ.get("CHECKOWNERS_STATE_DIR")
    return Path(override) if override else _STATE_DIR


def _repo_digest(repo_root: Path) -> str:
    return hashlib.sha256(str(repo_root.resolve()).encode("utf-8")).hexdigest()[:16]


def _state_path(repo_root: Path) -> Path:
    """Resolve the per-repo state file path, honoring CHECKOWNERS_STATE_DIR."""
    return _base_dir() / _STATE_SUBDIR / f"{_repo_digest(repo_root)}.json"


def _graph_cache_path(repo_root: Path) -> Path:
    """Resolve the serialized-graph cache path for a repo (keyed by repo hash)."""
    return _base_dir() / _GRAPH_CACHE_SUBDIR / f"{_repo_digest(repo_root)}.json"


def write_graph_cache(repo_root: Path, last_analyzed: datetime, graph_data: dict[str, Any]) -> Path:
    """Persist a serialized knowledge graph, tagged with the analysis timestamp."""
    target = _graph_cache_path(repo_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "repo": str(repo_root.resolve()),
        "last_analyzed": last_analyzed.astimezone(UTC).isoformat(),
        "graph": graph_data,
    }
    target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return target


def read_graph_cache(repo_root: Path, last_analyzed: datetime) -> dict[str, Any] | None:
    """Return the cached graph for a repo when present and not stale, else None.

    Freshness is keyed on the analysis timestamp: a cache built from an older
    ``analyze`` run is ignored so the graph never lags the ownership map.
    """
    target = _graph_cache_path(repo_root)
    if not target.exists():
        return None
    try:
        data: Any = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return None
    if data.get("last_analyzed") != last_analyzed.astimezone(UTC).isoformat():
        return None
    graph = data.get("graph")
    return graph if isinstance(graph, dict) else None


def read_state(repo_root: Path) -> dict[str, Any] | None:
    """Read a repo's state as a dict, or None if missing/version/repo mismatch."""
    target = _state_path(repo_root)
    if not target.exists():
        return None
    try:
        data: Any = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema_version") != SCHEMA_VERSION:
        return None
    model_version = data.get("model_version")
    if model_version is not None and model_version != OWNERSHIP_MODEL_VERSION:
        return None
    if data.get("repo") != str(repo_root.resolve()):
        return None
    return data


def write_state(
    repo_root: Path,
    ownership: OwnershipMap,
    *,
    topology: tuple[TeamCluster, ...] = (),
    bus_factor_summary: tuple[BusFactor, ...] = (),
    drift_detected: bool = False,
    qualified_owner_count_cap: int = 3,
    drift_reported_severity: str | None = None,
    drift_pending_severity: str | None = None,
    drift_pending_streak: int = 0,
) -> Path:
    """Persist the latest ownership map and derived intelligence to disk."""
    existing = read_state(repo_root)
    if drift_reported_severity is None and existing is not None:
        prior_reported = existing.get("drift_reported_severity")
        prior_pending = existing.get("drift_pending_severity")
        prior_streak = existing.get("drift_pending_streak", 0)
        drift_reported_severity = prior_reported if isinstance(prior_reported, str) else None
        drift_pending_severity = prior_pending if isinstance(prior_pending, str) else None
        drift_pending_streak = prior_streak if isinstance(prior_streak, int) else 0
    target = _state_path(repo_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "model_version": OWNERSHIP_MODEL_VERSION,
        "repo": str(repo_root.resolve()),
        "inferred": {
            path: _serialize_path(po, qualified_owner_count_cap)
            for path, po in sorted(ownership.paths.items())
        },
        "topology": {"clusters": [asdict(c) for c in topology]},
        "bus_factor_summary": _serialize_bus_factor_summary(
            bus_factor_summary, qualified_owner_count_cap
        ),
        "qualified_owner_count_cap": qualified_owner_count_cap,
        "deprecated_keys": [DEPRECATED_COUNT_KEY, DEPRECATED_SCORE_KEY],
        "last_analyzed": ownership.last_analyzed.astimezone(UTC).isoformat(),
        "analysis_ref": ownership.analysis_ref,
        "analysis_completeness": {
            "ignore_revs_applied": ownership.analysis_completeness.ignore_revs_applied,
            "ignore_revs_file": ownership.analysis_completeness.ignore_revs_file,
            "mailmap_applied": ownership.analysis_completeness.mailmap_applied,
            "mailmap_file": ownership.analysis_completeness.mailmap_file,
            "excluded_gitattributes": ownership.analysis_completeness.excluded_gitattributes,
            "excluded_static": ownership.analysis_completeness.excluded_static,
        },
        "drift_detected": drift_detected,
        "drift_reported_severity": drift_reported_severity,
        "drift_pending_severity": drift_pending_severity,
        "drift_pending_streak": drift_pending_streak,
    }
    target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return target


def read_handle_cache() -> dict[str, str]:
    """Read the persistent email -> @handle cache (shared across repos).

    An empty-string value is a remembered miss: the email was looked up before
    and did not resolve, so callers should not re-query the API for it.
    """
    target = _base_dir() / _HANDLE_CACHE_FILENAME
    if not target.exists():
        return {}
    try:
        data: Any = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def write_handle_cache(cache: dict[str, str]) -> Path:
    """Persist the email -> @handle cache, merging over any existing entries."""
    target = _base_dir() / _HANDLE_CACHE_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    merged = {**read_handle_cache(), **cache}
    target.write_text(json.dumps(merged, indent=2, sort_keys=True), encoding="utf-8")
    return target


def load_ownership(repo_root: Path) -> OwnershipMap | None:
    """Reconstruct a repo's OwnershipMap, or None if state is missing/invalid."""
    data = read_state(repo_root)
    if data is None:
        return None
    inferred = data.get("inferred")
    last_analyzed_raw = data.get("last_analyzed")
    if not isinstance(inferred, dict) or not isinstance(last_analyzed_raw, str):
        return None
    paths: dict[str, PathOwnership] = {}
    for path, raw in inferred.items():
        if not isinstance(raw, dict):
            continue
        path_ownership = _deserialize_path(raw)
        if path_ownership is None:
            continue
        paths[path] = path_ownership
    try:
        last_analyzed = datetime.fromisoformat(last_analyzed_raw)
    except ValueError:
        return None
    analysis_ref_raw = data.get("analysis_ref", "")
    analysis_ref = analysis_ref_raw if isinstance(analysis_ref_raw, str) else ""
    return OwnershipMap(
        paths=paths,
        last_analyzed=last_analyzed,
        analysis_ref=analysis_ref,
        analysis_completeness=_deserialize_completeness(data.get("analysis_completeness")),
    )


def load_hysteresis(repo_root: Path) -> tuple[Severity | None, Severity | None, int]:
    """Return persisted drift severity hysteresis, or first-run defaults."""
    data = read_state(repo_root)
    if data is None:
        return None, None, 0
    reported = _read_severity(data.get("drift_reported_severity"))
    pending = _read_severity(data.get("drift_pending_severity"))
    streak_raw = data.get("drift_pending_streak", 0)
    streak = streak_raw if isinstance(streak_raw, int) and not isinstance(streak_raw, bool) else 0
    return reported, pending, streak


def _read_severity(raw: object) -> Severity | None:
    if raw == "low":
        return "low"
    if raw == "medium":
        return "medium"
    if raw == "high":
        return "high"
    if raw == "critical":
        return "critical"
    return None


def _serialize_path(po: PathOwnership, cap: int) -> dict[str, Any]:
    return {
        "owners": [_serialize_owner(o) for o in po.owners],
        **qualified_owner_count_fields(po.qualified_owner_count, cap),
        "decay_warnings": [_serialize_decay(w) for w in po.decay_warnings],
    }


def _serialize_owner(entry: OwnerEntry) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "handle": entry.handle,
        "ownership_score": entry.ownership_score,
        "confidence": entry.confidence,
        "evidence_quality": entry.evidence_quality,
        "last_commit": entry.last_commit.astimezone(UTC).isoformat() if entry.last_commit else None,
        "commits": entry.commits,
    }
    if entry.score_breakdown is not None:
        payload["signals"] = entry.score_breakdown.signals_payload()
    return payload


def _serialize_decay(warning: DecayWarning) -> dict[str, Any]:
    return {
        "handle": warning.handle,
        "path": warning.path,
        "last_commit": warning.last_commit.astimezone(UTC).isoformat(),
        "days_since_last_commit": warning.days_since_last_commit,
        "historical_confidence": warning.historical_confidence,
    }


def _serialize_bus_factor_summary(
    entries: tuple[BusFactor, ...],
    cap: int,
) -> dict[str, Any]:
    critical_paths = sorted(e.path for e in entries if e.qualified_owner_count <= 1)
    repo_average = (
        round(sum(e.qualified_owner_count for e in entries) / len(entries), 2) if entries else 0.0
    )
    serialized_entries: list[dict[str, Any]] = []
    for entry in entries:
        row = asdict(entry)
        row.update(qualified_owner_count_fields(entry.qualified_owner_count, cap))
        serialized_entries.append(row)
    return {
        "critical_paths": critical_paths,
        "repo_average": repo_average,
        "qualified_owner_count_cap": cap,
        "deprecated_keys": [DEPRECATED_COUNT_KEY],
        "entries": serialized_entries,
    }


def _deserialize_completeness(raw: object) -> AnalysisCompleteness:
    if not isinstance(raw, dict):
        return AnalysisCompleteness()
    applied = raw.get("ignore_revs_applied", False)
    path = raw.get("ignore_revs_file", "")
    mailmap_applied = raw.get("mailmap_applied", False)
    mailmap_file = raw.get("mailmap_file", "")
    excluded_gitattributes = raw.get("excluded_gitattributes", 0)
    excluded_static = raw.get("excluded_static", 0)
    return AnalysisCompleteness(
        ignore_revs_applied=applied is True,
        ignore_revs_file=path if isinstance(path, str) else "",
        mailmap_applied=mailmap_applied is True,
        mailmap_file=mailmap_file if isinstance(mailmap_file, str) else "",
        excluded_gitattributes=(
            excluded_gitattributes if isinstance(excluded_gitattributes, int) else 0
        ),
        excluded_static=excluded_static if isinstance(excluded_static, int) else 0,
    )


def _deserialize_path(raw: dict[str, Any]) -> PathOwnership | None:
    raw_owners = raw.get("owners")
    if not isinstance(raw_owners, list):
        return None
    owners: list[OwnerEntry] = []
    for entry in raw_owners:
        if not isinstance(entry, dict):
            continue
        deserialized = _deserialize_owner(entry)
        if deserialized is not None:
            owners.append(deserialized)
    qualified_owner_count = _read_qualified_owner_count(raw)
    raw_decay = raw.get("decay_warnings", [])
    decay_warnings: list[DecayWarning] = []
    if isinstance(raw_decay, list):
        for warning in raw_decay:
            if isinstance(warning, dict):
                deserialized_warning = _deserialize_decay(warning)
                if deserialized_warning is not None:
                    decay_warnings.append(deserialized_warning)
    return PathOwnership(
        owners=tuple(owners),
        qualified_owner_count=qualified_owner_count,
        decay_warnings=tuple(decay_warnings),
    )


def _read_qualified_owner_count(raw: dict[str, Any]) -> int:
    raw_count = raw.get("qualified_owner_count", raw.get("bus_factor", 0))
    if isinstance(raw_count, bool) or not isinstance(raw_count, int):
        return 0
    return raw_count


def _deserialize_owner(raw: dict[str, Any]) -> OwnerEntry | None:
    handle = raw.get("handle")
    score_raw = raw.get("ownership_score", raw.get("confidence"))
    quality_raw = raw.get("evidence_quality", 1.0)
    commits = raw.get("commits")
    last_commit_raw = raw.get("last_commit")
    if not isinstance(handle, str) or isinstance(score_raw, bool):
        return None
    if not isinstance(score_raw, int | float):
        return None
    if isinstance(quality_raw, bool) or not isinstance(quality_raw, int | float):
        return None
    if not isinstance(commits, int):
        return None
    last_commit: datetime | None
    if isinstance(last_commit_raw, str):
        try:
            last_commit = datetime.fromisoformat(last_commit_raw)
        except ValueError:
            last_commit = None
    else:
        last_commit = None
    score_breakdown = _deserialize_signals(raw.get("signals"), float(score_raw))
    return OwnerEntry(
        handle=handle,
        ownership_score=float(score_raw),
        last_commit=last_commit,
        commits=commits,
        evidence_quality=float(quality_raw),
        score_breakdown=score_breakdown,
    )


def _deserialize_signals(raw: object, total: float) -> ConfidenceScore | None:
    if not isinstance(raw, dict):
        return None
    recency = _read_signal(raw.get("recency"))
    frequency = _read_signal(raw.get("frequency"))
    blame = _read_signal(raw.get("blame"))
    review = _read_signal(raw.get("review"))
    if recency is None or frequency is None or blame is None or review is None:
        return None
    return ConfidenceScore(
        total=total,
        recency=recency,
        frequency=frequency,
        blame=blame,
        review=review,
    )


def _read_signal(raw: object) -> SignalScore | None:
    if not isinstance(raw, dict):
        return None
    available = raw.get("available")
    if not isinstance(available, bool):
        return None
    if not available:
        return SignalScore(available=False)
    score = raw.get("score", 0.0)
    if isinstance(score, bool) or not isinstance(score, int | float):
        return None
    return SignalScore(available=True, score=float(score))


def _deserialize_decay(raw: dict[str, Any]) -> DecayWarning | None:
    try:
        return DecayWarning(
            handle=str(raw["handle"]),
            path=str(raw["path"]),
            last_commit=datetime.fromisoformat(str(raw["last_commit"])),
            days_since_last_commit=int(raw["days_since_last_commit"]),
            historical_confidence=float(raw["historical_confidence"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
