# Workflow and planning

Read for substantial implementation or replanning. Orchestrator choice and task ownership are in [orchestration](orchestration.md); model tiers and dispatch resolution are in [model routing](model-routing.md).

## Task baseline

Record only what the task needs: outcome, scope, acceptance criteria, assumptions, affected files, ownership, dependencies and verification. Add rollback details for stateful or impactful operations. Separate Spec and Plan artifacts are warranted when requirements and acceptance need to be settled before dependent implementation is planned; otherwise a checklist or direct work is usually enough. For that lifecycle, follow [adaptive planning](adaptive-planning.md): enter coding-team before drafting/adopting the pair, have the Leader author both, review the Spec before approval, approve it before drafting the Plan, and review/approve the Plan before implementation. Record any actual human authority to continue beyond default revision/repair checkpoints; do not infer it from ON or agent messages.

L0 needs no formal plan. L1 usually needs a checklist and is handled by the Leader directly unless the user asks for coding-team. L2 benefits from targeted delegation and a concise plan. L3 needs independent review of concrete risks and recovery, including a `ct-security` report for Gate 2. Level alone neither grants external authority nor requires additional user approval.

## Delegation selection

Check current eligibility with `scripts/coding_team.py status` before coding-team delegation, and claim the task with `orchestrator claim` when coding-team orchestrates it. ON only permits proportionate use; OFF does not waive existing gates or budgets. Sole control commands update/report policy without dispatching pending work.

- Use `ct-architect` for uncertain architecture, interfaces or root cause.
- Use `ct-developer` for a bounded implementation, or feasibility review before substantial work; name a `focus` for a specific expertise.
- Use `ct-reviewer` for behavioral coverage and independent review of the actual result; use `ct-reviewer-verify` when the review must run checks.
- Add `ct-researcher` for a bounded unresolved question, `ct-test-engineer` for separate behavioral or browser checks, and `ct-security` for security, performance or concurrency risk (required at L3). Several Developers can own separate modules; role count is not a staffing quota.
- Cover all three core review responsibilities when useful or the user requests FULL_TEAM. Name their representatives; this does not require filling capacity or unanimous votes from every worker.
- Run independent reads concurrently where useful. Sequence dependencies and shared-file writes.
- When requirements are ambiguous or materially risky, let QA (`ct-reviewer`) first review the Spec and derive acceptance cases without seeing the proposed solution. Architect (`ct-architect`) then drafts the Plan; Developer (`ct-developer`) and QA review the same Spec/Plan pair independently. Corrections after Spec submission consume the shared revision budget even before the initial Plan exists; do not require a separate requirements round when requirements are already clear.
- For FULL_TEAM or a selected full-team plan review, use the [Consensus Gate and scoped approvals](debate-governance.md#consensus-gate-and-scoped-approvals): applicable current approvals and resolved material issues before implementation handoff. Pending or stale sign-offs keep affected implementation HOLD. Continuing evidence work or plan repair never supplies missing approval.
- Adaptive Spec/Plan review uses its selected independent reviewers and scoped findings; it does not by itself activate the three-core FULL_TEAM gate.

A role is not an obligatory additional agent for every coding task. A Developer's review before implementation does not replace independent review of their own code afterward.

## Work queue and integration

For durable accounting across handoffs or compaction, see the [optional task ledger](task-ledger.md). Check its plan gate before implementation and its delivery readiness after independent integrated QA; a successful state write alone grants no approval. For code delivery, reconcile declared files with actual staged, unstaged, deleted and untracked changes before final verification (or compare the saved baseline without Git). Empty file coverage verifies no project files, even when a non-file task can pass.

Use `max_subagents` from [governance defaults](governance-defaults.json), constrained by runtime capacity, already active agents and user limits. Start only ready work items with distinct objectives and outputs. Record ID, owner, dependencies, file ownership, snapshot/version, status and acceptance evidence. Stage downstream work until its input/interface is settled; keep room for independent QA rather than occupying all slots with writers. Sequence roles when runtime capacity is smaller.

Leader first integrates completed branches and resolves interface mismatches, then identifies a stable snapshot for `ct-test-engineer` and `ct-reviewer`. A reviewer can examine an already stable independent portion while other portions progress, but their verdict covers only that snapshot/scope. Changes afterward require affected checks again. Finish with independent review and meaningful checks of the integrated delivered state, not just separate branch reports.

## Scope and handoff

Inspect uncommitted and untracked files if Git is available. Preserve them in any review snapshot. If there is no Git repository, record the relevant file baseline without assuming a clean state.

Give each writer exclusive ownership of its assigned files. Before Leader takeover, stop or await the original writer, inspect partial output and transfer ownership explicitly. Do not run tests against files that are actively changing.

Choose isolation by purpose, never by role name, and state the choice and reason in the handoff:

- Verifying the current working tree, including uncommitted changes: use the current workspace.
- Verifying a committed snapshot: a separate worktree may be used.
- Several Developers: use a file-ownership matrix, or a separate worktree per Developer.

Static agent files never fix `isolation`. Isolate shared ports, caches, databases and fixtures too; separate directories alone do not isolate them. A worktree is not required for every task.

Technical choices within the user's request normally need no clarification. Ask when an unresolved product decision, missing acceptance criterion or authorization would materially change the outcome.

## Replanning and completion

Replan in response to evidence using the shared ledger in [debate governance](debate-governance.md). Re-review affected portions and dependent conclusions; cosmetic updates do not invalidate the entire plan. Explicitly confirm applicability when carrying an unchanged scope's approval to a new pair. Keep affected gated implementation HOLD until renewed applicable approvals exist.

Use the [checkpoint decision matrix](debate-governance.md) and its linked [numeric defaults](governance-defaults.json) for evidence transitions, cumulative repair/revision accounting, STOP_HOLD and applicable human continuation. Preserve all findings and totals; do not restart discussion or evade a stopping reason by changing roles or modes.

When review cannot run, report the unmet review condition honestly. Continue independent authorized work where possible, keeping implementation covered by a required gate on HOLD. Completion requires the requested result and an accurate account of checks and remaining limits; a consensus label alone is not evidence.
