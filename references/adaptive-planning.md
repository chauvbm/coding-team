# Adaptive Spec and Plan review

## Decide whether the task needs both artifacts

Leader assesses complexity, importance, uncertainty, dependencies, compatibility, data impact and reversibility from the actual request and current project. Make this judgment before selecting a planning workflow. A clear, bounded, reversible edit normally needs direct work or a checklist. A focused investigation can resolve an unknown before deciding. File count, a long prompt, the presence of a Markdown file or a risk level alone does not require Spec + Plan.

Choose Spec + Plan when the work benefits from separately stabilizing what must be delivered and how dependent implementation will achieve it. Examples include substantial ambiguous behavior, interacting interface or schema changes, migrations with compatibility constraints, and consequential changes whose acceptance and recovery need explicit agreement. Record a brief reason for the choice. Reassess when new evidence changes the scope. Do not create both documents for every task or skip needed planning to avoid review.

## Enter early and retain ownership

When Leader decides both artifacts are needed, or adopts an existing Spec/Plan pair for the current work, select coding-team immediately, before drafting or presenting the first artifact for approval. Check eligibility first. Explicit user instructions, OFF, SOLO and missing capabilities retain their meaning. Record any actual exception and its scope. A missing reviewer is an unmet condition, never a successful review. A request to implement an already approved design authorizes its agreed scope; do not demand that the user repeat that approval merely to restate this workflow.

Coding-team owns this lifecycle through implementation and integrated verification. Leader personally writes and revises both Spec and Plan. Other agents supply research, technical options, findings and independent review. Leader may use brainstorming and writing-plans as drafting techniques within coding-team ownership, including when a plan template names SDD. Follow existing ownership/handoff rules if another orchestrator already owns active work. Explicit user choice takes precedence.

## Choose reviewers for the actual questions

Before each review, Leader records the artifact authors, selected reviewers, each reviewer's scope, why that expertise is needed and what evidence closes that scope. Assign at least one reviewer to each artifact and cover its material questions across the selected scopes. One focused reviewer can suffice. Every selected reviewer supplying a scoped verdict must be independent of all authors of that artifact. Add specialists when a concrete unresolved question needs them. Record author responses and design acceptance separately from independent review.

For a Spec, useful questions include whether it faithfully captures the original user request, has observable acceptance criteria, resolves contradictory requirements and is feasible under project constraints. For a Plan, inspect coverage of the approved Spec, interfaces, dependencies, implementation order, file ownership, failure handling and validation. Select Architect, Developer, QA, Researcher or a specialist according to these questions. The two stages may use different reviewers. Security review requirements for consequential work continue to apply where required.

For adaptive agreement, include any individual role the user requests and select other reviewers as the work warrants. Apply the three-role gate when the user explicitly requests FULL_TEAM or all three core roles, or when a full-team review has already been established. Preserve prior review responsibilities and unresolved findings. Give each additional reviewer a bounded assignment.

## Review Spec before asking for approval

1. Leader drafts the Spec from the original request, subsequent user decisions and verified project constraints. Record assumptions as proposed decisions.
2. Supply each selected reviewer with those original inputs, the actual Spec version, review scope and acceptance questions. Reviewers independently assess the same version before seeing peers' conclusions. Missing source requirements are a review gap to resolve.
3. Leader consolidates duplicate findings and preserves attribution. Bring each issue to the reviewers whose scopes it affects. Use source evidence, a bounded investigation or a minimal reproduction to resolve disputed factual claims. Bring unresolved product choices to the user for clarification when needed, even before the document is ready for final approval.
4. Leader revises the Spec and obtains reassessment of changed scopes and affected dependencies. Preserve unresolved findings and the original verdicts. Cosmetic edits may carry review forward with a recorded no-impact comparison.
5. Present the actual reviewed Spec for user approval only when selected current reviews accept their scopes and no unresolved BLOCKER or MAJOR remains. Include a concise account of consequential decisions and remaining nonblocking notes. An author summary or vote count cannot supply missing acceptance.
6. Record explicit user approval of that Spec version, or an applicable existing approval of the same content and scope. Draft the implementation Plan after this condition is satisfied. Technical feasibility exploration needed for Spec review can occur earlier.

## Review Plan against the approved Spec

1. Leader writes the Plan and binds it to the approved Spec version. Include implementable tasks, dependencies, ownership, acceptance coverage and relevant checks. An Architect can advise on interfaces and dependencies when useful.
2. Select reviewers for the Plan's actual risks. Give them the approved Spec, original user decisions, current Plan and review scopes. Obtain independent initial verdicts.
3. Leader resolves findings, revises the Plan and obtains affected reassessment using the same procedure as Spec review.
4. When all selected current reviews accept and material findings are resolved, present that Plan for user approval. Record approval bound to both the actual Plan and its approved Spec.
5. Begin covered implementation only after the Spec review, Spec user approval, Plan review and Plan user approval all apply to the current documents, together with any other established gates. Continue with coding-team and independent verification of the stable integrated result.

## Changed and pre-existing documents

A material Spec change found during planning reopens the affected Spec review and user approval, and invalidates dependent Plan conclusions. Revise and review the Spec, obtain renewed user approval for changed scope, then reconcile and review the dependent Plan before asking for Plan approval. A material Plan change reopens its affected reviews and user approval. Preserve unrelated valid evidence with an explicit applicability explanation.

User edits made during approval receive the same impact assessment. Approval of a previous version does not cover newly changed behavior, acceptance or scope. Link approvals to an immutable copy, commit or content hash and version. Compare actual content before carrying acceptance forward. Document renaming, advancing a version label, switching agents and resuming a chat cannot reset obligations.

For imported or previously approved artifacts, inspect the existing evidence first. Reuse applicable review and human approval instead of requesting the same decision again. Complete missing reviews and record applicability to the content already approved. If corrections change approved scope, obtain renewed affected approval. An existing file alone proves neither review nor approval.

## Record and completion conditions

The user-facing approval is a normal conversation. After reviewers accept the corrected artifact, Leader presents that final Spec or Plan for the user to read and approve. A clear response such as "agreed" or "implement" approves the artifact and scope actually presented in that context. Leader records the reference internally. No approval form, signature, version entry or bookkeeping is required from the user. A question or requested correction is not acceptance.

Keep a compact Leader-owned planning record with the work, outside public source when it contains private conversation references. Simple direct/checklist tasks need no planning record. For this lifecycle, persist enough information to resume safely:

- Assessment and reason for choosing Spec + Plan, orchestrator, eligibility and any explicit user exceptions.
- Original request and user decisions or their retrievable references.
- Spec and Plan paths, versions and content identities, with the Plan's approved Spec dependency.
- Authors, selected reviewers, scope and selection rationale for each artifact.
- Findings with IDs, evidence, severity, owner, closure criteria and reassessment outcomes.
- Current scoped review verdicts, user approval references and their exact artifact bindings.
- Current stage and pending next action, preserving cumulative repair/review history.

Use explicit stage results such as Spec review PASS/HOLD, Spec user approval PENDING/APPROVED, Plan review PASS/HOLD and Plan user approval PENDING/APPROVED. A stage can pass only with the evidence required above. A user-approved waiver must name the affected requirement and scope and remain visible in the record. Apply the shared debate-governance rules to stalled discussions, repairs and reviewer transfers, honoring actual user continuation authority and quota pauses.

The optional JSON task ledger checks its existing declared Plan and delivery responsibilities. Once a complete pair exists, `gated: false` with nonempty `required_reviews` can track a selected Plan-review roster. The field `gated: true` retains the legacy three-core FULL_TEAM requirement. The helper also requires QA evidence for material issue closure. Its `gate: PASS` does not check these two user approvals or the separate Spec review stage. Keep the planning record and check all lifecycle conditions before implementation. Instructions and caller-authored records are behavioral controls, and do not intercept runtime tool calls or authenticate human approval.
