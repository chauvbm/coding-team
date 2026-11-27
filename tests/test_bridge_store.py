"""SQLite transactions shared by the bridge's controller and worker helpers."""

from contextlib import closing
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))


class BridgeStoreTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='ct bridge db '))).resolve()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True, capture_output=True)
        from ct_bridge_store import Store
        from ct_bridge_files import locate_workspace

        self.paths = locate_workspace(self.root)
        self.store = Store(self.paths)

    def test_read_of_fresh_workspace_creates_nothing(self):
        with self.store.read() as connection:
            self.assertIsNone(connection)
        self.assertFalse(self.paths.data.exists())

    def test_schema_refuses_wrong_version_and_partial_db(self):
        from ct_bridge_files import BridgeError

        with self.store.write() as connection:
            self.assertEqual(connection.execute('PRAGMA foreign_keys').fetchone()[0], 1)
        with closing(sqlite3.connect(self.paths.db)) as connection:
            connection.execute('PRAGMA user_version=7')
        with self.assertRaises(BridgeError):
            with self.store.read():
                self.fail('A newer database version may not be interpreted as v1.')
        with closing(sqlite3.connect(self.paths.db)) as connection:
            connection.execute('PRAGMA user_version=1')
            connection.execute('DROP TABLE waiter')
        with self.assertRaises(BridgeError):
            with self.store.read():
                self.fail('An incomplete schema may not be interpreted as an empty bridge.')

    def test_writer_rollback_preserves_last_committed_owner(self):
        with self.store.write() as connection:
            connection.execute("UPDATE group_state SET leader_epoch = 1, leader_platform = 'codex', "
                               "leader_chat = 'first', leader_manifest = 'fixture' WHERE id = 1")
        with self.assertRaisesRegex(RuntimeError, 'simulated failure'):
            with self.store.write() as connection:
                connection.execute("UPDATE group_state SET leader_chat = 'second' WHERE id = 1")
                raise RuntimeError('simulated failure')
        with self.store.read() as connection:
            row = connection.execute('SELECT leader_chat, leader_epoch FROM group_state').fetchone()
            self.assertEqual(tuple(row), ('first', 1))

    def test_idle_read_preserves_state_without_rechecking_entire_worktree(self):
        from unittest.mock import patch
        import ct_bridge_store

        with self.store.write() as connection:
            connection.execute('UPDATE group_state SET leader_epoch = 2 WHERE id = 1')
        before = self.paths.db.read_bytes()
        with patch.object(ct_bridge_store, 'verify_layout', side_effect=AssertionError('full scan in idle poll')):
            with self.store.poll_read() as connection:
                self.assertEqual(connection.execute('SELECT leader_epoch FROM group_state').fetchone()[0], 2)
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute('UPDATE group_state SET leader_epoch = 3 WHERE id = 1')
        self.assertEqual(self.paths.db.read_bytes(), before)

    def test_sqlite_writer_cross_process_serializes(self):
        command = (
            'import sys; from pathlib import Path; '
            'sys.path.insert(0, sys.argv[1]); '
            'from ct_bridge_files import locate_workspace; from ct_bridge_store import Store; '
            'paths=locate_workspace(Path(sys.argv[2])); '
            'context=Store(paths,busy_timeout=0.1).write(); print("attempt", flush=True); '
            'context.__enter__(); context.__exit__(None,None,None)'
        )
        with self.store.write() as connection:
            connection.execute('UPDATE group_state SET leader_epoch = 8 WHERE id = 1')
        with self.store.write() as connection:
            connection.execute('UPDATE group_state SET leader_epoch = 9 WHERE id = 1')
            child = subprocess.run([sys.executable, '-c', command, str(SCRIPTS), str(self.root)],
                                   capture_output=True, text=True, timeout=10)
            self.assertEqual(child.stdout.strip(), 'attempt')
            self.assertNotEqual(child.returncode, 0)
            self.assertIn('STORE_BUSY', child.stderr)
        child = subprocess.run([sys.executable, '-c', command, str(SCRIPTS), str(self.root)],
                               capture_output=True, text=True, timeout=10)
        self.assertEqual(child.returncode, 0, child.stderr)
        with self.store.read() as connection:
            self.assertEqual(connection.execute('SELECT leader_epoch FROM group_state').fetchone()[0], 9)


if __name__ == '__main__':
    unittest.main()
