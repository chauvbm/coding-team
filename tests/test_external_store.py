import importlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from threading import Barrier, Event
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import ct_policy
import ct_storage
from ct_winjob import OwnerProcess, ProcessIdentity


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('ct_external_store'), 'external store is missing')
        self.api = importlib.import_module('ct_external_store')
        fixture = tempfile.TemporaryDirectory()
        self.addCleanup(fixture.cleanup)
        root = Path(fixture.name).resolve()
        self.policy = root / 'config' / 'policy.json'
        self.root = self.policy.parent / 'external' / 'v1'
        self.workspace = root / 'repo'
        self.workspace.mkdir()
        self.owner = self.api.Owner('codex', 'chat', str(self.workspace), 'antigravity')
        self.other = self.api.Owner('codex', 'other', str(self.workspace), 'antigravity')
        self.key = self.api.AttemptKey('CT-1', 1)
        self.check = self.api.Request('CHECK', '', '', {}, ())
        self.fingerprint = 'a' * 64
        self.identity = ProcessIdentity('job-fixture', 123, 456, 1, 'fixture-boot')
        self.stamp = ct_policy.PolicyStamp('ON', 0, 0)
        self.now = 100
        self.store = self.api.Store(self.root, clock=lambda: self.now)

    def enable(self, owner=None):
        self.store.set_opt_in(owner or self.owner, True, self.stamp)

    def assert_reason(self, reason, fn, *args, **kwargs):
        with self.assertRaises(self.api.StoreError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.reason, reason)

    def reserve(self, key=None, request=None, owner=None):
        return self.store.reserve(
            owner or self.owner, key or self.key, request or self.check, self.stamp, self.fingerprint,
        )

    def start(self, key=None):
        key = key or self.key
        self.store.set_projection(key, self.owner, 'b' * 64)
        view = self.store.claim_start(key, self.owner, self.stamp)
        self.assertEqual(view.state, 'STARTING')
        claim = self.store.run_snapshot(key, owner=self.owner).runner_claim
        self.store.claim_runner(key, claim, self.stamp, OwnerProcess(234, 678, 1, 'fixture-boot'))
        return claim

    def launch(self, claim, key=None, identity=None):
        key = key or self.key
        identity = identity or self.identity
        self.store.admit_launch(key, claim, self.stamp, {'job_name': identity.job_name})
        self.store.record_process(key, claim, identity)
        calls = []
        self.store.resume_process(key, claim, self.stamp, lambda: calls.append(True))
        self.assertEqual(calls, [True])

    def proof(self, claim, key=None, identity=None, basis='JOB_EMPTY'):
        return self.api.StopProof(key=key or self.key, runner_claim=claim, identity=identity, basis=basis)

    def check_progress(self, claim):
        evidence = []
        probes = ('read', 'edit', 'deny_read_write', 'deny_outside_read', 'deny_outside_write',
                  'deny_shell', 'deny_mcp', 'deny_web', 'deny_native_read', 'deny_private_read')
        for step, name in enumerate(probes, 1):
            denied = name.startswith('deny_')
            identity = ProcessIdentity(f'job-fixture-{step}', 123 + step, 456 + step, 1, 'fixture-boot')
            self.launch(claim, identity=identity)
            proof = self.proof(claim, identity=identity)
            evidence.append({'passed': True, 'probe': name, 'runtime_denial': denied})
            self.store.record_stopped(self.key, claim, proof)
            self.store.record_check_step(self.key, claim, self.stamp, step, proof, evidence[-1])
        return proof, evidence

    def ready(self):
        self.enable()
        self.reserve()
        claim = self.start()
        proof, evidence = self.check_progress(claim)
        return self.store.commit_check(self.key, claim, self.stamp, proof, self.fingerprint, evidence)

    def test_status_is_read_only_and_missing_database_is_off(self):
        self.assertEqual(self.store.status(self.owner).opt_in, 'OFF')
        self.assertFalse(self.root.exists())

    def test_opt_in_mutation_returns_status_without_history(self):
        self.assertFalse(self.store.opt_in_enabled(self.owner))
        self.assertFalse(self.root.exists())
        self.enable()
        self.reserve()
        self.assertTrue(self.store.opt_in_enabled(self.owner))
        result = self.store.set_opt_in(self.owner, False, self.stamp)
        self.assertEqual(result.attempts, ())
        self.assertFalse(self.store.opt_in_enabled(self.owner))
        self.assertEqual(self.store.status(self.owner, include_history=False).attempts, ())
        self.assertEqual(self.store.status(self.owner).attempts[0].state, 'CANCELED')

    def test_unknown_native_policy_cannot_turn_on_or_create_store(self):
        unknown = ct_policy.PolicyStamp(None, 0, None)
        self.assert_reason('POLICY_UNKNOWN', self.store.set_opt_in, self.owner, True, unknown)
        self.assertFalse(self.root.exists())

    def test_old_writer_cannot_reinitialize_missing_metadata(self):
        ct_policy.initialize_state(self.policy)
        with ct_storage.locked(self.policy):
            self.assert_reason('INSTALLATION_INCOMPATIBLE', self.store.ensure_policy_epochs,
                               self.policy, compatible=False)
            self.assertFalse(self.root.exists())
            self.store.ensure_policy_epochs(self.policy, compatible=True)
            self.assertTrue(self.store.epochs_initialized())
            state = ct_policy.load_state(self.policy)
            state.pop('policy_epoch_v1')
            ct_policy.save_state(self.policy, state)
            self.assert_reason('POLICY_WRITER_INCOMPATIBLE', self.store.ensure_policy_epochs,
                               self.policy, compatible=True)
            self.assertNotIn('policy_epoch_v1', ct_policy.load_state(self.policy))

    def test_identical_reservation_is_idempotent_after_off(self):
        self.enable()
        first = self.reserve()
        self.store.set_opt_in(self.owner, False, self.stamp)
        self.assertEqual(self.reserve().key, first.key)
        self.assertEqual(self.reserve().state, 'CANCELED')
        changed = self.api.Request('CHECK', 'changed', '', {}, ())
        self.assert_reason('CONFLICT', self.reserve, request=changed)
        self.assert_reason('OWNER_MISMATCH', self.reserve, owner=self.other)

    def test_reserved_off_projection_cleanup_survives_reopening(self):
        self.enable()
        self.reserve()
        self.store.set_projection(self.key, self.owner, 'b' * 64)
        self.store.set_opt_in(self.owner, False, self.stamp)
        reopened = self.api.Store(self.root)
        pending = reopened.pending_projection_cleanup()
        self.assertEqual([(view.key, view.reason) for view in pending], [(self.key, 'OPT_IN_OFF')])
        reopened.finish_projection_cleanup(self.key)
        reopened.finish_projection_cleanup(self.key)
        self.assertEqual(reopened.pending_projection_cleanup(), ())

    def test_reservation_expiry_and_direct_cancel_queue_cleanup(self):
        self.enable()
        self.reserve()
        another = self.api.AttemptKey('CT-2', 1)
        self.now += 61
        self.assertEqual(self.reserve(key=another).state, 'RESERVED')
        self.assertEqual([(item.key, item.reason) for item in self.store.pending_projection_cleanup()],
                         [(self.key, 'PREPARE_LEASE_EXPIRED')])
        self.assert_reason('INVALID_STATE', self.store.finish_projection_cleanup, another)
        self.store.cancel(self.owner, another)
        self.assertEqual(tuple(item.key for item in self.store.pending_projection_cleanup(maximum=1)), (self.key,))
        self.store.finish_projection_cleanup(self.key)
        self.assertEqual(tuple(item.key for item in self.store.pending_projection_cleanup()), (another,))

    def test_managed_recovery_uses_exact_active_owner_when_worktree_is_renamed(self):
        self.enable()
        self.reserve()
        self.store.cancel(self.owner, self.key)
        active = self.api.AttemptKey('CT-2', 1)
        self.reserve(key=active)
        self.store.claim_start(active, self.owner, self.stamp)
        self.workspace.rename(self.workspace.with_name('renamed'))
        self.assert_reason('INVALID_WORKSPACE', self.store.run_snapshot, active, owner=self.owner)
        self.assert_reason('MAINTENANCE', self.store.run_snapshot, active, owner=self.owner, maintenance=True)
        self.assertEqual(self.store.begin_maintenance().key, active)
        self.assert_reason('ATTEMPT_NOT_FOUND', self.store.run_snapshot, self.key, owner=self.owner, maintenance=True)
        self.assert_reason('OWNER_MISMATCH', self.store.run_snapshot, active, owner=self.other, maintenance=True)
        self.assert_reason('OWNER_MISMATCH', self.store.fence_expired_start, active, self.other, maintenance=True)
        self.assert_reason('STOP_PROOF_REQUIRED', self.store.fence_expired_start, active, self.owner, maintenance=True)
        self.now += 15
        self.assert_reason('INVALID_WORKSPACE', self.store.fence_expired_start, active, self.owner)
        snapshot = self.store.run_snapshot(active, owner=self.owner, maintenance=True)
        self.assertEqual((snapshot.view.owner, snapshot.view.state), (self.owner, 'STOPPING'))
        self.store.fence_expired_start(active, self.owner, maintenance=True)
        proof = self.proof(snapshot.runner_claim, active, basis='NEVER_STARTED')
        terminal = self.store.commit_result(active, snapshot.runner_claim, None, proof, {'state': 'FAILED'})
        self.assertEqual((terminal.state, terminal.reason), ('CANCELED', 'MAINTENANCE'))
        self.assert_reason('ATTEMPT_NOT_FOUND', self.store.run_snapshot, active, owner=self.owner, maintenance=True)
        self.assertEqual(tuple(item.key for item in self.store.pending_projection_cleanup()), (self.key,))

    def test_read_only_reservation_lookup_recognizes_a_replay_after_off(self):
        self.assertIsNone(self.store.lookup_reservation(self.owner, self.key, self.check))
        self.assertFalse(self.root.exists())
        self.enable()
        self.reserve()
        self.store.set_opt_in(self.owner, False, self.stamp)
        existing = self.store.lookup_reservation(self.owner, self.key, self.check)
        self.assertEqual((existing.key, existing.state), (self.key, 'CANCELED'))
        changed = self.api.Request('CHECK', 'different', '', {}, ())
        self.assert_reason('CONFLICT', self.store.lookup_reservation, self.owner, self.key, changed)

    def test_two_connections_can_reserve_only_one_slot(self):
        self.enable()
        self.enable(self.other)
        barrier = Barrier(2, timeout=5)

        def compete(owner, key):
            store = self.api.Store(self.root, clock=lambda: self.now)
            barrier.wait()
            try:
                return store.reserve(owner, key, self.check, self.stamp, self.fingerprint).state
            except self.api.StoreError as exc:
                return exc.reason

        with ThreadPoolExecutor(max_workers=2) as threads:
            first = threads.submit(compete, self.owner, self.key)
            second = threads.submit(compete, self.other, self.api.AttemptKey('CT-2', 1))
            self.assertCountEqual([first.result(10), second.result(10)], ['RESERVED', 'BUSY'])

    def test_check_and_edit_compete_for_the_same_slot(self):
        self.ready()
        barrier = Barrier(2, timeout=5)
        edit = self.api.Request('EDIT', 'edit', 'a' * 40, {}, ('one.txt',))

        def compete(task, request):
            store = self.api.Store(self.root)
            barrier.wait()
            try:
                return store.reserve(self.owner, self.api.AttemptKey(task, 1),
                                     request, self.stamp, self.fingerprint).state
            except self.api.StoreError as exc:
                return exc.reason

        with ThreadPoolExecutor(max_workers=2) as threads:
            check = threads.submit(compete, 'check', self.check)
            editing = threads.submit(compete, 'edit', edit)
            self.assertCountEqual([check.result(10), editing.result(10)], ['RESERVED', 'BUSY'])

    def test_read_grants_are_attempt_scoped_rotated_and_expire(self):
        self.enable()
        self.reserve()
        grant = self.store.grant_read(self.owner, self.key, 5)
        self.assertEqual(self.store.collect(self.key, grant)['state'], 'RESERVED')
        self.assert_reason('INVALID_GRANT', self.store.collect, self.api.AttemptKey('CT-1', 2), grant)
        self.assert_reason('INVALID_GRANT', self.store.collect, self.key, grant, workspace_key=self.root)
        newer = self.store.grant_read(self.owner, self.key, 5)
        self.assert_reason('INVALID_GRANT', self.store.collect, self.key, grant)
        self.assertNotIn(newer.encode(), self.store.db_path.read_bytes())
        self.now += 5
        self.assert_reason('INVALID_GRANT', self.store.collect, self.key, newer)

    def test_ready_is_for_the_exact_committed_fingerprint(self):
        terminal = self.ready()
        self.assertEqual(terminal.state, 'COMPLETED')
        self.assertEqual(self.store.status(self.owner, self.stamp, self.fingerprint).readiness, 'READY')
        self.assertEqual(self.store.status(self.owner, self.stamp, 'c' * 64).readiness, 'NOT_READY')
        self.store.set_opt_in(self.owner, False, self.stamp)
        self.assertEqual(self.store.collect_for_owner(self.owner, self.key)['result_id'], terminal.result_id)

    def test_check_qualification_persists_binary_version_without_exposing_path(self):
        self.enable()
        self.reserve()
        claim = self.start()
        proof, evidence = self.check_progress(claim)
        qualification = {
            'binary_path': str(self.workspace / 'provider' / 'agy.exe'),
            'binary_sha256': 'e' * 64,
            'version': '1.5.9',
            'signer': 'Google LLC',
        }
        self.store.commit_check(self.key, claim, self.stamp, proof, self.fingerprint, evidence,
                               profile_metadata=qualification)
        result = self.store.collect_for_owner(self.owner, self.key)['result']
        self.assertEqual(result['binary_sha256'], 'e' * 64)
        self.assertEqual(result['executable_version'], '1.5.9')
        self.assertNotIn('binary_path', result)

    def test_off_after_resume_requires_exact_stop_proof(self):
        self.enable()
        self.reserve()
        claim = self.start()
        self.launch(claim)
        self.store.set_opt_in(self.owner, False, self.stamp)
        self.assertEqual(self.store.status(self.owner).attempts[0].state, 'STOPPING')
        never = self.proof(claim, basis='NEVER_STARTED')
        bad_identity = self.proof(claim, identity=ProcessIdentity('job-fixture', 321, 456, 1, 'fixture-boot'))
        for fake in (never, bad_identity):
            self.assert_reason('STOP_PROOF_REQUIRED', self.store.commit_result, self.key, claim, self.stamp,
                               fake, {'state': 'COMPLETED', 'summary': 'late'})
        self.enable()
        self.assert_reason('BUSY', self.reserve, key=self.api.AttemptKey('CT-2', 1))
        proof = self.proof(claim, identity=self.identity)
        first = self.store.commit_result(self.key, claim, self.stamp, proof, {'state': 'COMPLETED'})
        second = self.store.commit_result(self.key, claim, self.stamp, proof, {'state': 'FAILED'})
        self.assertEqual(first, second)
        self.assertEqual(first.state, 'CANCELED')
        self.assertNotIn('late', str(self.store.collect_for_owner(self.owner, self.key)))
        self.assertEqual(self.store.status(self.owner, self.stamp, self.fingerprint).readiness, 'NOT_READY')

    def test_root_exit_keeps_slot_until_stop_proof(self):
        self.enable()
        self.reserve()
        claim = self.start()
        self.launch(claim)
        self.now += 10
        self.store.record_process_exit(self.key, claim, 0)
        record = self.store.collect_for_owner(self.owner, self.key)
        self.assertEqual(record['timestamps']['process_exit_at'], self.now)
        self.assertIsNone(record['timestamps']['stopped_at'])
        self.assertEqual(record['state'], 'RUNNING')
        self.assert_reason('BUSY', self.reserve, key=self.api.AttemptKey('CT-2', 1))

    def test_old_policy_stamp_cannot_publish_after_off_on(self):
        ct_policy.initialize_state(self.policy)
        with ct_storage.locked(self.policy):
            self.store.ensure_policy_epochs(self.policy, compatible=True)
        self.enable()
        self.reserve()
        claim = self.start()
        self.launch(claim)
        ct_policy.apply_command(self.policy, '/coding-team off --global', None)
        ct_policy.apply_command(self.policy, '/coding-team on --global', None)
        proof = self.proof(claim, identity=self.identity)
        with ct_storage.locked(self.policy):
            current_stamp = ct_policy.policy_stamp(self.policy, self.owner.chat_id, require_epochs=True)
            view = self.store.commit_result(self.key, claim, current_stamp, proof, {'state': 'COMPLETED'})
        self.assertEqual((view.state, view.reason), ('CANCELED', 'POLICY_CHANGED'))
        self.assertEqual(self.store.status(self.owner, current_stamp, self.fingerprint).readiness, 'NOT_READY')

    def test_off_before_resume_blocks_callback_on_a_separate_connection(self):
        self.enable()
        self.reserve()
        claim = self.start()
        self.store.admit_launch(self.key, claim, self.stamp, {'job_name': self.identity.job_name})
        self.store.record_process(self.key, claim, self.identity)
        other_connection = self.api.Store(self.root)
        other_connection.set_opt_in(self.owner, False, self.stamp)
        resumed = []
        self.assert_reason('OPT_IN_OFF', self.store.resume_process, self.key, claim, self.stamp,
                           lambda: resumed.append(True))
        self.assertEqual(resumed, [])
        self.assertEqual(self.store.active_attempt().state, 'STOPPING')

    def test_check_result_cannot_publish_ready_after_fingerprint_mismatch(self):
        self.enable()
        self.reserve()
        claim = self.start()
        proof, evidence = self.check_progress(claim)
        result = self.store.commit_check(self.key, claim, self.stamp, proof, 'd' * 64, evidence)
        self.assertEqual(result.state, 'FAILED')
        self.assertEqual(self.store.status(self.owner, self.stamp, self.fingerprint).readiness, 'NOT_READY')

    def test_check_cannot_publish_ready_after_only_the_first_probe(self):
        self.enable()
        self.reserve()
        claim = self.start()
        self.launch(claim)
        proof = self.proof(claim, identity=self.identity)
        evidence = {'passed': True, 'probe': 'read', 'runtime_denial': False}
        self.store.record_check_step(self.key, claim, self.stamp, 1, proof, evidence)
        result = self.store.commit_check(self.key, claim, self.stamp, proof, self.fingerprint, [evidence])
        self.assertEqual(result.state, 'FAILED')
        self.assertEqual(self.store.status(self.owner, self.stamp, self.fingerprint).readiness, 'NOT_READY')

    def test_subsequent_check_spawn_failure_uses_never_started_for_that_job(self):
        self.enable()
        self.reserve()
        claim = self.start()
        self.launch(claim)
        proof = self.proof(claim, identity=self.identity)
        self.store.record_check_step(self.key, claim, self.stamp, 1, proof, {
            'passed': True, 'probe': 'read', 'runtime_denial': False,
        })
        self.store.admit_launch(self.key, claim, self.stamp, {'job_name': 'job-next'})
        never = self.proof(claim, basis='NEVER_STARTED')
        failed = self.store.commit_result(self.key, claim, self.stamp, never, {
            'state': 'FAILED', 'reason': 'JOB_CREATE_FAILED',
        })
        self.assertEqual(failed.state, 'FAILED')
        self.assertIsNone(self.store.active_attempt())

    def test_partial_apply_survives_reopen_and_holds_scope(self):
        self.ready()
        first = self.api.AttemptKey('CT-2', 1)
        second = self.api.AttemptKey('CT-3', 1)
        edit = self.api.Request('EDIT', 'edit', 'a' * 40, {}, ('one.txt',))
        self.reserve(key=first, request=edit)
        claim = self.start(first)
        self.launch(claim, first)
        proof = self.proof(claim, first, self.identity)
        self.store.commit_result(first, claim, self.stamp, proof, {
            'state': 'COMPLETED', 'artifact_sha': 'b' * 64,
        })
        self.store.record_candidate(self.owner, first, 'b' * 64, 'c' * 64)
        self.store.record_tested(self.owner, first, 'c' * 64, {'passed': True})
        self.store.begin_apply(self.owner, first, 'c' * 64, 'journal')
        self.store.finish_disposition(self.owner, first, 'RECONCILE_REQUIRED', {'state': 'RECONCILE_REQUIRED'})
        self.store = self.api.Store(self.root)
        self.assert_reason('EDIT_SCOPE_BUSY', self.reserve, key=second, request=edit)
        descendant = self.api.Request('EDIT', 'edit', 'a' * 40, {}, ('ONE.TXT/child.txt',))
        self.assert_reason('EDIT_SCOPE_BUSY', self.reserve, key=second, request=descendant)
        self.assert_reason('RECONCILE_REQUIRED', self.store.finish_disposition, self.owner, first, 'DISCARDED', {})
        self.store.finish_disposition(self.owner, first, 'APPLIED', {'state': 'APPLIED'})
        self.assertEqual(self.reserve(key=second, request=edit).state, 'RESERVED')

    def test_sqlite_reader_observes_no_half_published_check(self):
        self.enable()
        self.reserve()
        claim = self.start()
        proof, evidence = self.check_progress(claim)
        competing = sqlite3.connect(self.store.db_path)
        try:
            with competing:
                competing.execute(
                    "CREATE TRIGGER fail_ready BEFORE INSERT ON checks "
                    "BEGIN SELECT RAISE(ABORT, 'fixture database failure'); END"
                )
        finally:
            competing.close()
        self.assert_reason('STORE_UNAVAILABLE', self.store.commit_check, self.key, claim, self.stamp,
                           proof, self.fingerprint, evidence)
        state = self.store.collect_for_owner(self.owner, self.key)
        self.assertEqual(state['state'], 'FINALIZING')
        self.assertIsNone(state['result_id'])
        self.assert_reason('BUSY', self.reserve, key=self.api.AttemptKey('CT-2', 1))

    def test_corrupt_database_is_left_for_reconciliation(self):
        self.root.mkdir(parents=True)
        original = b'invalid SQLite file'
        self.store.db_path.write_bytes(original)
        self.assert_reason('STORE_UNAVAILABLE', self.store.status, self.owner)
        self.assert_reason('STORE_UNAVAILABLE', self.store.set_opt_in, self.owner, True, self.stamp)
        self.assertEqual(self.store.db_path.read_bytes(), original)

    def windows_unknown_link_count(self, target, *, after_second_stat=None):
        path_type = type(target)
        original = path_type.lstat
        seen = 0

        def lstat(path):
            nonlocal seen
            info = original(path)
            if path != target:
                return info
            seen += 1
            if seen == 2 and after_second_stat is not None:
                after_second_stat()
            return SimpleNamespace(st_mode=info.st_mode, st_nlink=0, st_file_attributes=info.st_file_attributes,
                                   st_reparse_tag=info.st_reparse_tag)

        return mock.patch.object(path_type, 'lstat', lstat)

    @unittest.skipUnless(os.name == 'nt', 'requires Windows journal metadata')
    def test_windows_store_checks_handle_when_live_journal_link_count_is_unknown(self):
        self.enable()
        journal = self.store.db_path.with_suffix('.sqlite3-journal')
        writer = sqlite3.connect(self.store.db_path)
        try:
            writer.execute('BEGIN IMMEDIATE')
            writer.execute("UPDATE meta SET value=value+1 WHERE name='runtime_epoch'")
            self.assertTrue(journal.is_file())
            with self.windows_unknown_link_count(journal):
                self.assertTrue(self.api.Store(self.root).opt_in_enabled(self.owner))
        finally:
            writer.close()

    @unittest.skipUnless(os.name == 'nt', 'requires Windows journal metadata')
    def test_windows_store_accepts_journal_that_disappears_during_link_check(self):
        self.enable()
        journal = self.store.db_path.with_suffix('.sqlite3-journal')
        writer = sqlite3.connect(self.store.db_path)
        try:
            writer.execute('BEGIN IMMEDIATE')
            writer.execute("UPDATE meta SET value=value+1 WHERE name='runtime_epoch'")
            self.assertTrue(journal.is_file())
            with self.windows_unknown_link_count(journal, after_second_stat=writer.commit):
                self.api.Store(self.root)
            self.assertFalse(journal.exists())
        finally:
            writer.close()

    @unittest.skipUnless(os.name == 'nt', 'requires Windows file metadata')
    def test_windows_unknown_link_count_does_not_admit_a_real_hardlink(self):
        self.enable()
        db = self.store.db_path
        os.link(db, self.workspace / 'alias.sqlite3')
        self.assertEqual(db.lstat().st_nlink, 2)
        self.assert_reason('INVALID_STORE_ROOT', self.api.Store, self.root)
        with self.windows_unknown_link_count(db):
            self.assert_reason('INVALID_STORE_ROOT', self.api.Store, self.root)

    @unittest.skipUnless(os.name == 'nt', 'requires Windows delete disposition')
    def test_windows_store_retries_delete_pending_journal_until_it_disappears(self):
        import ct_external_workspace as workspace

        self.enable()
        journal = self.store.db_path.with_suffix('.sqlite3-journal')
        journal.write_bytes(b'sidecar')
        ready, release = Event(), Event()
        open_handle = workspace._win_open

        def delete_journal():
            with open_handle(journal, 0x10000, 7) as handle:
                workspace._delete_handle(handle)
                ready.set()
                self.assertTrue(release.wait(5))

        @contextmanager
        def release_on_denied_metadata_open(path, access, sharing):
            try:
                with open_handle(path, access, sharing) as handle:
                    yield handle
            except PermissionError:
                release.set()
                raise

        with ThreadPoolExecutor(max_workers=1) as worker:
            deletion = worker.submit(delete_journal)
            try:
                self.assertTrue(ready.wait(5))
                self.assertEqual(journal.lstat().st_nlink, 0)
                with mock.patch.object(workspace, '_win_open', release_on_denied_metadata_open):
                    self.api.Store(self.root)
                self.assertTrue(release.is_set())
            finally:
                release.set()
            deletion.result(5)
        self.assertFalse(journal.exists())

    @unittest.skipUnless(os.name == 'nt', 'requires Windows delete disposition')
    def test_windows_store_rejects_journal_that_remains_unverifiable(self):
        from ct_external_workspace import _delete_handle, _win_open

        self.enable()
        journal = self.store.db_path.with_suffix('.sqlite3-journal')
        journal.write_bytes(b'sidecar')
        with _win_open(journal, 0x10000, 7) as handle:
            _delete_handle(handle)
            self.assert_reason('INVALID_STORE_ROOT', self.api.Store, self.root)

    def test_locked_database_cannot_release_slot(self):
        self.enable()
        self.reserve()
        short_timeout = self.api.Store(self.root, busy_timeout=.1)
        competing = sqlite3.connect(self.store.db_path)
        try:
            competing.execute('BEGIN EXCLUSIVE')
            self.assert_reason('STORE_UNAVAILABLE', short_timeout.status, self.owner, self.stamp, self.fingerprint)
            self.assert_reason('STORE_UNAVAILABLE', short_timeout.cancel, self.owner, self.key)
        finally:
            competing.close()
        self.assertEqual(self.store.active_attempt().key, self.key)


if __name__ == '__main__':
    unittest.main()
