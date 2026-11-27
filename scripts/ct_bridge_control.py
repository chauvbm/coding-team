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
import secrets
import uuid

import ct_bridge_binding as binding
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


@_view_operation
def set_desktop(ctx, expected, enabled):
    if type(enabled) is not bool:
        raise ValueError('INVALID_ENABLED')
    with _guard(ctx, terminal=not enabled, use_worker=False) as (_, db, leader, _):
        row = require_leader(db, ctx, expected)
        if enabled:
            _require_policy(leader, row)
        if bool(row['desktop_enabled']) != enabled:
            db.execute('UPDATE group_state SET desktop_enabled=?,desktop_epoch=desktop_epoch+1,invite_hash=NULL,invite_expires_at=NULL WHERE id=1', (int(enabled),))
            db.execute('DELETE FROM waiter')
        if not enabled:
            _revoke_pending(db)
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
        worker = ctx.own_binding()
        _register_worktree(leader, ctx.paths.root)
        _register_worktree(worker, ctx.paths.root)
        db.execute('''UPDATE group_state SET worker_platform=?,worker_chat=?,worker_manifest=?,
            invite_hash=NULL,invite_expires_at=NULL WHERE id=1''', (actor.platform, actor.chat_id, _manifest(worker)))
        db.execute('DELETE FROM waiter')
        return _view('CONNECTED', leader_epoch=row['leader_epoch'], pair_epoch=row['pair_epoch'])


def _desktop_status(ctx, *, worker_only=False):
    store = Store(ctx.paths)
    with store.read() as db:
        row = dict(_group(db)) if db is not None else None
        rounds = _busy(db) if db is not None else []
        attempts = _open_attempts(db) if db is not None else []
        waiter = dict(db.execute('SELECT * FROM waiter WHERE id=1').fetchone() or {}) if db is not None else {}
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
        and waiter['expires_at'] > store.clock() and waiter['policy_stamp'] == policy_stamp
        and all(waiter[name] == row[name] for name in ('leader_epoch', 'pair_epoch', 'auto_epoch', 'desktop_epoch')))
    reason = (problem or ('LEADER_NOT_SELECTED' if not selected else 'NATIVE_POLICY_OFF' if policy != 'ON'
        else 'DESKTOP_OFF' if not desktop else 'NOT_PAIRED' if not paired
        else 'WAITER_NOT_READY' if auto and not waiter_ready else None))
    return _view('READY' if reason is None else reason, reason=reason, ready=reason is None,
        selected=selected, leader={'platform': row['leader_platform'], 'chat_id': row['leader_chat']} if selected else None,
        leader_epoch=row['leader_epoch'] if row else 0, effective_policy=policy,
        desktop_enabled=desktop, auto_enabled=auto, paired=paired, waiter_ready=waiter_ready,
        round_ids=rounds, attempts=attempts if problem != 'WRONG_WORKER' else [],
        pair_epoch=row['pair_epoch'] if row else 0)


@_view_operation
def desktop_status(ctx):
    return _desktop_status(ctx)


@_view_operation
def worker_status(ctx):
    return _desktop_status(ctx, worker_only=True)
