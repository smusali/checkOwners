# Guides

## Monorepos

`output.consolidate` collapses a directory into one rule when every inferred file under it shares an owner set. `analysis.follow_renames` keeps history on the current path after a rename or same-commit copy. A repository that is already large should start with the [large-repository](#large-repositories) rollout instead of failing CI on the first drift run. Sample configs are in the [examples directory](https://github.com/smusali/checkowners/tree/main/examples).

## GitHub teams

Set `github.org` and leave `github.resolve_teams` on when a rule should name `@org/team` instead of the expanded members. Team checks and review coverage need `github.api_enabled` and `GITHUB_TOKEN`. An offline run does not treat a cached team answer as `aligned`. Token scopes are in the [FAQ](FAQ.md#what-token-scopes-are-needed).

## Existing CODEOWNERS

`generate` keeps a `# checkowners:manual` block byte for byte and rewrites only the generated block. `output.block_order` chooses which block wins when they overlap. `drift` reports a manual rule that differs from observed expertise for review rather than as `changed`. The file layout is in [Generated CODEOWNERS](USAGE.md#generated-codeowners).

## Large repositories

Enabling drift on a repository that already has thousands of mismatches fails CI on day one. Capture today's findings, commit the file, and fail only on new ones.

```bash
checkowners baseline create
git add .checkowners-baseline.json
```

Then pass the file to every gate:

```bash
checkowners drift --baseline .checkowners-baseline.json
```

Or set `drift.baseline_file: .checkowners-baseline.json` in `.github/checkowners.yml`, or pass the Action `baseline` input (exported as `CHECKOWNERS_BASELINE`). The CLI flag wins over the environment variable, which wins over the config key. The pre-commit drift hook expects `.checkowners-baseline.json` at the repository root.

The file is JSON (`schema_version: "1"`) with a sorted `findings` list. Each finding is identified by `rule` plus `path` plus `owners`. Line numbers and CODEOWNERS order are not part of the identity, so reordering the file does not invalidate the baseline.

Rules that can appear: `missing`, `stale`, `changed`, `single-expert` (qualified-owner critical paths), and `invalid-owner`. Decay is not snapshotted.

When a finding is genuinely fixed, the matching baseline row is reported as stale (`stale_baseline` in JSON and "Stale baseline" in the summary). That does not fail the run. Re-run `checkowners baseline create` to shrink the file.

### Policy tuning

Start from the default policy. Drift, a single expert, unowned paths, and incomplete analysis warn and do not fail. Invalid owners fail. `--exit-zero`, placed before the command name, turns exit code 3 into 0 after the report is printed, which is the informational first CI run.

After the baseline is committed, set `fail: true` on one check at a time. `policy.drift` is the usual first gate. A novel row fails only when its check has `fail: true`, or when it sits under `security.paths` and breaks that block's stricter rule. A baselined or suppressed row is reported and does not fail the run.

`security.paths` applies `minimum_effective_owners`, `require_declared_owner`, and `require_active_expert` to the paths you name. Leave the list empty until you want that stricter bar. Do not use the baseline to hide a path you intend that block to enforce.

### Suppression hygiene

For a named exception that should not live in the bulk snapshot, add an explicit suppression. A reason is required. Expiry is optional. An expired row fails the command instead of keeping the silence:

```yaml
suppressions:
  - path: legacy/**
    rule: single-expert
    expires: 2026-12-31
    reason: "Scheduled for retirement in Q4"
```

`path` uses CODEOWNERS pattern matching. `rule` must be one of `missing`, `stale`, `changed`, `single-expert`, or `invalid-owner`. `drift` and `github-action` apply suppressions first, then the baseline. Both commands print `baselined`, `suppressed`, and `stale_baseline` counts so the remaining debt stays visible.

A suppression is a named exception, not a second baseline. Remove it when the reason no longer holds. A suppression without a reason, with an unknown rule, or with a date that is not `YYYY-MM-DD` is a configuration error at load.

## Offline mode

Place `--offline` before the command name. The run opens no network connection. Review evidence and team verification are unavailable, and the command says so. `github.token` is rejected in the config file. The only token source is `GITHUB_TOKEN`, and an offline run does not use it.

## Security-sensitive repositories

Put the paths that need a higher bar in `security.paths`. The block can require two effective owners, a declared owner, and an active expert. Privacy controls, including redaction and what the cache holds, are in [Privacy](PRIVACY.md).

Zero telemetry, ever, by default. CheckOwners does not phone home. Network use is opt-in GitHub API access.
