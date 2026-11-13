# Delegation contracts

Read before spawning. Resolve model and tier with [model routing](model-routing.md). Use the platform's actual native subagent API with the installed role agents; do not invent parameters or substitute user-visible chat creation.

## Roles

Each role's contract lives in one source file, rendered into the platform's static agents at install time together with a `Runtime guarantees` section:

| Role / variant | Contract | Ledger role |
| --- | --- | --- |
| `ct-architect` | [ct-architect.md](../agents/roles/ct-architect.md) | Architect |
| `ct-researcher` | [ct-researcher.md](../agents/roles/ct-researcher.md) | - |
| `ct-developer` | [ct-developer.md](../agents/roles/ct-developer.md) | Developer |
| `ct-test-engineer` | [ct-test-engineer.md](../agents/roles/ct-test-engineer.md) | - |
| `ct-reviewer`, `ct-reviewer-verify` | [ct-reviewer.md](../agents/roles/ct-reviewer.md) | QA |
| `ct-security` | [ct-security.md](../agents/roles/ct-security.md) | - (Gate 2 condition at L3) |

The old Investigator is `ct-researcher`, Test Engineer is `ct-test-engineer`, QA / Reviewer is `ct-reviewer` (plus `ct-reviewer-verify` when checks must run), Specialist is `ct-security` for security/performance/concurrency or a Developer/Reviewer `focus` otherwise.

## Actual guarantees

Each rendered agent states four guarantees: `no_file_write`, `write_scope`, `command_limits`, `no_delegate`, each `ENFORCED`, `ADVISORY` or `N/A`. `doctor` reports the same matrix for the installed copy. ENFORCED appears only where a runtime mechanism exists:

- **Claude Code / Antigravity:** read-only roles without shell (`ct-architect`, `ct-researcher`, `ct-reviewer`, `ct-security`) get a tool allowlist with no write, shell or agent tool: `no_file_write` and `no_delegate` are ENFORCED. Granting any MCP tool (for example `install --docs-mcp NAME` on Claude Code) or inheriting MCP lowers both to ADVISORY, because a tool name proves nothing about server behavior.
- Any role with a shell (`ct-developer`, `ct-test-engineer`, `ct-reviewer-verify`): `no_delegate` is ADVISORY, since a shell can start another AI CLI; `ct-reviewer-verify`'s read-only behavior and command limits are ADVISORY.
- `write:assigned` and `write:tests` (file ownership) are always ADVISORY.
- **Codex:** every guarantee is ADVISORY; subagents inherit the parent session's sandbox, and the generated role files set no tool allowlist or sandbox.

## Compact handoff: always required

Send a self-contained request containing:

- Role/variant ID (for example `ct-reviewer-verify`), bounded objective, acceptance criteria, expected deliverable and evidence.
- Exact workspace, ownership or inspection scope, allowed actions and stopping conditions.
- Stage: investigate, plan-review, implement or verify.
- Preferred Tier and Requested Model/effort, plus user pins; say if a force-model variable is active.
- The actual guarantees for this platform and role, copied from the agent or `doctor`, labeled ENFORCED or ADVISORY.
- Allowed technique skills, by name (for example test-driven-development, systematic-debugging, verification-before-completion, receiving-code-review), and the forbidden ones: coding-team, subagent-driven-development, dispatching-parallel-agents, executing-plans, requesting-code-review.
- Isolation choice and its reason: current workspace (verifying the working tree including uncommitted changes), a separate worktree (verifying a committed snapshot), or a file-ownership matrix / per-Developer worktree (parallel Developers).
- Effective root task eligibility and any explicit human task override.
- An explicit prohibition on nested delegation (including through a shell) and on policy changes.

Use supported structured model/effort fields. If the dispatch needs fresh context, supply the relevant context explicitly. Prompts alone do not configure models or enforce permissions.

## Full handoff: add when relevant

For gated work, repair, or dependencies across workers, include work-item/dependency IDs, reviewed snapshot and Spec/Plan pair, named gate representative and approval scope, canonical issues/aliases and closure criteria, consumed/reserved counters, operating/gate state, original deadline and recorded human continuation authority. Never omit known gates or findings because a follow-up looks simple. Workers cannot reset counters, extend deadlines or launch follow-up cycles. A [controlled reviewer transfer](debate-governance.md#controlled-reviewer-transfer) also carries the recorded unavailability reason/source, old/new reviewer IDs and affected issues/scopes; the independent successor directly reassesses inherited failures on the current version and returns an explicit scoped verdict within the original recovery/deadline allowance.

At L3, the `ct-security` handoff names the exact snapshot Gate 2 will verify, and the `ct-reviewer` handoff includes that report.

Keep context scoped to the assignment. Initial reviewers must not see peers' verdicts before their independent pass. Return findings to Leader; do not recruit votes or start worker debates. Report new evidence or an actionable correction, not a repeated unchanged objection. Leader controls routing and deduplication under [debate governance](debate-governance.md).

When reusing an agent, explicitly change its stage and send the latest plan, ownership and constraints. Stop or await a writer before another agent takes ownership of its files.

## Handoff evidence

Return a concise result with role/variant, work-item and canonical issue IDs, inspected snapshot and paths, findings, changed files, actual checks and remaining limitations. Include the Spec/Plan pair, reviewed scope and explicit verdict for plan reviews. A reassessment states which finding changed and what evidence justifies it; silence is not acceptance.

Record `Effective Model` only from runtime metadata; otherwise record `unverified`. Never copy the Requested Model into the Effective Model field.
