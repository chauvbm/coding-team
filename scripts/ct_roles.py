"""Role catalog, capability/guarantee matrix and per-platform agent renderers."""
from __future__ import annotations

import json
import re
from pathlib import Path

from ct_platform import ROLES, read_json_unique, resolve_tier

CAPABILITIES = ('read', 'docs_lookup', 'write:assigned', 'write:tests', 'shell',
                'shell:checks', 'browser_testing')
LEDGER_ROLES = ('Architect', 'Developer', 'QA')
GUARANTEE_KEYS = ('no_file_write', 'write_scope', 'command_limits', 'no_delegate')
ENFORCED, ADVISORY, NA = 'ENFORCED', 'ADVISORY', 'N/A'

WORKER_CLAUSE = (
    'You are a coding-team worker, not the Leader. Leader-only rules, routing policy and '
    'orchestrator skills (coding-team, subagent-driven-development, dispatching-parallel-agents, '
    'executing-plans, requesting-code-review) do not apply to you and must not be invoked. '
    'Never dispatch, spawn or define agents, and never start another AI agent or CLI session '
    'through a shell. Use technique skills (test-driven-development, systematic-debugging, '
    'verification-before-completion, receiving-code-review) only when your handoff names them.'
)

# Capability -> platform tool group (cfg['tools'] key).
_TOOL_GROUP = {
    'read': 'read',
    'docs_lookup': 'docs_lookup',
    'write:assigned': 'write',
    'write:tests': 'write',
    'shell': 'shell',
    'shell:checks': 'shell',
    'browser_testing': 'browser_testing',
}
_SHELL_LIKE = {'shell', 'shell:checks', 'browser_testing'}
_ID = re.compile(r'^[a-z][a-z0-9-]*$')


def _fail(message: str):
    raise ValueError(message)


def _check_caps(capabilities, label='capabilities'):
    if not isinstance(capabilities, (list, tuple)) or not capabilities:
        _fail(f'{label} must be a non-empty list')
    for cap in capabilities:
        if not isinstance(cap, str) or cap not in CAPABILITIES:
            _fail(f'{label}: unknown capability {cap!r}; expected one of {CAPABILITIES}')
    if len(set(capabilities)) != len(capabilities):
        _fail(f'{label} has duplicates')


def load_roles(directory: Path) -> dict[str, dict]:
    directory = Path(directory)
    data = read_json_unique(directory / 'roles.json')
    if not isinstance(data, dict) or set(data) != {'schema_version', 'roles'}:
        _fail('roles.json keys must be exactly schema_version, roles')
    version = data['schema_version']
    if isinstance(version, bool) or version != 1:
        _fail('roles.json schema_version must be 1')
    entries = data['roles']
    if not isinstance(entries, dict) or set(entries) != set(ROLES):
        _fail(f'roles.json roles must be exactly {sorted(ROLES)}')
    result = {}
    seen_variants = set()
    for role_id in ROLES:
        entry = entries[role_id]
        if not isinstance(entry, dict) or set(entry) != {'description', 'ledger_role', 'variants'}:
            _fail(f'roles.{role_id} keys must be exactly description, ledger_role, variants')
        description = entry['description']
        if not isinstance(description, str) or not description.strip():
            _fail(f'roles.{role_id}.description must be a non-empty string')
        ledger = entry['ledger_role']
        if ledger is not None and ledger not in LEDGER_ROLES:
            _fail(f'roles.{role_id}.ledger_role must be null or one of {LEDGER_ROLES}')
        variants = entry['variants']
        if not isinstance(variants, dict) or role_id not in variants:
            _fail(f'roles.{role_id}.variants must include {role_id}')
        for variant_id, caps in variants.items():
            if not _ID.match(variant_id) or not (variant_id == role_id
                                                 or variant_id.startswith(role_id + '-')):
                _fail(f'roles.{role_id}: invalid variant id {variant_id!r}')
            if variant_id in seen_variants:
                _fail(f'duplicate variant id {variant_id!r}')
            seen_variants.add(variant_id)
            _check_caps(caps, f'roles.{role_id}.variants.{variant_id}')
        body_path = directory / f'{role_id}.md'
        body = body_path.read_bytes().decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
        if not body.strip():
            _fail(f'{body_path.name} must not be empty')
        if not body.endswith('\n'):
            body += '\n'
        result[role_id] = {
            'description': description,
            'ledger_role': ledger,
            'variants': {k: list(v) for k, v in variants.items()},
            'body': body,
        }
    return result


def extra_mcp_tools(capabilities, mcp_tools: dict[str, list[str]] | None) -> list[str]:
    """MCP tools granted to a role, selected by the capabilities it holds."""
    _check_caps(capabilities)
    if not mcp_tools:
        return []
    if not isinstance(mcp_tools, dict):
        _fail('mcp_tools must be a mapping of capability -> tool names')
    for key, tools in mcp_tools.items():
        if key not in CAPABILITIES:
            _fail(f'mcp_tools: unknown capability {key!r}')
        if not isinstance(tools, (list, tuple)) or not all(isinstance(t, str) and t.strip() for t in tools):
            _fail(f'mcp_tools[{key!r}] must be a list of non-empty strings')
    result = []
    for cap in capabilities:
        for tool in mcp_tools.get(cap, ()):
            if tool not in result:
                result.append(tool)
    return result


def inherits_mcp(capabilities, cfg) -> bool:
    _check_caps(capabilities)
    return cfg['agent_format'] == 'antigravity-md' and 'browser_testing' in capabilities


def tools_for(capabilities, cfg, mcp_tools=None) -> list[str]:
    _check_caps(capabilities)
    banned = set(cfg['tools']['delegate'])
    result = []
    candidates = [tool for cap in capabilities for tool in cfg['tools'][_TOOL_GROUP[cap]]]
    candidates += extra_mcp_tools(capabilities, mcp_tools)
    for tool in candidates:
        if tool not in banned and tool not in result:
            result.append(tool)
    return result


def guarantees(capabilities, cfg, mcp_tools=None) -> dict[str, str]:
    _check_caps(capabilities)
    enforced = cfg['tool_allowlist_enforced'] is True
    writes = any(cap.startswith('write:') for cap in capabilities)
    shell_like = any(cap in _SHELL_LIKE for cap in capabilities)
    # Any MCP tool is unverified: its name proves nothing about server behavior.
    unsafe_mcp = bool(extra_mcp_tools(capabilities, mcp_tools)) or inherits_mcp(capabilities, cfg)
    if writes:
        no_file_write = NA
    elif shell_like or unsafe_mcp:
        no_file_write = ADVISORY
    else:
        no_file_write = ENFORCED if enforced else ADVISORY
    # A shell can launch another AI CLI, so only a shell-free allowlist enforces this.
    no_delegate = ENFORCED if enforced and not shell_like and not unsafe_mcp else ADVISORY
    return {
        'no_file_write': no_file_write,
        'write_scope': ADVISORY if writes else NA,
        'command_limits': ADVISORY if 'shell:checks' in capabilities else NA,
        'no_delegate': no_delegate,
    }


def _variant_caps(role_id, variant_id, roles):
    if role_id not in roles:
        _fail(f'unknown role {role_id!r}')
    variants = roles[role_id]['variants']
    if variant_id not in variants:
        _fail(f'role {role_id!r} has no variant {variant_id!r}')
    return variants[variant_id]


def _instructions(role, capabilities, cfg, mcp_tools) -> str:
    lines = [WORKER_CLAUSE, '', f'## Runtime guarantees ({cfg["platform"]})', '']
    for key, level in guarantees(capabilities, cfg, mcp_tools).items():
        lines.append(f'- {key}: {level}')
    return '\n'.join(lines) + '\n\n' + role['body']


def _quoted(text: str) -> str:
    """Double-quoted scalar valid as both YAML and TOML basic string."""
    out = []
    for ch in text:
        code = ord(ch)
        if ch == '\\':
            out.append('\\\\')
        elif ch == '"':
            out.append('\\"')
        elif code < 0x20 or code == 0x7f:
            out.append(f'\\u{code:04x}')
        else:
            out.append(ch)
    return '"' + ''.join(out) + '"'


def _toml_multiline(text: str) -> str:
    literal_ok = "'''" not in text and not text.endswith("'") and all(
        ch in '\t\n' or (ord(ch) >= 0x20 and ord(ch) != 0x7f) for ch in text)
    if literal_ok:
        return "'''\n" + text + "'''"
    out = []
    for ch in text:
        code = ord(ch)
        if ch == '\\':
            out.append('\\\\')
        elif ch == '"':
            out.append('\\"')
        elif ch in '\t\n':
            out.append(ch)
        elif code < 0x20 or code == 0x7f:
            out.append(f'\\u{code:04x}')
        else:
            out.append(ch)
    return '"""\n' + ''.join(out) + '"""'


def _yaml_item(value: str) -> str:
    return value if re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', value) else _quoted(value)


def _encode(text: str) -> bytes:
    return text.replace('\r\n', '\n').replace('\r', '\n').encode('utf-8')


def render_agent(role_id, variant_id, roles, cfg, mcp_tools=None) -> tuple[str, bytes]:
    capabilities = _variant_caps(role_id, variant_id, roles)
    role = roles[role_id]
    body = _instructions(role, capabilities, cfg, mcp_tools)
    tools = tools_for(capabilities, cfg, mcp_tools)
    fmt = cfg['agent_format']
    if fmt == 'claude-md':
        if not tools:
            # An empty/omitted tools list would grant every tool in Claude Code.
            _fail(f'{variant_id}: no tools resolved for claude-md')
        head = ['---', f'name: {variant_id}', f'description: {_quoted(role["description"])}',
                f'tools: {", ".join(tools)}']
        if cfg['static_agent_model'] == 'default':
            tier = resolve_tier(cfg, role_id)
            if tier['model'] is not None:
                head.append(f'model: {tier["model"]}')
                if tier['effort'] is not None:
                    head.append(f'effort: {tier["effort"]}')
        head.append('---')
        return f'{variant_id}.md', _encode('\n'.join(head) + '\n\n' + body)
    if fmt == 'codex-toml':
        text = (f'name = {_quoted(variant_id)}\n'
                f'description = {_quoted(role["description"])}\n'
                f'developer_instructions = {_toml_multiline(body)}\n')
        return f'{variant_id}.toml', _encode(text)
    if fmt == 'antigravity-md':
        shell_like = any(cap in _SHELL_LIKE for cap in capabilities)
        head = ['---', f'name: {variant_id}', f'description: {_quoted(role["description"])}',
                'mainAgent: false', 'subagent: true', 'hidden: false',
                'inheritCustomizations: true',
                f'inheritMcp: {"true" if inherits_mcp(capabilities, cfg) else "false"}',
                f'commandExecutionPolicy: {"auto" if shell_like else "off"}']
        if tools:
            head.append('tools:')
            head.extend(f'  - {_yaml_item(tool)}' for tool in tools)
        else:
            head.append('tools: []')
        head.append('---')
        return f'{variant_id}/agent.md', _encode('\n'.join(head) + '\n\n' + body)
    _fail(f'unsupported agent_format {fmt!r}')


def render_all(roles, cfg, mcp_tools=None) -> list[tuple[str, bytes]]:
    files = []
    for role_id in ROLES:
        for variant_id in roles[role_id]['variants']:
            files.append(render_agent(role_id, variant_id, roles, cfg, mcp_tools))
    return files
