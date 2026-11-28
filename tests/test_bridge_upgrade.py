"""Recovery tests using a frozen, independent copy of the v1 SQLite schema."""

from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from ct_bridge_files import AttemptKey, BridgeError, locate_workspace, publish_file, recovery_pending, verify_layout


# SQLite DDL from scripts/ct_bridge_store.py at 9c5f745, independent of the upgrader.
V1_DDL = (
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


class BridgeUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='ct-upgrade-'))).resolve()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True, capture_output=True)
        self.paths = locate_workspace(self.root)
        self.clock = lambda: 100.0
        self.manifests = {
            'leader_manifest': (self.root / 'leader' / 'install-manifest.json').as_posix(),
            'worker_manifest': (self.root / 'worker' / 'install-manifest.json').as_posix(),
        }
        self.key = AttemptKey('CT-v1', 1)
        self.request = b'# v1 request\n'
        self.result = b'# v1 result\n'

    def seed_v1(self, *, open_round=False):
        verify_layout(self.paths, create=True)
        with closing(sqlite3.connect(self.paths.db)) as db, db:
            for ddl in V1_DDL:
                db.execute(ddl)
            db.execute('PRAGMA application_id=0x43544231')
            db.execute('PRAGMA user_version=1')
            db.execute("""INSERT INTO group_state VALUES
                (1,'codex','leader-tab',?,8,1,11,1,12,7,'antigravity','worker-tab',?,'old-invitation',200)""",
                       tuple(self.manifests.values()))
            db.execute("""INSERT INTO rounds VALUES
                ('R-v1',8,'codex','leader-tab',?,1,?,'round evidence')""",
                       (self.manifests['leader_manifest'], None if open_round else 7))
            db.execute("""INSERT INTO native_participants VALUES
                ('R-v1','N-v1','runtime-handle',6,'native evidence')""")
            db.execute("""INSERT INTO tasks VALUES ('CT-v1','R-v1',7,'antigravity','worker-tab',NULL)""")
            db.execute("""INSERT INTO attempts
                (task_id,attempt_no,publish_id,state,mode,kind,request_sha256,request_size,
                 scope,baseline,desktop_epoch,auto_epoch,pair_epoch,leader_epoch,policy_stamp,created_at,
                 execution_hash,started_at,finished_at,result_id,result_sha256,result_size,ack_at,evidence)
                VALUES ('CT-v1',1,'publish-v1','COMPLETED','auto','READ',?,?,'[]','{}',11,12,7,8,'stamp',2,
                        'old-execution',3,4,'receipt-v1',?,?,5,'receipt evidence')""",
                       (sha256(self.request).hexdigest(), len(self.request),
                        sha256(self.result).hexdigest(), len(self.result)))
            db.execute("INSERT INTO read_grants VALUES ('CT-v1',1,'old-grant',200)")
            db.execute("INSERT INTO waiter VALUES (1,'old-nonce',8,7,12,11,'stamp',200)")
        publish_file(self.paths, self.key, 'request', self.request)
        publish_file(self.paths, self.key, 'result', self.result)

    def migrate(self):
        from ct_bridge_upgrade import migrate_v1

        return migrate_v1(self.paths, clock=self.clock, **self.manifests)

    def read_db(self, sql, *, path=None):
        path = path or self.paths.db
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            return db.execute(sql).fetchall()

    def test_v1_read_is_unchanged_until_explicit_migration(self):
        from ct_bridge_store import Store

        self.seed_v1()
        before = self.paths.db.read_bytes()
        with Store(self.paths).read_compatible() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
        self.assertEqual(self.paths.db.read_bytes(), before)
        self.assertFalse((self.paths.data.parent / 'bridge-recovery').exists())

    def test_migrate_preserves_closed_history_and_reuses_one_valid_pair(self):
        self.seed_v1()
        result = self.migrate()
        self.assertEqual(result['state'], 'MIGRATED')
        self.assertFalse(result['needs_repair'])
        self.assertEqual(self.read_db('PRAGMA user_version'), [(2,)])
        self.assertEqual(self.read_db('PRAGMA foreign_key_check'), [])
        self.assertEqual(self.read_db('SELECT id,closed_at,evidence FROM rounds'), [('R-v1', 7, 'round evidence')])
        self.assertEqual(self.read_db("SELECT participant_id,handle,evidence,role_variant FROM participants WHERE executor='native'"),
                         [('N-v1', 'runtime-handle', 'native evidence', None)])
        self.assertEqual(self.read_db('SELECT id,legacy_pair_epoch FROM tasks'), [('CT-v1', 7)])
        self.assertEqual(self.read_db('SELECT work_item_id,state FROM work_items'), [('CT-v1', 'historical')])
        self.assertEqual(
            self.read_db('SELECT task_id,attempt_no,publish_id,request_sha256,result_id,result_sha256,ack_at,evidence FROM attempts'),
            [('CT-v1', 1, 'publish-v1', sha256(self.request).hexdigest(), 'receipt-v1',
              sha256(self.result).hexdigest(), 5, 'receipt evidence')])
        self.assertEqual(self.read_db('SELECT platform,chat_id,manifest FROM workers WHERE retired_at IS NULL'),
                         [('antigravity', 'worker-tab', self.manifests['worker_manifest'])])
        self.assertEqual(self.read_db('SELECT enabled,desktop_epoch,auto_enabled,auto_epoch,legacy_ticket_hash FROM peers'),
                         [(1, 11, 1, 12, None)])
        self.assertEqual(self.read_db('SELECT * FROM read_grants'), [])
        self.assertEqual(self.read_db('SELECT * FROM waiter'), [])
        self.assertEqual((self.paths.data / 'CT-v1/attempt-1/result.md').read_bytes(), self.result)
        self.assertEqual(self.migrate()['state'], 'ALREADY_V2')
        self.assertEqual(self.read_db('SELECT count(*) FROM attempts'), [(1,)])

    def test_unverified_pair_requires_rejoin_and_preserves_mode(self):
        from ct_bridge_upgrade import migrate_v1

        self.seed_v1()
        result = migrate_v1(self.paths, clock=self.clock)
        self.assertTrue(result['needs_repair'])
        self.assertEqual(self.read_db('SELECT * FROM workers WHERE retired_at IS NULL'), [])
        self.assertEqual(self.read_db('SELECT enabled,auto_enabled FROM peers'), [(1, 1)])
        self.assertEqual(self.read_db('SELECT result_id FROM attempts'), [('receipt-v1',)])

    def test_migrate_rejects_open_round_without_backup(self):
        self.seed_v1(open_round=True)
        backup = self.paths.data.parent / 'bridge-recovery'
        with self.assertRaisesRegex(BridgeError, 'ROUND_BUSY'):
            self.migrate()
        self.assertFalse(backup.exists())
        self.assertEqual(self.read_db('PRAGMA user_version'), [(1,)])

    def test_migrate_rejects_wrong_schema_without_backup(self):
        self.seed_v1()
        with closing(sqlite3.connect(self.paths.db)) as db:
            db.execute('DROP TABLE waiter')
        with self.assertRaisesRegex(BridgeError, 'STORE_UNAVAILABLE'):
            self.migrate()
        self.assertFalse((self.paths.data.parent / 'bridge-recovery').exists())

    def test_migration_fault_leaves_v1_and_retry_is_idempotent(self):
        import ct_bridge_upgrade as upgrade

        self.seed_v1()
        with patch.object(upgrade, '_check_migration', side_effect=RuntimeError('interrupted')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                self.migrate()
        self.assertEqual(self.read_db('PRAGMA user_version'), [(1,)])
        self.assertEqual(self.read_db('SELECT result_id FROM attempts'), [('receipt-v1',)])
        self.assertEqual(self.migrate()['state'], 'MIGRATED')
        self.assertEqual(self.read_db('SELECT count(*) FROM attempts'), [(1,)])

    def test_retry_migration_captures_new_v1_generation(self):
        import ct_bridge_upgrade as upgrade

        self.seed_v1()
        with patch.object(upgrade, '_check_migration', side_effect=RuntimeError('migration interrupted')):
            with self.assertRaisesRegex(RuntimeError, 'migration interrupted'):
                self.migrate()
        # The old runtime can still legitimately finish work on the v1 store.
        with closing(sqlite3.connect(self.paths.db)) as db, db:
            db.execute("INSERT INTO rounds VALUES ('R-later',8,'codex','leader-tab',?,10,11,'later v1 evidence')",
                       (self.manifests['leader_manifest'],))
        self.assertEqual(self.migrate()['state'], 'MIGRATED')
        self.assertEqual(upgrade.restore_v1(self.paths, clock=self.clock)['state'], 'RESTORED_V1')
        self.assertEqual(self.read_db("SELECT evidence FROM rounds WHERE id='R-later'"), [('later v1 evidence',)])

    def test_rollback_fences_admission_before_archive_and_retries_frozen_generation(self):
        import ct_bridge_upgrade as upgrade
        from ct_bridge_store import Store

        self.seed_v1()
        self.migrate()
        self.add_v2_attempt()
        archive = upgrade._archive

        def interrupt(*args, **kwargs):
            archive(*args, **kwargs)
            raise RuntimeError('interrupted after archiving')

        with patch.object(upgrade, '_archive', side_effect=interrupt):
            with self.assertRaisesRegex(RuntimeError, 'interrupted after archiving'):
                upgrade.restore_v1(self.paths, clock=self.clock)
        self.assertTrue(recovery_pending(self.paths))
        with self.assertRaisesRegex(BridgeError, 'RECOVERY_IN_PROGRESS'):
            with Store(self.paths).write() as db:
                db.execute('UPDATE group_state SET leader_epoch=9')
        self.assertEqual(upgrade.restore_v1(self.paths, clock=self.clock)['state'], 'RESTORED_V1')
        archived = self.paths.data.parent / 'bridge-recovery' / 'v2' / 'bridge.sqlite3'
        self.assertEqual(self.read_db('SELECT result_id FROM attempts ORDER BY attempt_no', path=archived),
                         [('receipt-v1',), ('receipt-v2',)])

    def add_v2_attempt(self):
        from ct_bridge_store import Store

        with Store(self.paths).write() as db:
            db.execute("""INSERT INTO attempts
                (task_id, attempt_no, publish_id, state, mode, kind, request_sha256, request_size,
                 scope, baseline, desktop_epoch, auto_epoch, legacy_pair_epoch, leader_epoch, policy_stamp,
                 created_at, finished_at, result_id, result_sha256, result_size, ack_at, evidence, worker_id)
                SELECT task_id, 2, 'publish-v2', state, mode, kind, request_sha256, request_size, scope, baseline,
                       desktop_epoch, auto_epoch, legacy_pair_epoch, leader_epoch, policy_stamp, 12, 13,
                       'receipt-v2', ?, ?, 14, 'v2 evidence', worker_id
                FROM attempts WHERE task_id='CT-v1' AND attempt_no=1""",
                       (sha256(b'new receipt').hexdigest(), len(b'new receipt')))
        publish_file(self.paths, AttemptKey('CT-v1', 2), 'request', self.request)
        publish_file(self.paths, AttemptKey('CT-v1', 2), 'result', b'new receipt')

    def test_restore_reuses_backup_and_preserves_v2_receipt_archive(self):
        from ct_bridge_store import Store
        from ct_bridge_upgrade import restore_v1

        self.seed_v1()
        self.migrate()
        self.add_v2_attempt()
        # v2 cleanup may remove content needed by the restored v1 history.
        (self.paths.data / 'CT-v1/attempt-1/result.md').unlink()
        with Store(self.paths).write() as db:
            db.execute("UPDATE tasks SET cleaned_at=15 WHERE id='CT-v1'")
        restored = restore_v1(self.paths, clock=self.clock)
        self.assertEqual(restored['state'], 'RESTORED_V1')
        self.assertTrue(recovery_pending(self.paths))
        self.assertEqual(self.read_db('PRAGMA user_version'), [(1,)])
        self.assertEqual(self.read_db('SELECT result_id FROM attempts'), [('receipt-v1',)])
        self.assertEqual(self.read_db('SELECT * FROM waiter'), [])
        self.assertEqual(self.read_db('SELECT * FROM read_grants'), [])
        self.assertEqual(self.read_db('SELECT invite_hash FROM group_state'), [(None,)])
        self.assertEqual((self.paths.data / 'CT-v1/attempt-1/result.md').read_bytes(), self.result)
        archive = self.paths.data.parent / 'bridge-recovery' / 'v2'
        self.assertEqual(self.read_db('SELECT result_id FROM attempts ORDER BY attempt_no', path=archive / 'bridge.sqlite3'),
                         [('receipt-v1',), ('receipt-v2',)])
        self.assertEqual((archive / 'files/CT-v1/attempt-2/result.md').read_bytes(), b'new receipt')
        self.assertFalse((self.paths.data / 'CT-v1/attempt-2/result.md').exists())
        manifest = json.loads((archive / 'manifest.json').read_bytes())
        self.assertEqual(manifest['database_sha256'], sha256((archive / 'bridge.sqlite3').read_bytes()).hexdigest())
        self.assertEqual(restore_v1(self.paths, clock=self.clock)['state'], 'RESTORED_V1')
        with self.assertRaisesRegex(BridgeError, 'RECOVERY'):
            with Store(self.paths).write():
                pass

    def test_restore_fresh_v2_creates_empty_baseline_group(self):
        from ct_bridge_store import Store
        from ct_bridge_upgrade import restore_v1

        with Store(self.paths).write():
            pass
        restored = restore_v1(self.paths, clock=self.clock)
        self.assertEqual(restored['state'], 'RESTORED_V1')
        self.assertEqual(self.read_db('PRAGMA user_version'), [(1,)])
        self.assertEqual(self.read_db('SELECT leader_chat,desktop_enabled,auto_enabled FROM group_state'), [(None, 0, 0)])
        self.assertEqual(self.read_db('SELECT * FROM rounds'), [])
        self.assertTrue(recovery_pending(self.paths))

    def test_rollback_archives_cancelled_publication_without_markdown(self):
        from ct_bridge_store import Store
        from ct_bridge_upgrade import restore_v1

        self.seed_v1()
        self.migrate()
        with Store(self.paths).write() as db:
            db.execute('''INSERT INTO attempts
                (task_id,attempt_no,worker_id,publish_id,state,mode,kind,request_sha256,request_size,
                 scope,baseline,desktop_epoch,auto_epoch,leader_epoch,policy_stamp,created_at,finished_at)
                SELECT task_id,2,worker_id,'interrupted','CANCELED',mode,kind,request_sha256,request_size,
                       scope,baseline,desktop_epoch,auto_epoch,leader_epoch,policy_stamp,12,13
                FROM attempts WHERE task_id='CT-v1' AND attempt_no=1''')
        self.assertEqual(restore_v1(self.paths, clock=self.clock)['state'], 'RESTORED_V1')
        archive = self.paths.data.parent / 'bridge-recovery' / 'v2' / 'bridge.sqlite3'
        self.assertEqual(self.read_db('SELECT state FROM attempts WHERE attempt_no=2', path=archive), [('CANCELED',)])

    def test_restore_retry_before_and_after_database_replace(self):
        import ct_bridge_upgrade as upgrade

        self.seed_v1()
        self.migrate()
        real_install = upgrade._install_database

        def replace_then_interrupt(*args, **kwargs):
            real_install(*args, **kwargs)
            raise RuntimeError('after replace')

        for injection, expected_version in (
            (RuntimeError('before replace'), 2),
            (replace_then_interrupt, 1),
        ):
            with self.subTest(expected_version=expected_version):
                with patch.object(upgrade, '_install_database', side_effect=injection):
                    with self.assertRaisesRegex(RuntimeError, 'replace'):
                        upgrade.restore_v1(self.paths, clock=self.clock)
                self.assertTrue(recovery_pending(self.paths))
                self.assertEqual(self.read_db('PRAGMA user_version'), [(expected_version,)])
        self.assertEqual(upgrade.restore_v1(self.paths, clock=self.clock)['state'], 'RESTORED_V1')
        self.assertEqual(self.read_db('SELECT result_id FROM attempts'), [('receipt-v1',)])


if __name__ == '__main__':
    unittest.main()
