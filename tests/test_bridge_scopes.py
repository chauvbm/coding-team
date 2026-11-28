"""Filesystem and SQLite checks for Desktop, native, and Leader scope admissions."""
from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import unittest
import uuid

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / 'scripts'))
from ct_bridge_files import AttemptKey, BridgeError, locate_workspace
from ct_bridge_roles import load_contract, validate_assignment


_DDL = '''CREATE TABLE scope_reservations (
    round_id TEXT, participant_id TEXT, task_id TEXT NOT NULL DEFAULT '',
    attempt_no INTEGER NOT NULL DEFAULT 0, access TEXT, path_key TEXT, path TEXT,
    PRIMARY KEY (round_id, participant_id, task_id, attempt_no, access, path_key)
)'''


def _wait_for_paths(paths):
    expires = time.monotonic() + 15
    while not all(path.exists() for path in paths):
        if time.monotonic() >= expires:
            raise TimeoutError('scope race fixture rendezvous')
        time.sleep(0.02)


def _compete(db_path, ready, start, round_id):
    from ct_bridge_scopes import ScopeHolder, ScopeSet, reserve

    with sqlite3.connect(db_path, timeout=10, isolation_level=None) as db:
        Path(ready).touch()
        _wait_for_paths([Path(start)])
        db.execute('BEGIN IMMEDIATE')
        try:
            reserve(db, ScopeHolder(round_id, 'participant'), ScopeSet(write_paths=('shared.out',)))
        except BridgeError as exc:
            print(exc.reason)
        else:
            db.commit()
            print('admitted')


class BridgeScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = load_contract(SOURCE)

    def setUp(self):
        from ct_bridge_scopes import ScopeHolder

        # Inherit the worktree ACL so the restricted Windows token can use the fixture.
        self.fixture = SOURCE / ('.ct-scopes-' + uuid.uuid4().hex)
        self.fixture.mkdir(mode=0o777)
        self.addCleanup(self.remove_fixture)
        self.root = self.fixture / 'worktree'
        self.root.mkdir(mode=0o777)
        self.git('init', '-q')
        (self.root / 'source.txt').write_bytes(b'checked-in source\n')
        self.git('add', 'source.txt')
        self.git('-c', 'user.name=Scope fixture', '-c', 'user.email=scope@example.test',
                 'commit', '-qm', 'fixture')
        self.head = self.git('rev-parse', 'HEAD').strip().decode('ascii')
        self.paths = locate_workspace(self.root)
        self.desktop = ScopeHolder('desktop-round', 'worker', AttemptKey('desktop-task', 1))
        self.native = ScopeHolder('native-round', 'worker')
        self.leader = ScopeHolder('native-round', 'leader')

    def remove_fixture(self):
        def remove_readonly(function, path, _exc_info):
            os.chmod(path, stat.S_IRWXU)
            for retry in range(20):
                try:
                    function(path)
                    return
                except PermissionError as exc:
                    if getattr(exc, 'winerror', None) != 32 or retry == 19:
                        raise
                    time.sleep(0.025)

        self.assertEqual(self.fixture.parent, SOURCE)
        shutil.rmtree(self.fixture, onerror=remove_readonly)

    def git(self, *args):
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith('GIT_')}
        result = subprocess.run(('git', '-C', str(self.root), *args), env=env, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        return result.stdout

    def assignment(self, *, reads=(), writes=(), outputs=(), inputs=None):
        return validate_assignment(self.contract, {
            'variant': 'ct-developer' if writes else 'ct-reviewer-verify',
            'work_item_id': 'scope-item',
            'snapshot': {'head': self.head, 'inputs': {} if inputs is None else inputs},
            'read_paths': list(reads), 'write_paths': list(writes),
            'check_outputs': list(outputs), 'test_paths': [],
        }, verified_model=None)

    def connection(self, path=':memory:'):
        db = sqlite3.connect(path, isolation_level=None)
        self.addCleanup(db.close)
        db.execute(_DDL)
        return db

    def test_mixed_admission_orders_detect_write_and_read_conflicts(self):
        from ct_bridge_scopes import canonical_scopes, release, reserve

        reader = canonical_scopes(self.paths, self.assignment(reads=('Future.txt',)))
        writer = canonical_scopes(self.paths, self.assignment(writes=('future.txt',)))
        for first, second in (((self.native, reader), (self.desktop, writer)),
                              ((self.desktop, writer), (self.native, reader)),
                              ((self.leader, writer), (self.desktop, reader)),
                              ((self.desktop, reader), (self.leader, writer)),
                              ((self.desktop, writer), (self.leader, writer))):
            with self.subTest(first=first, second=second):
                db = self.connection()
                db.execute('BEGIN IMMEDIATE')
                reserve(db, *first)
                with self.assertRaisesRegex(BridgeError, '^SCOPE_CONFLICT$'):
                    reserve(db, *second)
                release(db, first[0])
                reserve(db, *second)

    def test_parent_and_child_writes_conflict_without_matching_unrelated_prefixes(self):
        from ct_bridge_scopes import canonical_scopes, reserve

        parent = canonical_scopes(self.paths, self.assignment(writes=('new',)))
        child = canonical_scopes(self.paths, self.assignment(writes=('new/file',)))
        sibling = canonical_scopes(self.paths, self.assignment(writes=('new-build/file',)))
        for first, second in ((parent, child), (child, parent)):
            with self.subTest(first=first):
                db = self.connection()
                db.execute('BEGIN IMMEDIATE')
                reserve(db, self.desktop, first)
                with self.assertRaisesRegex(BridgeError, '^SCOPE_CONFLICT$'):
                    reserve(db, self.native, second)
                reserve(db, self.native, sibling)

    def test_read_check_outputs_conflict_only_at_shared_path(self):
        from ct_bridge_scopes import canonical_scopes, reserve

        check = canonical_scopes(self.paths, self.assignment(reads=('source.txt',), outputs=('cache/shared',)))
        isolated = canonical_scopes(self.paths, self.assignment(reads=('source.txt',), outputs=('cache/per-attempt',)))
        edit = canonical_scopes(self.paths, self.assignment(writes=('cache/shared',)))
        db = self.connection()
        db.execute('BEGIN IMMEDIATE')
        reserve(db, self.desktop, check)
        reserve(db, self.native, isolated)
        with self.assertRaisesRegex(BridgeError, '^SCOPE_CONFLICT$'):
            reserve(db, self.leader, edit)
        db.rollback()
        db.execute('BEGIN IMMEDIATE')
        reserve(db, self.leader, edit)
        with self.assertRaisesRegex(BridgeError, '^SCOPE_CONFLICT$'):
            reserve(db, self.desktop, check)
        reserve(db, self.native, isolated)

    def test_check_outputs_cannot_cover_reviewed_source_or_tracked_files(self):
        from ct_bridge_scopes import canonical_scopes

        (self.root / 'local.txt').write_bytes(b'untracked review input')
        (self.root / 'staged.txt').write_bytes(b'new indexed source')
        self.git('add', 'staged.txt')
        self.git('rm', '-q', 'source.txt')  # Still tracked in the committed snapshot.
        for output, reads in (('SOURCE.TXT', ()), ('staged.txt', ()), ('local.txt', ('local.txt',))):
            with self.subTest(output=output), self.assertRaises(BridgeError):
                canonical_scopes(self.paths, self.assignment(reads=reads, outputs=(output,)))
        (self.root / '.gitignore').write_bytes(b'ignored/\n')
        scope = canonical_scopes(self.paths, self.assignment(outputs=('ignored/log', 'untracked/log')))
        self.assertEqual(scope.check_outputs, ('ignored/log', 'untracked/log'))

    def test_windows_aliases_and_non_regular_paths_fail_closed(self):
        from ct_bridge_scopes import canonical_scopes

        real_dir = self.root / 'ActualName'
        real_dir.mkdir()
        real_file = real_dir / 'File.txt'
        real_file.write_bytes(b'regular')
        hardlink = self.root / 'hardlink.txt'
        os.link(real_file, hardlink)
        assignment = self.assignment(writes=('new.txt',))
        for path in ('C:/temp.txt', '../escape.txt', 'file:stream', 'foo//bar', 'NUL.txt',
                     'SHORTN~2.txt', 'trailing. ', '.git/index', '.coding-team/bridge.json',
                     'actualname/new.txt', 'ActualName/file.txt', 'ActualName',
                     'ActualName/File.txt/child', 'hardlink.txt', 'ActualName/File.txt'):
            with self.subTest(path=path), self.assertRaises(BridgeError):
                canonical_scopes(self.paths, replace(assignment, write_paths=(path,)))

    def test_parent_aliases_in_one_scope_are_rejected_before_creation(self):
        from ct_bridge_scopes import canonical_scopes

        for writes in (('New/path-one', 'new/path-two'), ('New', 'New/child'),
                       ('I/path-one', '\u0131/path-two')):
            with self.subTest(writes=writes), self.assertRaises(BridgeError):
                canonical_scopes(self.paths, self.assignment(writes=writes))

    def test_rejects_linked_existing_ancestor_for_an_absent_child(self):
        from ct_bridge_scopes import canonical_scopes

        outside = self.fixture / 'outside'
        outside.mkdir()
        linked = self.root / 'linked'
        if os.name == 'nt':
            result = subprocess.run(('cmd', '/c', 'mklink', '/J', str(linked), str(outside)), capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.addCleanup(linked.rmdir)
        else:
            linked.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(BridgeError):
            canonical_scopes(self.paths, self.assignment(writes=('linked/new/deep/file',)))
        self.assertEqual(list(outside.iterdir()), [])

    def test_reservation_retries_and_release_target_exact_attempt_and_access(self):
        from ct_bridge_scopes import ScopeHolder, ScopeSet, release, reserve

        db = self.connection()
        db.execute('BEGIN IMMEDIATE')
        write_one = ScopeSet(write_paths=('one.out',))
        write_two = ScopeSet(write_paths=('two.out',))
        same_read = ScopeSet(read_paths=('one.out',))
        retry = ScopeHolder(self.desktop.round_id, self.desktop.participant_id, AttemptKey('desktop-task', 2))
        reserve(db, self.desktop, write_one)
        reserve(db, self.desktop, write_one)
        reserve(db, self.desktop, same_read)
        reserve(db, retry, write_two)
        release(db, self.desktop, access='read')
        with self.assertRaisesRegex(BridgeError, '^SCOPE_CONFLICT$'):
            reserve(db, self.native, write_one)
        release(db, self.desktop)
        reserve(db, self.native, write_one)
        with self.assertRaisesRegex(BridgeError, '^SCOPE_CONFLICT$'):
            reserve(db, self.native, write_two)
        release(db, retry)
        reserve(db, self.native, write_two)

    def test_independent_processes_cannot_both_reserve_same_file(self):
        db_path = self.fixture / 'scope.sqlite3'
        db = self.connection(db_path)
        signal = self.fixture / 'start'
        ready = [self.fixture / 'one.ready', self.fixture / 'two.ready']
        processes = []
        try:
            for marker in ready:
                processes.append(subprocess.Popen(
                    (sys.executable, __file__, '--race', str(db_path), str(marker), str(signal), marker.stem),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                ))
            _wait_for_paths(ready)
            signal.touch()
            outcomes = []
            for process in processes:
                result, error = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, error)
                outcomes.append(result.strip())
            self.assertCountEqual(outcomes, ['admitted', 'SCOPE_CONFLICT'])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM scope_reservations').fetchone()[0], 1)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)

    def test_verify_baseline_rechecks_existing_and_absent_write_paths(self):
        from ct_bridge_scopes import canonical_scopes, verify_baseline

        source = self.root / 'source.txt'
        assignment = self.assignment(writes=('source.txt', 'new-output'))
        scope = canonical_scopes(self.paths, assignment)
        baseline = {'source.txt': sha256(source.read_bytes()).hexdigest(), 'new-output': 'ABSENT'}
        verify_baseline(self.paths, snapshot=assignment.snapshot, scope=scope, baseline=baseline)
        (self.root / 'new-output').write_bytes(b'created since publish')
        with self.assertRaisesRegex(BridgeError, '^BASELINE_CHANGED$'):
            verify_baseline(self.paths, snapshot=assignment.snapshot, scope=scope, baseline=baseline)
        (self.root / 'new-output').unlink()
        source.write_bytes(b'modified since publish')
        with self.assertRaisesRegex(BridgeError, '^BASELINE_CHANGED$'):
            verify_baseline(self.paths, snapshot=assignment.snapshot, scope=scope, baseline=baseline)
        with self.assertRaises(BridgeError):
            verify_baseline(self.paths, snapshot=assignment.snapshot, scope=scope, baseline={'new-output': 'ABSENT'})

    def test_snapshot_inputs_reserve_uncommitted_files_and_recheck_head(self):
        from ct_bridge_scopes import canonical_scopes, reserve, verify_baseline

        local = self.root / 'local.py'
        local.write_bytes(b'raw bytes\r\n' * 100000)
        assignment = self.assignment(inputs={'local.py': sha256(local.read_bytes()).hexdigest()})
        scope = canonical_scopes(self.paths, assignment)
        verify_baseline(self.paths, snapshot=assignment.snapshot, scope=scope, baseline={})
        db = self.connection()
        db.execute('BEGIN IMMEDIATE')
        reserve(db, self.native, scope)
        writer = canonical_scopes(self.paths, self.assignment(writes=('local.py',)))
        with self.assertRaisesRegex(BridgeError, '^SCOPE_CONFLICT$'):
            reserve(db, self.desktop, writer)
        local.write_bytes(b'uncommitted change')
        with self.assertRaisesRegex(BridgeError, '^SNAPSHOT_CHANGED$'):
            verify_baseline(self.paths, snapshot=assignment.snapshot, scope=scope, baseline={})
        self.git('-c', 'user.name=Scope fixture', '-c', 'user.email=scope@example.test',
                 'commit', '--allow-empty', '-qm', 'new head, same existing paths')
        empty_scope = canonical_scopes(self.paths, self.assignment())
        with self.assertRaisesRegex(BridgeError, '^SNAPSHOT_CHANGED$'):
            verify_baseline(self.paths, snapshot={'head': self.head, 'inputs': {}}, scope=empty_scope, baseline={})

    def test_unfingerprinted_source_read_is_rejected(self):
        from ct_bridge_scopes import canonical_scopes, verify_baseline

        assignment = self.assignment(reads=('source.txt',))
        scope = canonical_scopes(self.paths, assignment)
        with self.assertRaises(BridgeError):
            verify_baseline(self.paths, snapshot=assignment.snapshot, scope=scope, baseline={})


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--race':
        _compete(*sys.argv[2:])
    else:
        unittest.main()
