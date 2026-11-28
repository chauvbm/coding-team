"""Worktree-local authority, worker registry, shared admission and evidence.

Install locks precede policy and SQLite. Model execution never holds these locks.
Read views do not initialize, migrate or repair storage.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from functools import wraps
import hashlib
import hmac
import json
from os.path import normcase
from pathlib import Path
import re
import secrets
import time
import uuid

import ct_bridge_binding as binding
from ct_bridge_commands import supports_pair
from ct_bridge_store import Store, inspect_store
import ct_policy
import ct_storage

RoundId = str
PENDING = ('PUBLISHING', 'WAITING_FOR_USER', 'QUEUED', 'CLAIMED')
TERMINAL = ('COMPLETED', 'FAILED', 'CANCELED', 'REVOKED')
PLATFORM = 'antigravity'
_STARTUP_VERSION = b'LISTENER_STARTUP_VERSION = 2'
_STARTUP_FIELDS = {'chat_id', 'worker_id', 'leader_epoch', 'desktop_epoch', 'auto_epoch',
                   'policy_digest', 'expires_at'}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


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


def _peer(db, platform=PLATFORM):
    return db.execute('SELECT * FROM peers WHERE platform=?', (platform,)).fetchone()


def _manifest(component):
    return Path(component.manifest_path).resolve().as_posix()


def matches_component(ctx, manifest_path):
    return manifest_path == (ctx.source_root / binding.MANIFEST_NAME).as_posix()


def context_leader(db):
    row = _group(db)
    if not row or not row['leader_chat']:
        return None
    return binding.LeaderRef(binding.Actor(row['leader_platform'], row['leader_chat']), row['leader_epoch'])


def require_leader(db, ctx, expected):
    actor = ctx.require_leader()
    row = _group(db)
    if (not isinstance(expected, binding.LeaderRef) or expected.actor != actor or not row
            or (row['leader_platform'], row['leader_chat'], row['leader_epoch'], row['leader_manifest']) !=
            (actor.platform, actor.chat_id, expected.epoch, _manifest(ctx.own_binding()))):
        raise ValueError('STALE_LEADER')
    return row


def worker_by_id(db, worker_id):
    row = db.execute('SELECT * FROM workers WHERE worker_id=?', (worker_id,)).fetchone()
    if row is None:
        raise ValueError('WRONG_WORKER')
    return row


def worker_for_actor(db, ctx):
    actor = ctx.require_actor()
    if ctx.component != 'worker' or actor.platform != PLATFORM:
        raise ValueError('WORKER_PLATFORM_UNSUPPORTED')
    group = _group(db)
    row = db.execute('''SELECT * FROM workers WHERE platform=? AND chat_id=?
        AND leader_epoch=? AND retired_at IS NULL''',
        (actor.platform, actor.chat_id, group['leader_epoch'])).fetchone()
    if row is None or not matches_component(ctx, row['manifest']):
        raise binding.BindingError('WRONG_WORKER')
    return row


def _register_worktree(component, root):
    if component is not None:
        from ct_bridge_install import register_worktree
        register_worktree(component.install_lock, root)


def _identity(row):
    return tuple(row[name] for name in ('leader_platform', 'leader_chat', 'leader_manifest', 'leader_epoch'))


@contextmanager
def operation_guard(ctx, *, worker_id=None, terminal=False):
    """Fence selected authority and worker placement after acquiring install locks."""
    ctx.require_actor()
    own = ctx.own_binding()
    store = Store(ctx.paths)
    with store.read() as db:
        observed = dict(_group(db)) if db is not None else None
        selected = None
        if observed and observed['leader_chat']:
            if ctx.component == 'worker':
                selected = dict(worker_for_actor(db, ctx))
                if worker_id is not None and selected['worker_id'] != worker_id:
                    raise binding.BindingError('WRONG_WORKER')
            elif worker_id is not None:
                selected = dict(worker_by_id(db, worker_id))
    if not observed or not observed['leader_manifest']:
        raise ValueError('LEADER_NOT_SELECTED')
    leader = own if matches_component(ctx, observed['leader_manifest']) else ctx.leader_binding(observed['leader_manifest'])
    worker = (own if ctx.component == 'worker' else
              ctx.worker_binding(selected['manifest']) if selected and not terminal else None)
    with binding.locked_bindings(*(part for part in (own, leader, worker) if part),
                                  require_clean_journal=not terminal):
        with ct_storage.locked(leader.policy_path), store.write() as db:
            if _identity(_group(db)) != _identity(observed):
                raise ValueError('BINDING_CHANGED')
            if selected:
                current = worker_by_id(db, selected['worker_id'])
                if dict(current) != selected:
                    raise ValueError('BINDING_CHANGED')
                if ctx.component == 'worker':
                    if worker_for_actor(db, ctx)['worker_id'] != selected['worker_id']:
                        raise binding.BindingError('WRONG_WORKER')
            yield store, db, leader, worker


@contextmanager
def _guard(ctx, *, terminal=False, use_worker=True, worker_id=None):
    # Retain the internal context-manager spelling while callers transition.
    with operation_guard(ctx, worker_id=worker_id, terminal=terminal) as guarded:
        yield guarded


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
    return [dict(row) for row in db.execute('''SELECT t.round_id,a.task_id,a.attempt_no,a.worker_id,
        a.mode,a.state,p.work_item_id,p.role_variant,
        CASE WHEN a.result_id IS NOT NULL AND a.ack_at IS NULL THEN 1 ELSE 0 END AS review_pending
        FROM attempts a JOIN tasks t ON t.id=a.task_id JOIN rounds r ON r.id=t.round_id
        LEFT JOIN participants p ON p.round_id=t.round_id AND p.participant_id=t.participant_id
        WHERE r.closed_at IS NULL ORDER BY a.created_at,a.task_id,a.attempt_no''')]


def _reset_authority(db, platform, chat, manifest, now=None):
    db.execute('''UPDATE group_state SET leader_platform=?,leader_chat=?,leader_manifest=?,
        leader_epoch=leader_epoch+1 WHERE id=1''', (platform, chat, manifest))
    db.execute('''UPDATE peers SET enabled=0,desktop_epoch=desktop_epoch+1,auto_enabled=0,
        auto_epoch=auto_epoch+1,join_id=NULL,join_expires_at=NULL,
        legacy_ticket_hash=NULL,legacy_expires_at=NULL''')
    db.execute('UPDATE workers SET retired_at=? WHERE retired_at IS NULL', (time.time() if now is None else now,))
    db.execute('DELETE FROM waiter')


def _discover_worker(ctx):
    return ctx.discover_worker()


def _prepare_store(ctx, clock, leader, worker):
    from ct_bridge_upgrade import migrate_v1
    state = inspect_store(ctx.paths)
    if state.maintenance:
        raise ValueError('RECOVERY_IN_PROGRESS')
    if state.version == 1:
        if worker is None:
            raise ValueError('INSTALLATION_INCOMPATIBLE')
        migrate_v1(ctx.paths, clock=clock, leader_manifest=_manifest(leader), worker_manifest=_manifest(worker))


@_view_operation
def leader_on(ctx):
    actor = ctx.require_leader()
    own = ctx.own_binding()
    store = Store(ctx.paths)
    worker = _discover_worker(ctx)
    with binding.locked_bindings(*(part for part in (own, worker) if part)), ct_storage.locked(own.policy_path):
        if ct_policy.policy_stamp(own.policy_path, actor.chat_id, require_epochs=True).effective != 'ON':
            raise ValueError('NATIVE_POLICY_OFF')
        _prepare_store(ctx, store.clock, own, worker)
        with store.write() as db:
            row = _group(db)
            if (row['leader_platform'], row['leader_chat'], row['leader_manifest']) == (
                    actor.platform, actor.chat_id, _manifest(own)):
                return _view('LEADER_SELECTED', leader_epoch=row['leader_epoch'])
            busy = _busy_view(db)
            if busy:
                return busy
            _reset_authority(db, actor.platform, actor.chat_id, _manifest(own), store.clock())
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
            raise ValueError('NOT_LEADER')
        busy = _busy_view(db)
        if busy:
            return busy
        _reset_authority(db, None, None, None, store.clock())
        return _view('LEADER_RELEASED', leader_epoch=_group(db)['leader_epoch'])


@_view_operation
def leader_status(ctx):
    with Store(ctx.paths).read() as db:
        row = _group(db) if db is not None else None
        selected = bool(row and row['leader_chat'])
        return _view('LEADER_SELECTED' if selected else 'LEADER_NOT_SELECTED', selected=selected,
            leader={'platform': row['leader_platform'], 'chat_id': row['leader_chat']} if selected else None,
            leader_epoch=row['leader_epoch'] if row else 0, round_ids=_busy(db) if db is not None else [],
            attempts=_open_attempts(db) if db is not None else [])


def open_round(ctx, expected):
    with operation_guard(ctx) as (store, db, leader, _):
        row = require_leader(db, ctx, expected)
        _require_policy(leader, row)
        _register_worktree(leader, ctx.paths.root)
        rid = 'R-' + uuid.uuid4().hex
        db.execute('''INSERT INTO rounds(id,leader_epoch,leader_platform,leader_chat,leader_manifest,opened_at)
            VALUES(?,?,?,?,?,?)''', (rid, expected.epoch, expected.actor.platform, expected.actor.chat_id,
                                     row['leader_manifest'], store.clock()))
        return rid


def round_for_leader(db, expected, round_id, *, allow_closed=False):
    row = db.execute('SELECT * FROM rounds WHERE id=?', (round_id,)).fetchone()
    if (row is None or (row['leader_epoch'], row['leader_platform'], row['leader_chat']) !=
            (expected.epoch, expected.actor.platform, expected.actor.chat_id)):
        raise ValueError('ROUND_MISMATCH')
    if not allow_closed and row['closed_at'] is not None:
        raise ValueError('ROUND_CLOSED')
    return row


_round = round_for_leader


def _evidence(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('EVIDENCE_REQUIRED')
    return value


def _participant_id(value):
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 256:
        raise ValueError('INVALID_PARTICIPANT')
    return value


def _participant(db, round_id, participant_id):
    row = db.execute('SELECT * FROM participants WHERE round_id=? AND participant_id=?',
                     (round_id, participant_id)).fetchone()
    if row is None:
        raise ValueError('PARTICIPANT_NOT_FOUND')
    return row


def _unresolved(db, round_id, participant_id=None):
    return db.execute('''SELECT a.task_id,a.attempt_no,a.state FROM attempts a JOIN tasks t ON t.id=a.task_id
        WHERE t.round_id=? AND (? IS NULL OR t.participant_id=?)
        AND (a.state NOT IN ('COMPLETED','FAILED','CANCELED','REVOKED')
        OR (a.result_id IS NOT NULL AND a.ack_at IS NULL))''',
        (round_id, participant_id, participant_id)).fetchall()


@_view_operation
def close_round(ctx, expected, round_id, evidence):
    evidence = _evidence(evidence)
    with operation_guard(ctx, terminal=True) as (store, db, _, _):
        require_leader(db, ctx, expected)
        row = round_for_leader(db, expected, round_id, allow_closed=True)
        if row['closed_at'] is not None:
            if row['evidence'] != evidence:
                raise ValueError('EVIDENCE_CONFLICT')
            return _view('ROUND_CLOSED', round_id=round_id)
        participants = db.execute('SELECT participant_id FROM participants WHERE round_id=? AND finished_at IS NULL', (round_id,)).fetchall()
        items = db.execute("SELECT work_item_id FROM work_items WHERE round_id=? AND state='active'", (round_id,)).fetchall()
        scopes = db.execute('SELECT 1 FROM scope_reservations WHERE round_id=? LIMIT 1', (round_id,)).fetchone()
        attempts = _unresolved(db, round_id)
        if participants or items or scopes or attempts:
            return _view('ROUND_BUSY', ok=False, reason='ROUND_BUSY', round_id=round_id,
                native_participants=[row['participant_id'] for row in participants],
                active_work_items=[row['work_item_id'] for row in items], attempts=[dict(row) for row in attempts])
        db.execute('UPDATE rounds SET closed_at=?,evidence=? WHERE id=?', (store.clock(), evidence, round_id))
        return _view('ROUND_CLOSED', round_id=round_id)


def _assignment(ctx, assignment):
    from ct_bridge_roles import RoleAssignment, assignment_to_dict, load_contract, validate_assignment
    raw = assignment_to_dict(assignment, include_derived=False) if isinstance(assignment, RoleAssignment) else assignment
    return validate_assignment(load_contract(ctx.source_root), raw, verified_model=None)


def _assignment_json(assignment):
    from ct_bridge_roles import canonical_assignment_json
    return canonical_assignment_json(assignment)


def _scope_tools():
    from ct_bridge_scopes import ScopeHolder, canonical_scopes, reserve, release, verify_baseline
    return ScopeHolder, canonical_scopes, reserve, release, verify_baseline


def _exclusive(db):
    return db.execute("SELECT 1 FROM participants WHERE executor='native' AND assignment IS NULL "
                      "AND observer=0 AND finished_at IS NULL LIMIT 1").fetchone()


def _snapshot_matches(paths, snapshot):
    from ct_bridge_scopes import ScopeSet, verify_baseline

    if (not isinstance(snapshot, dict) or set(snapshot) != {'head', 'inputs'}
            or not isinstance(snapshot['inputs'], dict)):
        raise ValueError('INVALID_SNAPSHOT')
    verify_baseline(paths, snapshot=snapshot, scope=ScopeSet(tuple(snapshot['inputs']), (), ()), baseline={})


def _dependencies(db, paths, round_id, assignment):
    for item_id, accepted in assignment.depends_on.items():
        row = db.execute('SELECT * FROM work_items WHERE round_id=? AND work_item_id=?', (round_id, item_id)).fetchone()
        if row is None or row['state'] != 'accepted' or row['acceptance_id'] != accepted:
            raise ValueError('DEPENDENCY_NOT_READY')
        snapshot = json.loads(row['snapshot'])
        _snapshot_matches(paths, snapshot)
        if any(assignment.snapshot['inputs'].get(path) != digest for path, digest in snapshot['inputs'].items()):
            raise ValueError('DEPENDENCY_SNAPSHOT_MISMATCH')


def admit_work_item(db, paths, round_id, participant_id, assignment, *,
                    replaces_participant_id=None, transfer_evidence=None):
    """Common Desktop/native/Leader ownership fence; caller owns the transaction."""
    from ct_bridge_roles import RoleAssignment
    if not isinstance(assignment, RoleAssignment):
        raise ValueError('INVALID_ASSIGNMENT')
    participant = _participant(db, round_id, participant_id)
    if participant['work_item_id'] != assignment.work_item_id or participant['role_variant'] != assignment.variant:
        raise ValueError('PARTICIPANT_ASSIGNMENT_MISMATCH')
    if _exclusive(db):
        raise ValueError('EXCLUSIVE_PARTICIPANT_ACTIVE')
    round_row = db.execute('SELECT * FROM rounds WHERE id=?', (round_id,)).fetchone()
    if not round_row or round_row['closed_at'] is not None or round_row['leader_epoch'] != _group(db)['leader_epoch']:
        raise ValueError('ROUND_NOT_OPEN')
    _dependencies(db, paths, round_id, assignment)
    row = db.execute('SELECT * FROM work_items WHERE round_id=? AND work_item_id=?', (round_id, assignment.work_item_id)).fetchone()
    if row is not None:
        if row['state'] != 'active':
            raise ValueError('WORK_ITEM_CLOSED')
        if row['depends_on'] != _json(dict(assignment.depends_on)):
            raise ValueError('WORK_ITEM_CONFLICT')
        if row['owner_participant_id'] != participant_id:
            if replaces_participant_id != row['owner_participant_id']:
                raise ValueError('WORK_ITEM_OWNED')
            _evidence(transfer_evidence)
            previous = _participant(db, round_id, replaces_participant_id)
            held = db.execute('SELECT 1 FROM scope_reservations WHERE round_id=? AND participant_id=? LIMIT 1',
                              (round_id, replaces_participant_id)).fetchone()
            if previous['finished_at'] is None or _unresolved(db, round_id, replaces_participant_id) or held:
                raise ValueError('TRANSFER_NOT_SAFE')
            db.execute('UPDATE work_items SET owner_participant_id=? WHERE round_id=? AND work_item_id=?',
                       (participant_id, round_id, assignment.work_item_id))
        return
    if replaces_participant_id is not None:
        raise ValueError('WORK_ITEM_NOT_FOUND')
    db.execute('''INSERT INTO work_items(round_id,work_item_id,owner_participant_id,depends_on,state)
        VALUES(?,?,?,?,'active')''', (round_id, assignment.work_item_id, participant_id, _json(dict(assignment.depends_on))))


def _reserve_participant(ctx, expected, round_id, participant_id, assignment, executor, *,
                         observer=False, replaces_participant_id=None, transfer_evidence=None):
    participant_id = _participant_id(participant_id)
    assignment = _assignment(ctx, assignment) if assignment is not None else None
    if observer and assignment is not None:
        raise ValueError('OBSERVER_CANNOT_EXECUTE')
    with operation_guard(ctx) as (store, db, leader, _):
        row = require_leader(db, ctx, expected)
        _require_policy(leader, row)
        round_for_leader(db, expected, round_id)
        old = db.execute('SELECT * FROM participants WHERE round_id=? AND participant_id=?', (round_id, participant_id)).fetchone()
        serialized = _assignment_json(assignment) if assignment is not None else None
        if old:
            if (old['executor'] != executor or old['assignment'] != serialized or bool(old['observer']) != observer
                    or old['replaces_participant_id'] != replaces_participant_id):
                raise ValueError('PARTICIPANT_CONFLICT')
            return _view('NATIVE_REGISTERED' if executor == 'native' else 'LEADER_RESERVED', participant_id=participant_id)
        if assignment is None and not observer:
            if db.execute('SELECT 1 FROM participants WHERE observer=0 AND finished_at IS NULL LIMIT 1').fetchone() or db.execute('SELECT 1 FROM scope_reservations LIMIT 1').fetchone():
                raise ValueError('EXCLUSIVE_SCOPE_REQUIRED')
        if _exclusive(db) and not observer:
            raise ValueError('EXCLUSIVE_PARTICIPANT_ACTIVE')
        handle = ctx.require_actor().chat_id if executor == 'leader' else None
        db.execute('''INSERT INTO participants(round_id,participant_id,work_item_id,executor,handle,
            role_variant,assignment,started_at,replaces_participant_id,observer) VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (round_id, participant_id, assignment.work_item_id if assignment else None,
             executor, handle, assignment.variant if assignment else None, serialized,
             store.clock() if executor == 'leader' else None, replaces_participant_id, int(observer)))
        if assignment is not None:
            admit_work_item(db, ctx.paths, round_id, participant_id, assignment,
                replaces_participant_id=replaces_participant_id, transfer_evidence=transfer_evidence)
            Holder, canonical, reserve, _, verify = _scope_tools()
            scopes = canonical(ctx.paths, assignment)
            if not set(assignment.write_paths) <= assignment.snapshot['inputs'].keys():
                raise ValueError('BASELINE_REQUIRED')
            verify(ctx.paths, snapshot=assignment.snapshot, scope=scopes,
                   baseline={path: assignment.snapshot['inputs'][path] for path in assignment.write_paths})
            reserve(db, Holder(round_id, participant_id), scopes)
        return _view('NATIVE_REGISTERED' if executor == 'native' else 'LEADER_RESERVED', participant_id=participant_id)


@_view_operation
def register_native(ctx, expected, round_id, participant_id, assignment=None, *, observer=False,
                    replaces_participant_id=None, transfer_evidence=None):
    return _reserve_participant(ctx, expected, round_id, participant_id, assignment, 'native', observer=observer,
        replaces_participant_id=replaces_participant_id, transfer_evidence=transfer_evidence)


@_view_operation
def reserve_leader(ctx, expected, round_id, participant_id, assignment, *,
                   replaces_participant_id=None, transfer_evidence=None):
    return _reserve_participant(ctx, expected, round_id, participant_id, assignment, 'leader',
        replaces_participant_id=replaces_participant_id, transfer_evidence=transfer_evidence)


@_view_operation
def bind_native(ctx, expected, round_id, participant_id, handle):
    handle = _participant_id(handle)
    with operation_guard(ctx, terminal=True) as (store, db, _, _):
        require_leader(db, ctx, expected)
        round_for_leader(db, expected, round_id)
        row = _participant(db, round_id, participant_id)
        if row['executor'] != 'native' or row['finished_at'] is not None or row['handle'] not in (None, handle):
            raise ValueError('NATIVE_PARTICIPANT_CONFLICT')
        db.execute('UPDATE participants SET handle=?,started_at=COALESCE(started_at,?) WHERE round_id=? AND participant_id=?',
                   (handle, store.clock(), round_id, participant_id))
        return _view('NATIVE_BOUND', participant_id=participant_id)


def _finish_participant(ctx, expected, round_id, participant_id, evidence, executor):
    evidence = _evidence(evidence)
    with operation_guard(ctx, terminal=True) as (store, db, _, _):
        require_leader(db, ctx, expected)
        round_for_leader(db, expected, round_id)
        row = _participant(db, round_id, participant_id)
        if row['executor'] != executor:
            raise ValueError('PARTICIPANT_MISMATCH')
        if row['finished_at'] is not None and row['evidence'] != evidence:
            raise ValueError('EVIDENCE_CONFLICT')
        db.execute('UPDATE participants SET finished_at=COALESCE(finished_at,?),evidence=? WHERE round_id=? AND participant_id=?',
                   (store.clock(), evidence, round_id, participant_id))
        Holder, _, _, release, _ = _scope_tools()
        release(db, Holder(round_id, participant_id))
        return _view('NATIVE_FINISHED' if executor == 'native' else 'LEADER_FINISHED', participant_id=participant_id)


@_view_operation
def finish_native(ctx, expected, round_id, participant_id, evidence):
    return _finish_participant(ctx, expected, round_id, participant_id, evidence, 'native')


@_view_operation
def finish_leader(ctx, expected, round_id, participant_id, evidence):
    return _finish_participant(ctx, expected, round_id, participant_id, evidence, 'leader')


@_view_operation
def complete_work_item(ctx, expected, round_id, work_item_id, *, outcome, snapshot, evidence):
    evidence = _evidence(evidence)
    if outcome not in ('accepted', 'cancelled'):
        raise ValueError('INVALID_OUTCOME')
    with operation_guard(ctx, terminal=True) as (store, db, _, _):
        require_leader(db, ctx, expected)
        round_for_leader(db, expected, round_id)
        row = db.execute('SELECT * FROM work_items WHERE round_id=? AND work_item_id=?', (round_id, work_item_id)).fetchone()
        if row is None:
            raise ValueError('WORK_ITEM_NOT_FOUND')
        encoded = _json(snapshot)
        if row['state'] != 'active':
            if (row['state'], row['snapshot'], row['evidence']) != (outcome, encoded, evidence):
                raise ValueError('EVIDENCE_CONFLICT')
            return _view('WORK_ITEM_' + outcome.upper(), work_item_id=work_item_id, acceptance_id=row['acceptance_id'])
        participant = _participant(db, round_id, row['owner_participant_id'])
        held = db.execute('SELECT 1 FROM scope_reservations WHERE round_id=? AND participant_id=? LIMIT 1',
                          (round_id, participant['participant_id'])).fetchone()
        if _unresolved(db, round_id, participant['participant_id']) or held or participant['finished_at'] is None:
            raise ValueError('WORK_ITEM_BUSY')
        if outcome == 'accepted':
            _snapshot_matches(ctx.paths, snapshot)
            latest = db.execute('''SELECT a.assignment,a.state FROM attempts a JOIN tasks t ON t.id=a.task_id
                WHERE t.round_id=? AND t.participant_id=? ORDER BY a.attempt_no DESC LIMIT 1''',
                (round_id, participant['participant_id'])).fetchone()
            if latest and latest['state'] != 'COMPLETED':
                raise ValueError('WORK_ITEM_NOT_COMPLETED')
            assignment = json.loads(latest['assignment'] if latest else participant['assignment'])
            if not set(assignment['write_paths']) <= snapshot['inputs'].keys():
                raise ValueError('OUTPUT_SNAPSHOT_REQUIRED')
        acceptance = 'AC-' + uuid.uuid4().hex if outcome == 'accepted' else None
        db.execute('UPDATE work_items SET state=?,acceptance_id=?,snapshot=?,evidence=? WHERE round_id=? AND work_item_id=?',
                   (outcome, acceptance, encoded, evidence, round_id, work_item_id))
        return _view('WORK_ITEM_' + outcome.upper(), work_item_id=work_item_id, acceptance_id=acceptance)


def _actor_id(db, participant):
    if participant['executor'] == 'desktop':
        worker = worker_by_id(db, participant['worker_id'])
        identity = ('desktop', worker['platform'], worker['chat_id'])
    elif participant['executor'] == 'leader':
        row = db.execute('SELECT leader_platform,leader_chat FROM rounds WHERE id=?', (participant['round_id'],)).fetchone()
        identity = ('desktop', row['leader_platform'], row['leader_chat'])
    elif participant['handle']:
        identity = ('native', participant['handle'])
    else:
        raise ValueError('NATIVE_HANDLE_REQUIRED')
    return 'actor-' + hashlib.sha256(_json(identity).encode()).hexdigest()


@_view_operation
def ledger_evidence(ctx, expected, round_id, participant_id, *, review_scope, snapshot):
    from ct_bridge_roles import assignment_to_dict, load_contract, validate_assignment
    ctx.require_leader()
    own = ctx.own_binding()
    own.verify()
    with Store(ctx.paths).read() as db:
        if db is None:
            raise ValueError('LEADER_NOT_SELECTED')
        require_leader(db, ctx, expected)
        round_for_leader(db, expected, round_id, allow_closed=True)
        participant = _participant(db, round_id, participant_id)
        if not participant['assignment'] or not participant['work_item_id']:
            raise ValueError('NO_EXECUTOR_EVIDENCE')
        latest = db.execute('''SELECT a.assignment,a.state,a.result_id,a.result_sha256,a.task_id,a.attempt_no
            FROM attempts a JOIN tasks t ON t.id=a.task_id WHERE t.round_id=? AND t.participant_id=?
            ORDER BY a.attempt_no DESC LIMIT 1''', (round_id, participant_id)).fetchone()
        assignment = validate_assignment(load_contract(ctx.source_root),
            json.loads(latest['assignment'] if latest else participant['assignment']), verified_model=None)
        writers = set()
        for previous in db.execute('''SELECT * FROM participants WHERE round_id=? AND assignment IS NOT NULL
                                   AND started_at IS NOT NULL''', (round_id,)):
            metadata = json.loads(previous['assignment'])
            if metadata.get('write_paths'):
                writers.add(_actor_id(db, previous))
        actor_id = _actor_id(db, participant)
        rendered = assignment_to_dict(assignment, include_derived=False)
        evidence = participant['evidence']
        if latest and latest['state'] == 'COMPLETED' and latest['result_id']:
            from ct_bridge_files import AttemptKey, read_file
            read_file(ctx.paths, AttemptKey(latest['task_id'], latest['attempt_no']), 'result', latest['result_sha256'])
            evidence = latest['result_id']
        if review_scope is not None:
            if (assignment.variant not in ('ct-architect', 'ct-reviewer', 'ct-reviewer-verify', 'ct-security')
                    or not isinstance(review_scope, list) or rendered.get('review_scope') != review_scope
                    or rendered['snapshot'] != snapshot or participant['finished_at'] is None
                    or not evidence or _unresolved(db, round_id, participant_id)):
                raise ValueError('INVALID_REVIEW_EVIDENCE')
            _snapshot_matches(ctx.paths, snapshot)
        receipts = [dict(row) for row in db.execute('''SELECT a.task_id,a.attempt_no,a.result_id,a.result_sha256
            FROM attempts a JOIN tasks t ON t.id=a.task_id WHERE t.round_id=? AND t.participant_id=? AND a.result_id IS NOT NULL''',
            (round_id, participant_id))]
        return _view('LEDGER_EVIDENCE', writer_ids=sorted(writers), actor_id=actor_id,
            work_item_id=participant['work_item_id'], role_variant=assignment.variant,
            review_scope=list(assignment.review_scope) if assignment.review_scope is not None else None,
            snapshot=rendered['snapshot'], evidence=evidence, receipts=receipts)


def _revoke_pending(db, *, auto_only=False, platform=PLATFORM):
    rows = db.execute('''SELECT a.task_id,a.attempt_no,t.round_id,t.participant_id FROM attempts a
        JOIN tasks t ON t.id=a.task_id JOIN workers w ON w.worker_id=a.worker_id
        WHERE w.platform=? AND a.state IN ('PUBLISHING','WAITING_FOR_USER','QUEUED','CLAIMED')'''
        + (" AND a.mode='auto'" if auto_only else ''), (platform,)).fetchall()
    if rows:
        Holder, _, _, release, _ = _scope_tools()
        from ct_bridge_files import AttemptKey
        now = time.time()
        for row in rows:
            db.execute("UPDATE attempts SET state='REVOKED',claim_hash=NULL,claim_expires_at=NULL,finished_at=?,reason=? WHERE task_id=? AND attempt_no=?",
                       (now, 'AUTO_OFF' if auto_only else 'DESKTOP_OFF', row['task_id'], row['attempt_no']))
            db.execute('UPDATE participants SET finished_at=? WHERE round_id=? AND participant_id=?',
                       (now, row['round_id'], row['participant_id']))
            release(db, Holder(row['round_id'], row['participant_id'], AttemptKey(row['task_id'], row['attempt_no'])))
    db.execute('DELETE FROM waiter WHERE worker_id IN (SELECT worker_id FROM workers WHERE platform=?)', (platform,))


def _new_invite(store, db, platform=PLATFORM):
    db.execute('UPDATE peers SET join_id=?,join_expires_at=? WHERE platform=?',
               (uuid.uuid4().hex, store.clock() + 600.0, platform))


def _listener_digest(ctx, group, peer, leader, worker, stamp):
    snapshot = _json([normcase(str(ctx.paths.root)), group['leader_chat'], group['leader_epoch'],
                      peer['desktop_epoch'], peer['auto_epoch'], stamp,
                      leader._manifest_digest, worker._manifest_digest])
    return hashlib.sha256(snapshot.encode('utf-8')).hexdigest()


def require_listener_startup(ctx, group, peer, leader, worker, stamp, startup, now, worker_id,
                             *, joined_worker_id=None):
    """Check the delivered startup against the receiver's current transaction."""
    if (not isinstance(startup, dict) or startup.keys() != _STARTUP_FIELDS
            or not isinstance(startup['chat_id'], str)
            or (startup['worker_id'] is not None and not isinstance(startup['worker_id'], str))
            or any(type(startup[key]) is not int for key in ('leader_epoch', 'desktop_epoch', 'auto_epoch'))
            or not isinstance(startup['policy_digest'], str)
            or re.fullmatch(r'[a-f0-9]{64}', startup['policy_digest']) is None
            or type(startup['expires_at']) not in (int, float) or not 0 <= startup['expires_at'] <= now + 120):
        raise ValueError('INVALID_LISTENER_STARTUP')
    if startup['expires_at'] <= now:
        raise ValueError('LISTENER_REQUEST_EXPIRED')
    if ctx.require_actor().chat_id != startup['chat_id']:
        raise ValueError('LISTENER_TARGET_CHANGED')
    if (not peer or not peer['enabled'] or not peer['auto_enabled'] or stamp is None
            or (group['leader_epoch'], peer['desktop_epoch'], peer['auto_epoch']) !=
            (startup['leader_epoch'], startup['desktop_epoch'], startup['auto_epoch'])
            or not hmac.compare_digest(startup['policy_digest'], _listener_digest(ctx, group, peer, leader, worker, stamp))):
        raise ValueError('STALE_ACTIVATION')
    if joined_worker_id is None:
        if worker_id != startup['worker_id']:
            raise ValueError('LISTENER_TARGET_CHANGED')
    elif joined_worker_id != worker_id or startup['worker_id'] not in (None, worker_id):
        raise ValueError('LISTENER_TARGET_CHANGED')


def listener_start_fence(ctx, epochs, chat_id, worker_id, *, policy_digest=None):
    """Authorize one exact target, with invitation renewal allowed only at preparation."""
    expected = binding.LeaderRef(ctx.require_leader(), epochs['leader_epoch'])
    with operation_guard(ctx, worker_id=worker_id) as (store, db, leader, worker_binding):
        group = require_leader(db, ctx, expected)
        peer = _peer(db)
        if not peer or (peer['desktop_epoch'], peer['auto_epoch']) != (epochs['desktop_epoch'], epochs['auto_epoch']):
            raise ValueError('STALE_ACTIVATION')
        stamp = _require_policy(leader, group)
        if not peer['enabled'] or not peer['auto_enabled']:
            raise ValueError('DESKTOP_OFF' if not peer['enabled'] else 'AUTO_OFF')
        worker = db.execute('''SELECT * FROM workers WHERE platform=? AND chat_id=?
            AND leader_epoch=? AND retired_at IS NULL''', (PLATFORM, chat_id, expected.epoch)).fetchone()
        if (worker['worker_id'] if worker else None) != worker_id:
            raise ValueError('LISTENER_TARGET_CHANGED')
        installed_worker = worker_binding if worker else _discover_worker(ctx)
        if installed_worker is None:
            raise ValueError('WORKER_INSTALLATION_MISSING')
        worker_entry = installed_worker.skill_dir / 'scripts/ct_bridge_worker.py'
        if _STARTUP_VERSION not in worker_entry.read_bytes().splitlines():
            raise ValueError('WORKER_STARTUP_UNSUPPORTED')
        serialized = _json(asdict(stamp))
        digest = _listener_digest(ctx, group, peer, leader, installed_worker, serialized)
        if policy_digest is not None and digest != policy_digest:
            raise ValueError('STALE_ACTIVATION')

        now = store.clock()
        if worker:
            if _worker_occupied(db, worker_id):
                raise ValueError('LISTENER_TARGET_BUSY')
            if worker_ready(db, worker, 'auto', serialized, now):
                raise ValueError('LISTENER_ALREADY_READY')
        else:
            expires = peer['join_expires_at'] if peer['join_id'] else None
            if policy_digest is None and (expires is None or expires <= now + 150):
                _new_invite(store, db)
            elif expires is None or expires <= now:
                raise ValueError('LISTENER_INVITE_EXPIRED')
        return digest


def _activate(store, db, platform=PLATFORM):
    row = _peer(db, platform)
    if row is None:
        db.execute('INSERT INTO peers(platform) VALUES(?)', (platform,))
        row = _peer(db, platform)
    if not row['enabled']:
        busy = _busy_view(db)
        if busy:
            return busy
        db.execute('''UPDATE peers SET enabled=1,desktop_epoch=desktop_epoch+1,auto_enabled=0,
            auto_epoch=auto_epoch+CASE WHEN auto_enabled=1 THEN 1 ELSE 0 END,
            legacy_ticket_hash=NULL,legacy_expires_at=NULL WHERE platform=?''', (platform,))
        db.execute('UPDATE workers SET retired_at=? WHERE platform=? AND retired_at IS NULL', (store.clock(), platform))
        _revoke_pending(db, platform=platform)
        _new_invite(store, db, platform)
    return None


def _disable(db, platform=PLATFORM):
    db.execute('''UPDATE peers SET desktop_epoch=desktop_epoch+CASE WHEN enabled=1 THEN 1 ELSE 0 END,
        enabled=0,join_id=NULL,join_expires_at=NULL,legacy_ticket_hash=NULL,legacy_expires_at=NULL WHERE platform=?''', (platform,))
    _revoke_pending(db, platform=platform)


def _configuration(ctx, db, state='CONFIGURED', platform=PLATFORM, **fields):
    group, peer = _group(db), _peer(db, platform)
    return _view(state, workspace=str(ctx.paths.root), leader_epoch=group['leader_epoch'], platform=platform,
        desktop_enabled=bool(peer and peer['enabled']), auto_enabled=bool(peer and peer['auto_enabled']),
        desktop_epoch=peer['desktop_epoch'] if peer else 0, auto_epoch=peer['auto_epoch'] if peer else 0,
        join_id=peer['join_id'] if peer else None, invite_expires_at=peer['join_expires_at'] if peer else None, **fields)


@_view_operation
def configure_peer(ctx, target, action):
    actor = ctx.require_leader()
    if not supports_pair(actor.platform, target):
        raise ValueError('UNSUPPORTED_PAIR')
    if action not in ('on', 'off', 'auto_on', 'auto_off'):
        raise ValueError('INVALID_ACTION')
    own, store = ctx.own_binding(), Store(ctx.paths)
    if action == 'off':
        with store.read() as db:
            if db is None:
                raise ValueError('NOT_LEADER')
    worker = None if action == 'off' else _discover_worker(ctx)
    with binding.locked_bindings(*(part for part in (own, worker) if part), require_clean_journal=action != 'off'):
        with ct_storage.locked(own.policy_path):
            if action != 'off':
                if ct_policy.policy_stamp(own.policy_path, actor.chat_id, require_epochs=True).effective != 'ON':
                    raise ValueError('NATIVE_POLICY_OFF')
                _prepare_store(ctx, store.clock, own, worker)
            with store.write() as db:
                group = _group(db)
                owner = (group['leader_platform'], group['leader_chat'], group['leader_manifest']) == (actor.platform, actor.chat_id, _manifest(own))
                if action == 'off':
                    if not owner:
                        raise ValueError('NOT_LEADER')
                    _disable(db, target)
                    return _configuration(ctx, db, 'DESKTOP_OFF', target)
                if not owner:
                    busy = _busy_view(db)
                    if busy:
                        return _configuration(ctx, db, platform=target, **busy)
                    _reset_authority(db, actor.platform, actor.chat_id, _manifest(own), store.clock())
                busy = _activate(store, db, target)
                if busy:
                    return _configuration(ctx, db, platform=target, **busy)
                peer = _peer(db, target)
                mode = bool(peer['auto_enabled']) if action == 'on' else action == 'auto_on'
                if bool(peer['auto_enabled']) != mode:
                    db.execute('UPDATE peers SET auto_enabled=?,auto_epoch=auto_epoch+1 WHERE platform=?', (int(mode), target))
                    db.execute('DELETE FROM waiter WHERE worker_id IN (SELECT worker_id FROM workers WHERE platform=?)', (target,))
                if not mode:
                    _revoke_pending(db, auto_only=True, platform=target)
                # Explicit auto_off on an already enabled peer changes only mode.
                peer = _peer(db, target)
                if action != 'auto_off' and (peer['join_id'] is None or peer['join_expires_at'] <= store.clock()):
                    _new_invite(store, db, target)
                _register_worktree(own, ctx.paths.root)
                _register_worktree(worker, ctx.paths.root)
                return _configuration(ctx, db, platform=target)


@_view_operation
def set_desktop(ctx, expected, enabled):
    if type(enabled) is not bool:
        raise ValueError('INVALID_ENABLED')
    with operation_guard(ctx, terminal=not enabled) as (store, db, leader, _):
        row = require_leader(db, ctx, expected)
        if enabled:
            _require_policy(leader, row)
            busy = _activate(store, db)
            if busy:
                return busy
        else:
            _disable(db)
        return _configuration(ctx, db, 'DESKTOP_ON' if enabled else 'DESKTOP_OFF')


@_view_operation
def set_auto(ctx, expected, enabled):
    if type(enabled) is not bool:
        raise ValueError('INVALID_ENABLED')
    with operation_guard(ctx, terminal=not enabled) as (_, db, leader, _):
        row = require_leader(db, ctx, expected)
        peer = _peer(db)
        if enabled:
            _require_policy(leader, row)
            if not peer or not peer['enabled']:
                raise ValueError('DESKTOP_OFF')
        if peer and bool(peer['auto_enabled']) != enabled:
            db.execute('UPDATE peers SET auto_enabled=?,auto_epoch=auto_epoch+1 WHERE platform=?', (int(enabled), PLATFORM))
            db.execute('DELETE FROM waiter')
        if not enabled:
            _revoke_pending(db, auto_only=True)
        return _configuration(ctx, db, 'AUTO_ON' if enabled else 'AUTO_OFF')


def _ticket_hash(ticket):
    if not isinstance(ticket, str) or not ticket or len(ticket) > 512:
        raise ValueError('INVALID_TICKET')
    return hashlib.sha256(ticket.encode()).hexdigest()


@_view_operation
def invite(ctx, expected):
    with operation_guard(ctx) as (store, db, leader, _):
        row = require_leader(db, ctx, expected)
        _require_policy(leader, row)
        busy = _busy_view(db)
        if busy:
            return busy
        db.execute('INSERT OR IGNORE INTO peers(platform) VALUES (?)', (PLATFORM,))
        ticket, expires = secrets.token_urlsafe(32), store.clock() + 600.0
        db.execute('UPDATE peers SET legacy_ticket_hash=?,legacy_expires_at=? WHERE platform=?', (_ticket_hash(ticket), expires, PLATFORM))
        return _view('INVITED', ticket=ticket, expires_at=expires, workspace=str(ctx.paths.root), leader_epoch=row['leader_epoch'])


def _register_worker(ctx, db, leader, store):
    actor = ctx.require_actor()
    own = ctx.own_binding()
    group = _group(db)
    current = db.execute('SELECT * FROM workers WHERE platform=? AND chat_id=? AND leader_epoch=? AND retired_at IS NULL',
                         (actor.platform, actor.chat_id, group['leader_epoch'])).fetchone()
    if current:
        if current['manifest'] != _manifest(own):
            raise ValueError('BINDING_CHANGED')
        return current['worker_id']
    worker_id = uuid.uuid4().hex
    _register_worktree(leader, ctx.paths.root)
    _register_worktree(own, ctx.paths.root)
    db.execute('''INSERT INTO workers(worker_id,platform,chat_id,manifest,leader_epoch,registered_at)
        VALUES(?,?,?,?,?,?)''', (worker_id, actor.platform, actor.chat_id, _manifest(own), group['leader_epoch'], store.clock()))
    return worker_id


def _join(ctx, ticket=None, *, startup=None):
    actor = ctx.require_actor()
    if ctx.component != 'worker' or actor.platform != PLATFORM:
        raise ValueError('WORKER_PLATFORM_UNSUPPORTED')
    own, store = ctx.own_binding(), Store(ctx.paths)
    with store.read() as db:
        observed = dict(_group(db)) if db is not None else None
        invite = dict(_peer(db, actor.platform) or {}) if db is not None else {}
    if not observed or not observed['leader_manifest']:
        raise ValueError('LEADER_NOT_SELECTED')
    leader = ctx.leader_binding(observed['leader_manifest'])
    with binding.locked_bindings(leader, own):
        with ct_storage.locked(leader.policy_path), store.write() as db:
            group, peer = _group(db), _peer(db, actor.platform)
            if _identity(group) != _identity(observed):
                raise ValueError('STALE_INVITE')
            if not supports_pair(group['leader_platform'], actor.platform):
                raise ValueError('UNSUPPORTED_PAIR')
            stamp = _require_policy(leader, group)
            if not peer or (not peer['enabled'] and ticket is None):
                raise ValueError('DESKTOP_OFF')
            existing = db.execute('SELECT * FROM workers WHERE platform=? AND chat_id=? AND leader_epoch=? AND retired_at IS NULL',
                                  (actor.platform, actor.chat_id, group['leader_epoch'])).fetchone()
            if startup is not None:
                require_listener_startup(ctx, group, peer, leader, own, _json(asdict(stamp)), startup, store.clock(),
                                         existing['worker_id'] if existing else None)
            if ticket is None and existing:
                worker_id = _register_worker(ctx, db, leader, store)
            else:
                names = ('desktop_epoch', 'legacy_ticket_hash', 'legacy_expires_at') if ticket is not None else ('desktop_epoch', 'join_id', 'join_expires_at')
                if any(peer[name] != invite.get(name) for name in names):
                    raise ValueError('STALE_INVITE')
                expires = peer['legacy_expires_at'] if ticket is not None else peer['join_expires_at']
                if ticket is not None:
                    if not peer['legacy_ticket_hash'] or not hmac.compare_digest(_ticket_hash(ticket), peer['legacy_ticket_hash']):
                        raise ValueError('INVALID_OR_EXPIRED_TICKET')
                elif not peer['join_id']:
                    raise ValueError('NO_INVITE')
                if expires is None or store.clock() >= expires:
                    raise ValueError('INVALID_OR_EXPIRED_TICKET' if ticket is not None else 'EXPIRED')
                worker_id = _register_worker(ctx, db, leader, store)
                if ticket is not None:
                    db.execute('UPDATE peers SET legacy_ticket_hash=NULL,legacy_expires_at=NULL WHERE platform=?', (actor.platform,))
            return _configuration(ctx, db, 'CONNECTED', actor.platform, worker_id=worker_id, paired=True)


@_view_operation
def join_peer(ctx, *, startup=None):
    return _join(ctx, startup=startup)


@_view_operation
def connect(ctx, ticket):
    _ticket_hash(ticket)
    return _join(ctx, ticket)


def _worker_occupied(db, worker_id):
    return db.execute("SELECT 1 FROM attempts WHERE worker_id=? AND state IN ('PUBLISHING','WAITING_FOR_USER','QUEUED','CLAIMED','RUNNING','NEEDS_USER','OUTCOME_UNKNOWN') LIMIT 1", (worker_id,)).fetchone() is not None


def worker_ready(db, worker, mode, policy_stamp, now):
    group, peer = _group(db), _peer(db, worker['platform'])
    if worker['retired_at'] is not None or worker['leader_epoch'] != group['leader_epoch'] or not peer or not peer['enabled']:
        return False
    if _worker_occupied(db, worker['worker_id']):
        return False
    if mode == 'manual':
        return True
    waiter = db.execute('SELECT * FROM waiter WHERE worker_id=?', (worker['worker_id'],)).fetchone()
    return bool(mode == 'auto' and peer['auto_enabled'] and waiter and waiter['expires_at'] > now
        and waiter['policy_stamp'] == policy_stamp and waiter['leader_epoch'] == group['leader_epoch']
        and all(waiter[name] == peer[name] for name in ('desktop_epoch', 'auto_epoch')))


def _snapshot(store, *, poll=False):
    with (store.poll_read() if poll else store.read()) as db:
        if db is None:
            return None, [], [], [], [], [], store.clock()
        return (dict(_group(db)), [dict(row) for row in db.execute('SELECT * FROM peers')],
            [dict(row) for row in db.execute('SELECT * FROM workers WHERE retired_at IS NULL ORDER BY registered_at,worker_id')],
            [dict(row) for row in db.execute('SELECT * FROM waiter')], _busy(db), _open_attempts(db), store.clock())


def _connection_status(ctx, snapshot, *, include_worker_chat_ids=False):
    group, peers, workers, waiters, rounds, attempts, now = snapshot
    actor = ctx.actor
    app = actor.platform if actor else (PLATFORM if ctx.component == 'worker' else 'codex')
    role, problem, local_worker = ('unknown' if actor is None else 'unbound'), None, None
    policy, serialized = None, None
    try:
        own = ctx.own_binding()
        own.verify()
        if group and group['leader_chat']:
            leader = ctx.leader_binding(group['leader_manifest'])
            leader.verify()
            stamp = _policy(leader, group)
            policy, serialized = stamp.effective, _json(asdict(stamp))
            if actor and (group['leader_platform'], group['leader_chat'], group['leader_manifest']) == (actor.platform, actor.chat_id, _manifest(own)):
                role = 'leader'
            for worker in workers:
                if actor and (worker['platform'], worker['chat_id'], worker['manifest'], worker['leader_epoch']) == (actor.platform, actor.chat_id, _manifest(own), group['leader_epoch']):
                    role, local_worker = 'worker', worker
        elif ctx.component == 'leader':
            policy = ct_policy.policy_stamp(own.policy_path, actor.chat_id if actor else None, require_epochs=True).effective
    except (ValueError, OSError) as exc:
        problem = getattr(exc, 'reason', None) or str(exc)
    peer = next((row for row in peers if row['platform'] == PLATFORM), None)
    desktop, auto = bool(peer and peer['enabled']), bool(peer and peer['auto_enabled'])
    base_reason = problem or ('LEADER_NOT_SELECTED' if not group or not group['leader_chat'] else 'NATIVE_POLICY_OFF' if policy != 'ON' else 'DESKTOP_OFF' if not desktop else None)
    visible = workers if role == 'leader' else [local_worker] if local_worker else []
    views = []
    for worker in visible:
        related = [row for row in attempts if row['worker_id'] == worker['worker_id']]
        waiter = next((row for row in waiters if row['worker_id'] == worker['worker_id']), None)
        live = bool(waiter and waiter['expires_at'] > now and waiter['policy_stamp'] == serialized
            and waiter['leader_epoch'] == group['leader_epoch']
            and all(waiter[name] == peer[name] for name in ('desktop_epoch', 'auto_epoch')))
        error = base_reason
        try:
            ctx.worker_binding(worker['manifest']).verify()
        except (ValueError, OSError) as exc:
            error = getattr(exc, 'reason', None) or str(exc)
        active = next((row['state'] for row in related if row['state'] in ('RUNNING', 'NEEDS_USER', 'OUTCOME_UNKNOWN')), None)
        pending = any(row['state'] in PENDING for row in related)
        review = any(row['review_pending'] for row in related)
        if active or error:
            readiness = active or error
        elif pending:
            readiness = 'ASSIGNED'
        elif auto:
            readiness = 'AUTO_READY' if live else 'WAITING'
        else:
            readiness = 'MANUAL_READY'
        reason = error or ('WAITER_NOT_READY' if readiness == 'WAITING' else None)
        views.append({'worker_id': worker['worker_id'], 'label': 'agy-' + worker['worker_id'],
            'paired': True, 'waiter_ready': live, 'readiness': readiness, 'reason': reason,
            'review_pending': review, 'attempts': related,
            'next_action': '/coding-team listen' if readiness == 'WAITING' else None})
        if include_worker_chat_ids and role == 'leader':
            views[-1]['chat_id'] = worker['chat_id']
    ready = any(row['readiness'] in ('MANUAL_READY', 'AUTO_READY') for row in views)
    expired = bool(peer and peer['join_expires_at'] is not None and now >= peer['join_expires_at'])
    reason = base_reason
    if actor is None:
        reason = problem or 'UNKNOWN_CHAT'
    elif role == 'unbound' and ctx.component == 'leader' and group and group['leader_chat']:
        reason = problem or 'NOT_LEADER'
    if reason:
        readiness = 'OFF' if reason == 'DESKTOP_OFF' else reason
    elif ready:
        readiness = 'AUTO_READY' if auto else 'MANUAL_READY'
    elif views:
        readiness = views[0]['readiness']
        reason = views[0]['reason']
    elif expired:
        readiness, reason = 'EXPIRED', 'EXPIRED'
    else:
        readiness, reason = 'PAIRING', 'NOT_PAIRED'
    next_action = ('/coding-team on' if reason == 'NATIVE_POLICY_OFF' else '/coding-team agy on' if readiness in ('OFF', 'EXPIRED')
                   else '/coding-team listen' if readiness in ('PAIRING', 'WAITING') else None)
    connection = {'app': PLATFORM if app == 'codex' else 'codex', 'desktop_enabled': desktop, 'auto_enabled': auto,
        'paired': bool(views), 'waiter_ready': any(row['waiter_ready'] for row in views), 'workers': views,
        'readiness': readiness, 'reason': reason, 'next_action': next_action,
        'next_action_app': PLATFORM if next_action == '/coding-team listen' else group['leader_platform'] if group else None,
        'next_action_chat': local_worker['chat_id'] if local_worker and next_action == '/coding-team listen' else group['leader_chat'] if group else None,
        'round_ids': rounds, 'attempts': attempts if role == 'leader' else [row for row in attempts if local_worker and row['worker_id'] == local_worker['worker_id']]}
    state = 'READY' if ready and not reason else 'WAITING' if readiness in ('PAIRING', 'WAITING') else readiness
    return _view(state, ok=problem is None, reason=reason, ready=state == 'READY', app=app, workspace=str(ctx.paths.root),
        role=role, leader={'platform': group['leader_platform'], 'chat_id': group['leader_chat']} if group and group['leader_chat'] else None,
        selected=bool(group and group['leader_chat']), connections=[connection], observed_at=now, effective_policy=policy,
        leader_epoch=group['leader_epoch'] if group else 0, desktop_epoch=peer['desktop_epoch'] if peer else 0,
        auto_epoch=peer['auto_epoch'] if peer else 0, platform=PLATFORM, join_id=peer['join_id'] if peer else None,
        invite_expires_at=peer['join_expires_at'] if peer else None, desktop_enabled=desktop, auto_enabled=auto,
        paired=connection['paired'], waiter_ready=connection['waiter_ready'], round_ids=rounds, attempts=connection['attempts'])


def _status_failure(ctx, exc):
    reason = getattr(exc, 'reason', None) or str(exc)
    return _view(reason, ok=False, reason=reason, app=ctx.actor.platform if ctx.actor else (PLATFORM if ctx.component == 'worker' else 'codex'),
        workspace=str(ctx.paths.root), role='unknown', leader=None, connections=[], observed_at=time.time())


def connection_status(ctx, *, include_worker_chat_ids=False):
    """Read status with chat IDs limited to the Leader's internal tab matching."""
    try:
        with Store(ctx.paths).read_compatible() as db:
            if db is not None and db.execute('PRAGMA user_version').fetchone()[0] == 1:
                return _status_failure(ctx, ValueError('BRIDGE_UPGRADE_REQUIRED'))
        return _connection_status(ctx, _snapshot(Store(ctx.paths)),
                                  include_worker_chat_ids=include_worker_chat_ids)
    except (ValueError, OSError, TimeoutError) as exc:
        return _status_failure(ctx, exc)


desktop_status = connection_status


@_view_operation
def worker_status(ctx):
    actor = ctx.require_actor()
    if ctx.component != 'worker' or actor.platform != PLATFORM:
        raise ValueError('WORKER_PLATFORM_UNSUPPORTED')
    view = connection_status(ctx)
    if view['role'] != 'worker':
        reason = view['reason']
        return {**view, 'ready': False, 'reason': 'WRONG_WORKER' if reason in (None, 'NOT_PAIRED') else reason}
    return view


@_view_operation
def rollback_store(ctx, expected):
    from ct_bridge_upgrade import restore_v1

    actor, own = ctx.require_leader(), ctx.own_binding()
    store = Store(ctx.paths)
    with store.read_compatible() as db:
        if db is None:
            raise ValueError('RECOVERY_NO_V2_STORE')
        state = db.execute('PRAGMA user_version').fetchone()[0]
        if state == 1 and inspect_store(ctx.paths).maintenance:
            return _view('RESTORED_V1', version=1)
        require_leader(db, ctx, expected)
        paths = {row['manifest'] for row in db.execute(
            'SELECT manifest FROM workers WHERE retired_at IS NULL AND manifest IS NOT NULL')}
    workers = [ctx.worker_binding(path) for path in paths]
    with binding.locked_bindings(own, *workers), ct_storage.locked(own.policy_path):
        with store.read() as db:
            require_leader(db, ctx, expected)
            busy = _busy_view(db)
            if busy:
                return busy
            if db.execute('SELECT 1 FROM peers WHERE enabled=1').fetchone():
                raise ValueError('DESKTOP_MUST_BE_OFF')
            current = {row['manifest'] for row in db.execute(
                'SELECT manifest FROM workers WHERE retired_at IS NULL AND manifest IS NOT NULL')}
            if paths != current or actor != expected.actor:
                raise ValueError('BINDING_CHANGED')
        return restore_v1(ctx.paths, clock=store.clock)


@_view_operation
def wait_ready(ctx, expected, *, platform, join_id, desktop_epoch, auto_epoch, wait_seconds):
    if type(wait_seconds) is not int or not 0 <= wait_seconds <= 60:
        raise ValueError('INVALID_WAIT_SECONDS')
    if (not isinstance(expected, binding.LeaderRef) or ctx.require_leader() != expected.actor
            or platform != PLATFORM or not isinstance(join_id, str)
            or any(type(value) is not int or value < 0 for value in (desktop_epoch, auto_epoch))):
        raise ValueError('STALE_ACTIVATION')
    store, deadline, poll = Store(ctx.paths), time.monotonic() + wait_seconds, False
    while True:
        snapshot = _snapshot(store, poll=poll)
        group, peers = snapshot[:2]
        peer = next((row for row in peers if row['platform'] == platform), None)
        if (not group or not peer or (group['leader_platform'], group['leader_chat'], group['leader_manifest'], group['leader_epoch']) !=
                (expected.actor.platform, expected.actor.chat_id, (ctx.source_root / binding.MANIFEST_NAME).as_posix(), expected.epoch)
                or (peer['join_id'], peer['desktop_epoch'], peer['auto_epoch']) != (join_id, desktop_epoch, auto_epoch)):
            return _view('STALE_ACTIVATION', ok=False, reason='STALE_ACTIVATION', ready=False,
                app=expected.actor.platform, workspace=str(ctx.paths.root), connections=[], observed_at=snapshot[-1])
        view = _connection_status(ctx, snapshot)
        if view['ready'] or view['state'] not in ('WAITING', 'ASSIGNED', 'RUNNING', 'NEEDS_USER', 'OUTCOME_UNKNOWN'):
            return view
        if peer['join_expires_at'] is None or snapshot[-1] >= peer['join_expires_at']:
            return {**view, 'state': 'EXPIRED', 'reason': 'EXPIRED', 'ready': False}
        if time.monotonic() >= deadline:
            return view
        poll = True
        time.sleep(min(.25, max(0, deadline - time.monotonic())))
