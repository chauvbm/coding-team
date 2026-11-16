from contextlib import contextmanager
from hashlib import sha256
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import ct_external_workspace as workspace


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        fixture = tempfile.TemporaryDirectory()
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        self.source = self.root / 'source repo'
        self.source.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Workspace Tests')
        self.git('config', 'user.email', 'workspace@example.test')
        self.git('config', 'core.autocrlf', 'false')
        for name, data in {
            'staged.py': b'C\r\n', 'delete.py': b'remove\n', 'other.py': b'before\n',
            'ngữ cảnh.txt': b'context\r\n', '.env': b'SECRET=fixture\n',
            'branch-only': b'read fixture\n', 'binary': b'\0bytes\n',
        }.items():
            (self.source / name).write_bytes(data)
        self.git('add', '.')
        self.git('commit', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD').strip().decode()
        self.attempt = self.root / 'state' / 'attempts' / 'task-scope' / 'attempt-1'

    def git(self, *args):
        return subprocess.check_output(('git', '-C', str(self.source), *args), stderr=subprocess.PIPE)

    def prepare(self, *, kind='EDIT', scope=(), choices=None, attempt=None):
        request = SimpleNamespace(kind=kind, base=self.base, file_choices=choices or {}, edit_paths=scope)
        return workspace.prepare(self.source, request, attempt or self.attempt)

    def freeze(self, manifest):
        identity = SimpleNamespace(job_name='fixture', pid=1, creation_time=1, session_id=0, boot_id='fixture-boot')
        proof = SimpleNamespace(key=manifest.key, runner_claim='test-claim', identity=identity, basis='JOB_EMPTY')
        return workspace.freeze(manifest, manifest.kind, self.attempt / 'result', proof)

    def edit(self, changes):
        manifest = self.prepare(scope=tuple(changes))
        for name, content in changes.items():
            path = manifest.project / name
            if content is None:
                path.unlink()
            else:
                path.write_bytes(content)
        frozen = self.freeze(manifest)
        candidate = workspace.build_candidate(manifest, frozen, self.attempt / 'candidate')
        self.assertEqual(workspace.candidate_digest(candidate), workspace.expected_candidate_digest(manifest, frozen))
        return manifest, frozen, candidate

    def apply(self, manifest, frozen, candidate):
        return workspace.apply_candidate(self.source, manifest, frozen, workspace.candidate_digest(candidate),
                                         self.attempt / 'apply.json')

    def test_manifest_uses_selected_sources_and_new_attempt_has_no_prior_context(self):
        (self.source / 'staged.py').write_bytes(b'A\r\n')
        self.git('add', 'staged.py')
        (self.source / 'staged.py').write_bytes(b'B\r\n')
        manifest = self.prepare(kind='READ', choices={'staged.py': 'INDEX', 'ngữ cảnh.txt': 'GIT'})
        self.assertEqual((manifest.project / 'staged.py').read_bytes(), b'A\r\n')
        self.assertEqual((manifest.project / 'ngữ cảnh.txt').read_bytes(), b'context\r\n')
        self.assertEqual((manifest.base / 'staged.py').read_bytes(), b'A\r\n')
        self.assertFalse((manifest.project / '.env').exists())
        loaded = workspace.load_manifest(manifest.path, manifest.sha256)
        self.assertEqual(loaded.key, manifest.key)
        frozen = self.freeze(loaded)
        self.assertFalse(frozen.entries)
        self.assertEqual((frozen.path.parent / 'changes.diff').read_bytes(), b'')
        second = self.prepare(kind='READ', choices={'staged.py': 'WORKTREE'},
                              attempt=self.attempt.with_name('attempt-2'))
        self.assertEqual((second.project / 'staged.py').read_bytes(), b'B\r\n')
        self.assertFalse((second.project / 'ngữ cảnh.txt').exists())
        with self.assertRaises(workspace.WorkspaceError):
            self.prepare(kind='READ', choices={'.env': 'GIT'}, attempt=self.attempt.with_name('attempt-3'))

    def test_rejects_unsafe_names_and_case_folded_ancestors(self):
        for name in (r'\\server\share\file', r'C:\file', 'a:stream', 'NUL.txt', 'COM¹.txt', 'dir/LPT9',
                     '../foo', 'foo/.', 'dir//foo', 'foo.', '.GIT/config', '.Gemini/hooks/foo',
                     'dir/private_key.pem', 'AGENTS.md', '_Agents/config.md',
                     'dir/ABCDEF~1/config'):
            with self.subTest(name=name), self.assertRaises(workspace.WorkspaceError):
                workspace.validate_relative_path(name)
        for names in (('Nested/a', 'nested/b'), ('File', 'file'), ('file', 'file/child')):
            with self.subTest(names=names), self.assertRaises(workspace.WorkspaceError):
                workspace.validate_relative_paths(names)

    def test_projection_does_not_use_git_from_within_source(self):
        repo_bin = self.source / 'bin'
        repo_bin.mkdir()
        impostor = repo_bin / ('git.exe' if os.name == 'nt' else 'git')
        impostor.write_bytes(b'ordinary test data')
        impostor.chmod(0o755)
        with patch.dict(os.environ, {'PATH': str(repo_bin) + os.pathsep + os.environ.get('PATH', '')}):
            manifest = self.prepare(kind='READ', choices={'branch-only': 'GIT'})
        self.assertEqual((manifest.project / 'branch-only').read_bytes(), b'read fixture\n')

    def test_rejects_hard_link_in_source_even_for_git_choice(self):
        os.link(self.source / 'branch-only', self.source / 'hardlink')
        with self.assertRaises(workspace.WorkspaceError):
            self.prepare(kind='READ', choices={'branch-only': 'GIT'})

    def test_rejects_source_junction_or_symlink(self):
        (self.root / 'outside').mkdir()
        (self.root / 'outside' / 'secret').write_bytes(b'outside')
        link = self.source / 'link'
        if os.name == 'nt':
            subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(self.root / 'outside')],
                           capture_output=True, check=True)
        else:
            link.symlink_to(self.root / 'outside', target_is_directory=True)
        with self.assertRaises(workspace.WorkspaceError):
            self.prepare(scope=('link/secret',))

    def test_rejects_git_link_mode_with_no_worktree_entry(self):
        blob = subprocess.check_output(('git', '-C', str(self.source), 'hash-object', '-w', '--stdin'),
                                       input=b'secret').strip().decode()
        self.git('update-index', '--add', '--cacheinfo', f'120000,{blob},selected-link')
        with self.assertRaises(workspace.WorkspaceError):
            self.prepare(kind='READ', choices={'selected-link': 'INDEX'})

    def test_git_and_index_choices_reject_aliases_absent_from_worktree(self):
        (self.source / 'branch-only').unlink()
        for attempt_no, choice in enumerate(('GIT', 'INDEX'), 1):
            with self.subTest(choice=choice), self.assertRaises(workspace.WorkspaceError):
                self.prepare(kind='READ', choices={'Branch-only': choice},
                             attempt=self.attempt.with_name(f'attempt-{attempt_no}'))

    def test_rejects_drift_while_preparing(self):
        original = workspace._write_new

        def change_source(path, content):
            original(path, content)
            if path.name == 'other.py':
                (self.source / 'other.py').write_bytes(b'outside change\n')

        with patch.object(workspace, '_write_new', side_effect=change_source):
            with self.assertRaises(workspace.WorkspaceError):
                self.prepare(scope=('other.py',))
        self.assertFalse((self.attempt / 'manifest.json').exists())
        self.assertEqual((self.source / 'other.py').read_bytes(), b'outside change\n')

    def test_freeze_rejects_wrong_stop_proof_and_non_edit_mutation(self):
        manifest = self.prepare(kind='READ', choices={'other.py': 'WORKTREE'})
        invalid = SimpleNamespace(key=('wrong', 2), runner_claim='claim', identity=object(), basis='JOB_EMPTY')
        with self.assertRaises(workspace.WorkspaceError):
            workspace.freeze(manifest, 'READ', self.attempt / 'result', invalid)
        invalid.key = manifest.key
        invalid.identity = None
        with self.assertRaises(workspace.WorkspaceError):
            workspace.freeze(manifest, 'READ', self.attempt / 'result', invalid)
        invalid.identity = object()
        with self.assertRaises(workspace.WorkspaceError):
            workspace.freeze(manifest, 'READ', self.attempt / 'result', invalid)
        (manifest.project / 'other.py').write_bytes(b'edited\n')
        with self.assertRaises(workspace.WorkspaceError):
            self.freeze(manifest)

    def test_freeze_rejects_out_of_scope_file_and_changed_type(self):
        manifest = self.prepare(scope=('other.py',))
        outside = manifest.project / 'outside'
        outside.write_bytes(b'created')
        with self.assertRaises(workspace.WorkspaceError):
            self.freeze(manifest)
        outside.unlink()
        selected = manifest.project / 'other.py'
        selected.unlink()
        selected.mkdir()
        with self.assertRaises(workspace.WorkspaceError):
            self.freeze(manifest)

    def test_freeze_checks_entire_scope_before_reading_project_contents(self):
        manifest = self.prepare(scope=('other.py',))
        (manifest.project / 'zz foreign').write_bytes(b'out of scope')
        with self.watch_project_reads(manifest) as reads:
            with self.assertRaises(workspace.WorkspaceError) as error:
                self.freeze(manifest)
        self.assertEqual(error.exception.reason, 'DIFF_OUTSIDE_SCOPE')
        self.assertEqual(reads, [])

    def test_freeze_stops_enumerating_after_an_out_of_scope_entry(self):
        manifest = self.prepare(kind='READ', choices={'branch-only': 'GIT'})
        (manifest.project / 'unexpected.txt').write_bytes(b'unexpected')
        original_scandir = os.scandir

        @contextmanager
        def guarded_scan(path):
            with original_scandir(path) as entries:
                if Path(path) != manifest.project:
                    yield entries
                    return

                def unexpected_first():
                    yield next(entry for entry in entries if entry.name == 'unexpected.txt')
                    self.fail('freeze enumerated past an entry it should reject immediately')

                yield unexpected_first()

        with patch.object(workspace.os, 'scandir', guarded_scan):
            with self.assertRaises(workspace.WorkspaceError) as caught:
                self.freeze(manifest)
        self.assertEqual(caught.exception.reason, 'DIFF_OUTSIDE_SCOPE')

    def test_freeze_rejects_sparse_file_and_aggregate_growth_before_reading(self):
        manifest = self.prepare(scope=('first', 'last'))
        for sizes in ((10 * 1024 * 1024 + 1, 0), (6 * 1024 * 1024, 5 * 1024 * 1024)):
            for name, size in zip(('first', 'last'), sizes):
                target = manifest.project / name
                target.touch()
                if os.name == 'nt':
                    subprocess.run(['fsutil.exe', 'sparse', 'setflag', str(target)], capture_output=True, check=True)
                with target.open('r+b') as stream:
                    stream.truncate(size)
            with self.subTest(sizes=sizes), self.watch_project_reads(manifest) as reads:
                with self.assertRaises(workspace.WorkspaceError) as error:
                    self.freeze(manifest)
                self.assertEqual(error.exception.reason, 'DIFF_TOO_LARGE')
                self.assertEqual(reads, [])

    def test_freeze_applies_remaining_growth_budget_when_file_changes_after_inventory(self):
        manifest = self.prepare(scope=('first', 'last'))
        (manifest.project / 'first').write_bytes(b'a' * (6 * 1024 * 1024))
        (manifest.project / 'last').touch()
        owner = workspace._WindowsGuard if os.name == 'nt' else workspace
        method = 'read' if os.name == 'nt' else '_read_state'
        original = getattr(owner, method)
        limits = []

        def grow(*args, **kwargs):
            if args[-1] == manifest.project / 'last':
                limits.append(kwargs['limit'])
                with args[-1].open('r+b') as stream:
                    stream.truncate(5 * 1024 * 1024)
            return original(*args, **kwargs)

        with patch.object(owner, method, new=grow), self.assertRaises(workspace.WorkspaceError) as error:
            self.freeze(manifest)
        self.assertEqual(error.exception.reason, 'DIFF_TOO_LARGE')
        self.assertEqual(limits, [4 * 1024 * 1024])

    def watch_project_reads(self, manifest):
        from contextlib import contextmanager

        @contextmanager
        def watch():
            reads = []
            owner = workspace._WindowsGuard if os.name == 'nt' else workspace
            method = 'read' if os.name == 'nt' else '_read_state'
            original = getattr(owner, method)

            def record(*args, **kwargs):
                path = Path(args[-1])
                if manifest.project in path.parents:
                    reads.append((path.name, kwargs.get('limit')))
                return original(*args, **kwargs)

            with patch.object(owner, method, new=record):
                yield reads

        return watch()

    def test_unstarted_cleanup_removes_prepared_and_interrupted_projections_only(self):
        manifest = self.prepare(scope=('other.py',))
        interrupted = self.attempt / 'workspace-partprep'
        (interrupted / 'base').mkdir(parents=True)
        (interrupted / 'base' / 'leftover').write_bytes(b'private selected bytes')
        receipt = self.attempt / 'artifact.json'
        receipt.write_bytes(b'keep')
        unrelated = self.attempt / 'workspace-unrelated'
        unrelated.mkdir()
        for _ in range(2):
            workspace.cleanup_unstarted(self.attempt)
        self.assertFalse(manifest.project.parent.exists())
        self.assertFalse(interrupted.exists())
        self.assertTrue(manifest.path.exists())
        self.assertEqual(receipt.read_bytes(), b'keep')
        self.assertTrue(unrelated.exists())

    def test_unstarted_cleanup_rejects_alias_and_link(self):
        self.attempt.mkdir(parents=True)
        alias = self.attempt / 'Workspace-abcdefgh'
        alias.mkdir()
        with self.assertRaises(workspace.WorkspaceError):
            workspace.cleanup_unstarted(self.attempt)
        self.assertTrue(alias.exists())
        alias.rmdir()
        storage = self.attempt / 'workspace-abcdefgh'
        storage.mkdir()
        destination = self.root / 'outside-preserve'
        destination.mkdir()
        (destination / 'sensitive').write_bytes(b'keep')
        if os.name == 'nt':
            subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(storage / 'base'), str(destination)],
                           capture_output=True, check=True)
        else:
            (storage / 'base').symlink_to(destination, target_is_directory=True)
        with self.assertRaises(workspace.WorkspaceError):
            workspace.cleanup_unstarted(self.attempt)
        self.assertEqual((destination / 'sensitive').read_bytes(), b'keep')

    @unittest.skipUnless(os.name == 'nt', 'Requires Windows file sharing enforcement')
    def test_unstarted_cleanup_retries_after_locked_snapshot_file(self):
        manifest = self.prepare(scope=('other.py',))
        with workspace._win_open(manifest.base / 'other.py', workspace._READ, workspace._SHARE_READ):
            with self.assertRaises(workspace.WorkspaceError) as error:
                workspace.cleanup_unstarted(self.attempt)
            self.assertEqual(error.exception.reason, 'CLEANUP_FAILED')
        workspace.cleanup_unstarted(self.attempt)
        self.assertFalse(manifest.base.parent.exists())
        self.assertTrue(manifest.path.exists())

    def test_freeze_rejects_binary_and_oversize_edit(self):
        manifest = self.prepare(scope=('binary', 'other.py'))
        (manifest.project / 'binary').write_bytes(b'\0change\n')
        with self.assertRaises(workspace.WorkspaceError):
            self.freeze(manifest)
        (manifest.project / 'binary').write_bytes(b'\0bytes\n')
        (manifest.project / 'other.py').write_bytes(b'x' * (10 * 1024 * 1024))
        with self.assertRaises(workspace.WorkspaceError):
            self.freeze(manifest)

    def test_freeze_limits_review_diff_instead_of_selected_file_size(self):
        context = (b'x' * 10_000 + b'\n') * 1100
        (self.source / 'other.py').write_bytes(context + b'before\n')
        _, frozen, candidate = self.edit({'other.py': context + b'after\n'})
        self.assertLess((frozen.path.parent / 'changes.diff').stat().st_size, 50_000)
        self.assertEqual((candidate / 'other.py').read_bytes(), context + b'after\n')

    def test_frozen_diff_builds_exact_candidate_after_project_is_removed(self):
        manifest, frozen, candidate = self.edit({'other.py': b'edited\r\n', 'delete.py': None,
                                                  'new name.txt': b'created\r\n'})
        import shutil
        shutil.rmtree(manifest.project)
        frozen = workspace.load_frozen_diff(frozen.path, frozen.sha256)
        candidate = workspace.build_candidate(manifest, frozen, self.attempt / 'candidate2')
        self.assertEqual((candidate / 'other.py').read_bytes(), b'edited\r\n')
        self.assertFalse((candidate / 'delete.py').exists())
        self.assertEqual((candidate / 'new name.txt').read_bytes(), b'created\r\n')
        digest = workspace.candidate_digest(candidate)
        (candidate / 'new name.txt').write_bytes(b'changed after candidate build')
        self.assertNotEqual(workspace.candidate_digest(candidate), digest)

    @unittest.skipUnless(os.name == 'nt', 'Requires Windows source mutation guards')
    def test_staged_bytes_and_interrupted_integration(self):
        (self.source / 'staged.py').write_bytes(b'A\r\n')
        self.git('add', 'staged.py')
        (self.source / 'staged.py').write_bytes(b'B\r\n')
        (self.source / 'untracked').write_bytes(b'untracked\n')
        index = self.source / '.git' / 'index'
        index_before = index.read_bytes()
        manifest, frozen, candidate = self.edit({'staged.py': b'new stage\r\n', 'delete.py': None,
                                                  'untracked': b'untracked edit\n', 'new name': b'created\n'})
        self.assertEqual(manifest.edit['staged.py'].sha256, sha256(b'B\r\n').hexdigest())
        mutate = workspace._replace_image

        def interrupt(guard, path, before, after):
            if path == 'untracked':
                raise OSError('interrupted')
            mutate(guard, path, before, after)

        with patch.object(workspace, '_replace_image', side_effect=interrupt):
            self.assertEqual(self.apply(manifest, frozen, candidate).disposition, 'RECONCILE_REQUIRED')
        self.assertEqual(index.read_bytes(), index_before)
        self.assertFalse((self.source / 'delete.py').exists())
        (self.source / 'staged.py').write_bytes(b'other writer\r\n')
        report = workspace.reconcile_apply(self.source, self.attempt / 'apply.json')
        self.assertEqual(report.disposition, 'RECONCILE_REQUIRED')
        self.assertEqual(report.conflicts, ('staged.py',))
        self.assertEqual((self.source / 'staged.py').read_bytes(), b'other writer\r\n')
        self.assertEqual((self.source / 'delete.py').read_bytes(), b'remove\n')
        self.assertFalse((self.source / 'new name').exists())
        self.assertEqual(index.read_bytes(), index_before)

    @unittest.skipUnless(os.name == 'nt', 'Requires Windows source mutation guards')
    def test_apply_creates_updates_and_deletes_without_staging(self):
        manifest, frozen, candidate = self.edit({'other.py': b'after\r\n', 'delete.py': None,
                                                  'new name.txt': b'created\r\n'})
        index = self.source / '.git' / 'index'
        original_index = index.read_bytes()
        self.assertEqual(self.apply(manifest, frozen, candidate).disposition, 'APPLIED')
        self.assertEqual((self.source / 'other.py').read_bytes(), b'after\r\n')
        self.assertFalse((self.source / 'delete.py').exists())
        self.assertEqual((self.source / 'new name.txt').read_bytes(), b'created\r\n')
        self.assertEqual(index.read_bytes(), original_index)
        self.assertEqual(workspace.reconcile_apply(self.source, self.attempt / 'apply.json').disposition, 'APPLIED')

    @unittest.skipUnless(os.name == 'nt', 'Requires Windows source mutation guards')
    def test_retry_reuses_journal_only_after_all_preimages_verify(self):
        (self.source / 'delete.py').unlink()
        manifest, frozen, candidate = self.edit({'delete.py': b'recreated from absent preimage\n',
                                                  'other.py': b'after\n'})
        self.assertFalse(manifest.edit['delete.py'].present)
        with patch.object(workspace, '_replace_image', side_effect=OSError('stopped between journal and write')):
            self.assertEqual(self.apply(manifest, frozen, candidate).disposition, 'NOT_APPLIED')
        journal = self.attempt / 'apply.json'
        old_state = journal.stat()
        report = workspace.reconcile_apply(self.source, journal)
        self.assertEqual(report.disposition, 'NOT_APPLIED')
        self.assertFalse((self.source / 'delete.py').exists())
        self.assertEqual(self.apply(manifest, frozen, candidate).disposition, 'APPLIED')
        self.assertEqual(journal.stat().st_mtime_ns, old_state.st_mtime_ns)
        self.assertEqual((self.source / 'delete.py').read_bytes(), b'recreated from absent preimage\n')

    @unittest.skipUnless(os.name == 'nt', 'Requires Windows source mutation guards')
    def test_concurrent_writer_before_handle_cannot_be_overwritten(self):
        manifest, frozen, candidate = self.edit({'other.py': b'after\n'})
        mutate = workspace._replace_image

        def contender(guard, path, before, after):
            self.competing_write(path, expected=0)
            mutate(guard, path, before, after)

        with patch.object(workspace, '_replace_image', side_effect=contender):
            report = self.apply(manifest, frozen, candidate)
        self.assertEqual(report.disposition, 'RECONCILE_REQUIRED')
        self.assertEqual((self.source / 'other.py').read_bytes(), b'outside\n')

    @unittest.skipUnless(os.name == 'nt', 'Requires Windows source mutation guards')
    def test_concurrent_create_is_never_overwritten(self):
        manifest, frozen, candidate = self.edit({'new': b'new content\n'})
        mutate = workspace._replace_image

        def contender(guard, path, before, after):
            self.competing_write(path, expected=0)
            mutate(guard, path, before, after)

        with patch.object(workspace, '_replace_image', side_effect=contender):
            report = self.apply(manifest, frozen, candidate)
        self.assertEqual(report.disposition, 'RECONCILE_REQUIRED')
        self.assertEqual((self.source / 'new').read_bytes(), b'outside\n')

    @unittest.skipUnless(os.name == 'nt', 'Requires Windows source mutation guards')
    def test_second_writer_is_rejected_while_handle_is_held(self):
        manifest, frozen, candidate = self.edit({'other.py': b'after\n'})
        write = workspace._write_handle

        def contend(handle, content):
            self.competing_write('other.py', expected=42)
            write(handle, content)

        with patch.object(workspace, '_write_handle', side_effect=contend):
            report = self.apply(manifest, frozen, candidate)
        self.assertEqual(report.disposition, 'APPLIED')
        self.assertEqual((self.source / 'other.py').read_bytes(), b'after\n')

    @unittest.skipUnless(os.name == 'nt', 'Requires Windows source mutation guards')
    def test_reconcile_rechecks_postimage_after_rollback_preflight(self):
        manifest, frozen, candidate = self.edit({'delete.py': b'edited\n', 'other.py': b'after\n'})
        mutate = workspace._replace_image

        def interrupt(guard, path, before, after):
            if path == 'other.py':
                raise OSError('interrupted')
            mutate(guard, path, before, after)

        with patch.object(workspace, '_replace_image', side_effect=interrupt):
            report = self.apply(manifest, frozen, candidate)
        self.assertEqual(report.disposition, 'RECONCILE_REQUIRED')

        def contend(guard, path, before, after):
            self.competing_write(path, expected=0)
            mutate(guard, path, before, after)

        with patch.object(workspace, '_replace_image', side_effect=contend):
            report = workspace.reconcile_apply(self.source, self.attempt / 'apply.json')
        self.assertEqual(report.disposition, 'RECONCILE_REQUIRED')
        self.assertEqual(report.conflicts, ('delete.py',))
        self.assertEqual((self.source / 'delete.py').read_bytes(), b'outside\n')

    @unittest.skipUnless(os.name == 'nt', 'Requires Windows source mutation guards')
    def test_read_only_target_preserves_source_before_first_write(self):
        manifest, frozen, candidate = self.edit({'delete.py': None, 'other.py': b'after\n'})
        read_only = self.source / 'other.py'
        read_only.chmod(0o444)
        self.addCleanup(read_only.chmod, 0o666)
        self.assertEqual(self.apply(manifest, frozen, candidate).disposition, 'NOT_APPLIED')
        self.assertEqual((self.source / 'delete.py').read_bytes(), b'remove\n')
        self.assertEqual(read_only.read_bytes(), b'before\n')
        self.assertFalse((self.attempt / 'apply.json').exists())

    @unittest.skipUnless(os.name == 'nt', 'Requires Windows source mutation guards')
    def test_apply_pins_source_ancestors_and_index_until_verification(self):
        nested = self.source / 'nested'
        nested.mkdir()
        (nested / 'other.py').write_bytes(b'before\n')
        manifest, frozen, candidate = self.edit({'nested/other.py': b'after\n'})
        write = workspace._write_handle

        def contend(handle, content):
            self.competing_write('.git/index', expected=42)
            script = ('from pathlib import Path\nimport sys\ntry:\n'
                      ' Path(sys.argv[1]).rename(sys.argv[2])\nexcept PermissionError:\n sys.exit(42)\n')
            result = subprocess.run([sys.executable, '-c', script, str(nested), str(self.source / 'displaced')],
                                    timeout=10, capture_output=True)
            self.assertEqual(result.returncode, 42, result.stderr)
            write(handle, content)

        with patch.object(workspace, '_write_handle', side_effect=contend):
            report = self.apply(manifest, frozen, candidate)
        self.assertEqual(report.disposition, 'APPLIED')
        self.assertEqual((nested / 'other.py').read_bytes(), b'after\n')

    @unittest.skipUnless(os.name == 'nt', 'Requires Windows source mutation guards')
    def test_journal_validation_rejects_rewritten_images_bound_to_same_attempt(self):
        manifest, frozen, candidate = self.edit({'other.py': b'after\n'})
        digest = workspace.candidate_digest(candidate)
        self.assertEqual(self.apply(manifest, frozen, candidate).disposition, 'APPLIED')
        journal_path = self.attempt / 'apply.json'
        verified = workspace.verify_apply_journal(self.source, manifest, frozen, digest, journal_path)
        self.assertEqual(verified.key, manifest.key)
        self.assertEqual(verified.artifact_sha256, frozen.sha256)
        old = journal_path.read_bytes()
        # Substitute a valid new image and its digest without touching any attempt bindings.
        import base64
        rewired = old.replace(base64.b64encode(b'after\n'), base64.b64encode(b'never was the candidate\n'))
        rewired = rewired.replace(sha256(b'after\n').hexdigest().encode(),
                                  sha256(b'never was the candidate\n').hexdigest().encode())
        journal_path.write_bytes(rewired)
        with self.assertRaises(workspace.WorkspaceError):
            workspace.verify_apply_journal(self.source, manifest, frozen, digest, journal_path)
        report = workspace.reconcile_apply(self.source, journal_path, expected_sha256=verified.sha256)
        self.assertEqual(report.disposition, 'RECONCILE_REQUIRED')
        self.assertEqual((self.source / 'other.py').read_bytes(), b'after\n')

    @unittest.skipUnless(os.name == 'nt', 'Requires Windows source mutation guards')
    def test_detects_concurrent_creation_of_previously_absent_index(self):
        index = self.source / '.git' / 'index'
        original = index.read_bytes()
        index.unlink()
        manifest, frozen, candidate = self.edit({'other.py': b'after\n'})
        mutate = workspace._replace_image

        def create_index(guard, path, before, after):
            mutate(guard, path, before, after)
            index.write_bytes(original)

        with patch.object(workspace, '_replace_image', side_effect=create_index):
            report = self.apply(manifest, frozen, candidate)
        self.assertEqual(report.disposition, 'RECONCILE_REQUIRED')
        self.assertEqual(report.reason, 'INDEX_CHANGED')
        self.assertEqual(index.read_bytes(), original)

    @unittest.skipUnless(os.name == 'nt', 'Requires NTFS named streams')
    def test_freeze_rejects_named_streams_on_project_files_and_directories(self):
        manifest = self.prepare(kind='READ', choices={'other.py': 'WORKTREE'})
        for target in (manifest.project / 'other.py', manifest.project):
            stream = Path(str(target) + ':fixture-stream')
            stream.write_bytes(b'alternate bytes')
            try:
                with self.assertRaises(workspace.WorkspaceError) as error:
                    self.freeze(manifest)
                self.assertEqual(error.exception.reason, 'ALTERNATE_DATA_STREAM')
            finally:
                stream.unlink()

    @unittest.skipUnless(os.name == 'nt', 'Requires NTFS named streams')
    def test_apply_cannot_delete_named_stream_added_after_snapshot(self):
        manifest, frozen, candidate = self.edit({'other.py': None})
        stream = Path(str(self.source / 'other.py') + ':fixture-stream')
        stream.write_bytes(b'alternate bytes')
        report = self.apply(manifest, frozen, candidate)
        self.assertEqual(report.disposition, 'NOT_APPLIED')
        self.assertEqual((self.source / 'other.py').read_bytes(), b'before\n')
        self.assertEqual(stream.read_bytes(), b'alternate bytes')

    def competing_write(self, name, *, expected):
        start = threading.Barrier(2, timeout=10)
        outcome = []

        def writer():
            start.wait()
            command = ('from pathlib import Path\nimport sys\ntry:\n'
                       ' Path(sys.argv[1]).write_bytes(b"outside\\n")\n'
                       'except PermissionError:\n sys.exit(42)\n')
            outcome.append(subprocess.run([sys.executable, '-c', command, str(self.source / name)],
                                          timeout=10, capture_output=True))

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        start.wait()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertEqual(outcome[0].returncode, expected, outcome[0].stderr)


if __name__ == '__main__':
    unittest.main()
