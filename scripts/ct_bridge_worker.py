"""Antigravity Desktop worker entry; credentials travel only in JSON transport.

This helper does not launch an app, choose a model or modify native policy.
Every invocation resolves the current chat from the trusted app environment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

import ct_bridge_binding as binding
import ct_bridge_files as files

COMMANDS = ('connect', 'status', 'listen', 'begin', 'resume', 'finish')
MAX_PAYLOAD_BYTES = files.MAX_FILE_BYTES
_REASON = re.compile(r'[A-Z][A-Z0-9_]{0,63}\Z', re.ASCII)
_TASK = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z', re.ASCII)


class WorkerInputError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default echoes the raw argv, which may contain a pasted token.
        raise WorkerInputError('INVALID_ARGUMENTS')


def _arguments(argv):
    parser = _Parser(add_help=False, allow_abbrev=False)
    parser.add_argument('operation', choices=COMMANDS)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--task-id')
    parser.add_argument('--attempt', type=int)
    parser.add_argument('--payload-file')
    args = parser.parse_args(argv)
    keyed = args.operation in ('begin', 'resume', 'finish')
    if keyed:
        if (not args.task_id or not _TASK.fullmatch(args.task_id)
                or args.attempt is None or not 1 <= args.attempt <= 2147483647):
            raise WorkerInputError('INVALID_ARGUMENTS')
    elif args.task_id is not None or args.attempt is not None:
        raise WorkerInputError('INVALID_ARGUMENTS')
    if args.operation in ('status', 'listen') and args.payload_file is not None:
        raise WorkerInputError('INVALID_ARGUMENTS')
    return args


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise WorkerInputError('INVALID_PAYLOAD')
        value[key] = item
    return value


def _secret(value, *, optional=False):
    if optional and value is None:
        return
    if not isinstance(value, str) or not value or len(value) > 512:
        raise WorkerInputError('INVALID_PAYLOAD')


def _payload(args, ctx, stdin):
    if args.operation in ('status', 'listen'):
        return {}
    if args.payload_file is not None:
        raw = files.consume_payload_file(ctx.paths, Path(args.payload_file))
    else:
        raw = stdin.read(MAX_PAYLOAD_BYTES + 1)
    if isinstance(raw, str):
        try:
            raw = raw.encode('utf-8')
        except UnicodeError:
            raise WorkerInputError('INVALID_PAYLOAD') from None
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise WorkerInputError('PAYLOAD_TOO_LARGE')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique) if raw else {}
    except (ValueError, UnicodeError, RecursionError):
        raise WorkerInputError('INVALID_PAYLOAD') from None
    schemas = {'connect': ({'ticket'}, {'ticket'}), 'begin': (set(), {'claim'}),
               'resume': ({'execution_ticket'}, {'execution_ticket'}),
               'finish': ({'execution_ticket', 'state', 'result'}, {'execution_ticket', 'state', 'result'})}
    required, allowed = schemas[args.operation]
    if not isinstance(value, dict) or not required <= value.keys() or not value.keys() <= allowed:
        raise WorkerInputError('INVALID_PAYLOAD')
    for key in ('ticket', 'execution_ticket'):
        if key in value:
            _secret(value[key])
    if 'claim' in value:
        _secret(value['claim'], optional=True)
    if args.operation == 'finish':
        if value['state'] not in ('COMPLETED', 'FAILED', 'CANCELED', 'NEEDS_USER', 'OUTCOME_UNKNOWN'):
            raise WorkerInputError('INVALID_PAYLOAD')
        if not isinstance(value['result'], str):
            raise WorkerInputError('INVALID_PAYLOAD')
        try:
            value['result'] = value['result'].encode('utf-8')
        except UnicodeError:
            raise WorkerInputError('INVALID_PAYLOAD') from None
    return value


def _dispatch(args, ctx, payload):
    import ct_bridge_control as control
    if args.operation == 'connect':
        return control.connect(ctx, payload['ticket'])
    if args.operation == 'status':
        return control.worker_status(ctx)
    import ct_bridge_attempts as attempts
    if args.operation == 'listen':
        return attempts.listen(ctx)
    key = files.AttemptKey(args.task_id, args.attempt)
    if args.operation == 'begin':
        return attempts.begin(ctx, key, payload.get('claim'))
    if args.operation == 'resume':
        return attempts.resume(ctx, key, payload['execution_ticket'])
    return attempts.finish(ctx, key, payload['execution_ticket'], payload['state'], payload['result'])


def main(argv=None, *, stdin=None, stdout=None, env=None, source_root=None, home=None):
    """Emit exactly one JSON reply. Keyword boundaries permit isolated fixture runs."""
    stdin = stdin if stdin is not None else getattr(sys.stdin, 'buffer', sys.stdin)
    stdout = stdout if stdout is not None else sys.stdout
    try:
        args = _arguments(sys.argv[1:] if argv is None else argv)
        ctx = binding.ctx_from_runtime('antigravity', args.workspace, os.environ if env is None else env,
            component='worker', source_root=Path(__file__).resolve().parents[1] if source_root is None else source_root,
            home=Path.home() if home is None else home)
        ctx.require_actor()
        installed = ctx.own_binding()
        payload = _payload(args, ctx, stdin)
        view = _dispatch(args, ctx, payload)
        if not isinstance(view, dict) or type(view.get('ok')) is not bool or not isinstance(view.get('state'), str):
            raise WorkerInputError('INVALID_HELPER_REPLY')
        view.setdefault('reason', None)
        # Helpers return stable reason codes. Never propagate arbitrary exception
        # text (paths, JSON content and tokens) through their error views either.
        reason = view.get('reason')
        user_pause = view['ok'] and view['state'] in ('NEEDS_USER', 'OUTCOME_UNKNOWN')
        if ((reason is not None and not user_pause and
             (not isinstance(reason, str) or not _REASON.fullmatch(reason)))
                or (not view['ok'] and reason is None)):
            view = {'ok': False, 'state': 'BRIDGE_OPERATION_FAILED', 'reason': 'BRIDGE_OPERATION_FAILED'}
        if view['ok'] and args.operation == 'status':
            helper = installed.skill_dir / 'scripts' / 'ct_bridge_worker.py'
            view.update(helper=helper.as_posix(), helper_sha256=hashlib.sha256(helper.read_bytes()).hexdigest(),
                        workspace=ctx.paths.root.as_posix(), scope=installed.scope, bridge_abi=binding.BRIDGE_ABI)
        encoded = json.dumps(view, ensure_ascii=True, separators=(',', ':'))
        code = 0 if view['ok'] else 2
    except Exception as exc:
        reason = getattr(exc, 'reason', None)
        if not isinstance(reason, str) or not _REASON.fullmatch(reason):
            reason = 'HELPER_UNAVAILABLE' if isinstance(exc, ImportError) else 'BRIDGE_OPERATION_FAILED'
        encoded = json.dumps({'ok': False, 'state': reason, 'reason': reason}, separators=(',', ':'))
        code = 2
    stdout.write(encoded + '\n')
    stdout.flush()
    return code


if __name__ == '__main__':
    raise SystemExit(main())
