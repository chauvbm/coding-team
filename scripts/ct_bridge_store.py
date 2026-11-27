"""Worktree-local SQLite transactions for the Desktop bridge."""

from __future__ import annotations

from contextlib import contextmanager
import sqlite3
import time

from ct_bridge_files import BridgeError, WorkspacePaths, database_guard, verify_layout


PROTOCOL_VERSION = 1
_APPLICATION_ID = 0x43544231
_SCHEMA = (
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
    """CREATE TABLE rounds (
        id TEXT PRIMARY KEY NOT NULL, leader_epoch INTEGER NOT NULL,
        leader_platform TEXT NOT NULL, leader_chat TEXT NOT NULL, leader_manifest TEXT NOT NULL,
        opened_at REAL NOT NULL, closed_at REAL, evidence TEXT
    )""",
    "CREATE INDEX open_rounds ON rounds (leader_epoch) WHERE closed_at IS NULL",
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
    """CREATE TABLE read_grants (
        task_id TEXT NOT NULL, attempt_no INTEGER NOT NULL,
        token_hash TEXT NOT NULL, expires_at REAL NOT NULL,
        PRIMARY KEY (task_id, attempt_no),
        FOREIGN KEY (task_id, attempt_no) REFERENCES attempts(task_id, attempt_no)
    )""",
    """CREATE TABLE waiter (
        id INTEGER PRIMARY KEY CHECK (id = 1), nonce TEXT NOT NULL,
        leader_epoch INTEGER NOT NULL, pair_epoch INTEGER NOT NULL, auto_epoch INTEGER NOT NULL,
        desktop_epoch INTEGER NOT NULL, policy_stamp TEXT NOT NULL, expires_at REAL NOT NULL
    )""",
)
_TABLES = {'group_state', 'rounds', 'native_participants', 'tasks', 'attempts', 'read_grants', 'waiter'}
_INDEXES = {'open_rounds', 'running_slot', 'delivery_queue'}


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

    @contextmanager
    def _transaction(self, *, writable, validate=True):
        if validate:
            verify_layout(self.paths, create=writable)
        with database_guard(self.paths) as exists:
            with self._connect(exists, writable=writable) as connection:
                yield connection

    @contextmanager
    def _connect(self, exists, *, writable):
        if not writable and not exists:
            yield None
            return
        connection = None
        try:
            mode = 'rwc' if writable else 'ro'
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
            self._schema(connection, writable=writable)
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
    def _schema(connection, *, writable):
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        app_id = connection.execute('PRAGMA application_id').fetchone()[0]
        objects = {(row['type'], row['name']) for row in connection.execute(
            "SELECT type, name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'")}
        if writable and version == 0 and app_id == 0 and not objects:
            for statement in _SCHEMA:
                connection.execute(statement)
            connection.execute('INSERT INTO group_state (id) VALUES (1)')
            connection.execute(f'PRAGMA application_id={_APPLICATION_ID}')
            connection.execute(f'PRAGMA user_version={PROTOCOL_VERSION}')
            return
        expected = {('table', name) for name in _TABLES} | {('index', name) for name in _INDEXES}
        if version != PROTOCOL_VERSION or app_id != _APPLICATION_ID or objects != expected:
            raise BridgeError('STORE_UNAVAILABLE')
        if connection.execute('SELECT id FROM group_state').fetchone() is None:
            raise BridgeError('STORE_UNAVAILABLE')
