"""Worker-specific attempt authority with immutable request and receipt files."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import hmac
import json
import math
import secrets
import time
import uuid

from ct_bridge_files import (AttemptKey, BridgeError, check_plain_file, cleanup_task,
                             publish_file, read_file, validate_relative_path)
from ct_bridge_store import Store
import ct_bridge_control as control
import ct_bridge_roles as roles
import ct_bridge_scopes as scopes
import ct_policy

TERMINAL = frozenset(('COMPLETED', 'FAILED', 'CANCELED'))
ACTIVE = frozenset(('RUNNING', 'NEEDS_USER', 'OUTCOME_UNKNOWN'))
PENDING = frozenset(('PUBLISHING', 'WAITING_FOR_USER', 'QUEUED', 'CLAIMED'))
WAITER_SECONDS = 15
CLAIM_SECONDS = 60
GRANT_SECONDS = 600
MAX_BYTES = 1048576
_HEARTBEAT_SECONDS = 4
_IDLE_INTERVAL = .25


@dataclass(frozen=True)
class Request:
    publish_id: str
    mode: str
    worker_id: str
    assignment: dict
    baseline: dict
    content: bytes
    task_id: str | None = None
    replaces_participant_id: str | None = None
    transfer_evidence: dict | None = None


def _json(value):
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                             allow_nan=False)
        _bytes(encoded.encode('utf-8'))
        return encoded
    except (TypeError, ValueError, UnicodeError) as exc:
        raise BridgeError('INVALID_INPUT') from exc


def _bytes(value):
    if not isinstance(value, bytes) or len(value) > MAX_BYTES:
        raise BridgeError('INVALID_PAYLOAD')
    try:
        value.decode('utf-8')
    except UnicodeError as exc:
        raise BridgeError('INVALID_PAYLOAD') from exc
    return value


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _token_hash(token):
    if not isinstance(token, str) or not token or len(token) > 256 or not token.isascii():
        return ''
    return _hash(token.encode('ascii'))


def _matches(token, digest):
    return bool(digest) and hmac.compare_digest(_token_hash(token), digest)


def _view(row=None, *, ok=True, state=None, reason=None, **extra):
    view = {'ok': ok, 'state': state or (row['state'] if row is not None else 'REJECTED'),
            'reason': reason}
    if row is not None:
        for name in ('task_id', 'attempt_no', 'round_id', 'participant_id', 'worker_id',
                     'work_item_id', 'role_variant', 'leader_epoch', 'contract_digest',
                     'result_id', 'result_sha256', 'request_sha256', 'mode', 'kind'):
            if name in row.keys() and row[name] is not None:
                view[name] = row[name]
        if 'worker_id' in view:
            view['worker_label'] = 'agy-' + view['worker_id']
        if row['assignment'] is not None:
            assignment = json.loads(row['assignment'])
            view.update(role=row['role_variant'].removesuffix('-verify'),
                        assignment=assignment, snapshot=assignment['snapshot'],
                        verified_model='unverified', limits='ADVISORY')
        if row['cleaned_at'] is not None:
            view.update(ok=False, state='CLEANED', reason='TASK_CLEANED')
            return view
        if reason is None:
            view['reason'] = row['reason']
    view.update(extra)
    return view


def _group(db):
    return db.execute('SELECT * FROM group_state WHERE id=1').fetchone()


def _peer(db, worker):
    return db.execute('SELECT * FROM peers WHERE platform=?', (worker['platform'],)).fetchone()


def _validate_task_id(task_id):
    validate_relative_path(task_id)
    if '/' in task_id:
        raise BridgeError('INVALID_ATTEMPT')


def _validate_key(key):
    if not isinstance(key, AttemptKey) or type(key.attempt_no) is not int or not 0 < key.attempt_no < 2**31:
        raise BridgeError('INVALID_ATTEMPT')
    _validate_task_id(key.task_id)


def _attempt(db, key):
    _validate_key(key)
    return db.execute(
        'SELECT a.*,t.round_id,t.participant_id,t.cleaned_at,p.work_item_id,p.role_variant,'
        'r.closed_at FROM attempts a JOIN tasks t ON t.id=a.task_id '
        'JOIN rounds r ON r.id=t.round_id LEFT JOIN participants p '
        'ON p.round_id=t.round_id AND p.participant_id=t.participant_id '
        'WHERE a.task_id=? AND a.attempt_no=?',
        (key.task_id, key.attempt_no)).fetchone()


def _key(row):
    return AttemptKey(row['task_id'], row['attempt_no'])


def _holder(row):
    return scopes.ScopeHolder(row['round_id'], row['participant_id'], _key(row))


def _set(db, key, **values):
    db.execute('UPDATE attempts SET ' + ','.join(name + '=?' for name in values)
               + ' WHERE task_id=? AND attempt_no=?',
               (*values.values(), key.task_id, key.attempt_no))


def _policy_stamp(leader, group):
    policy = ct_policy.policy_stamp(leader.policy_path, group['leader_chat'], require_epochs=True)
    return _json(asdict(policy)) if policy.effective == 'ON' else None


def _admission(group, worker, peer, stamp, mode):
    if worker is None or worker['retired_at'] is not None or worker['leader_epoch'] != group['leader_epoch']:
        return 'WRONG_WORKER'
    if stamp is None:
        return 'POLICY_OFF'
    if peer is None or not peer['enabled']:
        return 'DESKTOP_OFF'
    if mode == 'auto' and not peer['auto_enabled']:
        return 'AUTO_OFF'
    return None


def _fence(group, worker, peer, stamp, row):
    if row['closed_at'] is not None or row['leader_epoch'] != group['leader_epoch']:
        return 'STALE_BINDING'
    reason = _admission(group, worker, peer, stamp, row['mode'])
    if reason:
        return reason
    if stamp != row['policy_stamp']:
        return 'STALE_POLICY'
    if peer['desktop_epoch'] != row['desktop_epoch']:
        return 'STALE_DESKTOP'
    if row['mode'] == 'auto' and peer['auto_epoch'] != row['auto_epoch']:
        return 'STALE_AUTO'
    return None


def _generation(group, peer, stamp):
    return (group['leader_epoch'], peer['desktop_epoch'], peer['auto_epoch'], stamp)


def _waiter_generation(waiter):
    return tuple(waiter[name] for name in ('leader_epoch', 'desktop_epoch', 'auto_epoch', 'policy_stamp'))


def _waiter(db, worker_id):
    return db.execute('SELECT * FROM waiter WHERE worker_id=?', (worker_id,)).fetchone()


def _live(waiter, generation, now):
    return (waiter is not None and waiter['expires_at'] > now
            and _waiter_generation(waiter) == generation)


def _slot(db, worker_id):
    return db.execute("SELECT task_id,attempt_no FROM attempts WHERE worker_id=? AND state IN "
                      "('PUBLISHING','WAITING_FOR_USER','QUEUED','CLAIMED',"
                      "'RUNNING','NEEDS_USER','OUTCOME_UNKNOWN')", (worker_id,)).fetchone()


def _participant(db, row, **values):
    db.execute('UPDATE participants SET ' + ','.join(name + '=?' for name in values)
               + ' WHERE round_id=? AND participant_id=?',
               (*values.values(), row['round_id'], row['participant_id']))


def _revoke(db, row, now, reason, *, state='REVOKED'):
    _set(db, _key(row), state=state, reason=reason, claim_hash=None, claim_expires_at=None,
         finished_at=now)
    scopes.release(db, _holder(row))
    _participant(db, row, finished_at=now)


def _pending_reason(db, row, group, worker, peer, stamp, now, *, check_waiter=False):
    reason = _fence(group, worker, peer, stamp, row)
    if reason:
        return reason
    if row['state'] == 'CLAIMED' and row['claim_expires_at'] <= now:
        return 'CLAIM_EXPIRED'
    if check_waiter and row['mode'] == 'auto' and row['state'] in ('PUBLISHING', 'QUEUED'):
        waiter = _waiter(db, row['worker_id'])
        if not _live(waiter, _generation(group, peer, stamp), now) or waiter['nonce_hash'] != row['claim_hash']:
            return 'WAITER_LOST'
    return None


def _assignment(leader, worker_binding, raw):
    contract = roles.load_contract(leader.skill_dir)
    if worker_binding is None or roles.load_contract(worker_binding.skill_dir).contract_digest != contract.contract_digest:
        raise BridgeError('ROLE_CONTRACT_MISMATCH')
    return roles.validate_assignment(contract, raw, verified_model=None)


def _stored_assignment(paths, leader, worker_binding, row):
    assignment = _assignment(leader, worker_binding, json.loads(row['assignment']))
    if (assignment.contract_digest != row['contract_digest']
            or assignment.variant != row['role_variant']
            or assignment.work_item_id != row['work_item_id']):
        raise BridgeError('ROLE_CONTRACT_MISMATCH')
    scope = scopes.canonical_scopes(paths, assignment)
    baseline = json.loads(row['baseline'])
    scopes.verify_baseline(paths, snapshot=assignment.snapshot, scope=scope, baseline=baseline)
    return assignment, scope, baseline


def _same_owner(db, row):
    item = db.execute('SELECT * FROM work_items WHERE round_id=? AND work_item_id=?',
                      (row['round_id'], row['work_item_id'])).fetchone()
    if item is None or item['owner_participant_id'] != row['participant_id'] or item['state'] != 'active':
        raise BridgeError('WORK_ITEM_OWNERSHIP_REQUIRED')
    for dependency, acceptance in json.loads(item['depends_on']).items():
        prerequisite = db.execute('SELECT state,acceptance_id FROM work_items '
                                  'WHERE round_id=? AND work_item_id=?',
                                  (row['round_id'], dependency)).fetchone()
        if (prerequisite is None or prerequisite['state'] != 'accepted'
                or prerequisite['acceptance_id'] != acceptance):
            raise BridgeError('DEPENDENCY_NOT_ACCEPTED')


def _scope_json(scope):
    return _json({'read_paths': scope.read_paths, 'write_paths': scope.write_paths,
                  'check_outputs': scope.check_outputs})


def _request(request):
    if (not isinstance(request, Request) or not isinstance(request.publish_id, str)
            or not request.publish_id or len(request.publish_id) > 200
            or request.mode not in ('manual', 'auto') or not isinstance(request.worker_id, str)
            or not request.worker_id or len(request.worker_id) > 200
            or not isinstance(request.assignment, dict) or not isinstance(request.baseline, dict)):
        raise BridgeError('INVALID_REQUEST')
    if request.task_id is not None:
        _validate_task_id(request.task_id)
        if request.replaces_participant_id is not None or request.transfer_evidence is not None:
            raise BridgeError('INVALID_REQUEST')
    _bytes(request.content)


def _dispatch_hash(round_id, request, assignment, digest):
    return _hash(_json({
        'round_id': round_id, 'worker_id': request.worker_id, 'mode': request.mode,
        'task_id': request.task_id, 'assignment': roles.assignment_to_dict(assignment),
        'contract_digest': assignment.contract_digest, 'baseline': request.baseline,
        'request_sha256': digest, 'request_size': len(request.content),
        'replaces_participant_id': request.replaces_participant_id,
        'transfer_evidence': request.transfer_evidence,
    }).encode('utf-8'))


def _task_for_publication(db, round_id, request, assignment, encoded, now):
    if request.task_id is None:
        task_id = 'CT-' + uuid.uuid4().hex
        participant_id = 'desktop-' + uuid.uuid4().hex
        db.execute('INSERT INTO participants(round_id,participant_id,work_item_id,executor,'
                   'worker_id,role_variant,assignment,replaces_participant_id) VALUES(?,?,?,?,?,?,?,?)',
                   (round_id, participant_id, assignment.work_item_id, 'desktop', request.worker_id,
                    assignment.variant, encoded, request.replaces_participant_id))
        db.execute('INSERT INTO tasks(id,round_id,participant_id,worker_id) VALUES(?,?,?,?)',
                   (task_id, round_id, participant_id, request.worker_id))
        return AttemptKey(task_id, 1), participant_id
    previous = db.execute('SELECT task_id,attempt_no FROM attempts WHERE task_id=? '
                          'ORDER BY attempt_no DESC LIMIT 1', (request.task_id,)).fetchone()
    row = _attempt(db, _key(previous)) if previous is not None else None
    if (row is None or row['round_id'] != round_id or row['worker_id'] != request.worker_id
            or row['work_item_id'] != assignment.work_item_id or row['role_variant'] != assignment.variant
            or row['cleaned_at'] is not None):
        raise BridgeError('STALE_TASK')
    if row['state'] not in TERMINAL or (row['result_id'] is not None and row['ack_at'] is None):
        raise BridgeError('REVIEW_REQUIRED')
    _same_owner(db, row)
    original = json.loads(row['assignment'])
    if original['depends_on'] != roles.assignment_to_dict(assignment)['depends_on']:
        raise BridgeError('DEPENDENCY_CONFLICT')
    _participant(db, row, finished_at=None)
    return AttemptKey(request.task_id, row['attempt_no'] + 1), row['participant_id']


def _reserve_publication(ctx, expected, round_id, request, digest):
    with control.operation_guard(ctx, worker_id=request.worker_id) as (store, db, leader, worker_binding):
        group = control.require_leader(db, ctx, expected)
        control.round_for_leader(db, expected, round_id)
        worker = control.worker_by_id(db, request.worker_id)
        if worker is None:
            raise BridgeError('WRONG_WORKER')
        assignment = _assignment(leader, worker_binding, request.assignment)
        dispatch = _dispatch_hash(round_id, request, assignment, digest)
        prior = db.execute('SELECT task_id,attempt_no FROM attempts WHERE publish_id=?',
                           (request.publish_id,)).fetchone()
        if prior is not None:
            row = _attempt(db, _key(prior))
            if row['dispatch_hash'] != dispatch:
                raise BridgeError('PUBLICATION_CONFLICT')
            if row['state'] != 'PUBLISHING' and row['cleaned_at'] is None and row['state'] not in {'REVOKED', 'CANCELED'}:
                read_file(ctx.paths, _key(row), 'request', row['request_sha256'])
            return row
        peer = _peer(db, worker)
        stamp = _policy_stamp(leader, group)
        reason = _admission(group, worker, peer, stamp, request.mode)
        if reason:
            raise BridgeError(reason)
        now = store.clock()
        waiter = _waiter(db, request.worker_id)
        if request.mode == 'auto' and not _live(waiter, _generation(group, peer, stamp), now):
            raise BridgeError('WAITER_NOT_READY')
        slot = _slot(db, request.worker_id)
        if slot is not None:
            held = _attempt(db, _key(slot))
            if held['state'] in PENDING:
                reason = _pending_reason(db, held, group, worker, peer, stamp, now, check_waiter=True)
                if reason:
                    _revoke(db, held, now, reason)
                    slot = None

        scope = scopes.canonical_scopes(ctx.paths, assignment)
        scopes.verify_baseline(ctx.paths, snapshot=assignment.snapshot, scope=scope, baseline=request.baseline)
        encoded = roles.canonical_assignment_json(assignment)
        key, participant_id = _task_for_publication(db, round_id, request, assignment, encoded, now)
        if request.task_id is None:
            control.admit_work_item(db, ctx.paths, round_id, participant_id, assignment,
                                    replaces_participant_id=request.replaces_participant_id,
                                    transfer_evidence=request.transfer_evidence)
        else:
            control._dependencies(db, ctx.paths, round_id, assignment)
        if slot is not None:
            raise BridgeError('SLOT_BUSY')
        scopes.reserve(db, scopes.ScopeHolder(round_id, participant_id, key), scope)
        claim_hash = waiter['nonce_hash'] if request.mode == 'auto' else None
        db.execute(
            'INSERT INTO attempts(task_id,attempt_no,publish_id,state,mode,kind,request_sha256,request_size,'
            'scope,baseline,desktop_epoch,auto_epoch,leader_epoch,policy_stamp,created_at,worker_id,'
            'assignment,contract_digest,dispatch_hash,claim_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (key.task_id, key.attempt_no, request.publish_id, 'PUBLISHING', request.mode,
             roles.assignment_kind(assignment), digest, len(request.content), _scope_json(scope),
             _json(request.baseline), peer['desktop_epoch'], peer['auto_epoch'], group['leader_epoch'],
             stamp, now, request.worker_id, encoded, assignment.contract_digest, dispatch, claim_hash))
        return _attempt(db, key)


def publish(ctx, expected, round_id, request: Request):
    """Reserve the work item, worker slot and scope before publishing Markdown."""
    _request(request)
    digest = _hash(request.content)
    try:
        reserved = _reserve_publication(ctx, expected, round_id, request, digest)
    except (BridgeError, ValueError) as exc:
        if str(exc) == 'PUBLICATION_CONFLICT':
            raise
        return _view(ok=False, reason=getattr(exc, 'reason', None) or str(exc))
    if reserved['state'] != 'PUBLISHING' or reserved['cleaned_at'] is not None:
        return _view(reserved)
    key = _key(reserved)
    ref = publish_file(ctx.paths, key, 'request', request.content)
    with control.operation_guard(ctx, worker_id=request.worker_id) as (store, db, leader, worker_binding):
        group = control.require_leader(db, ctx, expected)
        row = _attempt(db, key)
        if row['state'] != 'PUBLISHING' or row['cleaned_at'] is not None:
            return _view(row)
        if row['dispatch_hash'] != reserved['dispatch_hash'] or row['request_sha256'] != ref.sha256:
            raise BridgeError('PUBLICATION_CONFLICT')
        worker = control.worker_by_id(db, request.worker_id)
        peer = _peer(db, worker) if worker is not None else None
        stamp = _policy_stamp(leader, group)
        reason = _pending_reason(db, row, group, worker, peer, stamp, store.clock(), check_waiter=True)
        if reason:
            _revoke(db, row, store.clock(), reason)
            return _view(_attempt(db, key), ok=False)
        try:
            _same_owner(db, row)
            assignment = _assignment(leader, worker_binding, json.loads(row['assignment']))
            if assignment.contract_digest != row['contract_digest']:
                raise BridgeError('ROLE_CONTRACT_MISMATCH')
            control._dependencies(db, ctx.paths, round_id, assignment)
        except (BridgeError, ValueError) as exc:
            _revoke(db, row, store.clock(), getattr(exc, 'reason', None) or str(exc))
            return _view(_attempt(db, key), ok=False)
        read_file(ctx.paths, key, 'request', row['request_sha256'])
        _set(db, key, state='QUEUED' if row['mode'] == 'auto' else 'WAITING_FOR_USER', reason=None)
        return _view(_attempt(db, key), request_path=str(ctx.paths.data / ref.relative_path))


def _worker_actor(db, ctx):
    if ctx.component != 'worker':
        return None
    return control.worker_for_actor(db, ctx)


def _wake(ctx, db, worker, worker_binding, leader, row, now):
    _same_owner(db, row)
    assignment = _assignment(leader, worker_binding, json.loads(row['assignment']))
    if assignment.contract_digest != row['contract_digest']:
        raise BridgeError('ROLE_CONTRACT_MISMATCH')
    key = _key(row)
    read_file(ctx.paths, key, 'request', row['request_sha256'])
    claim = secrets.token_urlsafe(32)
    _set(db, key, state='CLAIMED', claim_hash=_token_hash(claim), claim_expires_at=now + CLAIM_SECONDS)
    db.execute('DELETE FROM waiter WHERE worker_id=?', (worker['worker_id'],))
    return _view(_attempt(db, key), claim=claim)


def listen(ctx, *, startup=None, joined_worker_id=None):
    """Register one heartbeat per worker and poll read-only until it can claim."""
    nonce_hash = _token_hash(secrets.token_urlsafe(32))
    worker_id = None
    generation = None
    store = Store(ctx.paths)
    try:
        while True:
            with control.operation_guard(ctx) as (writer, db, leader, worker_binding):
                worker = _worker_actor(db, ctx)
                if worker is None:
                    return _view(ok=False, state='STOPPED', reason='WRONG_WORKER')
                group = _group(db)
                peer = _peer(db, worker)
                stamp = _policy_stamp(leader, group)
                reason = _admission(group, worker, peer, stamp, 'auto')
                if reason:
                    return _view(ok=False, state='STOPPED', reason=reason)
                observed_worker_id = worker['worker_id']
                current = _waiter(db, observed_worker_id)
                now = writer.clock()
                signature = _generation(group, peer, stamp)
                if generation is None:
                    if startup is not None:
                        control.require_listener_startup(ctx, group, peer, leader, worker_binding, stamp,
                                                         startup, now, observed_worker_id,
                                                         joined_worker_id=joined_worker_id)
                    if _live(current, signature, now):
                        return _view(ok=False, state='STOPPED', reason='WAITER_BUSY')
                elif (worker_id != observed_worker_id or signature != generation
                      or not _live(current, signature, now) or current['nonce_hash'] != nonce_hash):
                    return _view(ok=False, state='STOPPED', reason='WAITER_INVALIDATED')

                slot = _slot(db, observed_worker_id)
                row = _attempt(db, _key(slot)) if slot is not None else None
                if row is not None and row['state'] in PENDING:
                    reason = _pending_reason(db, row, group, worker, peer, stamp, now, check_waiter=True)
                    if reason:
                        _revoke(db, row, now, reason)
                        row = None
                if row is not None and row['state'] in ACTIVE | {'CLAIMED', 'WAITING_FOR_USER'}:
                    return _view(ok=False, state='STOPPED', reason='SLOT_BUSY')
                if row is not None and row['state'] == 'QUEUED':
                    return _wake(ctx, db, worker, worker_binding, leader, row, now)

                db.execute('INSERT INTO waiter(worker_id,nonce_hash,leader_epoch,desktop_epoch,auto_epoch,'
                           'policy_stamp,expires_at) VALUES(?,?,?,?,?,?,?) '
                           'ON CONFLICT(worker_id) DO UPDATE SET nonce_hash=excluded.nonce_hash,'
                           'leader_epoch=excluded.leader_epoch,desktop_epoch=excluded.desktop_epoch,'
                           'auto_epoch=excluded.auto_epoch,policy_stamp=excluded.policy_stamp,'
                           'expires_at=excluded.expires_at',
                           (observed_worker_id, nonce_hash, *signature, now + WAITER_SECONDS))
                worker_id, generation = observed_worker_id, signature

            heartbeat = time.monotonic() + _HEARTBEAT_SECONDS
            while time.monotonic() < heartbeat:
                with store.poll_read() as db:
                    if db is None:
                        return _view(ok=False, state='STOPPED', reason='WAITER_INVALIDATED')
                    current = _waiter(db, worker_id)
                    current_group = _group(db)
                    current_peer = db.execute('SELECT * FROM peers WHERE platform=?',
                                              (worker['platform'],)).fetchone()
                    if (not _live(current, generation, store.clock()) or current['nonce_hash'] != nonce_hash
                            or current_peer is None or not current_peer['enabled']
                            or not current_peer['auto_enabled']
                            or _generation(current_group, current_peer, stamp) != generation):
                        return _view(ok=False, state='STOPPED', reason='WAITER_INVALIDATED')
                    queued = db.execute("SELECT 1 FROM attempts WHERE worker_id=? AND state='QUEUED'",
                                        (worker_id,)).fetchone()
                    if queued:
                        break
                time.sleep(_IDLE_INTERVAL)
    finally:
        if worker_id is not None:
            try:
                with store.poll_read() as db:
                    waiter = _waiter(db, worker_id) if db is not None else None
                    owned = waiter is not None and waiter['nonce_hash'] == nonce_hash
                if owned:
                    with store.write() as db:
                        db.execute('DELETE FROM waiter WHERE worker_id=? AND nonce_hash=?',
                                   (worker_id, nonce_hash))
            except (OSError, ValueError):
                pass


def _request_view(ctx, row, **extra):
    key = _key(row)
    payload = read_file(ctx.paths, key, 'request', row['request_sha256'])
    if len(payload) != row['request_size']:
        raise BridgeError('PUBLICATION_CONFLICT')
    return _view(row, request=payload.decode('utf-8'), scope=json.loads(row['scope']),
                 baseline=json.loads(row['baseline']), **extra)


def begin(ctx, key: AttemptKey, claim: str | None):
    _validate_key(key)
    with control.operation_guard(ctx) as (store, db, leader, worker_binding):
        worker = _worker_actor(db, ctx)
        row = _attempt(db, key)
        if row is None or worker is None or row['worker_id'] != worker['worker_id']:
            return _view(ok=False, reason='WRONG_WORKER')
        if row['cleaned_at'] is not None or row['state'] not in ('WAITING_FOR_USER', 'CLAIMED'):
            return _view(row, ok=False, reason='NOT_STARTABLE')
        if row['mode'] == 'auto' and not _matches(claim, row['claim_hash']):
            return _view(ok=False, reason='INVALID_CLAIM')
        if row['mode'] == 'manual' and claim is not None:
            return _view(ok=False, reason='UNEXPECTED_CLAIM')
        group = _group(db)
        peer = _peer(db, worker)
        reason = _pending_reason(db, row, group, worker, peer, _policy_stamp(leader, group), store.clock())
        if reason:
            _revoke(db, row, store.clock(), reason)
            return _view(_attempt(db, key), ok=False)
        try:
            slot = _slot(db, worker['worker_id'])
            if slot is None or _key(slot) != key:
                raise BridgeError('SLOT_BUSY')
            _same_owner(db, row)
            assignment, scope, _ = _stored_assignment(ctx.paths, leader, worker_binding, row)
            control._dependencies(db, ctx.paths, row['round_id'], assignment)
            if _scope_json(scope) != row['scope']:
                raise BridgeError('SCOPE_CHANGED')
        except (BridgeError, ValueError) as exc:
            _revoke(db, row, store.clock(), getattr(exc, 'reason', None) or str(exc))
            return _view(_attempt(db, key), ok=False)
        ticket = secrets.token_urlsafe(32)
        now = store.clock()
        _set(db, key, state='RUNNING', started_at=now, execution_hash=_token_hash(ticket),
             claim_hash=None, claim_expires_at=None, reason=None)
        if row['mode'] == 'manual':
            db.execute('DELETE FROM waiter WHERE worker_id=?', (worker['worker_id'],))
        db.execute('UPDATE participants SET started_at=COALESCE(started_at,?) '
                   'WHERE round_id=? AND participant_id=?', (now, row['round_id'], row['participant_id']))
        return _request_view(ctx, _attempt(db, key), execution_ticket=ticket,
                             assignment=roles.assignment_to_dict(assignment))


def _leader_attempt(db, ctx, expected, key):
    group = control.require_leader(db, ctx, expected)
    return group, _attempt(db, key)


def _selected_worker(ctx, key):
    # This preliminary read selects the component lock only. Authority and the
    # exact attempt are checked again under the installation and policy locks.
    with Store(ctx.paths).read() as db:
        row = _attempt(db, key) if db is not None else None
        return row['worker_id'] if row is not None else None


def reoffer(ctx, expected, key: AttemptKey, mode):
    if mode not in ('manual', 'auto'):
        raise BridgeError('INVALID_MODE')
    _validate_key(key)
    worker_id = _selected_worker(ctx, key)
    try:
        with control.operation_guard(ctx, worker_id=worker_id) as (store, db, leader, worker_binding):
            group, row = _leader_attempt(db, ctx, expected, key)
            if row is None or row['started_at'] is not None or row['state'] not in PENDING | {'REVOKED'}:
                return _view(row, ok=False, reason='NOT_PENDING')
            if row['cleaned_at'] is not None or row['closed_at'] is not None or row['leader_epoch'] != group['leader_epoch']:
                return _view(row, ok=False, reason='STALE_BINDING')
            if row['mode'] == 'manual' and mode == 'auto':
                return _view(row, ok=False, reason='MANUAL_DRAFT')
            worker = control.worker_by_id(db, worker_id)
            peer = _peer(db, worker) if worker is not None else None
            stamp = _policy_stamp(leader, group)
            reason = _admission(group, worker, peer, stamp, mode)
            if reason:
                return _view(row, ok=False, reason=reason)
            now = store.clock()
            waiter = _waiter(db, worker_id)
            if mode == 'auto' and not _live(waiter, _generation(group, peer, stamp), now):
                return _view(row, ok=False, reason='WAITER_NOT_READY')
            slot = _slot(db, worker_id)
            if slot is not None and _key(slot) != key:
                return _view(row, ok=False, reason='SLOT_BUSY')
            _same_owner(db, row)
            _, scope, _ = _stored_assignment(ctx.paths, leader, worker_binding, row)
            read_file(ctx.paths, key, 'request', row['request_sha256'])
            if row['state'] == 'REVOKED':
                scopes.reserve(db, _holder(row), scope)
            _set(db, key, mode=mode, state='QUEUED' if mode == 'auto' else 'WAITING_FOR_USER',
                 desktop_epoch=peer['desktop_epoch'], auto_epoch=peer['auto_epoch'],
                 policy_stamp=stamp, finished_at=None, reason=None, claim_expires_at=None,
                 claim_hash=waiter['nonce_hash'] if mode == 'auto' else None)
            _participant(db, row, finished_at=None)
            return _view(_attempt(db, key))
    except (BridgeError, ValueError) as exc:
        return _view(ok=False, reason=getattr(exc, 'reason', None) or str(exc))


def _execution_owner(ctx, db, row, execution_ticket):
    if row is None or row['worker_id'] is None or ctx.component != 'worker':
        return False
    worker = control.worker_by_id(db, row['worker_id'])
    actor = ctx.require_actor()
    return (worker is not None and worker['platform'] == actor.platform and worker['chat_id'] == actor.chat_id
            and worker['leader_epoch'] == row['leader_epoch']
            and control.matches_component(ctx, worker['manifest'])
            and _matches(execution_ticket, row['execution_hash']))


def _result_path(paths, key):
    return paths.data / key.task_id / f'attempt-{key.attempt_no}' / 'result.md'


def _read_result(paths, row):
    payload = read_file(paths, _key(row), 'result', row['result_sha256'])
    if len(payload) != row['result_size']:
        raise BridgeError('RESULT_CONFLICT')
    return payload


def _finish_row(db, row, now, state, digest, size, *, reason=None, evidence=None):
    _set(db, _key(row), state=state, result_id='result-' + uuid.uuid4().hex,
         result_sha256=digest, result_size=size, finished_at=now, reason=reason, evidence=evidence)
    _participant(db, row, finished_at=now)
    scopes.release(db, _holder(row), access='read')


def finish(ctx, key: AttemptKey, execution_ticket, state, result: bytes):
    _validate_key(key)
    if not isinstance(state, str) or state not in TERMINAL | {'NEEDS_USER', 'OUTCOME_UNKNOWN'}:
        raise BridgeError('INVALID_FINISH_STATE')
    _bytes(result)
    with control.operation_guard(ctx, terminal=True) as (store, db, _, _):
        row = _attempt(db, key)
        if not _execution_owner(ctx, db, row, execution_ticket):
            return _view(ok=False, reason='INVALID_EXECUTION')
        if row['cleaned_at'] is not None:
            return _view(row)
        if row['state'] in TERMINAL:
            if row['state'] != state or row['result_sha256'] != _hash(result):
                raise BridgeError('RESULT_CONFLICT')
            _read_result(ctx.paths, row)
            return _view(row)
        if row['state'] not in ACTIVE or row['closed_at'] is not None:
            return _view(row, ok=False, reason='NOT_RUNNING')
        if state not in TERMINAL:
            reason = result.decode('utf-8')
            if check_plain_file(_result_path(ctx.paths, key), ctx.paths.data):
                return _view(row, ok=False, reason='RESULT_RECONCILIATION_REQUIRED')
            if row['state'] == state and row['reason'] != reason:
                raise BridgeError('RESULT_CONFLICT')
            _set(db, key, state=state, reason=reason)
            return _view(_attempt(db, key))
        ref = publish_file(ctx.paths, key, 'result', result)
        _finish_row(db, row, store.clock(), state, ref.sha256, ref.size)
        return _view(_attempt(db, key), result_path=str(ctx.paths.data / ref.relative_path))


def resume(ctx, key: AttemptKey, execution_ticket):
    _validate_key(key)
    with control.operation_guard(ctx, terminal=True) as (_, db, _, _):
        row = _attempt(db, key)
        if not _execution_owner(ctx, db, row, execution_ticket):
            return _view(ok=False, reason='INVALID_EXECUTION')
        if row['cleaned_at'] is not None:
            return _view(row)
        if row['closed_at'] is not None or row['state'] not in ('NEEDS_USER', 'OUTCOME_UNKNOWN'):
            return _view(row, ok=False, reason='NOT_RESUMABLE')
        if check_plain_file(_result_path(ctx.paths, key), ctx.paths.data):
            return _view(row, ok=False, reason='RESULT_RECONCILIATION_REQUIRED')
        _set(db, key, state='RUNNING', reason=None)
        return _request_view(ctx, _attempt(db, key))


def cancel_pending(ctx, expected, key: AttemptKey):
    _validate_key(key)
    with control.operation_guard(ctx, terminal=True) as (store, db, _, _):
        _, row = _leader_attempt(db, ctx, expected, key)
        if row is not None and row['cleaned_at'] is not None:
            return _view(row)
        if (row is None or row['started_at'] is not None
                or row['state'] not in PENDING | {'CANCELED', 'REVOKED'} or row['closed_at'] is not None):
            return _view(row, ok=False, reason='STARTED_REQUIRES_RECONCILIATION')
        if row['state'] != 'CANCELED':
            _revoke(db, row, store.clock(), 'CANCELED_BEFORE_BEGIN', state='CANCELED')
        return _view(_attempt(db, key))


def _scope_evidence(key, row, evidence):
    return (isinstance(evidence, dict) and evidence.get('task_id') == key.task_id
            and type(evidence.get('attempt_no')) is int and evidence['attempt_no'] == key.attempt_no
            and isinstance(evidence.get('summary'), str) and bool(evidence['summary'].strip())
            and evidence.get('scope_verified') is True and row['assignment'] is not None
            and evidence.get('snapshot') == json.loads(row['assignment'])['snapshot'])


def reconcile(ctx, expected, key: AttemptKey, evidence):
    _validate_key(key)
    with control.operation_guard(ctx, terminal=True) as (store, db, _, _):
        group, row = _leader_attempt(db, ctx, expected, key)
        if row is not None and row['cleaned_at'] is not None:
            return _view(row)
        if row is None or row['leader_epoch'] != group['leader_epoch'] or row['closed_at'] is not None:
            return _view(row, ok=False, reason='STALE_ATTEMPT')
        if (not _scope_evidence(key, row, evidence) or evidence.get('confirmed_by') not in ('worker', 'user')
                or evidence.get('stopped') is not True):
            return _view(row, ok=False, reason='SCOPED_CONFIRMATION_REQUIRED')
        encoded = _json(evidence)
        if row['state'] in TERMINAL:
            if row['evidence'] == encoded and row['result_id']:
                _read_result(ctx.paths, row)
                return _view(row)
            return _view(row, ok=False, reason='ALREADY_FINISHED')
        if row['state'] not in ACTIVE:
            return _view(row, ok=False, reason='NOT_RUNNING')
        if check_plain_file(_result_path(ctx.paths, key), ctx.paths.data):
            state, digest = evidence.get('state'), evidence.get('result_sha256')
            if not isinstance(state, str) or state not in TERMINAL or not isinstance(digest, str):
                return _view(row, ok=False, reason='RESULT_RECONCILIATION_REQUIRED')
            size = len(read_file(ctx.paths, key, 'result', digest))
        else:
            if evidence.get('state', 'CANCELED') != 'CANCELED' or 'result_sha256' in evidence:
                return _view(row, ok=False, reason='RESULT_RECONCILIATION_REQUIRED')
            state = 'CANCELED'
            ref = publish_file(ctx.paths, key, 'result', encoded.encode('utf-8'))
            digest, size = ref.sha256, ref.size
        _finish_row(db, row, store.clock(), state, digest, size, reason='CONFIRMED_STOPPED', evidence=encoded)
        scopes.release(db, _holder(row), access='write')
        return _view(_attempt(db, key))


def grant_read(ctx, expected, key: AttemptKey):
    _validate_key(key)
    with control.operation_guard(ctx, terminal=True) as (store, db, _, _):
        _, row = _leader_attempt(db, ctx, expected, key)
        if row is None:
            return _view(ok=False, reason='ATTEMPT_NOT_FOUND')
        if row['cleaned_at'] is not None:
            return _view(row)
        token = secrets.token_urlsafe(32)
        expires_at = store.clock() + GRANT_SECONDS
        db.execute('INSERT INTO read_grants(task_id,attempt_no,token_hash,expires_at) VALUES(?,?,?,?) '
                   'ON CONFLICT(task_id,attempt_no) DO UPDATE SET token_hash=excluded.token_hash,'
                   'expires_at=excluded.expires_at',
                   (key.task_id, key.attempt_no, _token_hash(token), expires_at))
        return _view(row, grant=token, expires_at=expires_at)


def _read_authorized(db, ctx, key, grant, now):
    if grant is None:
        control.require_leader(db, ctx, control.context_leader(db))
        return True
    ctx.require_leader()
    group = _group(db)
    if group is None or not control.matches_component(ctx, group['leader_manifest']):
        return False
    ctx.own_binding()
    permission = db.execute('SELECT token_hash,expires_at FROM read_grants WHERE task_id=? AND attempt_no=?',
                            (key.task_id, key.attempt_no)).fetchone()
    return (permission is not None and permission['expires_at'] > now
            and _matches(grant, permission['token_hash']))


def collect(ctx, key: AttemptKey, grant: str | None, *, wait_seconds=0):
    """Collect one receipt under exact grant or Leader authority without changing state."""
    _validate_key(key)
    if (type(wait_seconds) not in (int, float) or not 0 <= wait_seconds <= 60
            or not math.isfinite(wait_seconds)):
        raise BridgeError('INVALID_WAIT')
    deadline = time.monotonic() + wait_seconds
    store = Store(ctx.paths)
    snapshot = store.read
    while True:
        with snapshot() as db:
            try:
                authorized = db is not None and _read_authorized(db, ctx, key, grant, store.clock())
            except (OSError, ValueError):
                authorized = False
            if not authorized:
                return _view(ok=False, reason='INVALID_READ_GRANT' if grant is not None else 'STALE_LEADER')
            row = _attempt(db, key)
            if row is None:
                return _view(ok=False, reason='ATTEMPT_NOT_FOUND')
            if row['cleaned_at'] is not None:
                return _view(row)
            if row['result_id'] is not None:
                payload = _read_result(ctx.paths, row)
                return _view(row, result=payload.decode('utf-8'), result_size=len(payload),
                             result_path=str(_result_path(ctx.paths, key)))
            view = _view(row)
        if time.monotonic() >= deadline or row['state'] in TERMINAL | {'REVOKED'}:
            return view
        snapshot = store.poll_read
        time.sleep(min(_IDLE_INTERVAL, max(0, deadline - time.monotonic())))


def ack(ctx, expected, key: AttemptKey, result_id, evidence):
    _validate_key(key)
    with control.operation_guard(ctx, terminal=True) as (store, db, _, _):
        _, row = _leader_attempt(db, ctx, expected, key)
        if row is not None and row['cleaned_at'] is not None:
            return _view(row)
        if row is None or row['state'] not in TERMINAL or row['result_id'] != result_id or not result_id:
            return _view(row, ok=False, reason='RECEIPT_MISMATCH')
        _read_result(ctx.paths, row)
        if not _scope_evidence(key, row, evidence):
            return _view(row, ok=False, reason='SCOPE_EVIDENCE_REQUIRED')
        encoded = _json(evidence)
        if row['ack_at'] is not None:
            if row['ack_evidence'] != encoded:
                raise BridgeError('ACK_CONFLICT')
        else:
            if row['closed_at'] is not None:
                return _view(row, ok=False, reason='STALE_ATTEMPT')
            _set(db, key, ack_at=store.clock(), ack_evidence=encoded)
            scopes.release(db, _holder(row), access='write')
        return _view(_attempt(db, key), acknowledged=True)


def cleanup(ctx, expected, task_id):
    """Commit a tombstone before removing the transport for a closed task."""
    _validate_task_id(task_id)
    with control.operation_guard(ctx, terminal=True) as (store, db, _, _):
        control.require_leader(db, ctx, expected)
        task = db.execute('SELECT t.*,r.closed_at FROM tasks t JOIN rounds r ON r.id=t.round_id '
                          'WHERE t.id=?', (task_id,)).fetchone()
        if task is None:
            return _view(ok=False, reason='TASK_NOT_FOUND')
        rows = db.execute('SELECT * FROM attempts WHERE task_id=? ORDER BY attempt_no', (task_id,)).fetchall()
        if task['closed_at'] is None or any(
                row['state'] not in TERMINAL | {'REVOKED'}
                or (row['started_at'] is not None and row['ack_at'] is None) for row in rows):
            return _view(ok=False, reason='REVIEW_REQUIRED', task_id=task_id)
        if task['cleaned_at'] is None:
            db.execute('UPDATE tasks SET cleaned_at=? WHERE id=?', (store.clock(), task_id))
        db.execute('DELETE FROM read_grants WHERE task_id=?', (task_id,))
        keys = [_key(row) for row in rows]
    with control.operation_guard(ctx, terminal=True) as (_, db, _, _):
        control.require_leader(db, ctx, expected)
        cleanup_task(ctx.paths, task_id, keys)
    return _view(state='CLEANED', task_id=task_id)
