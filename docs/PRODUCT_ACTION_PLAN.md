# Product Feedback Action Plan

Actionable breakdown of the external product review received 2026-07-24, mapped
onto the current codebase (v0.5.0). Every review point is converted into a
tracked step with an ID, the exact files it touches, and acceptance criteria.
Work is grouped into workstreams (A–N) and sequenced by the review's P0/P1/P2
priorities in the [Roadmap](#roadmap) section at the end.

**Review verdict being acted on:** the implementation is more credible than the
product story. The repository presents a broad ownership-analysis platform
before proving the one workflow that makes someone install it. The fix is to
make the next version smaller, safer, and more evidentiary.

## Baseline scores (from the review)

| Dimension | Score |
|---|---|
| Technical foundation | 7.5/10 |
| Problem quality | 8/10 |
| Product focus | 5/10 |
| First-run experience | 5/10 |
| Trust and evidence | 5/10 |
| README conversion | 5/10 |
| GitHub Action conversion | 4/10 |
| HN readiness (after revised post) | 6.5/10 |
| Urgent adoption today | 3.5/10 |
| Potential after narrowing and validation | 8/10 |

## Guiding constraints (apply to every step below)

1. **One target user:** platform / DevEx leader.
2. **One urgent job:** detect and correct stale or risky ownership.
3. **One primary output:** an explained, reviewable diff.
4. **One distribution path:** the GitHub Action.
5. **One proof asset:** a public case study.
6. **One trust promise:** no silent changes, no black-box score, no LLM required.
7. **The wedge:** drift detection plus a proposed change. Bus factor, decay,
   topology, balance, onboarding, and trends *support* that wedge; they never
   compete with it in positioning, docs ordering, or Action defaults.
8. **Vocabulary:** keep four concepts separate everywhere (models, CLI output,
   docs, PR comments): **observed** ownership (what git history shows),
   **recommended** ownership (what checkOwners proposes), **declared**
   ownership (what CODEOWNERS says), **approved** ownership (what a human
   accepted).
9. **Trust framing:** git history is evidence, not ground truth. Every surface
   that shows a score must make that impossible to miss.
10. Every feature must tie to one of these incidents/decisions: a critical path
    with only one person who understands it; a production or security PR going
    to the wrong reviewer; CODEOWNERS failing because a team disappeared; an
    offboarding event exposing undocumented ownership; a high-risk subsystem
    with no backup; overloaded reviewers while qualified people stay invisible.

---

## Workstream A — Positioning and README (P0)

The README currently asks users to believe everything at once
(`README.md:9`): inference, confidence scoring, a knowledge graph, decay, bus
factor, topology, balancing, onboarding, trends, CI, and a composite Action.
That triggers either "too much to learn" or "inflated claims."

- [ ] **A1. Replace the README opening paragraph** (`README.md:9`) with the
  review's approved copy, verbatim:

  > checkOwners compares declared CODEOWNERS with evidence from git history.
  > It identifies stale or risky ownership, explains why an owner was
  > inferred, and proposes a reviewable CODEOWNERS change. It runs locally and
  > in GitHub Actions, with no LLM required.

  *Acceptance:* the first paragraph mentions only drift/audit + evidence +
  reviewable change; the platform-feature list is gone from the opening.

- [ ] **A2. Remove the "first CODEOWNERS tool" claim** (`README.md:11`
  blockquote). It is unprovable and invites an unnecessary argument.
  *Acceptance:* no superlative/priority claim ("first", "only") anywhere in
  README, `action.yml` description, or PyPI metadata (`pyproject.toml`
  description field).

- [ ] **A3. Rename "knowledge graph" → "ownership graph" / "ownership map"**
  in user-facing copy until the broader claim is earned: `README.md:9`,
  `README.md:58` (graph command row), `docs/USAGE.md`, `action.yml`
  description, `graph.py` docstrings, `CLAUDE.md` summary line.
  *Acceptance:* "knowledge graph" appears nowhere user-facing.

- [ ] **A4. Restructure the README around the first user job** so a new user
  learns in 30 seconds: (1) what command to run, (2) what files are read,
  (3) what output appears, (4) what decision the output supports, (5) whether
  the repository changes automatically (answer: never, without approval).
  Order: positioning → real output example (A5) → quick start (audit-first,
  see B1) → Action snippet (H2) → limitations (J2) → everything else.
  *Acceptance:* `analyze`/`generate`-first quick start is replaced by the
  audit/drift-first flow; the 16-command table moves below the fold under an
  "All commands" heading framed as the expert surface.

- [ ] **A5. Make output the hero: one copied, real example near the top** of
  the README showing existing CODEOWNERS excerpt, inferred result, evidence
  summary, confidence + bus factor, drift finding, generated diff, and the
  human decision. Target shape (populate with real output, not invented data):

  ```
  Path: services/payments/
  Declared: @payments-team
  Inferred: @alice, @bob
  Confidence: medium
  Bus factor: 1
  Drift: declared team has no recent supporting activity
  Evidence: 180-day git history, PR reviews enabled
  Recommendation: add @bob as backup owner
  Action: generate reviewable diff
  ```

  *Acceptance:* the example is captured from a real run (case study repo, see
  Workstream L), plus a screenshot or terminal recording (asciinema/VHS)
  embedded or linked. People trust output more than a feature list.

- [ ] **A6. Adopt the recommended final positioning** as the canonical
  two-line description reused across README, PyPI, `action.yml`, and the
  Marketplace listing:

  > checkOwners finds stale and risky CODEOWNERS rules by comparing declared
  > ownership with evidence from git history, then proposes a reviewable
  > correction. It runs locally or in GitHub Actions, explains every
  > recommendation, and keeps humans in control of ownership changes.

- [ ] **A7. Add the two incident-anchored copy lines** to README/landing
  copy: *"Find the production paths nobody can safely hand off before the
  person who owns them leaves."* and *"See why a reviewer is recommended, and
  reject the recommendation when the evidence is wrong."*

- [ ] **A8. Demote platform features in copy.** Bus factor, decay, topology,
  balance, onboard, trends, graph remain documented but are framed as
  "supporting analyses for the audit," listed after the core flow. Do not lead
  with dashboards, Slack, MCP, or a company-brain narrative anywhere.

- [ ] **A9. Reframe the comparison table** (`docs/USAGE.md` "How checkowners
  compares"): the differentiation is *evidence-backed recommendation*, not
  feature count and not syntax validation — GitHub's validate-codeowners
  ecosystem already covers syntax, duplicate patterns, missing files, valid
  users, and valid teams. Rewrite the table around "explains its evidence /
  proposes a reviewable change / detects drift vs. reality" columns.

---

## Workstream B — First-run experience: `audit` as the default path (P0)

The existing command set (16 subcommands in `checkowners/cli.py`) is useful
for experts but too fragmented for first contact.

- [ ] **B1. Add `checkowners audit`** as the primary command and default
  recommended entry point. One report per path containing: declared owner,
  inferred candidates, confidence (with uncertainty label, see C4), evidence
  summary, disagreement type (drift category from `drift.py`), bus-factor
  risk (`busfactor.py`), recommended action, and whether human review is
  required. Implementation: new `checkowners/audit.py` composing
  `analyze.py` + `drift.py` + `busfactor.py` + `decay.py` over the cached
  state (`state.py`), registered in `cli.py`; `--json` supported like every
  other command; new `tests/test_audit.py` with mocked subprocess per the
  testing conventions.
  *Acceptance:* on a repo with a CODEOWNERS file, `checkowners audit` is the
  only command a new user needs to reach a decision; README quick start leads
  with it.

- [ ] **B2. Encode the ideal first-run flow** end to end and document it as
  the canonical walkthrough: (1) run one command → (2) declared ownership
  beside inferred candidates → (3) the evidence behind the score → (4) risk
  and uncertainty → (5) generate a diff or PR, never an unexplained
  overwrite.

- [ ] **B3. Keep the expert surface intact but secondary.** No existing
  subcommand is removed; `audit` becomes the front door and the README/docs
  route experts to the per-domain commands from there.

---

## Workstream C — Explainability and trust (P0)

Trust is the product. A score like `0.82` implies false precision;
explainability and calibrated uncertainty matter more than another scoring
feature.

- [ ] **C1. Add `checkowners explain <path>`** producing a human-readable
  rationale for every recommendation, in this register:

  > "Suggested @alice for services/payments because she authored 38% of
  > recent non-generated lines, contributed to 61% of recent commits touching
  > this path, and reviewed 14 related changes. Confidence is reduced because
  > the signal is concentrated in one refactor and recent review activity is
  > absent."

  Implementation: extend `expertise.py` (already ranks per-path) with an
  explanation builder over the four `ConfidenceScore` factors in `models.py`;
  register in `cli.py`; `--json` output includes the structured breakdown.
  *Acceptance:* every owner surfaced by `audit`, `drift`, and the PR comment
  can be traced to an `explain` output; the same rationale text is reused in
  the diff/PR annotations (D3).

- [ ] **C2. Expose signal-by-signal weights in output**, not just config:
  `explain` and `audit --json` show each factor's raw value, weight
  (`scoring.*_weight` from `config.py`), and weighted contribution.

- [ ] **C3. Surface time window, exclusions, and identity merges** in every
  report: `lookback_days`, active `paths.exclude` patterns that suppressed
  files, and which emails were merged into which `@handle`
  (`cli._merge_identities` / `state.py` handles cache). The evidence line in
  audit output states them (e.g. "Evidence: 180-day git history, PR reviews
  enabled").

- [ ] **C4. Add confidence *penalties* and uncertainty labels.** Detect and
  penalize (with the penalty named in `explain`): signal concentrated in a
  single large refactor/commit, absent recent review activity, sparse history
  on the path, and history shorter than the lookback window. Map numeric
  confidence to labels — `high`, `medium`, `low`, `contested` (contested =
  multiple candidates within a small delta) — and prefer the label over the
  raw number in human-facing output; the number stays in `--json`.
  *Acceptance:* human-facing tables show `medium`, not `0.82`; JSON keeps
  both; clamping to `[0.0, 1.0]` preserved.

- [ ] **C5. Document known false-positive classes** prominently (README
  limitations + `docs/FAQ.md` + audit output footnote): prolific contributors
  who do not own architecture; generated or vendored code; managers who
  review without committing; pair programming; contractors; large refactors;
  squash merges; bots; sparse history; collective ownership; experts who work
  mostly in another repository.
  *Acceptance:* limitations appear near the top of the README (J2), phrased
  as "git history is evidence, not ground truth."

---

## Workstream D — Safe, reviewable CODEOWNERS changes (P0)

The highest-value behavior is not "generate a file"; it is "propose a safe
ownership change a team can review."

- [ ] **D1. Unified diff as the default change output.** `audit` (and a new
  `generate --diff` / `drift --diff`) emit a unified diff against the current
  CODEOWNERS instead of writing the file. Writing remains explicit
  (`generate`/`sync`), never a side effect of auditing.

- [ ] **D2. Optional branch + pull-request creation** (opt-in flag /
  Action mode, see H3), gated on the `github` extra and a token — consistent
  with the network-boundary rule (network only in `github.py`, `notify.py`,
  `balance.py`, `topology.py`, `action.yml`).

- [ ] **D3. PR comment explaining every proposed change**, reusing the C1
  rationale per changed rule (extend the `actions/github-script` step in
  `action.yml`).

- [ ] **D4. Explicit approval before modification.** No mode may modify
  `.github/CODEOWNERS` without an explicit flag; audit-only paths are
  guaranteed read-only. `sync` (which currently generates and commits,
  `cli.py`) is re-documented as the explicit-approval path and must never run
  by default in the Action.

- [ ] **D5. Preserve comments and formatting** of an existing CODEOWNERS when
  proposing changes: move `generate.py` from whole-file rewrite to a
  patch-style edit for hand-written files (keep comments, blank lines, rule
  order; only touch changed rules). The `--force` full-rewrite path and the
  machine-generated-header rule stay for generated files.

- [ ] **D6. Never silently remove the last owner of a path.** Guard in
  `generate.py`/`audit.py`: a proposal that leaves a previously-owned path
  ownerless is emitted as a *warning + suggestion requiring human review*,
  never as part of an auto-applied diff.

- [ ] **D7. Warn when coverage decreases.** Compare covered-file counts
  (via `patterns.py` matching over `git ls-files`) before/after the proposal
  and flag any net decrease in the diff summary and PR comment.

- [ ] **D8. Protected paths.** New config `paths.protected` (security,
  production, billing, compliance globs): proposals touching protected paths
  are always marked "human review required" and excluded from any automatic
  PR mode. Wire through `config.py` → `models.py` → `audit.py`/`generate.py`.

- [ ] **D9. Allowlists and denylists for bots and generated files.** Extend
  `analysis.exclude_bots` (already in config) with explicit
  `analysis.owner_denylist` / `owner_allowlist` and honor
  `linguist-generated`-style path exclusions beyond the current
  `paths.exclude` defaults.

- [ ] **D10. Dry-run mode** (`--dry-run`) on every mutating command
  (`generate`, `sync`, future PR-creation), printing exactly what would
  change and exiting with the stable "changes pending" exit code (I3).

- [ ] **D11. Rollback metadata.** Record each applied change (timestamp, diff
  hash, prior file hash, approving flag/actor) in per-repo state
  (`state.py`; bump `SCHEMA_VERSION` from 3 per the schema rule) so an
  applied proposal can be reverted and so accept/reject tracking (K3) has a
  substrate.

- [ ] **D12. Encode the four-ownership vocabulary in `models.py`** (observed
  / recommended / declared / approved) so CLI output and JSON payloads can
  never conflate them.

---

## Workstream E — Scoring and configuration controls (P0/P1)

Current signals (recency, frequency, blame, review — `docs/USAGE.md`
"Confidence scoring") are a reasonable start; teams need controls for:

- [ ] **E1. Lookback window** — exists (`analysis.lookback_days`); document
  per-path override need or explicitly document its absence.
- [ ] **E2. Decay curve** — exists (`scoring.recency_half_life_days`); expose
  in `explain` output (C2).
- [ ] **E3. Minimum meaningful change size** — new: ignore commits below a
  configurable changed-lines threshold in `analyze.py`.
- [ ] **E4. Generated and vendored file exclusions** — defaults exist
  (`paths.exclude`); add `.gitattributes` `linguist-generated` detection.
- [ ] **E5. Bots and former employees** — bots exist (`exclude_bots`); add
  former-employee handling: `analysis.departed_owners` list and, with the
  `github` extra, org-membership verification via `github.py`; departed
  owners are excluded from recommendations and *flagged* when declared.
- [ ] **E6. Merge and squash handling** — verify/implement first-parent vs.
  all-commits behavior in `analyze.py`'s `git log` invocation and document
  it; squash-merge identity already partially covered by noreply parsing.
- [ ] **E7. Renames and moves** — verify/implement `--follow`/rename
  detection in `analyze.py` so moved files keep their history.
- [ ] **E8. Binary files** — blame coverage is meaningless for binaries;
  skip/zero the blame factor and note it in `explain`.
- [ ] **E9. Tests and docs weighting** — optional path-class weights (e.g.
  `scoring.path_weights: {tests: 0.5, docs: 0.5}`) so test-only contributors
  don't outrank implementation owners.
- [ ] **E10. Review weighting** — exists (`scoring.review_weight`, gated on
  `github.api_enabled`); document the 0.0-when-disabled behavior in `explain`.
- [ ] **E11. Ownership and evidence thresholds** — exist
  (`analysis.confidence_threshold`, `min_commits`); add a separate
  *evidence-sufficiency* threshold: below N commits total on a path, output
  "insufficient evidence" instead of a low-confidence owner.
- [ ] **E12. Individual versus team ownership** — see Workstream F.

All new keys flow `config.py` → `models.py` (Config dataclasses) → consuming
module → `docs/USAGE.md` config reference → tests.

---

## Workstream F — Team ownership modes (P1)

Individual ownership is often the wrong CODEOWNERS answer. Building on
existing team resolution (`github.resolve_teams`):

- [ ] **F1. Team-owner mode** — recommend `@org/team` when the evidence set
  collapses to a team (exists partially; make it a first-class output mode).
- [ ] **F2. Primary-plus-backup mode** — emit two owners per rule with roles
  labeled; ties into bus-factor backup suggestions (`busfactor.py`).
- [ ] **F3. Required-reviewer plus advisory-reviewer mode** — distinguish
  rules meant to block merges from advisory suggestions (advisory goes in the
  PR comment, not the CODEOWNERS line).
- [ ] **F4. Owner-candidate-only mode** — report candidates without proposing
  any CODEOWNERS change (pure-audit posture for teams not ready to edit).

---

## Workstream G — Owner-validity checks (P1)

Differentiation is evidence-backed recommendation, not syntax validation
(`validate.py` stays syntax-only). Add a separate *liveness* check layer
(requires `github` extra) covering:

- [ ] **G1.** Deleted users declared in CODEOWNERS.
- [ ] **G2.** Renamed users (declared handle no longer resolves; suggest the
  new handle when discoverable).
- [ ] **G3.** Invisible teams (team not visible to the token/org).
- [ ] **G4.** Teams without repository access (CODEOWNERS silently fails for
  them — flag as a `critical` audit finding).
- [ ] **G5.** Inactive teams (no member activity in the lookback window).
- [ ] **G6.** External owners (outside the configured `github.org`).
- [ ] **G7.** Stale email mappings — add cache invalidation/TTL for
  `~/.checkowners/handles.json` (`state.py`), including remembered misses.

Surface all of these as audit findings with severities feeding
`notify.compute_severity`.

---

## Workstream H — GitHub Action conversion (P0/P1)

Scored 4/10 — the weakest surface. The Marketplace page must explain *what
happens to a repository* instead of duplicating the README.

- [ ] **H1. Rewrite the Action documentation** (README Action section +
  Marketplace listing sourced from `action.yml`) to state explicitly:
  inputs and defaults (already in `action.yml`), outputs
  (`checkowners_drift`, `bus_factor_summary`, `decay_summary`), token
  requirements, private-repository and Enterprise behavior, expected runtime,
  cache behavior (`~/.checkowners/` is ephemeral per runner), failure
  conditions (shallow-clone guard, `fail_on_drift`), PR-comment behavior
  (single managed comment, updated in place), write-operation controls, and
  API-failure behavior (`notify` never raises; document the same for the
  Action steps).

- [ ] **H2. Publish a complete copy-paste least-privilege workflow** with an
  immutable action version (pin by commit SHA, not tag), explicit
  least-privilege `permissions:` block, `fetch-depth: 0`, and comments
  explaining every permission. Replace the current example in
  `docs/USAGE.md` (which grants `pull-requests: write` unconditionally).

- [ ] **H3. Separate audit mode from PR-creation mode.** New `mode`-style
  input (or a distinct documented workflow) so audit-only runs request
  **zero write permissions**: `comment_on_pr` defaults must not force
  `pull-requests: write` on users who only want the check. Document the two
  modes side by side: audit (contents: read only) vs. propose
  (pull-requests: write, explicit opt-in).

- [ ] **H4. Scheduled execution.** Add an `on: schedule` example workflow —
  ownership decays even when CODEOWNERS does not change, so PR-triggered runs
  alone miss decay/bus-factor drift. Pairs with D2/D3 for a weekly
  "ownership audit" issue or PR.

- [ ] **H5. Path filters for large monorepos.** Action input (and CLI flag)
  restricting analysis to given path globs, mapped through
  `paths.exclude`-style matching in `analyze.py`.

- [ ] **H6. SARIF output.** `checkowners audit --format sarif` (or
  `--sarif`) so findings appear in GitHub code scanning; add an Action step
  uploading via `github/codeql-action/upload-sarif`. Findings map: drift
  `changed`/`stale`/`missing` and validity checks (G) → SARIF rules with the
  C1 rationale as the message.

---

## Workstream I — CLI, installation, and operability gaps (P0)

- [ ] **I1. Document the runtime contract** in README/USAGE: supported Python
  versions (3.11+ per `pyproject.toml`), supported operating systems, minimum
  Git version, shallow-clone behavior (CLI should fail as clearly as the
  Action's guard in `action.yml` — add the same check to `analyze.py`),
  detached-HEAD/CI checkouts, authentication (`GITHUB_TOKEN` env var only,
  never in config — `config.py` already raises on `github.token`), cache
  location and invalidation (`~/.checkowners/`, `CHECKOWNERS_STATE_DIR`),
  behavior on non-current commits, and the optional dependency matrix
  (`[graph]`, `[github]`, `[all]`).

- [ ] **I2. Add `checkowners doctor`.** Preflight command checking:
  repository state (inside a git work tree), history depth (shallow-clone
  detection), CODEOWNERS location (the three auto-detect paths in
  `config.py`), optional dependencies present vs. needed (networkx,
  PyGithub), authentication (token present/valid when `api_enabled`), cache
  readability and schema version (`state.py` v3), and filesystem
  permissions. Human-readable and `--json`; exits non-zero (per I3) when a
  blocking problem is found. New module `checkowners/doctor.py` +
  `tests/test_doctor.py`.

- [ ] **I3. Stable, documented exit codes for CI policy.** Today every
  failure is exit 1 (`typer.Exit(code=1)` throughout `cli.py`), so CI cannot
  distinguish "drift found" from "misconfigured." Define and document a
  contract, e.g.: `0` success/no findings, `1` findings (drift detected /
  audit failures), `2` usage or configuration error, `3` environment error
  (not a git repo, shallow clone, missing dependency), `4` network/API
  error. Apply across `cli.py`, cover in `tests/test_cli.py`, document in
  USAGE, and use in the Action's `fail_on_drift` step.

---

## Workstream J — Documentation priorities (P0/P1)

- [ ] **J1. Task-based guides** (new `docs/guides/` or USAGE sections), one
  per job: auditing an existing CODEOWNERS file; monorepos; GitHub Actions
  setup; private repositories; GitHub teams; generated-code exclusions;
  interpreting confidence; handling false positives; single-owner risk;
  scheduled audits; reviewing a proposed change; migration from other
  ownership tools.

- [ ] **J2. Limitations near the top.** A "Limitations" section high in the
  README (before the command table) listing the C5 false-positive classes and
  the evidence-not-ground-truth framing. Honest limitations increase trust.

- [ ] **J3. Marketplace copy** — see H1; keep it distinct from the README
  (explain repository effects, not features).

---

## Workstream K — Evaluation, metrics, and feedback loop (P0)

Do not claim the model improves until accepted, rejected, and corrected
recommendations are measured.

- [ ] **K1. Labeled evaluation set.** Create evaluation repositories (or
  pinned public-repo snapshots) with maintainer-labeled ground truth for
  path→owner. Store under `eval/` (or a sibling repo) with a runner script;
  keep it out of the wheel.

- [ ] **K2. Report metrics per release:** precision of high-confidence
  recommendations, false positives, false negatives, and calibration by
  repository type (monorepo vs. small, squash vs. merge). Publish in
  `docs/` and cite in the README instead of uncalibrated claims.

- [ ] **K3. Accepted / rejected / corrected feedback capture.** New
  `checkowners feedback <path> --accept|--reject|--correct <owner>` recording
  the verdict into per-repo state (`state.py`, schema bump shared with D11);
  `audit` surfaces prior verdicts so a rejected recommendation is not
  re-proposed identically. Aggregate counts feed K2.

---

## Workstream L — The case study (P0)

One complete case study helps the README, HN, DEV, and outreach more than
another ten commands.

- [ ] **L1. Select a public repository** with an existing CODEOWNERS file
  and run the full audit flow against it.
- [ ] **L2. Publish `docs/case-study.md`** containing, explicitly:
  repository size and history window; the existing CODEOWNERS excerpt;
  inferred output; score evidence; the disagreement; the maintainer verdict;
  the generated diff; whether the recommendation was accepted or rejected;
  runtime and resource usage; and discovered false positives.
- [ ] **L3. Feed the case study back into the README** as the A5 hero example
  and before/after asset (screenshot or recording).

---

## Workstream M — Validation plan (P0, non-code)

- [ ] **M1. Run 10–15 sessions with platform or DevEx leaders.** Protocol per
  person: name a real CODEOWNERS pain; run checkOwners on a repository they
  know; review recommendations *without* author explanation; mark three
  recommendations right / wrong / uncertain; compare with their existing
  process; decide whether they would run it in CI; state the security,
  permission, or policy blocker.
- [ ] **M2. Measure:** time to first useful result; recommendation
  comprehension; precision of high-confidence findings; false-positive rate;
  approved changes; repeat runs; scheduled audits; whether anyone can name an
  avoided incident or saved cost.
- [ ] **M3. Question discipline:** never ask "Would you use this?" — ask them
  to run it and show the last ownership disagreement they had.
- [ ] **M4. Record findings** (anonymized) and gate P1/P2 work on them.

---

## Workstream N — Explicit de-prioritization (standing policy)

- [ ] **N1. Freeze new platform surfaces** until the wedge is proven:
  no dashboards, Slack surfaces beyond the existing webhook, MCP, hosted
  multi-repository analytics, or "engineering brain" narrative in code or
  copy.
- [ ] **N2. Keep existing P2 features** (topology visualization, onboarding
  paths, review balancing, broader graphs) maintained but not promoted;
  they re-enter the roadmap only after users ask, and only once teams
  repeatedly run audits, accept recommendations, and schedule them.
- [ ] **N3. Success gate for expansion:** teams repeatedly run it, accept
  recommendations, and schedule it — i.e., it becomes a habit an engineering
  team would notice if it disappeared. Until then, the P2 list is plausible
  features, not product-market proof.

---

## Roadmap

### P0 — do now (review's numbered list → steps above)

| # | Review item | Steps |
|---|---|---|
| 1 | Narrow README positioning | A1–A4, A6–A9 |
| 2 | Real before-and-after case study | L1–L3 |
| 3 | Drift/audit as the default path | B1–B3 |
| 4 | Score explanations | C1–C4 |
| 5 | Reviewable diff with rationale | D1, D3–D7, D10, D12 |
| 6 | Generated files, bots, former employees, shallow clones | D9, E4, E5, I1 (CLI shallow-clone guard) |
| 7 | Complete least-privilege Action workflow | H1–H3 |
| 8 | Stable exit codes and `doctor` | I2, I3 |
| 9 | Labeled evaluation repositories and metrics | K1, K2 |
| 10 | Accepted/rejected/corrected feedback | K3 |

Plus A5 (hero output), C5/J2 (limitations up top), D8 (protected paths),
D11 (rollback metadata), M1–M4 (validation sessions).

### P1 — next

Scheduled audits (H4), evidence-rich PR comments (D3 expanded), optional
automatic PR creation (D2), SARIF (H6), organization and team validation
(G1–G7), ownership transfer workflows (builds on `decay.py` transfer
recommendations), primary and backup policies (F1–F4), historical
comparisons (extend `trends.py`), high-risk Slack alerts (extend
`notify.py` severity gating), monorepo path filters (H5), remaining scoring
controls (E3, E6–E9, E11), task-based guides (J1).

### P2 — only after users ask (gated by N3)

Topology visualization, onboarding paths, review balancing, broader
engineering knowledge graphs, Slack and MCP surfaces, hosted
multi-repository analytics, additional integrations.

## Success metrics (definition of done for the pivot)

- A new user reaches a useful audit result from the README in under five
  minutes, understanding what was read, what the output means, and that
  nothing changed automatically.
- ≥ the M2 measurement set collected across 10–15 validation sessions.
- Published precision/calibration numbers for high-confidence
  recommendations (K2) and a public case study (L2).
- Action installable in audit mode with `contents: read` only.
- Every recommendation in every surface (CLI, JSON, PR comment, SARIF)
  carries a human-readable rationale and an uncertainty label.
- Evidence of habit: repeat runs, accepted recommendations, and scheduled
  audits in at least one external team's CI.
