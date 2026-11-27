---
name: coding-team
description: "Handle /coding-team controls and coordinate substantial coding, debugging, planning or review with role-scoped native subagents (ct-architect, ct-researcher, ct-developer, ct-test-engineer, ct-reviewer, ct-security) and independent verification. Respect eligibility and orchestrator routing; skip routine explanations and trivial edits unless requested."
---

# Coding Team

Use the smallest amount of coordination that improves the result. Leader owns scope, integration, verification and the final answer. Follow higher-priority instructions, project rules and the user's authorization; this skill adds no permissions.

When the work needs separate Spec and Plan artifacts, or an existing pair is adopted, apply [adaptive planning](references/adaptive-planning.md) before drafting or presenting either artifact. Leader personally writes and revises both, selects independent reviewers for each artifact's actual questions, obtains user approval of the reviewed Spec before drafting the Plan, then obtains approval of the reviewed Plan before implementation. Small, clear work can use direct work or a checklist. The adaptive process does not impose a fixed reviewer roster or automatically select FULL_TEAM. Instructions remain ADVISORY; they do not enforce runtime approvals.

## Choose the orchestrator

Each phase of a task has one primary orchestrator. Apply top to bottom, first match wins; an explicit user choice wins unless safety policy forbids it. Details, ownership and handoff: [orchestration](references/orchestration.md).

| # | Condition | Orchestrator |
| --- | --- | --- |
| 1 | L3 consequences | coding-team |
| 2 | Architecture conflict, or user asks for critique/consensus | coding-team |
| 3 | Several interface contracts, schemas or APIs change | prefer coding-team |
| 4 | Several independent problems to investigate | DPA (investigation only), then re-route |
| 5 | Plan exists, scope clear, sequential review suffices | SDD |
| 6 | Small, low-impact (L0 or bounded L1) | Leader directly |

File count alone is never the criterion. Without Superpowers in the session, rows 4-5 go to coding-team AUTO. To apply the table to classified facts, run `python <skill dir>/scripts/coding_team.py orchestrator route --input FACTS.json`. Routing is ADVISORY: say the choice in one line and report it in the final answer or ledger. When coding-team takes a task, run `orchestrator status`, then `orchestrator claim`; keep the returned token in context only.

## Levels

| Level | Meaning | Action |
| --- | --- | --- |
| **L0** | Routine explanation, obvious reversible local edit | Leader works directly; no team, plan or ledger. |
| **L1** | Bounded bug or feature | Leader works directly with a checklist and may use technique skills; coding-team only on user request. |
| **L2** | Cross-module work, unclear root cause, interface risk | Concise plan; delegate useful investigation, implementation or review. Read [workflow](references/workflow.md). |
| **L3** | Meaningful security, integrity, production or irreversible consequences | Independent review before the affected operation, a `ct-security` report, validation and recovery. Read workflow and [quality guidance](references/quality-safety.md). |

Level alone neither requires FULL_TEAM nor grants external authority or a new approval gate. Ask only for a missing decision or permission.

## Modes

- **AUTO**, default: the smallest useful team; no unanimous plan gate.
- **SOLO**: do not delegate; verify directly and disclose missing independence.
- **PLAN_ONLY**: investigate and plan without implementation.
- **REVIEW_ONLY**: inspect and check without fixing.
- **FULL_TEAM**: Architect, Developer and independent QA representatives and the plan gate below.
- **STRICT_MODEL**: honor exact pins and configured selections without substitution; read [model routing](references/model-routing.md).
- **USER_APPROVAL_REQUIRED**: wait for the user's plan approval.

Do not change modes to evade a gate or stopping condition.

## Eligibility

Use [`scripts/coding_team.py`](scripts/coding_team.py) for the seven commands: `/coding-team status`, `on`, `off`, `on --global`, `off --global`, `reset`, `help` (see [control policy](references/control-policy.md)). Run `status` before applying coding-team and immediately before each dispatch; never reuse a cached ON after OFF. Chat policy overrides global; an explicit human task request overrides eligibility for that task only. Missing, corrupt or `UNKNOWN` state means no automatic dispatch. Controls never reset task history or waive gates.

## Roles and delegation

If this installation offers an [external Desktop worker](references/control-policy.md#external-workers), handle its direct user controls with the installed `command` entry from the worktree. Check active Leader, native policy, opt-in, pairing and delivery readiness before assigning work. Open a coordination round before dispatch, and keep it open while reviewing a worker result. Follow the platform guide for the native representative, repeated corrections and recovery.

For direct `/coding-team status`, `/coding-team help` or `/coding-team listen`, check the [external worker routing](references/control-policy.md#external-workers) before responding. If this chat is the Desktop worker, follow its installed worker skill for these commands. Handle native policy changes with the native command entry.

| Role | Purpose | Ledger role |
| --- | --- | --- |
| `ct-architect` | Read-only design, interfaces, file-ownership matrix | Architect |
| `ct-researcher` | One bounded code/docs/root-cause question, read-only | - |
| `ct-developer` | Implements assigned files, optional `focus` | Developer |
| `ct-test-engineer` | Writes and runs tests, incl. browser E2E | - |
| `ct-reviewer` / `ct-reviewer-verify` | Independent QA: read-only review / review that runs checks | QA |
| `ct-security` | Security, performance, concurrency on an exact snapshot; required at L3 | - |

Before dispatch, read [agent contracts](references/agent-contracts.md), [model routing](references/model-routing.md) and [numeric defaults](references/governance-defaults.json). Tiers: default tiers come from the installed `platforms/<platform>.json`; Leader may raise or lower them. Guarantees: report ENFORCED/ADVISORY honestly; never present ADVISORY as a guarantee.

**No nested delegation**: workers never dispatch, spawn or define agents, and never start another agent or AI CLI through a shell. Capacity is `max_subagents` excluding Leader, limited by runtime and user limits; start small when capacity is unknown. Rejected spawns do not count as participation. Give each file one writer; keep first reviews independent and route findings through Leader. Do not change global settings or relax permissions to obtain agents.

## Gates and disagreement

Read [debate governance](references/debate-governance.md) for full-team plan review, material disagreement or repair loops. FULL_TEAM, a selected full-team plan review or a user-required gate needs PASS **before covered implementation**: distinct Architect, Developer and QA representatives approving the same Spec/Plan pair, testable acceptance and no unresolved BLOCKER/MAJOR. Pending or stale approval means HOLD. Material changes invalidate affected approvals; votes, silence, replacement or Leader rebuttal never supply approval.

At L3, Gate 2 also requires a complete `ct-security` report bound to the same snapshot, checked by `ct-reviewer`, with no unresolved BLOCKER/MAJOR, unless a valid user exception is recorded.

Keep cumulative counters and findings across workers, retries and compaction; do not reset counters by renaming issues or changing executors. Use the [optional ledger](references/task-ledger.md) when context-only accounting is fragile; it validates records, not authority.

## Verify and finish

Preserve workspace changes. Verify the stable integrated result, including uncommitted and untracked files. Without agents, continue feasible authorized work only where required independent review or gates do not block it. Self-review is never independent QA. Never fabricate participation, consensus, model metadata or results. Report actual checks, outcomes, effective models (or `unverified`) and limits in the user's language. Skill rules are behavioral guidance, not runtime locks.
