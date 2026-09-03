# checkOwners

**Keep CODEOWNERS aligned with reality.**

> The open-source ownership intelligence engine that continuously reconciles who is *supposed* to own
> code with who *actually knows* it.

---

## The Problem

CODEOWNERS is a static file describing a dynamic property.

Teams move. People leave. Code is inherited, rewritten, and reorganized. The file drifts away from
reality — silently — until a high-risk pull request routes to someone who changed teams nine months
ago, or to one overloaded maintainer, or to nobody who can actually approve it.

Worse, CODEOWNERS **fails silently by design**: an owner without write access is ignored; an invisible
team is ignored; an invalid line is skipped; a file over 3 MB is not loaded at all; a later general
rule quietly overrides your specific one. Branch protection still looks configured. No review is ever
requested.

Nobody finds out until an incident, a migration, or an audit.

---

## What It Does

Analyzes git and review history to infer who actually knows each part of a repository, compares that
evidence against declared policy, exposes continuity risk before it becomes an incident, and shows
the evidence behind every recommendation.

```bash
uvx checkowners audit
```

```text
HIGH ownership drift
/payments/
Declared: @platform
Observed: @payments   (@alice 0.91 · @bob 0.78, both now on @payments)

Suggested:
-/payments/ @platform
+/payments/ @payments
```

---

## The One Question

> **Who should review this change, and how much should we trust that recommendation?**

## The One Principle

> **Observed expertise is evidence, not authority.**

checkOwners recommends and audits. Humans and organizational policy still decide responsibility.

---

## Core Capabilities

| Command | Answers |
|---|---|
| `audit` | Is my ownership configuration correct, current, and enforceable? |
| `doctor` | Is it working *right now* — write access, visible teams, shadowed rules, size limits? |
| `explain PATH` | Why this person? Why **not** that person? |
| `owners PATH` | Who owns this? |
| `suggest --diff` | Who should review this change, and who is too loaded to? |
| `risk` | Where is knowledge dangerously concentrated, stale, or gone? |
| `simulate --remove` | What breaks if these people leave? |
| `generate` / `sync --pr` | Reconcile CODEOWNERS — as a reviewable pull request |
| `report --html` | One self-contained, explorable ownership report |

---

## How It Decides

**A confidence-scored spectrum, not a binary assignment.** Ownership is inferred from recency,
meaningful change frequency, current-line attribution, review participation, historical depth, change
survival, and maintainer continuity — normalized over the signals actually available.

**Two numbers, never conflated:**

```json
{ "owner": "@alice", "ownership_score": 0.84, "evidence_quality": 0.71 }
```

`ownership_score` says what the evidence indicates. `evidence_quality` says how much evidence there
is. `@alice` at 0.91/0.93 and `@bob` at 0.79/0.31 are different claims — and only this split can say
so.

**Missing evidence is not negative evidence.** An unavailable signal lowers confidence in the answer,
never a person's score.

**Real knowledge concentration**, computed over every contributor:

```text
top_owner_share · effective_owners (1/Σpᵢ²) · truck_factor_50/75/90 · entropy · HHI

100/0/0/0 → 1.00 effective owners     25/25/25/25 → 4.00
```

A dominant owner at 82% yields `TF50 = 1`. A count of owners cannot see that.

**Repository risk is a distribution, weighted by criticality** — never an arithmetic mean that lets
documentation files drown out `auth/crypto.py`.

---

## Guarantees

| | |
|---|---|
| **Local-first** | Core analysis is pure git. No SaaS account. No source-code upload. |
| **No LLM** | Deterministic, auditable, reproducible, air-gap compatible. Nothing is hallucinated. |
| **Zero telemetry** | checkOwners sends nothing anywhere. Ever. By default. |
| **Deterministic** | Same repo + commit + config + model = byte-identical output. No flapping CI. |
| **Explainable** | Every score decomposes into signals, evidence, and the commits behind them. |
| **Honest about gaps** | `analysis completeness: 73%`, itemized. "Could not verify" is never reported as "no problem". |
| **Human-authoritative** | Proposals, not policy. Manual blocks survive regeneration untouched. |
| **Reproducible supply chain** | Trusted Publishing, Sigstore attestations, SBOM, SHA-pinned actions, hash-locked installs. |

---

## In CI

One Action, one analysis pass, and every native surface:

- **Step summary, annotations, Check Runs, and SARIF** — all working **without** `pull-requests:
  write`, so fork pull requests and read-only-permission organizations get full reports instead of a
  red X.
- **Baseline ratchet** — turn it on for a 12,000-file monorepo and fail only on **new** findings.
- **Policy engine** — structural CODEOWNERS defects block; knowledge risk informs. Path-scoped, so
  `security/**` is strict and `docs/**` is not.
- **Incremental analysis** — warm runs in seconds, not minutes. Slow required checks get deleted.
- **Stable contracts** — versioned JSON schemas, documented exit codes, model versioning, so
  improving an algorithm never silently changes someone else's merge gate.

```text
0 clean · 1 internal error · 2 config/usage · 3 findings · 4 integration failure
```

---

## Not a Performance Tool

checkOwners is a **knowledge-risk** tool. Using it for individual evaluation is unsupported and
harmful, and the product says so in the README, the docs, and every report footer.

Anonymized and aggregate-only modes, email redaction and hashing at rest, per-contributor opt-out,
cache purge, and a published data-flow and threat model. Positioned around repository resilience,
knowledge continuity, review routing, organizational alignment, and onboarding — never around who
contributes least.

---

## Proof, Not Assertion

Most tools in this category ship a scoring heuristic and call it confidence. checkOwners publishes
the measurement.

- **Ground truth is future behavior** — who *subsequently* maintains and reviews the code — not the
  existing CODEOWNERS file, which may be exactly the stale artifact under test.
- **20+ public repositories**, precision@k, recall, MRR, Brier score, and calibration curves, against
  naive baselines (most commits, blame, recency), **including where checkOwners loses**.
- **Weights optimized on held-out data**, shipped with the study.
- **A public false-positive ledger** — prediction, maintainer judgment, reason, fix — with every
  fixed entry becoming a permanent regression test.
- **A published CODEOWNERS compatibility corpus** and a conformance pass rate against GitHub's own
  errors endpoint.
- **Reproducible performance benchmarks** against named public repositories at pinned commits.
- **`checkowners calibrate`** — verify the model against *your* repository before trusting it.

---

## Where It Runs

`pip` · `uvx` · `pipx` · pre-commit · Docker · standalone binaries · Homebrew · GitHub Action ·
reusable workflow · `gh` extension · Nix · GitLab CI

Multi-forge output via `--dialect github|gitlab|bitbucket|gerrit`, including GitLab sections and
approval counts. A stable library API and an MCP server expose `who_owns`, `suggest_reviewers`,
`bus_factor`, and `explain` — deterministic, local, token-free ownership ground truth for humans,
dashboards, and coding agents alike.

---

## The Loop

```text
Observe → Infer → Explain → Compare → Detect drift/risk
        → Recommend → Update policy → Continuously verify
```

```text
"Is your CODEOWNERS wrong?" → audit → ownership risk → organization-wide intelligence
```

Individual pieces exist elsewhere. The integrated closed loop — native CODEOWNERS semantics,
historical inference, confidence and evidence modeling, declared-versus-observed drift, knowledge-risk
analysis, CI enforcement, and local-first open source — does not.

---

## Why It Wins

The moat is not the code. Anyone can clone a CLI concept.

The moat is the **benchmark**, the **validated inference methodology**, the **compatibility corpus**,
and **real-world calibration** — plus a normalized engineering ownership evidence graph behind an
open schema, reusable for reviewer routing, onboarding, knowledge transfer, service ownership,
incident escalation, and engineering search, with CODEOWNERS as merely one adapter.

Far fewer projects can credibly say: *we benchmarked six ownership models across hundreds of
repositories and historical snapshots, and this one best predicts future maintenance and review
responsibility.*

---

Most tools answer *"what should the CODEOWNERS file say."*

**checkOwners answers "is anyone actually reviewing this code, and what breaks when they leave?"**
