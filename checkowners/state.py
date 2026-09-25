"""Persistent state at ~/.checkowners/state/<repo-hash>.json.

The state file is the cache of the most recent analyze run for a repo.
Downstream commands (drift, decay, qualified-owners, topology, balance, onboard)
read from it to avoid re-running git log on every invocation. State is keyed
per repo (schema 1) by the normalized origin URL, or by the absolute path
when the repo has no origin. The payload records that identity and it is
checked on load.

Schema is versioned. Older state files are not auto-migrated; they are
ignored and a fresh state replaces them on the next analyze.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, TypedDict

from checkowners import __version__
from checkowners.busfactor import (
    distribution_json,
    owner_distribution,
    qualified_owner_count_fields,
)
from checkowners.models import (
    GAP_CATALOG,
    AnalysisCompleteness,
    AnalysisGap,
    BusFactor,
    ConfidenceScore,
    Config,
    DecayWarning,
    FreshnessStatus,
    GapCode,
    OwnerEntry,
    OwnershipFreshness,
    OwnershipMap,
    PathOwnership,
    Severity,
    SignalScore,
    TeamCluster,
    models_payload,
)
from checkowners.privacy import rekey_handle_cache, stored_identity

SCHEMA_VERSION: int = 1
CACHE_LIMIT_BYTES: int = 256 * 1024 * 1024
_STATE_DIR = Path.home() / ".checkowners"
_STATE_SUBDIR = "state"
_GRAPH_CACHE_SUBDIR = "graph"
_HANDLE_CACHE_FILENAME = "handles.json"
_SCP_REMOTE = re.compile(r"^(?:[^@/]+@)?([^:/]+):(.+)$")


class CacheEntry(TypedDict):
    repo_id: str
    analysis_ref: str
    last_analyzed: str
    bytes: int


class CacheInfo(TypedDict):
    path: str
    schema_version: int
    bytes: int
    state_files: int
    graph_files: int
    handles: bool
    limit_bytes: int
    entries: list[CacheEntry]


def cache_directory() -> Path:
    """Return the cache root, honoring CHECKOWNERS_STATE_DIR."""
    return _base_dir()


def _base_dir() -> Path:
    """Resolve the checkowners state directory, honoring CHECKOWNERS_STATE_DIR."""
    override = os.environ.get("CHECKOWNERS_STATE_DIR")
    return Path(override) if override else _STATE_DIR


def repository_identity(repo_root: Path) -> str:
    """Return a stable id for `repo_root`: normalized origin, else absolute path."""
    remote = _origin_url(repo_root)
    if remote:
        normalized = _normalize_remote(remote)
        if normalized:
            return f"origin:{normalized}"
    return f"path:{repo_root.resolve()}"


def config_hash(config: Config) -> str:
    """Return a sha256 of the scoring-relevant fields of `config`."""
    payload = {
        "analysis": asdict(config.analysis),
        "qualification": asdict(config.qualification),
        "scoring": asdict(config.scoring),
        "decay": asdict(config.decay),
        "bus_factor": asdict(config.bus_factor),
        "risk": asdict(config.risk),
        "paths": asdict(config.paths),
        "git": asdict(config.git),
        "models": asdict(config.models),
        "github": {
            "api_enabled": config.github.api_enabled,
            "org": config.github.org,
            "resolve_handles": config.github.resolve_handles,
            "resolve_teams": config.github.resolve_teams,
        },
        "contributors_exclude": list(config.contributors_exclude),
        "criticality": [list(rule) for rule in config.criticality],
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def _git_config_path(repo_root: Path) -> Path | None:
    git_entry = repo_root / ".git"
    if git_entry.is_dir():
        return git_entry / "config"
    if not git_entry.is_file():
        return None
    text = git_entry.read_text(encoding="utf-8", errors="replace").strip()
    if not text.lower().startswith("gitdir:"):
        return None
    raw = text.split(":", 1)[1].strip()
    git_dir = Path(raw)
    if not git_dir.is_absolute():
        git_dir = (repo_root / git_dir).resolve()
    common = git_dir / "commondir"
    if common.is_file():
        common_raw = common.read_text(encoding="utf-8", errors="replace").strip()
        common_dir = Path(common_raw)
        if not common_dir.is_absolute():
            common_dir = (git_dir / common_dir).resolve()
        return common_dir / "config"
    return git_dir / "config"


def _origin_url(repo_root: Path) -> str | None:
    config_path = _git_config_path(repo_root)
    if config_path is None or not config_path.is_file():
        return None
    in_origin = False
    try:
        lines = config_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_origin = stripped.lower() == '[remote "origin"]'
            continue
        if not in_origin:
            continue
        key, separator, value = stripped.partition("=")
        if separator and key.strip().lower() == "url":
            url = value.strip()
            return url or None
    return None


def _normalize_remote(url: str) -> str:
    text = url.strip().rstrip("/")
    if text.endswith(".git"):
        text = text[: -len(".git")]
    if "://" in text:
        parsed = urllib.parse.urlsplit(text)
        host = (parsed.hostname or "").lower()
        path = parsed.path.strip("/")
        if host and path:
            return f"{host}/{path}"
        return ""
    match = _SCP_REMOTE.fullmatch(text)
    if match is None:
        return ""
    host = match.group(1).lower()
    path = match.group(2).strip("/")
    return f"{host}/{path}"


def _repo_digest(repo_root: Path) -> str:
    return hashlib.sha256(repository_identity(repo_root).encode("utf-8")).hexdigest()[:16]


def _models_current(data: dict[str, Any]) -> bool:
    models = data.get("models")
    if not isinstance(models, dict):
        return False
    expected = models_payload()
    return all(models.get(name) == version for name, version in expected.items())


def _state_path(repo_root: Path) -> Path:
    """Resolve the per-repo state file path, honoring CHECKOWNERS_STATE_DIR."""
    return _base_dir() / _STATE_SUBDIR / f"{_repo_digest(repo_root)}.json"


def _graph_cache_path(repo_root: Path) -> Path:
    """Resolve the serialized-graph cache path for a repo (keyed by repo hash)."""
    return _base_dir() / _GRAPH_CACHE_SUBDIR / f"{_repo_digest(repo_root)}.json"


def _resolved_config(config: Config | None) -> Config:
    return config if config is not None else Config()


def _identity_fields(repo_root: Path, config: Config | None) -> dict[str, object]:
    resolved = _resolved_config(config)
    return {
        "schema_version": SCHEMA_VERSION,
        "models": models_payload(),
        "repo": str(repo_root.resolve()),
        "repo_id": repository_identity(repo_root),
        "config_hash": config_hash(resolved),
        "package_version": __version__,
    }


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return {str(key): value for key, value in data.items()}


def _acquire_lock(handle: IO[str]) -> None:
    try:
        import fcntl  # noqa: PLC0415
    except ImportError:
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _release_lock(handle: IO[str]) -> None:
    try:
        import fcntl  # noqa: PLC0415
    except ImportError:
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _locked(target: Path) -> Iterator[None]:
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(f"{target.name}.lock")
    with lock_path.open("a+", encoding="utf-8") as handle:
        _acquire_lock(handle)
        try:
            yield
        finally:
            _release_lock(handle)


def _atomic_write(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _json_files(subdir: str) -> list[Path]:
    directory = _base_dir() / subdir
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.json") if path.is_file())


def _bounded_files() -> list[Path]:
    return _json_files(_STATE_SUBDIR) + _json_files(_GRAPH_CACHE_SUBDIR)


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _evict(keep: Path) -> None:
    files = _bounded_files()
    total = sum(_file_size(path) for path in files)
    if total <= CACHE_LIMIT_BYTES:
        return
    keep_resolved = keep.resolve()
    victims = [path for path in files if path.resolve() != keep_resolved]
    victims.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0)
    for path in victims:
        if total <= CACHE_LIMIT_BYTES:
            break
        size = _file_size(path)
        path.unlink(missing_ok=True)
        total -= size


def _string_field(data: dict[str, Any] | None, key: str) -> str:
    if data is None:
        return ""
    value = data.get(key, "")
    return value if isinstance(value, str) else ""


def write_graph_cache(
    repo_root: Path,
    last_analyzed: datetime,
    graph_data: dict[str, Any],
    *,
    config: Config | None = None,
    analysis_ref: str = "",
) -> Path:
    """Persist a serialized knowledge graph, tagged with the analysis timestamp.

    `config` selects the scoring hash stored beside the graph. `analysis_ref`
    is the commit the graph was built from. Returns the cache file path.
    """
    target = _graph_cache_path(repo_root)
    payload = {
        **_identity_fields(repo_root, config),
        "last_analyzed": last_analyzed.astimezone(UTC).isoformat(),
        "analysis_ref": analysis_ref,
        "graph": _stored_graph(graph_data),
    }
    text = json.dumps(payload, indent=2, sort_keys=True)
    with _locked(target):
        _atomic_write(target, text)
        _evict(target)
    return target


def read_graph_cache(
    repo_root: Path,
    last_analyzed: datetime,
    *,
    config: Config | None = None,
    analysis_ref: str | None = None,
) -> dict[str, Any] | None:
    """Return the cached graph when it matches this analysis, else None.

    `config`, when passed, must hash to the stored config hash. `analysis_ref`,
    when passed, must equal the stored commit. A timestamp mismatch also misses.
    """
    data = _read_json(_graph_cache_path(repo_root))
    if data is None or data.get("schema_version") != SCHEMA_VERSION:
        return None
    if not _models_current(data):
        return None
    if data.get("repo_id") != repository_identity(repo_root):
        return None
    if data.get("last_analyzed") != last_analyzed.astimezone(UTC).isoformat():
        return None
    if config is not None and data.get("config_hash") != config_hash(config):
        return None
    if analysis_ref is not None and data.get("analysis_ref") != analysis_ref:
        return None
    graph = data.get("graph")
    return graph if isinstance(graph, dict) else None


def read_state(repo_root: Path) -> dict[str, Any] | None:
    """Read a repo's state as a dict, or None if missing or structurally invalid."""
    data = _read_json(_state_path(repo_root))
    if data is None:
        return None
    if data.get("schema_version") != SCHEMA_VERSION:
        return None
    if not _models_current(data):
        return None
    if data.get("repo_id") != repository_identity(repo_root):
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
    config: Config | None = None,
) -> Path:
    """Persist the latest ownership map and derived intelligence to disk.

    `config` selects the scoring hash stored with the map. When omitted, the
    default config is hashed. Returns the state file path.
    """
    target = _state_path(repo_root)
    with _locked(target):
        existing = read_state(repo_root)
        if drift_reported_severity is None and existing is not None:
            prior_reported = existing.get("drift_reported_severity")
            prior_pending = existing.get("drift_pending_severity")
            prior_streak = existing.get("drift_pending_streak", 0)
            drift_reported_severity = prior_reported if isinstance(prior_reported, str) else None
            drift_pending_severity = prior_pending if isinstance(prior_pending, str) else None
            drift_pending_streak = prior_streak if isinstance(prior_streak, int) else 0
        payload: dict[str, Any] = {
            **_identity_fields(repo_root, config),
            "inferred": {
                path: _serialize_path(po, qualified_owner_count_cap)
                for path, po in sorted(ownership.paths.items())
            },
            "topology": {"clusters": [_serialize_cluster(cluster) for cluster in topology]},
            "bus_factor_summary": _serialize_bus_factor_summary(
                bus_factor_summary,
                qualified_owner_count_cap,
                ownership,
                config if config is not None else Config(),
            ),
            "qualified_owner_count_cap": qualified_owner_count_cap,
            "last_analyzed": ownership.last_analyzed.astimezone(UTC).isoformat(),
            "analysis_ref": ownership.analysis_ref,
            "analysis_completeness": {
                "ignore_revs_applied": ownership.analysis_completeness.ignore_revs_applied,
                "ignore_revs_file": ownership.analysis_completeness.ignore_revs_file,
                "mailmap_applied": ownership.analysis_completeness.mailmap_applied,
                "mailmap_file": ownership.analysis_completeness.mailmap_file,
                "excluded_gitattributes": ownership.analysis_completeness.excluded_gitattributes,
                "excluded_static": ownership.analysis_completeness.excluded_static,
                "score": ownership.analysis_completeness.score,
                "gaps": [
                    {"code": gap.code, "reason": gap.reason}
                    for gap in ownership.analysis_completeness.gaps
                ],
            },
            "drift_detected": drift_detected,
            "drift_reported_severity": drift_reported_severity,
            "drift_pending_severity": drift_pending_severity,
            "drift_pending_streak": drift_pending_streak,
        }
        _atomic_write(target, json.dumps(payload, indent=2, sort_keys=True))
        _evict(target)
    return target


def _handle_map(data: dict[str, Any] | None) -> dict[str, str]:
    if data is None:
        return {}
    return {key: value for key, value in data.items() if isinstance(value, str)}


def read_handle_cache() -> dict[str, str]:
    """Read the email-token -> @handle cache (shared across repos).

    An empty-string value is a remembered miss: the address was looked up
    before and did not resolve, so callers should not re-query the API for it.
    A file that still uses plaintext email keys is rewritten on read.
    """
    target = _base_dir() / _HANDLE_CACHE_FILENAME
    with _locked(target):
        raw = _handle_map(_read_json(target))
        stored = rekey_handle_cache(raw)
        if stored != raw:
            _atomic_write(target, json.dumps(stored, indent=2, sort_keys=True))
        return stored


def write_handle_cache(cache: dict[str, str]) -> Path:
    """Persist the email-token -> @handle cache, merging over any existing entries.

    Email keys are stored as tokens. Returns the cache file path.
    """
    target = _base_dir() / _HANDLE_CACHE_FILENAME
    with _locked(target):
        merged = rekey_handle_cache({**_handle_map(_read_json(target)), **cache})
        _atomic_write(target, json.dumps(merged, indent=2, sort_keys=True))
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


def reusable_ownership(
    repo_root: Path,
    config: Config,
    *,
    head: str,
    allow_stale: bool = False,
    max_age: int | None = None,
) -> OwnershipMap | None:
    """Return cached ownership when it is safe to reuse, else None.

    `head` is the current commit. The map is reused when `analysis_ref` equals
    `head`, or when `allow_stale` is true. `max_age` is a maximum age in
    seconds; `0` refuses every cached map. A config-hash mismatch returns None
    even when `allow_stale` is true.
    """
    data = read_state(repo_root)
    if data is None or data.get("config_hash") != config_hash(config):
        return None
    ownership = load_ownership(repo_root)
    if ownership is None:
        return None
    if max_age is not None and (max_age == 0 or _age_seconds(ownership.last_analyzed) > max_age):
        return None
    if ownership.analysis_ref == head or allow_stale:
        return ownership
    return None


def _age_seconds(last_analyzed: datetime) -> float:
    aware = last_analyzed if last_analyzed.tzinfo is not None else last_analyzed.replace(tzinfo=UTC)
    return (datetime.now(tz=UTC) - aware.astimezone(UTC)).total_seconds()


def cache_info() -> CacheInfo:
    """Return path, size, file counts, and one entry per state file."""
    state_files = _json_files(_STATE_SUBDIR)
    graph_files = _json_files(_GRAPH_CACHE_SUBDIR)
    handles = _base_dir() / _HANDLE_CACHE_FILENAME
    total = sum(_file_size(path) for path in (*state_files, *graph_files))
    if handles.is_file():
        total += _file_size(handles)
    entries: list[CacheEntry] = []
    for path in state_files:
        data = _read_json(path)
        entries.append(
            {
                "repo_id": _string_field(data, "repo_id"),
                "analysis_ref": _string_field(data, "analysis_ref"),
                "last_analyzed": _string_field(data, "last_analyzed"),
                "bytes": _file_size(path),
            }
        )
    return {
        "path": str(_base_dir()),
        "schema_version": SCHEMA_VERSION,
        "bytes": total,
        "state_files": len(state_files),
        "graph_files": len(graph_files),
        "handles": handles.is_file(),
        "limit_bytes": CACHE_LIMIT_BYTES,
        "entries": entries,
    }


def cache_clear() -> int:
    """Delete state and graph JSON files. Return how many files were removed."""
    return _unlink_json(_STATE_SUBDIR) + _unlink_json(_GRAPH_CACHE_SUBDIR)


def cache_purge() -> int:
    """Delete every file under the cache root, including handles.

    Returns how many files were removed. The cache root directory is kept.
    """
    base = _base_dir()
    if not base.exists():
        return 0
    removed = 0
    for child in list(base.iterdir()):
        if child.is_dir() and not child.is_symlink():
            removed += sum(1 for path in child.rglob("*") if path.is_file())
            shutil.rmtree(child)
        else:
            child.unlink()
            removed += 1
    return removed


def _unlink_json(subdir: str) -> int:
    removed = 0
    for path in _json_files(subdir):
        path.unlink(missing_ok=True)
        removed += 1
    return removed


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


def _stored_names(names: tuple[str, ...]) -> list[str]:
    return [stored_identity(name) for name in names]


def _serialize_cluster(cluster: TeamCluster) -> dict[str, Any]:
    payload = asdict(cluster)
    payload["members"] = _stored_names(cluster.members)
    return payload


def _stored_node_id(node_id: object) -> object:
    if not isinstance(node_id, str) or not node_id.startswith("contrib::"):
        return node_id
    handle = node_id.removeprefix("contrib::")
    return f"contrib::{stored_identity(handle)}"


def _stored_graph(graph_data: dict[str, Any]) -> dict[str, Any]:
    nodes = graph_data.get("nodes")
    edges = graph_data.get("edges")
    stored_nodes: list[dict[str, Any]] = []
    if isinstance(nodes, list):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            copied = dict(node)
            copied["id"] = _stored_node_id(copied.get("id"))
            stored_nodes.append(copied)
    stored_edges: list[dict[str, Any]] = []
    if isinstance(edges, list):
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            copied = dict(edge)
            copied["source"] = _stored_node_id(copied.get("source"))
            copied["target"] = _stored_node_id(copied.get("target"))
            stored_edges.append(copied)
    return {"nodes": stored_nodes, "edges": stored_edges}


def _serialize_path(po: PathOwnership, cap: int) -> dict[str, Any]:
    return {
        "owners": [_serialize_owner(o) for o in po.owners],
        "scored_owners": [_serialize_owner(o) for o in po.scored_owners],
        **qualified_owner_count_fields(po.qualified_owner_count, cap),
        "decay_warnings": [_serialize_decay(w) for w in po.decay_warnings],
    }


def _serialize_owner(entry: OwnerEntry) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "handle": stored_identity(entry.handle),
        "ownership_score": entry.ownership_score,
        "evidence_quality": entry.evidence_quality,
        "last_commit": entry.last_commit.astimezone(UTC).isoformat() if entry.last_commit else None,
        "commits": entry.commits,
    }
    if entry.score_breakdown is not None:
        payload["signals"] = entry.score_breakdown.signals_payload()
    if entry.freshness is not None:
        payload["freshness"] = _serialize_freshness(entry.freshness)
    return payload


def _serialize_freshness(freshness: OwnershipFreshness) -> dict[str, float | str]:
    return {
        "active_expertise": freshness.active_expertise,
        "historical_expertise": freshness.historical_expertise,
        "maintenance_recency": freshness.maintenance_recency,
        "status": freshness.status,
        "half_life_days": freshness.half_life_days,
    }


def _serialize_decay(warning: DecayWarning) -> dict[str, Any]:
    return {
        "handle": stored_identity(warning.handle),
        "path": warning.path,
        "last_commit": warning.last_commit.astimezone(UTC).isoformat(),
        "days_since_last_commit": warning.days_since_last_commit,
        "historical_confidence": warning.historical_confidence,
        "status": warning.status,
    }


def _serialize_bus_factor_summary(
    entries: tuple[BusFactor, ...],
    cap: int,
    ownership: OwnershipMap,
    config: Config,
) -> dict[str, Any]:
    critical_paths = sorted(e.path for e in entries if e.qualified_owner_count <= 1)
    distribution = owner_distribution(entries, ownership, config)
    serialized_entries: list[dict[str, Any]] = []
    for entry in entries:
        row = asdict(entry)
        row["contributors_above_threshold"] = _stored_names(entry.contributors_above_threshold)
        row["recommended_backups"] = _stored_names(entry.recommended_backups)
        row.update(qualified_owner_count_fields(entry.qualified_owner_count, cap))
        serialized_entries.append(row)
    return {
        "critical_paths": critical_paths,
        "distribution": distribution_json(distribution),
        "qualified_owner_count_cap": cap,
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
        score=_optional_score(raw.get("score")),
        gaps=_deserialize_gaps(raw.get("gaps")),
    )


def _optional_score(raw: object) -> float | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return raw * 1.0
    if isinstance(raw, float):
        return raw
    return None


def _is_gap_code(value: str) -> bool:
    return value in GAP_CATALOG


def _deserialize_gaps(raw: object) -> tuple[AnalysisGap, ...]:
    if not isinstance(raw, list):
        return ()
    gaps: list[AnalysisGap] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        code = item.get("code")
        reason = item.get("reason")
        if not isinstance(code, str) or not isinstance(reason, str) or not _is_gap_code(code):
            continue
        gaps.append(AnalysisGap(code=_as_gap_code(code), reason=reason))
    return tuple(gaps)


def _as_gap_code(code: str) -> GapCode:
    for known in GAP_CATALOG:
        if known == code:
            return known
    msg = f"unknown gap code: {code}"
    raise ValueError(msg)


def _deserialize_owners(raw: object) -> tuple[OwnerEntry, ...]:
    if not isinstance(raw, list):
        return ()
    owners: list[OwnerEntry] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        deserialized = _deserialize_owner(entry)
        if deserialized is not None:
            owners.append(deserialized)
    return tuple(owners)


def _deserialize_path(raw: dict[str, Any]) -> PathOwnership | None:
    if not isinstance(raw.get("owners"), list):
        return None
    owners = _deserialize_owners(raw.get("owners"))
    scored_owners = (
        _deserialize_owners(raw.get("scored_owners")) if "scored_owners" in raw else owners
    )
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
        owners=owners,
        qualified_owner_count=qualified_owner_count,
        decay_warnings=tuple(decay_warnings),
        scored_owners=scored_owners,
    )


def _read_qualified_owner_count(raw: dict[str, Any]) -> int:
    raw_count = raw.get("qualified_owner_count", 0)
    if isinstance(raw_count, bool) or not isinstance(raw_count, int):
        return 0
    return raw_count


def _deserialize_owner(raw: dict[str, Any]) -> OwnerEntry | None:
    handle = raw.get("handle")
    score_raw = raw.get("ownership_score")
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
    score_breakdown = _deserialize_signals(raw.get("signals"), _number(score_raw))
    return OwnerEntry(
        handle=handle,
        ownership_score=_number(score_raw),
        last_commit=last_commit,
        commits=commits,
        evidence_quality=_number(quality_raw),
        score_breakdown=score_breakdown,
        freshness=_deserialize_freshness(raw.get("freshness")),
    )


def _number(value: int | float) -> float:
    if isinstance(value, int):
        return value + 0.0
    return value


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


_FRESHNESS_STATUSES: tuple[FreshnessStatus, ...] = (
    "inactive",
    "superseded",
    "stable",
    "departed",
)


def _match_freshness_status(value: object) -> FreshnessStatus | None:
    if not isinstance(value, str):
        return None
    for known in _FRESHNESS_STATUSES:
        if known == value:
            return known
    return None


def _read_freshness_status(value: object) -> FreshnessStatus:
    matched = _match_freshness_status(value)
    if matched is None:
        return "inactive"
    return matched


def _read_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, int):
        return value + 0.0
    return value


def _deserialize_freshness(raw: object) -> OwnershipFreshness | None:
    if not isinstance(raw, dict):
        return None
    active = _read_number(raw.get("active_expertise"))
    historical = _read_number(raw.get("historical_expertise"))
    maintenance = _read_number(raw.get("maintenance_recency"))
    half_life = _read_number(raw.get("half_life_days"))
    status = _match_freshness_status(raw.get("status"))
    if (
        active is None
        or historical is None
        or maintenance is None
        or half_life is None
        or status is None
    ):
        return None
    return OwnershipFreshness(
        active_expertise=active,
        historical_expertise=historical,
        maintenance_recency=maintenance,
        status=status,
        half_life_days=half_life,
    )


def _deserialize_decay(raw: dict[str, Any]) -> DecayWarning | None:
    try:
        confidence = _read_number(raw.get("historical_confidence"))
        days = raw.get("days_since_last_commit")
        if confidence is None or isinstance(days, bool) or not isinstance(days, int):
            return None
        return DecayWarning(
            handle=str(raw["handle"]),
            path=str(raw["path"]),
            last_commit=datetime.fromisoformat(str(raw["last_commit"])),
            days_since_last_commit=days,
            historical_confidence=confidence,
            status=_read_freshness_status(raw.get("status")),
        )
    except (KeyError, TypeError, ValueError):
        return None
