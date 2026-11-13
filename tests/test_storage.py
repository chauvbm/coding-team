import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import ct_storage

MODULE = Path(ct_storage.__file__)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory()
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name)
        self.state = self.root / 'custom state' / 'policy.json'

    def test_shell_argument_quotes_metacharacters_for_both_shells(self):
        value = 'space apostrophe\' "double" $(echo substituted); & | >'
        with mock.patch.object(ct_storage.os, 'name', 'nt'):
            self.assertEqual(ct_storage.shell_argument(value), "'" + value.replace("'", "''") + "'")
        with mock.patch.object(ct_storage.os, 'name', 'posix'):
            self.assertEqual(shlex.split(ct_storage.shell_argument(value)), [value])

    def test_atomic_replace_failure_preserves_original_and_cleans_temp(self):
        ct_storage.atomic_write(self.state, b'original')
        with mock.patch.object(ct_storage.os, 'replace', side_effect=OSError('fixture replace failure')):
            with self.assertRaises(OSError):
                ct_storage.atomic_write(self.state, b'replacement')
        self.assertEqual(self.state.read_bytes(), b'original')
        self.assertEqual(list(self.state.parent.glob('.*.tmp')), [])
        ct_storage.atomic_write(self.state, b'replacement')
        self.assertEqual(self.state.read_bytes(), b'replacement')

    def test_atomic_fsync_failure_preserves_original_and_cleans_temp(self):
        ct_storage.atomic_write(self.state, b'original')
        with mock.patch.object(ct_storage.os, 'fsync', side_effect=OSError('fixture fsync failure')):
            with self.assertRaises(OSError):
                ct_storage.atomic_write(self.state, b'replacement')
        self.assertEqual(self.state.read_bytes(), b'original')
        self.assertEqual(list(self.state.parent.glob('.*.tmp')), [])

    def start_lock_holder(self):
        ready = self.root / 'lock-ready'
        code = (
            'import importlib.util,pathlib,sys,time;'
            's=importlib.util.spec_from_file_location("ct_storage",sys.argv[1]);'
            'm=importlib.util.module_from_spec(s);s.loader.exec_module(m);'
            'lock=m.locked(sys.argv[2]);lock.__enter__();'
            'pathlib.Path(sys.argv[3]).write_text("ready");time.sleep(30)'
        )
        process = subprocess.Popen([sys.executable, '-c', code, str(MODULE), str(self.state), str(ready)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
        self.addCleanup(cleanup)
        deadline = time.monotonic() + 10
        while not ready.exists():
            if process.poll() is not None:
                self.fail('lock holder failed: ' + process.communicate()[1])
            if time.monotonic() >= deadline:
                self.fail('lock holder startup timeout')
            time.sleep(.02)
        return process

    def test_os_lock_timeout_and_process_exit_release(self):
        ct_storage.atomic_write(self.state, b'{}')
        process = self.start_lock_holder()
        with self.assertRaises(TimeoutError):
            with ct_storage.locked(self.state, timeout=.08):
                self.fail('another process owns the lock')
        process.kill()
        process.communicate(timeout=10)
        with ct_storage.locked(self.state, timeout=1):
            pass
        self.assertTrue(Path(str(self.state) + '.lock').exists())

    def test_os_lock_released_when_body_raises(self):
        with self.assertRaises(RuntimeError):
            with ct_storage.locked(self.state):
                raise RuntimeError('fixture')
        with ct_storage.locked(self.state, timeout=.1):
            pass

    def test_reject_links_symlink_ancestor(self):
        real = self.root / 'real'
        real.mkdir()
        link = self.root / 'link'
        try:
            os.symlink(real, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable')
        with self.assertRaises(ct_storage.LinkedPathError):
            ct_storage.reject_links(link / 'file.txt', self.root)

    def test_reject_links_accepts_plain_and_missing_paths(self):
        plain = self.root / 'a' / 'b'
        plain.mkdir(parents=True)
        (plain / 'f.txt').write_text('x')
        ct_storage.reject_links(plain / 'f.txt', self.root)
        ct_storage.reject_links(self.root / 'a' / 'missing' / 'deep' / 'f.txt', self.root)
        ct_storage.reject_links(self.root, self.root)


if __name__ == '__main__':
    unittest.main()
