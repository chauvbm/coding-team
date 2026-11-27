"""Worktree-local Desktop leadership, pairing and coordination rounds.

All authority decisions are made in the writer transaction. Read-only status
views deliberately neither initialize policy nor create bridge storage.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from functools import wraps
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import time
import uuid

import ct_bridge_binding as binding
from ct_bridge_commands import supports_pair
from ct_bridge_store import Store
import ct_policy
import ct_storage

RoundId = str
PENDING = ('PUBLISHING', 'WAITING_FOR_USER', 'QUEUED', 'CLAIMED')
TERMINAL = ('COMPLETED', 'FAILED', 'CANCELED', 'REVOKED')


def _view(state, *, ok=True, reason=None, **fields):
    return {'ok': ok, 'state': state, 'reason': reason, **fields}


def _view_operation(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (ValueError, OSError, TimeoutError) as exc:
            reason = getattr(exc, 'reason', None) or str(exc)
            return _view(reason, ok=False, reason=reason)
    return call


def _group(db):
    return db.execute('SELECT * FROM group_state WHERE id=1').fetchone()


def context_leader(db):
    row = _group(db)
    if row is None or row['leader_chat'] is None:
        return None
    return binding.LeaderRef(binding.Actor(row['leader_platform'], row['leader_chat']), row['leader_epoch'])


def require_leader(db, ctx, expected):
    """Fence both caller identity and the authority epoch, inside the transaction."""
    actor = ctx.require_leader()
    row = _group(db)
    if (not isinstance(expected, binding.LeaderRef) or actor != expected.actor
            or row is None or row['leader_platform'] != actor.platform
            or row['leader_chat'] != actor.chat_id or row['leader_epoch'] != expected.epoch
            or row['leader_manifest'] != _manifest(ctx.own_binding())):
        raise ValueError('STALE_LEADER')
    return row


def _manifest(component):
    return Path(component.manifest_path).resolve().as_posix()


def matches_component(ctx, manifest_path):
    return manifest_path == (ctx.source_root / binding.MANIFEST_NAME).as_posix()


def _placement(row):
    return None if row is None else (row['leader_manifest'], row['worker_manifest'])


def _register_worktree(component, root):
    # Deferred import keeps control independent from the installer's CLI.
    from ct_bridge_install import register_worktree
    register_worktree(component.install_lock, root)


@contextmanager
def _guard(ctx, *, terminal=False, use_worker=True):
    """Lock installed components before policy and SQLite, then recheck placement."""
    ctx.require_actor()
    own = ctx.own_binding()
    store = Store(ctx.paths)
    with store.read() as db:
        snapshot = dict(_group(db)) if db is not None else None
    if snapshot is None or not snapshot['leader_manifest']:
        raise ValueError('LEADER_NOT_SELECTED')
    leader = own if matches_component(ctx, snapshot['leader_manifest']) else ctx.leader_binding(snapshot['leader_manifest'])
    worker = None
    if (use_worker and snapshot['worker_manifest'] and not (terminal and ctx.component == 'leader')):
        worker = own if matches_component(ctx, snapshot['worker_manifest']) else ctx.worker_binding(snapshot['worker_manifest'])
    components = [part for part in (own, leader, worker) if part is not None]
    with binding.locked_bindings(*components, require_clean_journal=not terminal):
        with ct_storage.locked(leader.policy_path):
            with store.write() as db:
                row = _group(db)
                if _placement(row) != _placement(snapshot):
                    raise ValueError('BINDING_CHANGED')
                yield store, db, leader, worker


def _policy(leader, row):
    return ct_policy.policy_stamp(leader.policy_path, row['leader_chat'], require_epochs=True)


def _require_policy(leader, row):
    stamp = _policy(leader, row)
    if stamp.effective != 'ON':
        raise ValueError('NATIVE_POLICY_OFF')
    return stamp


def _busy(db):
    return [row['id'] for row in db.execute('SELECT id FROM rounds WHERE closed_at IS NULL ORDER BY opened_at,id')]


def _busy_view(db):
    rounds = _busy(db)
    return _view('LEADER_BUSY', ok=False, reason='LEADER_BUSY', round_id=rounds[0], round_ids=rounds) if rounds else None


def _open_attempts(db):
    return [dict(row) for row in db.execute('''SELECT r.id AS round_id,a.task_id,a.attempt_no,a.mode,a.state,
        CASE WHEN a.result_id IS NOT NULL AND a.ack_at IS NULL THEN 1 ELSE 0 END AS review_pending
        FROM attempts a JOIN tasks t ON a.task_id=t.id JOIN rounds r ON t.round_id=r.id
        WHERE r.closed_at IS NULL ORDER BY a.created_at,a.task_id,a.attempt_no''')]


def _reset_authority(db, platform, chat, manifest):
    db.execute('''UPDATE group_state SET leader_platform=?,leader_chat=?,leader_manifest=?,
        leader_epoch=leader_epoch+1,desktop_enabled=0,desktop_epoch=desktop_epoch+1,
        auto_enabled=0,auto_epoch=auto_epoch+1,pair_epoch=pair_epoch+1,
        worker_platform=NULL,worker_chat=NULL,worker_manifest=NULL,
        invite_hash=NULL,invite_expires_at=NULL WHERE id=1''', (platform, chat, manifest))
    db.execute('DELETE FROM waiter')


@_view_operation
def leader_on(ctx):
    actor = ctx.require_leader()
    own = ctx.own_binding()
    with binding.locked_bindings(own), Store(ctx.paths).write() as db:
        row = _group(db)
        if (row['leader_platform'], row['leader_chat'], row['leader_manifest']) == (
                actor.platform, actor.chat_id, _manifest(own)):
            return _view('LEADER_SELECTED', leader_epoch=row['leader_epoch'])
        busy = _busy_view(db)
        if busy:
            return busy
        _reset_authority(db, actor.platform, actor.chat_id, _manifest(own))
        return _view('LEADER_SELECTED', leader_epoch=_group(db)['leader_epoch'])


@_view_operation
def leader_off(ctx):
    actor = ctx.require_leader()
    own = ctx.own_binding()
    store = Store(ctx.paths)
    with store.read() as db:
        if db is None:
            return _view('NOT_LEADER', ok=False, reason='NOT_LEADER')
    with binding.locked_bindings(own, require_clean_journal=False), store.write() as db:
        row = _group(db)
        if (row['leader_platform'], row['leader_chat'], row['leader_manifest']) != (
                actor.platform, actor.chat_id, _manifest(own)):
            return _view('NOT_LEADER', ok=False, reason='NOT_LEADER')
        busy = _busy_view(db)
        if busy:
            return busy
        _reset_authority(db, None, None, None)
        return _view('LEADER_RELEASED', leader_epoch=_group(db)['leader_epoch'])


@_view_operation
def leader_status(ctx):
    with Store(ctx.paths).read() as db:
        row = _group(db) if db is not None else None
        selected = row is not None and row['leader_chat'] is not None
        return _view('LEADER_SELECTED' if selected else 'LEADER_NOT_SELECTED', selected=selected,
            leader={'platform': row['leader_platform'], 'chat_id': row['leader_chat']} if selected else None,
            leader_epoch=row['leader_epoch'] if row else 0, round_ids=_busy(db) if db is not None else [],
            attempts=_open_attempts(db) if db is not None else [])


def open_round(ctx, expected):
    with _guard(ctx) as (store, db, leader, worker):
        row = require_leader(db, ctx, expected)
        _require_policy(leader, row)
        for component in (leader, worker):
            if component is not None:
                _register_worktree(component, ctx.paths.root)
        round_id = 'R-' + uuid.uuid4().hex
        db.execute('''INSERT INTO rounds(id,leader_epoch,leader_platform,leader_chat,leader_manifest,opened_at)
            VALUES(?,?,?,?,?,?)''', (round_id, expected.epoch, expected.actor.platform, expected.actor.chat_id,
                                    row['leader_manifest'], store.clock()))
        return round_id


def _round(db, expected, round_id, *, allow_closed=False):
    row = db.execute('SELECT * FROM rounds WHERE id=?', (round_id,)).fetchone()
    if (row is None or row['leader_epoch'] != expected.epoch
            or row['leader_platform'] != expected.actor.platform or row['leader_chat'] != expected.actor.chat_id):
        raise ValueError('ROUND_MISMATCH')
    if not allow_closed and row['closed_at'] is not None:
        raise ValueError('ROUND_CLOSED')
    return row


def _evidence(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('EVIDENCE_REQUIRED')
    return value


@_view_operation
def close_round(ctx, expected, round_id, evidence):
    evidence = _evidence(evidence)
    with _guard(ctx, terminal=True) as (store, db, _, _):
        require_leader(db, ctx, expected)
        row = _round(db, expected, round_id, allow_closed=True)
        if row['closed_at'] is not None:
            if row['evidence'] != evidence:
                raise ValueError('EVIDENCE_CONFLICT')
            return _view('ROUND_CLOSED', round_id=round_id)
        native = db.execute('SELECT id FROM native_participants WHERE round_id=? AND finished_at IS NULL', (round_id,)).fetchall()
        unresolved = db.execute('''SELECT a.task_id,a.attempt_no,a.state FROM attempts a JOIN tasks t ON t.id=a.task_id
            WHERE t.round_id=? AND (a.state NOT IN ('COMPLETED','FAILED','CANCELED','REVOKED')
            OR (a.result_id IS NOT NULL AND a.ack_at IS NULL))''', (round_id,)).fetchall()
        if native or unresolved:
            return _view('ROUND_BUSY', ok=False, reason='ROUND_BUSY', round_id=round_id,
                native_participants=[row['id'] for row in native], attempts=[dict(row) for row in unresolved])
        db.execute('UPDATE rounds SET closed_at=?,evidence=? WHERE id=? AND closed_at IS NULL',
                   (store.clock(), evidence, round_id))
        return _view('ROUND_CLOSED', round_id=round_id)


def _participant_id(value):
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 256:
        raise ValueError('INVALID_PARTICIPANT')
    return value


@_view_operation
def register_native(ctx, expected, round_id, participant_id):
    participant_id = _participant_id(participant_id)
    with _guard(ctx) as (_, db, leader, _):
        row = require_leader(db, ctx, expected)
        _require_policy(leader, row)
        _round(db, expected, round_id)
        db.execute('INSERT OR IGNORE INTO native_participants(round_id,id) VALUES(?,?)', (round_id, participant_id))
        return _view('NATIVE_REGISTERED', round_id=round_id, participant_id=participant_id)


@_view_operation
def bind_native(ctx, expected, round_id, participant_id, handle):
    handle = _participant_id(handle)
    with _guard(ctx) as (_, db, _, _):
        require_leader(db, ctx, expected)
        _round(db, expected, round_id)
        row = db.execute('SELECT * FROM native_participants WHERE round_id=? AND id=?', (round_id, participant_id)).fetchone()
        if row is None or row['finished_at'] is not None or row['handle'] not in (None, handle):
            raise ValueError('NATIVE_PARTICIPANT_CONFLICT')
        db.execute('UPDATE native_participants SET handle=? WHERE round_id=? AND id=? AND finished_at IS NULL',
                   (handle, round_id, participant_id))
        return _view('NATIVE_BOUND', round_id=round_id, participant_id=participant_id)


@_view_operation
def finish_native(ctx, expected, round_id, participant_id, evidence):
    evidence = _evidence(evidence)
    with _guard(ctx, terminal=True) as (store, db, _, _):
        require_leader(db, ctx, expected)
        _round(db, expected, round_id)
        row = db.execute('SELECT * FROM native_participants WHERE round_id=? AND id=?', (round_id, participant_id)).fetchone()
        if row is None:
            raise ValueError('NATIVE_PARTICIPANT_UNKNOWN')
        if row['finished_at'] is not None and row['evidence'] != evidence:
            raise ValueError('EVIDENCE_CONFLICT')
        db.execute('''UPDATE native_participants SET finished_at=?,evidence=?
            WHERE round_id=? AND id=? AND finished_at IS NULL''', (store.clock(), evidence, round_id, participant_id))
        return _view('NATIVE_FINISHED', round_id=round_id, participant_id=participant_id)


def _revoke_pending(db, *, auto_only=False):
    db.execute('''UPDATE attempts SET state='REVOKED',claim_hash=NULL,claim_expires_at=NULL,reason=?
        WHERE state IN ('PUBLISHING','WAITING_FOR_USER','QUEUED','CLAIMED')''' + (" AND mode='auto'" if auto_only else ''),
        ('AUTO_OFF' if auto_only else 'DESKTOP_OFF',))
    db.execute('DELETE FROM waiter')


def _listen_invite(value):
    return isinstance(value, str) and re.fullmatch(r'listen:[0-9a-f]{32}', value, re.ASCII) is not None


def _new_invite(store, db):
    db.execute('''UPDATE group_state SET invite_hash=?,invite_expires_at=?,pair_epoch=pair_epoch+1,
        worker_platform=NULL,worker_chat=NULL,worker_manifest=NULL WHERE id=1''',
        ('listen:' + uuid.uuid4().hex, store.clock() + 600.0))
    db.execute('DELETE FROM waiter')


def _activate(store, db):
    """Re-enabling may discard an old pair only after its rounds have drained."""
    row = _group(db)
    if not row['desktop_enabled']:
        busy = _busy_view(db)
        if busy:
            return busy
        db.execute('''UPDATE group_state SET desktop_enabled=1,desktop_epoch=desktop_epoch+1,
            auto_enabled=0,auto_epoch=auto_epoch+CASE WHEN auto_enabled=1 THEN 1 ELSE 0 END WHERE id=1''')
        _new_invite(store, db)
    return None


def _disable(db):
    db.execute('''UPDATE group_state SET desktop_epoch=desktop_epoch+CASE WHEN desktop_enabled=1 THEN 1 ELSE 0 END,
        desktop_enabled=0,invite_hash=NULL,invite_expires_at=NULL WHERE id=1''')
    _revoke_pending(db)


def _configuration(ctx, row, state='CONFIGURED', **fields):
    return _view(state, workspace=str(ctx.paths.root),
        **{name: row[name] for name in ('leader_epoch', 'pair_epoch', 'desktop_epoch', 'auto_epoch',
            'desktop_enabled', 'auto_enabled', 'invite_expires_at')}, **fields)


@_view_operation
def configure_peer(ctx, target: str, action: str):
    actor = ctx.require_leader()
    if not supports_pair(actor.platform, target):
        raise ValueError('UNSUPPORTED_PAIR')
    if action not in ('on', 'off', 'auto_on', 'auto_off'):
        raise ValueError('INVALID_ACTION')
    own = ctx.own_binding()
    store = Store(ctx.paths)
    # An OFF from an unselected chat cannot initialize the bridge.
    if action == 'off':
        with store.read() as db:
            if db is None:
                return _view('NOT_LEADER', ok=False, reason='NOT_LEADER')
    with binding.locked_bindings(own, require_clean_journal=action != 'off'):
        with ct_storage.locked(own.policy_path):
            # Validate policy before opening the first writer/create path.
            if action != 'off':
                stamp = ct_policy.policy_stamp(own.policy_path, actor.chat_id, require_epochs=True)
                if stamp.effective != 'ON':
                    raise ValueError('NATIVE_POLICY_OFF')
            with store.write() as db:
                row = _group(db)
                owner = (row['leader_platform'], row['leader_chat'], row['leader_manifest']) == (
                    actor.platform, actor.chat_id, _manifest(own))
                if action == 'off':
                    if not owner:
                        raise ValueError('NOT_LEADER')
                    _disable(db)
                    return _configuration(ctx, _group(db), 'DESKTOP_OFF')
                if not owner:
                    busy = _busy_view(db)
                    if busy:
                        return _configuration(ctx, row, **busy)
                    _reset_authority(db, actor.platform, actor.chat_id, _manifest(own))
                busy = _activate(store, db)
                if busy:
                    return _configuration(ctx, _group(db), **busy)
                row = _group(db)
                mode = action == 'auto_on' if action != 'on' else bool(row['auto_enabled'])
                if bool(row['auto_enabled']) != mode:
                    db.execute('UPDATE group_state SET auto_enabled=?,auto_epoch=auto_epoch+1 WHERE id=1', (int(mode),))
                    db.execute('DELETE FROM waiter')
                if not mode:
                    _revoke_pending(db, auto_only=True)
                row = _group(db)
                if not row['worker_chat'] and not (_listen_invite(row['invite_hash'])
                        and row['invite_expires_at'] > store.clock()):
                    busy = _busy_view(db)
                    if busy:
                        # No invitation generation can change while a round owns it.
                        raise ValueError('LEADER_BUSY')
                    _new_invite(store, db)
                return _configuration(ctx, _group(db))


@_view_operation
def set_desktop(ctx, expected, enabled):
    if type(enabled) is not bool:
        raise ValueError('INVALID_ENABLED')
    with _guard(ctx, terminal=not enabled, use_worker=False) as (store, db, leader, _):
        row = require_leader(db, ctx, expected)
        if enabled:
            _require_policy(leader, row)
            busy = _activate(store, db)
            if busy:
                return busy
        else:
            _disable(db)
        return _view('DESKTOP_ON' if enabled else 'DESKTOP_OFF', desktop_enabled=enabled,
                     desktop_epoch=_group(db)['desktop_epoch'])


@_view_operation
def set_auto(ctx, expected, enabled):
    if type(enabled) is not bool:
        raise ValueError('INVALID_ENABLED')
    with _guard(ctx, terminal=not enabled, use_worker=False) as (_, db, leader, _):
        row = require_leader(db, ctx, expected)
        if enabled:
            _require_policy(leader, row)
            if not row['desktop_enabled']:
                raise ValueError('DESKTOP_OFF')
        if bool(row['auto_enabled']) != enabled:
            db.execute('UPDATE group_state SET auto_enabled=?,auto_epoch=auto_epoch+1 WHERE id=1', (int(enabled),))
            db.execute('DELETE FROM waiter')
        if not enabled:
            _revoke_pending(db, auto_only=True)
        return _view('AUTO_ON' if enabled else 'AUTO_OFF', auto_enabled=enabled, auto_epoch=_group(db)['auto_epoch'])


def _ticket_hash(ticket):
    if not isinstance(ticket, str) or not ticket or len(ticket) > 512:
        raise ValueError('INVALID_TICKET')
    return hashlib.sha256(ticket.encode('utf-8')).hexdigest()


@_view_operation
def invite(ctx, expected):
    with _guard(ctx, use_worker=False) as (store, db, leader, _):
        row = require_leader(db, ctx, expected)
        _require_policy(leader, row)
        busy = _busy_view(db)
        if busy:
            return busy
        ticket = secrets.token_urlsafe(32)
        expires = store.clock() + 600.0
        db.execute('''UPDATE group_state SET invite_hash=?,invite_expires_at=?,pair_epoch=pair_epoch+1,
            worker_platform=NULL,worker_chat=NULL,worker_manifest=NULL WHERE id=1''', (_ticket_hash(ticket), expires))
        db.execute('DELETE FROM waiter')
        return _view('INVITED', ticket=ticket, expires_at=expires, workspace=str(ctx.paths.root),
                     leader_epoch=row['leader_epoch'], pair_epoch=row['pair_epoch'] + 1)


@_view_operation
def connect(ctx, ticket):
    actor = ctx.require_actor()
    if ctx.component != 'worker' or actor.platform != 'antigravity':
        raise ValueError('WORKER_PLATFORM_UNSUPPORTED')
    digest = _ticket_hash(ticket)
    with _guard(ctx) as (store, db, leader, _):
        row = _group(db)
        if (not row['invite_hash'] or row['invite_expires_at'] is None
                or store.clock() >= row['invite_expires_at']
                or not hmac.compare_digest(digest, row['invite_hash'])):
            raise ValueError('INVALID_OR_EXPIRED_TICKET')
        _require_policy(leader, row)
        busy = _busy_view(db)
        if busy:
            return busy
        _connect_worker(ctx, db, leader)
        return _view('CONNECTED', leader_epoch=row['leader_epoch'], pair_epoch=row['pair_epoch'])


def _connect_worker(ctx, db, leader):
    actor = ctx.require_actor()
    worker = ctx.own_binding()
    _register_worktree(leader, ctx.paths.root)
    _register_worktree(worker, ctx.paths.root)
    db.execute('''UPDATE group_state SET worker_platform=?,worker_chat=?,worker_manifest=?,
        invite_hash=NULL,invite_expires_at=NULL WHERE id=1''', (actor.platform, actor.chat_id, _manifest(worker)))
    db.execute('DELETE FROM waiter')


@contextmanager
def _join_guard(ctx):
    try:
        with _guard(ctx) as guarded:
            yield guarded
    except ValueError as exc:
        if str(exc) == 'BINDING_CHANGED':
            raise ValueError('STALE_INVITE') from None
        raise


@_view_operation
def join_peer(ctx):
    actor = ctx.require_actor()
    if ctx.component != 'worker' or actor.platform != 'antigravity':
        raise ValueError('WORKER_PLATFORM_UNSUPPORTED')
    own = ctx.own_binding()
    store = Store(ctx.paths)
    with store.read() as db:
        observed = dict(_group(db)) if db is not None else None
    if not observed or not observed['leader_chat']:
        raise ValueError('LEADER_NOT_SELECTED')
    with _join_guard(ctx) as (store, db, leader, _):
        row = _group(db)
        # A new Leader may occupy the identical managed installation location.
        authority = ('leader_platform', 'leader_chat', 'leader_manifest', 'leader_epoch',
                     'pair_epoch', 'desktop_epoch')
        if any(row[name] != observed[name] for name in authority):
            raise ValueError('STALE_INVITE')
        if not supports_pair(row['leader_platform'], actor.platform):
            raise ValueError('UNSUPPORTED_PAIR')
        _require_policy(leader, row)
        if not row['desktop_enabled']:
            raise ValueError('DESKTOP_OFF')
        if row['worker_chat']:
            if (row['worker_platform'], row['worker_chat'], row['worker_manifest']) != (
                    actor.platform, actor.chat_id, _manifest(own)):
                raise ValueError('WRONG_WORKER')
            return _configuration(ctx, row, 'CONNECTED', paired=True)
        if (row['invite_hash'], row['invite_expires_at']) != (
                observed['invite_hash'], observed['invite_expires_at']):
            raise ValueError('STALE_INVITE')
        if not _listen_invite(row['invite_hash']):
            raise ValueError('NO_INVITE')
        if row['invite_expires_at'] is None or store.clock() >= row['invite_expires_at']:
            raise ValueError('EXPIRED')
        busy = _busy_view(db)
        if busy:
            return busy
        _connect_worker(ctx, db, leader)
        return _configuration(ctx, _group(db), 'CONNECTED', paired=True)


def _snapshot(store, *, poll=False):
    with (store.poll_read() if poll else store.read()) as db:
        row = dict(_group(db)) if db is not None else None
        rounds = _busy(db) if db is not None else []
        attempts = _open_attempts(db) if db is not None else []
        waiter = dict(db.execute('SELECT * FROM waiter WHERE id=1').fetchone() or {}) if db is not None else {}
    return row, rounds, attempts, waiter, store.clock()


def _desktop_status(ctx, *, worker_only=False, snapshot=None):
    row, rounds, attempts, waiter, observed_at = snapshot or _snapshot(Store(ctx.paths))
    selected = bool(row and row['leader_chat'])
    desktop = bool(row and row['desktop_enabled'])
    auto = bool(row and row['auto_enabled'])
    paired = bool(row and row['worker_chat'])
    policy, policy_stamp, problem = None, None, None
    if worker_only:
        actor = ctx.require_actor()
        if ctx.component != 'worker' or actor.platform != 'antigravity':
            problem = 'WORKER_PLATFORM_UNSUPPORTED'
        elif paired and (row['worker_platform'], row['worker_chat'], row['worker_manifest']) != (
                actor.platform, actor.chat_id, _manifest(ctx.own_binding())):
            problem = 'WRONG_WORKER'
    if selected:
        try:
            leader = ctx.leader_binding(row['leader_manifest'])
            leader.verify(require_clean_journal=True)
            if paired:
                ctx.worker_binding(row['worker_manifest']).verify(require_clean_journal=True)
            stamp = _policy(leader, row)
            policy = stamp.effective
            policy_stamp = json.dumps(asdict(stamp), sort_keys=True, separators=(',', ':'))
        except (ValueError, OSError) as exc:
            problem = problem or getattr(exc, 'reason', None) or str(exc)
    elif ctx.component == 'leader':
        try:
            own = ctx.own_binding()
            own.verify(require_clean_journal=True)
            stamp = ct_policy.policy_stamp(own.policy_path,
                ctx.actor.chat_id if ctx.actor is not None else None, require_epochs=True)
            policy = stamp.effective
        except (ValueError, OSError) as exc:
            problem = problem or getattr(exc, 'reason', None) or str(exc)
    waiter_ready = bool(waiter and row and paired and policy_stamp is not None
        and waiter['expires_at'] > observed_at and waiter['policy_stamp'] == policy_stamp
        and all(waiter[name] == row[name] for name in ('leader_epoch', 'pair_epoch', 'auto_epoch', 'desktop_epoch')))
    reason = (problem or ('LEADER_NOT_SELECTED' if not selected else 'NATIVE_POLICY_OFF' if policy != 'ON'
        else 'DESKTOP_OFF' if not desktop else 'NOT_PAIRED' if not paired
        else 'WAITER_NOT_READY' if auto and not waiter_ready else None))
    return _view('READY' if reason is None else reason, reason=reason, ready=reason is None,
        selected=selected, leader={'platform': row['leader_platform'], 'chat_id': row['leader_chat']} if selected else None,
        leader_epoch=row['leader_epoch'] if row else 0, effective_policy=policy,
        desktop_enabled=desktop, auto_enabled=auto, paired=paired, waiter_ready=waiter_ready,
        round_ids=rounds, attempts=attempts if problem != 'WRONG_WORKER' else [],
        pair_epoch=row['pair_epoch'] if row else 0,
        desktop_epoch=row['desktop_epoch'] if row else 0, auto_epoch=row['auto_epoch'] if row else 0,
        invite_expires_at=row['invite_expires_at'] if row else None, observed_at=observed_at)


def _connection_status(ctx, snapshot):
    row, rounds, attempts, _, observed_at = snapshot
    view = _desktop_status(ctx, snapshot=snapshot)
    actor = ctx.actor
    app = actor.platform if actor is not None else ('antigravity' if ctx.component == 'worker' else 'codex')
    role, problem = ('unknown' if actor is None else 'unbound'), None
    try:
        own = ctx.own_binding()
        own.verify(require_clean_journal=True)
        if row and actor is not None:
            for candidate in ('leader', 'worker'):
                if (row[candidate + '_platform'], row[candidate + '_chat'], row[candidate + '_manifest']) == (
                        actor.platform, actor.chat_id, _manifest(own)):
                    role = candidate
                    break
    except (ValueError, OSError) as exc:
        problem = getattr(exc, 'reason', None) or str(exc)
    reason = problem or view['reason']
    if actor is None:
        reason = problem or 'UNKNOWN_CHAT'
    elif role == 'unbound' and row and row['leader_chat'] and (ctx.component != 'worker' or row['worker_chat']):
        reason = problem or ('WRONG_WORKER' if ctx.component == 'worker' else 'NOT_LEADER')
    readiness = reason
    next_action = '/coding-team help'
    if reason is None or reason in ('NOT_PAIRED', 'WAITER_NOT_READY', 'DESKTOP_OFF'):
        if not view['desktop_enabled']:
            readiness, next_action = 'OFF', '/coding-team agy on'
        elif any(attempt['state'] in ('RUNNING', 'NEEDS_USER', 'OUTCOME_UNKNOWN') for attempt in attempts):
            readiness, next_action = 'WORKING', '/coding-team status'
        elif any(attempt['review_pending'] for attempt in attempts):
            readiness, next_action = 'REVIEW', '/coding-team status'
        elif not view['paired']:
            expires = row['invite_expires_at'] if row else None
            expired = expires is not None and observed_at >= expires
            readiness = 'EXPIRED' if expired else 'PAIRING'
            reason = 'EXPIRED' if expired else 'NOT_PAIRED'
            next_action = '/coding-team agy on' if expired else '/coding-team listen'
        elif view['auto_enabled'] and not view['waiter_ready']:
            readiness, next_action = 'WAITING', '/coding-team listen'
        else:
            readiness = 'AUTO_READY' if view['auto_enabled'] else 'MANUAL_READY'
            next_action = None
    if reason == 'NATIVE_POLICY_OFF':
        next_action = '/coding-team on'
    local_attempts = attempts if role in ('leader', 'worker') else []
    connection = {'app': 'antigravity' if app == 'codex' else 'codex',
        **{key: view[key] for key in ('desktop_enabled', 'auto_enabled', 'paired', 'waiter_ready')},
        'readiness': readiness, 'reason': reason, 'next_action': next_action,
        'next_action_app': row['leader_platform'] if row else None,
        'next_action_chat': row['leader_chat'] if row else None,
        'round_ids': rounds, 'attempts': local_attempts}
    if next_action == '/coding-team listen':
        connection['next_action_app'] = 'antigravity'
        connection['next_action_chat'] = row['worker_chat'] if row else None
    if ctx.component == 'worker' and role != 'worker':
        connection.update(paired=False, waiter_ready=False,
            paired_elsewhere=bool(row and row['worker_chat']))
        view = {**view, 'paired': False, 'waiter_ready': False}
    state = ('READY' if readiness in ('MANUAL_READY', 'AUTO_READY') else
             'WAITING' if readiness in ('PAIRING', 'WAITING') else readiness)
    return {**view, 'ok': problem is None, 'state': state, 'reason': reason,
        'ready': state == 'READY', 'app': app, 'workspace': str(ctx.paths.root), 'role': role,
        'connections': [connection], 'attempts': local_attempts}


@_view_operation
def connection_status(ctx):
    try:
        return _connection_status(ctx, _snapshot(Store(ctx.paths)))
    except (ValueError, OSError, TimeoutError) as exc:
        reason = getattr(exc, 'reason', None) or str(exc)
        return _view(reason, ok=False, reason=reason,
            app=ctx.actor.platform if ctx.actor else ('antigravity' if ctx.component == 'worker' else 'codex'),
            workspace=str(ctx.paths.root), role='unknown', leader=None, connections=[], observed_at=time.time())


@_view_operation
def wait_ready(ctx, expected: binding.LeaderRef, *, pair_epoch: int, desktop_epoch: int,
               auto_epoch: int, wait_seconds: int):
    if type(wait_seconds) is not int or not 0 <= wait_seconds <= 60:
        raise ValueError('INVALID_WAIT_SECONDS')
    epochs = {'pair_epoch': pair_epoch, 'desktop_epoch': desktop_epoch, 'auto_epoch': auto_epoch}
    if (not isinstance(expected, binding.LeaderRef) or ctx.require_leader() != expected.actor
            or any(type(value) is not int or value < 0 for value in epochs.values())):
        raise ValueError('STALE_ACTIVATION')
    store = Store(ctx.paths)
    deadline = time.monotonic() + wait_seconds
    poll = False
    while True:
        snapshot = _snapshot(store, poll=poll)
        row = snapshot[0]
        view = _connection_status(ctx, snapshot)
        if (not row or (row['leader_platform'], row['leader_chat'], row['leader_manifest']) != (
                expected.actor.platform, expected.actor.chat_id,
                (ctx.source_root / binding.MANIFEST_NAME).as_posix()) or row['leader_epoch'] != expected.epoch
                or any(row[name] != value for name, value in epochs.items())):
            return _view('STALE_ACTIVATION', ok=False, reason='STALE_ACTIVATION', ready=False,
                app=expected.actor.platform, workspace=str(ctx.paths.root), connections=[],
                observed_at=snapshot[4])
        if view['state'] != 'WAITING' or time.monotonic() >= deadline:
            return view
        poll = True
        time.sleep(min(.25, max(0, deadline - time.monotonic())))


@_view_operation
def desktop_status(ctx):
    return _desktop_status(ctx)


@_view_operation
def worker_status(ctx):
    return _desktop_status(ctx, worker_only=True)
