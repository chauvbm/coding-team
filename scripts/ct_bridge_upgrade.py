"""Versioned recovery for the managed Desktop bridge, under component locks."""
from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import json
import os
import re
import sqlite3
import uuid

from ct_bridge_files import (AttemptKey, BridgeError, _target, cleanup_task, publish_file,
                             recovery_guard, recovery_pending, verify_layout)
from ct_bridge_store import (APPLICATION_ID, PROTOCOL_VERSION, SCHEMA_V1, SCHEMA_V2,
                             Store, inspect_store)


def _encode(data):
    return json.dumps(data, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _fingerprint(db):
    digest = sha256()
    for statement in db.iterdump():
        digest.update(statement.encode('utf-8') + b'\n')
    return digest.hexdigest()


def _digest(path, guard):
    if not guard.plain(path):
        raise BridgeError('RECOVERY_BACKUP_MISSING')
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        digest = sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise BridgeError('RECOVERY_BACKUP_CHANGED')
    return digest.hexdigest()


def _check_database(db, *, version):
    if (db.execute('PRAGMA user_version').fetchone()[0] != version
            or db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID
            or db.execute('PRAGMA quick_check').fetchall()[0][0] != 'ok'
            or db.execute('PRAGMA foreign_key_check').fetchone() is not None):
        raise BridgeError('RECOVERY_INVALID_DATABASE')


def _check_idle(db):
    if db.execute('SELECT 1 FROM rounds WHERE closed_at IS NULL LIMIT 1').fetchone():
        raise BridgeError('ROUND_BUSY')
    if db.execute("SELECT 1 FROM attempts WHERE state NOT IN ('COMPLETED','FAILED','CANCELED','REVOKED') "
                  "OR (result_id IS NOT NULL AND ack_at IS NULL) LIMIT 1").fetchone():
        raise BridgeError('UNRESOLVED_ATTEMPT')


def _artifact_rows(db):
    return db.execute('''SELECT a.task_id,a.attempt_no,a.state,a.request_sha256,a.request_size,
        a.result_sha256,a.result_size,a.started_at,t.cleaned_at FROM attempts a JOIN tasks t ON t.id=a.task_id
        ORDER BY a.task_id,a.attempt_no''').fetchall()


def _backup_files(paths, db, guard, destination):
    records = []
    guard.directory(destination, create=True)
    for row in _artifact_rows(db):
        key = AttemptKey(row['task_id'], row['attempt_no'])
        for kind in ('request', 'result'):
            path = _target(paths, key, kind)
            digest, size = row[kind + '_sha256'], row[kind + '_size']
            if not guard.plain(path):
                canceled_before_start = (kind == 'request' and row['started_at'] is None
                                         and row['state'] in ('CANCELED', 'REVOKED'))
                if row['cleaned_at'] is None and digest is not None and not canceled_before_start:
                    raise BridgeError('RECOVERY_ARTIFACT_MISSING')
                continue
            content = guard.read(path)
            actual = sha256(content).hexdigest()
            if digest is not None and (digest != actual or size != len(content)):
                raise BridgeError('RECOVERY_ARTIFACT_CHANGED')
            relative = path.relative_to(paths.data)
            guard.directory(destination / key.task_id, create=True)
            guard.directory((destination / relative).parent, create=True)
            guard.publish(destination / relative, content)
            records.append({'task_id': key.task_id, 'attempt_no': key.attempt_no,
                            'kind': kind, 'sha256': actual, 'size': len(content)})
    return records


def _save_database(paths, guard, destination, version):
    # Separate read connection sees the pre-migration snapshot while the caller
    # holds a reserved writer lock. No DDL runs until this backup closes.
    temporary = destination.with_name('.snapshot-' + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(paths.db.as_uri() + '?mode=ro', uri=True)) as source:
            with closing(sqlite3.connect(temporary)) as target:
                source.backup(target)
                _check_database(target, version=version)
        with temporary.open('rb+') as stream:
            os.fsync(stream.fileno())
        if guard.plain(destination):
            # The manifest is published last. A prior interrupted capture may
            # leave a snapshot of the same idle generation.
            if _digest(destination, guard) != _digest(temporary, guard):
                raise BridgeError('RECOVERY_BACKUP_CHANGED')
        else:
            os.rename(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _read_archive(guard, folder, version):
    try:
        data = json.loads(guard.read(folder / 'manifest.json', limit=16 * 1024 * 1024))
        if (data['version'] != version or not isinstance(data['files'], list)
                or _digest(folder / 'bridge.sqlite3', guard) != data['database_sha256']):
            raise BridgeError('RECOVERY_BACKUP_CHANGED')
        return data
    except (TypeError, KeyError, UnicodeError, json.JSONDecodeError) as exc:
        raise BridgeError('RECOVERY_INVALID_MANIFEST') from exc


def _archive(paths, db, guard, root, version):
    source_fingerprint = _fingerprint(db)
    # v1 can accrue new legitimate history after an interrupted migration.
    # Name each baseline independently, then bind its name in the v2 transaction.
    folder = root / ('v1-' + source_fingerprint if version == 1 else 'v2')
    guard.directory(folder, create=True)
    if guard.plain(folder / 'manifest.json'):
        data = _read_archive(guard, folder, version)
        if data.get('source_fingerprint') != source_fingerprint:
            raise BridgeError('RECOVERY_STALE_ARCHIVE')
        _read_artifacts(paths, guard, folder, data)
        return folder
    records = _backup_files(paths, db, guard, folder / 'files')
    _save_database(paths, guard, folder / 'bridge.sqlite3', version)
    data = {'version': version, 'database_sha256': _digest(folder / 'bridge.sqlite3', guard),
            'source_fingerprint': source_fingerprint, 'files': records}
    guard.publish(folder / 'manifest.json', _encode(data))
    return folder


def _rename_v1(db):
    for name in ('open_rounds', 'running_slot', 'delivery_queue'):
        db.execute(f'DROP INDEX {name}')
    for name in ('group_state', 'rounds', 'native_participants', 'tasks', 'attempts', 'read_grants', 'waiter'):
        db.execute(f'ALTER TABLE {name} RENAME TO old_{name}')
    for statement in SCHEMA_V2:
        db.execute(statement)


def _port_history(db, group, *, now, active_worker):
    db.execute('INSERT INTO rounds SELECT * FROM old_rounds')
    db.execute('''INSERT INTO participants(round_id,participant_id,executor,handle,finished_at,evidence)
        SELECT round_id,id,'native',handle,finished_at,evidence FROM old_native_participants''')
    historical = {}
    active_key = (group['worker_platform'], group['worker_chat'], group['leader_epoch'], group['pair_epoch'])
    if active_worker:
        historical[active_key] = active_worker
    tasks = db.execute('''SELECT t.*,r.leader_epoch,r.closed_at FROM old_tasks t
                       JOIN old_rounds r ON r.id=t.round_id ORDER BY t.id''').fetchall()
    for task in tasks:
        key = tuple(task[name] for name in ('worker_platform', 'worker_chat', 'leader_epoch', 'pair_epoch'))
        if key not in historical:
            platform, chat, epoch, pair = key
            worker_id = uuid.uuid4().hex
            db.execute('INSERT OR IGNORE INTO peers(platform) VALUES (?)', (platform,))
            db.execute('''INSERT INTO workers(worker_id,platform,chat_id,leader_epoch,registered_at,retired_at,legacy_pair_epoch)
                VALUES (?,?,?,?,?,?,?)''', (worker_id, platform, chat, epoch, now, now, pair))
            historical[key] = worker_id
        worker_id = historical[key]
        participant_id = 'legacy-' + uuid.uuid4().hex
        db.execute('''INSERT INTO participants(round_id,participant_id,work_item_id,executor,worker_id,finished_at)
            VALUES (?,?,?,'desktop',?,?)''',
            (task['round_id'], participant_id, task['id'], worker_id, task['closed_at']))
        db.execute('''INSERT INTO work_items(round_id,work_item_id,owner_participant_id,state)
            VALUES (?,?,?,'historical')''', (task['round_id'], task['id'], participant_id))
        db.execute('''INSERT INTO tasks(id,round_id,participant_id,worker_id,cleaned_at,legacy_pair_epoch)
            VALUES (?,?,?,?,?,?)''', (task['id'], task['round_id'], participant_id,
                                     worker_id, task['cleaned_at'], task['pair_epoch']))
    db.execute('''INSERT INTO attempts(task_id,attempt_no,worker_id,publish_id,state,mode,kind,request_sha256,
        request_size,scope,baseline,desktop_epoch,auto_epoch,legacy_pair_epoch,leader_epoch,policy_stamp,
        created_at,started_at,finished_at,result_id,result_sha256,result_size,reason,ack_at,evidence)
        SELECT a.task_id,a.attempt_no,t.worker_id,a.publish_id,a.state,a.mode,a.kind,a.request_sha256,
        a.request_size,a.scope,a.baseline,a.desktop_epoch,a.auto_epoch,a.pair_epoch,a.leader_epoch,a.policy_stamp,
        a.created_at,a.started_at,a.finished_at,a.result_id,a.result_sha256,a.result_size,a.reason,a.ack_at,a.evidence
        FROM old_attempts a JOIN tasks t ON t.id=a.task_id''')


def _check_migration(db):
    columns = 'task_id,attempt_no,publish_id,state,request_sha256,result_id,result_sha256,ack_at'
    if (db.execute('SELECT count(*) FROM old_attempts').fetchone()[0] !=
            db.execute('SELECT count(*) FROM attempts').fetchone()[0]
            or db.execute(f'SELECT {columns} FROM old_attempts EXCEPT SELECT {columns} FROM attempts').fetchone()):
        raise BridgeError('RECOVERY_HISTORY_MISMATCH')
    for name in ('read_grants', 'waiter', 'attempts', 'tasks', 'native_participants', 'rounds', 'group_state'):
        db.execute(f'DROP TABLE old_{name}')
    db.execute(f'PRAGMA user_version={PROTOCOL_VERSION}')
    _check_database(db, version=PROTOCOL_VERSION)


def migrate_v1(paths, *, clock, leader_manifest=None, worker_manifest=None):
    if recovery_pending(paths):
        raise BridgeError('RECOVERY_IN_PROGRESS')
    with Store(paths, clock=clock).write_compatible() as db:
        if db is None:
            return {'ok': True, 'state': 'EMPTY', 'version': None}
        if db.execute('PRAGMA user_version').fetchone()[0] == PROTOCOL_VERSION:
            return {'ok': True, 'state': 'ALREADY_V2', 'version': PROTOCOL_VERSION}
        _check_database(db, version=1)
        _check_idle(db)
        group = dict(db.execute('SELECT * FROM group_state WHERE id=1').fetchone())
        with recovery_guard(paths, create=True) as (guard, root):
            archive = _archive(paths, db, guard, root, 1)
        _rename_v1(db)
        db.execute('''INSERT INTO group_state(id,leader_platform,leader_chat,leader_manifest,leader_epoch,baseline_archive)
            VALUES (1,?,?,?,?,?)''', (*tuple(group[name] for name in
            ('leader_platform', 'leader_chat', 'leader_manifest', 'leader_epoch')), archive.name))
        db.execute('INSERT INTO peers(platform,enabled,desktop_epoch,auto_enabled,auto_epoch) VALUES (?,?,?,?,?)',
                   ('antigravity', *(group[name] for name in ('desktop_enabled', 'desktop_epoch', 'auto_enabled', 'auto_epoch'))))
        reopen = bool(group['worker_chat'] and group['worker_platform'] == 'antigravity'
                      and group['desktop_enabled'] and leader_manifest and worker_manifest
                      and group['leader_manifest'] == leader_manifest and group['worker_manifest'] == worker_manifest)
        now = clock()
        worker_id = uuid.uuid4().hex if reopen else None
        if worker_id:
            db.execute('''INSERT INTO workers(worker_id,platform,chat_id,manifest,leader_epoch,registered_at,legacy_pair_epoch)
                VALUES (?,'antigravity',?,?,?,?,?)''', (worker_id, group['worker_chat'], worker_manifest,
                                                      group['leader_epoch'], now, group['pair_epoch']))
        _port_history(db, group, now=now, active_worker=worker_id)
        _check_migration(db)
        return {'ok': True, 'state': 'MIGRATED', 'version': PROTOCOL_VERSION,
                'needs_repair': bool(group['worker_chat'] and not reopen)}


def _read_artifacts(paths, guard, root, archive):
    result = {}
    for entry in archive['files']:
        try:
            key = AttemptKey(entry['task_id'], entry['attempt_no'])
            kind = entry['kind']
            relative = _target(paths, key, kind).relative_to(paths.data)
            content = guard.read(root / 'files' / relative)
            if len(content) != entry['size'] or sha256(content).hexdigest() != entry['sha256']:
                raise BridgeError('RECOVERY_ARTIFACT_CHANGED')
        except (TypeError, KeyError, FileNotFoundError) as exc:
            raise BridgeError('RECOVERY_INVALID_MANIFEST') from exc
        if (key, kind) in result:
            raise BridgeError('RECOVERY_INVALID_MANIFEST')
        result[key, kind] = content
    return result


def _install_database(paths, guard, baseline):
    # Pin the parent, close all SQLite handles, then switch the one DB name.
    # Stage outside the v1 transport, on the same volume.
    directory = paths.data.parent / 'bridge-recovery'
    guard.directory(paths.data)
    temporary = directory / ('.restore-' + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(temporary)) as destination:
            if baseline:
                with closing(sqlite3.connect(baseline.as_uri() + '?mode=ro', uri=True)) as source:
                    source.backup(destination)
                with destination:
                    destination.execute('DELETE FROM waiter')
                    destination.execute('DELETE FROM read_grants')
                    destination.execute('UPDATE group_state SET invite_hash=NULL,invite_expires_at=NULL')
            else:
                with destination:
                    for statement in SCHEMA_V1:
                        destination.execute(statement)
                    destination.execute('INSERT INTO group_state(id) VALUES (1)')
                    destination.execute(f'PRAGMA application_id={APPLICATION_ID}')
                    destination.execute('PRAGMA user_version=1')
            _check_database(destination, version=1)
        with temporary.open('rb+') as stream:
            os.fsync(stream.fileno())
        if not guard.plain(paths.db):
            raise BridgeError('RECOVERY_DATABASE_MISSING')
        os.replace(temporary, paths.db)
    finally:
        temporary.unlink(missing_ok=True)


def restore_v1(paths, *, clock):
    state = inspect_store(paths)
    if state.version == 1 and state.maintenance:
        return {'ok': True, 'state': 'RESTORED_V1', 'version': 1}
    if state.version != PROTOCOL_VERSION:
        raise BridgeError('RECOVERY_NO_V2_STORE')
    verify_layout(paths, create=False)
    with recovery_guard(paths, create=True) as (guard, root):
        with Store(paths, clock=clock).write_compatible() as db:
            if db is None or db.execute('PRAGMA user_version').fetchone()[0] != PROTOCOL_VERSION:
                raise BridgeError('RECOVERY_NO_V2_STORE')
            _check_idle(db)
            baseline_name = db.execute('SELECT baseline_archive FROM group_state WHERE id=1').fetchone()[0]
            marker = _encode({'from': PROTOCOL_VERSION, 'to': 1, 'source_fingerprint': _fingerprint(db)})
            marker_path = root / 'rollback.json'
            if not guard.plain(marker_path):
                guard.publish(marker_path, marker)
                if os.name != 'nt':
                    os.fsync(guard.directory(root))
            elif guard.read(marker_path) != marker:
                raise BridgeError('RECOVERY_STALE_ARCHIVE')
            folder = _archive(paths, db, guard, root, PROTOCOL_VERSION)
        v2 = _read_archive(guard, folder, PROTOCOL_VERSION)
        archived = _read_artifacts(paths, guard, folder, v2)
        baseline, original = None, {}
        if baseline_name is not None:
            if re.fullmatch(r'v1-[a-f0-9]{64}', baseline_name) is None:
                raise BridgeError('RECOVERY_INVALID_MANIFEST')
            baseline_folder = root / baseline_name
            v1 = _read_archive(guard, baseline_folder, 1)
            if v1.get('source_fingerprint') != baseline_name[3:]:
                raise BridgeError('RECOVERY_STALE_ARCHIVE')
            baseline = baseline_folder / 'bridge.sqlite3'
            original = _read_artifacts(paths, guard, baseline_folder, v1)
        for (key, kind), content in {**archived, **original}.items():
            path = _target(paths, key, kind)
            if guard.plain(path) and guard.read(path) != content:
                raise BridgeError('RECOVERY_ARTIFACT_CHANGED')
        # Only remove entire attempt pairs absent from v1, after the archived
        # bytes have been verified. Existing Markdown remains immutable.
        original_keys = {key for key, _ in original}
        for key in sorted({key for key, _ in archived} - original_keys, key=lambda item: (item.task_id, item.attempt_no)):
            cleanup_task(paths, key.task_id, (key,))
        for (key, kind), content in original.items():
            publish_file(paths, key, kind, content)
        _install_database(paths, guard, baseline)
    return {'ok': True, 'state': 'RESTORED_V1', 'version': 1}
