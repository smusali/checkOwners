"""Self-contained offline HTML ownership report."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path

from checkowners.analyze import analyze_ownership, run_git
from checkowners.audit import AuditFacts, AuditRow, build_audit
from checkowners.config import find_codeowners_path, load_config
from checkowners.drift import detect_drift
from checkowners.graph import GraphExtraMissingError
from checkowners.models import Config, DriftResult, OwnerEntry, OwnershipMap, models_payload
from checkowners.patterns import CodeownersRule, matching_rules, parse_rules
from checkowners.privacy import KNOWLEDGE_RISK_NOTICE, sanitize
from checkowners.risk import RiskEntry, assess_risk
from checkowners.state import repository_identity
from checkowners.topology import infer_topology
from checkowners.trends import analyze_trends
from checkowners.validate import validate_codeowners

LIMITATIONS_SUMMARY = (
    "Inference can be wrong. A score is evidence of who has changed the code, "
    "not a judgment of a person. Missing evidence is not negative evidence. "
    "CODEOWNERS may intentionally differ from repository history."
)
REMOVAL_PATH = (
    "A maintainer can ask for this report to be removed by opening an issue "
    "on the checkOwners project."
)
HTML_SIZE_BUDGET = 8 * 1024 * 1024
_URL = re.compile(r"https?://", re.IGNORECASE)
_TIER_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@dataclass(frozen=True)
class ReportOwner:
    handle: str
    ownership_score: float
    evidence_quality: float
    commits: int
    last_commit: str
    signals: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class ReportFile:
    path: str
    change_events: int | None
    risk: str
    rule: str
    owners: tuple[ReportOwner, ...]


@dataclass(frozen=True)
class ReportDocument:
    head_sha: str
    generated_at: str
    analysis_completeness: float | None
    models: tuple[tuple[str, str], ...]
    overview: tuple[tuple[str, str, str], ...]
    files: tuple[ReportFile, ...]
    timeline: tuple[tuple[str, float], ...]
    comparisons: tuple[tuple[str, str, float, float, float, float], ...]
    clusters: tuple[tuple[str, bool, tuple[str, ...]], ...]
    teams_reason: str
    ownership_coverage: float
    critical_components: int
    drift_count: int
    finding: str

    def payload(self) -> dict[str, object]:
        return {
            "files": [_file_payload(item) for item in self.files],
            "overview": [
                {"rule": rule, "status": status, "summary": summary}
                for rule, status, summary in self.overview
            ],
            "timeline_model": "historical-v1",
            "timeline": [
                {"period_end": stamp, "historical_activity_confidence": score}
                for stamp, score in self.timeline
            ],
            "teams_reason": self.teams_reason,
            "comparisons": [
                {
                    "team": team,
                    "cluster": cluster,
                    "jaccard": jaccard,
                    "precision": precision,
                    "recall": recall,
                    "coverage": coverage,
                }
                for team, cluster, jaccard, precision, recall, coverage in self.comparisons
            ],
            "clusters": [
                {"name": name, "declared": declared, "members": list(members)}
                for name, declared, members in self.clusters
            ],
            "owners": _people(self.files),
        }


def build_document(repo_root: Path, config: Config | None = None) -> ReportDocument:
    """Return the report document for `repo_root` using one ownership analysis."""
    loaded = config if config is not None else load_config(repo_root)
    ownership = analyze_ownership(repo_root, loaded, incremental=False)
    codeowners_path = find_codeowners_path(repo_root)
    text = codeowners_path.read_text(encoding="utf-8") if codeowners_path.is_file() else ""
    rules = parse_rules(text) if text else ()
    drift = detect_drift(repo_root, ownership, loaded, codeowners_path=codeowners_path)
    risk = assess_risk(ownership, loaded, rules, drift)
    trends = analyze_trends(repo_root, loaded, as_of=ownership.last_analyzed)
    tiers = {entry.path: entry.tier for entry in risk.entries}
    files = tuple(_report_file(path, ownership, tiers, rules) for path in ownership.paths)
    audit = build_audit(
        _audit_facts(
            repo_root,
            loaded,
            codeowners_path,
            text,
            rules,
            ownership,
            drift,
        )
    )
    comparisons, clusters, teams_reason = _teams(ownership, loaded)
    return ReportDocument(
        head_sha=ownership.analysis_ref,
        generated_at=ownership.last_analyzed.astimezone(UTC).isoformat(),
        analysis_completeness=ownership.analysis_completeness.score,
        models=tuple(sorted(models_payload().items())),
        overview=tuple((row.rule, row.status, row.summary) for row in audit.rows),
        files=files,
        timeline=tuple(
            (point.period_end.isoformat(), point.historical_activity_confidence)
            for point in trends.points
        ),
        comparisons=comparisons,
        clusters=clusters,
        teams_reason=teams_reason,
        ownership_coverage=risk.summary.observed_ownership_coverage,
        critical_components=risk.summary.critical_components,
        drift_count=len(drift.stale) + len(drift.missing) + len(drift.changed),
        finding=_finding(risk.entries, audit.rows),
    )


def render_report(document: ReportDocument, config: Config, repo_id: str) -> str:
    """Return one offline HTML page for `document` under the privacy mode in `config`."""
    payload = _without_urls(sanitize(document.payload(), config, repo_id))
    completeness = (
        "unknown"
        if document.analysis_completeness is None
        else f"{document.analysis_completeness:.4f}"
    )
    models = ", ".join(f"{name} {version}" for name, version in document.models)
    html = (
        _TEMPLATE.replace("__NOTICE__", _escape(KNOWLEDGE_RISK_NOTICE))
        .replace("__LIMITATIONS__", _escape(LIMITATIONS_SUMMARY))
        .replace("__REMOVAL__", _escape(REMOVAL_PATH))
        .replace("__HEAD__", _escape(document.head_sha))
        .replace("__GENERATED__", _escape(document.generated_at))
        .replace("__COMPLETENESS__", _escape(completeness))
        .replace("__MODELS__", _escape(models))
        .replace("__REPORT_JSON__", _embed(payload))
    )
    if _URL.search(html):
        msg = "report contains an external URL"
        raise ValueError(msg)
    return html


def write_report(repo_root: Path, destination: Path, config: Config | None = None) -> Path:
    """Write the HTML report for `repo_root` to `destination` and return that path."""
    loaded = config if config is not None else load_config(repo_root)
    document = build_document(repo_root, loaded)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        render_report(document, loaded, repository_identity(repo_root)),
        encoding="utf-8",
    )
    return destination


def _report_file(
    path: str,
    ownership: OwnershipMap,
    tiers: Mapping[str, str],
    rules: tuple[CodeownersRule, ...],
) -> ReportFile:
    path_ownership = ownership.paths[path]
    owners = tuple(_report_owner(owner) for owner in path_ownership.owners)
    return ReportFile(
        path=path,
        change_events=path_ownership.change_events,
        risk=tiers.get(path, "low"),
        rule=_rule_text(rules, path),
        owners=owners,
    )


def _report_owner(owner: OwnerEntry) -> ReportOwner:
    signals: list[tuple[str, float]] = []
    breakdown = owner.score_breakdown
    if breakdown is not None:
        pairs = (
            ("recency", breakdown.recency),
            ("frequency", breakdown.frequency),
            ("blame", breakdown.blame),
            ("review", breakdown.review),
            ("change_survival", breakdown.change_survival),
        )
        signals = [(name, round(score.score, 4)) for name, score in pairs if score.available]
    last_commit = ""
    if owner.last_commit is not None:
        last_commit = owner.last_commit.isoformat()
    return ReportOwner(
        handle=owner.handle,
        ownership_score=round(owner.ownership_score, 4),
        evidence_quality=round(owner.evidence_quality, 4),
        commits=owner.commits,
        last_commit=last_commit,
        signals=tuple(signals),
    )


def _rule_text(rules: tuple[CodeownersRule, ...], path: str) -> str:
    matches = matching_rules(rules, path)
    if not matches:
        return ""
    winner = matches[-1]
    return f"{winner.pattern} {' '.join(winner.owners)}".strip()


def _file_payload(item: ReportFile) -> dict[str, object]:
    return {
        "path": item.path,
        "change_events": item.change_events,
        "risk": item.risk,
        "rule": item.rule,
        "owners": [
            {
                "handle": owner.handle,
                "ownership_score": owner.ownership_score,
                "evidence_quality": owner.evidence_quality,
                "commits": owner.commits,
                "last_commit": owner.last_commit,
                "signals": [{name: score} for name, score in owner.signals],
            }
            for owner in item.owners
        ],
    }


def _people(files: Sequence[ReportFile]) -> list[dict[str, object]]:
    directories: dict[str, set[str]] = {}
    for item in files:
        if not item.owners:
            continue
        parent = item.path.rsplit("/", 1)[0] if "/" in item.path else item.path
        for owner in item.owners:
            directories.setdefault(owner.handle, set()).add(parent)
    return [
        {"handle": handle, "directories": sorted(dirs)}
        for handle, dirs in sorted(directories.items())
    ]


def _finding(entries: Sequence[RiskEntry], rows: Sequence[AuditRow]) -> str:
    if entries:
        worst = min(
            entries,
            key=lambda entry: (_TIER_RANK.get(entry.tier, 9), -entry.risk, entry.path),
        )
        return f"{worst.path} is {worst.tier}: {worst.reason}"
    for row in rows:
        if row.status in {"warn", "fail"} and row.summary:
            return row.summary
    return "No elevated ownership risk was found."


def _teams(
    ownership: OwnershipMap,
    config: Config,
) -> tuple[
    tuple[tuple[str, str, float, float, float, float], ...],
    tuple[tuple[str, bool, tuple[str, ...]], ...],
    str,
]:
    try:
        report = infer_topology(ownership, config)
    except GraphExtraMissingError:
        return (), (), "Team alignment is unavailable."
    comparisons = tuple(
        (
            item.team,
            item.cluster,
            item.jaccard,
            item.precision,
            item.recall,
            item.coverage,
        )
        for item in report.comparisons
    )
    clusters = tuple(
        (cluster.name, cluster.declared, cluster.members) for cluster in report.clusters
    )
    reason = "" if comparisons else "Declared teams were not available."
    return comparisons, clusters, reason


def _audit_facts(
    repo_root: Path,
    config: Config,
    codeowners_path: Path,
    text: str,
    rules: tuple[CodeownersRule, ...],
    ownership: OwnershipMap,
    drift: DriftResult,
) -> AuditFacts:
    tracked = _tracked(repo_root)
    branch = _branch(repo_root)
    return AuditFacts(
        repo_root=repo_root,
        config=config,
        codeowners_path=codeowners_path,
        text=text,
        rules=rules,
        tracked=tracked,
        head_branch=branch,
        base_branch=branch,
        file_bytes=codeowners_path.stat().st_size if codeowners_path.is_file() else 0,
        teams=None,
        ownership=ownership,
        analysis_error="",
        errors=tuple(validate_codeowners(repo_root, codeowners_path=codeowners_path)),
        remote_errors=None,
        private=False,
        ref=branch or ownership.analysis_ref,
        default_branch=branch or ownership.analysis_ref,
        access={},
        review="unknown",
        token_present=False,
        include_inference=True,
        drift=drift,
    )


def _tracked(repo_root: Path) -> tuple[str, ...]:
    try:
        result = run_git(repo_root, ["git", "ls-files"], check=True, paths=True)
    except (OSError, ValueError, subprocess.CalledProcessError):
        return ()
    return tuple(line for line in result.stdout.splitlines() if line)


def _branch(repo_root: Path) -> str:
    try:
        result = run_git(repo_root, ["git", "rev-parse", "--abbrev-ref", "HEAD"], check=True)
    except (OSError, ValueError, subprocess.CalledProcessError):
        return ""
    name = result.stdout.strip()
    if name == "HEAD":
        return ""
    return name


def _without_urls(value: object) -> object:
    if isinstance(value, str):
        return _URL.sub("", value)
    if isinstance(value, list):
        return [_without_urls(item) for item in value]
    if isinstance(value, dict):
        cleaned: dict[str, object] = {}
        for key, item in value.items():
            if isinstance(key, str):
                cleaned[key] = _without_urls(item)
        return cleaned
    return value


def _embed(payload: object) -> str:
    return json.dumps(payload, sort_keys=True).replace("<", "\\u003c")


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Ownership report</title>
<style>
body { font: 15px/1.4 sans-serif; margin: 24px; color: #1b1b1b; background: #fff; }
header, section { margin-bottom: 28px; }
table { border-collapse: collapse; width: 100%; }
th, td { border-bottom: 1px solid #ddd; text-align: left; padding: 4px 8px; vertical-align: top; }
.treemap { position: relative; height: 360px; background: #f4f4f4; }
.cell { position: absolute; overflow: hidden; box-sizing: border-box;
  border: 1px solid #fff; color: #111; padding: 4px; cursor: pointer; }
.risk-critical { background: #e07a7a; }
.risk-high { background: #f0a36b; }
.risk-medium { background: #f2d16b; }
.risk-low { background: #8fce93; }
.notice, .limits { background: #f7f4ea; padding: 12px; }
</style>
</head>
<body>
<header>
<h1>Ownership report</h1>
<p id="provenance">head <span id="head-sha">__HEAD__</span>
generated <span id="generated-at">__GENERATED__</span>
completeness <span id="analysis-completeness">__COMPLETENESS__</span>
models <span id="models">__MODELS__</span></p>
<p id="knowledge-notice" class="notice">__NOTICE__</p>
</header>
<section id="overview"><h2>Repository overview</h2><div id="overview-body"></div></section>
<section id="treemap"><h2>Treemap</h2><div id="treemap-body" class="treemap"></div></section>
<section id="ownership-map"><h2>Ownership map</h2><div id="map-body"></div></section>
<section id="timeline"><h2>Timeline</h2>
<p>This series is historical-v1 historical_activity_confidence, not ownership_score.</p>
<div id="timeline-body"></div></section>
<section id="team-alignment"><h2>Team alignment</h2><div id="team-body"></div></section>
<section id="drilldown"><h2>Drill-down</h2><div id="drill-body"></div></section>
<footer>
<p class="limits">__LIMITATIONS__</p>
<p id="removal">__REMOVAL__</p>
<p>Each score is ownership_score next to evidence_quality.</p>
</footer>
<script>
const REPORT = __REPORT_JSON__;
const rank = {critical: 0, high: 1, medium: 2, low: 3};
function text(node, value) { node.textContent = value == null ? "" : String(value); }
function worse(left, right) { return (rank[left] ?? 9) <= (rank[right] ?? 9) ? left : right; }
function overview() {
  const body = document.getElementById("overview-body");
  const table = document.createElement("table");
  (REPORT.overview || []).forEach(function (row) {
    const tr = document.createElement("tr");
    [row.status, row.rule, row.summary].forEach(function (value) {
      const td = document.createElement("td");
      text(td, value);
      tr.appendChild(td);
    });
    table.appendChild(tr);
  });
  body.appendChild(table);
}
function tree() {
  const root = {name: "", children: {}, files: [], size: 0, risk: "low"};
  (REPORT.files || []).forEach(function (file) {
    const parts = file.path.split("/");
    let node = root;
    parts.slice(0, -1).forEach(function (part) {
      const fresh = {name: part, children: {}, files: [], size: 0, risk: "low"};
      node.children[part] = node.children[part] || fresh;
      node = node.children[part];
    });
    node.files.push(file);
  });
  rollup(root);
  return root;
}
function rollup(node) {
  let size = 0;
  let risk = "low";
  node.files.forEach(function (file) {
    size += file.change_events || 1;
    risk = worse(risk, file.risk || "low");
  });
  Object.keys(node.children).forEach(function (name) {
    const child = node.children[name];
    rollup(child);
    size += child.size;
    risk = worse(risk, child.risk);
  });
  node.size = size || 1;
  node.risk = risk;
}
function paint(node) {
  const host = document.getElementById("treemap-body");
  host.replaceChildren();
  const kids = Object.keys(node.children).sort().map(function (name) {
    return node.children[name];
  });
  const items = kids.length ? kids : node.files.map(function (file) {
    return {
      name: file.path,
      size: file.change_events || 1,
      risk: file.risk || "low",
      file: file,
      children: {},
      files: [file]
    };
  });
  const total = items.reduce(function (sum, item) { return sum + (item.size || 1); }, 0) || 1;
  let offset = 0;
  const width = host.clientWidth || 640;
  items.forEach(function (item) {
    const cell = document.createElement("div");
    cell.className = "cell risk-" + (item.risk || "low");
    const span = (item.size || 1) / total * width;
    cell.style.left = offset + "px";
    cell.style.top = "0";
    cell.style.width = span + "px";
    cell.style.height = "360px";
    text(cell, item.name || item.path);
    cell.addEventListener("click", function () { show(item); });
    host.appendChild(cell);
    offset += span;
  });
}
function show(node) {
  const body = document.getElementById("drill-body");
  body.replaceChildren();
  const files = node.files || (node.file ? [node.file] : []);
  if (!files.length) { paint(node); return; }
  const table = document.createElement("table");
  files.forEach(function (file) {
    const tr = document.createElement("tr");
    const path = document.createElement("td");
    text(path, file.path);
    tr.appendChild(path);
    const risk = document.createElement("td");
    text(risk, file.risk);
    tr.appendChild(risk);
    const rule = document.createElement("td");
    text(rule, file.rule);
    tr.appendChild(rule);
    const owners = document.createElement("td");
    if (!file.owners) {
      text(owners, "Identities are omitted in this privacy mode.");
    } else if (!file.owners.length) {
      text(owners, "No scored owner.");
    } else {
      text(owners, file.owners.map(function (owner) {
        const name = owner.handle || "unnamed";
        return name + " ownership_score " + owner.ownership_score
          + " evidence_quality " + owner.evidence_quality;
      }).join("; "));
    }
    tr.appendChild(owners);
    table.appendChild(tr);
  });
  body.appendChild(table);
}
function mapOwners() {
  const body = document.getElementById("map-body");
  if (!REPORT.owners) {
    text(body, "Identities are omitted in this privacy mode.");
    return;
  }
  const table = document.createElement("table");
  REPORT.owners.forEach(function (owner) {
    const tr = document.createElement("tr");
    const who = document.createElement("td");
    text(who, owner.handle || "unnamed");
    const where = document.createElement("td");
    text(where, (owner.directories || []).join(", "));
    tr.appendChild(who);
    tr.appendChild(where);
    table.appendChild(tr);
  });
  body.appendChild(table);
}
function timeline() {
  const body = document.getElementById("timeline-body");
  const table = document.createElement("table");
  (REPORT.timeline || []).forEach(function (point) {
    const tr = document.createElement("tr");
    const when = document.createElement("td");
    text(when, point.period_end);
    const score = document.createElement("td");
    text(score, "historical_activity_confidence " + point.historical_activity_confidence);
    tr.appendChild(when);
    tr.appendChild(score);
    table.appendChild(tr);
  });
  body.appendChild(table);
}
function teams() {
  const body = document.getElementById("team-body");
  if (REPORT.teams_reason) {
    const note = document.createElement("p");
    text(note, REPORT.teams_reason);
    body.appendChild(note);
  }
  const table = document.createElement("table");
  (REPORT.comparisons || []).forEach(function (row) {
    const tr = document.createElement("tr");
    const cells = [row.team, row.cluster, row.jaccard, row.precision, row.recall, row.coverage];
    cells.forEach(function (value) {
      const td = document.createElement("td");
      text(td, value);
      tr.appendChild(td);
    });
    table.appendChild(tr);
  });
  (REPORT.clusters || []).forEach(function (cluster) {
    const tr = document.createElement("tr");
    const name = document.createElement("td");
    text(name, cluster.name);
    const declared = document.createElement("td");
    text(declared, cluster.declared ? "declared" : "observed");
    tr.appendChild(name);
    tr.appendChild(declared);
    table.appendChild(tr);
  });
  body.appendChild(table);
}
overview();
const root = tree();
paint(root);
mapOwners();
timeline();
teams();
</script>
</body>
</html>
"""
