# Sample output

Trimmed `analyze --json` and `drift --json` from this repository (0.5.0). Handles are shown as GitHub usernames. A run without `GITHUB_TOKEN` emits commit emails instead and skips owner-set comparison.

## `checkowners analyze --json`

```json
{
  "model_version": "ownership-v2",
  "inferred": {
    "README.md": {
      "owners": [
        {
          "handle": "@smusali",
          "ownership_score": 0.85,
          "confidence": 0.85,
          "evidence_quality": 0.85,
          "commits": 21,
          "last_commit": "2026-09-15T14:28:35+04:00",
          "signals": {
            "recency": { "score": 0.91, "available": true },
            "frequency": { "score": 1.0, "available": true },
            "blame": { "score": 0.86, "available": true },
            "review": { "available": false }
          }
        }
      ],
      "qualified_owner_count": 1,
      "bus_factor": 1,
      "qualified_owner_count_cap": 3,
      "decay_warnings": []
    },
    "checkowners/analyze.py": {
      "owners": [
        {
          "handle": "@smusali",
          "ownership_score": 0.85,
          "confidence": 0.85,
          "evidence_quality": 0.85,
          "commits": 18,
          "last_commit": "2026-09-15T14:28:35+04:00",
          "signals": {
            "recency": { "score": 0.91, "available": true },
            "frequency": { "score": 1.0, "available": true },
            "blame": { "score": 0.86, "available": true },
            "review": { "available": false }
          }
        }
      ],
      "qualified_owner_count": 1,
      "bus_factor": 1,
      "qualified_owner_count_cap": 3,
      "decay_warnings": []
    }
  },
  "deprecated_keys": ["bus_factor", "confidence"]
}
```

A full run on this repo inferred 66 paths. `qualified_owner_count` is capped by `analysis.top_n_owners` (default 3). `bus_factor` is a deprecated alias of that count.

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
  ],
  "deprecated_keys": ["bus_factor"]
}
```

`docs/CODEOWNERS` is a generated `* @smusali` rule, so coverage is complete. Without a token the owner *sets* are not compared (email vs `@handle`). The Action's dogfood comment on a clean PR is the resolved form: "CheckOwners: no drift detected."
