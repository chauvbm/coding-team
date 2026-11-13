"""Per-platform adapter configuration (schema v2) for coding-team."""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = ('claude-code', 'codex', 'antigravity')
ROLES = ('ct-architect', 'ct-researcher', 'ct-developer',
         'ct-test-engineer', 'ct-reviewer', 'ct-security')
TIERS = ('inherit', 'strong', 'standard', 'light')

_TOP_KEYS = {
    'schema_version', 'platform', 'verified_on', 'config_root', 'tiers', 'roles',
    'effort_vocabulary', 'static_agent_model', 'tool_allowlist_enforced', 'tools',
    'agent_format', 'paths', 'project_paths', 'chat_id_env', 'detect_env',
    'force_model_env', 'invocation',
}
_TOOL_KEYS = {'read', 'docs_lookup', 'write', 'shell', 'browser_testing', 'delegate'}
_PATH_KEYS = {'skill_dir', 'agents_dir', 'global_rules_file', 'state_file'}
_PROJECT_KEYS = {'skill_dir', 'agents_dir'}
_AGENT_FORMATS = {'claude-md', 'codex-toml', 'antigravity-md'}
_PLACEHOLDER = re.compile(r'\{([^{}]*)\}')
_ALLOWED_PLACEHOLDERS = {'config_root', 'home'}


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON key: {key!r}')
        result[key] = value
    return result


def read_json_unique(path: Path) -> object:
    text = Path(path).read_text(encoding='utf-8-sig')
    try:
        return json.loads(text, object_pairs_hook=_no_duplicates)
    except json.JSONDecodeError as exc:
        raise ValueError(f'invalid JSON in {path}: {exc}') from exc


def _fail(message: str):
    raise ValueError(message)


def _exact_keys(obj, keys, label):
    if not isinstance(obj, dict):
        _fail(f'{label} must be an object')
    if set(obj) != set(keys):
        _fail(f'{label} keys must be exactly {sorted(keys)}, got {sorted(obj)}')


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        _fail(f'{label} must be a non-empty string')


def _text_list(value, label):
    if not isinstance(value, list):
        _fail(f'{label} must be a list')
    for item in value:
        _text(item, f'{label} item')
    if len(set(value)) != len(value):
        _fail(f'{label} has duplicates')


def _check_placeholders(value, label):
    _text(value, label)
    for name in _PLACEHOLDER.findall(value):
        if name not in _ALLOWED_PLACEHOLDERS:
            _fail(f'{label} uses unsupported placeholder {{{name}}}')
    stripped = _PLACEHOLDER.sub('', value)
    if '{' in stripped or '}' in stripped:
        _fail(f'{label} has unbalanced braces')


def validate_platform(data: dict) -> dict:
    _exact_keys(data, _TOP_KEYS, 'platform config')
    version = data['schema_version']
    if isinstance(version, bool) or version != 2:
        _fail('schema_version must be 2')
    if data['platform'] not in PLATFORMS:
        _fail(f'platform must be one of {PLATFORMS}')
    verified = data['verified_on']
    if not isinstance(verified, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', verified):
        _fail('verified_on must be YYYY-MM-DD')
    try:
        date.fromisoformat(verified)
    except ValueError:
        _fail('verified_on is not a real date')

    root = data['config_root']
    _exact_keys(root, {'env', 'default'}, 'config_root')
    if root['env'] is not None:
        _text(root['env'], 'config_root.env')
    _check_placeholders(root['default'], 'config_root.default')

    vocab = data['effort_vocabulary']
    _text_list(vocab, 'effort_vocabulary')

    tiers = data['tiers']
    _exact_keys(tiers, TIERS, 'tiers')
    for name, tier in tiers.items():
        _exact_keys(tier, {'model', 'effort'}, f'tiers.{name}')
        if name == 'inherit':
            if tier['model'] is not None or tier['effort'] is not None:
                _fail('tiers.inherit must have null model and effort')
            continue
        if tier['model'] is not None:
            _text(tier['model'], f'tiers.{name}.model')
        effort = tier['effort']
        if effort is not None and (not isinstance(effort, str) or effort not in vocab):
            _fail(f'tiers.{name}.effort must be null or in effort_vocabulary')

    roles = data['roles']
    _exact_keys(roles, ROLES, 'roles')
    for role, entry in roles.items():
        _exact_keys(entry, {'tier'}, f'roles.{role}')
        if entry['tier'] not in TIERS:
            _fail(f'roles.{role}.tier must be one of {TIERS}')

    if data['static_agent_model'] not in ('default', 'omit'):
        _fail('static_agent_model must be default or omit')
    if not isinstance(data['tool_allowlist_enforced'], bool):
        _fail('tool_allowlist_enforced must be boolean')

    tools = data['tools']
    _exact_keys(tools, _TOOL_KEYS, 'tools')
    for name, items in tools.items():
        _text_list(items, f'tools.{name}')

    if data['agent_format'] not in _AGENT_FORMATS:
        _fail(f'agent_format must be one of {sorted(_AGENT_FORMATS)}')

    paths = data['paths']
    _exact_keys(paths, _PATH_KEYS, 'paths')
    for name, value in paths.items():
        _check_placeholders(value, f'paths.{name}')

    project = data['project_paths']
    _exact_keys(project, _PROJECT_KEYS, 'project_paths')
    for name, value in project.items():
        _text(value, f'project_paths.{name}')
        if '{' in value or '}' in value:
            _fail(f'project_paths.{name} must not use placeholders')
        if (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()
                or PureWindowsPath(value).drive or value.startswith(('/', '\\'))):
            _fail(f'project_paths.{name} must be relative')
        parts = re.split(r'[\\/]', value)
        if '..' in parts:
            _fail(f'project_paths.{name} must not contain ..')

    for key in ('chat_id_env', 'detect_env', 'force_model_env'):
        _text_list(data[key], key)
    _text(data['invocation'], 'invocation')
    return {'valid': True, 'platform': data['platform'], 'availability_verified': False}


def load_platform(name: str, source_root: Path = REPO_ROOT, input_path: Path | None = None) -> dict:
    if name not in PLATFORMS:
        raise ValueError(f'unknown platform {name!r}; expected one of {PLATFORMS}')
    path = Path(input_path) if input_path is not None else Path(source_root) / 'platforms' / f'{name}.json'
    data = read_json_unique(path)
    validate_platform(data)
    if data['platform'] != name:
        raise ValueError(f'platform {data["platform"]!r} does not match requested {name!r}')
    return data


def _env_value(env: Mapping[str, str], key: str) -> str | None:
    value = env.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _expand(template: str, config_root: Path | None, home: Path) -> Path:
    def sub(match):
        name = match.group(1)
        if name == 'home':
            return str(home)
        if name == 'config_root' and config_root is not None:
            return str(config_root)
        raise ValueError(f'cannot resolve placeholder {{{name}}}')
    return Path(_PLACEHOLDER.sub(sub, template))


def resolve_config_root(cfg, env: Mapping[str, str], home: Path) -> Path:
    spec = cfg['config_root']
    if spec['env']:
        value = _env_value(env, spec['env'])
        if value is not None:
            return Path(value)
    return _expand(spec['default'], None, Path(home))


def target_paths(cfg, scope: str, env: Mapping[str, str], home: Path,
                 project_dir: Path | None) -> dict:
    if scope not in ('user', 'project'):
        raise ValueError(f'scope must be user or project, got {scope!r}')
    home = Path(home)
    root = resolve_config_root(cfg, env, home)
    paths = cfg['paths']
    state_file = _expand(paths['state_file'], root, home)
    if scope == 'user':
        skill_dir = _expand(paths['skill_dir'], root, home)
        agents_dir = _expand(paths['agents_dir'], root, home)
        rules = _expand(paths['global_rules_file'], root, home)
    else:
        if project_dir is None:
            raise ValueError('project scope requires project_dir')
        project_dir = Path(project_dir)
        skill_dir = project_dir / cfg['project_paths']['skill_dir']
        agents_dir = project_dir / cfg['project_paths']['agents_dir']
        rules = None
    managed = [skill_dir, agents_dir]
    if rules is not None:
        managed.append(rules.parent)
    managed.append(state_file.parent)
    unique = []
    for item in managed:
        if item not in unique:
            unique.append(item)
    return {
        'skill_dir': skill_dir,
        'agents_dir': agents_dir,
        'global_rules_file': rules,
        'state_file': state_file,
        'backup_root': state_file.parent / 'backups',
        'lock_file': state_file.parent / 'install.lock',
        'journal_file': state_file.parent / 'install-journal.json',
        'managed_roots': unique,
    }


def detect_platform(env: Mapping[str, str]) -> str | None:
    matches = []
    for name in PLATFORMS:
        cfg = load_platform(name)
        if any(_env_value(env, key) is not None for key in cfg['detect_env']):
            matches.append(name)
    return matches[0] if len(matches) == 1 else None


def resolve_tier(cfg, role: str, tier: str | None = None) -> dict:
    if role not in ROLES:
        raise ValueError(f'unknown role {role!r}')
    chosen = tier if tier is not None else cfg['roles'][role]['tier']
    if chosen not in TIERS:
        raise ValueError(f'unknown tier {chosen!r}')
    entry = cfg['tiers'][chosen]
    return {'tier': chosen, 'model': entry['model'], 'effort': entry['effort']}


def chat_id_from_env(cfg, env: Mapping[str, str]) -> str | None:
    for key in cfg['chat_id_env']:
        value = _env_value(env, key)
        if value is not None:
            return value
    return None
