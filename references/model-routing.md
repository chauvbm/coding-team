# Model routing

Read before delegation or when STRICT_MODEL applies. The Leader stays on the current chat configuration.

## Three concepts

- **Preferred Tier:** the desired tier, from the role's entry in the platform config or chosen by the Leader for this task. Tiers are inherit, `strong`, `standard` and `light`.
- **Requested Model:** the model and effort actually passed at dispatch.
- **Effective Model:** the model the runtime actually used. Record it only from runtime metadata; otherwise record `unverified`.

Tier defaults have one source: the installed `platforms/<platform>.json` (`tiers` maps each tier to a model and effort; `roles` maps each role to a tier). The inherit tier has null model and effort: omit the override and let the runtime default apply; it is not a model name. Current defaults: `ct-architect`, `ct-reviewer` and `ct-security` are `strong`; `ct-developer` and `ct-test-engineer` are `standard`; `ct-researcher` is `light`.

| | Claude Code | Codex | Antigravity |
| --- | --- | --- | --- |
| strong / standard / light | `opus` / `sonnet` / `haiku` with effort high / medium / low | configured GPT model names with `model_reasoning_effort` | `pro` / `flash` / `flash_lite`, no effort |
| `static_agent_model` | `default` | `omit` | `omit` |
| Force-model variable | `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` | none | none |

The config being valid JSON says nothing about whether the account can use a model; `verified_on` is only the date the documentation was checked, and `doctor` reports `model_availability: unverified`.

## `static_agent_model`

This decides whether the tier's model is written into the static agent file:

- `default`: the role's tier model/effort is written as the agent default. Used only where the runtime is verified to let a model passed at dispatch win over the file (Claude Code).
- `omit`: no model in the agent file; the Leader always passes the model at dispatch. Codex uses `omit` because a model in the agent TOML wins over the model passed at spawn, which would freeze the tier. Antigravity uses `omit` until the precedence between a static `model` and the tier passed to `invoke_subagent` is verified.

## Resolve a dispatch

Priority, highest first:

1. A user-pinned model, or STRICT_MODEL.
2. A Leader choice for this specific task, with a stated reason.
3. The role's Preferred Tier.

The Leader may raise or lower the tier by difficulty: lighter for bounded investigation or mechanical work, stronger for hard design, debugging, security, integrity or concurrency risk. Escalation rule: when a `ct-developer` fix fails after one repair round, the next repair attempt uses a higher tier. Tool or credential failure alone is not a reason for a stronger model. Skip delegation when direct work is more efficient.

Pass model and effort through the platform's structured dispatch fields. Naming a model in a prompt does not select it. Do not edit runtime or global configuration to make a preference work.

If a force-model variable is set, or the runtime rejects or replaces the requested model, say in the report that the model was forced or substituted. Under STRICT_MODEL or an exact user pin, stop and ask the user instead of substituting silently, while continuing independent work that does not depend on it. Outside STRICT_MODEL, disclose an unavailable preference and use the runtime default.

Distinguish Requested Model from Effective Model in reports: missing metadata means `unverified`, not a mismatch and not a confirmation. No token or speed saving is established merely by choosing a lighter tier.

Reconfigure existing agents only through supported controls. Otherwise stop or await the former writer, inspect partial changes, transfer ownership and dispatch a newly configured agent. Changed executors keep task history and allowances.

## Change the defaults

Change tiers or role mappings only when the user asks for a persistent change: edit `platforms/<platform>.json` in the source checkout and reinstall (editing the installed copy makes `doctor` report a modified managed file). Validate with `python scripts/coding_team.py check-config --platform <platform>` or `check-config --input FILE`: exit 0 for valid structure, 2 for invalid input, always with `availability_verified: false`. Effort values must be in the platform's `effort_vocabulary`.

A valid JSON is not evidence routing works. The evidence is a smoke test on the real runtime that dispatches each role and observes the model it actually ran on.
