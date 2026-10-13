"""Which findings block after baseline and suppression filtering."""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

from checkowners.expertise import path_matches_glob
from checkowners.models import (
    CheckPolicy,
    Config,
    Finding,
    FindingRule,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
    PolicyCheck,
    PolicyConfig,
    PolicyLevel,
    SecurityPolicy,
    knowledge_concentration,
)
from checkowners.patterns import CodeownersRule, match_path
from checkowners.validate import ValidationFinding

_MISSING_FILE = "CODEOWNERS file not found"
_INVALID_OWNER = "Invalid owner format: "


class CheckPolicyJson(TypedDict):
    fail: bool
    severity: PolicyLevel


class SecurityPolicyJson(TypedDict):
    paths: list[str]
    minimum_effective_owners: int
    require_declared_owner: bool
    require_active_expert: bool


class EffectivePolicyJson(TypedDict):
    drift: CheckPolicyJson
    single_expert: CheckPolicyJson
    incomplete_analysis: CheckPolicyJson
    unowned: CheckPolicyJson
    invalid_owner: CheckPolicyJson
    security: SecurityPolicyJson


def check_of(rule: FindingRule) -> PolicyCheck | None:
    """Return the policy check for `rule`, or None when that rule is not gated."""
    if rule in {"stale", "changed"}:
        return "drift"
    if rule == "missing":
        return "unowned"
    if rule == "single-expert":
        return "single_expert"
    if rule == "invalid-owner":
        return "invalid_owner"
    return None


def effective_policy(config: Config) -> EffectivePolicyJson:
    """Return the resolved policy and security block for `config`."""
    policy = config.policy
    security = config.security
    return {
        "drift": _check_json(policy.drift),
        "single_expert": _check_json(policy.single_expert),
        "incomplete_analysis": _check_json(policy.incomplete_analysis),
        "unowned": _check_json(policy.unowned),
        "invalid_owner": _check_json(policy.invalid_owner),
        "security": {
            "paths": list(security.paths),
            "minimum_effective_owners": security.minimum_effective_owners,
            "require_declared_owner": security.require_declared_owner,
            "require_active_expert": security.require_active_expert,
        },
    }


def invalid_owner_findings(
    repo_root: Path,
    codeowners_path: Path,
    errors: list[ValidationFinding],
    *,
    include_missing: bool,
) -> tuple[Finding, ...]:
    """Return `invalid-owner` findings for `errors` on `codeowners_path`.

    `include_missing` keeps a missing-file error. Line errors always count.
    """
    relative = _relative_codeowners(repo_root, codeowners_path)
    findings: list[Finding] = []
    for error in errors:
        if error.line_number == 0 and error.message == _MISSING_FILE and not include_missing:
            continue
        suffix = f":{error.line_number}" if error.line_number else ""
        findings.append(
            Finding(
                rule="invalid-owner",
                path=f"{relative}{suffix}",
                owners=_owner_token(error),
            )
        )
    return tuple(findings)


def security_findings(
    ownership: OwnershipMap,
    config: Config,
    rules: tuple[CodeownersRule, ...],
) -> tuple[Finding, ...]:
    """Return stricter findings for analyzed paths under `config.security`."""
    security = config.security
    if not security.paths:
        return ()
    findings: list[Finding] = []
    for path, path_ownership in ownership.paths.items():
        if not _matches_security(path, security):
            continue
        handles = tuple(owner.handle for owner in _owners(path_ownership))
        if _below_minimum(path_ownership, config):
            findings.append(Finding(rule="single-expert", path=path, owners=handles))
        if security.require_declared_owner and not _has_declared_owner(rules, path):
            findings.append(Finding(rule="missing", path=path, owners=()))
        if security.require_active_expert and _lacks_active_expert(path_ownership):
            findings.append(Finding(rule="single-expert", path=path, owners=handles))
    return tuple(findings)


def blocks(
    finding: Finding,
    config: Config,
    ownership: OwnershipMap | None,
    rules: tuple[CodeownersRule, ...],
) -> bool:
    """Return whether novel `finding` fails the run under `config`."""
    check = check_of(finding.rule)
    if check is None:
        return False
    if _policy_check(config.policy, check).fail:
        return True
    return _security_blocks(finding, config, ownership, rules)


def blocking_findings(
    findings: tuple[Finding, ...],
    config: Config,
    ownership: OwnershipMap | None,
    rules: tuple[CodeownersRule, ...],
) -> tuple[Finding, ...]:
    """Return the novel `findings` that fail the run."""
    return tuple(item for item in findings if blocks(item, config, ownership, rules))


def _policy_check(policy: PolicyConfig, check: PolicyCheck) -> CheckPolicy:
    selected = {
        "drift": policy.drift,
        "single_expert": policy.single_expert,
        "incomplete_analysis": policy.incomplete_analysis,
        "unowned": policy.unowned,
        "invalid_owner": policy.invalid_owner,
    }
    return selected[check]


def _check_json(policy: CheckPolicy) -> CheckPolicyJson:
    return {"fail": policy.fail, "severity": policy.severity}


def _relative_codeowners(repo_root: Path, codeowners_path: Path) -> str:
    try:
        return codeowners_path.relative_to(repo_root).as_posix()
    except ValueError:
        return codeowners_path.as_posix()


def _owner_token(error: ValidationFinding) -> tuple[str, ...]:
    if error.message.startswith(_INVALID_OWNER):
        token = error.message.removeprefix(_INVALID_OWNER).strip()
        return (token,) if token else ()
    if error.message == _MISSING_FILE:
        return ()
    return (error.message,)


def _owners(path_ownership: PathOwnership) -> tuple[OwnerEntry, ...]:
    if path_ownership.scored_owners:
        return path_ownership.scored_owners
    return path_ownership.owners


def _matches_security(path: str, security: SecurityPolicy) -> bool:
    return any(path_matches_glob(path, pattern) for pattern in security.paths)


def _below_minimum(path_ownership: PathOwnership, config: Config) -> bool:
    scores = tuple(owner.ownership_score for owner in _owners(path_ownership))
    effective = knowledge_concentration(
        scores,
        config.risk.truck_factor_thresholds,
    ).effective_owners
    return effective < config.security.minimum_effective_owners


def _has_declared_owner(rules: tuple[CodeownersRule, ...], path: str) -> bool:
    rule = match_path(rules, path.lstrip("/"))
    return rule is not None and bool(rule.owners)


def _lacks_active_expert(path_ownership: PathOwnership) -> bool:
    owners = _owners(path_ownership)
    if not owners:
        return False
    statuses: list[str] = []
    for owner in owners:
        freshness = owner.freshness
        if freshness is None:
            return False
        statuses.append(freshness.status)
    return all(status != "stable" for status in statuses)


def _security_blocks(
    finding: Finding,
    config: Config,
    ownership: OwnershipMap | None,
    rules: tuple[CodeownersRule, ...],
) -> bool:
    if ownership is None or not config.security.paths:
        return False
    path_ownership = _path_ownership(finding.path, ownership)
    if path_ownership is None or not _matches_security(finding.path, config.security):
        return False
    if finding.rule == "missing":
        undeclared = not _has_declared_owner(rules, finding.path)
        return config.security.require_declared_owner and undeclared
    if finding.rule != "single-expert":
        return False
    low = _below_minimum(path_ownership, config)
    inactive = config.security.require_active_expert and _lacks_active_expert(path_ownership)
    return low or inactive


def _path_ownership(path: str, ownership: OwnershipMap) -> PathOwnership | None:
    if path in ownership.paths:
        return ownership.paths[path]
    stripped = path.lstrip("/")
    if stripped in ownership.paths:
        return ownership.paths[stripped]
    return None
