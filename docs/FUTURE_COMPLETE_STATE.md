# checkOwners — Future Complete State

The system as it exists once every tracked action item has landed. This is the target specification:
what the engine computes, what it guarantees, what it refuses to claim, how it is distributed, and
what proof stands behind it. Written in the present tense as a description of the completed system.

---

## 1. What checkOwners Is

**The open-source ownership intelligence engine that continuously reconciles who is supposed to own
code with who actually knows it.**

CODEOWNERS is a static file describing a dynamic property. Teams move, people leave, code is
inherited and rewritten, and the file drifts away from reality without anyone noticing — until a
high-risk pull request routes to someone who left the team nine months ago, or to one overloaded
maintainer, or to nobody who can actually approve it.

checkOwners analyzes git and review history to infer who actually knows each part of a repository,
compares that evidence against declared policy, exposes continuity risk before it becomes an
incident, and shows the evidence behind every recommendation.

The one product decision it is excellent at:

> **Who should review this change, and how much should we trust that recommendation?**

The one governing principle that resolves every downstream design question:

> **Observed expertise is evidence, not authority.**

---

## 2. Architecture

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

The deeper asset is the **observed ownership graph**. CODEOWNERS is the compatibility and output
layer:

```text
Ownership Intelligence Engine
            ↓
      CODEOWNERS adapter
```

### 2.1 The evidence layer

Observations are stored, not just scores:

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

Scoring is an interpretation layer over evidence. This enables new models without re-extracting
history, richer explanations, model benchmarking, historical reproducibility, and plugins.

### 2.2 Provider interfaces

```python
class EvidenceProvider:
    def collect(...) -> Iterable[OwnershipEvidence]: ...
```

`GitCommitProvider` · `GitBlameProvider` · `GitHubReviewProvider` · `GitHubTeamProvider` ·
`GitLabMergeRequestProvider` · `BackstageProvider` · `ManualOwnershipProvider`

No GitHub-specific conditional remains inside core ownership logic. GitHub is the best-supported
provider, not the architecture.

### 2.3 Distinct ontologies, never collapsed

```text
DeclaredOwner       who the organization says is responsible
ObservedExpert      who evidence suggests understands the implementation
ReviewCandidate     who should review this particular change
TeamMember          who belongs to the declared team
HistoricalExpert    who understood it before
CriticalMaintainer  whose loss creates continuity risk
```

Related, not identical. Modeling them separately prevents an entire class of conceptual error.

---

## 3. The Ownership Model

### 3.1 Scoring

For contributor `u` and path `p`, over **available** signals only:

```text
C(u,p) = Σ wᵢ aᵢ Sᵢ(u,p) / Σ wᵢ aᵢ
```

```text
R = recency                       F = meaningful change frequency
B = blame / current authorship    V = review participation
H = historical depth              S = change survival
M = maintainer continuity
```

The attainable range is `[0, 1]` in **every** configuration. The same checkout produces the same
classification with or without a token; only evidence quality differs.

### 3.2 Evidence quality, computed separately

```text
Q(u,p) = Σ(wᵢ × aᵢ × reliabilityᵢ) / Σ(wᵢ)
```

derived from amount of history, age of path, number of observations, API completeness, identity
certainty, blame reliability, and rename confidence.

```json
{
  "owner": "@alice",
  "ownership_score": 0.84,
  "evidence_quality": 0.71,
  "signals": {
    "recency":   { "score": 0.91, "available": true },
    "frequency": { "score": 0.77, "available": true },
    "blame":     { "score": 0.86, "available": true },
    "review":    { "available": false }
  }
}
```

`@alice` at 0.91/0.93 and `@bob` at 0.79/0.31 are meaningfully different claims. Only this split can
express that.

**Missing evidence is not negative evidence.** Unavailable review or team information reduces
evidence quality; it never depresses a person's score.

### 3.3 Qualification by evidence, not eligibility

Commit count is evidence, not a gate:

```text
1 commit  + 95% blame + recent creation  → strong ownership, medium evidence quality
1 commit  + 3% blame                     → weak ownership
20 commits + 55% blame + recent reviews  → very strong ownership
```

Bayesian shrinkage replaces the hard cutoff, and an absolute-evidence dampener prevents a
three-commit sole contributor from scoring 1.0 on frequency. New files, young repositories,
squash-merge workflows, large feature commits, and low-churn infrastructure all produce owners.

### 3.4 Determinism

```python
def resolve_as_of(cli_value: str | None, repo: Repo) -> datetime:
    if cli_value:
        return datetime.fromisoformat(cli_value)
    if (epoch := os.environ.get("SOURCE_DATE_EPOCH")):
        return datetime.fromtimestamp(int(epoch), tz=UTC)
    return repo.head_commit_datetime()   # not datetime.now()
```

Analysis is a pure function of `(repo, commit, config, model)`. `--as-of` and `SOURCE_DATE_EPOCH` are
honored; `analysis_ref` and `analysis_epoch` appear in every payload; output is byte-stable across
runs, clock offsets, and worker counts. Drift carries hysteresis so severity does not flip on noise.

A pull request does not go red on Tuesday and green on Wednesday with no code change.

### 3.5 Adaptive time horizons

Recency is measured relative to each path's own change cadence, bounded by a configurable floor and
ceiling. A file whose median inter-commit interval is 400 days is not penalized for a 300-day gap.
High-churn paths decay quickly; stable paths do not.

Three concepts are separated:

```text
active_expertise      recent, demonstrable involvement
historical_expertise  depth of past involvement not yet superseded
maintenance_recency   how recently the path itself changed at all
```

Decay classifies its reason — `inactive`, `superseded`, `stable`, `departed` — rather than emitting a
boolean past a day threshold.

### 3.6 Change survival

For contributor `u` and path `p`, the fraction of `u`'s historically authored lines surviving in the
current implementation, computed across the path's rename lineage. This bridges historical commits
and current blame: expertise in code that still exists counts; expertise in code that was rewritten
by others counts less. It is what makes `superseded` a computed state rather than an assumption.

---

## 4. Git Evidence Fidelity

```python
BLAME_ARGS = [
    "blame", "--line-porcelain",
    "-M",              # moved lines within a file
    "-C",              # copied lines across files
    "-w",              # ignore whitespace-only changes
    "--use-mailmap",
]
if (revs := config.git.blame_ignore_revs_file) and Path(revs).exists():
    BLAME_ARGS += [f"--ignore-revs-file={revs}"]

LOG_ARGS = ["log", "--use-mailmap", "--no-merges", "--numstat", "-M", "-C"]
```

- **`.git-blame-ignore-revs`** is auto-detected; `blame.ignoreRevsFile` is honored. A repository-wide
  formatting sweep does not transfer ownership to whoever ran it. Mass-refactor commits are
  down-weighted.
- **`.mailmap`** is the first stage of identity resolution — free, offline, token-free,
  rate-limit-free, and already maintained by most serious repositories. Maintaining it is documented
  as the cheapest way to improve accuracy.
- **Rename and copy lineage** gives each file a logical identity that survives refactors:

```json
{
  "logical_file_id": "…",
  "current_path": "services/payments/billing.py",
  "history": ["src/api/billing.py", "src/billing.py"]
}
```

  A directory rename does not reset ownership for its contents. Rename confidence feeds evidence
  quality.
- **`Co-authored-by:` trailers** are parsed and credited; merge strategy is detected and documented
  per strategy.
- **`.gitattributes`** `linguist-generated` and `linguist-vendored` drive generated- and
  vendored-file exclusion, with the static list as fallback. Exclusion counts are reported by
  mechanism.

---

## 5. Identity

Resolution order: `.mailmap` → local noreply parsing → per-repository disk cache → commit-based
`author.login` lookup → search API last.

```text
ContributorIdentity
├── GitHub handle
├── emails[]
├── names[]
└── aliases[]
```

```yaml
identities:
  "@alice":
    emails: [alice@gmail.com, alice@company.com]
```

Safeguards: cache namespaced per repository or organization; TTL on negative entries; explicit alias
and service-account handling; merge **and unmerge** audit trails; a dry-run identity report; a
**"do not merge"** override; redaction and hashing; an API-free mode; documented data flow for every
API lookup. `checkowners identity audit` surfaces probable duplicates **without silently merging
uncertain identities**. Identity resolution is auditable and reversible; identity certainty feeds
evidence quality.

Contributor status is modeled explicitly — `active`, `inactive`, `unknown`, `former`, `bot`,
`external` — sourced from configuration roster, then organization membership, then inference, then
honest `unknown`. This makes the highest-value organizational question answerable:

```text
payments/legacy/ — 84% of knowledge attributed to former contributors
```

Bot detection covers account type, known automation addresses, dependency and release bots,
configured identities, and commit signatures, plus mass-automation detection for human-looking
service accounts.

---

## 6. Risk Metrics

### 6.1 Knowledge concentration

Computed over **all** contributors above threshold, before any display truncation.

```python
def knowledge_shares(expertise): ...          # pᵢ = expertiseᵢ / Σ expertise
def effective_owners(shares): ...             # N_eff = 1 / Σ pᵢ²
def truck_factor(shares, q): ...              # smallest k removing ≥ q of knowledge
```

```text
100/0/0/0    → 1.00 effective owners
50/50        → 2.00
25/25/25/25  → 4.00
```

Emitted: `top_owner_share`, `effective_owners`, `truck_factor_50/75/90`, Shannon entropy, HHI,
minor-contributor share, major-contributor count. With a dominant owner at 82%, `TF50 = 1` —
correctly identifying concentration that a count misses.

The legacy count survives, honestly named `qualified_owner_count`, with its cap stated in every
output.

### 6.2 Repository risk is a distribution, never a mean

`minimum`, `p10`, `median`, `p90`, `critical-path risk`, `knowledge-at-risk %`, weighted by
criticality:

```yaml
criticality:
  "payments/**": 1.0
  "auth/**": 1.0
  "analytics/**": 0.6
  "docs/**": 0.1
```

with partial criticality inferred from dependency centrality, production manifests, and deployment
configuration. A repository whose only risky paths are two critical files reports high risk despite a
favorable unweighted mean. When criticality data is unavailable, the output says so — risk without
criticality is incomplete.

### 6.3 Composite hotspot risk

```text
risk = ownership_concentration × change_frequency × dependency_criticality
     × code_criticality × expertise_decay
```

| Path | Risk | Reason |
|---|---:|---|
| `payments/core.py` | 96 | single expert + high churn |
| `auth/token.py` | 88 | single expert + dependency criticality |
| `legacy/report.py` | 42 | stale expertise |
| `docs/foo.md` | 5 | low criticality |

A single-expert file that changes thirty times a week is urgent; one that changes every three years
is merely risky. The model tells them apart.

### 6.4 Named ownership pathologies

| Pathology | Definition |
|---|---|
| **Knowledge vacuum** | No contributor has sufficiently strong, current, reliable expertise — `legacy/auth/`, last meaningful expert activity 742 days ago, highest score 0.21 |
| **Phantom ownership** | CODEOWNERS names someone who has never touched or reviewed the component — flagged as *declared ownership without observed expertise*, surfaced for review rather than declared wrong |
| **Shadow maintainer** | Declared `@platform`, dominant observed expert `@bob` from `@payments` |
| **Review monopoly** | One developer performs 72% of reviews despite multiple knowledgeable owners — `review_concentration`, distinct from ownership concentration |
| **Ownership/review divergence** | Alice writes, Bob reviews everything, Carol is declared owner — three roles shown, never compressed into one score |

### 6.5 Departure simulation

```text
$ checkowners simulate --remove @alice --remove @bob

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

Backups are labeled **candidate** backup reviewers. The evidence does not support the stronger claim.

---

## 7. Drift and Reconciliation

### 7.1 Team-aware, with an explicit third state

```yaml
# inferred {@alice, @bob} ⊆ declared {@alice, @bob, @carol}
status: aligned
team_coverage: 1.0
inactive_members: ["@carol"]
```

```yaml
# inferred {@alice, @dave}, declared {@alice, @bob, @carol}
status: partial-drift
observed_outside_team: ["@dave"]
```

```yaml
status: unverifiable
reason: team_membership_requires_github_api
```

**"Could not verify" is never reported as "no drift."** Invisible teams, empty teams, and nested
teams each carry a distinct documented status. Team snapshots are timestamped so reports remain
interpretable months later.

### 7.2 Severity and direction

Severity is composite — identity overlap, team overlap, confidence gap, criticality, activity —
tiered `INFO / LOW / MEDIUM / HIGH / CRITICAL`. Declared `{Alice, Bob}` versus observed
`{Alice, Bob, Carol}` scores lower than versus `{Dave, Erin}` at the same delta. A solo-maintainer
repository is not permanently critical.

| Direction | Meaning |
|---|---|
| `missing_observed_expert` | CODEOWNERS lacks a major current expert |
| `stale_declared_owner` | CODEOWNERS includes someone with decayed expertise |
| `complete_ownership_replacement` | Observed experts no longer overlap declared ownership |
| `organizational_mismatch` | The observed expert belongs to a different team |

```json
{
  "path": "services/payments/**",
  "declared": { "owners": ["@company/platform"] },
  "observed": { "owners": ["@alice", "@bob"], "teams": ["@company/payments"] },
  "drift": { "severity": "high", "type": "organizational_mismatch",
             "declared_observed_overlap": 0.0 },
  "recommendation": { "action": "review_codeowners_rule",
                      "suggested_team": "@company/payments" }
}
```

### 7.3 Generation that cannot silently un-own code

- **Round-trip verification is mandatory.** After writing, every tracked path is re-resolved against
  the generated file using the project's own matcher; any divergence from the inference fails
  generation, naming path, intended owners, resolved owners, and the winning rule.
- **Size is guarded.** Warn at 2 MB, refuse above `output.max_bytes` without `--force`. GitHub does
  not load a CODEOWNERS file over 3 MB at all, silently — that cliff is never reached by accident.
- **Lossy consolidation is refused:**

```text
WARNING: GitHub CODEOWNERS cannot precisely represent: routes/[id]/
Generated fallback: routes/*/
This also matches: routes/[slug]/, routes/static/
Affected owners differ. Refusing automatic consolidation.
```

  `--allow-broad-patterns` permits it explicitly; owner-preserving broadening proceeds and is
  recorded.

### 7.4 Hybrid CODEOWNERS

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

Human intent **plus** machine inference. Regeneration preserves manual blocks byte-for-byte. Manual
rules that diverge from observed expertise are surfaced as deliberate policy decisions, not defects.
`security/` may belong to `@security-team` even if history says one platform engineer wrote most of
it — the discrepancy is explained, never erased.

### 7.5 Reconciliation by pull request

`sync` opens `chore: reconcile CODEOWNERS ownership drift` with per-rule rationale, scores, evidence,
severity, direction, and an explicit statement that inferred ownership is evidence, not
accountability. An existing open reconciliation pull request is updated rather than duplicated.
Direct commit is the escape hatch, not the default.

```diff
$ checkowners audit --fix-plan

-/payments/** @platform
+/payments/** @payments
```

Suggestions are proposed, never applied silently.

---

## 8. Command Surface

```text
Core        checkowners audit · owners PATH · explain PATH · generate · diff REV
Risk        checkowners risk · simulate
Reporting   checkowners report · coverage
Support     checkowners doctor · cache · baseline

Advanced / compatibility
            analyze · drift · bus-factor · decay · graph · topology ·
            balance · onboard · trends · validate · sync · github-action
```

### 8.1 `checkowners audit` — the default front door

Bare `checkowners` produces the repository audit. It is the strongest CODEOWNERS auditor available,
covering: syntax; invalid owners; permissions; invisible teams; unowned files; observationally
unowned files; duplicate rules; shadowed rules; dead patterns; excessively broad rules;
self-protection; ruleset audit; drift; concentration; decayed owners; orphaned ownership.

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

### 8.2 `checkowners doctor` — configuration against reality

```text
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

Plus environment verification:

```text
Git repository ✓   Full history ✓   GitHub token ✓   Token permissions ✓
CODEOWNERS found ✓ .github/CODEOWNERS      GitHub API ✓   Team visibility ✓
Review-history access ✓   .git-blame-ignore-revs ✓   Cache ✓
Model ownership-v2   Package 1.0.0   Config valid
```

This requires no inference at all, so it works on day one for teams that do not yet trust the
scoring. **Land on verification, expand into inference.**

### 8.3 `checkowners explain` — every inference is interrogable

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

```text
$ checkowners explain path/to/file.py --why-not @carol

@carol was not inferred because:
- last contribution: 418 days ago
- 3% current blame
- no reviews in observation window
- confidence: 0.12 < threshold 0.30
```

Variants: `explain FILE`, `explain DIRECTORY`, `explain FILE --owner @alice`,
`explain FILE --why-not @bob`, plus `explain-path` showing which CODEOWNERS rule wins and why.
Output names the specific commits behind each factor, shows rename lineage, and states which
configuration knobs would change the outcome. Neither `explain` nor `owners PATH` triggers a
full-repository pass.

**A system that influences repository governance never behaves like an unexplained scoring black
box.**

### 8.4 `checkowners suggest` / `diff` — the daily-use surface

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

Ownership impact analysis makes the tool part of the conversation rather than a gate:

```text
checkOwners: this PR shifts ownership

services/billing/invoice.ts   @alice (0.81) -> @you (0.52)  [new primary]
services/billing/tax.ts       bus factor 2 -> 1             [regression]

After merge, 3 files in this PR would have a single owner.
```

Blame runs only on changed paths. Humans accept, edit, or dismiss; nothing is auto-assigned.

### 8.5 `checkowners risk`

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

### 8.6 Remaining surface

- `checkowners owners PATH` / `who` — the simplest question, answered simply: `@alice 0.87 /
  @bob 0.61`.
- `checkowners coverage --fail-under 80` — ownership coverage as a badge-able SLO, with a
  shields-compatible endpoint and coverage trending.
- `checkowners baseline create` / `--baseline` — the ratchet.
- `checkowners cache info | clear | path | purge`.
- `checkowners transfers` — inferred knowledge transfer (`Alice owns → Bob reviews → Bob contributes
  → Alice declines → Bob's ownership rises`), warning when expertise declines with no successor.
- `checkowners report --html` — the self-contained interactive report.
- `checkowners calibrate` / `--fit` — per-repository back-testing and weight fitting.
- `checkowners identity audit` · `checkowners org analyze` · `checkowners serve --mcp`.
- `--offline` as an explicit guarantee, `--diff-only`, `--no-cache`, `--as-of`, `--exit-zero`,
  `--fail-on-incomplete`, `--allow-stale`, `--max-age`.

Retired or demoted: the webhook notifier is replaced by a three-line workflow recipe; `graph` becomes
`analyze --export dot`; `topology` is labeled experimental; `balance` feeds `suggest`; `print` folds
into `analyze --format text`; `github-action` becomes the only CI path.

---

## 9. Contracts

### 9.1 Every payload carries provenance

```json
{
  "schema_version": "1.0",
  "checkowners_version": "1.0.0",
  "model_version": "ownership-v2",
  "repository": "…",
  "head_sha": "…",
  "generated_at": "…",
  "analysis_completeness": 0.94
}
```

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

JSON Schema documents ship per command, are versioned, and are validated against real output in CI.
External evidence is timestamped (`github_evidence_collected_at`, `repository_head`, `team_snapshot`)
so a report regenerated six months later explains why results differ. **Downstream consumers never
reverse-engineer text output.**

### 9.2 Model and metric versioning

```yaml
model:
  ownership: v2
  risk: v1
  topology: v1
```

Algorithms improve without silently changing enforcement behavior in other people's pipelines. Cache
keys incorporate the model version, so evidence computed under incompatible assumptions is never
reused. Breaking changes are defined: CLI command removal, JSON schema changes, scoring-model
changes, default policy changes, Action input/output changes, configuration schema changes.

### 9.3 Exit codes

```text
0  success / no policy violations
1  internal error
2  configuration or usage error
3  findings (policy violation)
4  git or GitHub integration failure
```

Uniform across every command, with `--exit-zero` as the escape hatch. An incomplete analysis is
distinguishable from a clean one. The Action checks exit status rather than re-parsing JSON.

### 9.4 Policy separates enforcement from information

```yaml
policy:
  drift:               { fail: true }
  single_expert:       { fail: false, severity: warning }
  incomplete_analysis: { fail: true }
  unowned:             { severity: warning }
  invalid_owner:       { fail: true }

security:
  paths: [auth/**, crypto/**]
  minimum_effective_owners: 2
  require_declared_owner: true
  require_active_expert: true
```

Knowledge risk takes months to remediate and does not block every pull request; structural CODEOWNERS
defects do. Path-scoped policy makes `security/**` strict and `docs/**` informational. The effective
policy is reported, so a user never guesses why a run passed or failed. **The default first run on a
small repository is not a red X.**

### 9.5 Completeness is first-class

```text
analysis completeness: 73%
```

itemized across shallow history, absent token, unavailable team membership, unavailable review
history, ambiguous identities, excluded files, insufficient history, API rate limits, unresolved
renames, and missing `.mailmap` or ignore-revs files. `--fail-on-incomplete` protects strict
environments. Resource budgets are enforced:

```yaml
analysis:
  max_runtime_seconds: 300
  max_git_workers: 16
  max_api_requests: 2000
```

Budget exhaustion reports **analysis incomplete** — never a silently truncated complete result. Rate
limits are checked before expensive collection: `Review evidence omitted: GitHub API budget
insufficient.`

**Explicit completeness beats silent degradation, always.**

### 9.6 Baseline and suppressions

`checkowners baseline create` captures current state with stable finding identities that survive
CODEOWNERS reordering. `--baseline` fails only on **new** findings. Stale baseline entries are
reported so debt shrinks rather than calcifies.

```yaml
suppressions:
  - path: legacy/**
    rule: single-expert
    expires: 2026-12-31
    reason: "Scheduled for retirement in Q4"
```

A reason is required; expired suppressions fail loudly; suppressed counts appear in every summary.
**No permanent invisible ignores.**

### 9.7 Configuration v2

```yaml
version: 2

analysis:
  lookback_days: adaptive
  max_owners: 5
  exclude_bots: true
  follow_renames: true
  use_blame_ignore_revs: true
  as_of: ""
  respect_gitattributes: true

qualification:
  strategy: adaptive
  min_commits: 1
  strong_blame_override: 0.5

git:
  blame_ignore_revs_file: .git-blame-ignore-revs
  use_mailmap: true
  detect_moves: true
  count_co_authors: true
  merge_strategy: auto

model:
  version: ownership-v2
  normalize_available_signals: true
  signals:
    recency:          { weight: 0.25 }
    frequency:        { weight: 0.15 }
    blame:            { weight: 0.25 }
    reviews:          { weight: 0.20 }
    historical_depth: { weight: 0.15 }

identity:
  github_resolution: auto
  mailmap: true

risk:
  truck_factor_thresholds: [0.50, 0.75, 0.90]
  critical_effective_owners: 1.5

drift:
  baseline_file: ""
  hysteresis_runs: 1

privacy:
  redact_emails: true
  contributors_exclude: []

output:
  schema_version: 1
  anonymize: false
  aggregate_only: false
  max_bytes: 2500000
  verify_round_trip: true
```

Defaults are empirically calibrated, not asserted. The standard model stays opinionated — most users
do not want to become statisticians — while research overrides and plugin models remain available.

---

## 10. CI Integration

The composite Action makes exactly one CLI invocation — `checkowners github-action` — performing one
analyze pass and deriving every report from it.

- **The token is wired.** A `github_token` input defaults to `${{ github.token }}` and
  `GITHUB_TOKEN` is exported on every CLI step. Handle resolution, team collapsing, review coverage,
  and team-aware drift all work in CI.
- **Reproducible.** Action `vX.Y.Z` installs `checkowners==X.Y.Z`, generated at release time so the
  two cannot diverge, from a hash-locked requirements file, with `index_url` for internal mirrors and
  an offline vendored-wheel path. Third-party actions are SHA-pinned with dependency automation.
- **Fork-safe and fail-soft.** Comment failures produce a warning, never a job failure. Fork pull
  requests skip commenting with an explanatory notice and still receive a full report.
- **Outputs are bounded.** Random delimiters; summaries in `GITHUB_OUTPUT`; full payloads uploaded as
  artifacts with an `artifact_name` output; `max_output_entries` with explicit truncation flags.
- **Native surfaces.** `$GITHUB_STEP_SUMMARY` on every run; workflow annotations on the changed line;
  a proper Check Run; SARIF into the Security tab; JUnit XML; and a reusable workflow alongside the
  composite Action. Every one of these works **without** `pull-requests: write`.

```text
checkowners/unowned-path        checkowners/stale-owner
checkowners/ownership-drift     checkowners/invalid-owner
checkowners/shadowed-rule       checkowners/single-expert-risk
checkowners/incomplete-analysis
```

- **Fast.** `cache: pip` plus `actions/cache` for `~/.checkowners`, keyed on repository, merge base,
  configuration hash, and model version, with the staleness contract preventing reuse of a cache
  whose ref is not an ancestor of HEAD. Median pull-request run time is under 30 seconds warm.
- **Concise comments:**

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

- **Least privilege documented:**

```yaml
permissions:          # with PR commenting
  contents: read
  pull-requests: write

permissions:          # commenting disabled
  contents: read
```

No documentation example uses `write-all`.

---

## 11. Performance

```text
first analysis      → expensive
subsequent analysis → incremental
PR analysis         → touched paths only
```

- **Incremental analysis** keyed on `analyzed_ref` processes `analyzed_ref..HEAD` and merges deltas,
  handling renames via lineage, deletions, and window expiry. An incremental result is **byte-identical**
  to a full re-analysis for the same `(repo, commit, config, model)`, asserted continuously in CI. An
  invalid baseline triggers a full pass with an explicit message.
- **Blob-SHA blame cache:** `blob SHA unchanged → blame result unchanged`. Cache keys include blame
  flags, ignore-revs content, and mailmap content. A warm run on an unchanged repository performs
  zero blame invocations.
- **Command-scoped work:** a reviewer recommendation blames only changed paths; `owners foo.py` needs
  only that file; only a full audit needs everything.
- **Efficient blame:** subprocess-per-file fan-out is replaced by the benchmarked winner among
  in-process blame, a single log-based attribution pass, or batched incremental blame — with any new
  dependency optional so pure-git operation survives.
- **Atomic, locked state:** temp file plus `os.replace` with advisory locking; bounded size with
  documented eviction; `analyzed_ref` staleness contract.
- **Published benchmarks** across `1k/1k`, `10k/25k`, `100k/250k`, a large monorepo, and a
  deep-history repository, measuring cold runtime, warm runtime, peak RSS, git command count, API
  request count, cache size, and incremental runtime — reproducibly, against named public
  repositories at pinned commits, with regression detection in CI.

---

## 12. Privacy and Ethics

**checkOwners is a knowledge-risk tool, not a performance-measurement tool. Using it for individual
evaluation is unsupported and harmful.** That statement appears in the README, the usage
documentation, and every report footer.

- `output.anonymize: true` — stable pseudonymous identifiers; risk topology reviewable without naming
  people.
- `output.aggregate_only: true` — no per-individual output, full repository-level risk.
- `--redact-emails`, `privacy.redact_emails`, `identity.mode: handle | email | hashed`.
- Emails hashed at rest in state and handle caches by default.
- `contributors.exclude` — individuals opt out entirely, honored across every command and output
  format.
- `checkowners cache purge`.

Modes apply uniformly across rendered output, JSON, graph exports, pull-request comments, Action
outputs, and reports. A mode that leaks through one surface is not a mode; a leak test asserts this.

A published privacy document states exactly what data is read, what leaves the machine and when
(every API call enumerated), what is cached where and in what form, how to purge it, how to disable
identity lookup, how to redact emails, and the retention policy — with a data-flow diagram and a
threat model.

**Zero telemetry, ever, by default, stated loudly.** checkOwners sends nothing anywhere.

Positioning guardrails are policy: the project is never positioned around "who contributes least",
"who is really working", or "which engineer is underperforming". It is positioned around repository
resilience, knowledge continuity, review routing, organizational alignment, and onboarding.

---

## 13. Security and Supply Chain

Trusted Publishing, Sigstore transparency entries, in-toto attestations, and SBOMs on every release
— and advertised, not hidden. OpenSSF Scorecard and Best Practices badges; CodeQL; dependency
review; `pip-audit` and OSV scanning; Dependabot or Renovate; a workflow-security linter; signed
release artifacts. Every `uses:` is SHA-pinned; a CI lint rejects unpinned references.

One release pipeline produces every artifact:

```text
tag → test → build → attest → publish PyPI → publish binaries
    → publish Docker → generate SBOM → GitHub release
```

No independent mutable distribution channels.

---

## 14. Distribution

| Channel | Form |
|---|---|
| PyPI | `pip install checkowners` |
| Zero-install trial | `uvx checkowners audit` |
| Isolated install | `pipx install checkowners` |
| Pre-commit | three lines in an existing config |
| Container | digest-pinned, multi-architecture OCI image |
| Binaries | linux-x86_64, linux-arm64, darwin-arm64, windows-x86_64 |
| Homebrew | `brew install checkowners` |
| GitHub Action | composite Action and reusable workflow |
| `gh` extension | where users already look |
| Nix, GitLab CI component | ecosystem coverage |

Pre-commit is the habitual-usage channel: `validate` needs no historical analysis, so a hook catches
a bad pattern, an invalid owner, a duplicate rule, shadowing, and newly added unowned files **before**
a pull request is opened.

```yaml
- id: checkowners-validate
  entry: checkowners validate
  language: python
  pass_filenames: false
  files: '(^|/)CODEOWNERS$'

- id: checkowners-drift
  entry: checkowners drift --baseline .checkowners-baseline.json
  language: python
  additional_dependencies: ["checkowners[github]"]
  pass_filenames: false
  stages: [pre-push]
```

Standalone binaries are packaged from the Python application. **The project is not rewritten in
another language merely to obtain binaries.**

---

## 15. Scope Beyond a Single Repository

- **Component and service ownership** at file, directory, package, service, and repository level,
  recognizing ecosystem manifests and workspace definitions.
- **Distributed ownership declarations** — `services/payments/OWNERS`, nested CODEOWNERS files,
  service-catalog metadata — reconciled across three explicit layers: **declared-local**,
  **declared-global**, and **observed**.
- **Multi-forge output** via `--dialect github|gitlab|bitbucket|gerrit` behind a `ForgeWriter`
  interface, with a `ForgeClient` for identity and permission checks. GitLab sections, optional
  sections, and per-section approval counts are emitted from inferred topology.
- **Organization-wide analysis:**

```text
$ checkowners org analyze acme

Repositories: 214 · Components: 1,932
Critical single-expert components: 117
Former-contributor risk: 42
Teams with ownership drift: 9
Cross-team knowledge dependencies: 27
```

- **Portfolio knowledge risk** — *Alice is the sole critical expert in `auth-service`, `billing`,
  `internal-sdk`, and `terraform-production`* — far more meaningful than four independent reports.
- **A stable library API** (`from checkowners import RepositoryAnalyzer`) enabling IDE extensions,
  internal dashboards, apps, and research without shelling out.
- **An MCP server** exposing `who_owns(path)`, `suggest_reviewers(diff)`, `bus_factor(path)`, and
  `explain(path, owner)`. Coding agents need deterministic, local, token-free ownership ground truth,
  and the no-LLM posture becomes a genuine feature here.

---

## 16. The Ownership Graph

Entities: `Developer`, `Team`, `File`, `Directory`, `Package`, `Service`, `Repository`, `Commit`,
`PullRequest`, `Review`, `Incident`, `Dependency`, `Domain`, `CODEOWNERSRule`.

Edges: `AUTHORED`, `REVIEWS`, `OWNS_DECLARED`, `OWNS_OBSERVED`, `MEMBER_OF`, `DEPENDS_ON`,
`MODIFIES`, `REPLACES`, `REVIEWED_BY`, `KNOWS`, `DECAYING_EXPERTISE`.

Answerable questions:

```text
Which service has no active expert?
Which developer is an articulation point between Payments and Platform?
Which services depend on code whose sole expert left?
What is the shortest knowledge-transfer path from Alice to Checkout?
Which teams have hidden cross-team dependencies?
```

Graph risk analytics: articulation points, betweenness centrality, community boundaries, orphan
subgraphs, ownership islands, transfer paths, organizational coupling. Exports in JSON, GraphML,
GEXF, and DOT so users load ownership data into standard graph tooling. `networkx` remains an
optional, lazily imported extra.

Team topology uses real weighted community detection — a named algorithm with an exposed resolution
parameter, a fixed seed, reported modularity and cross-seed stability, and support for overlapping,
nested, and matrix membership. Comparison against declared teams is probabilistic:

```text
Inferred cluster: Alice, Bob, Carol, Dave
Declared @payments: Alice, Bob, Carol
Jaccard: 0.75
Potential cross-team expert: Dave
```

Inferred teams are framed as **exploratory repository topology** and stated as a hypothesis for a
human to confirm — never as an org chart.

---

## 17. Correctness Assurance

- **Real-git integration fixtures** with date-pinned commit scripting, alongside the mocked unit
  tests. Drift, patterns, generate, and validate all have integration coverage. Every historical
  product-invalidating bug has a regression test that fails against the pre-fix behavior.
- **A public CODEOWNERS compatibility corpus** — thousands of `{pattern, path, expected}` cases
  covering roots, nested directories, `**`, `*`, `?`, spaces, escaped spaces, comments, ordering,
  unsupported negation, bracket syntax, Unicode names, dotfiles, comment-like paths, multiple owners,
  and owner-less rules — plus ~500 real-world files as regression inputs. **Published, so other tools
  can test against it.**
- **Property-based tests and fuzzing** asserting the invariants directly: `**` crosses segments, `*`
  does not, leading `/` anchors, last match wins, `dir/*` is direct children only, trailing `/`
  matches contents. The matcher is the best-tested module in the project by branch coverage and
  mutation score.
- **A conformance harness** diffing the local validator against GitHub's own errors endpoint over the
  corpus, with a **published pass rate** per release, handling and documenting the private-repository
  empty-response caveat.
- **Model golden tests** encoding the project's ownership philosophy as executable specification:
  formatting confers no ownership; ownership survives renames; ownership transitions gradually; a
  single large commit confers strong ownership with medium evidence quality; a stable path retains
  its owner; rewritten code shows near-zero survival; co-authors are credited; mailmap-merged
  identities count once.
- **Mutation testing** on the correctness-critical modules with an enforced floor on the matcher.
- **Enforced coverage floor**, ruff `S` and `PTH` plus `RUF`, `C4`, `PL`, `ARG`, `TID`, `ERA`, and
  tracked branch coverage on critical modules.
- **Cross-platform and git-version matrices** — Ubuntu, macOS, Windows across supported Python
  versions; minimum, mid, and latest git — with a path corpus exercising spaces, Unicode, separators,
  drive roots, case sensitivity, and long paths.
- **Determinism tests** in CI on every pull request.

---

## 18. Proof

### 18.1 The validation study

**Ground truth is future behavior, not the existing CODEOWNERS file** — treating a possibly-stale
file as truth would be circular, since detecting that staleness is the product.

```text
history before t → CheckOwners prediction → actual reviewers/maintainers after t
```

Across 20+ public repositories spanning size, language, merge strategy, and turnover, measuring
`precision@1/3/5`, `recall@3`, MRR, NDCG, Brier score, calibration error, changed-file coverage,
acceptance and rejection rates, false-positive rate, reviewer overload, review latency, time to first
useful review, stale-ownership detection, and handoff time — broken out by repository type, language,
monorepo scale, merge strategy, and turnover.

Naive baselines — most commits, blame, recency, frequency — are implemented and compared honestly,
including cases where checkOwners loses. Weights are **optimized against held-out repositories**, not
asserted, and the learned defaults ship with the study. A calibration curve is published. Dataset
generation, the evaluation harness, the scores, and the methodology are all open-source.

If calibration justifies it, the score is named `confidence` and means a measured probability.
Otherwise it remains `ownership_score`. **The name follows the evidence.**

### 18.2 Per-repository calibration

`checkowners calibrate` back-tests inference against a team's **own** history and reports
`precision@1/3`, `recall@3`, MRR, Brier score, and a calibration curve. `--fit` learns
per-repository weights, writes them with full provenance under a distinct model version, refuses
insufficient history, reports held-out rather than training performance, and warns when fitted
weights diverge substantially from defaults.

### 18.3 The false-positive ledger

Published for benchmark repositories, recording prediction, maintainer judgment, reason, and
algorithmic fix:

```text
False positive:
Formatting migration caused Bob to dominate blame.

Fix:
.git-blame-ignore-revs support.
```

Corrections are categorized — correct; wrong; right person but unavailable; team owns it; security
owner required; compliance owner required; temporary migration ownership; departed contributor;
useful but incomplete; no ownership for this path; should not own this area. Every fixed entry
becomes a permanent regression test. Both the human judgment **and** the repository evidence are
preserved; neither overwrites the other.

**The product deliberately publishes failure cases, not only favorable demonstrations.**

### 18.4 The public dataset

100–500 significant public repositories analyzed, raw reports published as a citable dataset, with a
findings post — *"we measured the ownership risk of the 250 most-depended-on packages; N% have at
least one directory with a truck factor of 1"* — and an interactive lookup. Fifty open-source audits
publish coverage, drift, concentration, one interesting finding, and an explorable report each.
Maintainers are contacted only when a result is genuinely interesting; every report carries
limitations and the non-performance-metric statement; a removal path exists and is honored.

checkOwners audits checkOwners publicly:

```text
Ownership coverage: 98%   Critical knowledge risk: 2 paths   CODEOWNERS drift: 0
```

---

## 19. Documentation

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

A published methodology document carries the exact formulas, the cited prior art (including where
this project's earlier metric diverged from the literature and why), what each signal measures and
proxies for, the terminology guardrails, the golden-test philosophy, and the benchmark and
calibration methodology.

A published limitations document states plainly:

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

alongside the can/cannot boundary: repository evidence supports claims about contribution frequency,
recency, current-line attribution, review participation, concentrated activity, stale rules, changing
patterns, continuity risk, and reviewer affinity — and **cannot prove** formal team ownership,
architectural authority, organizational accountability, operational ownership, incident
responsibility, compliance responsibility, security responsibility, current employment, reviewer
availability, business ownership, or managerial accountability.

**An open-source intelligence tool earns trust by explaining when it can be wrong.**

---

## 20. Terminology Discipline

Never treated as equivalent: contribution and ownership; blame and authority; recent activity and
expertise; co-commit clusters and teams; inferred reviewer affinity and organizational
accountability; an uncalibrated score and a probability.

| Instead of | Used |
|---|---|
| organizational owner | repository-evidence candidate · reviewer candidate · evidence-ranked reviewer |
| expertise decay | ownership freshness · evidence freshness · continuity risk |
| backup reviewer | candidate backup reviewer |
| bus factor (as a truncated count) | qualified owner count · reviewer depth |
| uncalibrated `0.92` | evidence score · ranking signal · ownership score |
| knowledge graph (bipartite ownership data) | ownership graph |
| org chart | exploratory repository topology |
| review load derived from commits | git authorship proxy |
| historical confidence | historical activity confidence |

---

## 21. Project Principles

1. **Evidence over guesses.** Every recommendation derives from inspectable evidence.
2. **Human policy remains authoritative.** Observed expertise informs CODEOWNERS; it never silently
   overrides governance.
3. **Missing evidence is not negative evidence.**
4. **Local-first.** Core analysis works without uploading proprietary source.
5. **Deterministic by default.** Same evidence plus same model equals same result.
6. **Explain everything.** Every score and recommendation is inspectable.
7. **Measure uncertainty.** Thin evidence is never presented as certainty.
8. **Compatibility matters.** GitHub CODEOWNERS semantics are reproduced accurately and provably.
9. **No employee surveillance.** Optimize organizational resilience, not individual scoring.
10. **Correctness before feature count.**

Behavioral guarantees: propose rather than enforce; explain rather than rank; display uncertainty;
preserve manual overrides; make corrections durable; remain reversible; show missing evidence; show
limitations; allow explicit exclusions; separate inferred evidence from accepted policy. **Repository
activity is never silently converted into permanent organizational responsibility.**

---

## 22. Governance

A second maintainer holds release rights and is named publicly — the tool that measures bus factor no
longer has a bus factor of one. A contributor ladder documents how responsibility is earned:

```text
Contributor → Reviewer → Maintainer → Core Maintainer
```

Architectural decision records cover the confidence model, the pattern engine, identity resolution,
the risk-metric redesign, the notifier decision, the blame architecture, and the forge abstraction. A
citation file unlocks academic reference. Community infrastructure — issue and pull-request
templates, discussions, a public roadmap, funding, scoped good-first-issues, real examples, and demo
assets — gives outsiders obvious entry points.

The open-core boundary is drawn at the repository boundary and stated publicly:

| Open source, forever, no asterisk | Commercial layer |
|---|---|
| Single-repository analysis, all commands | Cross-repository and organization-wide aggregation |
| Full JSON output, full schema, no gating | Historical time-series storage and trend alerting |
| CODEOWNERS generation, drift, validation | Organization-level identity graph and team reconciliation |
| Per-repository risk, decay, topology | Ranked organization-wide risk register and remediation workflow |
| The Action, the CLI, the pre-commit hook | Hosted dashboards, SSO, RBAC, ticket and chat routing |
| The inference spec and conformance corpus | Benchmarks against peer organizations |

Protected by rules: never cripple the open-source tool to sell the paid one; zero telemetry by
default, stated loudly; the commercial product consumes the open-source JSON contract unmodified,
with schema changes shipping to open source first; the license stays MIT; the privacy work is not
optional to this strategy; the validation study is the moat, not a nice-to-have.

---

## 23. What `1.0` Means

Released only when all of the following hold — never because the feature list feels complete.

**Correctness** — the compatibility corpus is excellent; rename-aware history exists; ignored
revisions are supported; team drift works; sparse-file inference is fixed.
**Metrics** — score semantics are stable; evidence quality exists; the risk methodology is replaced;
model versioning exists.
**API** — JSON schema, configuration schema, and exit-code behavior are stable and documented.
**CI** — the Action is reproducible; dependencies are controlled; changed-files mode is usable.
**Trust** — security policy, provenance, SBOM, benchmark, and published limitations exist.
**Adoption** — multiple external production users, real issue and pull-request activity, several
outside contributors. No star threshold; actual usage matters more.

---

## 24. The Position Achieved

The defensible combination nobody else has assembled:

```text
native CODEOWNERS semantics + historical ownership inference
+ confidence and evidence modeling + declared-versus-observed drift
+ knowledge-risk analysis + CI enforcement + local-first open source
```

Individual pieces exist elsewhere. The integrated closed loop is stronger:

```text
Observe → Infer → Explain → Compare → Detect drift/risk
        → Recommend → Update policy → Continuously verify
```

CODEOWNERS remains the adoption wedge because it gives developers an immediately understandable
problem:

```text
"Is your CODEOWNERS wrong?" → checkowners audit → ownership risk
                            → organization-wide intelligence
```

The strongest moat is not the code. It is the benchmark, the validated inference methodology, the
compatibility corpus, and real-world calibration. Any developer can clone a CLI concept. Far fewer
can credibly say: *we benchmarked six ownership models across hundreds of repositories and historical
snapshots, and this model best predicts future maintenance and review responsibility.*

The second moat is a normalized engineering ownership evidence graph behind an open schema — reusable
infrastructure for reviewer routing, onboarding, documentation, knowledge transfer, service
ownership, incident escalation, and engineering search, with CODEOWNERS as merely one adapter.

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

The tool no longer answers only *"what should the CODEOWNERS file say."* It answers:

> **"Is anyone actually reviewing this code, and what breaks when they leave?"**

Not a good CODEOWNERS utility — **the canonical open-source engine for understanding, auditing, and
continuously reconciling software ownership and engineering knowledge.**

> **checkOwners is the local-first, evidence-driven system that keeps pull-request review ownership
> current, recommends the right reviewers with explainable uncertainty, detects stale or concentrated
> ownership early, reconciles declared policy with observed expertise, and learns from human
> corrections — without ever turning git activity into false organizational authority.**
