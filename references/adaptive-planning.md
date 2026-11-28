# Adaptive Spec and Plan review

## Decide which artifacts the task needs

Leader assesses complexity, importance, uncertainty, dependencies, compatibility, data impact and reversibility from the actual request and current project. Make this judgment before selecting a planning workflow. A clear, bounded, reversible edit normally needs direct work or a checklist. A focused investigation can resolve an unknown before deciding. File count, a long prompt, the presence of a Markdown file or a risk level alone does not require Spec + Plan.

Choose Spec + Plan when the work benefits from settling what must be delivered before planning how to implement it. Examples include substantial ambiguous behavior, interacting interface or schema changes, migrations with compatibility constraints, and consequential changes whose acceptance and recovery need explicit agreement. Record a brief reason for the choice. Reassess when new evidence changes the scope. Apply this workflow to each formal artifact used for complex work, including a standalone Spec or Plan. If one document holds both, close Spec review and approval before developing the implementation Plan in it. Base a standalone Plan on the authorized requirements.

## Enter early and retain ownership

Select coding-team before drafting or presenting a formal artifact for complex work, including when adopting one. Check eligibility first. Explicit user instructions, OFF, SOLO and missing capabilities retain their meaning. Record any actual exception and its scope. A request to implement an already approved design authorizes its agreed scope.

Coding-team owns this lifecycle through implementation and integrated verification. Leader personally writes and revises both Spec and Plan. Other agents supply research, technical options, findings and independent review. When using brainstorming or writing-plans as drafting techniques, use this lifecycle's approval and ownership rules even if a template calls for a user sign-off or SDD. Follow existing ownership and handoff rules if another orchestrator already owns active work. Explicit user choice takes precedence.

## Choose reviewers for the actual questions

For **each** Spec and **each** Plan, require completed reviews from **at least two distinct independent subagents**. Record each reviewer's identity, scope, relevant expertise and closure criteria. The roster covers all material questions. Roles may differ by artifact and reviewers may serve on both stages if independent of the author and reviewing each artifact separately. Leader and anyone who helped author that artifact cannot supply independent review.

Native subagents and [eligible external Desktop workers](control-policy.md#external-workers) can serve in the same roster. Count the agent that examined the artifact and issued a scoped verdict. A native representative relaying a Desktop verdict gives no additional review. Count each agent once per artifact. If a reviewer becomes unavailable, use an eligible native or Desktop replacement under the existing recovery rules. Keep the artifact on HOLD while the minimum or any required acceptance is missing. Proceed with authorized investigation or corrections while waiting.

For a Spec, useful questions include whether it faithfully captures the original user request, has observable acceptance criteria, resolves contradictory requirements and is feasible under project constraints. For a Plan, inspect coverage of the approved Spec, interfaces, dependencies, implementation order, file ownership, failure handling and validation. Select Architect, Developer, QA, Researcher or a specialist according to these questions. The two stages may use different reviewers. Security review requirements for consequential work continue to apply where required.

For adaptive agreement, include any individual role the user requests and select other reviewers as the work warrants. Apply the three-role gate when the user explicitly requests FULL_TEAM or all three core roles, or when a full-team review has already been established. Preserve prior review responsibilities and unresolved findings. Give each additional reviewer a bounded assignment.

## Review the Spec

1. Leader drafts the Spec from the original request, subsequent user decisions and verified project constraints. Record assumptions as proposed decisions.
2. Supply each reviewer with those original inputs, the exact Spec version, the review scope and acceptance questions. Reviewers independently assess the same version before seeing peers' conclusions. Resolve missing source requirements before closing review.
3. Leader consolidates findings and routes disagreements to the affected reviewers. Use source evidence, a bounded investigation or a minimal reproduction to test disputed claims. Seek user input if resolving a material choice requires new scope or architecture authorization, or the authorized intent is genuinely unclear.
4. Leader revises the Spec and obtains reassessment of changed scopes and affected dependencies. Keep findings, original verdicts and reassessment evidence. For a material revision, carry an unaffected verdict forward only with a recorded comparison and the reviewer's confirmation that it still applies. Cosmetic edits need a recorded no-impact comparison.
5. Once the Spec meets the agreement and approval conditions below, draft the dependent Plan if needed. Feasibility exploration for the Spec can occur earlier.

## Review Plan against the approved Spec

1. Leader writes the Plan and binds it to the approved Spec version when one exists. Include implementable tasks, dependencies, ownership, acceptance coverage and relevant checks.
2. Give the Plan reviewers the approved Spec if applicable, original user decisions, exact Plan version and review scopes. Obtain at least two independent initial verdicts for the Plan itself.
3. Leader resolves findings, revises the Plan and obtains affected reassessment using the same procedure as Spec review. Reopen the Spec if a correction materially changes it.
4. Begin the authorized implementation as soon as the current Plan and its Spec, if applicable, meet the agreement and approval conditions below and any other established gates. Continue with coding-team and independent verification of the stable integrated result.

## Record agreement and decide who approves

Pass review only when at least two independent subagents have supplied current scoped acceptance, all selected reviewers or their valid successors accept their scopes, and all BLOCKER and MAJOR findings are resolved. Use [bounded review and reassessment](debate-governance.md) to resolve remaining dissent. Silence, a majority vote or Leader's rebuttal cannot supply a required verdict.

Evaluate the reviewed artifact against the user's actual authorization at **each stage**. Apply the first matching row.

| Condition | Approval and next action |
| --- | --- |
| Proposes scope expansion or a major architectural change without applicable user authorization | Present the reviewed artifact, the specific new decision and its impact. Wait for explicit user confirmation before advancing the affected work. |
| The user explicitly requires approval at this stage | Honor that approval requirement and record the user's decision. |
| Fits the authorized scope and architecture | Leader records approval of the reviewed version and immediately takes the next authorized step. |

Scope expansion changes the agreed deliverables, behavior or acceptance beyond what the user authorized. Major architecture changes include replacing core storage, moving ownership across system boundaries, or substantially changing the deployment or trust model. Internal implementation details within the agreed architecture normally fall under Leader approval. When the user already authorized the specific decision, record that authority and carry it forward where it applies.

User approval uses normal conversation. A clear reply such as “đồng ý” or “implement” approves the decision and artifact presented in that context. Leader records the authorization and current artifact version. Questions and requested corrections require follow-up before treating the decision as approved.

## Changed and pre-existing documents

A material Spec change reopens affected review and approval and invalidates dependent Plan conclusions. Once the corrected Spec is reviewed and approved, reconcile and review the dependent Plan. A material Plan change reopens its affected review and approval. Reapply the decision above after reassessment. Preserve unrelated valid evidence with an explicit applicability explanation.

User edits made during approval receive the same impact assessment. Approval of a previous version does not cover newly changed behavior, acceptance or scope. Link approvals to an immutable copy, commit or content hash and version. Compare actual content before carrying acceptance forward. Document renaming, advancing a version label, switching agents and resuming a chat cannot reset obligations.

For imported or previously approved artifacts, inspect the existing evidence first. Carry forward applicable review and approval, obtain any missing independent verdicts, and apply the current approval decision to substantive corrections.

## Planning record

Keep a compact Leader-owned planning record with the work, outside public source when it contains private conversation references. Simple direct/checklist tasks need no planning record. For this lifecycle, persist enough information to resume safely:

- Assessment and reason for choosing the artifacts, orchestrator, eligibility and any explicit user exceptions.
- Original request and user decisions or their retrievable references.
- Spec and Plan paths, versions and content identities, with the Plan's approved Spec dependency.
- Authors, selected reviewers, scope and selection rationale for each artifact.
- Findings with IDs, evidence, severity, owner, closure criteria and reassessment outcomes.
- Current scoped review verdicts, approval actor and rationale, applicable user authorization and their exact artifact bindings.
- Current stage and pending next action, preserving cumulative repair/review history.

For each artifact, record review PASS/HOLD and approval PENDING/APPROVED with the responsible actor. A user-approved waiver must name the affected requirement and scope and remain visible in the record. Apply shared debate-governance rules to stalled discussions, repairs and reviewer transfers, honoring actual user continuation authority.

The optional JSON task ledger checks its existing declared Plan and delivery responsibilities. Use `gated: false` with nonempty `required_reviews` to track supported Plan-review responsibilities. The field `gated: true` retains the three-core FULL_TEAM requirement. The helper also requires QA evidence for material issue closure. Verify the two-reviewer minimum for each artifact and the applicable approvals from the planning record before implementation, even when the helper reports `gate: PASS`. Instructions and caller-authored records do not intercept runtime tool calls or authenticate human approval.
