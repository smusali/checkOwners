# checkOwners: Unified State of the Project, Problem Register, Solution Register, and Suggestion Register

**Subject:** `github.com/smusali/checkowners` — PyPI package `checkowners` 0.5.0 — GitHub Marketplace composite Action `checkOwners` v0.5.0
**Canonical brand:** CheckOwners (product) / `checkowners` (CLI, package, repository slug)
**Document scope:** every substantive finding, criticism, caveat, metric, code snippet, prioritization, roadmap item, risk, and strategic recommendation about this project, unified into a single register, cross-checked against the current contents of the repository.

---

## 1. Canonical Identity and Distribution Surfaces

| Surface | Value | Status |
|---|---|---|
| Repository | `github.com/smusali/checkowners` | Canonical home. 76 commits, MIT licensed |
| Marketplace Action | `checkOwners`, v0.5.0 | Categories: continuous-integration, code-review. Not GitHub-certified (normal for third-party Actions; not itself a security defect) |
| PyPI package | `checkowners` 0.5.0 | Released 2026-07-04. sdist 95.4 kB, wheel 54.4 kB. `Development Status :: 4 - Beta`. `requires-python = ">=3.11"`, classifiers 3.11 / 3.12 / 3.13 |
| `pyproject.toml` `[project.urls]` Homepage / Issues / Source | Point at a previous organization slug | **Stale — must be rewritten to `smusali/checkowners`** |
| README CI badge | Points at a previous organization slug | **Stale** |
| `docs/USAGE.md` Actions example | `uses: <previous-org>/checkOwners@v0.5.0` | **Broken front door for every new user** |
| `docs/CHANGELOG.md` compare links | All resolve against a previous organization slug | **Stale** |
| `docs/CONTRIBUTING.md` clone URL and issues URL | Previous organization slug | **Stale** |
| `SECURITY.md` private advisory link | Previous organization slug | **Stale** |
| Sigstore / Trusted Publishing provenance | Publisher `publish.yml` under the previous organization | Correct at signing time, confusing now |
| Community counters | 0 stars, 0 forks, 0 watchers, 0 open issues, 0 open PRs | **Zero external signal** |
| Contributors | 2 (`@smusali`, `@claude`) | Project bus factor 1 |
| Runtime dependencies | `typer>=0.9.0`, `rich>=13.0.0`, `pyyaml>=6.0` | No upper bounds, no lockfile |
| Optional extras | `graph` (`networkx>=3.0`), `github` (`pygithub>=2.0.0`), `all` | `all` duplicates the lists instead of composing `checkowners[graph,github]` |

The naming currently fragments across a lowercase package slug, a camel-case repository name, a previous organization, and the current organization. A first-time visitor can encounter four identities for one product on the first screen. That reads as abandoned or forked — the worst possible first impression for a tool that asks for `pull-requests: write` inside someone's CI.

---

## 2. Current State — Release History and Cadence

| Version | CHANGELOG date | PyPI date | Note |
|---|---|---|---|
| 0.1.0 | 2026-05-26 | 2026-05-26 | consistent |
| 0.1.1 | 2026-05-26 | 2026-05-26 | consistent |
| 0.2.0 | 2026-05-26 | 2026-05-26 | consistent |
| 0.3.0 | 2026-05-28 | 2026-06-06 | **9 days apart** |
| 0.4.0 | 2026-06-14 | 2026-06-14 | consistent |
| 0.5.0 | 2026-07-03 | 2026-07-04 | 1 day |

Six releases in roughly six weeks, then approximately eight weeks (56 days) of silence with an empty `Unreleased` section. Two of six changelog entries misdate their own release. The cadence collapse plus the empty `Unreleased` heading reads as abandonment to anyone evaluating the project, regardless of intent.

---

## 3. Current State — Architecture and Module Map

```text
checkowners/
  __init__.py      # version export
  cli.py           # Typer app (1,094 lines); all subcommands + --version; progress bar;
                   # identity merging (emails -> @handles, bus factor recomputed)
  analyze.py       # git log + parallel git blame -> confidence-scored OwnershipMap;
                   # blames only min_commits-qualified paths; optional on_progress hook
  patterns.py      # CODEOWNERS matcher: gitignore-style semantics, last match wins
  generate.py      # OwnershipMap -> CODEOWNERS writer; directory consolidation;
                   # refuses to overwrite a hand-written file without --force
  drift.py         # Pattern-aware compare -> DriftResult with notes:
                   # missing / stale / changed
  notify.py        # Webhook POST with severity gating; never raises on network errors
  validate.py      # Syntax-only CODEOWNERS validator
  config.py        # PyYAML loader, CODEOWNERS path auto-detection
  state.py         # Per-repo state (schema v3), handles.json cache, graph cache
  expertise.py     # Per-path expertise ranking; shared path_matches_glob helper
  decay.py         # Expertise decay detector with transfer recommendations
  graph.py         # Knowledge graph builder (lazy networkx import; DOT-escaped)
  busfactor.py     # Bus factor calculator with backup-reviewer suggestions
  topology.py      # Team topology inference from commit co-occurrence
  balance.py       # PR review load balancer (bounded PR scan, fallback_reason)
  onboard.py       # Onboarding path generator
  trends.py        # Historical ownership-confidence trends (per-period snapshots)
  github.py        # GitHub API: email->@handle, team resolution, review coverage
  models.py        # Dataclasses for every domain object and config section
tests/
  conftest.py      # autouse fixture: CHECKOWNERS_STATE_DIR isolated per test
  test_<module>.py # one per module; subprocess fully mocked
action.yml         # Composite GitHub Action (237 lines)
```

Approximately 4,260 lines of package code and 4,400 lines of tests. The decomposition is genuinely good: this is not a one-file weekend script that accidentally became a package.

**State layout.**
- `~/.checkowners/state/<repo-hash>.json` — schema v3, absolute repository path embedded and verified on load.
- `~/.checkowners/handles.json` — email-to-handle cache including remembered misses.
- `~/.checkowners/graph/<repo-hash>.json` — serialized graph cache.

**Config.** `.github/checkowners.yml` with sections `analysis`, `scoring`, `decay`, `bus_factor`, `paths`, `output`, `drift`, `notifications`, `github`.

**Environment variables.** `GITHUB_TOKEN`, `CHECKOWNERS_STATE_DIR`, `CHECKOWNERS_CONFIG`, `CHECKOWNERS_DRIFT_MODE`, plus runner-provided `GITHUB_REPOSITORY` and `GITHUB_OUTPUT`.

---

## 4. Current State — Command Surface

Sixteen subcommands (counted as fifteen domain commands plus `github-action`):

`analyze`, `generate`, `print`, `validate`, `drift`, `notify`, `sync`, `expertise <path>`, `decay`, `graph`, `bus-factor`, `topology`, `balance`, `onboard <path>`, `trends`, `github-action`.

All support `--json` except `graph`, which supports `--export dot`.

Sixteen commands, zero stars, zero issues, zero forks, zero external validation. Every command is permanent maintenance surface: a JSON contract, a test file, a documentation section, and a backward-compatibility obligation. `topology`, `balance`, `onboard`, `trends`, and `graph` were all built before a single external user confirmed that `analyze` produces owners a real team agrees with.

---

## 5. Current State — The Inference Model as Implemented

**Confidence** is a weighted sum clamped to `[0.0, 1.0]`:

```text
confidence = 0.35 × recency
           + 0.25 × frequency
           + 0.25 × blame
           + 0.15 × review
```

- `recency = 0.5 ** (days_since_last_commit / recency_half_life_days)`, default half-life 90 days, evaluated against `datetime.now(UTC)`.
- `frequency = commits / max_commits_for_path`.
- `blame` = the contributor's fraction of current lines from `git blame --line-porcelain`.
- `review` = per-path review coverage, populated only when `github.api_enabled` is true, a token is present, and `GITHUB_REPOSITORY` is set; otherwise `0.0`. The review scan is bounded to the 200 most recently updated closed pull requests.

**Configuration defaults as implemented.**

```yaml
analysis:
  lookback_days: 365
  min_commits: 3
  top_n_owners: 3
  confidence_threshold: 0.3
  exclude_bots: true
scoring:
  recency_half_life_days: 90
  recency_weight: 0.35
  frequency_weight: 0.25
  blame_weight: 0.25
  review_weight: 0.15
decay:
  threshold_days: 180
  alert_on_decay: true
bus_factor:
  critical_threshold: 1
  warn_threshold: 2
drift:
  mode: commit
  min_confidence_delta: 0.2
notifications:
  severity_threshold: medium
github:
  api_enabled: false
  resolve_handles: true
  resolve_teams: true
output:
  consolidate: true
  header: "# Generated by checkOwners. Do not edit manually."
```

**Default exclusions.** `*.lock`, `package-lock.json`, `pnpm-lock.yaml`, `dist/**`, `vendor/**`, `node_modules/**`, `*.generated.*`, `*.min.js`, `*.min.css`, `*.map`, plus the three CODEOWNERS locations.

**Severity tiers.** `critical` when bus factor is at or below `bus_factor.critical_threshold` or decay is true; `high` at confidence delta ≥ 0.7; `medium` at ≥ 0.3; `low` otherwise.

**Bus factor as implemented.** `_compute_bus_factor(top, threshold)` counts entries in `top`, which is already truncated to `config.analysis.top_n_owners`. The repository-level figure is the arithmetic mean of those per-path counts.

**Pipeline.** `config.py` loads `.github/checkowners.yml` and auto-detects the CODEOWNERS location → `analyze.py` runs `git log --format=… --name-only --since=<N> days ago`, filters excluded paths, filters nonexistent paths, filters bot authors, filters paths where no author reaches `min_commits`, runs `git blame --line-porcelain` on a thread pool sized to CPU count, and computes the four-factor confidence → `models.OwnershipMap` → `cli._resolve_github_owners` maps emails to handles and merges identities → `state.py` persists per repository → downstream commands read state and emit per-domain reports. `trends` is independent of state and runs its own single `git log` pass.

---

## 6. Unified Ratings and Scorecards

Different assessment passes used different aggregation logic and different category labels. All are preserved; they are directionally consistent.

### 6.1 Strategic scorecard

| Dimension | Rating | Unified interpretation |
|---|---:|---|
| Problem | 8/10 | Strong, real, universal engineering problem |
| Technical concept / core idea | 8/10 | Credible, inspectable, technically sound |
| Focus | 5/10 | Too many product surfaces competing for the core job |
| Validation | 3/10 | Public evidence of correctness and business impact is weak |
| OSS traction evidence | 3/10 | Public adoption evidence is effectively absent |
| Developer experience | 6/10 | Promising, but trust, explainability, and the first-five-minutes workflow need polish |
| Enterprise readiness | 3/10 | Controls, proof, privacy, reliability, and workflow maturity still needed |
| Long-term potential | 9/10 | Large if the reviewer-routing and reconciliation wedge becomes trusted |

### 6.2 Engineering scorecard

| Dimension | Rating | Main gap |
|---|---:|---|
| Problem selection | 9.5/10 | none |
| Product idea | 9/10 | focus |
| Differentiation | 8.5/10 | proof |
| Engineering hygiene | 8/10 | enforcement of its own standards |
| CODEOWNERS semantics | 8/10 | no differential/fuzz corpus |
| Ownership inference | 7/10 | qualification, history fidelity, calibration |
| Explainability | 6.5/10 | no `explain` command |
| Performance architecture | 7.5/10 | no incrementality, subprocess fan-out |
| GitHub integration | 7/10 | token never wired, teams not reconciled |
| Bus-factor methodology | 3.5/10 | metric needs redesign |
| Team-topology methodology | 5/10 | connected components is too crude |
| Review-balancing methodology | 5/10 | authorship proxy conflated with review load |
| Historical/trend methodology | 5.5/10 | different metric presented as comparable |
| Validation/auditing | 5/10 | validator breadth |
| Supply-chain/reproducibility | 6/10 | Action install path unpinned |
| Packaging/distribution | 5/10 | single channel |
| Documentation | 7.5/10 | story and methodology |
| Scientific calibration | 4/10 | no benchmark, no ground truth |
| Community/adoption | 1/10 | zero visible traction |
| **Current overall** | **~6.7/10** | |
| **Realistic category-leading ceiling** | **9.5+/10** | |

### 6.3 Adversarial peer-review scorecard

| Dimension | Grade | One-line justification |
|---|---|---|
| Problem selection | A | CODEOWNERS rot is real, universal, and silently dangerous |
| Core architecture | A- | Clean module boundaries, functional style, correct dependency gating |
| Code discipline | A- | `mypy --strict`, ruff, conventional commits, documented invariants |
| Conceptual differentiation | A- | Confidence-as-spectrum plus second-order risk is a genuinely novel framing |
| CODEOWNERS semantics | B+ | Real pattern engine exists; zero differential validation against a reference |
| Inference methodology | C- | Unvalidated weights, uncalibrated scores, blame used naively |
| "Bus factor" correctness | D | Redefines an established metric into something bounded by `top_n_owners` |
| Testing strategy | D+ | 100% mocked git; the changelog proves the worst bugs escaped it |
| GitHub Action quality | C- | Token never wired, fork PRs break, unpinned installs, two divergent CI paths |
| Supply chain posture | C | Trusted Publishing and attestations are excellent; runtime install is unpinned |
| Release engineering | D | Identity split-brain across every distribution surface |
| Documentation depth | A- | USAGE and FAQ are better than 95% of tools this size |
| Documentation accuracy | D | Multiple locations point at the previous org; a token claim is wrong |
| Positioning honesty | C- | "First" claim is overreaching; comparison table is a strawman |
| Privacy posture | D | Per-person expertise and workload profiles with no anonymization path |
| Performance architecture | C- | Threaded subprocess fan-out; no incrementality; cold cache in CI |
| Distribution | F | Zero adoption, no demo, no visuals, no proof artifact, no launch |
| Community readiness | D | No Discussions, no templates, no roadmap, no good-first-issues, no funding |
| Project bus factor | F | A bus-factor tool with a bus factor of one |
| **Overall** | **Strong B- artifact, F- project** | The gap between those two grades is the entire opportunity |
| **Alternative aggregation** | **C+** | A B+ engine wrapped in a D- product, shipped through an F- go-to-market |

### 6.4 Area-by-area gap table

| Area | Today | Category-leading target | Main gap |
|---|---:|---:|---|
| Problem | 9.5 | 10 | none |
| Positioning | 7 | 10 | too many features in the headline |
| Ownership inference | 7 | 9.5 | qualification, history, calibration |
| Confidence model | 6 | 10 | availability normalization plus calibration |
| CODEOWNERS matching | 8 | 10 | formal compatibility and fuzz corpus |
| Drift | 8 | 10 | teams, severity, explanations |
| Generator | 8 | 9.5 | hybrid manual/generated ownership |
| Validation | 5 | 10 | policy and audit breadth |
| Bus factor | 3.5 | 10 | metric redesign |
| Decay | 6.5 | 9 | survival and context awareness |
| Reviewer balancing | 5 | 9 | true review workload |
| Topology | 5 | 9 | real community detection |
| Knowledge graph | 6 | 10 | richer ontology and analytics |
| Onboarding | 5.5 | 9 | architecture/dependency model |
| Trends | 5.5 | 9 | comparable, versioned methodology |
| Explainability | 6.5 | 10 | `explain` command |
| Performance | 7.5 | 10 | incremental and blob-level caching |
| GitHub Action | 7 | 10 | reproducibility plus native checks |
| Security | 6.5 | 10 | Action dependency chain, OSS controls |
| Packaging | 5 | 9.5 | binaries, container, pre-commit, Homebrew |
| Docs | 7.5 | 10 | clearer story plus methodology |
| Scientific rigor | 4 | 10 | benchmark and calibration |
| OSS community | 1 | 9 | virtually no visible traction |
| Product potential | **9.5** | **10** | execution and focus |

These are qualitative judgments, not measured benchmarks. Turning them into measured benchmarks is itself one of the highest-value recommendations in this document.

---

## 7. What the Project Gets Right (Do Not Regress Any of This)

1. **It attacks the real weakness of CODEOWNERS.** GitHub's mechanism is purely declarative. It can request reviewers and enforce code-owner approval, it defines matching semantics, it requires owners to have repository access, it applies the final matching pattern, and it skips invalid lines. What it cannot determine is whether the declared owner still represents the people who understand the code. The compelling question is not "what does CODEOWNERS say?" but **"does CODEOWNERS still correspond to reality?"**

2. **The pattern engine exists at all.** `patterns.py` implements gitignore-style CODEOWNERS semantics: segment-scoped `*`, segment-crossing `**`, root anchoring on leading or interior `/`, trailing-slash directory matching, `dir/*` direct-children-only, and last-match-wins. Most competitors string-compare. The 0.5.0 changelog admits the previous literal comparison produced near-100% false positives on real repositories. Fixing that is the single most important thing the project has done.

3. **Confidence-scored ownership is directionally correct.** Ownership is not `person_with_most_commits`. Someone may have created a component years ago, have many commits with no recent involvement, own most current lines, repeatedly review without authoring, or be a recent maintainer inheriting old code. A composite model is clearly better than a raw commit count.

4. **Declared-versus-observed drift is the strongest feature and the best available wedge.** It parses the real file, evaluates rules against tracked files, and compares declared with inferred ownership. The analogy is exact and powerful: Terraform reconciles desired infrastructure against actual infrastructure; CheckOwners reconciles declared code ownership against observed engineering ownership.

5. **Correct dependency gating.** Core inference is pure git via `subprocess`. `networkx` and `pygithub` are optional extras, lazy-imported, degrading gracefully with a hint. GitPython was correctly dropped. Unusual discipline.

6. **The token refusal.** `github.token` is rejected in `.github/checkowners.yml` at load time with a clear error, because that file gets committed. The `${ENV_VAR}` indirection for `notifications.webhook_url` is the same instinct applied correctly a second time. This is a security decision made by someone who has watched a token leak.

7. **Per-repo state isolation with schema versioning.** The 0.5.0 fix for a global state file leaking one repository's data into another, plus embedding and verifying the absolute repository path on load, plus the codified invariant that `SCHEMA_VERSION` must be bumped on change, is exactly right.

8. **Refusing to clobber hand-written CODEOWNERS.** `generate` and `sync` require `--force` when the machine-generated header is absent. Ownership files are governance artifacts, not disposable build products. The default is `do not destroy human intent`, not `machine inference is automatically authoritative`.

9. **Excluding the CODEOWNERS file itself from inference.** A `sync` commit would otherwise make whoever ran the tool the inferred owner of the ownership file, perturbing every subsequent run. That is a feedback-loop bug most projects would ship.

10. **Consolidating inferred rules into directory rules.** Emitting `/src/api/ @alice` instead of four per-file lines keeps the file understandable, keeps it small, and makes future files inherit a rule automatically.

11. **Identity resolution treated as a real problem.** The three-stage cascade — noreply parsed locally with zero network, then a disk cache with remembered misses, then the API — is well-engineered cost minimization over a genuinely messy domain.

12. **Identity merging so bus factor counts people, not addresses.** Conceptually correct and rarely done.

13. **Drift `notes` instead of false positives.** Emitting an explanation when comparison would be meaningless — raw emails versus handles, team-owned rules versus individual inference — rather than fabricating drift is mature product judgment.

14. **Supply-chain provenance on the package side.** PyPI Trusted Publishing, Sigstore transparency entries, in-toto attestations, workflow permalinks. Better than most projects with a hundred times the stars, and currently unmentioned in the README.

15. **`mypy --strict` plus a non-trivial ruff rule set plus `Typing :: Typed`.** The engineering bar is set far above what the star count would suggest.

16. **The composite Action's shallow-clone guard.** Failing fast with an actionable error naming `fetch-depth: 0`, instead of silently producing a garbage ownership map, saves users hours.

17. **Dogfooding against a real production monorepo.** 24k commits, 12k active files, 80+ minutes reduced to under three. The bracket-segment dynamic-route bug — GitHub ignores CODEOWNERS lines containing `[...]` ranges, so generating them silently un-owns those paths — is a finding you only get from real usage, and it was found and fixed.

18. **An honest changelog.** It states plainly that "previously the comparison was literal string equality" and that validation "previously demanded every pattern start with `/` or `*`, failing perfectly valid real-world files." Publicly documenting your own severe bugs builds more trust than a clean-looking history.

19. **Local-first, pure-git, no-LLM operation.** Deterministic, auditable, offline-capable, air-gap-compatible, no token spend, no vendor lock-in, no hallucinated owners, no source-code upload. In the current market this is a differentiator, not a limitation, and it should be leaned on much harder.

20. **State caching at all.** A tool that recomputes blame and history across large repositories on every invocation becomes irritating immediately. The foundation for genuine incrementality already exists.

---

## 8. The Central Strategic Problem: Too Many Products at Once

The current surface spans CODEOWNERS generation, ownership drift, bus factor, expertise decay, inferred topology, ownership balance, onboarding, graphs, trends, notifications, webhooks, GitHub Actions, reviewer recommendations, identity resolution, team collapsing, PR comments, and JSON output.

A buyer or developer cannot immediately answer the only question that matters at install time:

> **"What do I install this for on Monday?"**

That is the clearest sign that the product is too broad. Two formulations of the dominant job are defensible, and they are complementary rather than competing:

> **Keep pull-request review ownership aligned with current repository evidence, and flag changes that lack a reliable reviewer.**

> **Continuously reconcile who is supposed to own code with who actually knows the code.**

Reduced to the one product decision the tool must become excellent at:

> **Who should review this change, and how much should we trust that recommendation?**

Everything else — bus factor, decay, reviewer routing, onboarding, topology, graphs, trends — should derive from an exceptionally trustworthy ownership model rather than compete with it for attention. The winning hierarchy is:

```text
Git history + review history + organizational identity
                    ↓
          Ownership Evidence Engine
                    ↓
       Confidence + Evidence Quality
                    ↓
          Observed Ownership Model
                    ↓
       ┌────────────┼─────────────┐
       ↓            ↓             ↓
  CODEOWNERS      Risk      Engineering Intelligence
 reconciliation  analysis      / recommendations
```

**The central strategic error to date:** feature velocity massively outran validation, distribution, and evidence. `topology`, `balance`, `onboard`, `trends`, and the knowledge graph shipped before a single external user validated that `analyze` produces owners anyone agrees with. The correct order is: get `analyze` + `generate` + `drift` right, get twenty users, then let their pain dictate commands four through sixteen. Every hour spent maintaining `topology` today is an hour not spent on the thing that would actually produce adoption.

**The worst possible next move** is to add ten more analytics commands. **The best possible next move** is to make the ownership model brutally correct, brutally explainable, brutally reproducible, and publicly benchmarked.

---

## 9. Conceptual Problems — Where the Model Overclaims

### 9.1 Commit activity is not ownership

Repository activity is evidence. It is not organizational truth. Every one of these breaks the equivalence:

- A frequent committer may simply be implementing assigned work rather than making architectural decisions.
- High blame coverage may reflect inherited code.
- A technical lead may understand the system deeply while rarely touching implementation files.
- A reviewer may have deep expertise despite committing infrequently.
- An on-call owner may be almost invisible in git.
- A manager may own the outcome without touching files.
- A contributor may have changed teams while their historical activity remains dominant.
- A contractor may look highly active without being a long-term responsible owner.
- A temporary migration creates a misleading burst of ownership evidence.
- Pair and mob programming distort attribution.
- Security, compliance, operational, and architecture responsibilities are frequently invisible in source history.

**Solution.** Model these as separate concepts and never collapse them: authorship; maintenance activity; review expertise; design authority; architectural authority; operational responsibility; on-call responsibility; current team membership; reviewer availability; security responsibility; compliance responsibility; organizational accountability. Until stronger ground truth exists, output must not be framed as "organizational ownership." The safe description is **repository evidence of contribution and review affinity**. Internally, model distinct entities:

```text
DeclaredOwner       # who the organization says is responsible
ObservedExpert      # who evidence suggests understands the implementation
ReviewCandidate     # who should review this particular change
TeamMember          # who belongs to the declared team
HistoricalExpert    # who understood it before
CriticalMaintainer  # whose loss creates continuity risk
```

The governing design principle, which resolves most downstream product arguments:

> **Observed expertise is evidence, not authority.**

### 9.2 The confidence score is transparent but unproven

`0.92` currently means "the formula produced 0.92." It does not mean "there is a 92% probability this person is the correct reviewer." There is no ground truth, no back-test, no precision-at-k, no Brier score, and no ablation showing that removing a signal degrades results. For a tool whose thesis is "ownership is a confidence-scored spectrum," the confidence number *is* the product, and it is currently an informed heuristic.

**Solution.** Either statistically calibrate it, or rename it. Until calibration exists, use `ownership_score`, `evidence score`, `ranking signal`, or `review-affinity score` — never probability language. Once calibration exists, the product can answer: "among recommendations scored around `x`, how often are they accepted as correct?"

### 9.3 Git history has major blind spots

Squash merges, rebases, cherry-picks, refactors, renames, copied code, generated files, AI-generated code, service accounts, bots, departed contributors, contractors, pair programming, migrations, incidents, on-call work, architecture decisions, support escalations, documentation, cross-repository expertise, temporary ownership, and code inherited from another team can all make git-derived ownership misleading.

**Solution.** Every report shows what evidence was available, what evidence was missing, the quality of that evidence, known limitations, uncertainty, and whether optional non-git signals were reachable. Missing evidence is never hidden behind a single authoritative score, and **missing evidence is not negative evidence**.

### 9.4 Automatic generation can harm trust

If inferred ownership silently becomes mandatory responsibility: the busiest engineer becomes overloaded; a former owner keeps receiving reviews; a departed person remains routed work; temporary migration work becomes permanent reviewer duty; existing silos are reinforced; a team becomes "owner" of code it is actively retiring; someone is assigned responsibility merely for touching code recently.

**Solution.** The default is a reviewable proposal or pull request, never automatic policy. Preserve a permanent, visible distinction between **evidence**, **recommendation**, and **accepted ownership policy**.

### 9.5 "Expertise decay" overclaims

A person who has not touched stable code recently may still understand it perfectly. Someone who changed a file yesterday may still be unsafe for a high-risk architectural change. Activity recency does not measure knowledge.

**Solution.** Prefer **ownership freshness**, **evidence freshness**, or **continuity risk**. Distinguish *inactive* expertise, *superseded* expertise, *stable* expertise, and *departed* expertise. Decay should consider subsequent **change survival**, not only elapsed days.

### 9.6 Bus factor is only one signal

Three weak contributors may be riskier than one excellent maintainer with strong documentation and a tested handoff.

**Solution.** A continuity-risk view needs confidence distribution, review coverage, documentation, incident involvement, handoff evidence, review latency, current employment or membership, whether proposed backups have actually reviewed the area, reviewer concentration, current availability, and knowledge-transfer evidence. Language stays conservative: **candidate backup reviewer**, not **backup reviewer**.

### 9.7 Topology is a hypothesis, not an org chart

Commit co-occurrence may reflect a migration, a shared repository, a dependency relationship, a temporary project, common tooling, or a platform team touching many areas.

**Solution.** Present inferred clusters with explicit uncertainty, compare them against declared teams probabilistically, and frame the output as **exploratory repository topology**, never as an org chart. Never silently label a co-commit cluster an organizational team.

### 9.8 Identity resolution needs stronger safeguards

Email-to-handle resolution and identity merging infer sensitive relationships and can be wrong.

**Solution.** A user-maintained identity map; explicit alias handling; service-account handling; merge and unmerge audit trails; a dry-run identity report; redaction; hashing options; an API-free mode; explicit data-flow documentation; a clear privacy model for API lookups; and a **"do not merge"** override. Identity resolution must remain auditable and reversible.

### 9.9 Performance is not validation

"24,000 commits, 12,000 files, monorepo, under three minutes" demonstrates computational performance. It does not demonstrate adoption, recommendation correctness, customer value, reduced review latency, fewer incidents, improved handoffs, lower reviewer concentration, or reduced operational risk.

**Solution.** Publish product-validation metrics alongside performance: repository count, labeled reviewer examples, acceptance rates, stale rules removed, reviewer-load reduction, handoff-time reduction, risky changes identified, risks found before incidents, false positives, memory usage, CI cost, case studies, and **examples of incorrect recommendations**. Deliberately publish failure cases, not only favorable demonstrations.

### 9.10 The false-authority failure mode

The single greatest risk to the product is **false authority**: presenting repository activity as organizational truth. A few visibly wrong recommendations delivered with too much confidence destroy trust faster than any missing feature. The system must therefore remain humble, inspectable, evidence-driven, explicit about uncertainty, correction-driven, human-overridable, and conservative about inferred responsibility.

---

## 10. Technical Problem Register with Solutions

Each entry states the defect, the evidence in the current code, why it matters, and the fix.

### 10.1 🔴 The bus factor is not a bus factor

**Defect.** `analyze._build_path_ownerships` truncates owners to `config.analysis.top_n_owners` (default 3), and `_compute_bus_factor(top, threshold)` then counts the survivors of that already-truncated tuple. `busfactor.compute_bus_factor` reads `po.bus_factor` directly and averages it across paths.

**Consequence.** Under the default configuration the maximum reportable per-path bus factor is 3. A path maintained by fifteen healthy contributors reports the same value as a path maintained by exactly three. Raising `top_n_owners` to 5 "improves" the entire repository's bus factor without one line of code changing hands. The classification collapses to a near-predetermined `1 → critical`, `2 → warning`, `3 → healthy`. The repository-level average of a count that is floored at 1 and capped at 3 cannot distinguish "one person knows everything" from "everyone knows one thing."

**Why it matters.** This is not the definition of bus factor, truck factor, or lottery factor in any published treatment. The literature — including truck-factor estimation from commit history, ownership-concentration research, degree-of-knowledge models combining authorship and interaction data, and commercial knowledge-loss modeling — defines it as a **removal simulation over a knowledge distribution**: the minimal set of contributors whose departure leaves the project, or a threshold fraction of its files, without a knowledge owner. Because bus factor is one of the headline claims in the repository description, this is a positioning-level risk, not a cosmetic one. The first informed reader will say so publicly and will be right.

**Illustration of the failure.** Knowledge shares of `82 / 7 / 5 / 3 / 2 / 1` produce "six knowledgeable people" under a count, while operational reality is "lose one person, catastrophic knowledge loss." Shares of `26 / 25 / 24 / 25` are genuinely distributed. A count cannot separate these.

**Fix.**

1. **Rename the existing metric immediately**, without changing its arithmetic, to `qualified_owner_count` or `reviewer_depth`, and state its cap in every output. This is intellectually honest and costs nothing.
2. Compute any successor metric **before** `top_n_owners` truncation, over all contributors above threshold.
3. Normalize expertise into knowledge shares `pᵢ = expertiseᵢ / Σ expertise`, then compute:
   - **Top-owner share:** `max(pᵢ)`.
   - **Effective number of owners** (inverse Herfindahl): `N_eff = 1 / Σ pᵢ²`. So `100/0/0/0 → 1.00`, `50/50 → 2.00`, `25/25/25/25 → 4.00`.
   - **Knowledge-loss truck factor:** `TF(q)` = smallest `k` such that removing the `k` highest-knowledge contributors removes at least `q` of inferred knowledge. Emit `TF50`, `TF75`, `TF90`. With Alice at 82%, `TF50 = 1`, correctly identifying enormous concentration.
4. Also emit Shannon entropy, the HHI concentration index, minor-contributor share, and major-contributor count.
5. **Never average path-level risk into one arithmetic mean.** A repository whose README and docs have five owners while `payments/settlement.py` and `auth/crypto.py` have one is not "healthy on average." Weight concentration by code criticality, hotspot activity, dependency centrality, production impact, security sensitivity, recent modification frequency, and incident history where available. Report `minimum`, `p10`, `median`, `p90`, `critical-path risk`, and `knowledge-at-risk %`.
6. Add `checkowners simulate --remove @alice --remove @bob` — the removal simulation the literature actually describes, and the single most compelling executive-facing output in the category.
7. Publish `docs/METHODOLOGY.md` citing the prior art, stating exactly what CheckOwners computes, what it does not, and where and why it diverges.

Do not silently change existing output semantics: deprecate the old key, introduce `knowledge_concentration`, `effective_owners`, `truck_factor_50`, `truck_factor_75`, `truck_factor_90` alongside it.

### 10.2 🔴 Confidence changes meaning depending on GitHub API availability

**Defect.** `_score_owners` computes `0.35·recency + 0.25·frequency + 0.25·blame + 0.15·review` with no renormalization. `review` is `0.0` whenever no review provider is injected — which is the default, since `github.api_enabled` defaults to `false`.

**Consequence.** The offline maximum attainable confidence is `0.85`; the API-enabled maximum is `1.00`. A score of `0.72` therefore has no single stable meaning: it depends on what happened to be reachable during execution. The same git checkout produces different ownership classifications purely because one environment has a token and another does not. `confidence_threshold: 0.3` is a different gate in each mode, and `drift.min_confidence_delta: 0.2` compares differently-scaled quantities. This is unacceptable for a metric that drives merge-blocking CI policy.

**Aggravating inconsistency.** `trends.py` already renormalizes over available weights when historical blame and review cannot be reconstructed. The main analysis path does not. The codebase contains both behaviors.

**Fix.** For each signal `i` with score `sᵢ`, configured weight `wᵢ`, and availability `aᵢ ∈ {0,1}`:

```text
raw_confidence = Σ(wᵢ × aᵢ × sᵢ) / Σ(wᵢ × aᵢ)
```

Then compute **evidence quality** as a genuinely separate quantity:

```text
evidence_quality = Σ(wᵢ × aᵢ × reliabilityᵢ) / Σ(wᵢ)
```

and emit both, with per-signal availability:

```json
{
  "owner": "@alice",
  "confidence": 0.84,
  "evidence_quality": 0.71,
  "signals": {
    "recency":   { "score": 0.91, "available": true },
    "frequency": { "score": 0.77, "available": true },
    "blame":     { "score": 0.86, "available": true },
    "review":    { "available": false }
  }
}
```

Now `confidence` says what the evidence indicates, and `quality` says how much evidence there actually is. `@alice` at confidence 0.91 with quality 0.93 and `@bob` at confidence 0.79 with quality 0.31 are meaningfully different claims, and only this split can express that.

### 10.3 🔴 `min_commits: 3` deletes legitimate owners

**Defect.** `_filter_unqualified` drops whole paths before the blame pass, and `_build_path_ownerships` filters authors again by `contrib.commits >= config.analysis.min_commits`.

**Consequence.** A newly added production component created by one large commit, whose author owns 100% of its meaningful lines, disappears entirely. This is not an edge case — it is the normal outcome for new files, young repositories, squash-merge workflows, large feature commits, low-churn infrastructure, newly extracted services, newly moved modules, and any repository where merge strategy produces exactly one commit per pull request.

**Fix.** Commit count must be **evidence, not eligibility**.

```text
1 commit  + 95% blame + recent creation  → strong ownership, medium evidence quality
1 commit  + 3% blame                     → weak ownership
20 commits + 55% blame + recent reviews  → very strong ownership
```

Use Bayesian shrinkage or a Wilson-style lower bound rather than a hard cutoff. Minimum viable change:

```yaml
qualification:
  strategy: adaptive
  min_commits: 1
  strong_blame_override: 0.5
```

Better: remove qualification entirely and let confidence plus evidence quality determine significance.

### 10.4 🔴 Frequency normalization inflates low-evidence paths

**Defect.** `_frequency_score(commits, max_commits) = commits / max_commits`.

**Consequence.** On a path where one person made exactly three commits and nobody else touched it, that person scores `1.0` on frequency. A single-contributor path with 3 commits and one with 300 commits produce identical frequency signals. There is no absolute-evidence term, so "certainly the owner" and "the only person who happened to touch a nearly dead file" are indistinguishable.

**Fix.** Add an absolute-evidence dampener — a shrinkage term or Wilson lower bound — in the same place. Ten lines of code. Then enrich the frequency family so that twenty typo commits do not outweigh two major architectural pull requests: `commit_count`, `line_churn`, `meaningful_line_churn`, `files_touched`, `PR_count`, `merged_PR_count`, `review_count`, `review_depth`, `change_survival`, `authorship_share`. Do not simply sum them; put them behind a provider architecture so they can be calibrated independently.

### 10.5 🔴 Drift has a team-ownership blind spot

**Defect.** `drift.py` carries `_TEAM_NOTE = "rules owned by teams (@org/team) are not compared against inferred individuals."` and skips comparison rather than resolving membership.

**Consequence.** Real organizational CODEOWNERS files are overwhelmingly team-shaped — `/payments/ @company/payments`, `/security/ @company/security` — not lists of individuals. A drift engine that confidently analyzes individuals but cannot reconcile team declarations misses the single most important enterprise use case.

**Fix.** When authenticated, resolve `@company/payments` through the teams API into `{@alice, @bob, @carol}` and compare against inferred ownership:

```yaml
# inferred {@alice, @bob} ⊂ declared {@alice, @bob, @carol}
status: aligned
team_coverage: 1.0
inactive_members: ["@carol"]
```

```yaml
# inferred {@alice, @dave}, declared {@alice, @bob, @carol}
status: partial-drift
observed_outside_team: ["@dave"]
```

Offline, emit an explicit third state:

```yaml
status: unverifiable
reason: team_membership_requires_github_api
```

**Never silently equate "could not verify" with "no drift."** Introduce a first-class `analysis_completeness` result so this distinction is machine-readable.

### 10.6 🔴 Git history is not path history: no rename lineage

**Defect.** `_get_commit_history` runs `git log --format=… --name-only --since=…` with no rename or copy detection, and `_aggregate_contributions` keys purely on the literal path string.

**Consequence.** `src/api/billing.py` renamed to `services/payments/billing.py` looks like a brand-new file. Five years of expertise evaporates. This is catastrophic in monorepos, where reorganizations are routine — and directory renames reset ownership history to zero for every file beneath them.

**Fix.** Track `old_path → new_path` through git's rename and copy detection and preserve a logical file identity across history:

```json
{
  "logical_file_id": "…",
  "current_path": "services/payments/billing.py",
  "history": ["src/api/billing.py", "src/billing.py"]
}
```

Ownership then survives refactors. Note that `--follow` works on one path at a time, so bulk analysis needs an explicit lineage pass rather than per-path follow.

### 10.7 🔴 `git blame` is used without any of the standard corrections

**Defect.** `_blame_for_path` runs exactly `git blame --line-porcelain -- <path>`. No `-w`, no `-M`, no `-C`, no `--ignore-revs-file`, no `--use-mailmap`. `_get_commit_history` likewise omits `--use-mailmap`.

**Consequence.** One `black .`, `prettier --write .`, `gofmt -w`, `cargo fmt`, license-header update, or large lint autofix reassigns blame for enormous fractions of the codebase to whoever ran it. A directory reorganization destroys attribution for every moved file. Blame currently confuses *the person who understood and wrote the code* with *the person who ran a formatter across 8,000 files*. `.git-blame-ignore-revs` is the industry-standard remedy, is honored natively by GitHub, and is already present in Chromium, Rust, Kubernetes, Django, and thousands of other repositories. Ignoring it produces confidently wrong answers on exactly the mature, well-maintained repositories the project wants as reference users. This is a first-order accuracy defect, not a refinement.

**Fix.** Auto-detect and honor `.git-blame-ignore-revs`; support `blame.ignoreRevsFile`; add `-w`, `-M`, `-C`; honor `.mailmap` on both `git log` and `git blame`; support configurable generated-file detection; down-weight mass refactors. Blame should be **semantic ownership evidence**, not "last commit touching a line."

### 10.8 🔴 `.mailmap` is reimplemented instead of used

**Defect.** The project built a three-stage identity pipeline — noreply parsing, disk cache with remembered misses, GitHub user-search API — while git's canonical mechanism goes unused.

**Consequences and sub-defects.**
- `.mailmap` is free, offline, token-free, rate-limit-free, and already maintained in a large fraction of serious repositories.
- The user-search API sits in a separate, much stricter rate-limit bucket, and matching a commit email to an account by search is inherently fuzzy and privacy-sensitive.
- The authoritative mapping exists and is unused: fetching a single commit by an unresolved author returns `author.login` resolved by GitHub itself. One request per *unique unresolved email* is cheap and exact, and needs no search API.
- `handles.json` is global across all repositories while state is per-repository. An email resolved while analyzing a private client monorepo persists into the cache used for an unrelated public repository. That is a small but real cross-tenant leak in a local cache.
- Negative results are cached with no TTL. A contributor who creates an account after the first run is permanently unresolved.

**Fix.** Start from `.mailmap` (`identity.mailmap: true` by default). Fall back to commit-based `author.login` lookup before search. Namespace `handles.json` per repository or per organization. Add a TTL to negative entries. Build a canonical contributor graph:

```text
ContributorIdentity
├── GitHub handle
├── emails[]
├── names[]
└── aliases[]
```

with repository-level overrides:

```yaml
identities:
  "@alice":
    emails: [alice@gmail.com, alice@company.com]
```

and a `checkowners identity audit` command that surfaces probable duplicate contributors **without silently merging uncertain identities**.

### 10.9 🔴 Wall-clock non-determinism produces CODEOWNERS churn and flapping CI

**Defect.** `analyze_ownership` calls `datetime.now(UTC)` and passes it into every recency computation.

**Consequence.** The same repository at the same commit produces different confidence scores on different days. With a 90-day half-life, scores move roughly 0.77% per day. `sync` generates diffs on unchanged code; drift results oscillate across `min_confidence_delta: 0.2` and the severity boundaries at 0.3 and 0.7; a pull request goes red on Tuesday and green on Wednesday with no code change; no result is reproducible for audit.

**Fix.** Add `--as-of <ISO8601>`, honor `SOURCE_DATE_EPOCH`, and default to the HEAD commit timestamp rather than now, so analysis becomes a pure function of `(repo, commit, config)`. Emit `analysis_ref` and `analysis_epoch` in every JSON payload. Add drift hysteresis so a delta must persist across N runs or exceed the threshold by a margin before flipping severity. Add a documented `--deterministic` guarantee and a test that analyzes twice with a clock offset and asserts byte-identical output. Guarantee a stable sort order for equal-confidence owners so generated files are byte-stable — `_score_owners` already sorts by `(-confidence, handle)`, which is the right instinct and must be preserved everywhere.

### 10.10 🟠 A 90-day half-life punishes correct stability

**Defect.** `scoring.recency_half_life_days: 90` applied uniformly, combined with `analysis.lookback_days: 365`.

**Consequence.** Mature, security-critical, protocol, or infrastructure code that legitimately has not changed in eighteen months decays toward zero, falls below `confidence_threshold`, and generates `missing` drift for files that have a perfectly good, perfectly available owner. That is exactly the class of file where knowing the historical expert matters most. The absence of recent commits does not mean nobody understands the code.

**Fix.** Separate `active_expertise`, `historical_expertise`, and `maintenance_recency`. Measure recency relative to the file's own change cadence rather than wall-clock: a file whose median inter-commit interval is 400 days must not be penalized for a 300-day gap. Use adaptive half-lives — shorter for high-churn paths, longer for stable ones. Stable-code ownership should decay intelligently, not vanish at a date boundary.

### 10.11 🟠 Squash merging distorts the model

**Defect.** Squash merges collapse many contributor interactions into a single commit. `_get_commit_history` parses only author email and timestamp; `Co-authored-by:` trailers are never read. `git log --name-only` omits merge commits by default. Rebase-merge repositories attribute differently again.

**Consequence.** Co-authors, intermediate commits, reviewers, and pair-programming partners are invisible. The merge strategy materially changes results and is documented nowhere.

**Fix.** Parse `Co-authored-by:` trailers and weight co-authors configurably; supplement raw git with PR metadata when API data exists; add `git.merge_strategy: auto | squash | merge | rebase`; document exactly how each strategy is handled. The engine must internalize that **git attribution ≠ complete engineering contribution history**.

### 10.12 🟠 Generated and vendored detection is a static blocklist

**Defect.** `paths.exclude` is a hand-curated tuple.

**Consequence.** It misses `yarn.lock`, `poetry.lock`, `Cargo.lock`, `go.sum`, `Gemfile.lock`, `composer.lock`, `*.pb.go`, `*_pb2.py`, `*.g.dart`, generated GraphQL and OpenAPI clients, Terraform lockfiles, `target/`, `build/`, `.next/`, `__generated__/`, snapshot files, and every migration directory.

**Fix.** Read `.gitattributes` and honor `linguist-generated` and `linguist-vendored` — the mechanism GitHub itself uses and that repositories already maintain. Optionally vendor the upstream linguist vendor patterns. Keep the static list as a fallback, not the primary mechanism.

### 10.13 🔴 Consolidation is unverified against the project's own resolver

**Defect.** `generate.py` collapses a directory into `/dir/ @owners` when every inferred file beneath shares an owner set, with no round-trip verification.

**Consequence.** Consolidation plus last-match-wins ordering is exactly the transformation where an off-by-one in rule ordering silently un-owns a subtree. Because GitHub does not load a CODEOWNERS file over 3 MB *at all* — code-owner information is not shown and reviews are not requested, silently, while branch protection still appears configured — an oversized generated file is a complete governance bypass that looks like success.

**Fix.**
- Add a mandatory post-generation verification pass: re-resolve every tracked path against the generated file using the project's own pattern engine and fail generation if any path's resolved owners differ from its inferred owners.
- Add a hard size guard: warn at 2 MB, refuse above a configurable ceiling well under 3 MB without `--force`, and name the limit in the error.
- Emit `bytes_written` and `rules_written` in `--json`.
- Ship `checkowners explain-path <path>` showing which rule wins and why — the single most requested capability in every CODEOWNERS tool.

### 10.14 🟠 Path sanitization can produce false ownership

**Defect.** Bracket-bearing paths are sanitized because GitHub ignores `[...]` lines, and sanitized patterns that collide are merged.

**Consequence.** `routes/[id]/` and `routes/[slug]/` may both collapse toward `routes/*/`, which also matches `routes/static/`. A *syntax correctness* problem silently becomes a *semantic over-ownership* problem, which is worse.

**Fix.** When exact representation is impossible, refuse and explain:

```text
WARNING: GitHub CODEOWNERS cannot precisely represent: routes/[id]/
Generated fallback: routes/*/
This also matches: routes/[slug]/, routes/static/
Affected owners differ. Refusing automatic consolidation.
```

Allow `--allow-broad-patterns` for explicit opt-in. Prefer safety over convenience.

### 10.15 🟠 The validator is not yet competitive

**Defect.** `validate.py` is syntax-only: it mirrors GitHub's documented rules (relative patterns valid, owner-less rules valid, `!` and `[...]` rejected) and stops there.

**Consequence.** It addresses none of the CODEOWNERS failure modes that actually cause unreviewed merges, all of which are **silent**: an owner without write access is ignored; a team that is not visible or lacks write access is ignored even if every member individually has access; an invalid line is skipped; a file over 3 MB is not loaded at all; the file must be on the base branch of the pull request; last-match-wins quietly overrides specific rules with a later general one. A syntactically perfect file in which half the owners cannot approve anything will report zero drift forever while no review is ever requested. Meanwhile a mature competing validator already checks owner validity (accounts exist, are organization members, teams exist), duplicated patterns, file existence, and not-owned files, with distinct exit codes — and GitHub itself has exposed CODEOWNERS syntax errors through REST and GraphQL since 2022, so the authoritative validator already exists and was hand-rolled instead.

**Fix — make `checkowners audit` the strongest CODEOWNERS auditor available.** Audit dimensions:

| Dimension | What it reports |
|---|---|
| Syntax | invalid patterns, unsupported constructs, malformed entries |
| Invalid owners | `@nonexistent-user`, `@deleted-team` |
| Permissions | owner exists but lacks required repository access |
| Invisible teams | teams unavailable for ownership assignment |
| Unowned files | `17.8% of tracked code has no declared owner` |
| Observationally unowned | `9.3% of tracked code has no sufficiently confident observed expert` |
| Duplicate rules | `/foo @alice` and `/foo @bob` |
| Shadowed rules | earlier rules made irrelevant by later ones under last-match-wins |
| Dead patterns | patterns matching no tracked file |
| Excessively broad rules | `* @platform` making one team responsible for nearly everything |
| Self-protection | does CODEOWNERS own itself, so ownership changes are reviewed by authorized owners |
| Ruleset audit | CODEOWNERS exists but required code-owner review is disabled |
| Drift | declared ownership ≠ observed expertise |
| Concentration | one person holds 91% of inferred expertise |
| Decayed owners | declared owner with no meaningful interaction for 420 days |
| Orphaned ownership | a former or deleted contributor dominates current knowledge |

Also build a conformance harness that runs the local validator and GitHub's own errors endpoint against a corpus of real files and publishes the diff rate. That converts "we implemented GitHub's semantics" from a claim into a measurement. Handle the known caveat that the endpoint can return an empty error list for branches in private repositories even when the UI shows errors.

### 10.16 🟠 The review signal is a biased sample

**Defect.** `github.REVIEW_SCAN_PR_LIMIT = 200` bounds the scan to the 200 most recently updated closed pull requests, scoped to `GITHUB_REPOSITORY`.

**Consequence.** Bounding was the correct fix for rate-limit exhaustion, but on a repository merging 200 pull requests per week the review signal covers seven days while the commit signal covers 365. The 0.15-weighted factor measures something structurally different from the other three, and its meaning varies with repository velocity.

**Fix.** Make the window time-based (`review_lookback_days`, aligned with `analysis.lookback_days`), report `reviews_sampled` and `reviews_window_days` in the payload, warn when the sample covers materially less than the lookback window, and use GraphQL batching to widen the window cheaply.

### 10.17 🟠 Topology uses connected components, which is too crude

**Defect.** `topology._cluster` performs greedy connected-component traversal over a co-ownership adjacency graph.

**Consequence.** The classic chaining problem: `Alice ↔ Bob`, `Bob ↔ Carol`, `Carol ↔ Dave` yields one four-person "team" even when Alice and Dave have never worked on overlapping code. The algorithm, similarity measure, resolution parameter, seed, and stability guarantee are all unspecified, which makes the output unfalsifiable.

**Fix.** Build weighted `developer ↔ developer` edges from co-ownership, co-change, co-review, shared-module activity, and temporal collaboration. Evaluate Leiden, Louvain, Infomap, and weighted spectral clustering. Fix the seed, expose the resolution parameter, and report community confidence, modularity, and stability across seeds. Support **multi-membership, nested teams, and matrix teams** — a person can simultaneously be Platform, Observability, Architecture, and Security Champions, and a strict partition does not model modern engineering organizations. Compare inferred and declared teams probabilistically rather than as exact match/mismatch:

```text
Inferred cluster: Alice, Bob, Carol, Dave
Declared @payments: Alice, Bob, Carol
Jaccard: 0.75
Potential cross-team expert: Dave
```

State honestly that inferred teams are a hypothesis for a human to confirm.

### 10.18 🟠 Trends compare a different metric than `analyze`

**Defect.** `trends.py` reconstructs per-period snapshots from a single `git log` pass and renormalizes over the available recency and frequency weights, because historical blame and review state cannot be cheaply reconstructed.

**Consequence.** The historical confidence series is computed from a weaker, different model than the present-day number. Charts appear comparable and are not.

**Fix.** Name them separately — `ownership_confidence` versus `historical_activity_confidence` — or version the metric explicitly:

```json
{ "schema_version": "1", "model_version": "ownership-v2", "signals": ["recency","frequency","blame","review"] }
{ "model_version": "historical-v1", "signals": ["recency","frequency"] }
```

Either reconstruct properly via `git blame --reverse` or checkout-per-period (expensive but correct), or rename so nobody assumes mathematical equivalence where none exists.

### 10.19 🟠 Reviewer load balancing conflates authorship with reviewing

**Defect.** `balance._gather_counts` falls back to `_gather_from_authorship` when the API path is unavailable, recording `source = "git_authorship"` plus a `fallback_reason`.

**Consequence.** Recording provenance is genuinely good and must be kept — but authorship load is not review load. A developer with many commits may perform few reviews; a developer with few commits may be the team's dominant reviewer.

**Fix.** Never render a bare `review load: 47` when it came from commits. Always surface `load_signal: git_authorship_proxy` versus `load_signal: github_completed_reviews` in human-readable output as well as JSON. When API data exists, model rolling windows over `reviews_requested`, `reviews_completed`, `PRs_waiting`, `median_response_time`, `reviewed_lines`, `PR_complexity`, and `ownership_criticality`, then produce a recommendation rather than a ranking of humans:

```text
@alice  expertise 0.94  current load high
@bob    expertise 0.81  current load low
Recommended: @bob
```

### 10.20 🟠 Onboarding is heuristic and named as though it were not

**Defect.** `onboard.py` derives a learning path largely from ownership and bus-factor information rather than a semantic dependency path through the codebase.

**Fix.** Rank modules by dependency order, architectural centrality, code complexity, test coverage, change frequency, ownership availability, documentation coverage, and domain criticality, then emit a reasoned sequence with suggested mentors. Combining a code dependency graph with the ownership graph is what would make onboarding genuinely differentiated. Until then, describe it as inferred suggestions, not a personalized learning plan.

### 10.21 🟠 "Knowledge graph" is more ambitious terminology than the data model

**Defect.** `graph.py` constructs a bipartite/multipartite contributor-file graph, optionally including teams, using confidence as edge information.

**Consequence.** That is an **ownership graph**, not a knowledge graph in the modern semantic-data sense.

**Fix.** Either rename it precisely, or make it deserve the term. To deserve it, expand entities to `Developer`, `Team`, `File`, `Directory`, `Package`, `Service`, `Repository`, `Commit`, `PullRequest`, `Review`, `Incident`, `Dependency`, `Domain`, `CODEOWNERSRule`, and edges to `AUTHORED`, `REVIEWS`, `OWNS_DECLARED`, `OWNS_OBSERVED`, `MEMBER_OF`, `DEPENDS_ON`, `MODIFIES`, `REPLACES`, `REVIEWED_BY`, `KNOWS`, `DECAYING_EXPERTISE`. Then real questions become answerable: which service has no active expert; which developer is an articulation point between Payments and Platform; which services depend on code whose sole expert left; what is the shortest knowledge-transfer path from Alice to Checkout; which teams have hidden cross-team dependencies.

Layer graph risk analytics on top: articulation points (people whose removal disconnects knowledge domains), betweenness centrality (people who bridge teams and services), community boundaries (architecture-to-team misalignment), orphan subgraphs (modules with no active expert), ownership islands, transfer paths (`Alice → Bob → Carol`, where Bob is the logical intermediary), and organizational coupling (teams repeatedly changing each other's code).

### 10.22 🟠 State has no staleness contract, no locking, and no atomic writes

**Defect.** `state.py` writes with plain `target.write_text(...)` in three places, keys on a repository hash, validates the embedded absolute path, and does not record the analyzed ref.

**Consequences.**
- A user can `analyze`, pull 400 commits, run `bus-factor`, and receive a silently stale answer with only a stderr hint.
- Two concurrent CI jobs on the same runner, or two terminals running `analyze`, can interleave writes and corrupt the cache.
- CI checkouts at different paths, or the same repository cloned twice locally, always miss the cache and re-analyze in full.
- There is no cache size bound, no eviction, and no cache command.

**Fix.** Write atomically via temp file plus `os.replace`, with an advisory lock. Embed `analyzed_ref` and refuse to reuse state whose ref is not an ancestor of HEAD unless `--allow-stale`. Add `--max-age` and `--no-cache`. Extend the cache key to include repository identity, HEAD, configuration hash, ownership-model version, and package version where relevant — **never reuse evidence computed under materially incompatible scoring assumptions**. Add `checkowners cache info | clear | path | purge`.

### 10.23 🟠 `sync` commits directly, which fights every modern workflow

**Defect.** `sync` generates CODEOWNERS and commits the result.

**Consequence.** In CI, commits made with the ambient token do not trigger downstream workflows, so the resulting commit runs no checks; it collides with branch protection and required reviews on the default branch; and it cannot be reviewed — which is odd for a tool about review routing.

**Fix.** `sync --pr` becomes the default, direct commit becomes the escape hatch. The eventual end state is `checkowners sync --pull-request` opening `chore: reconcile CODEOWNERS ownership drift` with per-rule rationale, confidence, and supporting evidence in the body. Human-reviewed ownership automation is a strong end state; silent commits are not.

### 10.24 🟠 The webhook notifier is a liability with no compensating value

**Defect.** `notify.py` POSTs drift payloads containing contributor identities to a configured URL.

**Consequences.** No HMAC request signing, so the receiver cannot verify authenticity. No documented timeout, no retry with backoff, no idempotency key. No scheme allowlist, so an `http://` URL sends contributor data in plaintext. No payload redaction. No native Slack, Teams, or PagerDuty formatting, so every user writes a translation shim anyway. Meanwhile the Action already emits structured `GITHUB_OUTPUT` that any workflow can pipe to any notification Action in three lines of YAML.

**Fix.** Remove it and replace it with a documented three-line YAML recipe, or rebuild it with signing, timeouts, backoff, idempotency, a scheme allowlist, and redaction. Do not keep it as-is: it adds a network egress path, a security-review question, and a maintenance burden in exchange for duplicating something the platform does better.

### 10.25 🟠 Hard-coded blame parsing and the absence of hotspot context

**Defect.** Risk is currently expressed as a bus-factor tier alone.

**Consequence.** A file with bus factor 1 that changes once every three years is *risky*. A file with bus factor 1 that changes thirty times a week and sits at the center of the dependency graph is *urgent*. The current model cannot tell them apart.

**Fix.** Compute a composite:

```text
risk = ownership_concentration
     × change_frequency
     × dependency_criticality
     × code_criticality
     × expertise_decay
```

and surface it:

| Path | Risk | Reason |
|---|---:|---|
| `payments/core.py` | 96 | single expert + high churn |
| `auth/token.py` | 88 | single expert + dependency criticality |
| `legacy/report.py` | 42 | stale expertise |
| `docs/foo.md` | 5 | low criticality |

Allow explicit criticality input, since risk without criticality is incomplete:

```yaml
criticality:
  "payments/**": 1.0
  "auth/**": 1.0
  "analytics/**": 0.6
  "docs/**": 0.1
```

and infer partial criticality from dependency centrality, production-service manifests, and deployment configuration.

---

## 11. GitHub Action and CI Problem Register

The Action is the primary distribution surface. It currently carries the most severe defects in the project.

### 11.1 🔴 The Action never passes a token to the CLI

**Defect.** `action.yml` sets exactly two environment variables on the CLI steps — `CHECKOWNERS_CONFIG` and `CHECKOWNERS_DRIFT_MODE`. It never sets `GITHUB_TOKEN`, and there is no `github_token` input at all.

**Why this is fatal.** In GitHub Actions, `GITHUB_TOKEN` is *not* automatically present as an environment variable inside a step; it must be explicitly passed. The `actions/github-script` step receives a token because that action requests one; `run:` steps do not.

**Consequences, all user-visible.**
1. Non-noreply email-to-handle resolution cannot run in CI; only local noreply parsing works.
2. Team and subteam collapsing cannot run in CI.
3. Every `github.api_enabled` feature — the review confidence signal, topology reconciliation, review-load balance — cannot run in CI.
4. Therefore `drift` in CI, on any repository that is not fully squash-merged, compares raw emails against `@handle` rules and emits the identity note instead of comparing. **The Action's headline feature silently degrades to "we could not compare" on exactly the repositories that need it most.**

**Aggravating documentation error.** The FAQ states that `${{ secrets.GITHUB_TOKEN }}` "is automatically available." That is true only in the sense that the secret can be referenced; it is false in the sense the sentence implies — that the CLI will find it. And the 0.5.0 changelog claims the Action "installs the `github` extra so handle resolution works in CI." Installing `pygithub` without a token does not make handle resolution work; it only makes the import succeed. The docs assert a capability, the changelog claims it was fixed, and the Action does not wire it.

**Fix.** Add a `github_token` input defaulting to `${{ github.token }}` and export `GITHUB_TOKEN` on all CLI steps. Correct the FAQ sentence.

### 11.2 🔴 The Action hard-fails on fork pull requests

**Defect.** The `comment_on_pr` step defaults to `"true"`, is gated only on `github.event_name == 'pull_request'`, calls `github.rest.issues.createComment` with the ambient token, and has no `continue-on-error` and no try/catch.

**Consequence.** On a public repository, the token is read-only for pull requests originating from forks regardless of the `permissions:` block. The API call returns 403, the step fails, and because composite-action steps abort the action on failure, **the entire check fails on every external contributor's pull request** even when drift is clean. Organizations that default workflow permissions to read-only — now the recommended and increasingly common posture — see the same failure on every PR. A permissions problem is indistinguishable from a real finding. For a tool whose target market includes open-source repositories, this is a guaranteed-fail path on exactly the pull requests that matter for community growth.

**Fix.** Wrap the API calls in try/catch, degrade to `$GITHUB_STEP_SUMMARY` with a warning annotation, add a `github_token` input so an App token or PAT can be substituted, detect fork pull requests and skip commenting with an explanatory notice, and consider defaulting `comment_on_pr` to `false` with documented opt-in. A tool that fails closed on a permissions detail is uninstalled before it is debugged.

### 11.3 🔴 The Action installs an unpinned package at runtime

**Defect.** `checkowners_version` defaults to `""`, and the install step therefore runs `pip install --upgrade "checkowners[graph,github]"`.

**Consequence.** `uses: smusali/checkowners@v0.5.0` creates the impression of a pinned dependency while executing whatever PyPI serves at that moment, inside a job granted `pull-requests: write`, with no pin, no hash, no lockfile, and no `--only-binary` constraint. A new PyPI release silently changes CI behavior for every downstream user without any workflow change. A compromised publishing account executes arbitrary code in every consumer's CI. Builds are not reproducible. The Action cannot run on an air-gapped or egress-restricted runner at all. Action v0.5.0 will happily install a future 0.9.0 with a changed JSON shape and then parse it with 0.5.0-era JavaScript.

The irony is sharp: the project performs Trusted Publishing with Sigstore attestations on the way *out* of PyPI, then discards all of that assurance on the way *in* to the consumer's CI.

**Fix.** Generate the default `checkowners_version` from the Action tag at release time so the two can never diverge: Action `vX.Y.Z` installs `checkowners==X.Y.Z`. Ship a committed `requirements.lock` with hashes and install with `--require-hashes`. Add an `index_url` input for internal mirrors and an offline mode installing a vendored wheel. Keep `checkowners_version` as an explicit advanced override. For an eventual moving `v1` tag, the floating major tag is normal Marketplace convention, but the internal Python dependency must still be intentionally controlled.

### 11.4 🔴 Third-party actions are not SHA-pinned

**Defect.** `actions/setup-python@v5` and `actions/github-script@v7` are referenced by mutable tag.

**Consequence.** Mutable tags are re-pointable — the exact vector behind multiple recent Actions supply-chain incidents — and a hard failure of the OpenSSF Scorecard pinned-dependencies check. Any organization running Scorecard as an intake gate rejects the Action on this alone, which for the stated buyer profile is disqualifying.

**Fix.** Pin to full 40-character commit SHAs with the version in a trailing comment, and add Dependabot or Renovate configured for `github-actions`.

### 11.5 🟠 `GITHUB_OUTPUT` heredoc fragility and payload size

**Defect.** Three steps write multi-line JSON using a literal `EOF` delimiter, then `cat` the file between markers. `bus-factor --all --json` is written in full.

**Two independent problems.**
- *Delimiter collision.* GitHub's own guidance is to use a randomly generated delimiter that cannot appear in the value. A static delimiter is a latent injection and corruption vector the moment any path, handle, note, or reason contains a line matching it exactly.
- *Size.* `bus-factor --all` emits one entry per tracked path. A single output is capped at 1 MB and total outputs at 50 MB per run. On the 24k-commit, 12k-file monorepo the project itself cites as its benchmark, the full payload will plausibly exceed the per-output cap, and the failure mode is opaque truncation rather than a clear error.

**Fix.** Generate the delimiter randomly. Emit *summaries* to `GITHUB_OUTPUT` — counts, severity, max delta, top N — and write full payloads to an uploaded artifact, exposing an `artifact_name` output. Add a `max_output_entries` input with a documented default and explicit truncation flags in the payload. Add `$GITHUB_STEP_SUMMARY` rendering, which is free, has no meaningful size ceiling, requires no permissions, and works on forks where commenting cannot.

### 11.6 🟠 Two divergent CI implementations

**Defect.** `checkowners github-action` exists as a subcommand and is documented as the command that runs the full CI flow and writes `GITHUB_OUTPUT`. `action.yml` does not call it: it invokes `drift`, `bus-factor`, and `decay` as three separate steps and assembles output in bash heredocs. `action.yml` even contains a vestigial comment about publishing "the same as the historical key used by older workflows" followed by a `cat` that publishes nothing additional.

**Consequence.** The documentation describes an architecture the Action does not use; `github-action` is effectively dead code carrying a test file and maintenance cost; the two paths will diverge further on the next feature; and the Action implicitly runs analysis up to three times, relying on state reuse that the docs describe as hint-emitting rather than guaranteed.

**Fix.** Make `action.yml` call `checkowners github-action` once and delete the bash assembly. One process, one analyze pass, one JSON assembly, testable in Python, no heredoc quoting hazards.

### 11.7 🟠 Three different defaults for the same setting

| Location | `drift.mode` default |
|---|---|
| `checkowners.yml` config reference | `commit` |
| `action.yml` input default | `both` |
| `docs/USAGE.md` Actions example | `repo` |

A user reading the documentation top to bottom gets three different behaviors depending on which surface they configured. Additionally, `CHECKOWNERS_CONFIG` and `CHECKOWNERS_DRIFT_MODE` are load-bearing environment variables documented only in an `action.yml` comment, while the USAGE configuration reference documents only `CHECKOWNERS_STATE_DIR`. And `action.yml` never sets `CHECKOWNERS_STATE_DIR` despite `SECURITY.md` advising exactly that for CI — the project does not follow its own security guidance in its own Action.

**Fix.** One value, stated once, referenced everywhere. Document every environment variable in the configuration reference. Set `CHECKOWNERS_STATE_DIR` to a runner-temp path in `action.yml`.

### 11.8 🟠 Markdown injection and unpaginated comment lookup

**Defect.** `listComments` uses `per_page: 100` with no pagination, so on a pull request with more than 100 comments the marker is not found and a duplicate comment is created on every push — precisely the behavior the marker system exists to prevent. Drift paths and notes are interpolated into a Markdown table with backtick wrapping and no escaping, so a tracked path containing a backtick (legal on POSIX filesystems) breaks the table or injects Markdown into a comment posted by a write-scoped token. If the drift step fails for any reason, no comment is posted and the user receives a raw stack trace instead of a diagnostic.

**Fix.** Paginate the lookup, escape interpolated values, and always render a diagnostic on failure.

### 11.9 🟠 `install_spec` accepts an arbitrary pip specification

**Defect.** The input is passed straight to `pip install`.

**Consequence.** If any consumer ever interpolates untrusted data into that input, it is remote code execution. Low likelihood, but an unnecessary footgun.

**Fix.** Document the hazard prominently, and validate or constrain the accepted forms.

### 11.10 🟠 Full-history requirement plus zero caching makes the check slow

**Defect.** The shallow-clone guard is correct — inference from insufficient history would be misleading — but every Action run performs setup-python (10–25 s), `pip install` with `networkx`, `pygithub`, `typer`, `rich`, `pyyaml` and transitive dependencies (10–30 s), `actions/checkout` with `fetch-depth: 0` (potentially minutes on a large monorepo), then a full analysis (up to two to three minutes on 24k commits) — **from scratch on every pull request**, because nothing caches `~/.checkowners` and `setup-python` has no `cache: pip`.

**Consequence.** Competitors ship a single static binary or run via `npx`. This is the slowest option in the category by an order of magnitude, and slow required checks get removed.

**Fix.** Cache pip and `~/.checkowners` keyed on the merge base. Add `--diff-only` so PR runs analyze only changed paths. Build incremental analysis keyed on `analyzed_ref`, so a three-minute CI step becomes a three-second one.

### 11.11 🟠 No SARIF, no annotations, no step summary, no reusable workflow

**Fix.** Add SARIF output so findings land in the GitHub Security tab and inline on the diff — and, critically, without needing `pull-requests: write`. Suggested rule IDs: `checkowners/unowned-path`, `checkowners/stale-owner`, `checkowners/ownership-drift`, `checkowners/invalid-owner`, `checkowners/shadowed-rule`, `checkowners/single-expert-risk`, `checkowners/incomplete-analysis`. Add `::warning file=path::` annotations so decay and concentration warnings appear on the changed line. Add `$GITHUB_STEP_SUMMARY` rendering. Ship a reusable workflow alongside the composite Action. Add JUnit XML for generic CI test-report integration.

### 11.12 🟠 A good PR comment, taken further

The managed single-comment behavior is right. The content should become concise and actionable rather than a JSON dump:

```text
## CheckOwners

### Ownership
✅ 8/8 changed files have observed owners

Recommended reviewers
- @alice — 0.91
- @bob — 0.74

### CODEOWNERS drift
⚠ `payments/providers/new.py`
Declared: @platform
Observed: @payments

### Knowledge risk
🔴 `payments/settlement.py`
81% of expertise belongs to one contributor
```

---

## 12. Packaging, Supply Chain, and Release Engineering Problems

| Problem | Evidence | Fix |
|---|---|---|
| `[project.urls]` point at the previous organization | `pyproject.toml` Homepage / Issues / Source | Rewrite to `smusali/checkowners`; add `Documentation` and `Changelog` entries; **ship a patch release**, because PyPI project links come from the uploaded distribution, so editing `main` alone fixes nothing users see |
| README documentation links are relative | PyPI renders them against the project page, producing dead URLs | Use absolute `https://github.com/smusali/checkowners/blob/main/...` URLs, or rewrite at build time. 15-minute fix on the one page where a Python developer decides whether to install |
| No floating major tags | Only `v0.5.0` exists | Publish `v0`, `v0.5`, and eventually `v1`, which is standard Marketplace practice |
| Old-slug reintroduction is possible | Nothing prevents it | Add a CI job that greps the whole tree for the old organization slug and fails the build |
| Redirect fragility | Renames resolve today; Actions workflows do not follow renames, and old references break the moment a name collision occurs under the old organization | If the old location is still controllable, leave a stub repository whose `action.yml` fails with a migration message |
| `all` extra duplicates rather than composes | `all = ["networkx>=3.0", "pygithub>=2.0.0"]` | `all = ["checkowners[graph,github]"]`; self-referential extras are long supported |
| No upper bounds, no lockfile | `typer`, `rich`, `pyyaml`, `pygithub`, `networkx` all unbounded | Add ceilings or a documented support policy; a future major `typer` or `pygithub` breaks every install with no maintainer action. The project's own CI is subject to the same drift it inflicts on Action users |
| No `dev` extra | Contributors without `hatch` cannot bootstrap from `pyproject.toml` alone | Add one |
| Version hard-coded | `version = "0.5.0"` in `pyproject.toml`, duplicated for `--version` | Single-source it via `hatch-vcs` or equivalent |
| No Python 3.14 classifier or CI matrix entry | Classifiers stop at 3.13 | Add it |
| Python 3.10 excluded | `requires-python = ">=3.11"` excludes a very common system Python on long-term-support Linux and many self-hosted runners | Support 3.10 only if cheap; do not complicate the code to chase old runtimes — standalone artifacts are the better answer |
| No SBOM | Attestations exist without one | Publish an SBOM alongside them; it is a cheap extension of existing provenance work |
| Releases may not have survived the transfer | Marketplace shows v0.5.0 while the repository release list appeared empty at review time | Verify; if releases are missing, every `uses: …@v0.5.0` reference is more fragile than it appears |
| Distribution channels desynchronized | Tag, GitHub release, PyPI, Marketplace, docs version, changelog | One pipeline: tag → test → build → attest → publish PyPI → publish binaries → publish Docker → generate SBOM → GitHub release. Avoid independent mutable channels |
| Security controls not visible | No Scorecard, no CodeQL, no dependency review, no `pip-audit`/OSV, no signed release artifacts beyond PyPI | Add them, plus a published threat model: what source data is read, what leaves the machine, when GitHub is contacted, what is cached, whether the cache contains sensitive identities, where it lives, how tokens are handled, what permissions are required. A project that participates in CI governance should aspire to be more secure than the average repository |
| Least-privilege permissions not documented | Examples do not show minimal permission blocks | Show `contents: read` plus `pull-requests: write` only when commenting is enabled, and `contents: read` alone otherwise. Never train users to paste `permissions: write-all` |

---

## 13. Testing and Quality-Assurance Problems

**The testing strategy is refuted by the project's own changelog.** `CONTRIBUTING.md` mandates that unit tests mock all subprocess calls and must not require a real git repository, with an 85% coverage target. The 0.5.0 changelog then documents what running the tool against one real repository found:

- Drift comparison was literal string equality, so directory rules never matched inferred file paths and real-world files produced near-100% false `missing`/`stale` reports.
- `validate` demanded every pattern start with `/` or `*`, failing perfectly valid real-world files.
- Generated CODEOWNERS emitted `[...]` character ranges, which GitHub ignores, silently un-owning those paths.

Three catastrophic, product-invalidating bugs. All three shipped. All three passed an 85%-covered suite. All three were caught by one real repository.

**Conclusion:** the coverage number measures execution of Python lines while mocking away the entire problem domain. Every git-semantics bug, every CODEOWNERS-semantics bug, and every pattern bug is invisible to a suite that mocks git and never renders a file GitHub would actually parse.

**Additional defects.**
- `addopts = "--strict-markers"` contains no `--cov-fail-under`. The 85% target documented in two places is aspirational and unenforced.
- `select = ["E","F","I","N","W","UP","ANN","B","A","SIM"]` omits `S` (flake8-bandit) in a codebase whose entire core is subprocess invocation of git with user-supplied paths and patterns — the single most relevant rule family. It also omits `PTH`, despite "all file paths via `pathlib.Path`" being a documented, human-enforced convention a linter could enforce for free.
- No property-based or fuzz testing of the pattern engine — the highest-risk component, which has already shipped one catastrophic correctness bug.
- No differential test corpus against an independent reference implementation.
- No mutation testing.
- No OS matrix; path separators, git behavior, and subprocess semantics differ on Windows.
- No git-version matrix; `--line-porcelain`, `--ignore-revs-file`, and `ls-files` behavior vary across versions.

**Fix — the full QA program.**
1. **Real-git fixtures.** A pytest fixture that runs `git init` in `tmp_path` and scripts commits with pinned `GIT_AUTHOR_DATE` and `GIT_COMMITTER_DATE`. Port at least the drift, patterns, generate, and validate suites onto it. Keep the mocked unit tests; add integration tests above them.
2. **A public CODEOWNERS compatibility corpus.** Thousands of `{pattern, path, expected}` cases covering roots, nested directories, `**`, `*`, `?`, spaces, escaped spaces, comments, line ordering, unsupported negation, bracket syntax, unusual Unicode names, dotfiles, paths resembling comments, multiple owners, and owner-less rules. Add 500 real CODEOWNERS files harvested from public repositories as regression inputs.
3. **Property-based fuzzing.** Generate random pattern/path pairs and compare against a trusted reference implementation and, where feasible, GitHub's own validation. Assert the invariants directly: `**` crosses segments, `*` does not, leading `/` anchors, last match wins, `dir/*` is direct children only. CODEOWNERS compatibility is not an edge feature; it is foundational, and pattern bugs directly produce wrong owner, wrong review, and wrong drift.
4. **Mutation testing** on `patterns`, scoring, `drift`, `generate`, and identity. If flipping `if match:` to `if not match:` does not fail a test, the coverage number is misleading.
5. **Model golden tests** encoding complete synthetic histories and their expected interpretations — these define the philosophy of the project:
   - Alice creates `foo.py`, Alice edits it, Bob reformats it, Alice reviews Bob → *Alice remains primary expert; Bob's formatting commit has little ownership impact.*
   - Alice owns `foo.py`; `foo.py` is renamed to `bar.py` → *ownership survives the rename.*
   - Alice disappears; Bob maintains the component for a year → *ownership transitions gradually.*
6. **Determinism tests.** Same repository, configuration, API snapshot, and model version must produce byte-identical output. Guard against unordered sets, thread completion order, and filesystem traversal order — especially since the blame pass is parallel.
7. **Enforce the coverage floor** (`--cov-fail-under`), publish the badge, and track critical-module branch coverage rather than optimizing for the badge alone.
8. **Cross-platform CI** on Ubuntu, macOS, and Windows across supported Python versions, specifically exercising spaces, Unicode, path separators, drive roots, case sensitivity, and long paths.

---

## 14. Privacy, Ethics, and Legal Exposure

**This is a workforce-analytics product wearing an infrastructure jacket, and it is currently undefended.** Enumerate what the tool produces about named individuals:

- A per-person, per-path expertise ranking with numeric scores.
- A dormancy detector flagging individuals as decaying or departed, recommending that their responsibilities be transferred away.
- A review-load report identifying overloaded reviewers and proposing redistribution between named people.
- A commit-count proxy explicitly labeled as a proxy when the API path is unavailable.
- A knowledge graph of contributors, files, and inferred teams.
- Historical trends of all of the above.

It caches contributor email addresses on disk, sends contributor emails to a third-party user-search API, POSTs them to arbitrary webhooks, and posts derived findings into pull-request comments visible to the whole organization.

**What is missing.** No anonymization or pseudonymization mode. No aggregate-only reporting mode. No retention policy or TTL on the handle cache. No `PRIVACY.md`, no data-flow description, no statement of what leaves the machine. No explicit "this is not a performance metric" disclaimer — the standard, necessary guardrail for this class of tool. No way to exclude an individual on request.

**Why this blocks adoption.** In the EU, per-employee productivity and expertise profiling can trigger works-council consultation and plausibly a data-protection impact assessment; a local unencrypted cache of contributor emails plus third-party lookups is processing of personal data with no documented retention policy and no controller guidance. Elsewhere it is a straightforward manager-misuse hazard: a balance or decay report is one screenshot away from a performance review. The first time an engineering manager pastes a decay report into a performance conversation, the tool is banned from that organization — reasonably. The existing `SECURITY.md` covers token handling and state-directory hygiene well and says nothing about the fact that the state directory is a database of who knows what about whom.

**Fix — treat this as adjacent to P0 for any enterprise ambition.**
- An explicit, prominent statement in README, USAGE, and every report footer: this is a knowledge-risk tool, not a performance-measurement tool, and using it for individual evaluation is unsupported and harmful.
- `output.anonymize: true` producing stable pseudonymous identifiers, so risk topology can be reviewed without naming people.
- `output.aggregate_only: true` suppressing all per-individual output while retaining repository-level risk.
- `--redact-emails`, and hashed emails at rest in the state cache by default.
- `identity.mode: handle | email | hashed`.
- `docs/PRIVACY.md`: exactly what is read, what leaves the machine, what is cached where, how to purge it, how to disable identity lookup, how to redact emails from output.
- `checkowners cache purge`.
- `contributors.exclude` so individuals can opt out, plus a "do not merge" identity override.
- Consider making `balance` opt-in rather than default-on in the Action.
- **Never position the tool around "who contributes least," "who is really working," or "which engineer is underperforming."** The data is not designed for that and the positioning would poison open-source adoption. Position around repository resilience, knowledge continuity, review routing, organizational alignment, and onboarding.

---

## 15. Positioning, Claim Honesty, and Documentation Problems

### 15.1 The "first" claim is not worth the risk

The README currently asserts that CheckOwners is "the first CODEOWNERS tool that treats it as a confidence-scored spectrum and surfaces the second-order risks." Even where narrowly defensible, this invites a burden nobody needs: first according to what search, first open-source, first Action, first with this exact scoring model? Prior art exists on git-history CODEOWNERS inference — at least one generator has produced CODEOWNERS from git history for years with `--since`, minimum-commit-count, maximum-committer, and identifier-selection options, which is inference, thresholding, and a lookback window minus the scoring. Prior art on bus factor is an entire research and tooling lineage.

**Fix.** Replace with a claim that is stronger, cleaner, and provable from the product itself:

> **CheckOwners treats code ownership as a confidence-scored spectrum rather than a static binary declaration.**

And state the actual, narrower, defensible differentiator:

> **No other open-source tool combines git-history inference, calibrated per-path confidence, pattern-aware drift with severity tiers, and knowledge-risk reporting behind a single CI-native JSON contract.**

### 15.2 The comparison table is a strawman

`docs/USAGE.md` compares CheckOwners against four alternatives and scores every one of them "no" on all seven dimensions. That is a foil, not a comparison. It omits every serious competitor, misrepresents at least one of the four, and will be dismantled publicly by the first informed reader — converting a real advantage into an apparent lack of homework.

**Fix.** Replace it with an honest matrix that gives competitors credit where they earn it:

| Tool | Infers from history | Confidence score | Drift detection | Owner validity | Knowledge risk | Forges |
|---|---|---|---|---|---|---|
| **checkowners** | yes | yes, four-factor | yes, pattern-aware with severity | syntax and handle format | qualified-owner depth, decay, topology, balance | GitHub |
| History-based generator | **yes** | no, threshold only | no | no | no | git |
| Monorepo CODEOWNERS compiler | no, composes files | no | **yes, `check` mode** | no | no | GitHub |
| Dedicated CODEOWNERS validator | no | no | partial: not-owned plus file-exist | **yes: verifies accounts, org membership, teams** | no | GitHub |
| Ownership-audit CLI | no | no | no | no | ownership stats only | GitHub |
| GitHub-endpoint linter | no | no | no | **yes, via GitHub's own endpoint** | no | GitHub |
| Bus/truck-factor research tooling | yes | knowledge model | no | no | **yes, formal bus/truck factor** | GitHub / git |
| GitHub native | no | no | no | partial, UI errors | no | GitHub |

### 15.3 Competitive positioning, honestly stated

- **GitHub CODEOWNERS.** Native, trusted, simple, integrated into review enforcement. Static declaration; infers nothing. *CheckOwners opportunity: become the intelligence and reconciliation layer above it.*
- **Dedicated validators.** Mature validation orientation, multiple integrity checks, CI flexibility, including checks CheckOwners does not have at all. *Lesson: CheckOwners' validation must meet or exceed what users expect from a specialist.*
- **CODEOWNERS generators/compilers.** Distributed ownership declarations and monorepo compilation. *Lesson: support distributed ownership inputs rather than treating the root file as the only source of organizational truth.*
- **Overlapping ownership-analysis tools.** Historical ownership analysis with bus factor, hot files, stale and orphan knowledge, reviewer suggestions, CODEOWNERS generation, JSON, CI. *Lesson: breadth does not establish superiority; the algorithms must become substantially more rigorous.*
- **Visual ownership explorers.** Self-contained interactive reports over ownership across time. *Lesson: terminal tables are not enough; a great interactive HTML report would materially improve usability.*
- **Commercial behavioral code analysis.** Mature framing around knowledge distribution, key-person risk, hotspots, former contributors, and team/code alignment. *Opportunity: be the excellent, transparent, local-first open-source ownership-intelligence layer without replicating an entire commercial suite.*

### 15.4 Other messaging corrections

- **Benchmark discrepancy.** The README says under two minutes; the changelog says under three, for the same claim in the same release. Two numbers for one claim is a credibility leak.
- **Feature-list-first README.** The README leads with capabilities. It should lead with the problem, then the proof, then the diff.
- **The strongest trust assets are invisible.** Trusted Publishing, Sigstore attestations, and the pure-git offline guarantee appear nowhere in the README.
- **"No LLMs" shipped alongside agent artifacts.** `CLAUDE.md` sits in the repository root and an AI agent appears as one of two contributors. This is not hypocrisy and there is nothing wrong with it — it is a *presentation* problem, because a hostile reader will screenshot both and the discussion becomes about that instead of the tool. Either move agent instructions into a directory already excluded from the sdist and let the positioning stand cleanly, or own it explicitly and turn it into a strength: the inference is deterministic git analysis, the codebase was built with agent assistance, every heuristic is auditable in `analyze.py`, and every AI-assisted change is human-reviewed, tested, and signed off. Ambiguity is the only bad option.

### 15.5 The README rewrite

```markdown
# CheckOwners

**Keep CODEOWNERS aligned with reality.**

Your CODEOWNERS says:

    /payments/ @platform

But the last 12 months show:

    @alice  91%
    @bob    78%

Both are now on @payments.

    $ checkowners audit

    HIGH ownership drift
    /payments/
    Declared: @platform
    Observed: @payments

    Suggested:
    -/payments/ @platform
    +/payments/ @payments

CheckOwners analyzes git and review history to infer who actually knows each
part of your repository, then compares that evidence with your CODEOWNERS
policy. Use it to detect stale or incorrect rules, find code with dangerously
concentrated knowledge, recommend knowledgeable reviewers, and discover
ownership gaps before they become operational risk.

Local-first. Git-native. No source-code upload. No LLM required.

    uvx checkowners audit
```

Then install, run, CI. Only afterward the graphs, topology, and onboarding. Add a 30-second terminal recording — clone, `uvx checkowners audit`, result, then the resulting PR warning. A good recording explains this tool better than two thousand words, and a visual, terminal-first CLI whose README contains no terminal output is a self-inflicted wound.

### 15.6 Documentation information architecture

```text
Getting Started      5-minute audit · CI setup · Generate CODEOWNERS
Concepts             Declared vs observed · Ownership score · Evidence quality ·
                     Drift · Knowledge risk · Identity resolution
Guides               Monorepos · GitHub teams · Existing CODEOWNERS ·
                     Large repositories · Offline mode · Security-sensitive repos
Reference            CLI · Configuration · JSON schema · Exit codes · Action I/O
Internals            Scoring model · Pattern semantics · Caching · Benchmark methodology
Project              Changelog · Roadmap · Security · Contributing · Governance
```

Plus `docs/limitations.md`, documented aggressively — an intelligence tool earns trust by explaining when it can be wrong:

```text
Git history is not complete human expertise.
Squash merges can hide authorship.
Blame can over-credit formatting changes.
CODEOWNERS may intentionally differ from expertise.
GitHub team analysis requires API access.
Review history availability affects evidence.
Historical trend scores use different data.
Identity resolution can be ambiguous.
```

Build the docs site with a static generator with instant search, versioned docs, examples, command reference, scoring methodology, and interactive demo reports. Do not build a marketing-heavy site yet.

---

## 16. Community and Governance Problems

| Gap | Impact | Fix |
|---|---|---|
| Bus factor of 1 | The tool that measures bus factor has a bus factor of one. This is the first question a platform team asks | Recruit a second maintainer publicly, with release rights, and say so in `GOVERNANCE.md` |
| AI co-authorship undisclosed in the README | For CI-executed code, an explicit human-review statement is a trust asset; silence is the liability | State the review process plainly |
| No OpenSSF Scorecard workflow or badge | Free, automated, targets exactly the buyer persona | Add it. It will currently score poorly on pinned dependencies and branch protection, which is precisely why running it is valuable |
| No OpenSSF Best Practices badge | Standard trust signal | Add it |
| No issue or PR templates, no `good first issue` labels, no Discussions | Zero on-ramp for the contributors the project needs | Add all three |
| No public roadmap | Nothing tells a contributor what to build | `ROADMAP.md` plus 20–30 carefully scoped issues labeled `good first issue`, `help wanted`, `research`, `performance`, `correctness`, `github`, `documentation` — for example: support `.git-blame-ignore-revs`; add GraphML export; add a Homebrew formula; add property tests for the matcher; add `--offline`; add `cache info`; add a Windows path-test corpus |
| No `CITATION.cff` | For a tool making research-adjacent claims this is table stakes and unlocks academic citation | Add it |
| No architectural decision records | Deeper contribution is hard | `docs/adr/0001-confidence-model.md`, `0002-codeowners-pattern-engine.md`, `0003-identity-resolution.md`, `0004-bus-factor-v2.md` |
| No contributor ladder | Ironically, an ownership tool should have exceptionally clear project ownership | Contributor → Reviewer → Maintainer → Core Maintainer, with documented criteria |
| No release automation documentation | Trusted Publishing is in place; the process is undocumented | Document it |
| 56 days with an empty `Unreleased` | Reads as abandoned | Ship a patch release now; cadence beats features |
| No `FUNDING.yml`, no `examples/`, no screenshots, no demo | Nothing to look at before installing | Add all of them, including sanitized design-partner configurations |
| Zero issues treated as a positive | For a young project, `0 issues / 0 PRs / 0 forks / 0 stars` does not imply perfect quality; it means almost nobody is participating | The objective is to create enough real usage that bugs and disagreements begin appearing. A useful project with fifty thoughtful issues is healthier than an invisible one with zero |
| Open-core intent unstated | Contributors will notice the organizational context; ambiguity suppresses contribution | State plainly in `README.md` or `GOVERNANCE.md` what stays MIT forever and what, if anything, becomes commercial. Say it before someone asks |

---

## 17. Risk Registers

### 17.1 Correctness risks ranked by blast radius

| # | Risk | Likelihood | Impact | Severity |
|---|---|---|---|---|
| 1 | Action never passes `GITHUB_TOKEN`; drift silently degrades to "cannot compare" | Certain | Core feature non-functional in CI | **Critical** |
| 2 | Blame without `-w -M -C` and `.git-blame-ignore-revs`: formatter sweeps reassign ownership | High | Confidently wrong owners on mature repositories | **Critical** |
| 3 | Fork PR comment 403 fails the whole Action | Certain on public repositories | Every external contribution shows a red X | **Critical** |
| 4 | Unpinned `pip install` in a `pull-requests: write` job | Certain | Full supply-chain exposure for every consumer | **Critical** |
| 5 | Bus factor capped by `top_n_owners`, not the literature definition | Certain | Headline metric is indefensible | **High** |
| 6 | Confidence capped at 0.85 without the API; thresholds not comparable | Certain | Every gate means something different per configuration | **High** |
| 7 | `critical` severity on any `bus_factor <= 1` plus `fail_on_drift: true` | Certain on small repositories | Red X on the first PR, immediate uninstall | **High** |
| 8 | Owners generated without verifying write access or team visibility | High | Silently un-owned paths, false sense of coverage | **High** |
| 9 | Stale previous-organization references in eight documented locations | Certain | Broken installs the day a name collision occurs | **High** |
| 10 | Fully mocked test suite over a git-semantics problem domain | Certain | The next severe bug also ships | **High** |
| 11 | No 3 MB guard on generated CODEOWNERS | Medium on monorepos | GitHub silently loads nothing at all | **Medium** |
| 12 | `listComments` unpaginated at 100 | Medium | Duplicate comments on busy PRs | **Medium** |
| 13 | No file locking or atomic writes on state and handle caches | Medium | Corrupted cache, confusing wrong output | **Medium** |
| 14 | 90-day half-life penalizes legitimately stable code | High | False `missing` drift, alert fatigue | **Medium** |
| 15 | `handles.json` global across repositories, negative caching without TTL | Medium | Cross-repository identity leakage, permanent misses | **Medium** |
| 16 | Consolidation ordering silently un-owns a subtree | Medium | Merge-gate bypass | **Medium** |
| 17 | Markdown injection through backticks in paths in PR comments | Low | Broken or manipulated comment body | **Low** |
| 18 | `install_spec` accepts an arbitrary pip specification | Low | RCE if a consumer interpolates untrusted input | **Low** |

### 17.2 Adoption, credibility, and business risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Split-brain organization reference persists and every trial user bounces | **Certain today** | Fatal | Fix every reference this week |
| A user's latest-package install changes CI behavior mid-sprint | High | Severe | Pin the Action's package version to its tag |
| Someone benchmarks the bus factor against the literature and publishes the critique | Medium | Severe, credibility | Rename pre-emptively and publish methodology |
| Generated CODEOWNERS silently un-owns a subtree via consolidation ordering | Medium | Severe, merge-gate bypass | Round-trip verification |
| A manager uses decay reports in a performance review and the tool is org-banned | Medium | Severe | Anonymization plus explicit non-endorsement |
| Data-protection objection to email caching and API lookup blocks EU adoption | Medium | High | Privacy docs and redaction |
| Daily confidence drift causes flapping CI and teams disable the check | **High** | High | Determinism, hysteresis, baseline |
| Monorepo user hits the output size cap with opaque truncation | Medium | High | Summaries plus artifacts |
| A breaking dependency release breaks all installs | Medium | High | Upper bounds and a lockfile |
| Project reads as abandoned after two quiet months and never recovers | **High** | High | Ship now; cadence beats features |
| A competitor with distribution adds confidence scoring first | Low–Medium | High | Ship the spec and the study; own the definition, not the feature |

### 17.3 The three structural risks

1. **The biggest risk is not competition.** It is that CheckOwners keeps adding impressive-sounding analytics before making its fundamental definition of ownership empirically and mathematically trustworthy — producing twelve commands, twenty reports, beautiful output, and uncertain answers. For this problem, correctness compounds: if ownership inference is trusted, everything downstream becomes valuable; if it is distrusted, everything downstream becomes decoration.
2. **The second biggest risk is building for hypothetical enterprises before obtaining open-source users.** Do not spend six months on SSO, multi-org dashboards, RBAC, ticket integrations, and a database while the repository has no community. First make one command so useful that maintainers independently recommend it.
3. **The third biggest risk is trying to be everything simultaneously** — CODEOWNERS plus behavioral code analysis plus engineering-productivity analytics plus a service catalog plus a reviewer bot plus a developer graph plus an onboarding platform. Ownership intelligence is already a massive category. Stay focused.

---

## 18. Solution Register — The Reshaped Command Surface

### 18.1 Commands to add

**`checkowners doctor` — verify ownership against reality.** This is the missing killer feature and the ideal wedge, because it requires no inference at all and therefore works on day one for teams that do not yet trust the scoring.

```text
$ checkowners doctor

  CODEOWNERS ................. .github/CODEOWNERS on `main`  ok
  File size .................. 412 KB / 3 MB                 ok
  Syntax (GitHub API) ........ 0 errors                      ok
  Owner write access ......... 3 of 41 owners LACK write     FAIL
     @alice-contractor    no write access (offboarded 2026-03)
     @bob-intern          read only
     @org/design-review   team not visible to this repo
  Empty teams ................ 1 team has zero members       FAIL
     @org/platform-oncall
  Shadowed rules ............. 2 specific rules overridden    WARN
     /apps/api/auth.ts @security-team
       shadowed by `*` @org/everyone on line 3 (last match wins)
  Coverage ................... 8,412 / 12,088 files (69.6%)  WARN
  Dead rules ................. 14 patterns match no file      WARN
  Bus factor 1 paths ......... 340 files, 1 owner             WARN

  2 failures, 4 warnings.  Exit 1.
```

A second, environment-facing form of the same idea eliminates enormous amounts of debugging:

```text
Git repository ✓   Full history ✓   GitHub token ✓   Token permissions ✓
CODEOWNERS found ✓ .github/CODEOWNERS      GitHub API ✓   Team visibility ✓
Review-history access ✓   .git-blame-ignore-revs ✓   Cache ✓
Model ownership-v2   Package 0.7.1   Config valid
```

**`checkowners explain PATH` — make every inference inspectable.** Probably the single highest-leverage UX improvement, and the best possible demo recording.

```text
path/to/file.py

Observed owners
──────────────────────────────────────────────────
@alice                        0.86 confidence
  Recency 0.92   Frequency 0.81   Blame 0.89   Reviews 0.74
  37 commits · 68% current blame · 11 reviews
  last meaningful change: 12 days ago

@bob                          0.54 confidence
  Recency 0.33   Frequency 0.58   Blame 0.62   Reviews 0.71

Evidence quality:             0.91
Declared CODEOWNERS:          @company/platform
Team resolution:              @alice ∈ @company/platform, @bob ∈ @company/platform
Assessment:                   aligned
```

with the inverse form, which is equally important:

```text
$ checkowners explain path/to/file.py --why-not @carol

@carol was not inferred because:
- last contribution: 418 days ago
- 3% current blame
- no reviews in observation window
- confidence: 0.12 < threshold 0.30
```

Support `explain FILE`, `explain DIRECTORY`, `explain FILE --owner @alice`, and `explain FILE --why-not @bob`. A system that influences repository governance must never behave like an unexplained scoring black box, and a confidence score you cannot interrogate is a score nobody trusts.

**`checkowners simulate --remove @alice --remove @bob` — knowledge-loss simulation.** The single most compelling executive-facing output in the category, directly matching how the literature defines the metric, and the screenshot that gets shared:

```text
Removing 2 of 34 contributors:

Files losing their ONLY owner ......... 412  (3.4% of repo)
Files dropping to bus factor 1 ........ 1,203
Directories fully orphaned ............ 7
   services/billing/          182 files   no remaining owner
   infra/terraform/vpc/        41 files   no remaining owner
Repo truck factor ..................... 4 -> 2

Suggested transfers (by residual confidence):
   services/billing/   -> @carol (0.61)  @dave (0.44)
   infra/terraform/vpc -> @erin  (0.38)  no strong second
```

**`checkowners suggest --pr <n>` / `checkowners diff BASE...HEAD` — the reviewer recommender.** This is the difference between a tool people run quarterly and a tool people run daily, and it is what finally makes load balancing earn its keep — by feeding load into a recommendation instead of publishing a ranking of humans.

```text
$ checkowners suggest --diff origin/main...HEAD

14 files changed across 3 ownership clusters.

Recommended reviewers:
  @carol   0.88   owns 9/14 changed files, active this week
  @dave    0.71   owns services/billing/, last touched 12 days ago
  @erin    0.44   backup for infra/, would raise bus factor here

Not recommended:
  @alice   0.91 expertise but 6 open reviews already (overloaded)

Coverage: 14/14 changed files have at least one qualified reviewer.
```

The diff form is more CI-friendly than repeatedly analyzing the entire repository:

```text
Changed: payments/processor.py, payments/providers/foo.py
Recommended reviewers: @alice 0.89, @bob 0.77
Ownership risks introduced: providers/foo.py has only one inferred expert
CODEOWNERS drift: none
```

**`checkowners audit` — the default front door.** Running the bare command should produce a repository audit, with detailed commands underneath:

```text
CheckOwners Repository Audit

CODEOWNERS
  ✓ valid syntax
  ✓ 97.8% file coverage
  ⚠ 4 stale rules
  ✗ 2 declared teams diverge from observed ownership

Knowledge risk
  ✗ 6 critical single-expert components
  ⚠ 14 decaying expertise areas

Ownership
  12,483 tracked files · 11,927 analyzed · 556 insufficient evidence

Top drift
  services/payments/**  HIGH
  services/search/**    MEDIUM

Run: checkowners explain services/payments/core.py
     checkowners audit --json
```

**`checkowners risk` — unify bus factor, decay, hotspots, concentration, and orphaned expertise:**

```text
Repository knowledge risk
────────────────────────────────────────────────────────────
CRITICAL  services/payments/
          TF50: 1 · effective owners 1.31
          dominant expert: @alice (86%) · change frequency: very high

HIGH      auth/crypto/
          TF50: 1 · effective owners 1.62
          latest active expert activity: 214 days

MEDIUM    analytics/legacy/
          historical expert no longer active · successor confidence 0.44

Repository
────────────────────────────────────────────────────────────
Observed ownership coverage      96.8%
Knowledge-at-risk                17.4%
Critical components              4
Ownership vacuums                3
High CODEOWNERS drift            2
```

**Other commands worth adding.**
- `checkowners owners PATH` / `checkowners who PATH` — the simplest question deserves the simplest command, with no giant analysis report: `@alice 0.87 / @bob 0.61`.
- `checkowners why PATH @alice` — full score decomposition including the specific commits behind each contribution and which configuration knobs would change the outcome.
- `checkowners coverage --fail-under 80` — ownership coverage as a first-class, badge-able SLO, plus a shields.io endpoint so repositories can display `ownership 87%` beside their coverage badge, plus coverage trend over time. This is a real, defensible, easily understood number that nobody currently owns mindshare on.
- `checkowners baseline create` / `--baseline` — the ratchet. This is the deciding factor for adoption in any repository large enough to need the tool: turning CheckOwners on for a 12k-file monorepo produces thousands of findings on day one and gets it turned off on day two. A baseline plus "fail only on new drift" converts an unusable tool into an installable one. Every successful linter in history shipped this.
- `checkowners cache info | clear | path | purge` — developers hate mysterious cached behavior.
- `checkowners identity audit` — surface probable duplicate contributors without silently merging.
- `checkowners transfers` — infer knowledge transfer (`Alice owns → Bob reviews → Bob contributes → Alice declines → Bob's ownership rises`) and warn when expertise is declining with no successor identified.
- `checkowners report --html checkowners.html` — self-contained, no server, no upload, no account: repository overview, treemap sized by lines or change volume and colored by risk, developer-to-component ownership map, ownership timeline, declared-versus-observed team alignment, and drill-down into any directory. This could become one of the strongest adoption mechanisms, because screenshots become self-explanatory.
- `checkowners org <name>` — organization-wide aggregation with CSV and Parquet output.
- `checkowners serve --mcp` — expose `who_owns(path)`, `suggest_reviewers(diff)`, `bus_factor(path)`, `explain(path, owner)` to coding agents. The "pure git, no LLMs" positioning becomes a genuine feature here: it is the deterministic ground-truth tool that agents call.
- `checkowners calibrate` / `calibrate --fit` — back-test inference at time T against the reviewers who actually reviewed after T; optionally learn per-repository weights.
- `--offline` — even though tokenless operation already works, an explicit flag converts an implicit property into a security promise: `Network access: disabled / Review evidence: unavailable / Team verification: unavailable`.

### 18.2 Commands to kill, demote, merge, or reframe

| Command | Recommendation | Why |
|---|---|---|
| `notify` | **Remove** | Duplicates `GITHUB_OUTPUT` plus any notification Action; adds unsigned network egress, a security-review question, and a config section. Replace with a documented three-line YAML recipe |
| `graph` | **Demote to `analyze --export dot`** | Nice demo, low daily utility, sole justification for the `networkx` extra |
| `topology` | **Hold behind `--experimental`** | Weakest inference in the product, and the one whose errors are socially costly: "the tool says you are not on the team you are on" |
| `sync` | **Rewrite as `sync --pr`** | Direct commits fight branch protection and skip CI |
| `balance` | **Merge into `suggest`** | Publishing a ranked list of humans by workload is a liability; feeding load into a reviewer recommendation is a feature |
| `onboard` | **Keep, deprioritize** | Genuinely nice; nobody adopts a tool for it |
| `trends` | **Keep, refocus** | Becomes valuable once ownership coverage is the tracked metric, and once its metric is renamed or reconstructed properly |
| `print` | **Fold into `analyze --format text`** | Two commands for one operation |
| `github-action` | **Make it the only CI path** | Ends the two-implementation split |
| `bus-factor` | **Keep as a compatibility alias under `risk`** | Preserves existing users |
| `decay` | **Keep as a compatibility alias under `risk`** | Preserves existing users |

### 18.3 The mature command surface

```text
Core        checkowners audit · owners PATH · explain PATH · generate · diff REV
Risk        checkowners risk · simulate
Reporting   checkowners report · coverage
Support     checkowners doctor · cache · baseline

Advanced / compatibility
            analyze · drift · bus-factor · decay · graph · topology ·
            balance · onboard · trends · validate · sync · github-action
```

Also label maturity explicitly so experimental heuristics do not erode trust in mature capabilities, and graduate features out of Labs only once validated:

```text
Core     audit · analyze · generate · drift · explain
Risk     bus factor / concentration · decay · hotspots
Org      teams · reviewer load
Labs     topology · onboarding · knowledge graph · trends
```

Do not add commands indefinitely. Sixteen commands with zero users is a liability, not a feature list.

---

## 19. Solution Register — Metric and Contract Specifications

### 19.1 The ownership model, version 2

For contributor `u` and path `p`:

```text
C(u,p) = Σ wᵢ aᵢ Sᵢ(u,p) / Σ wᵢ aᵢ
```

with signals:

```text
R = recency                       F = meaningful change frequency
B = blame / current authorship    V = review participation
H = historical depth              S = change survival
M = maintainer continuity
```

and a separately computed evidence quality `Q(u,p)` derived from amount of history, age of path, number of observations, API completeness, identity certainty, blame reliability, and rename confidence.

### 19.2 Change survival

How much of a developer's contribution still survives in the current implementation. If Alice wrote 80% of a file three years ago and 75% of those lines and concepts remain, that historical expertise matters. If the component has since been rewritten by others, it matters much less. This bridges historical commits and current blame far more intelligently than either alone, and it is what makes decay modeling defensible.

### 19.3 Ownership pathologies worth naming

- **Knowledge vacuum.** Not "no CODEOWNERS rule" but "no contributor has sufficiently strong, current, reliable expertise": `legacy/auth/ — latest meaningful expert activity 742 days ago, highest confidence 0.21`. More interesting than "unowned."
- **Phantom ownership.** CODEOWNERS names someone who has never touched or reviewed the component. Could be intentional governance, could be organizational fiction. Label it *declared ownership without observed expertise* and ask for review rather than declaring it wrong.
- **Shadow maintainer.** Declared `@platform`, dominant observed expert `@bob` from `@payments`. Reveals organizational drift, unofficial responsibilities, workload problems, undocumented knowledge, and team-boundary problems.
- **Review monopoly.** One developer performs 72% of reviews for a component despite multiple knowledgeable owners. Distinct from ownership concentration; track `review_concentration` and feed it into balancing.
- **Ownership/review divergence.** Alice writes the code, Bob reviews everything, Carol is the declared owner. Show all three roles; do not compress them into one score.

### 19.4 Drift severity and direction

Not every difference matters equally. Declared `{Alice, Bob}` versus observed `{Alice, Bob, Carol}` is mild; declared `{Alice, Bob}` versus observed `{Dave, Erin}` is severe. Compute severity from identity overlap, team overlap, confidence gap, criticality, and activity, then tier it `INFO / LOW / MEDIUM / HIGH / CRITICAL`. Also classify direction, because remediation differs:

- **Missing observed expert** — CODEOWNERS lacks a major current expert.
- **Stale declared owner** — CODEOWNERS includes someone with decayed expertise.
- **Complete ownership replacement** — observed experts no longer overlap declared ownership at all.
- **Organizational mismatch** — the observed expert belongs to a different team.

### 19.5 Suggested drift schema

```json
{
  "path": "services/payments/**",
  "declared": { "owners": ["@company/platform"] },
  "observed": { "owners": ["@alice", "@bob"], "teams": ["@company/payments"] },
  "drift": {
    "severity": "high",
    "type": "organizational_mismatch",
    "declared_observed_overlap": 0.0
  },
  "recommendation": {
    "action": "review_codeowners_rule",
    "suggested_team": "@company/payments"
  }
}
```

### 19.6 Suggested ownership schema

```json
{
  "schema_version": "1.0",
  "model_version": "ownership-v2",
  "path": "services/payments/core.py",
  "head_sha": "abc123",
  "analysis": {
    "completeness": 0.94,
    "signals_available": ["recency", "frequency", "blame", "reviews"]
  },
  "owners": [
    {
      "identity": "@alice",
      "ownership_score": 0.89,
      "evidence_quality": 0.93,
      "signals": { "recency": 0.94, "frequency": 0.81, "blame": 0.91, "reviews": 0.77 }
    }
  ],
  "risk": {
    "top_owner_share": 0.71,
    "effective_owners": 1.72,
    "truck_factor_50": 1,
    "truck_factor_75": 2
  }
}
```

This is far more future-proof than one ambiguous confidence number plus one ambiguous bus-factor number.

### 19.7 Every machine-readable output must carry provenance

```json
{
  "schema_version": "1.0",
  "checkowners_version": "0.8.0",
  "model_version": "ownership-v2",
  "repository": "…",
  "head_sha": "…",
  "generated_at": "…",
  "analysis_completeness": 0.94
}
```

Never let downstream consumers reverse-engineer text output. Publish a JSON Schema per command, version it, validate output against it in CI, and document a stability policy.

### 19.8 Analysis completeness as a first-class metric

Sources of missing evidence: shallow history, absent token, unavailable team membership, unavailable review history, ambiguous identities, excluded files, insufficient history, API rate limits.

Surface `analysis completeness: 73%` and provide `--fail-on-incomplete` for strict CI. **Explicit completeness beats silent degradation**, always.

### 19.9 Model and metric versioning — do this before 1.0

Once organizations put `confidence >= 0.55` into CI, changing the scoring formula is a breaking change even when the JSON shape is identical.

```yaml
model:
  ownership: v2
  risk: v1
  topology: v1
```

Then algorithms can improve without silently changing enforcement behavior.

### 19.10 Stable exit-code semantics

```text
0  success / no policy violations
1  policy violation (or internal error, if the scheme separates them)
2  configuration or usage error
3  findings
4  git/GitHub integration failure
```

Competitors publish exit-code tables; scripts need predictable contracts. Provide `--exit-zero` as an escape hatch. Today `validate --json` is documented to exit non-zero while `drift`'s exit semantics are specified nowhere — which is exactly why `action.yml` re-parses the JSON in an inline Python heredoc instead of checking the status.

### 19.11 Policy separation: enforcement versus information

```yaml
policy:
  drift:
    fail: true
  single_expert:
    fail: false
    severity: warning
  incomplete_analysis:
    fail: true
```

Knowledge risk often requires months of remediation and should not instantly block every pull request. Teams must decide what is blocking.

### 19.12 Suppressions with reasons and expiry

```yaml
suppressions:
  - path: legacy/**
    rule: single-expert
    expires: 2026-12-31
    reason: "Scheduled for retirement in Q4"
```

Require a reason; support optional expiry; never encourage permanent invisible ignores.

### 19.13 Resource budgets and rate-limit awareness

```yaml
analysis:
  max_runtime_seconds: 300
  max_git_workers: 16
  max_api_requests: 2000
```

When the budget is exhausted, report *analysis incomplete* — never pretend completeness. Before expensive API collection, check remaining requests and reset time, degrade adaptively, and say so: `Review evidence omitted: GitHub API budget insufficient.`

### 19.14 Reproducible external evidence

```json
{
  "github_evidence_collected_at": "…",
  "repository_head": "…",
  "team_snapshot": "…"
}
```

A report regenerated six months later must clearly indicate why results can differ.

### 19.15 Configuration, version 2

```yaml
version: 2

analysis:
  lookback_days: adaptive
  max_owners: 5
  exclude_bots: true
  follow_renames: true
  use_blame_ignore_revs: true
  as_of: ""                    # ISO8601; defaults to HEAD commit time
  respect_gitattributes: true  # linguist-generated / linguist-vendored

qualification:
  strategy: adaptive
  min_commits: 1
  strong_blame_override: 0.5

git:
  blame_ignore_revs_file: .git-blame-ignore-revs
  use_mailmap: true
  detect_moves: true
  count_co_authors: true
  merge_strategy: auto         # auto | squash | merge | rebase

model:
  version: ownership-v2
  normalize_available_signals: true
  signals:
    recency:           { weight: 0.25 }
    frequency:         { weight: 0.15 }
    blame:             { weight: 0.25 }
    reviews:           { weight: 0.20 }
    historical_depth:  { weight: 0.15 }

identity:
  github_resolution: auto
  mailmap: true

risk:
  truck_factor_thresholds: [0.50, 0.75, 0.90]
  critical_effective_owners: 1.5

criticality:
  "payments/**": 1.0
  "auth/**": 1.0
  "docs/**": 0.1

security:
  paths: [auth/**, crypto/**]
  minimum_effective_owners: 2
  require_declared_owner: true
  require_active_expert: true

bots:
  exclude: true
  additional: [release@company.com]

drift:
  baseline_file: ""            # ratchet: fail only on new findings
  hysteresis_runs: 1

policy:
  drift:               { severity: error }
  unowned:             { severity: warning }
  incomplete_analysis: { severity: warning }

privacy:
  redact_emails: true
  contributors_exclude: []

output:
  schema_version: 1
  anonymize: false
  aggregate_only: false
  max_bytes: 2500000           # hard guard under GitHub's 3 MB ceiling
  verify_round_trip: true
```

Exact defaults must be empirically calibrated rather than copied blindly. Keep the standard model opinionated — most users do not want to become statisticians — while allowing research overrides and, eventually, plugin models.

### 19.16 Hybrid CODEOWNERS generation

The long-term model is not `human file OR machine-generated file`. It is `human intent + machine inference`:

```text
# checkowners:manual:start
/security/** @security-team
/legal/**    @security-team
# checkowners:manual:end

# checkowners:generated:start
/backend/payments/** @payments-team
/backend/search/**   @search-team
# checkowners:generated:end
```

Or compile `CODEOWNERS.manual` plus `CODEOWNERS.generated` into `.github/CODEOWNERS`.

There are always exceptions where organizational accountability intentionally differs from observed expertise: `security/` may legally or operationally belong to `@security-team` even if history says one platform engineer wrote most of it. CheckOwners should **detect and explain that discrepancy, not erase the policy**.

Add suggested patches rather than silent application:

```diff
$ checkowners audit --fix-plan

-/payments/** @platform
+/payments/** @payments
```

and for subtler cases: *"Consider adding @alice while retaining @platform. @alice has 0.91 observed expertise and is not currently represented."*

### 19.17 Distributed and component-level ownership

Files are too granular for many engineering decisions. Infer ownership at file, directory, package, service, and repository level. Recognize ecosystem manifests — `package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`, `pom.xml`, `build.gradle`, `*.csproj`, `WORKSPACE`, `BUILD`, `Dockerfile` — plus workspace definitions (`package.json` workspaces and maintainers fields, `pnpm-workspace.yaml`, `turbo.json`, `nx.json`, Cargo workspaces, Bazel targets, Gradle modules). Accept explicit component definitions:

```yaml
components:
  payments: { paths: ["services/payments/**"] }
  auth:     { paths: ["services/auth/**"] }
```

Accept distributed declarations — `services/payments/OWNERS`, `packages/ui/OWNERS`, nested CODEOWNERS files, and service-catalog metadata — then reconcile three layers explicitly: **declared-local**, **declared-global**, and **observed** ownership. Generating package-boundary-aware rules also keeps the output small, which keeps it under the 3 MB ceiling, which matters.

### 19.18 Evidence schema and provider interfaces

Store observations rather than only scores:

```json
{
  "subject": "@alice",
  "path": "payments/core.py",
  "evidence": [
    { "type": "commit", "timestamp": "…", "weight": 0.72 },
    { "type": "blame",  "share": 0.61 },
    { "type": "review", "timestamp": "…" }
  ]
}
```

Then scoring becomes an interpretation layer over evidence, which enables new models without re-extracting history, richer explanations, model benchmarking, historical reproducibility, and plugins.

```python
class EvidenceProvider:
    def collect(...) -> Iterable[OwnershipEvidence]: ...
```

with `GitCommitProvider`, `GitBlameProvider`, `GitHubReviewProvider`, `GitHubTeamProvider`, `GitLabMergeRequestProvider`, `BackstageProvider`, `ManualOwnershipProvider`. This is far stronger than scattering GitHub-specific conditionals through ownership logic. Make GitHub the best-supported provider, not the architecture — ownership intelligence is not GitHub-specific. Do not overgeneralize early; just preserve the architectural seams.

### 19.19 Multi-forge and multi-dialect output

CODEOWNERS is not GitHub-only and the dialects differ in ways that matter. GitLab has sections, optional sections, per-section approval counts, and default owners — a generator that emits GitLab sections from inferred topology would be genuinely novel. Bitbucket, Azure Repos, Gerrit `OWNERS`, and Chromium-style `OWNERS` are each a writer plugin against the same inference core. Abstract as `--dialect github|gitlab|bitbucket|gerrit` behind a `ForgeWriter` interface, plus a `ForgeClient` interface for identity and permission checks. This roughly triples the addressable audience for the same engine.

### 19.20 A stable library API and machine-readable graph exports

```python
from checkowners import RepositoryAnalyzer
result = RepositoryAnalyzer(...).analyze()
```

A documented, stable public API enables IDE extensions, internal dashboards, GitHub Apps, research, and custom integrations. Do not force everyone to shell out to the CLI. Export graphs as JSON, GraphML, GEXF, and DOT so users can load ownership data into standard graph tooling without CheckOwners building every visualization itself. Prioritize self-contained HTML over an interactive local server: no server, no account, no backend, easy artifact sharing.

### 19.21 Performance architecture

- **Do not blame files that cannot affect the requested command.** PR review recommendation touches only changed paths; `owners foo.py` needs only lineage and evidence for that file; only a full audit needs everything.
- **Cache at blob and history identity level.** Blob SHA unchanged means blame result unchanged, so store `blob_sha → blame ownership`. For contributions, analyze only `previous_HEAD..new_HEAD` and merge deltas. This transforms steady-state CI runtime from `O(repository history)` toward `O(changes since baseline)`.
- **Key the cache on** HEAD, blob SHAs, path lineage, analysis model version, and configuration hash.
- **Subprocess-per-file blame is the wrong shape at scale.** Twelve thousand process spawns means process creation, not blame computation, likely dominates. The under-three-minute result is impressive *given that architecture*, which means the ceiling is much higher. Options: in-process blame via libgit2 bindings; a single `git log --numstat --no-merges` pass computing incremental line attribution; or `git blame --incremental` with batching. Keep the parallelism; make it smarter.
- Add path batching, lazy blame, priority analysis, and a process pool where it genuinely helps.

---

## 20. Paste-Ready Hardening

### 20.1 Hardened Action install step

```yaml
- name: Install checkowners
  shell: bash
  env:
    CHECKOWNERS_VERSION: ${{ inputs.checkowners_version }}
    CHECKOWNERS_INSTALL_SPEC: ${{ inputs.install_spec }}
    CHECKOWNERS_INDEX_URL: ${{ inputs.index_url }}
    # Generated at release time so the Action tag and the package version
    # can never diverge.
    CHECKOWNERS_PINNED_VERSION: "0.5.1"
  run: |
    set -euo pipefail
    INDEX_ARGS=()
    [ -n "${CHECKOWNERS_INDEX_URL}" ] && INDEX_ARGS+=(--index-url "${CHECKOWNERS_INDEX_URL}")
    if [ -n "${CHECKOWNERS_INSTALL_SPEC}" ]; then
      pip install "${INDEX_ARGS[@]}" "${CHECKOWNERS_INSTALL_SPEC}"
    else
      VERSION="${CHECKOWNERS_VERSION:-$CHECKOWNERS_PINNED_VERSION}"
      pip install "${INDEX_ARGS[@]}" "checkowners[graph,github]==${VERSION}"
    fi
    checkowners --version
```

### 20.2 Safe multi-line output

```yaml
- name: Publish drift output
  shell: bash
  run: |
    set -euo pipefail
    DELIM="ghadelim_$(openssl rand -hex 16)"
    {
      echo "checkowners_drift<<${DELIM}"
      # Summary only. Full payload goes to an artifact.
      jq -c '{schema_version, drift_detected, severity, max_confidence_delta,
              notes, counts: {missing: (.missing|length),
                              stale:   (.stale|length),
                              changed: (.changed|length)}}' drift.json
      echo "${DELIM}"
    } >> "$GITHUB_OUTPUT"
```

### 20.3 Fail-soft PR comment plus step summary

```yaml
- name: Render summary
  if: always()
  shell: bash
  run: |
    checkowners drift --format markdown >> "$GITHUB_STEP_SUMMARY" || true

- name: Comment on PR
  if: ${{ inputs.comment_on_pr == 'true' && github.event_name == 'pull_request' }}
  continue-on-error: true
  uses: actions/github-script@<40-char-sha>  # v7.0.1
  with:
    github-token: ${{ inputs.github_token || github.token }}
    script: |
      try {
        // ...existing managed-comment logic, with pagination and escaping...
      } catch (e) {
        core.warning(
          `Could not post PR comment (${e.message}). ` +
          `Grant 'pull-requests: write' or set comment_on_pr: false. ` +
          `Full report is in the job summary.`
        );
      }
```

### 20.4 Token wiring on every CLI step

```yaml
env:
  GITHUB_TOKEN: ${{ inputs.github_token || github.token }}
  CHECKOWNERS_CONFIG: ${{ inputs.config }}
  CHECKOWNERS_DRIFT_MODE: ${{ inputs.mode }}
  CHECKOWNERS_STATE_DIR: ${{ runner.temp }}/checkowners-state
```

### 20.5 `.pre-commit-hooks.yaml` (new file at repository root)

```yaml
- id: checkowners-validate
  name: Validate CODEOWNERS syntax
  description: Validate CODEOWNERS against GitHub's documented semantics.
  entry: checkowners validate
  language: python
  pass_filenames: false
  files: '(^|/)CODEOWNERS$'

- id: checkowners-drift
  name: Detect CODEOWNERS drift
  entry: checkowners drift --baseline .checkowners-baseline.json
  language: python
  additional_dependencies: ["checkowners[github]"]
  pass_filenames: false
  stages: [pre-push]
```

### 20.6 Determinism

```python
# Resolve "now" once, from an explicit source, never from the wall clock.
def resolve_as_of(cli_value: str | None, repo: Repo) -> datetime:
    if cli_value:
        return datetime.fromisoformat(cli_value)
    if (epoch := os.environ.get("SOURCE_DATE_EPOCH")):
        return datetime.fromtimestamp(int(epoch), tz=UTC)
    return repo.head_commit_datetime()   # not datetime.now()
```

### 20.7 Blame and log fidelity

```python
BLAME_ARGS = [
    "blame", "--line-porcelain",
    "-M",   # moved lines within a file
    "-C",   # copied lines across files
    "-w",   # ignore whitespace-only changes
    "--use-mailmap",
]
if (revs := config.git.blame_ignore_revs_file) and Path(revs).exists():
    BLAME_ARGS += [f"--ignore-revs-file={revs}"]

LOG_ARGS = ["log", "--use-mailmap", "--no-merges", "--numstat", "-M", "-C"]
```

### 20.8 Confidence renormalization

```python
def confidence(signals: dict[str, tuple[float, bool]], weights: dict[str, float]) -> float:
    """Weighted mean over AVAILABLE signals only, so the range is always [0, 1]."""
    num = sum(weights[k] * score for k, (score, available) in signals.items() if available)
    den = sum(weights[k] for k, (_, available) in signals.items() if available)
    return _clamp(num / den) if den else 0.0
```

### 20.9 Knowledge concentration primitives

```python
def knowledge_shares(expertise: dict[str, float]) -> dict[str, float]:
    total = sum(expertise.values())
    return {k: v / total for k, v in expertise.items()} if total else {}

def effective_owners(shares: dict[str, float]) -> float:
    """Inverse Herfindahl: 100/0/0/0 -> 1.00, 50/50 -> 2.00, 25/25/25/25 -> 4.00."""
    denom = sum(p * p for p in shares.values())
    return 1.0 / denom if denom else 0.0

def truck_factor(shares: dict[str, float], q: float) -> int:
    """Smallest k whose removal takes away at least q of inferred knowledge."""
    removed, k = 0.0, 0
    for p in sorted(shares.values(), reverse=True):
        removed += p
        k += 1
        if removed >= q:
            return k
    return k
```

---

## 21. Validation Program — The Work Nobody Else Will Do

### 21.1 Why calibration matters

The current weights of `35 / 25 / 25 / 15` are plausible, but nothing explains why they are not `25 / 20 / 30 / 25`. Without empirical calibration those weights are informed heuristics. That is acceptable for `0.x`. It is not acceptable for a category-defining `1.0`.

### 21.2 Ground truth must be future behavior, not the existing file

Existing CODEOWNERS files can be stale — that is the exact problem the tool exists to detect — so treating them as ground truth is circular. Better ground truth: **who subsequently maintains and reviews that code?**

For each historical cutoff `t`:

```text
history before t → CheckOwners prediction → actual reviewers/maintainers after t
```

Evaluate whether inferred owners predict actual reviewers, future maintainers, PR assignees, subsequent authors, declared teams, and accepted review recommendations.

### 21.3 Recommendation labels to capture

Recommended reviewer accepted; recommended reviewer dismissed; another reviewer selected; recommended reviewer unavailable; recommended reviewer no longer on the team; recommendation blocked by ownership policy; security reviewer added; compliance reviewer added; team-level ownership superseded individual evidence; recommendation accepted but incomplete.

### 21.4 Evaluation dimensions

`precision@1`, `precision@3`, `precision@5`, `recall@3`, MRR, NDCG, Brier score, calibration error, changed-file coverage, acceptance rate, rejection rate, false-positive rate, reviewer overload, review latency, time to first useful review, stale-ownership detection, handoff time — broken out by repository type, language, monorepo scale, merge strategy, and team turnover.

### 21.5 Publish a benchmark

Compare baselines honestly:

| Method | Precision@1 | Recall@3 |
|---|---:|---:|
| Most commits | .61 | .72 |
| Git blame | .64 | .75 |
| Recency only | .67 | .77 |
| CheckOwners v2 | **.78** | **.88** |

*Those figures are illustrative placeholders only; actual values must come from experiments.* If CheckOwners substantially beats naive baselines, publish it. Open-source the dataset generation, the evaluation harness, the scores, and the methodology. That benchmark would be more valuable than five more commands, and it is the artifact that would let the project say: **`0.8` confidence means something empirically measured.**

### 21.6 The false-positive ledger

For benchmark repositories, publish prediction, maintainer judgment, reason, and algorithmic fix:

```text
False positive: Formatting migration caused Bob to dominate blame.
Fix: .git-blame-ignore-revs support.
```

Transparency of this kind is unusually powerful and demonstrates scientific maturity. Show cases where the tool was wrong; that is critical to credibility.

### 21.7 Performance benchmark

Turn the anecdote into a reproducible suite across `1k files / 1k commits`, `10k files / 25k commits`, `100k files / 250k commits`, a large monorepo, and a deep-history repository. Measure cold runtime, warm runtime, peak RSS, git command count, API request count, cache size, and incremental runtime. Publish every release, and add a CI job that flags regressions. Then performance claims become defensible instead of anecdotal.

### 21.8 The public ownership-risk dataset

Run CheckOwners across 100–500 significant public repositories. Publish the raw JSON reports as a downloadable dataset with a citable identifier, a findings post ("we measured the ownership risk of the 250 most-depended-on Python packages; 61% have at least one directory with a truck factor of 1"), and an interactive page where anyone can look up their own repository. This is the highest-leverage marketing action available: it costs compute rather than headcount, it validates the inference at scale, and it produces the single artifact that makes a public launch succeed.

### 21.9 Public example reports

Generate reports against recognizable open-source repositories where runtime allows and publish them statically. People understand a tool far faster by inspecting a real report than by reading twenty command descriptions.

### 21.10 Dogfood publicly

Run CheckOwners against CheckOwners and publish the result, with README badges: `Ownership coverage 98% · Critical knowledge risk 2 paths · CODEOWNERS drift 0`. If the tool cannot produce an interesting report about its own repository, fix that first.

### 21.11 The research direction

> **Can repository history and review behavior reliably infer software ownership and predict future maintenance responsibility better than commit-count or blame baselines?**

Dataset: public repositories, historical snapshots, CODEOWNERS files, future reviews, future commits. Outputs: a benchmark dataset, a preprint, an algorithm comparison, and a calibration methodology. That would give the open-source project an unusually strong foundation, and it is the difference between "a good CODEOWNERS tool" and "the reference implementation of how software ownership is measured."

---

## 22. Distribution and Developer Experience

| Gap | Why it matters | Effort |
|---|---|---|
| No `.pre-commit-hooks.yaml` | The single highest-leverage distribution channel for a Python lint-adjacent tool. Repositories adopt via three lines in an existing config. The repository already uses pre-commit for itself but does not expose itself as a hook | 1h |
| No `uvx` / `pipx` documentation | `uvx checkowners audit` is a zero-install trial | 15m |
| No Docker/OCI image | Blocks polyglot teams, GitLab CI, Jenkins, Buildkite, air-gapped runners | 3h |
| No Homebrew formula or tap | The macOS developer default install path | 2h |
| No standalone binaries | Competitors ship one file with no runtime. Python is a real adoption tax for a CLI. Ship `checkowners-linux-x86_64`, `-linux-arm64`, `-darwin-arm64`, `-windows-x86_64.exe` — **but do not rewrite in Go or Rust merely to obtain binaries; package the Python application first** | 1w |
| No `gh` CLI extension | Users already look there; competitors occupy that surface | 1d |
| No GitLab support | GitLab CODEOWNERS has sections, optional approvals, and required-approval counts. Largest single addressable market outside GitHub | 2w |
| No Bitbucket / Gitea / Forgejo / Gerrit / Chromium `OWNERS` | A category-leading tool cannot be single-forge | 3w |
| No Nix package, no GitLab CI component | Ecosystem coverage | — |
| No docs site | Four Markdown files are not discoverable | 4h |
| No demo asset | No recording, no screenshot, no sample output for a tool whose value *is* its reports | 2h |

**Pre-commit is potentially the largest single win.** `checkowners validate` needs no historical analysis at all, so a pre-commit hook can catch a bad pattern, an invalid owner format, a duplicate rule, shadowing, and newly added unowned files *before* a pull request is opened. That creates habitual usage, which creates adoption.

**A GitHub App comes later, not now.** It could eventually deliver automatic reviewer recommendations, a continuous ownership index, organization-wide analysis, team reconciliation, and historical dashboards — but not before the local CLI model is rock-solid. Open-source moat first: the best ownership engine. Hosted product later, if desired.

**Organization-wide analysis is the natural expansion** once per-repository inference is trustworthy:

```text
$ checkowners org analyze acme

Repositories: 214 · Components: 1,932
Critical single-expert components: 117
Former-contributor risk: 42
Teams with ownership drift: 9
Cross-team knowledge dependencies: 27
```

Plus portfolio-level knowledge risk across repositories — *Alice is the sole critical expert in `auth-service`, `billing`, `internal-sdk`, and `terraform-production`* is far more meaningful organizationally than four independent per-repository reports.

---

## 23. Modeling Contributor Status

Git history cannot know employment status, but authenticated organization data plus configurable rosters can. Model contributor status explicitly as `active`, `inactive`, `unknown`, `former`, `bot`, or `external`. Then:

```text
payments/legacy/ — 84% of knowledge attributed to former contributors
```

is dramatically more actionable than `bus factor 1`. This maps directly to one of the highest-value organizational questions: *which critical code is primarily understood by someone who no longer works here?*

Expand bot detection beyond the current `[bot]` / `actions@github.com` / `bot@` heuristics to account type, known automation addresses, dependency bots, release bots, configured identities, and commit signatures — and additionally identify **mass automated commits** even when the committer is technically a human service account.

---

## 24. Enterprise Readiness Gaps

The gaps are not raw technical execution; they are trust, validation, controls, and integration maturity:

documented minimum permissions · a privacy model · identity-resolution controls · auditability · durable overrides · reproducibility · configuration versioning · deterministic outputs · source-of-evidence reporting · false-positive measurement · calibration · team-membership integration · incident and on-call evidence where needed · safe PR-comment retry behavior · explicit handling of generated code · explicit handling of bots and service accounts · clear treatment of departed users · data-flow documentation · API-free operation · case studies · serious pilots · measured business outcomes.

The system must make it trivially easy to understand what evidence was used, what evidence was missing, which configuration produced the result, how a recommendation changed, what a human overrode, and whether a result rests on low-quality or incomplete evidence.

---

## 25. The Open-Core Boundary

If a commercial knowledge-risk layer is intended, draw the line at the repository boundary and draw it publicly.

| Open source, forever, no asterisk | Commercial layer |
|---|---|
| Single-repository analysis, all commands | Cross-repository and organization-wide aggregation |
| Full JSON output, full schema, no gating | Historical time-series storage and trend alerting |
| CODEOWNERS generation, drift, validation | Organization-level identity graph and team reconciliation |
| Per-repository risk, decay, topology | Ranked organization-wide risk register and remediation workflow |
| The Action, the CLI, the pre-commit hook | Hosted dashboards, SSO, RBAC, ticket and chat routing |
| The inference spec and conformance corpus | Benchmarks against peer organizations |

**Rules that protect both sides.**
1. **Never cripple the open-source tool to sell the paid one.** A discovered artificial limit is the fastest way to kill an infrastructure project. If a feature is in the CLI, it is complete in the CLI.
2. **Zero telemetry, ever, by default, stated loudly.** A CI tool that phones home fails security review. "CheckOwners sends nothing anywhere" is both a headline guarantee and a genuine differentiator against every hosted competitor.
3. **The commercial product consumes the open-source JSON contract unmodified.** If the paid product needs a schema change, ship it to open source first. That is what makes the schema credible as a standard — and a standard is worth more than any feature.
4. **Keep the license MIT.** A restrictive relicense at this adoption level would destroy the only asset the project has: the option value of being adopted.
5. **The privacy and anti-performance-metric work is not optional for this strategy.** The buyer is an engineering leader; the blocker is their security and legal review; "this tool generates per-developer productivity reports and caches employee emails in plaintext" is a lost deal. Fixing it in open source is what makes the commercial version sellable.
6. **The validation study is the moat, not a nice-to-have.** Anyone can ship a scoring heuristic. Almost nobody will prove theirs predicts anything. Publishing the study makes CheckOwners the *cited* implementation, and citation is what a category-defining tool has that a merely good tool does not.

---

## 26. Terminology Guardrails

**Never treat these as equivalent:** contribution and ownership; blame and authority; recent activity and expertise; co-commit clusters and teams; inferred reviewer affinity and organizational accountability; an uncalibrated score and a probability.

| Instead of | Use |
|---|---|
| organizational owner | repository-evidence candidate · reviewer candidate · contribution/review affinity · evidence-ranked reviewer |
| expertise decay | ownership freshness · evidence freshness · continuity risk |
| backup reviewer | candidate backup reviewer |
| bus factor (as currently computed) | qualified owner count · reviewer depth |
| confidence `0.92` (uncalibrated) | evidence score · ranking signal · review-affinity score · ownership score |
| knowledge graph (as currently modeled) | ownership graph — until the ontology is expanded |
| org chart | exploratory repository topology |
| review load (from commits) | git authorship proxy |
| historical confidence | historical activity confidence |

---

## 27. Product Behavior Guardrails and Project Principles

The safest default is advisory, not authoritative. The product should propose rather than silently enforce; explain rather than merely rank; display uncertainty; preserve manual overrides; make corrections durable; remain reversible; show missing evidence; show limitations; allow explicit exclusions; and separate inferred evidence from accepted policy. It must never silently convert repository activity into permanent organizational responsibility.

Put these principles in the repository:

1. **Evidence over guesses.** Every ownership recommendation must derive from inspectable evidence.
2. **Human policy remains authoritative.** Observed expertise informs CODEOWNERS; it does not silently override governance.
3. **Missing evidence is not negative evidence.** Unavailable review or team information must reduce evidence quality, not arbitrarily depress owner scores.
4. **Local-first.** Core analysis works without uploading proprietary source.
5. **Deterministic by default.** Same evidence plus same model equals same result.
6. **Explain everything.** Every score and recommendation is inspectable.
7. **Measure uncertainty.** Do not pretend thin evidence is certainty.
8. **Compatibility matters.** GitHub CODEOWNERS semantics must be reproduced accurately.
9. **No employee surveillance.** Optimize organizational resilience, not individual performance scoring.
10. **Correctness before feature count.** This should become the defining engineering philosophy.

---

## 28. What Not to Prioritize Yet

Before reviewer recommendations and ownership inference are trusted, do not make primary priorities of: generic codebase chat; broad documentation-health analysis; chat-platform Q&A; a generalized organization chart; a large LLM layer; agent knowledge-graph infrastructure; automatic responsibility rewrites; graph visualization as the primary product; dozens of integrations; sophisticated onboarding reports; broad "AI" positioning; "knowledge graph" positioning; "the first" as a marketing hook; or the full command list as the main pitch.

**Do not add LLMs merely because everything has LLMs.** The strongest attributes of this project are that it is deterministic, inspectable, local, reproducible, and evidence-driven. Sending git history to a model and asking who owns the code would destroy exactly that. LLMs may optionally help with human-readable report summaries, component naming, architecture descriptions, and onboarding explanations — but must never become required for ownership inference.

**A more legitimate future direction than generic LLM scoring is semantic code analysis:** build dependency relationships from imports, calls, packages, services, and build graphs, then propagate ownership carefully across architectural boundaries (Alice owns the interface and eight implementations → stronger domain expertise). Do this *after* history inference is excellent.

**Symbol-level ownership could become a killer advanced feature.** For a pull request touching lines 200–260 of one file, file-level ownership is too coarse. Use blame and ownership of the surrounding symbols and lines to identify the expert in the changed subsystem, falling back progressively through symbol → file → directory → service. CheckOwners need not generate symbol-level CODEOWNERS; it can simply recommend reviewers from symbol ownership, which no file-level rule can express.

---

## 29. Do Not Overfit to CODEOWNERS — but Keep It as the Wedge

The deeper asset is the **observed ownership graph**; CODEOWNERS is the compatibility and output layer. The product hierarchy should eventually be:

```text
Ownership Intelligence Engine
            ↓
      CODEOWNERS adapter
```

rather than:

```text
CODEOWNERS generator
       ↓
other random analytics
```

But CODEOWNERS remains the adoption wedge, because it gives developers an immediately understandable problem:

```text
"Is your CODEOWNERS wrong?" → checkowners audit → ownership risk → organization-wide intelligence
```

The defensible combination nobody else has assembled is: native CODEOWNERS semantics + historical ownership inference + confidence and evidence modeling + declared-versus-observed drift + knowledge-risk analysis + CI enforcement + local-first open source. Individual pieces exist elsewhere; the integrated closed loop is stronger:

```text
Observe → Infer → Explain → Compare → Detect drift/risk → Recommend → Update policy → Continuously verify
```

The naming is already good — `CheckOwners` immediately communicates "check ownership" and "CODEOWNERS." Do not rename the project. Just standardize casing: product **CheckOwners**, CLI and package and repository slug **`checkowners`**.

---

## 30. Roadmap

### 30.1 P0 — fix before adding any major feature

| # | Item | Impact | Effort |
|---|---|---|---|
| P0.1 | **Correct canonical project identity.** Rewrite every previous-organization reference in `pyproject.toml` URLs, README badge, `docs/USAGE.md` Actions example, `SECURITY.md` advisory link, `docs/CONTRIBUTING.md` clone and issues URLs, and all `docs/CHANGELOG.md` compare links. Add `Documentation` and `Changelog` project URLs. Ship a patch release so PyPI metadata is corrected on the live release. Add a CI grep job that fails the build on the old slug. Publish floating `v0` and `v0.5` tags. Document the migration in one README sentence | Trust and discoverability | Tiny |
| P0.2 | **Fix PyPI documentation links.** Absolute URLs in the README | Conversion | 15m |
| P0.3 | **Make the Action reproducible.** Action `vX.Y.Z` installs `checkowners==X.Y.Z`; never latest-by-default. Ship a hash-pinned requirements lock | Security and reliability | Tiny |
| P0.4 | **SHA-pin third-party actions** and add dependency automation | Security | 30m |
| P0.5 | **Wire the token.** `github_token` input plus `GITHUB_TOKEN` exported on every CLI step; correct the FAQ | Core feature works in CI | 1h |
| P0.6 | **Make PR commenting fail-soft**, add fork detection, add step summary | Stops guaranteed failures | 1h |
| P0.7 | **Random `GITHUB_OUTPUT` delimiter**, summaries in outputs, full payloads as artifacts | Correctness at scale | 2h |
| P0.8 | **Rename the bus factor** to `qualified_owner_count` and document the cap; add `top_owner_share`, `effective_owner_count`, `TF50/75/90` | Correctness and credibility | Small |
| P0.9 | **Normalize confidence over available signals**, and add `evidence_quality` plus per-signal availability | Correctness and reproducibility | Small |
| P0.10 | **Remove the hard sparse-file exclusion.** Replace `min_commits` eligibility with evidence uncertainty | Correctness | Small |
| P0.11 | **Team-aware drift.** Resolve `@org/team` against membership; emit `unverifiable` offline rather than silence | Enterprise usefulness | Medium |
| P0.12 | **Honor `.git-blame-ignore-revs`**, `-w`, `-M`, `-C`, and `.mailmap` | The single largest available accuracy improvement | Medium |
| P0.13 | **Rename lineage.** Track moved files across history | Monorepo correctness | Medium |
| P0.14 | **Add `checkowners explain`** | Trust and UX | Medium |
| P0.15 | **Introduce model and schema versioning** before algorithms keep changing | Future compatibility | Small |
| P0.16 | **Determinism.** `--as-of` plus `SOURCE_DATE_EPOCH`, defaulting to the HEAD commit timestamp | Stops flapping CI | Small |
| P0.17 | **Unify the drift-mode default**, document every environment variable, set `CHECKOWNERS_STATE_DIR` in the Action | Consistency | Small |
| P0.18 | **Kill the two-implementation split.** `action.yml` calls `checkowners github-action` once | Maintainability | Small |
| P0.19 | **Enforce the coverage floor** and add ruff `S` and `PTH` | Quality | 30m |
| P0.20 | **Rewrite the comparison table honestly** and drop the "first" claim | Credibility | 1h |
| P0.21 | **Correct the misdated changelog entries** and repopulate `Unreleased` | Maintenance signal | 15m |

**Do the identity fix, the PyPI links, the SHA pinning, the metric rename, and the comparison-table rewrite first.** They cost well under eight hours combined and move the project from "cannot be trusted" to "can be evaluated."

### 30.2 P1 — make the core world-class

- Round-trip verification of generated CODEOWNERS plus a hard size guard well under 3 MB.
- Ownership audit breadth: invalid owners, permission errors, duplicates, shadowing, unowned files, dead patterns, self-protection, ruleset audit.
- Hybrid CODEOWNERS generation: human overrides plus generated blocks.
- Risk model: hotspots, knowledge concentration, orphaned expertise, former-contributor risk.
- PR mode: `checkowners diff BASE...HEAD` and `--diff-only`.
- Baseline/ratchet mode plus suppressions with reasons and expiry.
- `.gitattributes` linguist-generated and linguist-vendored support.
- SARIF output, GitHub Checks, annotations, `$GITHUB_STEP_SUMMARY`.
- Exit-code contract and versioned JSON Schema per command.
- Privacy modes, `docs/PRIVACY.md`, `cache purge`, contributor exclusion.
- `docs/METHODOLOGY.md` with prior art, honest limits, and an explicit non-performance-tool statement.
- OpenSSF Scorecard workflow and badge.
- Property tests, real-git fixtures, and a public CODEOWNERS compatibility corpus.
- Self-contained interactive HTML report.
- Blob-based and incremental caching.
- `.pre-commit-hooks.yaml`, `uvx`/`pipx` docs, Docker image.
- Atomic state writes with advisory locking, `analyzed_ref` staleness contract, `--no-cache`, `--max-age`.
- `sync --pr` as the default.

### 30.3 P2 — become ownership infrastructure

Package and service-level ownership · distributed `OWNERS` files · service-catalog integration · organizational analysis · portfolio knowledge risk · a GitLab provider · richer graph exports · knowledge-transfer detection · ownership vacuums · review concentration · real community detection for topology · dependency-aware onboarding · standalone binaries · Homebrew · Nix · a `gh` extension.

### 30.4 P3 — ecosystem, only after significant adoption

GitHub App · optional hosted dashboard · organization-wide continuous indexing · IDE integrations · plugin ecosystem · symbol-level ownership · semantic dependency analysis.

### 30.5 Version milestones

| Version | Theme | Contents |
|---|---|---|
| `0.5.1` | **Unbreak** | Organization references fixed everywhere; PyPI links absolute; Action version pinned; third-party actions SHA-pinned; random output delimiter; fail-soft comment plus step summary; token wired; floating tags published; metric renamed with the cap documented; comparison table rewritten; `Unreleased` repopulated |
| `0.6.0` | **Correctness and trust** | Signal renormalization; evidence quality; `.git-blame-ignore-revs`, `-w`/`-M`/`-C`, `.mailmap`, `Co-authored-by`; `.gitattributes` linguist support; sparse-file fix; `--as-of` determinism plus reproducibility test; round-trip verification plus size guard; baseline ratchet; `.pre-commit-hooks.yaml`; exit codes and JSON Schema with `schema_version`; privacy modes plus `PRIVACY.md`; `METHODOLOGY.md`; Scorecard; property tests |
| `0.7.0` | **Ownership risk and scale** | Bus-factor v2, effective owners, knowledge concentration, hotspots, `risk` command, `simulate`; incremental analysis keyed on `analyzed_ref`; `--diff-only`; in-process or log-based blame; Action caching; reproducible benchmark harness; SARIF; Docker; docs site with a demo cast |
| `0.8.0` | **Audit and rigor** | `checkowners audit` as the front door; team-aware drift; validator parity and beyond; `explain`; the validation study across 20+ repositories with precision/recall/F1 by threshold, weight optimization, and calibration curves; real truck factor; conformance harness against GitHub's errors endpoint with a published pass rate; named topology algorithm with stability reporting |
| `0.9.0` | **Research and reporting** | Rename lineage; incremental cache; the ownership benchmark dataset; HTML reports; published performance benchmark |
| `1.0.0` | **The contract** | Frozen JSON Schema with a stability policy; documented semver guarantees for config, CLI, and output; second maintainer named in `GOVERNANCE.md`; `CITATION.cff`; published conformance corpus other tools can test against |
| Beyond `1.0` | **Standard-setting** | Multi-forge (GitLab sections and optional approvals, Bitbucket, Gitea/Forgejo, Gerrit and Chromium-style `OWNERS`); publish the *ownership inference spec* as a document separate from the implementation so other tools can implement it; publish the labeled corpus as a research dataset; write the paper |

Do not bind to this numbering if development reality differs; the sequence matters more than the exact labels.

### 30.6 The twelve-month phase view

```text
Phase 1 — Correctness   confidence v2 · evidence quality · bus-factor replacement ·
                        team-aware drift · rename lineage · ignore-revs ·
                        sparse-file handling · model/schema versioning
Phase 2 — Audit         checkowners audit · validator expansion · explain ·
                        policy engine · baseline · SARIF · GitHub Checks
Phase 3 — Proof         ownership benchmark · performance benchmark ·
                        50 open-source reports · maintainer validation ·
                        false-positive ledger
Phase 4 — UX            HTML report · pre-commit · binaries · Homebrew ·
                        Docker · incremental analysis
Phase 5 — Organization  components · distributed ownership · GitHub teams ·
                        service catalog · former contributors · knowledge transfers
Phase 6 — Ecosystem     GitLab · organization-wide analysis · GitHub App ·
                        plugins · symbol ownership
```

### 30.7 The alternative product-first sequencing

If the reviewer-routing framing is chosen as the primary wedge rather than reconciliation, the phases become:

**Phase 1 — Own the PR-review wedge.** Analyze repository evidence; inspect changed PR files; recommend reviewers; explain evidence and uncertainty; flag stale ownership; flag single-reviewer risk; respect exclusions; respect declared teams; let humans accept, edit, or dismiss; learn from those decisions; create a reviewable CODEOWNERS proposal; never silently rewrite organizational history.

**Phase 2 — Build the correction-data moat.** The highest-value proprietary asset is not git history, which everyone has. It is **high-quality human corrections tied to real outcomes**: recommendation correct; recommendation wrong; right person but unavailable; team owns it; security owner required; compliance owner required; reflects temporary migration ownership; based on a departed contributor; useful but incomplete; no ownership for this path; the proposed person should not own this area. Correction data creates labeled evidence that improves ranking and calibration. The feedback system must preserve both the repository evidence and the correction — never silently rewrite historical facts.

**Phase 3 — Add evidence incrementally.** Candidates: pull-request reviews, CODEOWNERS history, incidents, on-call systems, service catalogs, architecture decisions, documentation, team directories, deployments, alerts. The rule for adding a source is: **add it only when it changes a decision that git alone gets wrong.** Do not begin with a giant knowledge graph. Do not begin with a broad LLM layer. Let the evidence architecture grow in response to measurable decision failures.

**Phase 4 — Run serious pilots.** Target organizations of roughly 100–2,000 engineers with large, aging repositories, monorepos, connected repositories, frequent team movement, expensive review delays, and meaningful operational, incident, or compliance exposure. Measure reviewer precision, acceptance rate, time to useful review, reviewer-load concentration, stale rules removed, handoff time, risky changes identified, risks found, incidents where ownership evidence helped, and false positives. The purpose is not to prove the tool runs; it is to prove it improves engineering outcomes.

### 30.8 The recommended first-five-minutes experience

```bash
pip install checkowners      # or: uvx checkowners audit
checkowners init             # initialize configuration
checkowners analyze --json   # analyze repository evidence, machine-readable
checkowners explain path/to/file.py   # "why this person?"
checkowners pr 123           # "who should review this change?"
```

Each command communicates a distinct job. This interaction is worth more than another generic report.

### 30.9 What `1.0` should mean

Do not publish `1.0` because the feature list feels complete. Release it only when:

- **Correctness:** the CODEOWNERS compatibility corpus is excellent; rename-aware history exists; ignored revisions are supported; team drift works; sparse-file inference is fixed.
- **Metrics:** confidence semantics are stable; evidence quality exists; the bus-factor methodology is replaced; model versioning exists.
- **API:** the JSON schema is stable; the configuration schema is stable; exit-code behavior is documented.
- **CI:** the Action is reproducible; dependencies are controlled; changed-files mode is usable.
- **Trust:** a security policy, provenance, an SBOM, a benchmark, and published limitations exist.
- **Adoption:** multiple external production users, real issue and pull-request activity, and several outside contributors. No star threshold is necessary; actual usage matters more.

Define breaking changes explicitly before then — CLI command removal, JSON schema changes, scoring-model changes, default policy changes, Action input/output changes, configuration schema changes — and document that `0.x` is evolving while `1.x` carries stable contracts. Do not rush `1.0`.

---

## 31. Go-to-Market

1. **Fix the identity rot first.** Do not launch anything while the documentation points at a different organization.
2. **Add visual proof to the README:** one terminal recording of `analyze` → `doctor` → `simulate`, one screenshot of the PR comment, one sample generated CODEOWNERS.
3. **Ship `doctor`.** It is the demo, the wedge, and the piece that works without trusting the inference.
4. **Publish the ownership-risk dataset.** This *is* the launch.
5. **Write the definitive "CODEOWNERS fails silently" article** covering write access, empty teams, last-match-wins shadowing, the 3 MB limit, base-branch placement, and invalid-line skipping — with `checkowners doctor` as the remedy at the end. That query has durable search demand and no canonical answer.
6. **Then launch publicly, with the dataset as the lede and the tool as the implication.** "We measured the bus factor of the top 250 Python packages" is a front-page post. "I built a CODEOWNERS tool" is not.
7. **Run it against 50 prominent open-source repositories** and publish "50 Open-Source CODEOWNERS Audits" — ownership coverage, drift, knowledge concentration, one interesting finding, and an interactive report for each. Contact maintainers only when a result is genuinely interesting. This could create more organic adoption than months of feature development.
8. **Recruit design partners:** five to ten serious open-source maintainers, five engineering teams, two large monorepos. Offer to personally analyze their repository and fix every false positive. Ship their sanitized configurations as `examples/`. Their first three bug reports are worth more than the next twenty features.
9. **Track every disagreement.** The most valuable dataset available right now is: *tool says Alice, maintainer says Bob, why?* Every disagreement improves the ownership model.
10. **Seek testimonials, not vanity stars.** The valuable quote is not "cool tool" but *"CheckOwners found six services whose CODEOWNERS entries still pointed at a team that had stopped maintaining them nine months earlier."*
11. **List everywhere** — curated Actions, DevOps, Python, static-analysis, and engineering-management lists. Free, permanent, compounding.
12. **Open the community surface:** Discussions, issue and PR templates, `ROADMAP.md`, eight labeled good-first-issues starting with the P0 documentation fixes and the `who` command, `FUNDING.yml`.
13. **Solve your own bus factor.** A bus-factor tool maintained by one person is a permanent objection. Recruit a co-maintainer with commit rights before asking any company to depend on this. If the tool cannot persuade its own author to raise its truck factor above one, the pitch does not work.
14. **Be explicit about open-core intent** before someone asks.

---

## 32. Metrics That Would Prove It Worked

### 32.1 Adoption and trust metrics

| Metric | Why | Target at 6 months |
|---|---|---|
| Repositories with `.github/checkowners.yml` (code search) | The only true adoption signal | 100 |
| Weekly PyPI downloads excluding CI mirrors | Real installs | 2,000 |
| Marketplace Action installs | CI adoption | 50 |
| Median PR-run wall time | Slow checks get deleted | under 30s |
| Ratio of Action runs that comment versus fail | Alert-fatigue proxy | above 4:1 |
| Externally filed issues | Proof someone is actually using it | 25 |
| External contributors with merged PRs | Project bus factor | 3 |
| Time to first maintainer response | The strongest open-source trust signal | under 48h |
| Calibration precision@3 on the benchmark corpus | Whether the core claim is true | above 0.7 |

### 32.2 Horizon targets

| Horizon | Metric | Target |
|---|---|---|
| 30 days | PyPI monthly downloads | 500 |
| 30 days | GitHub stars | 100 |
| 30 days | Issues opened by non-maintainers | 5 |
| 90 days | Repositories using the Action | 25 |
| 90 days | External contributors with a merged PR | 3 |
| 90 days | OpenSSF Scorecard | ≥ 7.0 |
| 90 days | Pre-commit hook adoption | 50 repositories |
| 180 days | PyPI monthly downloads | 10,000 |
| 180 days | Second maintainer with release rights | 1 |
| 180 days | Public validation study published | 1 |
| 180 days | An independent blog post or conference talk not by the author | 1 |
| 365 days | A tool other than CheckOwners implementing the published spec | 1 |

The last metric is the only one that means category leadership. The rest are inputs.

---

## 33. The Single Best Move in Each Dimension

| Dimension | The single best move |
|---|---|
| **Product iteration** | **Ownership Audit v2**: confidence normalization, evidence quality, team-aware drift, rename-aware history, blame-ignore-revs, sparse/new-file ownership, true knowledge-concentration metrics, explainability, a richer validator, and a reproducible Action — then expose `checkowners audit` as the default |
| **Distribution** | Run Audit v2 against 50 prominent open-source repositories and publish all 50 audits |
| **Credibility** | Publish the ownership-inference benchmark comparing commit count, blame, recency, frequency, and CheckOwners against future maintenance and review ground truth, with open dataset generation, harness, scores, and methodology |
| **Community** | Open 20–30 carefully scoped roadmap issues with clear labels so outsiders have obvious entry points |
| **Security** | Fix "Action pinned → latest PyPI package" immediately, then publish a concise threat model and security policy |
| **Algorithm** | Stop treating a count of qualifying owners as bus factor; implement knowledge shares, effective ownership, and knowledge-loss truck factor, and build the rest of risk analysis on those primitives |
| **UX** | Build `checkowners explain PATH` — trust grows when developers can interrogate the model |
| **Enterprise** | Team-aware reconciliation: GitHub team ↔ team membership ↔ inferred experts ↔ declared CODEOWNERS |
| **Performance** | Cache by blob SHA plus HEAD lineage and support incremental analysis |
| **Documentation** | Replace the feature enumeration at the top of the README with one concrete CODEOWNERS-drift example |
| **Research** | Calibrate against *future behavior*, not the existing CODEOWNERS file, which is precisely the artifact the tool exists to distrust |
| **Design principle** | **Observed expertise is evidence, not authority** |

---

## 34. Consolidated Priority Order

1. Fix the canonical identity everywhere and ship the patch release.
2. Make the Action reproducible, token-wired, fork-safe, and fail-soft.
3. Rename the bus-factor metric honestly and implement real knowledge-concentration primitives.
4. Normalize confidence over available signals and introduce evidence quality.
5. Make analysis deterministic and add a baseline ratchet.
6. Fix blame fidelity: ignore-revs, `-w`/`-M`/`-C`, mailmap, co-authors, rename lineage.
7. Replace the `min_commits` eligibility cutoff with evidence uncertainty.
8. Make drift team-aware, with an explicit `unverifiable` state.
9. Ship `explain`, then `doctor`, then `audit` as the default front door.
10. Introduce model and schema versioning, exit codes, and versioned JSON Schemas before ecosystem adoption.
11. Verify generated CODEOWNERS round-trip and guard the size ceiling.
12. Publish `METHODOLOGY.md`, `PRIVACY.md`, and `limitations.md`; add anonymization and aggregate-only modes.
13. Rewrite the comparison table honestly and drop the "first" claim.
14. Replace mocked-only tests with real-git fixtures, a compatibility corpus, property tests, and mutation testing.
15. Capture human corrections and outcomes as ground truth; make manual overrides durable and auditable.
16. Publish labeled evaluations, failure examples, and a reproducible performance benchmark.
17. Ship pre-commit, `uvx` docs, Docker, binaries, and a docs site with a demo.
18. Measure business outcomes in serious pilots.
19. Add external evidence one source at a time, only when it corrects a known git-only failure.
20. Keep positioning on the operational failure prevented, not on implementation buzzwords.
21. Do not expand into generic chat, broad knowledge graphs, large LLM layers, or dozens of integrations before ownership inference and reviewer routing are trusted.

---

## 35. Preserved Discrepancies

Where independent assessment passes disagreed, both readings are retained rather than adjudicated:

| Topic | Reading A | Reading B | Treatment |
|---|---|---|---|
| Evidence access | Contributing, agent-instruction, and security files were read directly | Those artifacts and the source tree were not retrievable | Both caveats stand within their own analysis; findings marked as inferred are still worth implementing as explicit documented guarantees, because an undocumented behavior is not a contract |
| CLI command count | 16 subcommands | 15 commands plus `github-action`, effectively 16 | A counting convention, not a substantive difference; the enumerated set is identical |
| Release dating | Separates changelog dates from PyPI dates and flags two mismatches | Uses PyPI dates throughout | Both representations retained |
| Organization-move severity | Redirects make old links resolve today but will break later | The documented Action invocation is effectively broken on arrival, since Actions do not follow renames | Both framings retained; both lead to the same fix |
| Benchmark wording | README says under two minutes, changelog says under three | Cites under three minutes and separately calls the benchmark unreproducible | Both retained; the discrepancy is itself the finding |
| Overall grade framing | "Strong B- artifact, F- project" | "Overall C+; a B+ engine wrapped in a D- product through an F- go-to-market" | Both retained; directionally consistent, different aggregation |
| Silence since last release | "Roughly eight weeks" | "56 days" | Both retained; the latter is the more explicit calculation |
| Confidence renormalization | Flagged as needing verification in source | Asserted as a real scale defect | **Resolved by inspection of the current code: `analyze._score_owners` performs no renormalization, so the offline ceiling is 0.85. The defect is real** |
| Blame flag corrections | Flagged as needing verification in source | Asserted as absent | **Resolved by inspection: `analyze._blame_for_path` runs `git blame --line-porcelain` with no `-w`, `-M`, `-C`, `--ignore-revs-file`, or `--use-mailmap`. The defect is real** |
| `py.typed` presence | Flagged as possibly missing despite the `Typing :: Typed` classifier | — | **Resolved: packaging work shipped `py.typed`; the classifier is accurate** |

---

## 36. External Reference Index

Repository, package, and Action surfaces:

- `github.com/smusali/checkowners`
- `github.com/smusali/checkowners/blob/main/pyproject.toml`
- `github.com/smusali/checkowners/blob/main/action.yml`
- `github.com/smusali/checkowners/blob/main/docs/CHANGELOG.md`
- `github.com/smusali/checkowners/blob/main/docs/USAGE.md`
- `github.com/smusali/checkowners/blob/main/docs/FAQ.md`
- `github.com/smusali/checkowners/blob/main/checkowners/analyze.py`
- `github.com/smusali/checkowners/blob/main/checkowners/busfactor.py`
- `github.com/smusali/checkowners/blob/main/checkowners/topology.py`
- `github.com/smusali/checkowners/blob/main/checkowners/decay.py`
- `github.com/smusali/checkowners/blob/main/checkowners/balance.py`
- `github.com/smusali/checkowners/blob/main/checkowners/trends.py`
- `github.com/smusali/checkowners/blob/main/checkowners/state.py`
- `github.com/smusali/checkowners/blob/main/checkowners/github.py`
- `github.com/smusali/checkowners/blob/main/checkowners/generate.py`
- `github.com/smusali/checkowners/blob/main/checkowners/validate.py`
- `github.com/smusali/checkowners/blob/main/checkowners/cli.py`
- `github.com/marketplace/actions/checkowners`
- `pypi.org/project/checkowners`

Platform documentation:

- GitHub: about code owners (CODEOWNERS syntax, matching semantics, access requirements, the 3 MB limit, silent skipping of invalid lines, self-ownership guidance)
- GitHub REST API: repositories and CODEOWNERS syntax errors
- `.git-blame-ignore-revs` and `blame.ignoreRevsFile`
- `.mailmap`, `git log --use-mailmap`, `git blame --use-mailmap`
- `.gitattributes` `linguist-generated` / `linguist-vendored`
- GitHub Actions: multi-line `GITHUB_OUTPUT` delimiters, output size limits, fork-PR token permissions, `$GITHUB_STEP_SUMMARY`, workflow annotations, SARIF upload

Adjacent tools and prior art:

- CODEOWNERS validators and Action-packaged validators (owner validity, duplicate patterns, file existence, not-owned checks, distinct exit codes)
- CODEOWNERS generators and monorepo compilers (distributed declarations, `check` modes, manifest-derived maintainers)
- Git-history CODEOWNERS inference tools with lookback, minimum-commit, and maximum-committer thresholds
- Ownership-audit CLIs (ownership stats, unowned files, CSV/JSONL output)
- GitHub-endpoint CODEOWNERS linters
- Reference CODEOWNERS parsers used in academic work
- `git-fame`-style contribution statistics
- Bus/truck-factor research tooling and explorers, including treemap visualization and departure simulation
- Truck-factor estimation from commit history, and ownership-concentration research on software quality
- Degree-of-knowledge models combining authorship and interaction data
- Knowledge-islands style truck-factor computation across folders and files
- Commercial behavioral code analysis with knowledge-distribution and offboarding-risk reporting
- Service catalogs and well-known ownership relations
- Practitioner guides to GitHub CODEOWNERS
- Preprint literature on ownership inference and bus-factor estimation

---

## 37. Final Judgment

**CheckOwners is a much better idea than its current public footprint makes it look, and it is a genuinely well-engineered piece of software attached to a project that does not yet exist publicly.**

The project has already crossed from *simple CODEOWNERS generator* into *early ownership-intelligence engine*. Its strongest existing characteristics are: the correct underlying problem, because CODEOWNERS becomes stale exactly where organizational declarations and actual engineering knowledge diverge; a genuinely differentiated observed-versus-declared ownership model; multi-signal inference instead of raw commit counts; GitHub-compatible drift analysis; local-first, pure-git, no-LLM operation; a useful CI integration; identity and team awareness; thoughtful safety around generated files; good modular Python engineering and broad test structure; and rapid iteration driven by a real production monorepo.

Its biggest weaknesses are equally clear: the bus-factor calculation is far weaker than its name implies; confidence scores are not comparable across evidence-availability modes; the minimum-commit qualification erases legitimate owners; team ownership remains a major drift blind spot; rename and move history needs real lineage handling; raw blame is vulnerable to formatting and refactoring noise; the review-load fallback conflates authorship with reviewing; topology uses an overly simplistic clustering model; historical trends use a different evidence model from present-time confidence; several advanced features sound more sophisticated than their underlying algorithms; the Action's default latest-package installation destroys reproducibility and never wires the token it needs; project provenance and naming are fragmented; the validator is not yet comprehensive; scientific calibration is largely absent; and public adoption evidence is effectively nil.

**The primary failure mode is false authority.** If teams interpret repository activity as organizational truth, obvious recommendation errors will erode confidence rapidly, and additional reports will only make a wrong answer look sophisticated. The system must therefore remain humble, inspectable, correction-driven, evidence-first, explicit about uncertainty, and human-overridable.

The most instructive fact in this entire assessment: the project ships a bus factor of one, an unenforced coverage target, and a comparison table that omits its real competitors, while selling a tool that detects exactly those categories of risk in other people's repositories. Fix that irony first; much else follows from it.

The tool today answers *"what should the CODEOWNERS file say."* The great version answers **"is anyone actually reviewing this code, and what breaks when they leave."** The distance between those two questions is roughly one quarter of focused work, most of it deletion and validation rather than construction. And roughly forty hours of unglamorous work separates the project from credible; roughly four hundred separate it from being the standard.

Center the entire project on **declared ownership versus observed expertise**, and build outward:

```text
                    CheckOwners
                         │
                         ▼
               Ownership Evidence
                         │
                         ▼
              Observed Expertise
                         │
             ┌───────────┼───────────┐
             ▼           ▼           ▼
        CODEOWNERS    Knowledge    Reviewer
          Drift          Risk       Routing
             │           │           │
             └───────────┼───────────┘
                         ▼
                 Engineering Teams
                         │
                         ▼
                   Organization
```

Executed well, the goal should not be *a good CODEOWNERS utility*. It should be:

> **the canonical open-source engine for understanding, auditing, and continuously reconciling software ownership and engineering knowledge.**

That is a significantly larger and significantly more defensible category.

**The one-sentence thesis:**

> **CheckOwners should become the local-first, evidence-driven system that keeps pull-request review ownership current, recommends the right reviewers with explainable uncertainty, detects stale or concentrated ownership early, reconciles declared policy with observed expertise, and learns from human corrections — without ever turning git activity into false organizational authority.**
