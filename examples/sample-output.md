# Sample output

Trimmed `analyze --json` and `drift --json`. Handles are shown as GitHub usernames. A run without `GITHUB_TOKEN` emits commit emails instead and skips owner-set comparison. Envelope fields (`schema_version`, `checkowners_version`, `models`, `repository`, `head_sha`, `generated_at`, `analysis_completeness`) are omitted here.

## `checkowners analyze --json`

```json
{
  "inferred": {
    "README.md": {
      "owners": [
        {
          "identity": "@smusali",
          "ownership_score": 0.85,
          "evidence_quality": 0.85,
          "commits": 21,
          "last_commit": "2026-09-15T14:28:35+04:00",
          "signals": {
            "recency": 0.91,
            "frequency": 1.0,
            "blame": 0.86
          }
        }
      ],
      "qualified_owner_count": 1,
      "qualified_owner_count_cap": 3,
      "decay_warnings": []
    }
  }
}
```

`qualified_owner_count` is capped by `analysis.top_n_owners` (default 3).

## `checkowners drift --json`

```json
{
  "stale": [],
  "missing": [],
  "changed": [],
  "drift_detected": false,
  "severity": "low",
  "max_confidence_delta": 0.0,
  "notes": [
    "inferred owners are commit emails but CODEOWNERS uses @handles; owner comparison skipped. Set GITHUB_TOKEN (github.resolve_handles) to compare owner sets."
  ]
}
```

`docs/CODEOWNERS` is a generated `* @smusali` rule, so coverage is complete. Without a token the owner *sets* are not compared (email vs `@handle`). The Action's dogfood comment on a clean PR is the resolved form: "CheckOwners: no drift detected."
