import ctypes
from ctypes import wintypes
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'
FIXTURE = ROOT / 'tests' / 'fixtures' / 'external' / 'process_tree.py'
sys.path.insert(0, str(SCRIPTS))

try:
    import ct_winjob
except ModuleNotFoundError as exc:
    if exc.name != 'ct_winjob':
        raise
    ct_winjob = None


def stopped(job):
    state = job.poll()
    return state.active_processes == 0 and state.root_exit_code is not None


@unittest.skipUnless(os.name == 'nt', 'Windows process lifecycle')
class JobTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(ct_winjob, 'Windows Job Object wrapper is missing')
        self.cwd = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='ct job é ')))

    def launch(self, mode, *args, stdin=b''):
        return SimpleNamespace(executable=Path(sys.executable),
                               args=(str(FIXTURE), mode, *args), cwd=self.cwd,
                               env=os.environ.copy(), stdin_bytes=stdin)

    def job(self, mode, *args, stdin=b''):
        return self.enterContext(ct_winjob.spawn_suspended(self.launch(mode, *args, stdin=stdin),
                                                            ct_winjob.new_job_name()))

    def wait_for(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(.01)
        self.fail('Timed out waiting for process fixture')

    def stop_proof(self, job):
        from ct_external_store import AttemptKey, StopProof

        key = AttemptKey('fixture', 1)
        return key, StopProof, job.prove_stopped(key, 'fixture-claim')

    def test_root_exit_keeps_child_in_job(self):
        job = self.job('root')
        self.assertEqual(job.poll().active_processes, 1)
        self.assertFalse((self.cwd / 'child.json').exists())
        job.resume()
        self.wait_for(lambda: job.poll().root_exit_code is not None and
                      (self.cwd / 'child.json').exists())
        self.assertGreater(job.poll().active_processes, 0)
        self.assertIsNone(self.stop_proof(job)[2])
        with self.assertRaises(ct_winjob.JobError):
            job.get_output()
        job.terminate()
        self.wait_for(lambda: stopped(job))
        key, proof_type, proof = self.stop_proof(job)
        self.assertIsInstance(proof, proof_type)
        self.assertEqual((proof.key, proof.runner_claim, proof.identity, proof.basis),
                         (key, 'fixture-claim', job.identity(), 'JOB_EMPTY'))
        output = job.get_output()
        self.assertIn(b'root', output.stdout)
        self.assertIn(b'child', output.stdout)
        (self.cwd / 'release').touch()
        time.sleep(.15)
        self.assertFalse((self.cwd / 'late-write').exists())

    def test_stdin_and_separate_output(self):
        payload = 'unicode é\n'.encode() * 22000
        job = self.job('input', stdin=payload)
        job.resume()
        self.wait_for(lambda: stopped(job))
        output = job.get_output()
        self.assertEqual((output.exit_code, output.stdout, output.stderr, output.overflow),
                         (0, payload, b'diagnostic', False))

    def test_both_output_streams_are_drained_and_capped(self):
        job = self.job('flood')
        job.resume()
        self.wait_for(lambda: stopped(job))
        output = job.get_output()
        self.assertEqual(output.exit_code, 0)
        self.assertTrue(output.overflow)
        self.assertEqual(output.stdout, b'o' * 1_048_576)
        self.assertEqual(output.stderr, b'e' * 262_144)

    def test_blocked_stdin_does_not_block_terminate_or_close(self):
        for method in ('terminate', 'close'):
            with self.subTest(method=method):
                (self.cwd / 'ready').unlink(missing_ok=True)
                with ct_winjob.spawn_suspended(self.launch('blocked', stdin=b'x' * 3_000_000),
                                              ct_winjob.new_job_name()) as job:
                    job.resume()
                    self.wait_for(lambda: (self.cwd / 'ready').exists())
                    start = time.monotonic()
                    getattr(job, method)()
                    self.assertLess(time.monotonic() - start, 2)
                    if method == 'terminate':
                        self.wait_for(lambda: stopped(job))
                        self.assertEqual(job.get_output().stdout, b'')

    def test_named_job_collision_and_failed_executable_fail_closed(self):
        job = self.job('blocked')
        with self.assertRaises(ct_winjob.JobError) as caught:
            ct_winjob.spawn_suspended(self.launch('blocked'), job.identity().job_name)
        self.assertEqual(caught.exception.code, 'JOB_CREATE_FAILED')
        self.assertEqual(job.poll().active_processes, 1)
        launch = self.launch('blocked')
        launch.executable = self.cwd / 'missing.exe'
        name = ct_winjob.new_job_name()
        with self.assertRaises(ct_winjob.JobError) as caught:
            ct_winjob.spawn_suspended(launch, name)
        self.assertEqual(caught.exception.code, 'PROCESS_CREATE_FAILED')
        with ct_winjob.spawn_suspended(self.launch('blocked'), name):
            pass

    def test_preexisting_inheritable_event_is_not_passed_to_child(self):
        create_event = ctypes.WinDLL('kernel32', use_last_error=True).CreateEventW
        create_event.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        create_event.restype = wintypes.HANDLE
        event = create_event(None, True, False, None)
        self.assertTrue(event)
        self.addCleanup(ct_winjob._api.CloseHandle, event)
        os.set_handle_inheritable(event, True)
        job = self.job('inherit', str(event))
        job.resume()
        self.wait_for(lambda: stopped(job))
        self.assertEqual(job.get_output().exit_code, 0)
        self.assertEqual(ct_winjob._api.WaitForSingleObject(event, 0), 258)

    def test_child_cannot_break_away(self):
        job = self.job('breakaway')
        job.resume()
        self.wait_for(lambda: stopped(job))
        self.assertEqual(job.get_output().stdout.strip(), b'blocked')

    def test_assignment_failure_cannot_fall_back_to_creating_a_process(self):
        name = ct_winjob.new_job_name()
        with mock.patch.object(ct_winjob._api, 'UpdateProcThreadAttribute', return_value=False), \
                mock.patch.object(ct_winjob._api, 'CreateProcessW') as create:
            with self.assertRaises(ct_winjob.JobError) as caught:
                ct_winjob.spawn_suspended(self.launch('blocked'), name)
            self.assertEqual(caught.exception.code, 'JOB_ASSIGNMENT_FAILED')
            create.assert_not_called()
        with ct_winjob.spawn_suspended(self.launch('blocked'), name):
            pass

    def test_resume_failure_drains_suspended_job(self):
        job = self.job('blocked')
        with mock.patch.object(ct_winjob._api, 'ResumeThread', return_value=0xFFFFFFFF):
            with self.assertRaises(ct_winjob.JobError) as caught:
                job.resume()
        self.assertEqual(caught.exception.code, 'PROCESS_RESUME_FAILED')
        self.wait_for(lambda: stopped(job))
        self.assertFalse((self.cwd / 'ready').exists())

    def test_struct_layout(self):
        is_64 = ctypes.sizeof(ctypes.c_void_p) == 8
        self.assertEqual(ctypes.sizeof(ct_winjob._StartupInfoEx), 112 if is_64 else 72)
        self.assertEqual(ctypes.sizeof(ct_winjob._ProcessInfo), 24 if is_64 else 16)
        self.assertEqual(ctypes.sizeof(ct_winjob._BasicLimits), 64 if is_64 else 48)
        self.assertEqual(ctypes.sizeof(ct_winjob._ExtendedLimits), 144 if is_64 else 112)
        self.assertEqual(ctypes.sizeof(ct_winjob._Accounting), 48)
        self.assertEqual(ct_winjob._Accounting.active.offset, 40)

    def supervisor(self, mode='root'):
        name = ct_winjob.new_job_name()
        proc = self.enterContext(subprocess.Popen(
            [sys.executable, str(FIXTURE), 'supervisor', str(SCRIPTS), name, mode], cwd=self.cwd,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

        def clean_fixture():
            if proc.poll() is None:
                proc.kill()
            proc.wait(5)

        self.addCleanup(clean_fixture)
        journal = self.cwd / 'identities.json'
        self.wait_for(lambda: journal.exists() and (self.cwd / 'child.json').exists())
        values = json.loads(journal.read_text(encoding='utf-8'))
        return proc, ct_winjob.ProcessIdentity(**values['worker']), ct_winjob.OwnerProcess(**values['owner'])

    def recovery(self, identity, owner):
        from ct_external_store import AttemptKey

        return ct_winjob.recover_tree(identity, owner, key=AttemptKey('fixture', 1),
                                      runner_claim='fixture-claim')

    def test_runner_crash_kills_tree_but_missing_same_boot_job_is_ambiguous(self):
        proc, identity, owner = self.supervisor()
        child_pid = json.loads((self.cwd / 'child.json').read_text(encoding='utf-8'))['pid']
        child = ct_winjob._api.OpenProcess(0x00100000, False, child_pid)
        self.assertTrue(child)
        self.addCleanup(ct_winjob._api.CloseHandle, child)
        self.assertIs(ct_winjob.owner_process_alive(owner), True)
        self.assertIsNone(self.recovery(identity, owner))
        proc.kill()
        proc.wait(5)
        self.assertIs(ct_winjob.owner_process_alive(owner), False)
        self.assertEqual(ct_winjob._api.WaitForSingleObject(child, 5000), 0)
        self.assertIsNone(self.recovery(identity, owner))
        (self.cwd / 'release').touch()
        self.assertFalse((self.cwd / 'late-write').exists())

    def test_owner_liveness_distinguishes_identity_mismatch_from_unknown(self):
        probe = ct_winjob.owner_process_alive
        owner = ct_winjob.current_owner_process()
        self.assertIs(probe(owner), True)
        self.assertIs(probe(replace(owner, creation_time=owner.creation_time + 1)), False)
        self.assertIs(probe(replace(owner, session_id=owner.session_id + 1)), False)
        self.assertIs(probe(replace(owner, boot_id=str(uuid.uuid4()))), False)
        self.assertIsNone(probe(replace(owner, pid=0)))
        self.assertIsNone(probe(replace(owner, boot_id='unverified')))

        def access_denied(*args):
            ctypes.set_last_error(5)
            return None

        with mock.patch.object(ct_winjob._api, 'OpenProcess', side_effect=access_denied):
            self.assertIsNone(probe(owner))
        with mock.patch.object(ct_winjob, 'boot_id', side_effect=ct_winjob.JobError('BOOT_ID_UNAVAILABLE')):
            self.assertIsNone(probe(owner))

    def test_recovery_needs_exact_root_identity_and_named_job_membership(self):
        proc, identity, owner = self.supervisor('root-wait')
        job = ct_winjob._api.OpenJobObjectW(0x0004, False, identity.job_name)
        self.assertTrue(job)
        self.addCleanup(ct_winjob._api.CloseHandle, job)
        self.assertEqual(identity.boot_id, ct_winjob.current_owner_process().boot_id)
        proc.kill()
        proc.wait(5)
        self.assertGreater(ct_winjob._accounting(job).active, 0)
        self.assertIsNone(self.recovery(replace(identity, creation_time=identity.creation_time + 1), owner))
        with ct_winjob.spawn_suspended(self.launch('blocked'), ct_winjob.new_job_name()) as other:
            self.assertIsNone(self.recovery(replace(identity, job_name=other.identity().job_name), owner))
            self.assertEqual(other.poll().active_processes, 1)
        self.assertGreater(ct_winjob._accounting(job).active, 0)
        proof = self.recovery(identity, owner)
        self.assertEqual((proof.basis, proof.identity, proof.runner_claim),
                         ('JOB_EMPTY', identity, 'fixture-claim'))
        self.assertEqual(ct_winjob._accounting(job).active, 0)

    def test_boot_proof_requires_a_consistent_persisted_boot_identity(self):
        job = self.job('blocked')
        identity = job.identity()
        owner = ct_winjob.current_owner_process()
        self.assertEqual((identity.boot_id, identity.session_id), (owner.boot_id, owner.session_id))
        previous = str(uuid.uuid4())
        self.assertIsNone(self.recovery(identity, replace(owner, boot_id=previous)))
        with mock.patch.object(ct_winjob, 'boot_id', return_value=previous):
            proof = self.recovery(identity, owner)
            self.assertEqual(proof.basis, 'BOOT_CHANGED')
            self.assertEqual(proof.identity, identity)
