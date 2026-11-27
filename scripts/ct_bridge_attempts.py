"""Local Desktop attempt protocol. Tokens are returned only to their helper caller.

No operation launches an application or performs work on the assigned sources.
SQLite records authority; immutable UTF-8 files carry requests and final results.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import hmac
import json
import math
import secrets
import time
import uuid

from ct_bridge_files import (AttemptKey, BridgeError, cleanup_task, publish_file, read_file,
                             validate_relative_path)
from ct_bridge_store import Store
import ct_bridge_control as control
import ct_policy

TERMINAL = frozenset(('COMPLETED', 'FAILED', 'CANCELED'))
ACTIVE = frozenset(('RUNNING', 'NEEDS_USER', 'OUTCOME_UNKNOWN'))
PENDING = frozenset(('PUBLISHING', 'WAITING_FOR_USER', 'QUEUED', 'CLAIMED', 'REVOKED'))
WAITER_SECONDS = 15
CLAIM_SECONDS = 60
GRANT_SECONDS = 600
MAX_BYTES = 1048576


@dataclass(frozen=True)
class Request:
    publish_id: str
    content: bytes
    kind: str = 'READ'
    mode: str = 'manual'
    scope: tuple[str, ...] = ()
    baseline: dict = field(default_factory=dict)
    task_id: str | None = None


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                          allow_nan=False)
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
    if not isinstance(token, str) or not token or len(token) > 256:
        return ''
    return _hash(token.encode('utf-8'))


def _matches(token, digest):
    return bool(digest) and hmac.compare_digest(_token_hash(token), digest)


def _view(row=None, *, ok=True, state=None, reason=None, **extra):
    if row is not None and 'cleaned_at' in row.keys() and row['cleaned_at'] is not None:
        return {'ok':False,'state':'CLEANED','reason':'TASK_CLEANED',
                'task_id':row['task_id'],'attempt_no':row['attempt_no']}
    result = {'ok': ok, 'state': state or (row['state'] if row is not None else 'UNCONFIGURED'),
              'reason': reason}
    if row is not None:
        for name in ('task_id', 'attempt_no', 'round_id', 'leader_epoch', 'result_id',
                     'result_sha256', 'request_sha256'):
            if name in row.keys() and row[name] is not None:
                result[name] = row[name]
        if reason is None and row['reason'] is not None:
            result['reason'] = row['reason']
    result.update(extra)
    return result


def _group(db):
    return db.execute('SELECT * FROM group_state WHERE id=1').fetchone()


def _attempt(db, key):
    if not isinstance(key, AttemptKey):
        raise BridgeError('INVALID_ATTEMPT')
    # Validate keys even when there is no file yet.
    validate_relative_path(key.task_id)
    if ('/' in key.task_id or type(key.attempt_no) is not int
            or not 0 < key.attempt_no < 2**31):
        raise BridgeError('INVALID_ATTEMPT')
    return db.execute('SELECT a.*,t.round_id,t.worker_platform,t.worker_chat,t.cleaned_at, '
                      'r.closed_at FROM attempts a JOIN tasks t ON t.id=a.task_id '
                      'JOIN rounds r ON r.id=t.round_id '
                      'WHERE a.task_id=? AND a.attempt_no=?',
                      (key.task_id, key.attempt_no)).fetchone()


def _set(db, key, **values):
    db.execute('UPDATE attempts SET ' + ','.join(name + '=?' for name in values)
               + ' WHERE task_id=? AND attempt_no=?',
               (*values.values(), key.task_id, key.attempt_no))


def _stamp(binding, chat_id):
    stamp = ct_policy.policy_stamp(binding.policy_path, chat_id, require_epochs=True)
    return stamp, _json(asdict(stamp))


def _worker(ctx, row, group):
    actor = ctx.require_actor()
    return (ctx.component == 'worker' and actor.platform == row['worker_platform']
            and actor.chat_id == row['worker_chat']
            and control.matches_component(ctx, group['worker_manifest']))


def _fence(group, row, leader_binding):
    if (row['closed_at'] is not None or row['leader_epoch'] != group['leader_epoch']
            or row['pair_epoch'] != group['pair_epoch']
            or row['worker_platform'] != group['worker_platform']
            or row['worker_chat'] != group['worker_chat']):
        return 'STALE_BINDING'
    stamp, serialized = _stamp(leader_binding, group['leader_chat'])
    if stamp.effective != 'ON':
        return 'POLICY_OFF'
    if serialized != row['policy_stamp']:
        return 'STALE_POLICY'
    if not group['desktop_enabled']:
        return 'DESKTOP_OFF'
    if row['desktop_epoch'] != group['desktop_epoch']:
        return 'STALE_DESKTOP'
    if row['mode'] == 'auto':
        if not group['auto_enabled']:
            return 'AUTO_OFF'
        if row['auto_epoch'] != group['auto_epoch']:
            return 'STALE_AUTO'
    return None


def _admission(group, leader_binding, worker_binding, mode):
    if not group['worker_chat'] or worker_binding is None:
        return 'NOT_PAIRED', None
    stamp, serialized = _stamp(leader_binding, group['leader_chat'])
    if stamp.effective != 'ON':
        return 'POLICY_OFF', serialized
    if not group['desktop_enabled']:
        return 'DESKTOP_OFF', serialized
    if mode == 'auto' and not group['auto_enabled']:
        return 'AUTO_OFF', serialized
    return None, serialized


def _register(ctx, leader, worker):
    # The guard already holds both installation locks. Register before granting
    # new authority so an installer can find open rounds outside its own cwd.
    for component in (leader,worker):
        if component is not None:
            control._register_worktree(component,ctx.paths.root)


def _waiter_ready(db, group, stamp, now):
    waiter = db.execute('SELECT * FROM waiter WHERE id=1').fetchone()
    return (waiter is not None and waiter['expires_at'] > now
            and waiter['policy_stamp'] == stamp
            and all(waiter[name] == group[name] for name in
                    ('leader_epoch', 'pair_epoch', 'auto_epoch', 'desktop_epoch')))


def _request(request):
    if (not isinstance(request, Request) or not isinstance(request.publish_id, str)
            or not request.publish_id or len(request.publish_id) > 200
            or request.kind not in ('READ', 'EDIT') or request.mode not in ('manual', 'auto')
            or not isinstance(request.scope, (list, tuple)) or not isinstance(request.baseline, dict)):
        raise BridgeError('INVALID_REQUEST')
    _bytes(request.content)
    scope = [validate_relative_path(path) for path in request.scope]
    if len(set(path.casefold() for path in scope)) != len(scope):
        raise BridgeError('INVALID_SCOPE')
    if request.kind == 'EDIT' and (not scope or set(request.baseline) != set(scope)):
        raise BridgeError('BASELINE_REQUIRED')
    for path in request.baseline:
        validate_relative_path(path)
    scope_json, baseline_json = _json(scope), _json(request.baseline)
    if len(baseline_json.encode('utf-8')) > MAX_BYTES:
        raise BridgeError('INVALID_BASELINE')
    return scope_json, baseline_json


def publish(ctx, expected, round_id, request: Request):
    """Reserve once, publish immutable bytes, then commit visibility to the worker."""
    scope, baseline = _request(request)
    digest = _hash(request.content)
    with control._guard(ctx) as (store, db, leader, worker):
        group = control.require_leader(db, ctx, expected)
        round_row = db.execute('SELECT * FROM rounds WHERE id=?', (round_id,)).fetchone()
        if (round_row is None or round_row['closed_at'] is not None
                or round_row['leader_epoch'] != expected.epoch):
            return _view(ok=False, state='REJECTED', reason='ROUND_NOT_OPEN')
        _register(ctx,leader,worker)
        prior = db.execute('SELECT a.*,t.round_id,t.cleaned_at FROM attempts a JOIN tasks t ON t.id=a.task_id '
                           'WHERE publish_id=?', (request.publish_id,)).fetchone()
        if prior is not None:
            if prior['cleaned_at'] is not None:
                return _view(prior)
            if (prior['request_sha256'] != digest or prior['request_size'] != len(request.content)
                    or prior['round_id'] != round_id or prior['scope'] != scope
                    or prior['baseline'] != baseline or prior['kind'] != request.kind
                    or (request.task_id is not None and request.task_id != prior['task_id'])):
                raise BridgeError('PUBLICATION_CONFLICT')
            key = AttemptKey(prior['task_id'], prior['attempt_no'])
            if prior['state'] != 'PUBLISHING':
                read_file(ctx.paths, key, 'request', digest)
                return _view(prior)
        else:
            reason, stamp = _admission(group, leader, worker, request.mode)
            if reason:
                return _view(ok=False, state='REJECTED', reason=reason)
            if request.mode == 'auto' and not _waiter_ready(db, group, stamp, store.clock()):
                return _view(ok=False, state='REJECTED', reason='WAITER_NOT_READY')
            if request.task_id is None:
                task_id, attempt_no = 'CT-' + uuid.uuid4().hex, 1
                db.execute('INSERT INTO tasks(id,round_id,pair_epoch,worker_platform,worker_chat) '
                           'VALUES(?,?,?,?,?)', (task_id,round_id,group['pair_epoch'],
                                               group['worker_platform'],group['worker_chat']))
            else:
                validate_relative_path(request.task_id)
                task_id = request.task_id
                task = db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
                if (task is None or task['round_id'] != round_id
                        or task['pair_epoch'] != group['pair_epoch']
                        or task['worker_chat'] != group['worker_chat'] or task['cleaned_at'] is not None):
                    return _view(ok=False,state='REJECTED',reason='STALE_TASK')
                previous = db.execute('SELECT * FROM attempts WHERE task_id=? '
                                      'ORDER BY attempt_no DESC LIMIT 1',(task_id,)).fetchone()
                if previous and (previous['state'] not in TERMINAL
                                 or (previous['started_at'] is not None and previous['ack_at'] is None)):
                    return _view(previous,ok=False,reason='REVIEW_REQUIRED')
                attempt_no = previous['attempt_no'] + 1 if previous else 1
            key = AttemptKey(task_id,attempt_no)
            db.execute('INSERT INTO attempts(task_id,attempt_no,publish_id,state,mode,kind,'
                       'request_sha256,request_size,scope,baseline,desktop_epoch,auto_epoch,'
                       'pair_epoch,leader_epoch,policy_stamp,created_at) '
                       'VALUES(?,?,?,\'PUBLISHING\',?,?,?,?,?,?,?,?,?,?,?,?)',
                       (key.task_id,key.attempt_no,request.publish_id,request.mode,request.kind,
                        digest,len(request.content),scope,baseline,group['desktop_epoch'],
                        group['auto_epoch'],group['pair_epoch'],group['leader_epoch'],stamp,store.clock()))
            if request.mode == 'auto':
                waiter = db.execute('SELECT nonce FROM waiter WHERE id=1').fetchone()
                _set(db,key,claim_hash=_token_hash(waiter['nonce']))
    # Reservation survives a crash here. A retry must use the same publication and bytes.
    ref = publish_file(ctx.paths,key,'request',request.content)
    with control._guard(ctx) as (store,db,leader,worker):
        group = control.require_leader(db,ctx,expected)
        row = _attempt(db,key)
        if row['cleaned_at'] is not None:
            return _view(row)
        if row['state'] != 'PUBLISHING':
            return _view(row)
        reason = _fence(group,row,leader)
        if reason:
            _set(db,key,state='REVOKED',reason=reason,claim_hash=None,claim_expires_at=None)
            return _view(_attempt(db,key),ok=False,reason=reason)
        read_file(ctx.paths,key,'request',row['request_sha256'])
        _set(db,key,state='QUEUED' if row['mode']=='auto' else 'WAITING_FOR_USER',reason=None)
        return _view(_attempt(db,key),request_path=str(ctx.paths.data / ref.relative_path))


_WAITER_EPOCHS = ('leader_epoch', 'pair_epoch', 'auto_epoch', 'desktop_epoch')
_HEARTBEAT_SECONDS = 4
_IDLE_INTERVAL = .25


def _scope(group, stamp):
    return tuple(group[name] for name in _WAITER_EPOCHS) + (stamp,)


def _next_delivery(db, now):
    busy = db.execute(
        "SELECT 1 FROM attempts WHERE state IN ('RUNNING','NEEDS_USER','OUTCOME_UNKNOWN') "
        "OR (state='CLAIMED' AND claim_expires_at>?) LIMIT 1", (now,)
    ).fetchone()
    return not busy and db.execute("SELECT 1 FROM attempts WHERE state='QUEUED' LIMIT 1").fetchone()


def _claim_next(ctx, db, leader, group, nonce, now):
    if not _next_delivery(db, now):
        return None
    db.execute("UPDATE attempts SET state='REVOKED',reason='CLAIM_EXPIRED',claim_hash=NULL,"
               "claim_expires_at=NULL WHERE state='CLAIMED' AND claim_expires_at<=?", (now,))
    rows = db.execute("SELECT task_id,attempt_no FROM attempts WHERE state='QUEUED' "
                      "ORDER BY created_at,task_id,attempt_no").fetchall()
    for candidate in rows:
        key = AttemptKey(candidate['task_id'], candidate['attempt_no'])
        row = _attempt(db, key)
        reason = _fence(group, row, leader)
        if reason is None and not _matches(nonce, row['claim_hash']):
            reason = 'WAITER_LOST'
        if reason:
            _set(db, key, state='REVOKED', reason=reason, claim_hash=None, claim_expires_at=None)
            continue
        read_file(ctx.paths, key, 'request', row['request_sha256'])
        claim = secrets.token_urlsafe(32)
        _set(db, key, state='CLAIMED', claim_hash=_token_hash(claim), claim_expires_at=now + CLAIM_SECONDS)
        # This is a healthy handoff, so the next waiter uses the same nonce.
        db.execute('UPDATE waiter SET expires_at=0 WHERE id=1 AND nonce=?', (nonce,))
        return _view(_attempt(db, key), claim=claim)
    return None


def listen(ctx):
    """Wait in the paired chat's tool, polling only read-only SQLite while idle."""
    nonce = secrets.token_urlsafe(32)
    initial = None
    delivered = False
    store = Store(ctx.paths)
    try:
        while True:
            with control._guard(ctx) as (writer, db, leader, worker):
                group = _group(db)
                if not _worker(ctx, group, group):
                    return _view(ok=False, state='STOPPED', reason='WRONG_WORKER')
                reason, stamp = _admission(group, leader, worker, 'auto')
                if reason:
                    return _view(ok=False, state='STOPPED', reason=reason)
                signature = _scope(group, stamp)
                now = writer.clock()
                current = db.execute('SELECT * FROM waiter WHERE id=1').fetchone()
                if initial is None:
                    if current is not None and current['expires_at'] > now:
                        return _view(ok=False, state='STOPPED', reason='WAITER_BUSY')
                    if (current is not None and current['expires_at'] == 0
                            and _scope(current, current['policy_stamp']) == signature):
                        nonce = current['nonce']
                    _register(ctx, leader, worker)
                elif (signature != initial or current is None or current['nonce'] != nonce
                      or current['expires_at'] <= now):
                    return _view(ok=False, state='STOPPED', reason='WAITER_INVALIDATED')
                db.execute('INSERT INTO waiter(id,nonce,leader_epoch,pair_epoch,auto_epoch,'
                           'desktop_epoch,policy_stamp,expires_at) VALUES(1,?,?,?,?,?,?,?) '
                           'ON CONFLICT(id) DO UPDATE SET nonce=excluded.nonce,'
                           'leader_epoch=excluded.leader_epoch,pair_epoch=excluded.pair_epoch,'
                           'auto_epoch=excluded.auto_epoch,desktop_epoch=excluded.desktop_epoch,'
                           'policy_stamp=excluded.policy_stamp,expires_at=excluded.expires_at',
                           (nonce, *signature, now + WAITER_SECONDS))
                initial = signature
                event = _claim_next(ctx, db, leader, group, nonce, now)
            if event is not None:
                delivered = True
                return event

            heartbeat = time.monotonic() + _HEARTBEAT_SECONDS
            while time.monotonic() < heartbeat:
                with store.poll_read() as db:
                    current = db.execute('SELECT * FROM waiter WHERE id=1').fetchone() if db is not None else None
                    group = _group(db) if db is not None else None
                    now = store.clock()
                    if (current is None or group is None or current['nonce'] != nonce
                            or current['expires_at'] <= now or _scope(group, stamp) != initial):
                        return _view(ok=False, state='STOPPED', reason='WAITER_INVALIDATED')
                    if _next_delivery(db, now):
                        break
                time.sleep(_IDLE_INTERVAL)
    finally:
        # A successful claim leaves a consumed row for its successor, even if
        # the next helper registers before this process has returned to the app.
        if initial is not None and not delivered:
            try:
                with store.poll_read() as db:
                    owned = db is not None and db.execute('SELECT 1 FROM waiter WHERE nonce=? '
                                                          'AND expires_at>0', (nonce,)).fetchone()
                if owned:
                    with store.write() as db:
                        db.execute('DELETE FROM waiter WHERE nonce=? AND expires_at>0', (nonce,))
            except (OSError, ValueError):
                # A failed cleanup expires via the persisted deadline.
                pass


def begin(ctx, key: AttemptKey, claim: str | None):
    with control._guard(ctx) as (store,db,leader,worker):
        row = _attempt(db,key)
        if row is not None and row['cleaned_at'] is not None:
            return _view(row)
        if row is None or not _worker(ctx, row, _group(db)):
            return _view(row,ok=False,reason='WRONG_WORKER')
        if row['state'] not in ('WAITING_FOR_USER','CLAIMED'):
            return _view(row,ok=False,reason='NOT_STARTABLE')
        reason = _fence(_group(db),row,leader)
        if reason:
            _set(db,key,state='REVOKED',reason=reason,claim_hash=None,claim_expires_at=None)
            return _view(_attempt(db,key),ok=False,reason=reason)
        if row['mode'] == 'auto':
            if not _matches(claim,row['claim_hash']):
                return _view(row,ok=False,reason='INVALID_CLAIM')
            if row['claim_expires_at'] is None or row['claim_expires_at'] <= store.clock():
                _set(db,key,state='REVOKED',reason='CLAIM_EXPIRED',claim_hash=None,claim_expires_at=None)
                return _view(_attempt(db,key),ok=False,reason='CLAIM_EXPIRED')
        elif claim is not None:
            return _view(row,ok=False,reason='UNEXPECTED_CLAIM')
        if db.execute("SELECT 1 FROM attempts WHERE state IN "
                      "('RUNNING','NEEDS_USER','OUTCOME_UNKNOWN')").fetchone():
            return _view(row,ok=False,reason='SLOT_BUSY')
        _register(ctx,leader,worker)
        payload = read_file(ctx.paths,key,'request',row['request_sha256'])
        ticket = secrets.token_urlsafe(32)
        _set(db,key,state='RUNNING',started_at=store.clock(),execution_hash=_token_hash(ticket),
             claim_hash=None,claim_expires_at=None,reason=None)
        return _view(_attempt(db,key),execution_ticket=ticket,request=payload.decode('utf-8'),
                     scope=json.loads(row['scope']),baseline=json.loads(row['baseline']),kind=row['kind'])


def reoffer(ctx, expected, key: AttemptKey, mode):
    if mode not in ('manual','auto'):
        raise BridgeError('INVALID_MODE')
    with control._guard(ctx) as (store,db,leader,worker):
        group = control.require_leader(db,ctx,expected)
        row = _attempt(db,key)
        if row is not None and row['cleaned_at'] is not None:
            return _view(row)
        if row is None or row['state'] not in PENDING or row['started_at'] is not None:
            return _view(row,ok=False,reason='NOT_PENDING')
        if row['closed_at'] is not None or row['pair_epoch'] != group['pair_epoch']:
            return _view(row,ok=False,reason='STALE_BINDING')
        if row['mode'] == 'manual' and mode == 'auto':
            return _view(row,ok=False,reason='MANUAL_DRAFT')
        reason,stamp = _admission(group,leader,worker,mode)
        if reason:
            return _view(row,ok=False,reason=reason)
        if mode == 'auto' and not _waiter_ready(db,group,stamp,store.clock()):
            return _view(row,ok=False,reason='WAITER_NOT_READY')
        read_file(ctx.paths,key,'request',row['request_sha256'])
        _set(db,key,mode=mode,state='QUEUED' if mode=='auto' else 'WAITING_FOR_USER',
             desktop_epoch=group['desktop_epoch'],auto_epoch=group['auto_epoch'],
             leader_epoch=group['leader_epoch'],policy_stamp=stamp,reason=None,
             claim_hash=None,claim_expires_at=None)
        if mode == 'auto':
            waiter = db.execute('SELECT nonce FROM waiter WHERE id=1').fetchone()
            _set(db,key,claim_hash=_token_hash(waiter['nonce']))
        return _view(_attempt(db,key))


def finish(ctx, key: AttemptKey, execution_ticket, state, result: bytes):
    if state not in TERMINAL | {'NEEDS_USER','OUTCOME_UNKNOWN'}:
        raise BridgeError('INVALID_FINISH_STATE')
    _bytes(result)
    with control._guard(ctx,terminal=True) as (store,db,leader,worker):
        row = _attempt(db,key)
        if row is not None and row['cleaned_at'] is not None:
            return _view(row)
        if row is None or not _worker(ctx, row, _group(db)) or not _matches(execution_ticket,row['execution_hash']):
            return _view(row,ok=False,reason='INVALID_EXECUTION')
        if row['state'] in TERMINAL:
            if row['state'] != state or row['result_sha256'] != _hash(result):
                raise BridgeError('RESULT_CONFLICT')
            read_file(ctx.paths,key,'result',row['result_sha256'])
            return _view(row)
        if row['state'] not in ACTIVE:
            return _view(row,ok=False,reason='NOT_RUNNING')
        if state not in TERMINAL:
            _set(db,key,state=state,reason=result.decode('utf-8'))
            return _view(_attempt(db,key))
        ref = publish_file(ctx.paths,key,'result',result)
        _set(db,key,state=state,result_id='result-'+uuid.uuid4().hex,
             result_sha256=ref.sha256,result_size=ref.size,finished_at=store.clock(),reason=None)
        return _view(_attempt(db,key),result_path=str(ctx.paths.data/ref.relative_path))


def resume(ctx, key: AttemptKey, execution_ticket):
    with control._guard(ctx,terminal=True) as (store,db,leader,worker):
        row = _attempt(db,key)
        if row is not None and row['cleaned_at'] is not None:
            return _view(row)
        if row is None or not _worker(ctx, row, _group(db)) or not _matches(execution_ticket,row['execution_hash']):
            return _view(row,ok=False,reason='INVALID_EXECUTION')
        if row['state'] not in ('NEEDS_USER','OUTCOME_UNKNOWN'):
            return _view(row,ok=False,reason='NOT_RESUMABLE')
        # A final file without a committed receipt must be recovered by finish, not replayed work.
        result_path = ctx.paths.data/key.task_id/('attempt-'+str(key.attempt_no))/'result.md'
        if result_path.exists():
            return _view(row,ok=False,reason='RESULT_RECONCILIATION_REQUIRED')
        payload = read_file(ctx.paths,key,'request',row['request_sha256'])
        _set(db,key,state='RUNNING',reason=None)
        return _view(_attempt(db,key),request=payload.decode('utf-8'),
                     scope=json.loads(row['scope']),baseline=json.loads(row['baseline']),kind=row['kind'])


def cancel_pending(ctx, expected, key: AttemptKey):
    with control._guard(ctx,terminal=True) as (store,db,leader,worker):
        control.require_leader(db,ctx,expected)
        row = _attempt(db,key)
        if row is not None and row['cleaned_at'] is not None:
            return _view(row)
        if row is None or row['started_at'] is not None or row['state'] not in PENDING | {'CANCELED'}:
            return _view(row,ok=False,reason='STARTED_REQUIRES_RECONCILIATION')
        if row['state'] == 'CANCELED':
            return _view(row)
        _set(db,key,state='CANCELED',finished_at=store.clock(),claim_hash=None,
             claim_expires_at=None,reason='CANCELED_BEFORE_BEGIN')
        return _view(_attempt(db,key))


def reconcile(ctx, expected, key: AttemptKey, evidence):
    valid = (isinstance(evidence,dict) and evidence.get('task_id')==key.task_id
             and evidence.get('attempt_no')==key.attempt_no
             and evidence.get('confirmed_by') in ('worker','user')
             and evidence.get('stopped') is True and isinstance(evidence.get('summary'),str)
             and bool(evidence['summary'].strip()))
    if not valid:
        return _view(ok=False,state='REJECTED',reason='SCOPED_CONFIRMATION_REQUIRED')
    encoded = _json(evidence)
    _bytes(encoded.encode('utf-8'))
    with control._guard(ctx,terminal=True) as (store,db,leader,worker):
        group = control.require_leader(db,ctx,expected)
        row = _attempt(db,key)
        if row is not None and row['cleaned_at'] is not None:
            return _view(row)
        if row is None or row['leader_epoch'] != group['leader_epoch'] or row['closed_at'] is not None:
            return _view(row,ok=False,reason='STALE_ATTEMPT')
        if row['state'] in TERMINAL:
            if row['evidence'] == encoded and row['result_id']:
                read_file(ctx.paths,key,'result',row['result_sha256'])
                return _view(row)
            return _view(row,ok=False,reason='ALREADY_FINISHED')
        if row['state'] not in ACTIVE:
            return _view(row,ok=False,reason='NOT_RUNNING')
        result_path = ctx.paths.data/key.task_id/('attempt-'+str(key.attempt_no))/'result.md'
        if result_path.exists():
            state = evidence.get('state')
            digest = evidence.get('result_sha256')
            if state not in TERMINAL or not isinstance(digest,str):
                return _view(row,ok=False,reason='RESULT_RECONCILIATION_REQUIRED')
            payload = read_file(ctx.paths,key,'result',digest)
            result_digest,result_size = digest,len(payload)
        else:
            state = 'CANCELED'
            ref = publish_file(ctx.paths,key,'result',encoded.encode('utf-8'))
            result_digest,result_size = ref.sha256,ref.size
        _set(db,key,state=state,finished_at=store.clock(),evidence=encoded,
             result_id='result-'+uuid.uuid4().hex,result_sha256=result_digest,result_size=result_size,
             reason='CONFIRMED_STOPPED',execution_hash=None,claim_hash=None,claim_expires_at=None)
        return _view(_attempt(db,key))


def grant_read(ctx, expected, key: AttemptKey):
    with control._guard(ctx,terminal=True) as (store,db,leader,worker):
        control.require_leader(db,ctx,expected)
        row = _attempt(db,key)
        if row is not None and row['cleaned_at'] is not None:
            return _view(row)
        if row is None:
            return _view(ok=False,reason='ATTEMPT_NOT_FOUND')
        token = secrets.token_urlsafe(32)
        db.execute('INSERT INTO read_grants(task_id,attempt_no,token_hash,expires_at) VALUES(?,?,?,?) '
                   'ON CONFLICT(task_id,attempt_no) DO UPDATE SET token_hash=excluded.token_hash,'
                   'expires_at=excluded.expires_at',
                   (key.task_id,key.attempt_no,_token_hash(token),store.clock()+GRANT_SECONDS))
        return _view(row,grant=token,expires_at=store.clock()+GRANT_SECONDS)


def collect(ctx, key: AttemptKey, grant: str | None, *, wait_seconds=0):
    """Read-only receipt collection. Expiry or timeout never changes writer ownership."""
    if (isinstance(wait_seconds,bool) or not isinstance(wait_seconds,(int,float))
            or not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= 60):
        raise BridgeError('INVALID_WAIT')
    deadline = time.monotonic()+wait_seconds
    store = Store(ctx.paths)
    snapshot = store.read
    while True:
        with snapshot() as db:
            if db is None:
                return _view(ok=False,reason='ATTEMPT_NOT_FOUND')
            row = _attempt(db,key)
            if row is None:
                return _view(ok=False,reason='ATTEMPT_NOT_FOUND')
            if row['cleaned_at'] is not None:
                return _view(row)
            if grant is None:
                control.require_leader(db,ctx,control.context_leader(db))
            else:
                permission = db.execute('SELECT * FROM read_grants WHERE task_id=? AND attempt_no=?',
                                        (key.task_id,key.attempt_no)).fetchone()
                if (permission is None or permission['expires_at'] <= store.clock()
                        or not _matches(grant,permission['token_hash'])):
                    return _view(row,ok=False,reason='INVALID_READ_GRANT')
            if row['result_id'] is not None:
                payload = read_file(ctx.paths,key,'result',row['result_sha256'])
                return _view(row,result=payload.decode('utf-8'),result_size=len(payload),
                             result_path=str(ctx.paths.data/key.task_id/
                                             ('attempt-'+str(key.attempt_no))/'result.md'))
            view = _view(row)
        if time.monotonic() >= deadline or row['state'] in TERMINAL:
            return view
        snapshot = store.poll_read
        time.sleep(min(_IDLE_INTERVAL, max(0, deadline - time.monotonic())))


def ack(ctx, expected, key: AttemptKey, result_id):
    with control._guard(ctx,terminal=True) as (store,db,leader,worker):
        control.require_leader(db,ctx,expected)
        row = _attempt(db,key)
        if row is not None and row['cleaned_at'] is not None:
            return _view(row)
        if row is None or row['state'] not in TERMINAL or row['result_id'] != result_id or not result_id:
            return _view(row,ok=False,reason='RECEIPT_MISMATCH')
        payload = read_file(ctx.paths,key,'result',row['result_sha256'])
        if len(payload) != row['result_size']:
            raise BridgeError('RESULT_CONFLICT')
        if row['ack_at'] is None:
            _set(db,key,ack_at=store.clock())
        return _view(_attempt(db,key),acknowledged=True)


def cleanup(ctx, expected, task_id):
    """Explicitly remove only a closed task's transport files after durable revocation."""
    validate_relative_path(task_id)
    if '/' in task_id:
        raise BridgeError('INVALID_ATTEMPT')
    with control._guard(ctx,terminal=True) as (store,db,leader,worker):
        control.require_leader(db,ctx,expected)
        task = db.execute('SELECT t.*,r.closed_at FROM tasks t JOIN rounds r ON r.id=t.round_id '
                          'WHERE t.id=?',(task_id,)).fetchone()
        if task is None:
            return _view(ok=False,state='REJECTED',reason='TASK_NOT_FOUND')
        rows = db.execute('SELECT * FROM attempts WHERE task_id=? ORDER BY attempt_no',(task_id,)).fetchall()
        if task['closed_at'] is None or any(
                row['state'] not in TERMINAL | {'REVOKED'}
                or (row['started_at'] is not None and row['ack_at'] is None) for row in rows):
            return _view(ok=False,state='REJECTED',reason='REVIEW_REQUIRED',task_id=task_id)
        if task['cleaned_at'] is None:
            db.execute('UPDATE tasks SET cleaned_at=? WHERE id=?',(store.clock(),task_id))
        db.execute('DELETE FROM read_grants WHERE task_id=?',(task_id,))
        keys = [AttemptKey(task_id,row['attempt_no']) for row in rows]
    # The tombstone is committed first. Readers cannot return content even if a
    # crash leaves some files behind, and retry uses the same exact owned keys.
    with control._guard(ctx,terminal=True) as (_,db,_,_):
        control.require_leader(db,ctx,expected)
        cleanup_task(ctx.paths,task_id,keys)
    return _view(state='CLEANED',task_id=task_id)
