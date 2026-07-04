"""Persistent state at ~/.checkowners/state/<repo-hash>.json.

The state file is the cache of the most recent analyze run for a repo.
Downstream commands (drift, decay, bus-factor, topology, balance, onboard)
read from it to avoid re-running git log on every invocation. State is keyed
per repo (schema v3): each repo gets its own file, and the payload embeds the
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

from checkowners.models import (
    BusFactor,
    ConfidenceScore,
    DecayWarning,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
    TeamCluster,
)

SCHEMA_VERSION: int = 3
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
) -> Path:
    """Persist the latest ownership map and derived intelligence to disk."""
    target = _state_path(repo_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "repo": str(repo_root.resolve()),
        "inferred": {path: _serialize_path(po) for path, po in ownership.paths.items()},
        "topology": {"clusters": [asdict(c) for c in topology]},
        "bus_factor_summary": _serialize_bus_factor_summary(bus_factor_summary),
        "last_analyzed": ownership.last_analyzed.astimezone(UTC).isoformat(),
        "drift_detected": drift_detected,
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
    return OwnershipMap(paths=paths, last_analyzed=last_analyzed)


def _serialize_path(po: PathOwnership) -> dict[str, Any]:
    return {
        "owners": [_serialize_owner(o) for o in po.owners],
        "bus_factor": po.bus_factor,
        "decay_warnings": [_serialize_decay(w) for w in po.decay_warnings],
    }


def _serialize_owner(entry: OwnerEntry) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "handle": entry.handle,
        "confidence": entry.confidence,
        "last_commit": entry.last_commit.astimezone(UTC).isoformat() if entry.last_commit else None,
        "commits": entry.commits,
    }
    if entry.score_breakdown is not None:
        payload["score_breakdown"] = asdict(entry.score_breakdown)
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
) -> dict[str, Any]:
    critical_paths = sorted(e.path for e in entries if e.bus_factor <= 1)
    repo_average = round(sum(e.bus_factor for e in entries) / len(entries), 2) if entries else 0.0
    return {
        "critical_paths": critical_paths,
        "repo_average": repo_average,
        "entries": [asdict(e) for e in entries],
    }


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
    bus_factor = int(raw.get("bus_factor", 0))
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
        bus_factor=bus_factor,
        decay_warnings=tuple(decay_warnings),
    )


def _deserialize_owner(raw: dict[str, Any]) -> OwnerEntry | None:
    handle = raw.get("handle")
    confidence = raw.get("confidence")
    commits = raw.get("commits")
    last_commit_raw = raw.get("last_commit")
    if not isinstance(handle, str) or not isinstance(confidence, int | float):
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
    score_breakdown: ConfidenceScore | None = None
    raw_breakdown = raw.get("score_breakdown")
    if isinstance(raw_breakdown, dict):
        try:
            score_breakdown = ConfidenceScore(
                total=float(raw_breakdown["total"]),
                recency=float(raw_breakdown["recency"]),
                frequency=float(raw_breakdown["frequency"]),
                blame=float(raw_breakdown["blame"]),
                review=float(raw_breakdown["review"]),
            )
        except (KeyError, TypeError, ValueError):
            score_breakdown = None
    return OwnerEntry(
        handle=handle,
        confidence=float(confidence),
        last_commit=last_commit,
        commits=commits,
        score_breakdown=score_breakdown,
    )


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
