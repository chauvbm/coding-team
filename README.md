# Coding Team

Repository: [chauvbm/coding-team](https://github.com/chauvbm/coding-team).

A skill for proportionate delegation, coordinated implementation and independent verification on three coding-agent platforms: **Claude Code**, **Codex** and **Antigravity**. The Leader (the main chat agent) picks the smallest useful workflow, dispatches role-scoped native subagents (`ct-architect`, `ct-researcher`, `ct-developer`, `ct-test-engineer`, `ct-reviewer` / `ct-reviewer-verify`, `ct-security`) and owns integration and the final result. Small tasks stay with the Leader; substantial work gets bounded workers within the host's actual capacity and the [governance defaults](references/governance-defaults.json). When Superpowers is installed, coding-team routes each task phase to exactly one orchestrator.

This README is a guide for users, maintainers and reviewers. [SKILL.md](SKILL.md) is the operational entrypoint and its linked references define the workflow; the README adds no authority, approvals or competing rules. Licensed under [MIT](LICENSE).

When a task needs separate Spec and Plan artifacts, use [adaptive planning](references/adaptive-planning.md): the Leader writes both, independent review is scoped to each artifact, and the reviewed Spec is approved before planning implementation. This process does not impose a fixed reviewer roster; runtime behavior remains platform-specific and instructions are ADVISORY.

## Install

Requires Git and Python 3.11 or newer (standard library only). Installation is done by one CLI, `scripts/coding_team.py`, run from a source checkout. Clone the repository to a **source directory of your choice** (not into the skill directory itself): `install` copies the skill, the rendered role agents and the platform config into the platform's own locations, transactionally, with backups and a SHA-256 manifest.

Always preview first: `install --dry-run` lists every path it would write, back up or remove, plus conflicts, and creates nothing (no file, directory, lock, staging or backup). Then run the real install. Options:

- `--default on|off` sets the global ON/OFF default (state is user-level). Without it, existing state is kept; a new state starts ON.
- A user-scope install writes the rules block by default, so the Leader uses coding-team automatically on Claude Code, Codex and Antigravity; pass `--no-global-rules` to skip it. The block (`--global-rules`, now the default) is a marked block (`<!-- coding-team-policy:start -->` ... `<!-- coding-team-policy:end -->`) into the platform's global rules file so the Leader handles `/coding-team` controls and routing even before selecting the skill. Bytes outside the markers are never changed. The block embeds the Python executable used at install time (`sys.executable`); re-run `install --global-rules` after changing or removing that Python or virtual environment. On Windows it carries both a PowerShell form and a Git Bash / POSIX form of each command.
- `--scope project --project-dir DIR` installs the skill and agents into one project. It uses the user-level install lock and backups. Codex also registers the project manifest beside the user-level policy file. Project scope cannot be combined with `--default` or an explicit `--global-rules`. Run a user-scope install to initialize the policy file for `/coding-team on|off`, including `--global`.

Every CLI call prints exactly one JSON object; exit 0 = success, 1 = valid but negative, 2 = error or conflict. Start a fresh chat after installing so the platform loads the new skill and agents. The examples use `python`; use your actual interpreter (for example `python3`), and in PowerShell quote paths with spaces and use the call operator (`& 'C:\path\python.exe' 'C:\src\coding-team\scripts\coding_team.py' ...`).

### Claude Code

Installs into `CLAUDE_CONFIG_DIR` (default `~/.claude`): skill in `skills/coding-team`, agents in `agents/`, rules block in `CLAUDE.md`, state in `coding-team/state.json`.

```sh
git clone https://github.com/chauvbm/coding-team.git ~/src/coding-team
python ~/src/coding-team/scripts/coding_team.py install --platform claude-code --dry-run
python ~/src/coding-team/scripts/coding_team.py install --platform claude-code --default on
```

Optional documentation MCP for `ct-researcher` (Claude Code only, repeatable). For example, with the Context7 MCP server configured in Claude Code:

```sh
python ~/src/coding-team/scripts/coding_team.py install --platform claude-code --global-rules \
  --docs-mcp mcp__context7__resolve-library-id --docs-mcp mcp__context7__query-docs
```

Without `--docs-mcp`, `ct-researcher` looks documentation up with `WebFetch`/`WebSearch` only and keeps ENFORCED no-write and no-delegation guarantees. Granting any MCP tool makes `ct-researcher` guarantees ADVISORY: a tool name proves nothing about what the server can do, so even a documentation server is not treated as read-only.

### Codex

Installs the skill into `~/.agents/skills/coding-team` and agents, rules block (`AGENTS.md`) and state (`coding-team/state.json`) under `CODEX_HOME` (default `~/.codex`).

```sh
git clone https://github.com/chauvbm/coding-team.git ~/src/coding-team
python ~/src/coding-team/scripts/coding_team.py install --platform codex --dry-run
python ~/src/coding-team/scripts/coding_team.py install --platform codex --default on
```

On Codex the commands may also be written with the `$coding-team` prefix. The installed `agents/openai.yaml` keeps implicit invocation enabled; leave it that way so the ON/OFF controls stay reachable while coding-team is OFF. Codex role files carry no model (`static_agent_model: omit`): the Leader passes the tier's model at spawn, because a model in the agent TOML would win over it.

#### Antigravity Desktop worker on Windows

Install the separate worker skill into Antigravity Desktop. Open the same Git worktree in both apps. Antigravity uses the account, model and permissions selected in its own app.

```sh
python ~/src/coding-team/scripts/coding_team.py install --platform antigravity --desktop-worker --dry-run
python ~/src/coding-team/scripts/coding_team.py install --platform antigravity --desktop-worker
```

1. In the Codex chat you want as Leader, send `/coding-team agy auto on`. Codex selects that chat and opens an invitation for the active worktree.
2. Send `/coding-team listen` in each Antigravity chat you want in the team on that worktree. Codex reports each chat's readiness. The Leader chooses which workers to use, assigns the same coding-team roles as for native agents, and sends corrections back to the original chat.

Use `/coding-team agy on` for manual delivery on first activation, or `/coding-team agy auto off` to select manual delivery explicitly. In either app, `/coding-team status` shows the current role and connection, and `/coding-team help` lists the commands. Each worktree has one inter-app Leader. To move the role, use the intended activation command in the new Codex chat after the current coordination round closes. Follow the [Desktop worker guide](references/antigravity-desktop.md) for readiness, result collection and recovery.

### Antigravity

Installs under `~/.gemini`: skill in `config/skills/coding-team`, agents in `config/agents`, rules block in `config/rules/coding-team-policy.md`, state in `antigravity/coding-team/state.json`.

```sh
git clone https://github.com/chauvbm/coding-team.git ~/src/coding-team
python ~/src/coding-team/scripts/coding_team.py install --platform antigravity --dry-run
python ~/src/coding-team/scripts/coding_team.py install --platform antigravity --default on
```

### Runtime-only package

Instead of installing from a full clone, a maintainer can build a smaller ZIP with `python scripts/build_runtime_package.py [--platform P] --output ../coding-team.zip`. It contains `LICENSE`, `SKILL.md`, `agents`, `platforms`, `references` and `scripts`, plus a hash manifest, under `coding-team/`. The full package and the Antigravity package also include the separate Desktop worker skill. Extract the ZIP and run `install` from the extracted `coding-team/scripts/coding_team.py`. Use the full package or the corresponding platform package to install each app's component. See [runtime packaging](CONTRIBUTING.md#runtime-only-installation-package).

### Conflicts, recovery and uninstall

`install` never overwrites silently:

| Situation | Default | Flag that allows it |
| --- | --- | --- |
| A file coding-team installed was edited by you (hash differs from the manifest) | conflict, exit 2, nothing written | `--overwrite-modified` (backed up first) |
| A target exists but coding-team does not manage it (for example an old git clone) | conflict, exit 2, nothing written | `--replace-unmanaged` (backed up first, each path listed) |
| A symlink, junction or reparse point below the managed roots | refused | none; a link at or above the config root (a location you chose) is allowed |

Neither flag skips any other check. Backups go to `backups/<timestamp>/` beside the state file and are kept after success, rollback or recovery. A normal error rolls the whole transaction back. If the process is killed mid-install, a journal remains and the next `install`/`uninstall` refuses to run until you recover:

```sh
python ~/src/coding-team/scripts/coding_team.py install --platform <p> --recover
```

`--recover` takes only `--platform`, `--scope` and `--project-dir`. It verifies every backup hash first (a missing or mismatched backup restores nothing and keeps the journal), restores each target, and does not overwrite or delete files you changed after the crash (they are listed in `kept_unexpected`).

`uninstall --platform <p> [--dry-run] [--remove-modified]` removes unmodified managed files, keeps files you edited unless `--remove-modified` (backed up first), removes the rules block only inside its markers and keeps the state file.

### Check the installation: `doctor`

```sh
python ~/src/coding-team/scripts/coding_team.py doctor --platform <p>
```

`doctor` is read-only. It reports whether the installation matches its manifest (modified or missing files, an unfinished journal), each agent's presence, tier, written model and guarantees (ENFORCED/ADVISORY), `model_availability: unverified`, active force-model variables, state health, the rules block and Superpowers `installed`/`enabled`. Whether Superpowers is available in a given session is decided by the Leader from that session's real skill list, not by `doctor`. An installed copy can run `doctor` without `--platform`; it reads its own manifest.

<!-- legacy-migration -->
### Migrating from the old Codex install

The previous release was a Codex-only git clone in `~/.agents/skills/coding-team` (repository formerly `coding-team-codex`), with `scripts/coding_team_policy.py` and the `/coding team ...` command syntax.

- **The old clone needs `--replace-unmanaged`.** It has no manifest, so `install --platform codex` reports its files as unmanaged and stops. Clone the new source elsewhere, preview with `--dry-run`, then rerun with `--replace-unmanaged`; each replaced path is backed up and listed. Files of the old clone that the new install does not write (for example `.git`, tests or the old helper) stay in place; to start from a clean directory, move the old clone aside before installing.
- **The old rules block is replaced.** `--global-rules` reuses the same markers in `AGENTS.md`, so the old block (which still lists the old commands) is replaced by the new one; bytes outside the markers are unchanged.
- **Old state is kept.** State schema v1 is unchanged, so existing global and per-chat ON/OFF settings keep working.
- **Old commands are rejected with a hint:** `/coding team on` / `/coding team off` become `/coding-team on` / `off`; `/coding team on all` / `/coding team off all` become `on --global` / `off --global`; `/coding team status` becomes `/coding-team status`; `/coding team inherit` becomes `/coding-team reset`.
- `scripts/coding_team_policy.py` and `scripts/check_model_profiles.py` are superseded by `scripts/coding_team.py` (the old files remain in an unmoved clone); `references/model-profiles.json` is superseded by `platforms/<platform>.json`.
<!-- /legacy-migration -->

## Commands

Typed in chat and handled by the Leader with `scripts/coding_team.py` (prompt conventions, not native slash commands or runtime enforcement). See [control policy](references/control-policy.md).

| Command | Effect |
| --- | --- |
| `/coding-team status` | Show global, chat and effective policy, plus the platform |
| `/coding-team on` | Turn coding-team ON for this chat (needs a trusted chat id) |
| `/coding-team off` | Turn coding-team OFF for this chat (needs a trusted chat id) |
| `/coding-team on --global` | Set the global default to ON; chat overrides are kept |
| `/coding-team off --global` | Set the global default to OFF; chat overrides are kept |
| `/coding-team reset` | Remove this chat's override so it follows the global default |
| `/coding-team help` | List the commands |

Chat policy overrides global. Without a trusted chat id the chat is `UNKNOWN`: `status` still shows the global default, chat commands fail and nothing is dispatched automatically. An explicit human request can authorize coding-team for its own task; SOLO still prevents delegation. Controls never reset task history, approve plans or waive gates.

Then ask, for example:

> Use coding-team to repair this bug with proportionate delegation and independent verification. Preserve unrelated changes.

## Guarantees per platform

Each rendered agent states its guarantees, and `doctor` reports the same matrix. ENFORCED appears only where a runtime mechanism exists; ADVISORY means the worker is instructed, not prevented. Never treat ADVISORY as a safety guarantee.

| Guarantee | Claude Code | Codex | Antigravity |
| --- | --- | --- | --- |
| No file writes for `ct-architect`, `ct-researcher`, `ct-reviewer`, `ct-security` | ENFORCED (tool allowlist without write, shell or agent tools) | ADVISORY (subagents inherit the parent sandbox) | ENFORCED (tool allowlist, command execution off) |
| No delegation for those read-only roles | ENFORCED (no `Agent` tool) | ADVISORY | ENFORCED (no subagent tools) |
| `ct-researcher` with `--docs-mcp` | ADVISORY for both | n/a | n/a (`inheritMcp: false`) |
| No delegation for `ct-developer`, `ct-test-engineer`, `ct-reviewer-verify` | ADVISORY | ADVISORY | ADVISORY |
| `ct-reviewer-verify` read-only behavior and command limits | ADVISORY | ADVISORY | ADVISORY |
| Writing only assigned files / only tests | ADVISORY | ADVISORY | ADVISORY |
| Model used by a worker | requested model recorded; effective model only from runtime metadata, else `unverified` | same | same |
| Orchestrator routing and ownership checks | ADVISORY | ADVISORY | ADVISORY |

Shell access makes no-delegation ADVISORY: a shell can start another agent or AI CLI, so roles with a shell are told not to, but nothing stops them. Granting any MCP tool, `browser_testing` or inherited MCP likewise rules out ENFORCED. A model name that is valid in `platforms/<platform>.json` does not mean your account can use it; see [model routing](references/model-routing.md).

## Routing with Superpowers

Each task phase has exactly one primary orchestrator. First match wins; an explicit user choice wins unless safety policy forbids it:

1. L3 (security, integrity, production or irreversible consequences): coding-team.
2. Architecture conflict, or the user asks for critique/consensus: coding-team.
3. Several interface contracts, schemas or APIs change: prefer coding-team.
4. Several independent problems to investigate: Superpowers dispatching-parallel-agents (investigation only), then route again.
5. Plan exists, scope clear, sequential review suffices: Superpowers subagent-driven-development.
6. Small, low-impact (L0 or bounded L1): the Leader works directly.

An L2 task that matches no earlier condition goes to coding-team. File count alone is never the criterion. Without Superpowers in the session, rows 4 and 5 go to coding-team AUTO. `orchestrator route --input FACTS.json` applies the table deterministically to facts the Leader classified; `orchestrator status|claim|handoff|takeover|release` keep per-repository ownership in `<main worktree>/.coding-team/orchestration.json`. Routing remains ADVISORY: nothing prevents another orchestrator from starting by itself. Details, handoff records and owner tokens: [orchestration](references/orchestration.md).

## Verified platforms

| Platform | Runtime smoke test | Runtime version | Date |
| --- | --- | --- | --- |
| Claude Code | not yet verified | - | - |
| Codex | not yet verified | - | - |
| Antigravity | not yet verified | - | - |

This table is filled only from a published smoke record, `evaluations/<date>-platform-smoke/release.json` (per-platform results in `evaluations/<date>-platform-smoke/<platform>.json`), produced with the [platform smoke kit](docs/smoke/platform-smoke.md). Passing unit tests and a valid platform config do not verify a runtime.

## Known limitations

- `browser_testing` has no provider-detection config, and the unsafe Playwright tools (`browser_run_code_unsafe`, `browser_file_upload`) are instructed against in `ct-test-engineer`, not disabled at runtime. The guarantee stays ADVISORY.

## Repository map

| File | Purpose |
| --- | --- |
| [SKILL.md](SKILL.md) | Entry point: routing, levels, modes, roles and essential gates |
| [orchestration.md](references/orchestration.md) | Orchestrator routing, ownership state, handoff and tokens |
| [workflow.md](references/workflow.md) | Work selection, dependencies, ownership and integration |
| [adaptive-planning.md](references/adaptive-planning.md) | Scoped Spec and Plan review and approval sequence |
| [agent-contracts.md](references/agent-contracts.md) | Roles, actual guarantees and handoff contents |
| [model-routing.md](references/model-routing.md) | Preferred Tier, Requested Model, Effective Model |
| [debate-governance.md](references/debate-governance.md) | Issues, evidence, counters, termination and approvals |
| [quality-safety.md](references/quality-safety.md) | Verification, recovery and accurate reporting |
| [control-policy.md](references/control-policy.md) | Commands, state, chat identity and the CLI |
| [antigravity-desktop.md](references/antigravity-desktop.md) | Pairing, handoff and the native representative |
| [task-ledger.md](references/task-ledger.md) | Optional durable task records and readiness |
| [governance-defaults.json](references/governance-defaults.json) | Single source of numeric defaults |
| [roles.json](agents/roles/roles.json) | Role capabilities; contracts in `agents/roles/ct-*.md` |
| `platforms/<platform>.json` | Paths, tiers, role tiers and tool mapping per platform ([claude-code](platforms/claude-code.json), [codex](platforms/codex.json), [antigravity](platforms/antigravity.json)) |
| [coding_team.py](scripts/coding_team.py) | Unified CLI: install, uninstall, status, command, doctor, check-config, orchestrator |
| [task_ledger.py](scripts/task_ledger.py) | Atomic ledger updates and readiness checks |

Generated `graphify-out/` files are local development aids and not needed to review or run the skill.

## Checks you can run locally

From the repository root:

```text
python -m unittest discover -s tests -p "test_*.py" -v
python scripts/coding_team.py check-config --platform claude-code
git diff --check
```

The suite covers the CLI, installer transactions and recovery, policy state, rules blocks, platform configs, role rendering, orchestration ownership and routing, the response checker and the task ledger. It does not execute agents on a real runtime; that is what the [platform smoke kit](docs/smoke/platform-smoke.md) is for. When testing the CLI by hand, use an isolated `--state` file and an explicit `--chat-id`, never your real state or chat identity.

## Independent behavioral evaluation

The decision fixture is [orchestration_cases.json](tests/orchestration_cases.json): `instructions`, scenario `input` and expected `checks` for the orchestrator-routing and gate cases.

A separate coordinator prepares blind inputs with the exporter, which copies `SKILL.md`, `references`, `scripts`, `agents` and `platforms` and one input file per case, omitting tests, expected checks and published results:

```text
python scripts/export_evaluation_inputs.py --cases tests/orchestration_cases.json
```

`--output` must name a new directory outside this repository. Give a fresh evaluator only the exported skill and its inputs, never the checks, the scorer or earlier verdicts; copying files is not a sandbox. Score the returned JSON arrays from the original repository:

```text
python tests/check_behavioral_results.py --cases tests/orchestration_cases.json path/to/responses.json
```

The checker exits 0 when every case is present and passes, 1 on decision failures. Read the rationales too: structured answers can pass while hiding contradictions. For sequential cases, reveal each input only after the previous response.

## Brief for a specialist reviewer

Review the exact branch or commit you were given and record `git rev-parse HEAD` first. A suitable static review request:

> Review this skill at the checked-out commit. Read README.md, SKILL.md, the linked references, scripts, platform configs and tests. Work read-only apart from isolated test artifacts; do not install the skill or change live configuration. Check instruction consistency, guarantee labels (ENFORCED only with a runtime mechanism), installer safety, routing and ownership, review independence and whether tests can pass unsafe decisions. For each finding give severity, file/line evidence, a failure scenario and the smallest correction. Distinguish executed checks from unrun ones and runtime enforcement from instructions.

A static reviewer may read the expected checks; a blind behavioral evaluator must not. See [CONTRIBUTING.md](CONTRIBUTING.md) for maintenance, packaging and the smoke/evaluation workflow.
