"""Real-filesystem tests for the Desktop bridge's transport boundary."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import threading
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import ct_bridge_files as files


class BridgeFileTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Path(__file__).resolve().parents[1] / ('.ct-files-' + uuid.uuid4().hex)
        self.fixture.mkdir(mode=0o777)
        self.addCleanup(self.remove_fixture)
        self.root = self.fixture / 'ngữ cảnh repo'
        self.root.mkdir(mode=0o777)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Bridge files fixture')
        self.git('config', 'user.email', 'bridge@example.test')
        self.git('config', 'core.autocrlf', 'false')
        (self.root / 'source.txt').write_text('fixture\n', encoding='utf-8')
        self.git('add', 'source.txt')
        self.git('commit', '-qm', 'fixture')
        self.paths = files.locate_workspace(self.root)
        self.key = files.AttemptKey('CT-014', 1)

    def remove_fixture(self):
        # Git objects are read-only on Windows; only our isolated fixture is touched.
        def writable_retry(function, path, _exc_info):
            os.chmod(path, stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)
            for retry in range(20):
                try:
                    function(path)
                    return
                except PermissionError as exc:
                    if getattr(exc, 'winerror', None) != 32 or retry == 19:
                        raise
                    time.sleep(0.025)

        self.assertEqual(self.fixture.parent, Path(__file__).resolve().parents[1])
        # onerror is available on every Python version in the test matrix.
        shutil.rmtree(self.fixture, onerror=writable_retry)

    def git(self, *args):
        environment = {key: value for key, value in os.environ.items() if not key.upper().startswith('GIT_')}
        result = subprocess.run(('git', '-C', str(self.root), *args), env=environment, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        return result.stdout

    def test_git_worktrees_get_distinct_data_roots(self):
        child = self.root / 'nested'
        child.mkdir()
        self.assertEqual(files.locate_workspace(child), self.paths)
        second = self.fixture / 'second checkout'
        self.git('worktree', 'add', '-q', '-b', 'fixture-second', str(second))
        other = files.locate_workspace(second)
        self.assertEqual(self.paths.root, self.root.resolve())
        self.assertEqual(other.data, second.resolve() / '.coding-team' / 'bridge')
        self.assertNotEqual(other.data, self.paths.data)

    def test_read_only_layout_of_pristine_workspace_creates_nothing(self):
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*'))
        files.verify_layout(self.paths, create=False)
        self.assertEqual(before, sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*')))
        with self.assertRaises(files.BridgeError):
            files.read_file(self.paths, self.key, 'request', '0' * 64)
        self.assertFalse((self.root / '.coding-team').exists())

    def test_layout_is_owned_ignored_and_preserves_user_ignore(self):
        original = b'# fixture user ignore\nkeep-me\n'
        (self.root / '.gitignore').write_bytes(original)
        files.verify_layout(self.paths, create=True)
        files.verify_layout(self.paths, create=False)
        self.assertEqual((self.root / '.gitignore').read_bytes(), original)
        self.assertEqual(self.git('check-ignore', '.coding-team/bridge/bridge.sqlite3').strip(),
                         b'.coding-team/bridge/bridge.sqlite3')
        self.assertFalse(self.paths.db.exists())

    def test_rejects_unowned_layout_without_modifying_its_bytes(self):
        self.paths.data.mkdir(parents=True)
        innocent = self.paths.data / 'user.txt'
        innocent.write_bytes(b'keep this\n')
        for create in (False, True):
            with self.subTest(create=create), self.assertRaises(files.BridgeError):
                files.verify_layout(self.paths, create=create)
        self.assertEqual(innocent.read_bytes(), b'keep this\n')
        self.assertFalse((self.paths.data / '.gitignore').exists())

    def test_rejects_linked_or_tracked_transport_path(self):
        self.paths.data.mkdir(parents=True)
        tracked = self.paths.data / 'bridge.sqlite3'
        tracked.write_bytes(b'user database')
        self.git('add', '-f', '.coding-team/bridge/bridge.sqlite3')
        with self.assertRaises(files.BridgeError):
            files.verify_layout(self.paths, create=True)
        self.assertEqual(tracked.read_bytes(), b'user database')
        self.assertFalse((self.paths.data / '.gitignore').exists())

    def test_rejects_hardlinked_db_and_sqlite_sidecars(self):
        files.verify_layout(self.paths, create=True)
        original = self.fixture / 'unrelated'
        original.write_bytes(b'outside')
        for suffix in ('', '-journal', '-wal', '-shm'):
            destination = self.paths.db.with_name(self.paths.db.name + suffix)
            os.link(original, destination)
            try:
                with self.subTest(suffix=suffix), self.assertRaises(files.BridgeError):
                    files.verify_layout(self.paths, create=False)
            finally:
                destination.unlink()
        self.assertEqual(original.read_bytes(), b'outside')

    @unittest.skipUnless(os.name == 'nt', 'Windows shared-delete handles')
    def test_disappearing_journal_during_windows_handle_check(self):
        from unittest.mock import patch

        files.verify_layout(self.paths, create=True)
        journal = self.paths.data / 'bridge.sqlite3-journal'
        real_check = files._win_check
        real_validate_tree = files._validate_tree

        def unlink_before_name_check(handle, path, **options):
            if path == journal:
                journal.unlink()
                raise files.BridgeError('PATH_COLLISION')
            return real_check(handle, path, **options)

        def create_journal_after_scan(paths, guard):
            real_validate_tree(paths, guard)
            journal.write_bytes(b'fixture journal')

        with patch.object(files, '_win_check', side_effect=unlink_before_name_check):
            journal.write_bytes(b'fixture journal')
            files.verify_layout(self.paths, create=False)
            self.assertFalse(journal.exists())

            with patch.object(files, '_validate_tree', side_effect=create_journal_after_scan):
                files.verify_layout(self.paths, create=False)
            self.assertFalse(journal.exists())

            journal.write_bytes(b'fixture journal')
            with files.database_guard(self.paths) as present:
                self.assertFalse(present)
            self.assertFalse(journal.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows shared-delete handles')
    def test_journal_unlinked_before_windows_handle_metadata_read(self):
        from contextlib import contextmanager
        from unittest.mock import patch

        files.verify_layout(self.paths, create=True)
        journal = self.paths.data / 'bridge.sqlite3-journal'
        journal.write_bytes(b'fixture journal')
        real_open = files._win_open
        real_validate_tree = files._validate_tree

        @contextmanager
        def unlink_after_open(path, **options):
            with real_open(path, **options) as handle:
                if path == journal:
                    journal.unlink()
                yield handle

        def create_journal_after_scan(paths, guard):
            real_validate_tree(paths, guard)
            journal.write_bytes(b'fixture journal')

        with patch.object(files, '_win_open', side_effect=unlink_after_open):
            files.verify_layout(self.paths, create=False)
            self.assertFalse(journal.exists())

            with patch.object(files, '_validate_tree', side_effect=create_journal_after_scan):
                files.verify_layout(self.paths, create=False)
            self.assertFalse(journal.exists())

            journal.write_bytes(b'fixture journal')
            with files.database_guard(self.paths) as present:
                self.assertFalse(present)
        self.assertFalse(journal.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows handle link count')
    def test_zero_link_handle_still_rejects_an_existing_path_or_main_database(self):
        from contextlib import contextmanager
        import ctypes
        from unittest.mock import patch

        files.verify_layout(self.paths, create=True)
        journal = self.paths.data / 'bridge.sqlite3-journal'
        real_open = files._win_open
        real_info = files._Info

        def zero_link_info(handle, out):
            result = real_info(handle, out)
            if result:
                ctypes.cast(out, ctypes.POINTER(files._FileInfo)).contents.links = 0
            return result

        for path, unlink in ((journal, False), (self.paths.db, True)):
            with self.subTest(file=path.name):
                path.write_bytes(b'fixture file')

                @contextmanager
                def report_zero_links(target, **options):
                    with real_open(target, **options) as handle:
                        if target == path:
                            if unlink:
                                path.unlink()
                            with patch.object(files, '_Info', side_effect=zero_link_info):
                                yield handle
                        else:
                            yield handle

                with patch.object(files, '_win_open', side_effect=report_zero_links):
                    with self.assertRaisesRegex(files.BridgeError, 'UNSAFE_FILE_TYPE'):
                        with files.database_guard(self.paths):
                            self.fail('accepted an invalid database file')
                if not unlink:
                    self.assertEqual(path.read_bytes(), b'fixture file')
                    path.unlink()

    @unittest.skipUnless(os.name == 'nt', 'Windows handle path validation')
    def test_existing_journal_still_rejects_a_windows_handle_path_collision(self):
        from unittest.mock import patch

        files.verify_layout(self.paths, create=True)
        journal = self.paths.data / 'bridge.sqlite3-journal'
        journal.write_bytes(b'fixture journal')
        real_check = files._win_check

        def reject_mismatched_journal(handle, path, **options):
            if path == journal:
                raise files.BridgeError('PATH_COLLISION')
            return real_check(handle, path, **options)

        with patch.object(files, '_win_check', side_effect=reject_mismatched_journal):
            with self.assertRaisesRegex(files.BridgeError, 'PATH_COLLISION'):
                files.verify_layout(self.paths, create=False)
            with self.assertRaisesRegex(files.BridgeError, 'PATH_COLLISION'):
                with files.database_guard(self.paths):
                    self.fail('accepted an existing sidecar with a mismatched path')
        self.assertEqual(journal.read_bytes(), b'fixture journal')

    def test_plain_file_rejects_directory_and_allows_absence(self):
        self.assertFalse(files.check_plain_file(self.root / 'absent', self.root))
        self.assertTrue(files.check_plain_file(self.root / 'source.txt', self.root))
        with self.assertRaises(files.BridgeError):
            files.check_plain_file(self.root / 'nested-directory', self.root.parent / 'not-an-ancestor')
        with self.assertRaises(files.BridgeError):
            files.check_plain_file(self.root, self.root)

    def test_published_file_requires_current_digest(self):
        content = 'nội dung\n'.encode('utf-8')
        ref = files.publish_file(self.paths, self.key, 'request', content)
        self.assertEqual(ref.relative_path, 'CT-014/attempt-1/request.md')
        self.assertEqual(ref.size, len(content))
        self.assertEqual(ref.sha256, sha256(content).hexdigest())
        self.assertEqual(files.read_file(self.paths, self.key, 'request', ref.sha256), content)
        self.assertEqual(files.publish_file(self.paths, self.key, 'request', content), ref)
        with self.assertRaises(files.BridgeError):
            files.publish_file(self.paths, self.key, 'request', b'overwrite')
        destination = self.paths.data / ref.relative_path
        self.assertEqual(destination.read_bytes(), content)
        destination.write_bytes(b'changed outside helper')
        with self.assertRaises(files.BridgeError):
            files.read_file(self.paths, self.key, 'request', ref.sha256)
        with self.assertRaises(files.BridgeError):
            files.read_file(self.paths, files.AttemptKey('CT-014', 2), 'request', ref.sha256)

    def test_rejects_invalid_attempts_before_creating_layout(self):
        for task, attempt in (('NUL', 1), ('con.txt', 1), ('COM1', 1), ('../escape', 1),
                              ('a/b', 1), ('okay', 0), ('okay', True), ('okay', 2**31)):
            with self.subTest(task=task, attempt=attempt), self.assertRaises(files.BridgeError):
                files.publish_file(self.paths, files.AttemptKey(task, attempt), 'request', b'x')
        with self.assertRaises(files.BridgeError):
            files.publish_file(self.paths, self.key, '../result', b'x')
        self.assertFalse(self.paths.data.exists())

    def test_rejects_case_colliding_task_and_unsafe_scope_paths(self):
        files.publish_file(self.paths, self.key, 'request', b'first')
        with self.assertRaises(files.BridgeError):
            files.publish_file(self.paths, files.AttemptKey('ct-014', 2), 'request', b'alias')
        for value in ('../a', '/a', 'a//b', 'a/./b', 'C:/a', r'\\server\share\a',
                      'a:stream', 'a/NUL.txt', 'a/COM¹', 'a.', 'a ', '.git/config',
                      'nested/.CODING-TEAM/x', 'a/ABCDEF~1/file', 'a\x00b', 'a/\ud800'):
            with self.subTest(value=repr(value)), self.assertRaises(files.BridgeError):
                files.validate_relative_path(value)
        self.assertEqual(files.validate_relative_path('src/ngữ cảnh.py'), 'src/ngữ cảnh.py')

    def test_utf8_size_limit_and_invalid_digest_preserve_existing_file(self):
        payload = b'a' * 1048576
        ref = files.publish_file(self.paths, self.key, 'result', payload)
        self.assertEqual(files.read_file(self.paths, self.key, 'result', ref.sha256), payload)
        for content in (payload + b'a', b'\xff', 'not bytes'):
            with self.subTest(content_type=type(content).__name__), self.assertRaises(files.BridgeError):
                files.publish_file(self.paths, self.key, 'result', content)
        with self.assertRaises(files.BridgeError):
            files.read_file(self.paths, self.key, 'result', 'not a digest')
        self.assertEqual((self.paths.data / ref.relative_path).read_bytes(), payload)

    def test_concurrent_first_writer_never_replaces_file(self):
        files.verify_layout(self.paths, create=True)
        barrier = threading.Barrier(2)

        def publish(payload):
            barrier.wait(timeout=10)
            try:
                return files.publish_file(self.paths, self.key, 'request', payload)
            except files.BridgeError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(publish, (b'first writer', b'second writer')))
        self.assertEqual(sum(result is not None for result in results), 1)
        ref = next(result for result in results if result is not None)
        self.assertEqual(sha256((self.paths.data / ref.relative_path).read_bytes()).hexdigest(), ref.sha256)

    def test_forged_workspace_paths_never_write_outside_worktree(self):
        outside = self.fixture / 'outside'
        forged = files.WorkspacePaths(self.root, outside, outside / 'bridge.sqlite3')
        with self.assertRaises(files.BridgeError):
            files.verify_layout(forged, create=True)
        self.assertFalse(outside.exists())

    def test_linked_workspace_and_transport_directory_are_rejected(self):
        alias = self.fixture / 'alias'
        try:
            alias.symlink_to(self.root, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest('symlink privilege unavailable: ' + str(exc))
        with self.assertRaises(files.BridgeError):
            files.locate_workspace(alias)
        (self.root / '.coding-team').mkdir()
        outside = self.fixture / 'outside'
        outside.mkdir()
        self.paths.data.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(files.BridgeError):
            files.verify_layout(self.paths, create=True)
        self.assertEqual(list(outside.iterdir()), [])

    def test_bridge_error_details_never_echo_control_characters_or_secrets(self):
        error = files.BridgeError('INVALID_PATH', 'private pairing secret\n\x1b[31m')
        self.assertEqual(error.reason, 'INVALID_PATH')
        self.assertNotIn('private pairing secret', str(error))
        self.assertNotIn('\n', str(error))

    @unittest.skipUnless(os.name == 'nt', 'Windows junction boundary')
    def test_junction_workspace_and_transport_are_rejected_without_writes(self):
        alias = self.fixture / 'junction'
        result = subprocess.run(('cmd', '/c', 'mklink', '/J', str(alias), str(self.root)), capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            with self.assertRaises(files.BridgeError):
                files.locate_workspace(alias)
        finally:
            os.rmdir(alias)
        self.paths.data.parent.mkdir()
        outside = self.fixture / 'outside'
        outside.mkdir()
        result = subprocess.run(('cmd', '/c', 'mklink', '/J', str(self.paths.data), str(outside)), capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            with self.assertRaises(files.BridgeError):
                files.publish_file(self.paths, self.key, 'request', b'no outside write')
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            os.rmdir(self.paths.data)

    @unittest.skipUnless(os.name == 'nt', 'Windows alternate stream boundary')
    def test_read_rejects_alternate_stream_on_transport_file(self):
        ref = files.publish_file(self.paths, self.key, 'request', b'visible content')
        target = self.paths.data / ref.relative_path
        Path(str(target) + ':hidden').write_bytes(b'hidden content')
        with self.assertRaises(files.BridgeError):
            files.read_file(self.paths, self.key, 'request', ref.sha256)
        self.assertEqual(target.read_bytes(), b'visible content')

    def test_tracked_transport_is_rejected_after_initialization(self):
        ref = files.publish_file(self.paths, self.key, 'request', b'keep tracked bytes')
        self.git('add', '-f', '.coding-team/bridge/' + ref.relative_path)
        with self.assertRaises(files.BridgeError):
            files.publish_file(self.paths, self.key, 'request', b'keep tracked bytes')
        with self.assertRaises(files.BridgeError):
            files.read_file(self.paths, self.key, 'request', ref.sha256)
        self.assertEqual((self.paths.data / ref.relative_path).read_bytes(), b'keep tracked bytes')

    def test_concurrent_identical_retries_both_return_the_same_reference(self):
        files.verify_layout(self.paths, create=True)
        barrier = threading.Barrier(2)

        def publish(_):
            barrier.wait(timeout=10)
            return files.publish_file(self.paths, self.key, 'request', b'same complete payload')

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.map(publish, range(2))
        self.assertEqual(first, second)
        self.assertEqual(files.read_file(self.paths, self.key, 'request', first.sha256), b'same complete payload')

    def test_nested_lookup_never_executes_git_from_inside_worktree(self):
        nested = self.root / 'nested'
        nested.mkdir()
        binaries = self.root / 'bin'
        binaries.mkdir()
        fake = binaries / ('git.exe' if os.name == 'nt' else 'git')
        fake.write_bytes(b'not an executable; must never be launched')
        fake.chmod(0o777)
        original_path = os.environ['PATH']
        os.environ['PATH'] = str(binaries) + os.pathsep + original_path
        try:
            self.assertEqual(files.locate_workspace(nested), self.paths)
        finally:
            os.environ['PATH'] = original_path

    def test_payload_file_is_consumed_once_after_bounded_utf8_validation(self):
        files.verify_layout(self.paths, create=True)
        payload = self.paths.data / 'inbox' / ('a' * 32 + '.json')
        payload.write_bytes(b'{"action":"fixture"}')
        self.assertEqual(files.consume_payload_file(self.paths, payload), b'{"action":"fixture"}')
        self.assertFalse(payload.exists())
        with self.assertRaises(files.BridgeError):
            files.consume_payload_file(self.paths, payload)

    def test_payload_rejects_invalid_content_and_foreign_path_without_deleting(self):
        files.verify_layout(self.paths, create=True)
        for index, content in enumerate((b'\xff', b'a' * 1048577)):
            payload = self.paths.data / 'inbox' / (str(index) * 32 + '.json')
            payload.write_bytes(content)
            with self.subTest(index=index), self.assertRaises(files.BridgeError):
                files.consume_payload_file(self.paths, payload)
            self.assertEqual(payload.read_bytes(), content)
        foreign = self.root / ('b' * 32 + '.json')
        foreign.write_bytes(b'outside transport')
        with self.assertRaises(files.BridgeError):
            files.consume_payload_file(self.paths, foreign)
        self.assertEqual(foreign.read_bytes(), b'outside transport')

    def test_payload_rejects_hardlink_and_directory_without_deleting(self):
        files.verify_layout(self.paths, create=True)
        payload = self.paths.data / 'inbox' / ('b' * 32 + '.json')
        original = self.fixture / 'original'
        original.write_bytes(b'foreign bytes')
        os.link(original, payload)
        with self.assertRaises(files.BridgeError):
            files.consume_payload_file(self.paths, payload)
        self.assertEqual(payload.read_bytes(), b'foreign bytes')
        payload.unlink()
        payload.mkdir()
        with self.assertRaises(files.BridgeError):
            files.consume_payload_file(self.paths, payload)
        self.assertTrue(payload.is_dir())

    def test_concurrent_payload_consumers_cannot_both_receive_bytes(self):
        files.verify_layout(self.paths, create=True)
        payload = self.paths.data / 'inbox' / ('c' * 32 + '.json')
        payload.write_bytes(b'one receipt')
        barrier = threading.Barrier(2)

        def consume(_):
            barrier.wait(timeout=10)
            try:
                return files.consume_payload_file(self.paths, payload)
            except files.BridgeError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(consume, range(2)))
        self.assertEqual(results.count(b'one receipt'), 1)
        self.assertEqual(results.count(None), 1)
        self.assertFalse(payload.exists())

    def test_cleanup_deletes_only_explicit_keys_and_is_idempotent(self):
        first = files.publish_file(self.paths, self.key, 'request', b'first request')
        result = files.publish_file(self.paths, self.key, 'result', b'first result')
        later_key = files.AttemptKey('CT-014', 2)
        later = files.publish_file(self.paths, later_key, 'request', b'later request')
        other = files.publish_file(self.paths, files.AttemptKey('CT-015', 1), 'request', b'other task')
        files.cleanup_task(self.paths, 'CT-014', (self.key,))
        self.assertFalse((self.paths.data / first.relative_path).exists())
        self.assertFalse((self.paths.data / result.relative_path).exists())
        self.assertEqual((self.paths.data / later.relative_path).read_bytes(), b'later request')
        self.assertEqual((self.paths.data / other.relative_path).read_bytes(), b'other task')
        files.cleanup_task(self.paths, 'CT-014', (self.key,))

    def test_cleanup_rejects_wrong_direction_and_unsafe_file_before_any_delete(self):
        first = files.publish_file(self.paths, self.key, 'request', b'preserve first')
        with self.assertRaises(files.BridgeError):
            files.cleanup_task(self.paths, 'CT-014', (self.key, files.AttemptKey('CT-015', 1)))
        original = self.fixture / 'unrelated-result'
        original.write_bytes(b'foreign hardlinked result')
        os.link(original, self.paths.data / 'CT-014' / 'attempt-1' / 'result.md')
        with self.assertRaises(files.BridgeError):
            files.cleanup_task(self.paths, 'CT-014', (self.key,))
        self.assertEqual((self.paths.data / first.relative_path).read_bytes(), b'preserve first')
        self.assertEqual(original.read_bytes(), b'foreign hardlinked result')

    def test_cleanup_preserves_unexpected_user_file_and_pristine_layout(self):
        files.cleanup_task(self.paths, 'CT-014', (self.key,))
        self.assertFalse(self.paths.data.parent.exists())
        ref = files.publish_file(self.paths, self.key, 'request', b'keep protocol file')
        foreign = (self.paths.data / ref.relative_path).with_name('user-notes.md')
        foreign.write_bytes(b'keep user notes')
        with self.assertRaises(files.BridgeError):
            files.cleanup_task(self.paths, 'CT-014', (self.key,))
        self.assertEqual(foreign.read_bytes(), b'keep user notes')
        self.assertEqual((self.paths.data / ref.relative_path).read_bytes(), b'keep protocol file')


if __name__ == '__main__':
    unittest.main()
