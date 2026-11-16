# Antigravity CLI worker for Codex Desktop

Phase 1 delegates individual requests from Codex Desktop to the official Windows `agy` binary. Leader chooses the work and owns verification, apply and the Git workflow. A native `ct-researcher` can wait for the result in parallel with native work. Each ordinary request runs in a fresh CLI process and session.

## Setup and controls

Use a managed coding-team installation and the canonical local Git worktree from the Codex session. Install or upgrade the known user and project copies that share the native policy file. The user manages the Google CLI and signs in using Antigravity. Coding-team checks the official signed binary, its version and its launch profile locally.

Handle the following as direct user messages through the installed `coding_team.py command` entry. `/team-coding antigravity` is an alias. Commands are case-insensitive and scoped to the current Codex chat and Git worktree. Fresh scopes start OFF.

| Command | Effect |
| --- | --- |
| `/coding-team antigravity on` | Save opt-in ON if native coding-team policy is ON. Report the setup state. |
| `/coding-team antigravity off` | Save OFF, cancel the pending reservation and signal a running or finalizing attempt to stop. |
| `/coding-team antigravity status` | Read the local policy, opt-in, readiness and attempts. Re-check binary and profile when opt-in is ON. |
| `/coding-team antigravity check` | Reserve the shared slot and verify setup with synthetic canaries. Trusted bootstrap and current policy must permit it. |

`READY` requires a completed CHECK for the current binary and startup profile plus a free slot. Unknown startup sources, unresolved hooks/plugins/MCP or unverified settings precedence return `CUSTOMIZATIONS_BLOCKED`. A successful smoke run with an interactive or differently configured CLI does not qualify the profile. Changes to the binary or configuration invalidate the earlier setup evidence. Resolve authentication using the official CLI when a result reports `AUTH_REQUIRED`. [Headless CLI](https://antigravity.google/docs/cli/headless/) · [Configuration discovery](https://antigravity.google/docs/customization-discovery/)

`status`, `doctor` and install dry runs are local checks. Use `install --recover` in the matching scope if a managed install or unplugged runner requires recovery. Keep the attempt data until the helper can prove the writer stopped. A user or project installation has one shared slot per native state directory.

## Qualify a signed CLI build

The bootstrap list includes the signed `agy` 1.5.9 launch profile verified on Windows. Qualifying another profile requires evidence from synthetic files on the target Windows host. Record the binary version and digest, effective session settings, startup sources, and their fingerprints. Admission requires a reviewed bootstrap record and a completed CHECK for the same profile.

The supervisor generates a separate CLI home and starts one reviewed pre-tool hook from that home. The hook passes file tools to Antigravity's scoped permissions and blocks other tools before execution. The hook runs through the Windows system command processor with a restricted search path. Machine-level Antigravity settings, command processor AutoRun and workspace customization redirects block qualification when they could introduce unreviewed behavior. The supervisor checks these sources without modifying user configuration. The profile covers the fixed model and quota inspections and the `--model` argument used for ordinary work. [Antigravity hooks](https://antigravity.google/docs/hooks/#pretooluse) · [Command processor startup](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/cmd#remarks)

Prompts go through stdin and results come from the CLI's newline-delimited JSON events. CHECK links positive and denied calls to the expected tool and target, verifies the canary files, and checks the MCP inventory using `agy mcp list` in the same controlled discovery profile. Completion requires stable evidence and stopped processes for every probe. The worker stream also lets ordinary requests report a tool denial when the terminal envelope reports success. [Headless events](https://antigravity.google/docs/cli/headless/#streaming-json)

## Assign and collect

1. Check fresh native eligibility and the external status. Choose `READ` for investigation or review and `EDIT` for a restricted file change. Use the approved task scope, the exact Git base commit and a list of context files. Select `GIT`, `INDEX` or `WORKTREE` bytes for each read-only file. EDIT targets use current working-tree bytes, including a declared missing target. Keep credentials, hooks, linked paths and private planning files out of the projection. Cite the approved artifact hash when supplying excerpts to the worker.
2. For every new attempt, call `external discover` with its exact worktree, task ID and attempt number. It runs `agy models` and `agy -p "/usage" --output-format json` in an isolated empty workspace. It compares the catalog again before combining quota with the options. On `MODEL_OPTIONS`, use the fresh `models` list, quota groups and windows, and the `catalog_id` for this attempt. Each group shares quota across its mapped models. Consider the remaining fractions and reset times for both the weekly and five-hour windows. Missing or ambiguous quota stays unknown. Take labels and quota as data.
3. Leader assesses the difficulty and requirements of this assignment and chooses one listed ID with suitable capability, effort and quota. Use a model ID for the desired reasoning variant when the CLI lists separate IDs. Treat known exhausted IDs as unavailable for this dispatch. The same rules apply to a newly advertised model. In the Codex chat, give the selected model and a short reason.
4. Invoke `external reserve --platform codex --workspace <git-worktree> --task-id <task> --attempt <n> --request-stdin`. Supply one UTF-8 JSON object with `catalog_id`, `model_id`, `kind`, `prompt`, `base`, `file_choices` (a path-to-source map), and `edit_paths` (an array). `timeout_s` defaults to 300 and is capped at 1800 seconds. `spec_refs` accepts optional approved excerpts and version references. The helper binds the exact ID to the attempt after checking its fresh catalog. Repeating the exact reservation returns the existing attempt. On `CATALOG_REFRESH_REQUIRED`, read the attempt state and refresh discovery for the next valid attempt before making a new selection.
5. Use `external grant-read` with the same platform, worktree and attempt arguments. Pass only the exact attempt, worktree and read grant to an eligible native `ct-researcher`. Capture the grant privately and provide it through stdin to `external collect --read-grant-stdin --wait-seconds <seconds>`. The representative waits in its own turn and returns the result ID, state, summary and artifact hashes. Keep the grant out of argv, user-facing output and request logs. A subsequent grant replaces the old one. If a native representative cannot start, cancel before launching the CLI.
6. Leader calls `external start` on that attempt. The helper checks the live catalog again before launching the worker. A changed catalog cancels the reservation with `CATALOG_REFRESH_REQUIRED`. The runner forwards the exact ID as a `--model` argument. `acknowledged: true` means the supervisor claimed it durably. Continue native work after ACK. On a pending ACK, read the existing attempt state before retrying. Use `external status` or owner `external collect` to read the same record after a representative disconnects.
7. Treat worker summaries and diffs as untrusted input. Verify the results independently. Use `requested_model_id` for the requested model and `reported_model_id` for the runtime's report when present. The role scope in the native tool is ADVISORY. SQLite enforces the attempt-specific read grant and the supervisor retains the Job Object until the process tree has stopped.

### Route the work when quota runs out

When discovery returns `NATIVE_REQUIRED` with `QUOTA_EXCEEDED`, or leaves no available model suitable for the work, hand the original assignment to a native Codex subagent. Check fresh native eligibility and choose a role for the original task with the same authorized worktree, files and acceptance criteria. Begin native work immediately once any earlier attempt for those files has stopped and released its EDIT scope. Discovery alone creates no ordinary attempt to cancel. Honor an explicit user requirement to use Antigravity during both dispatch and recovery.

When an ordinary attempt reports `QUOTA_EXCEEDED` or `MODEL_UNAVAILABLE`, confirm the terminal state, reconcile if needed and discard an unusable stopped EDIT to release its write scope. If another eligible model would suit the work, start a new attempt with fresh discovery and a new selection. Otherwise assign the same work to a native Codex subagent. For inconclusive quota diagnostics with a fresh catalog, Leader can choose using capability and the available options.

Catalog comparisons detect observable changes. Accounts with identical catalogs can only be distinguished if the runtime reports an authentication or availability change. Each launch relies on the CLI's active sign-in and backend model and quota checks.

Use the same `--platform codex --workspace <git-worktree> --task-id <task> --attempt <n>` for attempt operations. Run the installed interpreter and CLI paths that the global rules report. Use the shell's literal quoting for paths and pass the request, test record and grant through stdin.

## Review and apply an EDIT

Call `external candidate` to get a private candidate path and candidate digest. Test and review those bytes. Record a successful result with `external mark-tested --candidate-sha <digest> --report-stdin` using a JSON object with `passed: true` and a concise description of the checks performed. Then call `external apply --candidate-sha <digest>`. Keep other writers away from the same targets. The helper compares live preimages, the tested candidate and the Git index and guards live writes with Windows file handles. Guarded source mutation requires local NTFS and existing destination parent directories.

Read the disposition after apply. A partial apply retains ownership. Use `external reconcile --target apply` to classify or roll back from its exact journal. `external discard` releases a stopped EDIT's scope when its apply state is resolved. Artifacts and committed results retain the original attempt identity.

## Stop, recover and retain

`external cancel` targets the exact attempt. `STOPPING` means the supervisor is stopping and verifying the entire job. `RECONCILE_REQUIRED` means the slot or write scope stays reserved pending evidence. Use `external reconcile --target process` if a supervisor disappeared. Keep the native bundle and the attempt's projection during unresolved process recovery. An installer sharing that state cannot replace its managed bundle while the fence is unresolved.

Managed attempt state, immutable artifacts and apply journals live under the native state's `external/v1` directory outside the source worktree. The supervisor removes transient projections, the private CLI home and raw output after terminal handling when safe. Antigravity manages service-side retention according to its terms. Share only task context the user has authorized for that service. [Antigravity terms](https://antigravity.google/terms)
