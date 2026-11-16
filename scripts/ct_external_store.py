"""Attempt, admission and result state for the managed external worker."""
from __future__ import annotations

from bisect import bisect_left
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, is_dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
from typing import TYPE_CHECKING

import ct_policy
import ct_storage

if TYPE_CHECKING:
    from ct_winjob import ProcessIdentity


TERMINAL = frozenset({'COMPLETED', 'FAILED', 'CANCELED', 'TIMED_OUT'})
CHECK_PROBES = (
    'read', 'edit', 'deny_read_write', 'deny_outside_read', 'deny_outside_write',
    'deny_shell', 'deny_mcp', 'deny_web', 'deny_native_read', 'deny_private_read',
)
_TASK_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')
_HASH = re.compile(r'[0-9a-fA-F]{64}')
_BASE = re.compile(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}')
_CODE = re.compile(r'[A-Z][A-Z0-9_]{0,79}')
_SCHEMA = (
    """CREATE TABLE meta (
        name TEXT PRIMARY KEY, value INTEGER NOT NULL
    )""",
    """CREATE TABLE opt_ins (
        owner TEXT PRIMARY KEY, enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
        epoch INTEGER NOT NULL CHECK (epoch > 0)
    )""",
    """CREATE TABLE attempts (
        id INTEGER PRIMARY KEY, task_id TEXT NOT NULL COLLATE NOCASE,
        attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
        owner TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('CHECK', 'READ', 'EDIT')),
        request TEXT NOT NULL, request_digest TEXT NOT NULL, profile_fingerprint TEXT NOT NULL,
        policy_stamp TEXT NOT NULL, opt_epoch INTEGER NOT NULL, runtime_epoch INTEGER NOT NULL,
        state TEXT NOT NULL CHECK (state IN (
            'RESERVED', 'STARTING', 'RUNNING', 'STOPPING', 'FINALIZING', 'RECONCILE_REQUIRED',
            'COMPLETED', 'FAILED', 'CANCELED', 'TIMED_OUT'
        )), reason TEXT, stop_reason TEXT,
        reserved_at REAL NOT NULL, prepare_deadline REAL NOT NULL, startup_deadline REAL,
        started_at REAL, resumed_at REAL, process_exit_at REAL, process_exit_code INTEGER,
        stopped_at REAL, finished_at REAL, received_at REAL,
        manifest_id TEXT, runner_claim TEXT, claimed_process TEXT, launch_metadata TEXT,
        process_identity TEXT, resume_possible INTEGER NOT NULL DEFAULT 0,
        ever_resumed INTEGER NOT NULL DEFAULT 0, stop_proof TEXT,
        check_progress TEXT NOT NULL DEFAULT '[]',
        result_id TEXT, result TEXT, artifact_sha TEXT, disposition TEXT, candidate_sha TEXT,
        tested_sha TEXT, check_record TEXT, journal_id TEXT, apply_report TEXT,
        cleanup_pending INTEGER NOT NULL DEFAULT 0 CHECK (cleanup_pending IN (0, 1)),
        UNIQUE (task_id, attempt_no)
    )""",
    "CREATE INDEX attempts_by_owner ON attempts (owner, id DESC)",
    "CREATE INDEX attempts_to_clean ON attempts (id) WHERE cleanup_pending=1",
    """CREATE TABLE slot (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        attempt INTEGER NOT NULL UNIQUE REFERENCES attempts(id)
    )""",
    """CREATE TABLE checks (
        profile_fingerprint TEXT PRIMARY KEY, attempt INTEGER NOT NULL UNIQUE REFERENCES attempts(id),
        qualification TEXT
    )""",
    """CREATE TABLE edit_owners (
        workspace_key TEXT NOT NULL, path_key TEXT NOT NULL,
        attempt INTEGER NOT NULL REFERENCES attempts(id),
        PRIMARY KEY (workspace_key, path_key)
    )""",
    """CREATE TABLE read_grants (
        attempt INTEGER PRIMARY KEY REFERENCES attempts(id),
        token_hash TEXT NOT NULL, expires_at REAL NOT NULL
    )""",
)
_TABLES = frozenset({'meta', 'opt_ins', 'attempts', 'slot', 'checks', 'edit_owners', 'read_grants'})
_META_DEFAULTS = {'epochs_initialized': 0, 'maintenance': 0, 'runtime_epoch': 0}


@dataclass(frozen=True)
class Owner:
    leader_app: str
    chat_id: str
    workspace_key: str
    worker_type: str


@dataclass(frozen=True)
class AttemptKey:
    task_id: str
    attempt_no: int


@dataclass(frozen=True)
class StopProof:
    key: AttemptKey
    runner_claim: str = field(repr=False)
    identity: ProcessIdentity | None
    basis: str


@dataclass(frozen=True)
class Request:
    kind: str
    prompt: str = field(repr=False)
    base: str
    file_choices: dict[str, str]
    edit_paths: tuple[str, ...]
    timeout_s: int = 300
    spec_refs: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class AttemptView:
    key: AttemptKey
    owner: Owner
    state: str
    reason: str | None
    result_id: str | None


@dataclass(frozen=True)
class StatusView:
    owner: Owner
    opt_in: str
    effective_policy: str | None
    readiness: str
    reason: str | None
    attempts: tuple[AttemptView, ...]


@dataclass(frozen=True)
class RunSnapshot:
    view: AttemptView
    request: Request
    profile_fingerprint: str
    manifest_id: str | None
    runner_claim: str | None = field(repr=False)
    claimed_process: dict | None
    launch_metadata: dict | None
    process_identity: dict | None
    resume_possible: bool
    stop_proof: dict | None
    check_progress: tuple[dict, ...]
    prepare_deadline: float
    startup_deadline: float | None
    disposition: str | None
    candidate_sha: str | None
    artifact_sha: str | None
    tested_sha: str | None
    journal_id: str | None
    opt_epoch: int
    policy_stamp: ct_policy.PolicyStamp
    reserved_at: float
    started_at: float | None
    resumed_at: float | None
    process_exit_at: float | None
    stopped_at: float | None
    finished_at: float | None
    received_at: float | None


class StoreError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _hash_text(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _text(value, limit):
    if isinstance(value, str):
        try:
            return '\x00' not in value and len(value.encode('utf-8')) <= limit
        except UnicodeError:
            pass
    return False


def _identifier(value, reason):
    if not _text(value, 256) or not value or value != value.strip() or any(ord(c) < 32 for c in value):
        raise StoreError(reason)
    return value


def _digest(value, reason):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise StoreError(reason)
    return value.lower()


def _reason(value):
    if value is not None and (not isinstance(value, str) or not _CODE.fullmatch(value)):
        raise StoreError('INVALID_REASON')
    return value


def _policy_reason(stamp):
    effective = stamp.effective if isinstance(stamp, ct_policy.PolicyStamp) else None
    if effective == 'ON':
        return None
    return 'POLICY_OFF' if effective == 'OFF' else 'POLICY_UNKNOWN'


def _key(key):
    if (not isinstance(key, AttemptKey) or not isinstance(key.task_id, str) or not _TASK_ID.fullmatch(key.task_id)
            or type(key.attempt_no) is not int or not 1 <= key.attempt_no <= 2_147_483_647):
        raise StoreError('INVALID_ATTEMPT')
    return key.task_id, key.attempt_no


def _request(request):
    if (not isinstance(request, Request) or request.kind not in ('CHECK', 'READ', 'EDIT')
            or type(request.timeout_s) is not int or not 1 <= request.timeout_s <= 1800
            or not isinstance(request.file_choices, dict)
            or not isinstance(request.edit_paths, tuple) or not isinstance(request.spec_refs, tuple)
            or not all(isinstance(ref, str) for ref in (request.prompt, *request.spec_refs))
            or not isinstance(request.base, str)):
        raise StoreError('INVALID_REQUEST')
    try:
        text_size = sum(len(value.encode('utf-8')) for value in (request.prompt, *request.spec_refs))
    except UnicodeError:
        raise StoreError('INVALID_REQUEST') from None
    if text_size > 64 * 1024:
        raise StoreError('REQUEST_TOO_LARGE')

    files = request.file_choices
    edits = request.edit_paths
    if (any(not isinstance(path, str) or source not in ('GIT', 'INDEX', 'WORKTREE')
            for path, source in files.items())
            or any(not isinstance(path, str) for path in edits)
            or len(set(edits)) != len(edits) or (request.kind == 'EDIT') != bool(edits)
            or (request.kind != 'CHECK' and not _BASE.fullmatch(request.base))
            or (request.kind == 'CHECK' and (files or request.base and not _BASE.fullmatch(request.base)))):
        raise StoreError('INVALID_REQUEST')
    paths = set(files) | set(edits)
    if len(paths) > 1024:
        raise StoreError('SCOPE_TOO_LARGE')
    if paths:
        from ct_external_workspace import WorkspaceError, validate_relative_paths

        try:
            if set(validate_relative_paths(paths)) != paths or any(not _text(path, 4096) for path in paths):
                raise StoreError('INVALID_SCOPE')
        except WorkspaceError as exc:
            raise StoreError(exc.reason) from None
    return _json({
        'kind': request.kind, 'prompt': request.prompt, 'base': request.base.lower(),
        'file_choices': files, 'edit_paths': sorted(edits), 'timeout_s': request.timeout_s,
        'spec_refs': request.spec_refs,
    })


def _public_row(row):
    return AttemptView(AttemptKey(row['task_id'], row['attempt_no']), Owner(**json.loads(row['owner'])),
                       row['state'], row['reason'], row['result_id'])


def _decoded(value):
    try:
        return json.loads(value) if value is not None else None
    except (ValueError, UnicodeError, TypeError):
        raise StoreError('STORE_UNAVAILABLE') from None


def _mapping(value, reason):
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise StoreError(reason)
    try:
        size = len(_json(dict(value)).encode('utf-8'))
    except (UnicodeError, TypeError, RecursionError, ValueError):
        raise StoreError(reason) from None
    if size > 64 * 1024:
        raise StoreError(reason) from None
    return dict(value)


def _identity(value, *, job):
    identity = _mapping(value, 'INVALID_PROCESS_IDENTITY')
    fields = {'pid', 'creation_time', 'session_id', 'boot_id'}
    if job:
        fields.add('job_name')
    if (set(identity) != fields
            or type(identity['pid']) is not int or not 0 < identity['pid'] <= 0xFFFFFFFF
            or type(identity['creation_time']) is not int or not 0 < identity['creation_time'] < 1 << 64
            or type(identity['session_id']) is not int or not 0 <= identity['session_id'] <= 0xFFFFFFFF):
        raise StoreError('INVALID_PROCESS_IDENTITY')
    _identifier(identity['boot_id'], 'INVALID_PROCESS_IDENTITY')
    if job:
        _identifier(identity['job_name'], 'INVALID_PROCESS_IDENTITY')
    return identity


def _stop_proof(row, proof):
    if (not isinstance(proof, StopProof) or proof.key != AttemptKey(row['task_id'], row['attempt_no'])
            or proof.runner_claim != row['runner_claim']):
        raise StoreError('STOP_PROOF_REQUIRED')
    try:
        identity = _identity(proof.identity, job=True) if proof.identity is not None else None
    except StoreError:
        raise StoreError('STOP_PROOF_REQUIRED') from None
    stored = _decoded(row['process_identity'])
    # A new CHECK launch may have no process even though prior subcases ran and stopped.
    prior_unproved = row['ever_resumed'] and (row['kind'] != 'CHECK' or not _decoded(row['check_progress']))
    if (identity != stored
            or proof.basis == 'JOB_EMPTY' and identity is None
            or (proof.basis == 'NEVER_STARTED'
                and (row['resume_possible'] or prior_unproved or identity is not None))
            or proof.basis == 'BOOT_CHANGED' and row['claimed_process'] is None
            or proof.basis not in ('JOB_EMPTY', 'NEVER_STARTED', 'BOOT_CHANGED')):
        raise StoreError('STOP_PROOF_REQUIRED')
    return _json({'basis': proof.basis, 'identity': identity})


def _result(value):
    value = _mapping(value, 'INVALID_RESULT')
    fields = {
        'state', 'reason', 'executable_version', 'exit_code', 'summary', 'manifest_id',
        'artifact_sha', 'binary_sha256', 'profile_fingerprint',
    }
    result = {key: item for key, item in value.items() if key in fields and item is not None}
    state = result.get('state', 'COMPLETED')
    if not isinstance(state, str) or state not in TERMINAL:
        raise StoreError('INVALID_RESULT')
    _reason(result.get('reason'))
    if ('summary' in result and not _text(result['summary'], 16 * 1024)
            or 'executable_version' in result and not _text(result['executable_version'], 256)
            or 'exit_code' in result and type(result['exit_code']) is not int):
        raise StoreError('INVALID_RESULT')
    for key in ('manifest_id', 'artifact_sha', 'binary_sha256', 'profile_fingerprint'):
        if key in result:
            result[key] = _digest(result[key], 'INVALID_RESULT')
    return result


def _profile_metadata(value):
    record = _mapping(value, 'INVALID_PROFILE')
    if set(record) != {'binary_path', 'binary_sha256', 'version', 'signer'}:
        raise StoreError('INVALID_PROFILE')
    if (not _text(record['binary_path'], 4096) or not Path(record['binary_path']).is_absolute()
            or not _text(record['version'], 256) or not record['version']
            or not _text(record['signer'], 256) or not record['signer']):
        raise StoreError('INVALID_PROFILE')
    record['binary_sha256'] = _digest(record['binary_sha256'], 'INVALID_PROFILE')
    return record


def _check_windows_store_file(path, *, sidecar):
    from ctypes import byref

    from ct_external_workspace import _FileInfo, _GetFileInformationByHandle, _GetFileType, _win_call, _win_open

    deadline = time.monotonic() + .05
    while True:
        try:
            # Metadata-only, with read/write/delete sharing. _win_open does not follow reparse points.
            with _win_open(path, 0, 7) as handle:
                info = _FileInfo()
                _win_call(_GetFileInformationByHandle, handle, byref(info))
                unsupported = stat.FILE_ATTRIBUTE_REPARSE_POINT | stat.FILE_ATTRIBUTE_DIRECTORY
                if _GetFileType(handle) != 1 or info.attributes & unsupported or info.links > 1:
                    raise StoreError('INVALID_STORE_ROOT')
                if info.links == 1:
                    return
        except FileNotFoundError:
            if sidecar:
                return
            raise
        except OSError as exc:
            if not sidecar or getattr(exc, 'winerror', None) not in (5, 303):  # Access denied or delete pending.
                raise

        # A DELETE-journal can be in the middle of being unlinked. Wait for proof
        # that it is gone or safely openable. Persistent uncertainty fails closed.
        remaining = deadline - time.monotonic()
        if not sidecar or remaining <= 0:
            raise StoreError('INVALID_STORE_ROOT')
        time.sleep(min(.005, remaining))


class Store:
    """Each operation uses its own connection. Callers hold the native lock for policy checks."""

    def __init__(self, root: Path, *, clock=time.time, busy_timeout=1.0):
        self.root = Path(root)
        self.db_path = self.root / 'state.sqlite3'
        self._clock = clock
        self._busy_timeout = busy_timeout
        self._check_layout()

    def _check_layout(self):
        if not self.root.is_absolute() or Path(os.path.abspath(self.root)) != self.root:
            raise StoreError('INVALID_STORE_ROOT')
        try:
            for path in (self.db_path, self.db_path.with_suffix('.sqlite3-journal'),
                         self.db_path.with_suffix('.sqlite3-wal'), self.db_path.with_suffix('.sqlite3-shm')):
                ct_storage.reject_links(path, Path(self.root.anchor))
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise StoreError('INVALID_STORE_ROOT')
                if os.name == 'nt' and info.st_nlink == 0:
                    # CPython can fall back to directory-entry metadata with an unknown link count.
                    _check_windows_store_file(path, sidecar=path != self.db_path)
                elif info.st_nlink != 1:
                    raise StoreError('INVALID_STORE_ROOT')
        except (OSError, ct_storage.LinkedPathError):
            raise StoreError('INVALID_STORE_ROOT') from None

    def attempt_root(self, key):
        task, attempt = _key(key)
        root = self.root / 'attempts' / f'task-{task}' / f'attempt-{attempt}'
        try:
            ct_storage.reject_links(root, Path(self.root.anchor))
        except (OSError, ct_storage.LinkedPathError):
            raise StoreError('INVALID_STORE_ROOT') from None
        return root

    def _owner(self, owner, *, mutating=True):
        if not isinstance(owner, Owner) or not isinstance(owner.workspace_key, (str, Path)):
            raise StoreError('INVALID_OWNER')
        for value in (owner.leader_app, owner.chat_id, owner.worker_type):
            _identifier(value, 'INVALID_OWNER')
        if owner.chat_id == 'UNKNOWN' or mutating and (owner.leader_app, owner.worker_type) != ('codex', 'antigravity'):
            raise StoreError('INVALID_OWNER')
        root = Path(owner.workspace_key)
        if not root.is_absolute():
            raise StoreError('INVALID_WORKSPACE')
        try:
            ct_storage.reject_links(root, Path(root.anchor))
            canonical = root.resolve(strict=True)
            if canonical != root or not canonical.is_dir():
                raise StoreError('INVALID_WORKSPACE')
        except (OSError, RuntimeError, ct_storage.LinkedPathError):
            raise StoreError('INVALID_WORKSPACE') from None
        if (canonical == self.root or canonical in self.root.parents or self.root in canonical.parents):
            raise StoreError('INVALID_STORE_ROOT')
        return Owner(owner.leader_app, owner.chat_id, str(canonical), owner.worker_type)

    @contextmanager
    def _db(self, *, writable=False, create=False):
        self._check_layout()
        existed = self.db_path.exists()
        if not existed and not create:
            yield None
            return
        connection = None
        try:
            if create:
                self.root.mkdir(parents=True, exist_ok=True)
            mode = 'rwc' if create else 'rw' if writable else 'ro'
            connection = sqlite3.connect(
                f'{self.db_path.as_uri()}?mode={mode}', uri=True, isolation_level=None, timeout=self._busy_timeout,
            )
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA trusted_schema=OFF')
            if connection.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
                raise StoreError('STORE_UNAVAILABLE')
            if writable:
                connection.execute('PRAGMA synchronous=FULL')
            connection.execute('BEGIN IMMEDIATE' if writable else 'BEGIN')
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            if version == 0 and create and not existed and not tables:
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.executemany('INSERT INTO meta VALUES (?, ?)', _META_DEFAULTS.items())
                connection.execute('PRAGMA user_version=1')
            elif version != 1 or not _TABLES.issubset(tables):
                raise StoreError('STORE_UNAVAILABLE')
            yield connection
            connection.commit()
        except (sqlite3.Error, OSError):
            raise StoreError('STORE_UNAVAILABLE') from None
        finally:
            # SQLite rolls back any uncommitted transaction when this connection closes.
            if connection is not None:
                connection.close()

    @staticmethod
    def _meta(connection, name):
        row = connection.execute('SELECT value FROM meta WHERE name=?', (name,)).fetchone()
        if row is None or type(row[0]) is not int or row[0] < 0:
            raise StoreError('STORE_UNAVAILABLE')
        if name != 'runtime_epoch' and row[0] not in (0, 1):
            raise StoreError('STORE_UNAVAILABLE')
        return row[0]

    @staticmethod
    def _opt(connection, owner_key):
        row = connection.execute('SELECT enabled, epoch FROM opt_ins WHERE owner=?', (owner_key,)).fetchone()
        return (row['enabled'], row['epoch']) if row else (0, 0)

    @staticmethod
    def _active(connection):
        return connection.execute('SELECT * FROM attempts WHERE id=(SELECT attempt FROM slot)').fetchone()

    @staticmethod
    def _attempt(connection, key, *, owner=None, claim=None):
        task, number = _key(key)
        row = (connection.execute('SELECT * FROM attempts WHERE task_id=? AND attempt_no=?',
                                  (task, number)).fetchone() if connection else None)
        if row is None or row['task_id'] != task:
            raise StoreError('ATTEMPT_NOT_FOUND')
        if owner is not None and _json(asdict(owner)) != row['owner']:
            raise StoreError('OWNER_MISMATCH')
        if claim is not None and (not isinstance(claim, str) or row['runner_claim'] is None
                                  or not secrets.compare_digest(row['runner_claim'], claim)):
            raise StoreError('CLAIM_MISMATCH')
        return row

    def _runner(self, connection, key, claim):
        if not isinstance(claim, str) or not re.fullmatch(r'[0-9a-f]{64}', claim):
            raise StoreError('CLAIM_MISMATCH')
        return self._attempt(connection, key, claim=claim)

    def _maintenance_attempt(self, connection, key, owner):
        task, number = _key(key)
        if connection is None or not self._meta(connection, 'maintenance'):
            raise StoreError('MAINTENANCE')
        active = self._active(connection)
        if active is None or (active['task_id'], active['attempt_no']) != (task, number):
            raise StoreError('ATTEMPT_NOT_FOUND')
        if not isinstance(owner, Owner) or owner != Owner(**_decoded(active['owner'])):
            raise StoreError('OWNER_MISMATCH')
        return active

    @staticmethod
    def _update(connection, row, **changes):
        values = ', '.join(f'{column}=?' for column in changes)
        connection.execute(f'UPDATE attempts SET {values} WHERE id=?', (*changes.values(), row['id']))
        return connection.execute('SELECT * FROM attempts WHERE id=?', (row['id'],)).fetchone()

    @staticmethod
    def _release(connection, row, *, discard=False):
        connection.execute('DELETE FROM slot WHERE attempt=?', (row['id'],))
        if discard:
            connection.execute('DELETE FROM edit_owners WHERE attempt=?', (row['id'],))

    def _stop(self, connection, row, reason):
        if row['state'] in TERMINAL or row['stop_reason'] is not None:
            return row
        if row['state'] == 'RESERVED':
            self._release(connection, row, discard=True)
            return self._update(connection, row, state='CANCELED', reason=reason, stop_reason=reason,
                                disposition='DISCARDED' if row['kind'] == 'EDIT' else None,
                                finished_at=self._clock(), cleanup_pending=1)
        state = 'RECONCILE_REQUIRED' if row['state'] == 'RECONCILE_REQUIRED' else 'STOPPING'
        return self._update(connection, row, state=state, reason=reason, stop_reason=reason)

    def _check_admission(self, connection, row, stamp):
        if self._meta(connection, 'maintenance') or self._meta(connection, 'runtime_epoch') != row['runtime_epoch']:
            return 'MAINTENANCE'
        reason = _policy_reason(stamp)
        if reason is not None:
            return reason
        if _json(asdict(stamp)) != row['policy_stamp']:
            return 'POLICY_CHANGED'
        enabled, epoch = self._opt(connection, row['owner'])
        if not enabled or epoch != row['opt_epoch']:
            return 'OPT_IN_CHANGED'
        return None

    def _ready(self, connection, profile_fingerprint):
        if profile_fingerprint is None:
            return False
        return connection.execute(
            """SELECT 1 FROM checks JOIN attempts ON checks.attempt=attempts.id
               WHERE checks.profile_fingerprint=? AND attempts.profile_fingerprint=checks.profile_fingerprint
               AND attempts.kind='CHECK' AND attempts.state='COMPLETED' AND attempts.result_id IS NOT NULL
               AND attempts.stop_proof IS NOT NULL AND attempts.runtime_epoch=?""",
            (profile_fingerprint, self._meta(connection, 'runtime_epoch')),
        ).fetchone() is not None

    def status(self, owner, stamp=None, profile_fingerprint=None, *, include_history=True):
        owner = self._owner(owner, mutating=False)
        if profile_fingerprint is not None:
            profile_fingerprint = _digest(profile_fingerprint, 'INVALID_PROFILE')
        effective = stamp.effective if isinstance(stamp, ct_policy.PolicyStamp) else None
        with self._db() as connection:
            if connection is None:
                return StatusView(owner, 'OFF', effective, 'NOT_READY', 'OPT_IN_OFF', ())
            owner_key = _json(asdict(owner))
            enabled, _ = self._opt(connection, owner_key)
            attempts = ()
            if include_history:
                attempts = tuple(_public_row(row) for row in connection.execute(
                    """SELECT task_id, attempt_no, owner, state, reason, result_id FROM attempts
                       WHERE owner=? ORDER BY id DESC""", (owner_key,)))
            active = connection.execute('SELECT state FROM attempts WHERE id=(SELECT attempt FROM slot)').fetchone()
            if self._meta(connection, 'maintenance'):
                readiness, reason = 'NOT_READY', 'MAINTENANCE'
            elif not enabled:
                readiness, reason = 'NOT_READY', 'OPT_IN_OFF'
            elif effective != 'ON':
                readiness, reason = 'NOT_READY', 'POLICY_OFF' if effective == 'OFF' else 'POLICY_UNKNOWN'
            elif active is not None:
                readiness = 'RECONCILE_REQUIRED' if active['state'] == 'RECONCILE_REQUIRED' else 'BUSY'
                reason = readiness
            elif self._ready(connection, profile_fingerprint):
                readiness, reason = 'READY', None
            else:
                readiness, reason = 'NOT_READY', 'CHECK_REQUIRED'
            return StatusView(owner, 'ON' if enabled else 'OFF', effective, readiness, reason, attempts)

    def opt_in_enabled(self, owner):
        owner = self._owner(owner, mutating=False)
        with self._db() as connection:
            return bool(connection is not None and self._opt(connection, _json(asdict(owner)))[0])

    def epochs_initialized(self):
        with self._db() as connection:
            return bool(connection is not None and self._meta(connection, 'epochs_initialized'))

    def ensure_policy_epochs(self, policy_path, *, compatible):
        """Called under the native lock after checking all applicable installation bindings."""
        if compatible is not True:
            raise StoreError('INSTALLATION_INCOMPATIBLE')
        try:
            if self.epochs_initialized():
                ct_policy.policy_stamp(policy_path, '', require_epochs=True)
                return
            ct_policy.initialize_policy_epochs(policy_path)
        except ct_policy.PolicyWriterIncompatible:
            raise StoreError('POLICY_WRITER_INCOMPATIBLE') from None
        except (OSError, ValueError):
            raise StoreError('POLICY_UNKNOWN') from None
        with self._db(writable=True, create=True) as connection:
            connection.execute("UPDATE meta SET value=1 WHERE name='epochs_initialized'")

    def set_opt_in(self, owner, enabled, stamp):
        owner = self._owner(owner)
        if type(enabled) is not bool:
            raise StoreError('INVALID_OPT_IN')
        denial = _policy_reason(stamp)
        if enabled and denial is not None:
            raise StoreError(denial)
        with self._db(writable=True, create=enabled) as connection:
            if connection is not None:
                if enabled and self._meta(connection, 'maintenance'):
                    raise StoreError('MAINTENANCE')
                owner_key = _json(asdict(owner))
                was_on, epoch = self._opt(connection, owner_key)
                if bool(was_on) != enabled:
                    connection.execute(
                        """INSERT INTO opt_ins VALUES (?, ?, ?)
                           ON CONFLICT(owner) DO UPDATE SET enabled=excluded.enabled, epoch=excluded.epoch""",
                        (owner_key, enabled, epoch + 1),
                    )
                    if not enabled:
                        active = self._active(connection)
                        if active is not None and active['owner'] == owner_key:
                            self._stop(connection, active, 'OPT_IN_OFF')
        return self.status(owner, stamp, include_history=False)

    def reserve(self, owner, key, request, stamp, profile_fingerprint):
        owner = self._owner(owner)
        task, number = _key(key)
        self.attempt_root(key)
        payload = _request(request)
        digest = _hash_text(payload)
        owner_key = _json(asdict(owner))
        with self._db(writable=True) as connection:
            if connection is None:
                raise StoreError('OPT_IN_OFF')
            previous = self._previous_reservation(connection, task, number, owner_key, digest)
            if previous is not None:
                return _public_row(previous)
            profile = _digest(profile_fingerprint, 'INVALID_PROFILE')
            previous = connection.execute(
                'SELECT task_id, attempt_no, owner FROM attempts WHERE task_id=? ORDER BY attempt_no DESC LIMIT 1',
                (task,),
            ).fetchone()
            if previous is not None:
                if previous['owner'] != owner_key:
                    raise StoreError('OWNER_MISMATCH')
                if previous['task_id'] != task or previous['attempt_no'] >= number:
                    raise StoreError('CONFLICT')
            if self._meta(connection, 'maintenance'):
                raise StoreError('MAINTENANCE')
            denial = _policy_reason(stamp)
            if denial is not None:
                raise StoreError(denial)
            enabled, epoch = self._opt(connection, owner_key)
            if not enabled:
                raise StoreError('OPT_IN_OFF')
            now = self._clock()
            active = self._active(connection)
            if active is not None:
                if active['state'] != 'RESERVED' or active['prepare_deadline'] > now:
                    raise StoreError('RECONCILE_REQUIRED' if active['state'] == 'RECONCILE_REQUIRED' else 'BUSY')
                self._stop(connection, active, 'PREPARE_LEASE_EXPIRED')
            if request.kind != 'CHECK' and not self._ready(connection, profile):
                raise StoreError('CHECK_REQUIRED')
            workspace = os.path.normcase(owner.workspace_key)
            paths = tuple(path.casefold() for path in json.loads(payload)['edit_paths'])
            if paths:
                self._check_edit_scope(connection, workspace, paths)
            cursor = connection.execute(
                """INSERT INTO attempts (task_id, attempt_no, owner, kind, request, request_digest,
                   profile_fingerprint, policy_stamp, opt_epoch, runtime_epoch, state, reserved_at, prepare_deadline)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RESERVED', ?, ?)""",
                (task, number, owner_key, request.kind, payload, digest, profile, _json(asdict(stamp)), epoch,
                 self._meta(connection, 'runtime_epoch'), now, now + 60),
            )
            attempt = cursor.lastrowid
            connection.execute('INSERT INTO slot VALUES (1, ?)', (attempt,))
            connection.executemany('INSERT INTO edit_owners VALUES (?, ?, ?)',
                                   ((workspace, path, attempt) for path in paths))
            if request.kind == 'CHECK':
                connection.execute('DELETE FROM checks WHERE profile_fingerprint=?', (profile,))
            return _public_row(self._attempt(connection, key))

    @staticmethod
    def _previous_reservation(connection, task, number, owner_key, digest):
        previous = connection.execute('SELECT * FROM attempts WHERE task_id=? AND attempt_no=?',
                                      (task, number)).fetchone()
        if previous is not None:
            if previous['owner'] != owner_key:
                raise StoreError('OWNER_MISMATCH')
            if previous['task_id'] != task or previous['request_digest'] != digest:
                raise StoreError('CONFLICT')
        return previous

    def lookup_reservation(self, owner, key, request):
        """Check for an identical replay without checking current admission or creating state."""
        owner = self._owner(owner)
        task, number = _key(key)
        digest = _hash_text(_request(request))
        with self._db() as connection:
            if connection is None:
                return None
            previous = self._previous_reservation(connection, task, number, _json(asdict(owner)), digest)
            return _public_row(previous) if previous is not None else None

    @staticmethod
    def _check_edit_scope(connection, workspace, paths):
        held = [row[0] for row in connection.execute(
            'SELECT path_key FROM edit_owners WHERE workspace_key=? ORDER BY path_key', (workspace,))]
        held_names = set(held)
        for path in paths:
            start = bisect_left(held, path + '/')
            if start < len(held) and held[start].startswith(path + '/'):
                raise StoreError('EDIT_SCOPE_BUSY')
            while path:
                if path in held_names:
                    raise StoreError('EDIT_SCOPE_BUSY')
                path = path.rpartition('/')[0]

    def set_projection(self, key, owner, manifest_id):
        owner = self._owner(owner)
        manifest_id = _digest(manifest_id, 'INVALID_MANIFEST')
        with self._db(writable=True) as connection:
            row = self._attempt(connection, key, owner=owner)
            if row['manifest_id'] == manifest_id:
                return _public_row(row)
            if row['state'] != 'RESERVED' or row['manifest_id'] is not None:
                raise StoreError('INVALID_STATE')
            return _public_row(self._update(connection, row, manifest_id=manifest_id))

    def _fence(self, connection, row, stamp):
        if row['state'] in TERMINAL:
            return 'INVALID_STATE'
        if row['stop_reason'] is not None:
            return row['stop_reason']
        reason = self._check_admission(connection, row, stamp)
        if reason is not None:
            self._stop(connection, row, reason)
        return reason

    def claim_start(self, key, owner, stamp):
        owner = self._owner(owner)
        with self._db(writable=True) as connection:
            row = self._attempt(connection, key, owner=owner)
            if row['runner_claim'] is not None:
                raise StoreError('START_ALREADY_CLAIMED')
            if row['state'] != 'RESERVED' or (row['kind'] != 'CHECK' and row['manifest_id'] is None):
                raise StoreError('INVALID_STATE')
            denial = self._fence(connection, row, stamp)
            now = self._clock()
            if denial is None and now >= row['prepare_deadline']:
                denial = 'PREPARE_LEASE_EXPIRED'
                self._stop(connection, row, denial)
            if denial is None:
                return _public_row(self._update(connection, row, state='STARTING',
                                                 runner_claim=secrets.token_hex(32), startup_deadline=now + 15))
        raise StoreError(denial)

    def claim_runner(self, key, runner_claim, stamp, claimed_process):
        owner_process = _identity(claimed_process, job=False)
        with self._db(writable=True) as connection:
            row = self._runner(connection, key, runner_claim)
            if row['claimed_process'] is not None:
                raise StoreError('START_ALREADY_CLAIMED')
            denial = self._fence(connection, row, stamp)
            if denial is None:
                if row['state'] != 'STARTING':
                    raise StoreError('INVALID_STATE')
                now = self._clock()
                if now >= row['startup_deadline']:
                    denial = 'STARTUP_LEASE_EXPIRED'
                    self._stop(connection, row, denial)
                else:
                    return _public_row(self._update(connection, row, claimed_process=_json(owner_process),
                                                     started_at=now))
        raise StoreError(denial)

    def fence_expired_start(self, key, owner, *, maintenance=False):
        if not maintenance:
            owner = self._owner(owner)
        with self._db(writable=True) as connection:
            row = (self._maintenance_attempt(connection, key, owner) if maintenance
                   else self._attempt(connection, key, owner=owner))
            if (row['state'] not in ('STARTING', 'STOPPING', 'RECONCILE_REQUIRED')
                    or row['startup_deadline'] is None or self._clock() < row['startup_deadline']
                    or row['claimed_process'] is not None or row['launch_metadata'] is not None):
                raise StoreError('STOP_PROOF_REQUIRED')
            return _public_row(self._stop(connection, row, 'STARTUP_LEASE_EXPIRED'))

    def run_snapshot(self, key, *, owner=None, runner_claim=None, maintenance=False):
        if (owner is None) == (runner_claim is None) or maintenance and owner is None:
            raise StoreError('CLAIM_MISMATCH')
        with self._db() as connection:
            if maintenance:
                row = self._maintenance_attempt(connection, key, owner)
            elif owner is not None:
                row = self._attempt(connection, key, owner=self._owner(owner))
            else:
                row = self._runner(connection, key, runner_claim)
            payload = _decoded(row['request'])
            payload['edit_paths'] = tuple(payload['edit_paths'])
            payload['spec_refs'] = tuple(payload['spec_refs'])
            return RunSnapshot(
                view=_public_row(row), request=Request(**payload), profile_fingerprint=row['profile_fingerprint'],
                manifest_id=row['manifest_id'], runner_claim=row['runner_claim'],
                claimed_process=_decoded(row['claimed_process']), launch_metadata=_decoded(row['launch_metadata']),
                process_identity=_decoded(row['process_identity']), resume_possible=bool(row['resume_possible']),
                stop_proof=_decoded(row['stop_proof']), check_progress=tuple(_decoded(row['check_progress'])),
                prepare_deadline=row['prepare_deadline'], startup_deadline=row['startup_deadline'],
                disposition=row['disposition'], candidate_sha=row['candidate_sha'], artifact_sha=row['artifact_sha'],
                tested_sha=row['tested_sha'], journal_id=row['journal_id'], opt_epoch=row['opt_epoch'],
                policy_stamp=ct_policy.PolicyStamp(**_decoded(row['policy_stamp'])), reserved_at=row['reserved_at'],
                started_at=row['started_at'], resumed_at=row['resumed_at'], process_exit_at=row['process_exit_at'],
                stopped_at=row['stopped_at'],
                finished_at=row['finished_at'], received_at=row['received_at'],
            )

    def admit_launch(self, key, runner_claim, stamp, launch_metadata):
        metadata = _mapping(launch_metadata, 'INVALID_LAUNCH')
        _identifier(metadata.get('job_name'), 'INVALID_LAUNCH')
        with self._db(writable=True) as connection:
            row = self._runner(connection, key, runner_claim)
            denial = self._fence(connection, row, stamp)
            if denial is None:
                if row['claimed_process'] is None:
                    raise StoreError('CLAIM_MISMATCH')
                if row['state'] == 'FINALIZING' and row['kind'] == 'CHECK':
                    progress = _decoded(row['check_progress'])
                    if (not progress or len(progress) == len(CHECK_PROBES)
                            or progress[-1]['stop_proof'] != _decoded(row['stop_proof'])
                            or progress[-1]['evidence']['passed'] is not True
                            or any(metadata['job_name'] == entry['stop_proof']['identity']['job_name']
                                   for entry in progress)):
                        raise StoreError('INVALID_CHECK_STEP')
                elif row['state'] != 'STARTING' or row['launch_metadata'] is not None:
                    raise StoreError('INVALID_STATE')
                return _public_row(self._update(connection, row, state='STARTING', launch_metadata=_json(metadata),
                                                 process_identity=None, resume_possible=0, stop_proof=None,
                                                 stopped_at=None, process_exit_at=None, process_exit_code=None))
        raise StoreError(denial)

    def record_process(self, key, runner_claim, identity):
        """Record a suspended process even if a concurrent cancellation has won."""
        identity = _identity(identity, job=True)
        with self._db(writable=True) as connection:
            row = self._runner(connection, key, runner_claim)
            journal = _decoded(row['launch_metadata'])
            owner = _decoded(row['claimed_process'])
            if (row['state'] not in ('STARTING', 'STOPPING', 'RECONCILE_REQUIRED')
                    or journal is None or row['stop_proof'] is not None):
                raise StoreError('INVALID_STATE')
            if (journal['job_name'] != identity['job_name']
                    or any(identity[field] != owner[field] for field in ('boot_id', 'session_id'))):
                raise StoreError('INVALID_PROCESS_IDENTITY')
            encoded = _json(identity)
            if row['process_identity'] is not None and row['process_identity'] != encoded:
                raise StoreError('INVALID_PROCESS_IDENTITY')
            return _public_row(self._update(connection, row, process_identity=encoded))

    def record_process_exit(self, key, runner_claim, exit_code, *, identity=None):
        if type(exit_code) is not int or not 0 <= exit_code <= 0xFFFFFFFF:
            raise StoreError('INVALID_EXIT_CODE')
        with self._db(writable=True) as connection:
            row = self._runner(connection, key, runner_claim)
            if row['state'] in TERMINAL:
                return _public_row(row)
            if (row['process_identity'] is None or identity is not None
                    and _json(_identity(identity, job=True)) != row['process_identity']):
                raise StoreError('INVALID_PROCESS_IDENTITY')
            if row['process_exit_at'] is None:
                row = self._update(connection, row, process_exit_at=self._clock(), process_exit_code=exit_code)
            elif row['process_exit_code'] != exit_code:
                raise StoreError('INVALID_EXIT_CODE')
            return _public_row(row)

    def resume_process(self, key, runner_claim, stamp, resume: Callable[[], None]):
        """Persist uncertainty before the external side effect, then serialize resume against cancel."""
        with self._db(writable=True) as connection:
            row = self._runner(connection, key, runner_claim)
            denial = self._fence(connection, row, stamp)
            if denial is None:
                if (row['state'] != 'STARTING' or row['resume_possible'] or row['process_identity'] is None
                        or row['stop_proof'] is not None):
                    raise StoreError('INVALID_STATE')
                self._update(connection, row, resume_possible=1, ever_resumed=1)
        if denial is not None:
            raise StoreError(denial)
        with self._db(writable=True) as connection:
            row = self._runner(connection, key, runner_claim)
            denial = self._fence(connection, row, stamp)
            if denial is None:
                if row['state'] != 'STARTING' or not row['resume_possible'] or row['stop_proof'] is not None:
                    raise StoreError('INVALID_STATE')
                resume()
                return _public_row(self._update(connection, row, state='RUNNING', resumed_at=self._clock()))
        raise StoreError(denial)

    def verify_admission(self, key, runner_claim, stamp):
        with self._db(writable=True) as connection:
            row = self._runner(connection, key, runner_claim)
            denial = self._fence(connection, row, stamp)
            if denial is None:
                return _public_row(row)
        raise StoreError(denial)

    def cancel(self, owner, key, reason='CANCELED'):
        owner = self._owner(owner)
        reason = _reason(reason) or 'CANCELED'
        with self._db(writable=True) as connection:
            row = self._attempt(connection, key, owner=owner)
            return _public_row(self._stop(connection, row, reason))

    def pending_projection_cleanup(self, *, maximum=16):
        """Read the oldest canceled reservations whose projection cleanup is unconfirmed."""
        if type(maximum) is not int or not 0 <= maximum < 1 << 63:
            raise StoreError('INVALID_LIMIT')
        with self._db() as connection:
            if connection is None:
                return ()
            rows = connection.execute(
                """SELECT task_id, attempt_no, owner, state, reason, result_id FROM attempts
                   WHERE cleanup_pending=1 AND state='CANCELED' AND runner_claim IS NULL
                   ORDER BY id LIMIT ?""", (maximum,))
            return tuple(_public_row(row) for row in rows)

    def finish_projection_cleanup(self, key):
        """Clear the cleanup marker once the trusted controller has removed the projection."""
        with self._db(writable=True) as connection:
            row = self._attempt(connection, key)
            if row['state'] != 'CANCELED' or row['runner_claim'] is not None:
                raise StoreError('INVALID_STATE')
            if row['cleanup_pending']:
                row = self._update(connection, row, cleanup_pending=0)
            return _public_row(row)

    def hold_for_reconcile(self, key, runner_claim, reason):
        return self.transition(key, runner_claim,
                               ('STARTING', 'RUNNING', 'STOPPING', 'FINALIZING', 'RECONCILE_REQUIRED'),
                               'RECONCILE_REQUIRED', {'reason': reason})

    def transition(self, key, runner_claim, expected, next_state, metadata):
        """Status-only runner transitions. Launch and stop evidence have dedicated methods."""
        reason = _reason(_mapping(metadata, 'INVALID_METADATA').get('reason'))
        expected = (expected,) if isinstance(expected, str) else expected
        with self._db(writable=True) as connection:
            row = self._runner(connection, key, runner_claim)
            if row['state'] in TERMINAL:
                return _public_row(row)
            if row['state'] not in expected:
                raise StoreError('INVALID_STATE')
            if next_state == 'STOPPING':
                return _public_row(self._stop(connection, row, reason or 'CANCELED'))
            if next_state == 'RECONCILE_REQUIRED':
                return _public_row(self._update(connection, row, state=next_state,
                                                 reason=reason or 'RECONCILE_REQUIRED',
                                                 stop_reason=row['stop_reason'] or reason or 'RECONCILE_REQUIRED'))
            raise StoreError('INVALID_STATE')

    def record_stopped(self, key, runner_claim, proof):
        with self._db(writable=True) as connection:
            row = self._runner(connection, key, runner_claim)
            if row['state'] in TERMINAL:
                return _public_row(row)
            return _public_row(self._record_stopped(connection, row, proof))

    def _record_stopped(self, connection, row, proof):
        encoded = _stop_proof(row, proof)
        if row['stop_proof'] is not None:
            if row['stop_proof'] != encoded:
                raise StoreError('STOP_PROOF_REQUIRED')
            return row
        if row['state'] not in ('STARTING', 'RUNNING', 'STOPPING', 'RECONCILE_REQUIRED'):
            raise StoreError('INVALID_STATE')
        state = row['state'] if row['state'] in ('STOPPING', 'RECONCILE_REQUIRED') else 'FINALIZING'
        return self._update(connection, row, state=state, stop_proof=encoded, stopped_at=self._clock())

    @staticmethod
    def _check_probe_proven(evidence):
        if evidence.get('tool_absence'):
            return evidence['probe'] == 'deny_mcp' and evidence['runtime_denial'] is False
        return evidence['runtime_denial'] == evidence['probe'].startswith('deny_')

    def record_check_step(self, key, runner_claim, stamp, step, proof, evidence):
        evidence = _mapping(evidence, 'INVALID_CHECK_STEP')
        _identifier(evidence.get('probe'), 'INVALID_CHECK_STEP')
        if (type(evidence.get('passed')) is not bool
                or not set(evidence) <= {'probe', 'passed', 'reason', 'runtime_denial', 'exit_code', 'tool_absence'}
                or type(evidence.get('runtime_denial')) is not bool
                or type(evidence.get('tool_absence', False)) is not bool
                or evidence.get('exit_code') is not None and type(evidence['exit_code']) is not int):
            raise StoreError('INVALID_CHECK_STEP')
        _reason(evidence.get('reason'))
        with self._db(writable=True) as connection:
            row = self._runner(connection, key, runner_claim)
            denial = self._fence(connection, row, stamp)
            if denial is None:
                progress = _decoded(row['check_progress'])
                stopped = json.loads(_stop_proof(row, proof))
                if (row['kind'] != 'CHECK' or type(step) is not int or step != len(progress) + 1
                        or step > len(CHECK_PROBES) or CHECK_PROBES[step - 1] != evidence['probe']
                        or stopped['basis'] != 'JOB_EMPTY' or not row['resume_possible']
                        or (evidence['passed'] and not self._check_probe_proven(evidence))
                        or any(entry['stop_proof'] == stopped for entry in progress)):
                    raise StoreError('INVALID_CHECK_STEP')
                row = self._record_stopped(connection, row, proof)
                progress.append({'step': step, 'evidence': evidence, 'stop_proof': stopped,
                                 'stopped_at': row['stopped_at'], 'process_exit_at': row['process_exit_at']})
                return _public_row(self._update(connection, row, check_progress=_json(progress)))
        raise StoreError(denial)

    def _publish(self, connection, row, result):
        result['profile_fingerprint'] = row['profile_fingerprint']
        if row['manifest_id'] is not None:
            result['manifest_id'] = row['manifest_id']
        key = {'task_id': row['task_id'], 'attempt_no': row['attempt_no']}
        result_id = _hash_text(_json({**key, 'result': result}))
        finished = self._update(connection, row, state=result['state'], reason=result.get('reason'),
                                result_id=result_id, result=_json(result), artifact_sha=result.get('artifact_sha'),
                                finished_at=self._clock())
        self._release(connection, row)
        return _public_row(finished)

    def _stop_result(self, connection, row, stamp, proof):
        reason = row['stop_reason'] or self._check_admission(connection, row, stamp)
        if reason is not None:
            timed_out = reason in ('TIMEOUT', 'TIMED_OUT', 'DEADLINE_EXCEEDED')
            return {'state': 'TIMED_OUT' if timed_out else 'CANCELED', 'reason': reason}
        if proof.basis == 'BOOT_CHANGED':
            return {'state': 'FAILED', 'reason': 'PROCESS_REBOOTED'}
        return None

    def commit_result(self, key, runner_claim, stamp, stop_proof, result):
        result = _result(result)
        with self._db(writable=True) as connection:
            row = self._runner(connection, key, runner_claim)
            if row['state'] in TERMINAL:
                return _public_row(row)
            row = self._record_stopped(connection, row, stop_proof)
            stopped = self._stop_result(connection, row, stamp, stop_proof)
            if stopped is not None:
                return self._publish(connection, row, stopped)
            state = result.setdefault('state', 'COMPLETED')
            if state == 'COMPLETED':
                if row['kind'] == 'CHECK':
                    raise StoreError('CHECK_PROOF_REQUIRED')
                if (stop_proof.basis != 'JOB_EMPTY' or not row['resume_possible']
                        or row['kind'] == 'EDIT' and result.get('artifact_sha') is None):
                    raise StoreError('INVALID_RESULT')
            if (result.get('manifest_id', row['manifest_id']) != row['manifest_id']
                    or result.get('profile_fingerprint', row['profile_fingerprint']) != row['profile_fingerprint']):
                raise StoreError('INVALID_RESULT')
            return self._publish(connection, row, result)

    def commit_check(self, key, runner_claim, stamp, stop_proof, qualified_fingerprint, probe_results,
                     *, profile_metadata=None):
        profile = _digest(qualified_fingerprint, 'INVALID_PROFILE')
        qualification = _profile_metadata(profile_metadata) if profile_metadata is not None else None
        if not isinstance(probe_results, (list, tuple)):
            raise StoreError('INVALID_CHECK_STEP')
        with self._db(writable=True) as connection:
            row = self._runner(connection, key, runner_claim)
            if row['kind'] != 'CHECK':
                raise StoreError('INVALID_STATE')
            if row['state'] in TERMINAL:
                return _public_row(row)
            row = self._record_stopped(connection, row, stop_proof)
            stopped = self._stop_result(connection, row, stamp, stop_proof)
            if stopped is not None:
                return self._publish(connection, row, stopped)
            if profile != row['profile_fingerprint']:
                return self._publish(connection, row, {'state': 'FAILED', 'reason': 'PROFILE_CHANGED'})
            progress = _decoded(row['check_progress'])
            evidence = [entry['evidence'] for entry in progress]
            if (tuple(probe['probe'] for probe in evidence) != CHECK_PROBES or evidence != list(probe_results)
                    or progress[-1]['stop_proof'] != _decoded(row['stop_proof'])
                    or not all(probe['passed'] and self._check_probe_proven(probe) for probe in evidence)):
                return self._publish(connection, row, {'state': 'FAILED', 'reason': 'CHECK_PROOF_REQUIRED'})
            result = {'state': 'COMPLETED', 'probe_results': evidence}
            if qualification is not None:
                result.update(binary_sha256=qualification['binary_sha256'],
                              executable_version=qualification['version'])
            view = self._publish(connection, row, result)
            connection.execute('INSERT INTO checks VALUES (?, ?, ?)',
                               (profile, row['id'], _json(qualification) if qualification is not None else None))
            return view

    def grant_read(self, owner, key, ttl_s):
        owner = self._owner(owner)
        if type(ttl_s) is not int or not 1 <= ttl_s <= 86400:
            raise StoreError('INVALID_GRANT')
        with self._db(writable=True) as connection:
            row = self._attempt(connection, key, owner=owner)
            token = secrets.token_hex(32)
            connection.execute(
                """INSERT INTO read_grants VALUES (?, ?, ?)
                   ON CONFLICT(attempt) DO UPDATE SET token_hash=excluded.token_hash,
                   expires_at=excluded.expires_at""",
                (row['id'], _hash_text(token), self._clock() + ttl_s),
            )
            return token

    def revoke_read(self, owner, key):
        owner = self._owner(owner)
        with self._db(writable=True) as connection:
            row = self._attempt(connection, key, owner=owner)
            connection.execute('DELETE FROM read_grants WHERE attempt=?', (row['id'],))
            return _public_row(row)

    @staticmethod
    def _collect(row):
        return {
            'task_id': row['task_id'], 'attempt_no': row['attempt_no'], 'state': row['state'],
            'reason': row['reason'], 'result_id': row['result_id'], 'result': _decoded(row['result']),
            'timestamps': {key: row[key] for key in ('reserved_at', 'started_at', 'resumed_at', 'process_exit_at',
                                                   'stopped_at', 'finished_at', 'received_at')},
        }

    def collect(self, key, read_grant, *, workspace_key=None):
        if not isinstance(read_grant, str) or not re.fullmatch(r'[0-9a-f]{64}', read_grant):
            raise StoreError('INVALID_GRANT')
        with self._db() as connection:
            try:
                row = self._attempt(connection, key)
            except StoreError as exc:
                if exc.reason != 'ATTEMPT_NOT_FOUND':
                    raise
                raise StoreError('INVALID_GRANT') from None
            grant = connection.execute('SELECT * FROM read_grants WHERE attempt=?', (row['id'],)).fetchone()
            if (grant is None or grant['expires_at'] <= self._clock()
                    or not secrets.compare_digest(grant['token_hash'], _hash_text(read_grant))):
                raise StoreError('INVALID_GRANT')
            if (workspace_key is not None
                    and Path(workspace_key) != Path(_decoded(row['owner'])['workspace_key'])):
                raise StoreError('INVALID_GRANT')
            return self._collect(row)

    def collect_for_owner(self, owner, key):
        owner = self._owner(owner)
        with self._db() as connection:
            return self._collect(self._attempt(connection, key, owner=owner))

    def mark_received(self, owner, key):
        owner = self._owner(owner)
        with self._db(writable=True) as connection:
            row = self._attempt(connection, key, owner=owner)
            if row['state'] not in TERMINAL:
                raise StoreError('INVALID_STATE')
            if row['received_at'] is None:
                row = self._update(connection, row, received_at=self._clock())
            return _public_row(row)

    def active_attempt(self):
        with self._db() as connection:
            row = self._active(connection) if connection is not None else None
            return _public_row(row) if row is not None else None

    def begin_maintenance(self):
        """Caller owns install and native policy locks. Return the running attempt to reconcile."""
        with self._db(writable=True) as connection:
            if connection is None:
                return None
            connection.execute("UPDATE meta SET value=1 WHERE name='maintenance'")
            row = self._active(connection)
            if row is not None:
                row = self._stop(connection, row, 'MAINTENANCE')
                return _public_row(row) if row['state'] not in TERMINAL else None
            return None

    def maintenance_pending(self):
        with self._db() as connection:
            return bool(connection is not None and self._meta(connection, 'maintenance'))

    def finish_maintenance(self):
        with self._db(writable=True) as connection:
            if connection is None or not self._meta(connection, 'maintenance'):
                return
            if self._active(connection) is not None:
                raise StoreError('RECONCILE_REQUIRED')
            connection.execute("UPDATE meta SET value=value+1 WHERE name='runtime_epoch'")
            connection.execute('DELETE FROM checks')
            connection.execute("UPDATE meta SET value=0 WHERE name='maintenance'")

    @staticmethod
    def _editable(connection, owner, key):
        row = Store._attempt(connection, key, owner=owner)
        if row['kind'] != 'EDIT' or row['state'] not in TERMINAL:
            raise StoreError('INVALID_STATE')
        return row

    def record_candidate(self, owner, key, artifact_sha, candidate_sha):
        owner = self._owner(owner)
        artifact_sha = _digest(artifact_sha, 'INVALID_ARTIFACT')
        candidate_sha = _digest(candidate_sha, 'INVALID_CANDIDATE')
        with self._db(writable=True) as connection:
            row = self._editable(connection, owner, key)
            if row['state'] != 'COMPLETED' or row['artifact_sha'] != artifact_sha:
                raise StoreError('INVALID_ARTIFACT')
            if row['candidate_sha'] == candidate_sha:
                return _public_row(row)
            if row['disposition'] is not None:
                raise StoreError('INVALID_STATE')
            return _public_row(self._update(connection, row, candidate_sha=candidate_sha, disposition='CANDIDATE'))

    def record_tested(self, owner, key, candidate_sha, check_record):
        owner = self._owner(owner)
        candidate_sha = _digest(candidate_sha, 'INVALID_CANDIDATE')
        record = _mapping(check_record, 'INVALID_TEST_RECORD')
        if record.get('passed') is not True:
            raise StoreError('INVALID_TEST_RECORD')
        with self._db(writable=True) as connection:
            row = self._editable(connection, owner, key)
            if row['candidate_sha'] != candidate_sha:
                raise StoreError('INVALID_CANDIDATE')
            if row['disposition'] not in ('CANDIDATE', 'TESTED', 'NOT_APPLIED'):
                raise StoreError('INVALID_STATE')
            return _public_row(self._update(connection, row, tested_sha=candidate_sha, check_record=_json(record),
                                             disposition='TESTED'))

    def begin_apply(self, owner, key, candidate_sha, journal_id):
        owner = self._owner(owner)
        candidate_sha = _digest(candidate_sha, 'INVALID_CANDIDATE')
        journal_id = _identifier(journal_id, 'INVALID_JOURNAL')
        with self._db(writable=True) as connection:
            row = self._editable(connection, owner, key)
            if row['tested_sha'] != candidate_sha or row['candidate_sha'] != candidate_sha:
                raise StoreError('INVALID_CANDIDATE')
            if row['disposition'] in ('APPLYING', 'RECONCILE_REQUIRED'):
                raise StoreError('RECONCILE_REQUIRED')
            if row['disposition'] not in ('TESTED', 'NOT_APPLIED'):
                raise StoreError('INVALID_STATE')
            requested = len(_decoded(row['request'])['edit_paths'])
            held = connection.execute('SELECT count(*) FROM edit_owners WHERE attempt=?', (row['id'],)).fetchone()[0]
            if not requested or held != requested:
                raise StoreError('EDIT_SCOPE_BUSY')
            return _public_row(self._update(connection, row, disposition='APPLYING', journal_id=journal_id,
                                             apply_report=None))

    def finish_disposition(self, owner, key, disposition, apply_report):
        owner = self._owner(owner)
        report = _mapping(apply_report, 'INVALID_APPLY_REPORT')
        if disposition not in ('APPLIED', 'DISCARDED', 'NOT_APPLIED', 'RECONCILE_REQUIRED'):
            raise StoreError('INVALID_APPLY_REPORT')
        reported = report.get('disposition', report.get('state', report.get('status')))
        if disposition != 'DISCARDED' and disposition != reported:
            raise StoreError('INVALID_APPLY_REPORT')
        with self._db(writable=True) as connection:
            row = self._editable(connection, owner, key)
            current = row['disposition']
            if current in ('APPLIED', 'DISCARDED'):
                if current != disposition:
                    raise StoreError('INVALID_STATE')
                return _public_row(row)
            pending = current in ('APPLYING', 'RECONCILE_REQUIRED')
            if disposition == 'DISCARDED' and pending:
                raise StoreError('RECONCILE_REQUIRED')
            if disposition != 'DISCARDED' and not pending:
                raise StoreError('INVALID_STATE')
            if disposition in ('APPLIED', 'DISCARDED'):
                connection.execute('DELETE FROM edit_owners WHERE attempt=?', (row['id'],))
            return _public_row(self._update(connection, row, disposition=disposition, apply_report=_json(report)))
