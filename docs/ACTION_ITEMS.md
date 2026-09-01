# checkOwners Action Items

Every action item derived from `docs/PROJECT_STATE_AND_ROADMAP.md`, cross-checked against the
current source tree. Each item is atomic, independently mergeable, and filed as a GitHub issue.

**ID scheme:** `<epic-letter><number>`. **Priority:** P0 (release-blocking) → P3 (post-adoption).
**Effort:** XS ≤ 1h · S ≤ 4h · M ≤ 2d · L ≤ 1w · XL > 1w.

| Epic | Theme | Items |
|---|---|---|
| A | Identity, release, and provenance | A1–A4 |
| B | GitHub Action correctness and supply chain | B1–B9 |
| C | Ownership metric correctness | C1–C9 |
| D | Git evidence fidelity | D1–D7 |
| E | Drift, generation, validation, and command surface | E1–E12 |
| F | Contracts and policy | F1–F6 |
| G | Module methodology | G1–G7 |
| H | Testing and quality assurance | H1–H7 |
| I | Privacy, ethics, and security | I1–I3 |
| J | Performance | J1–J4 |
| K | Distribution and integration | K1–K7 |
| L | Documentation and positioning | L1–L4 |
| M | Validation and proof | M1–M4 |
| N | Community and governance | N1–N2 |

---

## Epic A — Identity, Release, and Provenance

| ID | Title | Priority | Effort | Depends on | Issue |
|---|---|---|---|---|---|
| A1 | Rewrite every stale organization reference to `smusali/checkowners` and add a CI guard | P0 | S | — | #10 |
| A2 | Make README documentation links absolute so they resolve on PyPI | P0 | XS | — | #11 |
| A3 | Ship `0.5.1` and publish floating `v0` / `v0.5` Action tags | P0 | S | A1, A2, B1–B6 | #12 |
| A4 | Correct misdated CHANGELOG entries and repopulate `Unreleased` | P0 | XS | — | #13 |

## Epic B — GitHub Action Correctness and Supply Chain

| ID | Title | Priority | Effort | Depends on | Issue |
|---|---|---|---|---|---|
| B1 | Wire `GITHUB_TOKEN` into every CLI step of the Action | P0 | S | — | #14 |
| B2 | Pin the Action's own package install to the Action tag, with a hash-locked requirements file | P0 | S | — | #15 |
| B3 | SHA-pin third-party actions and add dependency automation | P0 | XS | — | #16 |
| B4 | Make pull-request commenting fail-soft and fork-safe | P0 | S | B1 | #17 |
| B5 | Fix `GITHUB_OUTPUT` delimiter collision and unbounded payload size | P0 | S | — | #18 |
| B6 | Paginate the managed-comment lookup and escape interpolated Markdown | P0 | S | B4 | #19 |
| B7 | Collapse the two CI implementations onto `checkowners github-action` | P0 | M | B1, B5 | #20 |
| B8 | Unify the `drift.mode` default and document every environment variable | P0 | XS | — | #21 |
| B9 | Cache pip and `~/.checkowners` in the Action | P1 | S | F6, J1 | #22 |

## Epic C — Ownership Metric Correctness

| ID | Title | Priority | Effort | Depends on | Issue |
|---|---|---|---|---|---|
| C1 | Rename `bus_factor` to `qualified_owner_count` and document the `top_n_owners` cap | P0 | S | F1 | #23 |
| C2 | Implement knowledge-concentration primitives: shares, top-owner share, effective owners, TF50/75/90 | P0 | M | C1 | #24 |
| C3 | Replace `repo_average` with a criticality-weighted risk distribution | P0 | M | C2 | #25 |
| C4 | Add `checkowners simulate --remove` departure simulation | P1 | M | C2 | #26 |
| C5 | Renormalize confidence over available signals and add `evidence_quality` | P0 | M | F1 | #27 |
| C6 | Replace `min_commits` eligibility with evidence-weighted qualification | P0 | M | C5 | #28 |
| C7 | Make analysis deterministic: `--as-of`, `SOURCE_DATE_EPOCH`, HEAD commit time | P0 | M | — | #29 |
| C8 | Adaptive recency half-life and separated active / historical expertise | P1 | M | C7 | #30 |
| C9 | Add the change-survival signal | P2 | L | C5, D3 | #31 |

## Epic D — Git Evidence Fidelity

| ID | Title | Priority | Effort | Depends on | Issue |
|---|---|---|---|---|---|
| D1 | Blame fidelity: `-w`, `-M`, `-C`, and `.git-blame-ignore-revs` | P0 | M | — | #32 |
| D2 | `.mailmap` support across `git log` and `git blame` | P0 | S | — | #33 |
| D3 | Rename and copy lineage: a logical file identity that survives refactors | P0 | L | — | #34 |
| D4 | `Co-authored-by` trailers and explicit merge-strategy handling | P1 | M | — | #35 |
| D5 | Honor `.gitattributes` `linguist-generated` and `linguist-vendored` | P1 | S | — | #36 |
| D6 | Identity-resolution safeguards and a canonical contributor graph | P1 | L | D2 | #37 |
| D7 | Contributor status model and expanded bot detection | P2 | M | D6 | #38 |

## Epic E — Drift, Generation, Validation, and Command Surface

| ID | Title | Priority | Effort | Depends on | Issue |
|---|---|---|---|---|---|
| E1 | Team-aware drift with an explicit `unverifiable` state | P0 | L | F5 | #39 |
| E2 | Drift severity tiers and drift direction classification | P1 | M | E1, C5 | #40 |
| E3 | Round-trip verification and a size guard for generated CODEOWNERS | P0 | M | — | #41 |
| E4 | Refuse lossy bracket-path consolidation unless explicitly allowed | P1 | S | E3 | #42 |
| E5 | Hybrid CODEOWNERS: manual blocks plus generated blocks | P1 | M | E3 | #43 |
| E6 | `checkowners audit`: the complete CODEOWNERS auditor and default front door | P1 | XL | E1, E7, F3 | #44 |
| E7 | `checkowners doctor`: verify ownership configuration against reality | P1 | L | B1 | #45 |
| E8 | `checkowners explain` and `checkowners owners` | P0 | M | C5 | #46 |
| E9 | `checkowners diff`, `suggest --pr`, and `--diff-only` | P1 | L | C5, J1 | #47 |
| E10 | `checkowners risk`: unify concentration, decay, hotspots, and criticality | P1 | L | C2, C3 | #48 |
| E11 | Baseline ratchet and suppressions with reasons and expiry | P1 | M | F2 | #49 |
| E12 | Make `sync --pr` the default and direct commit the escape hatch | P1 | M | — | #50 |

## Epic F — Contracts and Policy

| ID | Title | Priority | Effort | Depends on | Issue |
|---|---|---|---|---|---|
| F1 | Model and metric versioning, and configuration schema v2 | P0 | M | — | #51 |
| F2 | Versioned JSON Schema per command with a provenance envelope | P0 | M | F1 | #52 |
| F3 | A documented, stable exit-code contract | P0 | S | — | #53 |
| F4 | A policy engine separating enforcement from information | P1 | M | F3 | #54 |
| F5 | Analysis completeness, resource budgets, and rate-limit awareness | P0 | M | F2 | #55 |
| F6 | `--offline`, cache commands, atomic state writes, and a staleness contract | P0 | M | F1 | #56 |

## Epic G — Module Methodology

| ID | Title | Priority | Effort | Depends on | Issue |
|---|---|---|---|---|---|
| G1 | Replace connected-component topology with weighted community detection | P2 | L | — | #57 |
| G2 | Real review-load semantics for `balance`, with explicit provenance | P1 | M | G6 | #58 |
| G3 | Version or reconstruct the historical trends metric | P1 | M | F1 | #59 |
| G4 | Rename the ownership graph, expand the ontology, add risk analytics and exports | P2 | L | — | #60 |
| G5 | Dependency-aware onboarding, honestly framed | P2 | L | G4 | #61 |
| G6 | Fix review-sampling window bias | P1 | M | F5 | #62 |
| G7 | Retire or rebuild the webhook notifier | P1 | S | — | #65 |

## Epic H — Testing and Quality Assurance

| ID | Title | Priority | Effort | Depends on | Issue |
|---|---|---|---|---|---|
| H1 | Real-git integration fixtures alongside the mocked unit tests | P0 | M | — | #66 |
| H2 | A public CODEOWNERS compatibility corpus and property-based fuzzing | P0 | L | H1 | #67 |
| H3 | Model golden tests that encode the project's ownership philosophy | P1 | M | H1 | #68 |
| H4 | Mutation testing on the correctness-critical modules | P1 | M | H1, H2 | #69 |
| H5 | Enforce the coverage floor and expand the ruff rule set | P0 | XS | — | #70 |
| H6 | Cross-platform and git-version test matrices | P1 | M | H1 | #71 |
| H7 | A conformance harness against GitHub's CODEOWNERS errors endpoint | P1 | M | H2 | #72 |

## Epic I — Privacy, Ethics, and Security

| ID | Title | Priority | Effort | Depends on | Issue |
|---|---|---|---|---|---|
| I1 | Privacy modes: anonymization, aggregate-only, redaction, hashing, exclusion | P0 | M | F1 | #73 |
| I2 | `PRIVACY.md`, the data-flow model, the non-performance-metric statement, and `cache purge` | P0 | S | I1, F6 | #74 |
| I3 | Security posture: threat model, Scorecard, CodeQL, dependency audit, SBOM, least privilege | P1 | M | B3 | #75 |

## Epic J — Performance

| ID | Title | Priority | Effort | Depends on | Issue |
|---|---|---|---|---|---|
| J1 | Incremental analysis keyed on `analyzed_ref` | P1 | XL | F6, D3 | #76 |
| J2 | Blob-SHA-keyed blame cache | P1 | L | F6 | #77 |
| J3 | Replace subprocess-per-file blame fan-out | P2 | XL | J2 | #78 |
| J4 | A reproducible performance benchmark harness with published results | P1 | M | — | #79 |

## Epic K — Distribution and Integration

| ID | Title | Priority | Effort | Depends on | Issue |
|---|---|---|---|---|---|
| K1 | Ship `.pre-commit-hooks.yaml` | P1 | XS | — | #80 |
| K2 | Packaging hygiene: composing extras, dev extra, upper bounds, single-sourced version, 3.14 | P0 | S | — | #81 |
| K3 | Distribution channels: `uvx`/`pipx` docs, Docker image, standalone binaries, Homebrew | P1 | L | K2 | #82 |
| K4 | SARIF output for GitHub code scanning | P1 | M | F2, F3 | #83 |
| K5 | GitHub Checks, annotations, step summary, reusable workflow, JUnit XML | P1 | M | B7 | #84 |
| K6 | A self-contained interactive HTML report | P2 | L | F2 | #85 |
| K7 | A stable library API, machine-readable graph exports, and forge abstraction | P2 | XL | G4, F2 | #86 |

## Epic L — Documentation and Positioning

| ID | Title | Priority | Effort | Depends on | Issue |
|---|---|---|---|---|---|
| L1 | Rewrite the README problem-first and surface the trust assets | P0 | S | A1, A2 | #87 |
| L2 | Replace the comparison table and remove the "first" claim | P0 | S | — | #88 |
| L3 | Publish `METHODOLOGY.md` and `limitations.md` | P0 | M | C1, C5 | #89 |
| L4 | Build the documentation site and information architecture | P1 | M | L1, L3 | #90 |

## Epic M — Validation and Proof

| ID | Title | Priority | Effort | Depends on | Issue |
|---|---|---|---|---|---|
| M1 | The validation study: labeled corpus, calibration, weight optimization | P1 | XL | C5, C7, D1 | #91 |
| M2 | `checkowners calibrate` and `calibrate --fit` | P2 | L | M1 | #92 |
| M3 | The public false-positive ledger | P1 | S | M1 | #93 |
| M4 | The public ownership-risk dataset, 50 open-source audits, and public dogfooding | P1 | L | E6, K6 | #94 |

## Epic N — Community and Governance

| ID | Title | Priority | Effort | Depends on | Issue |
|---|---|---|---|---|---|
| N1 | Community infrastructure: templates, discussions, roadmap, funding, good-first-issues | P0 | S | — | #95 |
| N2 | Governance: a second maintainer, contributor ladder, ADRs, citation, open-core boundary | P1 | M | N1 | #96 |

---

## Issue Index

[A1](https://github.com/smusali/checkowners/issues/10) · [A2](https://github.com/smusali/checkowners/issues/11) · [A3](https://github.com/smusali/checkowners/issues/12) · [A4](https://github.com/smusali/checkowners/issues/13) · [B1](https://github.com/smusali/checkowners/issues/14) · [B2](https://github.com/smusali/checkowners/issues/15) · [B3](https://github.com/smusali/checkowners/issues/16) · [B4](https://github.com/smusali/checkowners/issues/17) · [B5](https://github.com/smusali/checkowners/issues/18) · [B6](https://github.com/smusali/checkowners/issues/19) · [B7](https://github.com/smusali/checkowners/issues/20) · [B8](https://github.com/smusali/checkowners/issues/21) · [B9](https://github.com/smusali/checkowners/issues/22) · [C1](https://github.com/smusali/checkowners/issues/23) · [C2](https://github.com/smusali/checkowners/issues/24) · [C3](https://github.com/smusali/checkowners/issues/25) · [C4](https://github.com/smusali/checkowners/issues/26) · [C5](https://github.com/smusali/checkowners/issues/27) · [C6](https://github.com/smusali/checkowners/issues/28) · [C7](https://github.com/smusali/checkowners/issues/29) · [C8](https://github.com/smusali/checkowners/issues/30) · [C9](https://github.com/smusali/checkowners/issues/31) · [D1](https://github.com/smusali/checkowners/issues/32) · [D2](https://github.com/smusali/checkowners/issues/33) · [D3](https://github.com/smusali/checkowners/issues/34) · [D4](https://github.com/smusali/checkowners/issues/35) · [D5](https://github.com/smusali/checkowners/issues/36) · [D6](https://github.com/smusali/checkowners/issues/37) · [D7](https://github.com/smusali/checkowners/issues/38) · [E1](https://github.com/smusali/checkowners/issues/39) · [E2](https://github.com/smusali/checkowners/issues/40) · [E3](https://github.com/smusali/checkowners/issues/41) · [E4](https://github.com/smusali/checkowners/issues/42) · [E5](https://github.com/smusali/checkowners/issues/43) · [E6](https://github.com/smusali/checkowners/issues/44) · [E7](https://github.com/smusali/checkowners/issues/45) · [E8](https://github.com/smusali/checkowners/issues/46) · [E9](https://github.com/smusali/checkowners/issues/47) · [E10](https://github.com/smusali/checkowners/issues/48) · [E11](https://github.com/smusali/checkowners/issues/49) · [E12](https://github.com/smusali/checkowners/issues/50) · [F1](https://github.com/smusali/checkowners/issues/51) · [F2](https://github.com/smusali/checkowners/issues/52) · [F3](https://github.com/smusali/checkowners/issues/53) · [F4](https://github.com/smusali/checkowners/issues/54) · [F5](https://github.com/smusali/checkowners/issues/55) · [F6](https://github.com/smusali/checkowners/issues/56) · [G1](https://github.com/smusali/checkowners/issues/57) · [G2](https://github.com/smusali/checkowners/issues/58) · [G3](https://github.com/smusali/checkowners/issues/59) · [G4](https://github.com/smusali/checkowners/issues/60) · [G5](https://github.com/smusali/checkowners/issues/61) · [G6](https://github.com/smusali/checkowners/issues/62) · [G7](https://github.com/smusali/checkowners/issues/65) · [H1](https://github.com/smusali/checkowners/issues/66) · [H2](https://github.com/smusali/checkowners/issues/67) · [H3](https://github.com/smusali/checkowners/issues/68) · [H4](https://github.com/smusali/checkowners/issues/69) · [H5](https://github.com/smusali/checkowners/issues/70) · [H6](https://github.com/smusali/checkowners/issues/71) · [H7](https://github.com/smusali/checkowners/issues/72) · [I1](https://github.com/smusali/checkowners/issues/73) · [I2](https://github.com/smusali/checkowners/issues/74) · [I3](https://github.com/smusali/checkowners/issues/75) · [J1](https://github.com/smusali/checkowners/issues/76) · [J2](https://github.com/smusali/checkowners/issues/77) · [J3](https://github.com/smusali/checkowners/issues/78) · [J4](https://github.com/smusali/checkowners/issues/79) · [K1](https://github.com/smusali/checkowners/issues/80) · [K2](https://github.com/smusali/checkowners/issues/81) · [K3](https://github.com/smusali/checkowners/issues/82) · [K4](https://github.com/smusali/checkowners/issues/83) · [K5](https://github.com/smusali/checkowners/issues/84) · [K6](https://github.com/smusali/checkowners/issues/85) · [K7](https://github.com/smusali/checkowners/issues/86) · [L1](https://github.com/smusali/checkowners/issues/87) · [L2](https://github.com/smusali/checkowners/issues/88) · [L3](https://github.com/smusali/checkowners/issues/89) · [L4](https://github.com/smusali/checkowners/issues/90) · [M1](https://github.com/smusali/checkowners/issues/91) · [M2](https://github.com/smusali/checkowners/issues/92) · [M3](https://github.com/smusali/checkowners/issues/93) · [M4](https://github.com/smusali/checkowners/issues/94) · [N1](https://github.com/smusali/checkowners/issues/95) · [N2](https://github.com/smusali/checkowners/issues/96)

## Release Mapping

| Milestone | Action items |
|---|---|
| `0.5.1` — unbreak | A1 · A2 · A3 · A4 · B1 · B2 · B3 · B4 · B5 · B6 · B8 · C1 · H5 · K2 · L2 · N1 |
| `0.6.0` — correctness and trust | B7 · C5 · C6 · C7 · D1 · D2 · D5 · E3 · E4 · E8 · E11 · F1 · F2 · F3 · F5 · F6 · G7 · H1 · H2 · H3 · I1 · I2 · I3 · K1 · L1 · L3 |
| `0.7.0` — ownership risk and scale | B9 · C2 · C3 · C4 · C8 · D4 · E10 · E12 · F4 · G2 · G3 · G6 · H6 · J1 · J2 · J4 · K3 · K4 · K5 · L4 |
| `0.8.0` — audit and rigor | D6 · E1 · E2 · E6 · E7 · E9 · H4 · H7 · M1 · M3 |
| `0.9.0` — research and reporting | C9 · D3 · D7 · G1 · G4 · J3 · K6 · M2 · M4 |
| `1.0.0` — the contract | G5 · K7 · N2 (plus schema freeze, semver policy, published conformance corpus) |

## Critical Path

```text
A1 ─┬─> A3 ──> (public credibility restored)
B1 ─┤
B2 ─┤
B3 ─┤
B4 ─┘

F1 ──> C5 ──> C6 ──> E8 ──> M1 ──> M3
 │      │
 │      └──> C1 ──> C2 ──> C3 ──> E10
 │
 └──> F2 ──> F5 ──> E1 ──> E6 ──> M4

D1 ──> D2 ──> D6         C7 ──> H3
 └──> D3 ──> J1 ──> E9   H1 ──> H2 ──> H4
```
