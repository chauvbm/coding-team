"""Worktree-local SQLite transactions for the Desktop bridge."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import sqlite3
import time

from ct_bridge_files import BridgeError, WorkspacePaths, database_guard, recovery_pending, verify_layout


PROTOCOL_VERSION = 2
APPLICATION_ID = 0x43544231
_ROUNDS = """CREATE TABLE rounds (
    id TEXT PRIMARY KEY NOT NULL, leader_epoch INTEGER NOT NULL,
    leader_platform TEXT NOT NULL, leader_chat TEXT NOT NULL, leader_manifest TEXT NOT NULL,
    opened_at REAL NOT NULL, closed_at REAL, evidence TEXT
)"""
_OPEN_ROUNDS = "CREATE INDEX open_rounds ON rounds (leader_epoch) WHERE closed_at IS NULL"
_READ_GRANTS = """CREATE TABLE read_grants (
    task_id TEXT NOT NULL, attempt_no INTEGER NOT NULL,
    token_hash TEXT NOT NULL, expires_at REAL NOT NULL,
    PRIMARY KEY (task_id, attempt_no),
    FOREIGN KEY (task_id, attempt_no) REFERENCES attempts(task_id, attempt_no)
)"""
SCHEMA_V1 = (
    """CREATE TABLE group_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        leader_platform TEXT, leader_chat TEXT, leader_manifest TEXT,
        leader_epoch INTEGER NOT NULL DEFAULT 0 CHECK (leader_epoch >= 0),
        desktop_enabled INTEGER NOT NULL DEFAULT 0 CHECK (desktop_enabled IN (0, 1)),
        desktop_epoch INTEGER NOT NULL DEFAULT 0 CHECK (desktop_epoch >= 0),
        auto_enabled INTEGER NOT NULL DEFAULT 0 CHECK (auto_enabled IN (0, 1)),
        auto_epoch INTEGER NOT NULL DEFAULT 0 CHECK (auto_epoch >= 0),
        pair_epoch INTEGER NOT NULL DEFAULT 0 CHECK (pair_epoch >= 0),
        worker_platform TEXT, worker_chat TEXT, worker_manifest TEXT,
        invite_hash TEXT, invite_expires_at REAL,
        CHECK ((leader_platform IS NULL) = (leader_chat IS NULL)
            AND (leader_chat IS NULL) = (leader_manifest IS NULL)),
        CHECK ((worker_platform IS NULL) = (worker_chat IS NULL)
            AND (worker_chat IS NULL) = (worker_manifest IS NULL)),
        CHECK ((invite_hash IS NULL) = (invite_expires_at IS NULL))
    )""",
    _ROUNDS,
    _OPEN_ROUNDS,
    """CREATE TABLE native_participants (
        round_id TEXT NOT NULL REFERENCES rounds(id), id TEXT NOT NULL,
        handle TEXT, finished_at REAL, evidence TEXT,
        PRIMARY KEY (round_id, id)
    )""",
    """CREATE TABLE tasks (
        id TEXT PRIMARY KEY NOT NULL, round_id TEXT NOT NULL REFERENCES rounds(id),
        pair_epoch INTEGER NOT NULL, worker_platform TEXT NOT NULL, worker_chat TEXT NOT NULL,
        cleaned_at REAL
    )""",
    """CREATE TABLE attempts (
        task_id TEXT NOT NULL REFERENCES tasks(id), attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
        publish_id TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL CHECK (state IN ('PUBLISHING', 'REVOKED', 'WAITING_FOR_USER',
            'QUEUED', 'CLAIMED', 'RUNNING', 'NEEDS_USER', 'OUTCOME_UNKNOWN',
            'COMPLETED', 'FAILED', 'CANCELED')),
        mode TEXT NOT NULL CHECK (mode IN ('manual', 'auto')),
        kind TEXT NOT NULL CHECK (kind IN ('READ', 'EDIT')),
        request_sha256 TEXT NOT NULL, request_size INTEGER NOT NULL CHECK (request_size >= 0),
        scope TEXT NOT NULL, baseline TEXT NOT NULL,
        desktop_epoch INTEGER NOT NULL, auto_epoch INTEGER NOT NULL,
        pair_epoch INTEGER NOT NULL, leader_epoch INTEGER NOT NULL, policy_stamp TEXT NOT NULL,
        created_at REAL NOT NULL, claim_hash TEXT, claim_expires_at REAL,
        execution_hash TEXT, started_at REAL, finished_at REAL, result_id TEXT,
        result_sha256 TEXT, result_size INTEGER, reason TEXT, ack_at REAL, evidence TEXT,
        PRIMARY KEY (task_id, attempt_no)
    )""",
    """CREATE UNIQUE INDEX running_slot ON attempts ((1))
        WHERE state IN ('RUNNING', 'NEEDS_USER', 'OUTCOME_UNKNOWN')""",
    "CREATE INDEX delivery_queue ON attempts (created_at) WHERE state = 'QUEUED'",
    _READ_GRANTS,
    """CREATE TABLE waiter (
        id INTEGER PRIMARY KEY CHECK (id = 1), nonce TEXT NOT NULL,
        leader_epoch INTEGER NOT NULL, pair_epoch INTEGER NOT NULL, auto_epoch INTEGER NOT NULL,
        desktop_epoch INTEGER NOT NULL, policy_stamp TEXT NOT NULL, expires_at REAL NOT NULL
    )""",
)

SCHEMA_V2 = (
    """CREATE TABLE group_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        leader_platform TEXT, leader_chat TEXT, leader_manifest TEXT,
        leader_epoch INTEGER NOT NULL DEFAULT 0 CHECK (leader_epoch >= 0),
        baseline_archive TEXT,
        CHECK ((leader_platform IS NULL) = (leader_chat IS NULL)
            AND (leader_chat IS NULL) = (leader_manifest IS NULL))
    )""",
    """CREATE TABLE peers (
        platform TEXT PRIMARY KEY NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
        desktop_epoch INTEGER NOT NULL DEFAULT 0 CHECK (desktop_epoch >= 0),
        auto_enabled INTEGER NOT NULL DEFAULT 0 CHECK (auto_enabled IN (0, 1)),
        auto_epoch INTEGER NOT NULL DEFAULT 0 CHECK (auto_epoch >= 0),
        join_id TEXT, join_expires_at REAL, legacy_ticket_hash TEXT, legacy_expires_at REAL,
        CHECK ((join_id IS NULL) = (join_expires_at IS NULL)),
        CHECK ((legacy_ticket_hash IS NULL) = (legacy_expires_at IS NULL))
    )""",
    """CREATE TABLE workers (
        worker_id TEXT PRIMARY KEY NOT NULL, platform TEXT NOT NULL REFERENCES peers(platform),
        chat_id TEXT NOT NULL, manifest TEXT, leader_epoch INTEGER NOT NULL CHECK (leader_epoch >= 0),
        registered_at REAL NOT NULL, retired_at REAL, legacy_pair_epoch INTEGER,
        CHECK (manifest IS NOT NULL OR retired_at IS NOT NULL)
    )""",
    """CREATE UNIQUE INDEX active_worker_chat ON workers (platform, chat_id, leader_epoch)
        WHERE retired_at IS NULL""",
    _ROUNDS,
    _OPEN_ROUNDS,
    """CREATE TABLE participants (
        round_id TEXT NOT NULL REFERENCES rounds(id), participant_id TEXT NOT NULL, work_item_id TEXT,
        executor TEXT NOT NULL CHECK (executor IN ('desktop', 'native', 'leader')),
        worker_id TEXT REFERENCES workers(worker_id), handle TEXT, role_variant TEXT, assignment TEXT,
        observer INTEGER NOT NULL DEFAULT 0 CHECK (observer IN (0,1)),
        started_at REAL, finished_at REAL, evidence TEXT, replaces_participant_id TEXT,
        PRIMARY KEY (round_id, participant_id),
        UNIQUE (round_id, work_item_id, participant_id),
        UNIQUE (round_id, participant_id, worker_id),
        FOREIGN KEY (round_id, work_item_id) REFERENCES work_items(round_id, work_item_id)
            DEFERRABLE INITIALLY DEFERRED,
        FOREIGN KEY (round_id, replaces_participant_id) REFERENCES participants(round_id, participant_id),
        CHECK ((executor = 'desktop') = (worker_id IS NOT NULL)),
        CHECK (observer=0 OR (executor='native' AND work_item_id IS NULL AND assignment IS NULL))
    )""",
    """CREATE TABLE work_items (
        round_id TEXT NOT NULL REFERENCES rounds(id), work_item_id TEXT NOT NULL,
        owner_participant_id TEXT NOT NULL, depends_on TEXT NOT NULL DEFAULT '{}',
        state TEXT NOT NULL CHECK (state IN ('active', 'accepted', 'cancelled', 'historical')),
        acceptance_id TEXT UNIQUE, snapshot TEXT, evidence TEXT,
        PRIMARY KEY (round_id, work_item_id),
        FOREIGN KEY (round_id, work_item_id, owner_participant_id)
            REFERENCES participants(round_id, work_item_id, participant_id)
            DEFERRABLE INITIALLY DEFERRED,
        CHECK ((state = 'accepted') = (acceptance_id IS NOT NULL))
    )""",
    """CREATE TABLE tasks (
        id TEXT PRIMARY KEY NOT NULL, round_id TEXT NOT NULL REFERENCES rounds(id),
        participant_id TEXT NOT NULL, worker_id TEXT NOT NULL REFERENCES workers(worker_id),
        cleaned_at REAL, legacy_pair_epoch INTEGER,
        UNIQUE (id, worker_id),
        UNIQUE (round_id, participant_id),
        FOREIGN KEY (round_id, participant_id, worker_id)
            REFERENCES participants(round_id, participant_id, worker_id)
    )""",
    """CREATE TABLE attempts (
        task_id TEXT NOT NULL, attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
        worker_id TEXT NOT NULL, publish_id TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL CHECK (state IN ('PUBLISHING', 'REVOKED', 'WAITING_FOR_USER',
            'QUEUED', 'CLAIMED', 'RUNNING', 'NEEDS_USER', 'OUTCOME_UNKNOWN',
            'COMPLETED', 'FAILED', 'CANCELED')),
        mode TEXT NOT NULL CHECK (mode IN ('manual', 'auto')),
        kind TEXT NOT NULL CHECK (kind IN ('READ', 'EDIT')),
        request_sha256 TEXT NOT NULL, request_size INTEGER NOT NULL CHECK (request_size >= 0),
        scope TEXT NOT NULL, baseline TEXT NOT NULL, assignment TEXT, contract_digest TEXT, dispatch_hash TEXT,
        desktop_epoch INTEGER NOT NULL, auto_epoch INTEGER NOT NULL, leader_epoch INTEGER NOT NULL,
        legacy_pair_epoch INTEGER, policy_stamp TEXT NOT NULL,
        created_at REAL NOT NULL, claim_hash TEXT, claim_expires_at REAL,
        execution_hash TEXT, started_at REAL, finished_at REAL, result_id TEXT,
        result_sha256 TEXT, result_size INTEGER, reason TEXT, ack_at REAL, ack_evidence TEXT, evidence TEXT,
        PRIMARY KEY (task_id, attempt_no),
        FOREIGN KEY (task_id, worker_id) REFERENCES tasks(id, worker_id)
    )""",
    """CREATE UNIQUE INDEX worker_slot ON attempts (worker_id)
        WHERE state IN ('PUBLISHING', 'WAITING_FOR_USER', 'QUEUED', 'CLAIMED',
                        'RUNNING', 'NEEDS_USER', 'OUTCOME_UNKNOWN')""",
    "CREATE INDEX delivery_queue ON attempts (worker_id, created_at) WHERE state = 'QUEUED'",
    _READ_GRANTS,
    """CREATE TABLE waiter (
        worker_id TEXT PRIMARY KEY NOT NULL REFERENCES workers(worker_id),
        nonce_hash TEXT NOT NULL, expires_at REAL NOT NULL,
        leader_epoch INTEGER NOT NULL, desktop_epoch INTEGER NOT NULL,
        auto_epoch INTEGER NOT NULL, policy_stamp TEXT NOT NULL
    )""",
    """CREATE TABLE scope_reservations (
        round_id TEXT NOT NULL, participant_id TEXT NOT NULL,
        task_id TEXT NOT NULL DEFAULT '', attempt_no INTEGER NOT NULL DEFAULT 0,
        access TEXT NOT NULL CHECK (access IN ('read', 'write')),
        path_key TEXT NOT NULL, path TEXT NOT NULL,
        PRIMARY KEY (round_id, participant_id, task_id, attempt_no, access, path_key),
        FOREIGN KEY (round_id, participant_id) REFERENCES participants(round_id, participant_id),
        CHECK ((task_id = '' AND attempt_no = 0) OR (task_id <> '' AND attempt_no > 0))
    )""",
)
_V1_TABLES = {'group_state', 'rounds', 'native_participants', 'tasks', 'attempts', 'read_grants', 'waiter'}
_V2_TABLES = {'group_state', 'peers', 'rounds', 'workers', 'participants', 'work_items',
              'tasks', 'attempts', 'read_grants', 'waiter', 'scope_reservations'}
_V1_INDEXES = {'open_rounds', 'running_slot', 'delivery_queue'}
_V2_INDEXES = {'open_rounds', 'worker_slot', 'delivery_queue', 'active_worker_chat'}
_EXPECTED = {
    version: {('table', name) for name in tables} | {('index', name) for name in indexes}
    for version, tables, indexes in ((1, _V1_TABLES, _V1_INDEXES), (2, _V2_TABLES, _V2_INDEXES))
}


@dataclass(frozen=True)
class StoreState:
    version: int | None
    open_rounds: tuple[str, ...]
    leader: dict | None
    maintenance: bool


def inspect_store(paths: WorkspacePaths) -> StoreState:
    """Inspect installed worktree state without creating or upgrading its database."""
    maintenance = recovery_pending(paths)
    with Store(paths).read_compatible() as connection:
        if connection is None:
            return StoreState(None, (), None, maintenance)
        if (connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok'
                or connection.execute('PRAGMA foreign_key_check').fetchone() is not None):
            raise BridgeError('STORE_UNAVAILABLE')
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        group = dict(connection.execute('SELECT * FROM group_state WHERE id=1').fetchone())
        open_rounds = tuple(row['id'] for row in connection.execute(
            'SELECT id FROM rounds WHERE closed_at IS NULL ORDER BY opened_at,id'))
        return StoreState(version, open_rounds, group, maintenance)


class Store:
    def __init__(self, paths: WorkspacePaths, *, clock=time.time, busy_timeout=2.0):
        self.paths = paths
        self.clock = clock
        self.busy_timeout = busy_timeout

    def read(self):
        """Use a read-only snapshot, yielding None before the first write."""
        return self._transaction(writable=False)

    def poll_read(self):
        """Read status from an already validated worktree during an idle wait."""
        return self._transaction(writable=False, validate=False)

    def write(self):
        """Serialize authorization checks and updates from both apps."""
        return self._transaction(writable=True)

    def read_compatible(self):
        """Read isolation for installer diagnostics and migration snapshots."""
        return self._transaction(writable=False, compatible=True)

    def write_compatible(self):
        """Mutate an existing schema during recovery under the component locks."""
        return self._transaction(writable=True, compatible=True)

    @contextmanager
    def _transaction(self, *, writable, validate=True, compatible=False):
        if validate:
            verify_layout(self.paths, create=writable and not compatible)
        with database_guard(self.paths) as exists:
            with self._connect(exists, writable=writable, compatible=compatible) as connection:
                yield connection

    @contextmanager
    def _connect(self, exists, *, writable, compatible):
        if not exists and (not writable or compatible):
            yield None
            return
        connection = None
        try:
            mode = ('rw' if compatible else 'rwc') if writable else 'ro'
            connection = sqlite3.connect(f'{self.paths.db.as_uri()}?mode={mode}', uri=True, isolation_level=None,
                                         timeout=self.busy_timeout)
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA trusted_schema=OFF')
            if connection.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
                raise BridgeError('STORE_UNAVAILABLE')
            if writable:
                connection.execute('PRAGMA synchronous=FULL')
            connection.execute('BEGIN IMMEDIATE' if writable else 'BEGIN')
            if writable and not compatible and recovery_pending(self.paths):
                raise BridgeError('RECOVERY_IN_PROGRESS')
            self._schema(connection, writable=writable, compatible=compatible)
            yield connection
            connection.commit()
        except sqlite3.Error as error:
            code = getattr(error, 'sqlite_errorcode', 0) & 0xFF
            reason = 'STORE_BUSY' if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) else 'STORE_UNAVAILABLE'
            raise BridgeError(reason) from None
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _schema(connection, *, writable, compatible):
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        app_id = connection.execute('PRAGMA application_id').fetchone()[0]
        objects = {(row['type'], row['name']) for row in connection.execute(
            "SELECT type, name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'")}
        if writable and not compatible and version == 0 and app_id == 0 and not objects:
            for statement in SCHEMA_V2:
                connection.execute(statement)
            connection.execute('INSERT INTO group_state (id) VALUES (1)')
            connection.execute(f'PRAGMA application_id={APPLICATION_ID}')
            connection.execute(f'PRAGMA user_version={PROTOCOL_VERSION}')
            return
        versions = _EXPECTED if compatible else (PROTOCOL_VERSION,)
        if version not in versions or app_id != APPLICATION_ID or objects != _EXPECTED[version]:
            raise BridgeError('STORE_UNAVAILABLE')
        if connection.execute('SELECT id FROM group_state WHERE id=1').fetchone() is None:
            raise BridgeError('STORE_UNAVAILABLE')
