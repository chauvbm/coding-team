"""Codex's chat controls and local Desktop bridge operations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re


_PREFIX = r'/(?:coding-team|team-coding)[ \t]+'
_FLAGS = re.IGNORECASE | re.ASCII
_FAMILY = re.compile(_PREFIX + r'(leader|antigravity|agy)\b', _FLAGS)
_CONTROL = re.compile(_PREFIX + r'(leader|antigravity|agy[ \t]+auto)[ \t]+([a-z]+)', _FLAGS)
_COMMANDS = {
    'leader': frozenset(('on', 'off', 'status')),
    'antigravity': frozenset(('on', 'off', 'pair', 'status', 'check')),
    'agy auto': frozenset(('on', 'off')),
}
ATTEMPT_OPERATIONS = ('publish', 'reoffer', 'grant-read', 'collect', 'cancel-pending',
                      'reconcile', 'ack', 'cleanup')
OPERATIONS = ('open-round', 'close-round', 'register-native', 'bind-native', 'finish-native',
              *ATTEMPT_OPERATIONS)
_PAYLOAD_LIMIT = 3 * 1024 * 1024
_MACHINE_REASON = re.compile(r'[A-Z][A-Z0-9_]{0,79}')


def is_bridge_control(text):
    return isinstance(text, str) and _FAMILY.match(text.strip(' \t\r\n')) is not None


def parse_control(text):
    match = _CONTROL.fullmatch(text.strip(' \t\r\n')) if isinstance(text, str) else None
    if match is None:
        return None
    family, action = match.groups()
    family, action = ' '.join(family.lower().split()), action.lower()
    return (family, action) if action in _COMMANDS[family] else None


class BridgeInputError(ValueError):
    pass


def _reject(reason):
    raise BridgeInputError(reason)


def _failure(error):
    candidate = getattr(error, 'reason', str(error))
    reason = candidate if isinstance(candidate, str) and _MACHINE_REASON.fullmatch(candidate) else 'BRIDGE_ERROR'
    return {'ok': False, 'state': 'NOT_READY', 'reason': reason}, 2


def _context(platform, workspace, source, env, home):
    from ct_bridge_binding import ctx_from_runtime

    if platform != 'codex':
        _reject('UNSUPPORTED_LEADER')
    return ctx_from_runtime(platform, workspace, env, component='leader', source_root=source, home=home)


def _runtime_view(ctx):
    # This identifies the actual installed entry that produced the status, rather than a candidate path.
    try:
        binding = ctx.own_binding()
    except (OSError, ValueError):
        return {}
    helper = binding.skill_dir / 'scripts' / 'coding_team.py'
    return {'platform': binding.platform, 'scope': binding.scope, 'workspace': ctx.paths.root.as_posix(),
            'helper': helper.as_posix(), 'helper_sha256': hashlib.sha256(helper.read_bytes()).hexdigest(),
            'bridge_abi': 1}


def command(args, source, platform, env, home):
    try:
        parsed = parse_control(args.text)
        if parsed is None:
            _reject('INVALID_BRIDGE_COMMAND')
        if args.chat_id is not None:
            _reject('RUNTIME_CHAT_REQUIRED')
        ctx = _context(platform, Path.cwd(), source, env, home)
        if args.state is not None and args.state.resolve() != ctx.own_binding().policy_path:
            _reject('POLICY_PATH_MISMATCH')
        return _run_control(ctx, *parsed)
    except (OSError, ValueError, TimeoutError) as error:
        return _failure(error)


def _run_control(ctx, family, action):
    from ct_bridge_binding import LeaderRef
    import ct_bridge_control as control

    if family == 'leader':
        operations = {'on': control.leader_on, 'off': control.leader_off, 'status': control.leader_status}
        result = operations[action](ctx)
    elif action in ('status', 'check'):
        result = control.desktop_status(ctx)
    else:
        actor = ctx.require_leader()
        status = control.leader_status(ctx)
        if not status.get('selected'):
            return {'ok': False, 'state': 'NOT_READY', 'reason': 'LEADER_NOT_SELECTED'}, 1
        expected = LeaderRef(actor, status['leader_epoch'])
        if action == 'pair':
            result = control.invite(ctx, expected)
            token = result.pop('ticket', None)
            if token:
                result['prompt'] = (f'/coding-team-worker connect worktree={json.dumps(ctx.paths.root.as_posix(), ensure_ascii=False)} '
                                    f'ticket={token}')
        else:
            toggle = control.set_auto if family == 'agy auto' else control.set_desktop
            result = toggle(ctx, expected, action == 'on')
            if result.get('ok'):
                current = control.desktop_status(ctx)
                result.update(readiness=current['state'], attempts=current.get('attempts', []))
    if action in ('status', 'check'):
        result.update(_runtime_view(ctx))
    return result, (0 if result.get('ok') else 1)


def doctor_status(skill_dir, workspace, env, home):
    try:
        from ct_bridge_control import desktop_status

        ctx = _context('codex', workspace, skill_dir, env, home)
        return dict(desktop_status(ctx), **_runtime_view(ctx))
    except (OSError, ValueError, TimeoutError) as error:
        return _failure(error)[0]


def _pairs(items):
    result = dict(items)
    if len(result) != len(items):
        _reject('INVALID_PAYLOAD')
    return result


def _not_json_constant(_value):
    _reject('INVALID_PAYLOAD')


def _payload(stream, enabled):
    if not enabled:
        return {}
    raw = stream.read(_PAYLOAD_LIMIT + 1)
    if len(raw) > _PAYLOAD_LIMIT:
        _reject('INVALID_PAYLOAD')
    try:
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_not_json_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        _reject('INVALID_PAYLOAD')
    if not isinstance(data, dict):
        _reject('INVALID_PAYLOAD')
    return data


def bridge_command(args, source, env, home, stream):
    try:
        ctx = _context(args.platform, args.workspace, source, env, home)
        payload = _payload(stream, args.payload_stdin)
        operation = args.operation
        if operation == 'collect':
            if payload.keys() - {'grant'}:
                _reject('INVALID_PAYLOAD')
            result = _collect(ctx, _key(args), payload.get('grant'), args.wait_seconds)
        else:
            result = _mutate(ctx, args, payload)
        if (operation in ('publish', 'reoffer') and result.get('ok')
                and result.get('state') == 'WAITING_FOR_USER'):
            key = result['task_id']
            number = result['attempt_no']
            request = ctx.paths.data / key / f'attempt-{number}' / 'request.md'
            result.update(request_path=request.as_posix(), prompt=(
                'Use skill coding-team-desktop-worker for this manual assignment. '
                f'Worktree {json.dumps(ctx.paths.root.as_posix(), ensure_ascii=False)}. '
                f'Task {key}, attempt {number}. Request {json.dumps(request.as_posix(), ensure_ascii=False)}.'
            ))
        return result, (0 if result.get('ok') else 1)
    except (OSError, ValueError, TimeoutError) as error:
        return _failure(error)


def _collect(ctx, key, grant, wait):
    from ct_bridge_attempts import collect

    ctx.own_binding()
    if grant is not None and not isinstance(grant, str):
        _reject('INVALID_PAYLOAD')
    return collect(ctx, key, grant, wait_seconds=wait)


def _key(args):
    from ct_bridge_files import AttemptKey

    if args.task_id is None or args.attempt is None:
        _reject('ATTEMPT_REQUIRED')
    return AttemptKey(args.task_id, args.attempt)


def _mutate(ctx, args, payload):
    import ct_bridge_attempts as attempts
    from ct_bridge_binding import LeaderRef
    import ct_bridge_control as control

    actor = ctx.require_leader()
    expected = LeaderRef(actor, payload.pop('leader_epoch', None))
    fields = {
        'open-round': (), 'close-round': ('round_id', 'evidence'),
        'register-native': ('round_id', 'participant_id'),
        'bind-native': ('round_id', 'participant_id', 'handle'),
        'finish-native': ('round_id', 'participant_id', 'evidence'),
        'publish': ('round_id', 'request'), 'reoffer': ('mode',), 'grant-read': (),
        'cancel-pending': (), 'reconcile': ('evidence',), 'ack': ('result_id',), 'cleanup': (),
    }[args.operation]
    if payload.keys() != set(fields) or args.wait_seconds != 0:
        _reject('INVALID_PAYLOAD')
    operation = args.operation
    detail = tuple(payload[field] for field in fields)
    if operation == 'open-round':
        round_id = control.open_round(ctx, expected)
        return {'ok': True, 'state': 'ROUND_OPEN', 'reason': None, 'round_id': round_id,
                'leader_epoch': expected.epoch}
    if operation == 'publish':
        return attempts.publish(ctx, expected, payload['round_id'], _request(payload['request'], attempts.Request))
    if operation == 'cleanup':
        return attempts.cleanup(ctx, expected, args.task_id)
    module = attempts if operation in ATTEMPT_OPERATIONS else control
    prefix = (_key(args),) if module is attempts else ()
    return getattr(module, operation.replace('-', '_'))(ctx, expected, *prefix, *detail)


def _request(data, request_type):
    names = {'publish_id', 'content', 'kind', 'mode', 'scope', 'baseline', 'task_id'}
    if (not isinstance(data, dict) or data.keys() - names or 'publish_id' not in data
            or not isinstance(data.get('content'), str)):
        _reject('INVALID_REQUEST')
    return request_type(**dict(data, content=data['content'].encode('utf-8')))
