"""Unified coding-team CLI: install, uninstall, policy status/commands, doctor, config check
and orchestration ownership. Every invocation prints exactly one JSON object.

Exit codes: 0 success or healthy; 1 valid but negative (doctor found problems,
`orchestrator status` cannot claim); 2 error (including install conflicts).
"""
from __future__ import annotations

import sys

# The installed copy must not leave scripts/__pycache__ behind (it would block uninstall).
sys.dont_write_bytecode = True

from pathlib import Path  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import argparse  # noqa: E402
import hashlib  # noqa: E402
import itertools  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import stat  # noqa: E402

import ct_install  # noqa: E402
import ct_orchestration  # noqa: E402
import ct_policy  # noqa: E402
import ct_rules  # noqa: E402  (read-only marker parsing; writes go through ct_install)
from ct_platform import (PLATFORMS, chat_id_from_env, detect_platform, load_platform,  # noqa: E402
                         read_json_unique, resolve_config_root, resolve_tier, target_paths,
                         validate_platform)
from ct_roles import guarantees, load_roles, render_agent  # noqa: E402

SOURCE_ROOT = SCRIPT_DIR.parent
DESCRIPTION = 'Unified coding-team CLI; prints exactly one JSON object.'
SUPERPOWERS_SKILL = ('skills', 'subagent-driven-development', 'SKILL.md')
SEARCH_DEPTH = 12
_MODEL_LINE = re.compile(r'^model\s*[:=]\s*"?([^"\n]*?)"?\s*$')


class CliError(Exception):
    """Argument error; reported as JSON instead of argparse's usage text."""


class HelpRequested(Exception):
    """-h/--help: reported as one JSON object {"help": text} with exit 0."""

    def __init__(self, text):
        super().__init__('help requested')
        self.text = text


# argparse echoes a rejected choice value; a secret typed in the wrong place must not be.
_INVALID_CHOICE = re.compile(r"invalid choice: .*? \(choose from", re.S)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CliError(_INVALID_CHOICE.sub('invalid choice (choose from', message))

    def print_help(self, file=None):
        raise HelpRequested(self.format_help())

    def parse_args(self, args=None, namespace=None):
        parsed, extras = self.parse_known_args(args, namespace)
        if extras:
            # Never echo the values: a token or secret may have been typed in the wrong place.
            raise CliError('%d unrecognized argument(s) rejected; values are not shown' % len(extras))
        return parsed


# --- platform ------------------------------------------------------------------------------

def _manifest_platform(source_root=SOURCE_ROOT):
    try:
        data = json.loads((Path(source_root) / ct_install.MANIFEST_NAME).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    platform = data.get('platform') if isinstance(data, dict) else None
    return platform if platform in PLATFORMS else None


def resolve_platform(arg, env) -> str:
    """--platform, else this copy's install manifest, else unambiguous env detection."""
    if arg:
        return arg
    platform = _manifest_platform()
    if platform:
        return platform
    try:
        platform = detect_platform(env)
    except (OSError, ValueError):  # an installed copy only ships its own platform config
        platform = None
    if platform is None:
        raise ValueError('cannot determine platform; pass --platform')
    return platform


def _config(name):
    return load_platform(name, SOURCE_ROOT)


# --- doctor --------------------------------------------------------------------------------

def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _model_in_file(data, fmt):
    """The model line of a rendered agent's header, None if absent."""
    lines = data.decode('utf-8', errors='replace').splitlines()
    if fmt in ('claude-md', 'antigravity-md'):
        if not lines or lines[0].strip() != '---':
            return None
        header = []
        for line in lines[1:]:
            if line.strip() == '---':
                break
            header.append(line)
    else:  # codex-toml: top-level keys before the instructions
        header = []
        for line in lines:
            if line.startswith('developer_instructions'):
                break
            header.append(line)
    for line in header:
        match = _MODEL_LINE.match(line.strip())
        if match:
            return match.group(1) or None
    return None


def _is_link_entry(entry):
    """Symlink, junction or other reparse point (DirEntry.is_junction needs Python 3.12)."""
    if entry.is_symlink():
        return True
    attributes = getattr(entry.stat(follow_symlinks=False), 'st_file_attributes', 0)
    return bool(attributes & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))


def _walk_dirs(directory, depth=0):
    """Yield real directories below directory, depth-limited, never through links."""
    if depth >= SEARCH_DEPTH:
        return
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return
    for entry in entries:
        try:
            if _is_link_entry(entry) or not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        yield Path(entry.path)
        yield from _walk_dirs(entry.path, depth + 1)


def _find_superpowers(roots):
    """Any `**/skills/subagent-driven-development/SKILL.md` below the roots (links not followed)."""
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for directory in itertools.chain([root], _walk_dirs(root)):
            if (directory.name == SUPERPOWERS_SKILL[1] and directory.parent.name == SUPERPOWERS_SKILL[0]
                    and (directory / SUPERPOWERS_SKILL[2]).is_file()):
                return True
    return False


def _superpowers_enabled(cfg, config_root):
    if cfg['platform'] != 'claude-code':
        return None
    try:
        settings = json.loads((Path(config_root) / 'settings.json').read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None
    plugins = settings.get('enabledPlugins') if isinstance(settings, dict) else None
    if not isinstance(plugins, dict):
        return None
    values = [value for key, value in plugins.items() if 'superpowers' in str(key).lower()]
    if not values:
        return None
    return any(value is True for value in values)


def _rules_status(paths):
    rules_file = paths['global_rules_file']
    if rules_file is None:
        return 'not_applicable'
    try:
        data = Path(rules_file).read_bytes()
    except FileNotFoundError:
        return 'absent'
    except OSError:
        return 'invalid'
    try:
        found = ct_rules._markers(data)
    except ValueError:
        return 'invalid'
    return 'present' if found else 'absent'


def doctor(cfg, *, scope, env, home, project_dir) -> dict:
    """Read-only health report of an installation. Never calls the runtime."""
    paths = target_paths(cfg, scope, env, home, project_dir)
    config_root = resolve_config_root(cfg, env, Path(home))
    skill_dir = Path(paths['skill_dir'])
    report = {'platform': cfg['platform'], 'scope': scope,
              'manifest_path': (skill_dir / ct_install.MANIFEST_NAME).as_posix(), 'manifest_error': None}
    manifest = None
    try:
        manifest = ct_install._load_manifest(skill_dir)
        if manifest is None:
            report['manifest'] = 'missing'
        elif manifest['platform'] != cfg['platform'] or manifest['scope'] != scope:
            report['manifest'] = 'invalid'
            report['manifest_error'] = ('manifest belongs to a %s (%s scope) installation'
                                        % (manifest['platform'], manifest['scope']))
            manifest = None
        else:
            report['manifest'] = 'ok'
    except (OSError, ValueError) as error:
        report['manifest'], report['manifest_error'] = 'invalid', str(error)

    modified, missing = [], []
    for label, entries in (manifest or {}).get('files', {}).items():
        for rel, digest in entries.items():
            path = Path(paths[label]) / rel
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                missing.append(path.as_posix())
                continue
            except OSError:
                modified.append(path.as_posix())
                continue
            if _sha(data) != digest:
                modified.append(path.as_posix())
    report['modified_files'], report['missing_files'] = sorted(modified), sorted(missing)
    report['journal_present'] = Path(paths['journal_file']).exists()

    options = (manifest or {}).get('options') or {}
    docs_mcp = options.get('docs_mcp') if isinstance(options, dict) else None
    mcp_tools = {'docs_lookup': list(docs_mcp)} if isinstance(docs_mcp, list) and docs_mcp else None
    roles = load_roles(SOURCE_ROOT / 'agents' / 'roles')
    agents = {}
    for role_id, role in roles.items():
        for variant_id, caps in role['variants'].items():
            rel, _ = render_agent(role_id, variant_id, roles, cfg, mcp_tools)
            path = Path(paths['agents_dir']) / rel
            try:
                data = path.read_bytes()
            except OSError:
                data = None
            agents[variant_id] = {
                'present': data is not None,
                'requested_tier': resolve_tier(cfg, role_id)['tier'],
                'model_in_file': None if data is None else _model_in_file(data, cfg['agent_format']),
                'guarantees': guarantees(caps, cfg, mcp_tools),
            }
    report['agents'] = agents
    report['model_availability'] = 'unverified'
    report['force_model_env_active'] = [key for key in cfg['force_model_env']
                                        if isinstance(env.get(key), str) and env[key].strip()]

    report['state_path'] = Path(paths['state_file']).as_posix()
    report['state_error'] = None
    try:
        ct_policy.load_state(paths['state_file'])
        report['state'] = 'ok'
    except FileNotFoundError:
        report['state'] = 'missing'
    except (OSError, ValueError) as error:
        report['state'], report['state_error'] = 'invalid', str(error)

    report['rules_block'] = _rules_status(paths)
    rules_expected = bool((manifest or {}).get('rules_file'))
    report['superpowers'] = {
        'installed': 'found' if _find_superpowers([config_root, Path(home) / '.agents' / 'skills'])
        else 'not_found',
        'enabled': _superpowers_enabled(cfg, config_root),
        'available_in_session': 'determined by Leader at runtime',
    }

    problems = []
    if report['manifest'] != 'ok':
        problems.append('manifest ' + report['manifest'])
    if modified:
        problems.append('managed files were modified')
    if missing:
        problems.append('managed files are missing')
    if report['journal_present']:
        problems.append('an install/uninstall did not finish; run `install --recover`')
    if not all(agent['present'] for agent in agents.values()):
        problems.append('agent files are missing')
    if report['state'] == 'invalid' or (scope == 'user' and report['state'] != 'ok'):
        problems.append('policy state ' + report['state'])
    if report['rules_block'] == 'invalid' or (rules_expected and report['rules_block'] != 'present'):
        problems.append('global rules block ' + report['rules_block'])
    report['problems'] = problems
    report['healthy'] = not problems
    if cfg['platform'] == 'codex' and os.name == 'nt':
        from ct_external import doctor_status

        report['antigravity_cli'] = doctor_status(cfg, paths, env, home, project_dir or Path.cwd(), manifest)
    return report


# --- argument parsing ----------------------------------------------------------------------

def _parser():
    parser = _Parser(prog='coding_team.py', description=DESCRIPTION)
    sub = parser.add_subparsers(dest='action', required=True, parser_class=_Parser)

    def scoped(item):
        item.add_argument('--scope', choices=('user', 'project'), default='user')
        item.add_argument('--project-dir', type=Path)

    item = sub.add_parser('install')
    item.add_argument('--platform', choices=PLATFORMS, required=True)
    scoped(item)
    item.add_argument('--default', choices=('on', 'off'))
    item.add_argument('--global-rules', action=argparse.BooleanOptionalAction, default=None)
    item.add_argument('--docs-mcp', action='append', default=[], metavar='NAME')
    item.add_argument('--dry-run', action='store_true')
    item.add_argument('--replace-unmanaged', action='store_true')
    item.add_argument('--overwrite-modified', action='store_true')
    item.add_argument('--recover', action='store_true')

    item = sub.add_parser('uninstall')
    item.add_argument('--platform', choices=PLATFORMS, required=True)
    scoped(item)
    item.add_argument('--dry-run', action='store_true')
    item.add_argument('--remove-modified', action='store_true')

    for name in ('status', 'command'):
        item = sub.add_parser(name)
        if name == 'command':
            item.add_argument('text')
        item.add_argument('--platform', choices=PLATFORMS)
        item.add_argument('--chat-id')
        item.add_argument('--state', type=Path)

    item = sub.add_parser('external')
    item.add_argument('operation', choices=('discover', 'reserve', 'start', 'status', 'cancel', 'reconcile',
                                             'grant-read', 'collect', 'candidate', 'mark-tested',
                                             'apply', 'discard', 'run-attempt'))
    item.add_argument('--platform', choices=PLATFORMS, required=True)
    item.add_argument('--workspace', type=Path, required=True)
    item.add_argument('--task-id')
    item.add_argument('--attempt', type=int)
    item.add_argument('--request-stdin', action='store_true')
    item.add_argument('--read-grant-stdin', action='store_true')
    item.add_argument('--runner-bootstrap-stdin', action='store_true', help=argparse.SUPPRESS)
    item.add_argument('--report-stdin', action='store_true')
    item.add_argument('--wait-seconds', type=int, default=0)
    item.add_argument('--ttl-seconds', type=int, default=300)
    item.add_argument('--candidate-sha')
    item.add_argument('--record-receipt', metavar='RESULT_ID')
    item.add_argument('--target', choices=('process', 'apply'))

    item = sub.add_parser('doctor')
    item.add_argument('--platform', choices=PLATFORMS)
    scoped(item)

    item = sub.add_parser('check-config')
    item.add_argument('--platform', choices=PLATFORMS)
    item.add_argument('--input', type=Path)

    item = sub.add_parser('orchestrator')
    item.add_argument('operation', choices=('status', 'claim', 'takeover', 'handoff', 'release', 'route'))
    item.add_argument('--platform', choices=PLATFORMS)
    item.add_argument('--workspace', type=Path)
    item.add_argument('--task-id')
    item.add_argument('--mode')
    item.add_argument('--phase')
    item.add_argument('--input', type=Path)
    item.add_argument('--session-id')
    item.add_argument('--token')
    item.add_argument('--reason')
    item.add_argument('--user-confirmed', action='store_true')
    return parser


# --- subcommands ---------------------------------------------------------------------------

def _context(args, env, home):
    project_dir = None if args.project_dir is None else Path(os.path.abspath(args.project_dir))
    return dict(scope=args.scope, env=env, home=home, project_dir=project_dir)


def _install(args, env, home):
    cfg = _config(args.platform)
    if args.recover:
        extra = [flag for flag, value in (('--dry-run', args.dry_run), ('--default', args.default),
                                           ('--global-rules', args.global_rules is not None), ('--docs-mcp', args.docs_mcp),
                                           ('--replace-unmanaged', args.replace_unmanaged),
                                           ('--overwrite-modified', args.overwrite_modified)) if value]
        if extra:
            raise CliError('--recover performs a real recovery and cannot be combined with '
                           + ', '.join(extra) + '; only --platform, --scope and --project-dir apply')
        return dict(ct_install.recover(cfg, **_context(args, env, home)), platform=cfg['platform']), 0
    plan = ct_install.build_plan(SOURCE_ROOT, cfg, default=args.default, global_rules=(args.scope == 'user') if args.global_rules is None else args.global_rules,
                                 python=sys.executable, docs_mcp=args.docs_mcp, **_context(args, env, home))
    result = ct_install.apply_plan(plan, dry_run=args.dry_run, replace_unmanaged=args.replace_unmanaged,
                                   overwrite_modified=args.overwrite_modified)
    return dict(result, platform=cfg['platform'], scope=args.scope), 0


def _uninstall(args, env, home):
    cfg = _config(args.platform)
    result = ct_install.uninstall(cfg, dry_run=args.dry_run, remove_modified=args.remove_modified,
                                  **_context(args, env, home))
    return dict(result, platform=cfg['platform'], scope=args.scope), 0


def _policy(args, env, home):
    cfg = _config(resolve_platform(args.platform, env))
    if args.action == 'command' and re.match(r'^\s*/(?:coding-team|team-coding)\s+antigravity\b',
                                            args.text, re.IGNORECASE | re.ASCII):
        from ct_external import control_command

        return control_command(args, SOURCE_ROOT, cfg, env, home)
    state = args.state or target_paths(cfg, 'user', env, home, None)['state_file']
    chat_id = args.chat_id if args.chat_id is not None else chat_id_from_env(cfg, env)
    if args.action == 'status':
        result = ct_policy.describe(ct_policy.load_state(state), chat_id)
    else:
        result = ct_policy.apply_command(state, args.text, chat_id)
    return dict(result, platform=cfg['platform'], state_path=Path(state).as_posix()), 0


def _external(args, env, home):
    from ct_external import external_command

    return external_command(args, SOURCE_ROOT, _config(args.platform), env, home, sys.stdin.buffer)


def _doctor(args, env, home):
    cfg = _config(resolve_platform(args.platform, env))
    report = doctor(cfg, **_context(args, env, home))
    return report, 0 if report['healthy'] else 1


def _check_config(args, env, home):
    if args.input is not None:
        data = read_json_unique(args.input)
        if not isinstance(data, dict):
            raise ValueError('platform config must be an object')
        result = validate_platform(data)
        if args.platform and data['platform'] != args.platform:
            raise ValueError('platform %r does not match requested %r' % (data['platform'], args.platform))
        return dict(result, input=Path(args.input).as_posix()), 0
    name = resolve_platform(args.platform, env)
    load_platform(name, SOURCE_ROOT)
    return {'valid': True, 'platform': name, 'availability_verified': False}, 0


def _session_id(args, env):
    if args.session_id is not None:
        return args.session_id
    try:
        name = resolve_platform(args.platform, env)
    except ValueError:
        return None  # no platform, no trusted session id: never guess
    return chat_id_from_env(_config(name), env)


def _orchestrator(args, env, home):
    op = args.operation
    if op == 'route':
        if args.input is None:
            raise CliError('orchestrator route requires --input FACTS.json')
        return ct_orchestration.route(read_json_unique(args.input)), 0
    workspace = Path(args.workspace) if args.workspace is not None else Path.cwd()
    if not workspace.is_dir():
        raise ValueError('workspace does not exist or is not a directory: ' + workspace.as_posix())
    workspace = workspace.resolve()

    def need(*names):
        missing = ['--' + name.replace('_', '-') for name in names if not getattr(args, name)]
        if missing:
            raise CliError('orchestrator %s requires %s' % (op, ', '.join(missing)))

    if op == 'status':
        result = ct_orchestration.status(workspace, args.task_id)
        return result, 0 if result['can_claim'] else 1
    session_id = _session_id(args, env)
    if op == 'claim':
        need('task_id', 'mode', 'phase')
        return ct_orchestration.claim(workspace, args.task_id, args.mode, args.phase,
                                      session_id=session_id, token=args.token), 0
    if op == 'takeover':
        need('task_id', 'reason')
        return ct_orchestration.takeover(workspace, args.task_id, session_id=session_id, reason=args.reason,
                                         user_confirmed=args.user_confirmed), 0
    if op == 'handoff':
        need('input')
        record = read_json_unique(args.input)
        if not isinstance(record, dict):
            raise ValueError('handoff record must be an object')
        if args.user_confirmed:
            record['user_confirmed'] = True
        if args.token is not None:
            record['token'] = args.token
        return ct_orchestration.handoff(workspace, record, session_id=session_id), 0
    need('task_id')
    return ct_orchestration.release(workspace, args.task_id, session_id=session_id, token=args.token), 0


HANDLERS = {'install': _install, 'uninstall': _uninstall, 'status': _policy, 'command': _policy,
            'doctor': _doctor, 'check-config': _check_config, 'orchestrator': _orchestrator,
            'external': _external}


def _error(error, action):
    if action == 'external':
        return {'ok': False, 'state': 'NOT_READY', 'reason': getattr(error, 'reason', 'INVALID_EXTERNAL_COMMAND'),
                'task_id': None, 'attempt_no': None}
    result: dict[str, object] = {'error': str(error), 'error_type': type(error).__name__}
    paths = getattr(error, 'paths', None)
    if paths:
        result['paths'] = list(paths)
    if isinstance(error, ct_policy.LegacyCommandError):
        result['hint'] = error.hint
    if isinstance(error, ct_orchestration.OrchestrationConflict):
        result['blockers'] = error.blockers
    if isinstance(error, ct_install.InstallRolledBack):
        result['backup_dir'] = error.backup_dir
    if action in ('status', 'command'):
        result['automatic_use_allowed'] = False
    if action == 'check-config':
        result.update(valid=False, availability_verified=False)
    return result


def main(argv=None) -> int:
    env = dict(os.environ)
    argv = sys.argv[1:] if argv is None else list(argv)
    # Known before parsing so that argument errors of status/command still fail closed.
    action = argv[0] if argv and argv[0] in HANDLERS else None
    try:
        args = _parser().parse_args(argv)
        action = args.action
        result, code = HANDLERS[action](args, env, Path.home())
    except HelpRequested as request:
        result, code = {'help': request.text}, 0
    except Exception as error:  # every outcome is reported as one JSON object
        result, code = _error(error, action), 2
    print(json.dumps(result, default=str))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
