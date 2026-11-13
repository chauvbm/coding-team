# Orchestrator routing and ownership

Each phase of a task has exactly one primary orchestrator. A task can pass through several phases (for example a parallel investigation, then coding-team implementation), but only one orchestrator acts at a time.

Routing is **ADVISORY** with checks. coding-team does not modify Superpowers and installs no hooks: the Leader reads this policy, picks the orchestrator, tells the user in one line and records the choice, and coding-team checks ownership before it takes a task. Nothing here prevents a second orchestrator from starting on its own; never report that parallel orchestrators were made impossible.

## Routing table

Apply top to bottom and stop at the first match. An explicit user choice of orchestrator always wins, unless safety policy forbids it.

| # | Condition | Orchestrator |
| --- | --- | --- |
| 1 | L3: meaningful security, data-integrity, production or irreversible consequences | coding-team (unless the user chose otherwise and safety policy allows it) |
| 2 | An architecture conflict or design choice needs challenge, or the user asks for critique or consensus | coding-team |
| 3 | The change spans several interface contracts, data schemas or APIs between modules | prefer coding-team |
| 4 | Several independent problems need investigation | dispatching-parallel-agents (DPA), investigation only; then a handoff point and a fresh pass over this table to pick the implementer |
| 5 | A plan exists, scope is clear and sequential review after each task is enough | subagent-driven-development (SDD) |
| 6 | A small, low-impact task (L0 or bounded L1) | Leader works directly and may use technique skills |

File count alone is never the criterion. Renaming one symbol across 10 files can still be row 6; fixing authentication in 2 files is still row 1.

Without Superpowers available in the session, rows 4 and 5 go to coding-team AUTO. The existing modes (AUTO, SOLO, PLAN_ONLY, REVIEW_ONLY, FULL_TEAM, STRICT_MODEL, USER_APPROVAL_REQUIRED) keep their meaning when coding-team orchestrates. AUTO no longer takes L1 by itself: L1 belongs to the Leader working directly unless the user asks for coding-team.

### Deterministic `route`

`orchestrator route` applies the table to facts the Leader has already classified. The model still classifies the facts, so routing as a whole stays ADVISORY; `route` only removes table-reading mistakes.

```json
{"level": "L2", "user_choice": null, "safety_forbids_user_choice": false,
 "architecture_dispute": false, "multi_contract_change": false, "has_plan": true,
 "sequential_review_sufficient": true, "superpowers_available": true, "independent_issues": 0}
```

- All nine facts are required. `level` is `L0`..`L3`; `user_choice` is null or one of `leader-direct`, `coding-team`, `superpowers-sdd`, `superpowers-dpa`, `superpowers-executing-plans`; `independent_issues` is an integer >= 0; the rest are booleans.
- Output: `{"orchestrator", "rule", "reason"}`. Rule 0 is the user's choice (skipped when `safety_forbids_user_choice` is true). Rules 1-6 are the table; row 4 needs `independent_issues >= 2`, row 5 needs `has_plan` and `sequential_review_sufficient`. Rule 7 is the fallback: an unmatched L2 goes to coding-team (an unmatched L0/L1 is rule 6).
- `superpowers_available` is the Leader's judgment from the skills actually listed in this session. `doctor` reports `installed` and `enabled`, but `doctor` cannot know `available_in_session`.

## Skill classes

| Class | Skills | Who may use them |
| --- | --- | --- |
| Orchestrator | coding-team, subagent-driven-development, dispatching-parallel-agents, executing-plans | Leader only, one per phase |
| Phase | brainstorming, writing-plans, using-git-worktrees, finishing-a-development-branch | Leader only |
| Technique | test-driven-development, systematic-debugging, verification-before-completion, receiving-code-review | Leader and every worker |
| Forbidden to workers | requesting-code-review (it spawns a reviewer) and every orchestrator skill | nobody below the Leader |

Superpowers workers stop at `SUBAGENT-STOP` and do not invoke skills on their own, so a coding-team handoff names the technique skills the worker may use. The worker's ban on spawning agents is ENFORCED only where the runtime allowlist supports it (see [agent contracts](agent-contracts.md)); elsewhere it is ADVISORY.

## coding-team keeps the whole task

When coding-team orchestrates, it keeps the task from Gate 1 through implementation to Gate 2. Do not hand implementation to SDD: SDD's "decide and keep going" behavior would pass through HOLD gates, the file-ownership matrix and same-snapshot verification. coding-team may use a Superpowers spec or plan (`docs/superpowers/specs|plans/...`) from brainstorming or writing-plans as its Spec/Plan input to Gate 1.

## Handoff points and record

Change orchestrator only at a handoff point: SDD stops for one of its own stop reasons; coding-team stops at STOP_HOLD or NEEDS_USER_DECISION; DPA finishes its investigation; or the user asks for a change. The new orchestrator inherits the open findings; it does not start from zero.

The handoff record (`--input` JSON) needs every field below; the CLI rejects missing or malformed ones.

| Field | Content |
| --- | --- |
| `task_id`, `reason`, `from`, `to` | Task, why, old and new orchestrator (different, from the list above) |
| `workers` | Every worker of the old phase as `{"id", "terminated": true, "evidence"}`. A worker still running blocks the handoff |
| `snapshot` | `{"head", "uncommitted": [...], "untracked": [...]}` |
| `tests` | `{"commands": [...], "result", "snapshot"}`: latest commands, result and the snapshot they ran on |
| `findings` | Open findings as a list |
| `previous_ledger` | Path of the old orchestrator's ledger or workspace, or null |
| `acknowledged_sdd` | Unresolved SDD workspaces the user has acknowledged (paths exactly as `status` prints them) |

Termination evidence is caller-asserted: verify with runtime tools before recording. The CLI cannot see other processes; it stores the claim and returns `termination_evidence: "caller-asserted"`. Check each worker with the runtime's own agent/task listing before writing `terminated: true`.

- Task **with** a record: the caller must be the current owner (token or matching session id, below) and `from` must equal the recorded orchestrator. The result has `from_verified: true`.
- Task **without** a record (for example SDD, which writes none): `user_confirmed: true` (`--user-confirmed`) is mandatory and the result has `from_verified: false`. A user-confirmed handoff into coding-team is the user's override for the unresolved SDD workspaces it acknowledges; ask the user before running it.
- Every handoff result returns the newly issued token for the new owner and increments `generation` when a record existed; the previous owner's token stops working.

## Ownership state

State lives in `<main worktree>/.coding-team/orchestration.json`, where the main worktree is the first entry of `git worktree list --porcelain`. All worktrees of one repository share it, so ownership is per repository, not per worktree. It is never inside `.git/` (agents are blocked from writing there). Outside git it is `<workspace>/.coding-team/`. The directory gets a `.gitignore` containing `*`. The file is separate from the task ledger format.

The location fails closed: only when git is missing, or git says "not a git repository" and no `.git` entry exists in the workspace or an ancestor, does it fall back to the workspace. Any other git failure (dubious ownership, timeout) is an error, and a bare main worktree is unsupported. Fix git rather than guessing a per-worktree file.

Before claiming, coding-team checks:

- **Unresolved SDD workspaces:** any `.superpowers/sdd/*/progress.md` in any worktree. This is unfinished progress, not a running process: it is not evidence that an SDD session is active. Result: HOLD and ask the user whether to continue SDD or hand off.
- **An existing record for the task** held by another session or orchestrator.

If either applies, do not take the task; report it and require a handoff point. This is a coding-team-side check only; it cannot stop SDD from starting by itself.

### Owner token and session

- `claim` creates the record with a random `owner_token` and records `owner_session_id` (the trusted chat id from the platform's `chat_id_env` or `--session-id`; null when none exists). The token appears only in the result that creates it (`claim`, `takeover`, `handoff`) and in `orchestration.json`; `status` never shows it (`has_token` only).
- Updating (`claim` again with new mode/phase), handing off or releasing requires the token, or a non-null `owner_session_id` equal to the current trusted session id. The session match keeps ownership across context compaction.
- Keep the token in the Leader's context. Never write it to any other file: not the ledger, a report, a handoff `--input` file or the rules file. Pass it with `--token`.
- A different session without the token gets HOLD. Nothing expires automatically.
- Recovering a task from a lost session uses `takeover`, which requires `--user-confirmed` from the user's own message and a `--reason`. It issues a new token, increments `generation` and keeps the former owner in `history`.
- `can_claim` in `status` means "claimable by a new session": it is false whenever a record for the task exists, even when you own it.
- `release` deletes the record together with its history. Copy anything you still need into the ledger or report first.
- Limit: the token prevents collisions between sessions that follow this process. It does not stop a process that deliberately reads the file.

## CLI

Every command prints one JSON object. Exit 0 = success, 1 = `status` with `can_claim: false`, 2 = error or conflict (with `blockers`). Use `--workspace` (default: current directory, must exist) and `--platform` when the platform cannot be detected.

```text
python <skill dir>/scripts/coding_team.py orchestrator route    --input FACTS.json
python <skill dir>/scripts/coding_team.py orchestrator status   [--task-id ID]
python <skill dir>/scripts/coding_team.py orchestrator claim    --task-id ID --mode AUTO --phase implementation [--token T]
python <skill dir>/scripts/coding_team.py orchestrator handoff  --input HANDOFF.json [--token T] [--user-confirmed]
python <skill dir>/scripts/coding_team.py orchestrator takeover --task-id ID --reason TEXT --user-confirmed
python <skill dir>/scripts/coding_team.py orchestrator release  --task-id ID [--token T]
```

`--session-id` overrides the session id read from the platform's chat id variable; pass only a trusted runtime id, never a guessed one.

## Without Superpowers

If Superpowers is not available in the session, route with `superpowers_available: false`: rows 4 and 5 become coding-team AUTO, and the orchestrator and phase skills above are simply absent. Technique work is done under the Leader's or the worker's own instructions.
