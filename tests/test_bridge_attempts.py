"""Attempt authorization and recovery against real Git, files and SQLite."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sys
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
try:
    import ct_bridge_attempts as attempts
except ImportError:
    attempts = None


class AttemptTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(attempts, 'attempt lifecycle implementation is missing')
        from test_bridge_binding import BindingTests
        import ct_bridge_binding as binding
        import ct_bridge_control as control
        import ct_policy
        from ct_bridge_store import Store

        fixture = BindingTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        leader_skill, self.leader_manifest, _ = fixture.install()
        worker_skill, self.worker_manifest, _ = fixture.install('worker')
        self.leader = fixture.context(leader_skill)
        self.worker = fixture.context(worker_skill, 'worker',
                                      {'ANTIGRAVITY_CONVERSATION_ID': 'worker-chat'})
        self.binding = binding
        self.store = Store(self.leader.paths)
        self.policy = self.leader.own_binding().policy_path
        ct_policy.initialize_state(self.policy)
        ct_policy.initialize_policy_epochs(self.policy)
        self.expected = binding.LeaderRef(self.leader.actor, 1)
        self.round_id = 'R-attempt-fixture'
        with self.store.write() as db:
            db.execute('UPDATE group_state SET leader_platform=?,leader_chat=?,leader_manifest=?, '
                       'leader_epoch=1,desktop_enabled=1,desktop_epoch=1,auto_enabled=0,auto_epoch=1, '
                       'pair_epoch=1,worker_platform=?,worker_chat=?,worker_manifest=? WHERE id=1',
                       ('codex', 'leader-chat', self.leader_manifest.as_posix(), 'antigravity',
                        'worker-chat', self.worker_manifest.as_posix()))
            db.execute('INSERT INTO rounds(id,leader_epoch,leader_platform,leader_chat,'
                       'leader_manifest,opened_at) VALUES(?,1,?,?,?,?)',
                       (self.round_id,'codex','leader-chat',self.leader_manifest.as_posix(),time.time()))

    def request(self, publish_id='publication-1', mode='manual', task_id=None):
        return attempts.Request(publish_id=publish_id, content=b'Exact request\n', kind='EDIT',
                                mode=mode, scope=('source.txt',), baseline={'source.txt': None},
                                task_id=task_id)

    def publish(self, **kwargs):
        return attempts.publish(self.leader, self.expected, self.round_id, self.request(**kwargs))

    def key(self, view):
        from ct_bridge_files import AttemptKey
        return AttemptKey(view['task_id'], view['attempt_no'])

    def row(self, key):
        with self.store.read() as db:
            return dict(db.execute('SELECT * FROM attempts WHERE task_id=? AND attempt_no=?',
                                   (key.task_id, key.attempt_no)).fetchone())

    def enable_auto(self):
        with self.store.write() as db:
            db.execute('UPDATE group_state SET auto_enabled=1, auto_epoch=auto_epoch+1')

    def stop_listener(self, pool):
        with self.store.write() as db:
            db.execute('UPDATE group_state SET auto_enabled=0,auto_epoch=auto_epoch+1')
        pool.shutdown(wait=True)

    def listen_and_publish(self, publish_id='auto-publication'):
        self.enable_auto()
        pool = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.stop_listener,pool)
        future = pool.submit(attempts.listen, self.worker)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self.store.read() as db:
                if db.execute('SELECT 1 FROM waiter').fetchone():
                    break
            time.sleep(.02)
        else:
            self.fail('worker did not register a waiter')
        published = self.publish(publish_id=publish_id, mode='auto')
        self.assertTrue(published['ok'], published)
        return self.key(published), future.result(timeout=5)

    def test_rearmed_waiter_cannot_replay_previous_waiters_queue(self):
        import ct_policy
        self.enable_auto()
        stamp = json.dumps(asdict(ct_policy.policy_stamp(self.policy,'leader-chat',require_epochs=True)),
                           sort_keys=True,separators=(',',':'))
        with self.store.write() as db:
            group = db.execute('SELECT * FROM group_state').fetchone()
            db.execute('INSERT INTO waiter(id,nonce,leader_epoch,pair_epoch,auto_epoch,desktop_epoch,'
                       'policy_stamp,expires_at) VALUES(1,?,1,1,?,1,?,?)',
                       ('old-helper',group['auto_epoch'],stamp,time.time()+60))
        key = self.key(self.publish(mode='auto'))
        with self.store.write() as db:
            db.execute('DELETE FROM waiter')
        pool = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.stop_listener,pool)
        future = pool.submit(attempts.listen,self.worker)
        deadline = time.monotonic()+10
        while time.monotonic()<deadline and self.row(key)['state']=='QUEUED':
            time.sleep(.05)
        self.assertEqual(self.row(key)['state'],'REVOKED')
        self.assertFalse(future.done())

    def test_uncommitted_final_file_requires_retry_finish_and_wrong_chat_cannot_resume(self):
        key = self.key(self.publish())
        begun = attempts.begin(self.worker,key,None)
        attempts.finish(self.worker,key,begun['execution_ticket'],'OUTCOME_UNKNOWN',b'Connection lost')
        wrong = replace(self.worker,actor=self.binding.Actor('antigravity','wrong-chat'))
        self.assertFalse(attempts.resume(wrong,key,begun['execution_ticket'])['ok'])
        real = attempts.publish_file
        def interrupted(*args,**kwargs):
            real(*args,**kwargs)
            raise RuntimeError('receipt commit interrupted')
        with patch.object(attempts,'publish_file',side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError,'commit interrupted'):
                attempts.finish(self.worker,key,begun['execution_ticket'],'COMPLETED',b'final')
        self.assertEqual(self.row(key)['state'],'OUTCOME_UNKNOWN')
        self.assertFalse(attempts.resume(self.worker,key,begun['execution_ticket'])['ok'])
        finished = attempts.finish(self.worker,key,begun['execution_ticket'],'COMPLETED',b'final')
        self.assertEqual(finished['state'],'COMPLETED')

    def test_reconcile_recovers_verified_uncommitted_result_without_execution_secret(self):
        key = self.key(self.publish())
        begun = attempts.begin(self.worker,key,None)
        real = attempts.publish_file
        def interrupted(*args,**kwargs):
            real(*args,**kwargs)
            raise RuntimeError('worker lost after file publication')
        with patch.object(attempts,'publish_file',side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                attempts.finish(self.worker,key,begun['execution_ticket'],'COMPLETED',b'verified final')
        evidence = {'task_id':key.task_id,'attempt_no':key.attempt_no,'confirmed_by':'user',
                    'stopped':True,'summary':'Worker stopped; final file and scope diff verified.',
                    'state':'COMPLETED','result_sha256':hashlib.sha256(b'verified final').hexdigest()}
        recovered = attempts.reconcile(self.leader,self.expected,key,evidence)
        self.assertTrue(recovered['ok'],recovered)
        self.assertEqual(recovered['state'],'COMPLETED')
        replay = attempts.reconcile(self.leader,self.expected,key,evidence)
        self.assertEqual(recovered['result_id'],replay['result_id'])
        self.assertTrue(attempts.ack(self.leader,self.expected,key,recovered['result_id'])['ok'])

    def test_two_queued_targets_survive_healthy_rearm(self):
        import ct_policy
        self.enable_auto()
        stamp = json.dumps(asdict(ct_policy.policy_stamp(self.policy,'leader-chat',require_epochs=True)),
                           sort_keys=True,separators=(',',':'))
        with self.store.write() as db:
            epoch = db.execute('SELECT auto_epoch FROM group_state').fetchone()[0]
            db.execute('INSERT INTO waiter(id,nonce,leader_epoch,pair_epoch,auto_epoch,desktop_epoch,'
                       'policy_stamp,expires_at) VALUES(1,?,1,1,?,1,?,?)',
                       ('healthy-helper',epoch,stamp,time.time()+60))
        first = self.key(self.publish(publish_id='queue-first',mode='auto'))
        second = self.key(self.publish(publish_id='queue-second',mode='auto'))
        # A consumed registration is the handoff from a successful tool wake to
        # the next helper in the same chat, as distinct from a lost heartbeat.
        with self.store.write() as db:
            db.execute('UPDATE waiter SET expires_at=0')
        pool = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.stop_listener,pool)
        pending = pool.submit(attempts.listen,self.worker)
        deadline = time.monotonic()+10
        while time.monotonic()<deadline and self.row(first)['state']=='QUEUED':
            time.sleep(.05)
        self.assertEqual(self.row(first)['state'],'CLAIMED')
        claim = pending.result(timeout=5)
        begun = attempts.begin(self.worker,first,claim['claim'])
        attempts.finish(self.worker,first,begun['execution_ticket'],'COMPLETED',b'first done')
        next_claim = pool.submit(attempts.listen,self.worker).result(timeout=10)
        self.assertEqual(self.key(next_claim),second)
        self.assertTrue(attempts.begin(self.worker,second,next_claim['claim'])['ok'])

    def test_idle_listener_does_not_reenter_admission_every_poll(self):
        from threading import Event
        import ct_bridge_control as control

        self.enable_auto()
        polled = Event()
        store_type = type(self.store)
        real_poll = getattr(store_type, 'poll_read', store_type.read)

        def observe_poll(store):
            polled.set()
            return real_poll(store)

        pool = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.stop_listener, pool)
        with patch.object(store_type, 'poll_read', observe_poll, create=True), patch.object(
                control, '_guard', wraps=control._guard) as admission:
            future = pool.submit(attempts.listen, self.worker)
            self.assertTrue(polled.wait(timeout=8), 'the idle waiter never reached its read-only polling path')
            time.sleep(.55)
            self.assertEqual(admission.call_count, 1)
            with self.store.write() as db:
                db.execute('UPDATE group_state SET auto_enabled=0,auto_epoch=auto_epoch+1')
            self.assertEqual(future.result(timeout=8)['state'], 'STOPPED')

    def test_publish_manual_stays_manual_after_auto_on(self):
        draft = self.publish()
        self.assertEqual(draft['state'], 'WAITING_FOR_USER')
        key = self.key(draft)
        self.enable_auto()
        begun = attempts.begin(self.worker, key, None)
        self.assertTrue(begun['ok'], begun)
        self.assertEqual(begun['request'], 'Exact request\n')
        self.assertEqual(begun['scope'], ['source.txt'])
        self.assertEqual(self.row(key)['mode'], 'manual')
        self.assertFalse(attempts.begin(self.worker, key, None)['ok'])

    def test_retry_after_file_publish_before_db_commit(self):
        real = attempts.publish_file
        def interrupted(*args, **kwargs):
            real(*args, **kwargs)
            raise RuntimeError('file published before commit')
        with patch.object(attempts, 'publish_file', side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'before commit'):
                self.publish()
        with self.store.read() as db:
            reserved = db.execute('SELECT * FROM attempts').fetchone()
            self.assertEqual(reserved['state'], 'PUBLISHING')
            reserved_task = reserved['task_id']
        retried = self.publish()
        self.assertEqual(retried['task_id'], reserved_task)
        self.assertEqual(retried['state'], 'WAITING_FOR_USER')
        self.assertEqual(self.publish()['attempt_no'], 1)
        with self.assertRaises(ValueError):
            attempts.publish(self.leader, self.expected, self.round_id,
                             attempts.Request('publication-1', b'different'))

    def test_two_distinct_attempts_contend_for_one_slot(self):
        key1 = self.key(self.publish())
        key2 = self.key(self.publish(publish_id='publication-2'))
        gate = threading.Barrier(2)
        def start(key):
            gate.wait(timeout=5)
            return attempts.begin(self.worker, key, None)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(start, (key1, key2)))
        self.assertEqual(sum(view['ok'] for view in outcomes), 1)
        winner = next(view for view in outcomes if view['ok'])
        winner_key = self.key(winner)
        loser_key = key2 if winner_key == key1 else key1
        for state in ('NEEDS_USER', 'OUTCOME_UNKNOWN'):
            paused = attempts.finish(self.worker, winner_key, winner['execution_ticket'], state,
                                     b'Needs a response')
            self.assertEqual(paused['state'], state)
            self.assertFalse(attempts.begin(self.worker, loser_key, None)['ok'])
            resumed = attempts.resume(self.worker, winner_key, winner['execution_ticket'])
            self.assertEqual(resumed['state'], 'RUNNING')

    def test_begin_fences_saved_policy_and_desktop_epochs(self):
        import ct_policy
        import ct_storage
        key = self.key(self.publish())
        with ct_storage.locked(self.policy):
            state = ct_policy.load_state(self.policy)
            metadata = ct_policy._native_epochs(state)
            state['global_policy'] = 'OFF'
            ct_policy._save_native_change(self.policy, state, metadata)
            state = ct_policy.load_state(self.policy)
            metadata = ct_policy._native_epochs(state)
            state['global_policy'] = 'ON'
            ct_policy._save_native_change(self.policy, state, metadata)
        self.assertFalse(attempts.begin(self.worker, key, None)['ok'])
        self.assertTrue(attempts.reoffer(self.leader, self.expected, key, 'manual')['ok'])
        with self.store.write() as db:
            db.execute('UPDATE group_state SET desktop_epoch=desktop_epoch+2')
        self.assertFalse(attempts.begin(self.worker, key, None)['ok'])
        fresh = self.key(self.publish(publish_id='fresh'))
        self.assertTrue(attempts.begin(self.worker, fresh, None)['ok'])

    def test_listen_claim_is_once_and_auto_revocation_can_be_reoffered_manually(self):
        key, claim = self.listen_and_publish()
        self.assertEqual(claim['state'], 'CLAIMED')
        self.assertNotEqual(self.row(key)['claim_hash'], claim['claim'])
        with self.store.write() as db:
            db.execute('UPDATE group_state SET auto_enabled=0,auto_epoch=auto_epoch+1')
        self.assertFalse(attempts.begin(self.worker, key, claim['claim'])['ok'])
        self.assertTrue(attempts.reoffer(self.leader, self.expected, key, 'manual')['ok'])
        self.assertEqual(self.row(key)['mode'], 'manual')
        begun = attempts.begin(self.worker, key, None)
        self.assertTrue(begun['ok'], begun)
        self.assertIsNone(self.row(key)['claim_hash'])

    def test_finish_replay_returns_same_receipt_and_allows_finish_when_off(self):
        key = self.key(self.publish())
        begun = attempts.begin(self.worker, key, None)
        with self.store.write() as db:
            db.execute('UPDATE group_state SET desktop_enabled=0,desktop_epoch=desktop_epoch+1')
        result = b'Completed review data\n'
        finished = attempts.finish(self.worker, key, begun['execution_ticket'], 'COMPLETED', result)
        replay = attempts.finish(self.worker, key, begun['execution_ticket'], 'COMPLETED', result)
        self.assertEqual(finished['result_id'], replay['result_id'])
        self.assertEqual(finished['result_sha256'], hashlib.sha256(result).hexdigest())
        self.assertFalse(attempts.finish(self.worker, key, 'wrong', 'COMPLETED', result)['ok'])
        with self.assertRaises(ValueError):
            attempts.finish(self.worker, key, begun['execution_ticket'], 'COMPLETED', b'overwrite')

    def test_read_grant_is_bound_to_one_attempt_and_collect_is_read_only(self):
        key = self.key(self.publish())
        other = self.key(self.publish(publish_id='other'))
        begun = attempts.begin(self.worker, key, None)
        attempts.finish(self.worker,key,begun['execution_ticket'],'COMPLETED',b'data')
        grant = attempts.grant_read(self.leader,self.expected,key)['grant']
        before = self.leader.paths.db.read_bytes()
        receipt = attempts.collect(self.leader,key,grant,wait_seconds=0)
        self.assertTrue(receipt['ok'], receipt)
        self.assertEqual(receipt['result'], 'data')
        self.assertEqual(before,self.leader.paths.db.read_bytes())
        self.assertFalse(attempts.collect(self.leader,other,grant)['ok'])
        with self.store.write() as db:
            db.execute('UPDATE read_grants SET expires_at=0')
        self.assertFalse(attempts.collect(self.leader,key,grant)['ok'])
        self.assertTrue(attempts.collect(self.leader,key,None)['ok'])

    def test_ack_rechecks_result_bytes_and_next_attempt_requires_ack(self):
        from ct_bridge_files import publish_file
        key = self.key(self.publish())
        begun = attempts.begin(self.worker,key,None)
        finished = attempts.finish(self.worker,key,begun['execution_ticket'],'COMPLETED',b'done')
        self.assertFalse(self.publish(publish_id='retry', task_id=key.task_id)['ok'])
        self.assertFalse(attempts.ack(self.leader,self.expected,key,'wrong')['ok'])
        self.assertTrue(attempts.ack(self.leader,self.expected,key,finished['result_id'])['ok'])
        following = self.publish(publish_id='retry', task_id=key.task_id)
        self.assertEqual(following['attempt_no'],2)
        result = publish_file(self.leader.paths,key,'result',b'done')
        (self.leader.paths.data / result.relative_path).write_bytes(b'tampered')
        with self.assertRaises(ValueError):
            attempts.ack(self.leader,self.expected,key,finished['result_id'])

    def test_cancel_never_releases_started_writer_and_reconcile_requires_scoped_confirmation(self):
        key = self.key(self.publish())
        begun = attempts.begin(self.worker,key,None)
        self.assertFalse(attempts.cancel_pending(self.leader,self.expected,key)['ok'])
        self.assertFalse(attempts.reconcile(self.leader,self.expected,key,{'stopped':True})['ok'])
        evidence = {'task_id':key.task_id,'attempt_no':key.attempt_no,'confirmed_by':'user',
                    'stopped':True,'summary':'Worker stopped; scope diff reviewed.'}
        reconciled = attempts.reconcile(self.leader,self.expected,key,evidence)
        self.assertEqual(reconciled['state'],'CANCELED')
        self.assertFalse(attempts.resume(self.worker,key,begun['execution_ticket'])['ok'])
        pending = self.key(self.publish(publish_id='pending'))
        self.assertEqual(attempts.cancel_pending(self.leader,self.expected,pending)['state'],'CANCELED')
        self.assertFalse(attempts.begin(self.worker,pending,None)['ok'])

    def test_cleanup_requires_closed_reviewed_task_and_tombstones_before_deleting(self):
        self.assertTrue(callable(getattr(attempts,'cleanup',None)), 'explicit cleanup is missing')
        import ct_bridge_control as control
        key = self.key(self.publish())
        begun = attempts.begin(self.worker,key,None)
        finished = attempts.finish(self.worker,key,begun['execution_ticket'],'COMPLETED',b'done')
        grant = attempts.grant_read(self.leader,self.expected,key)['grant']
        self.assertFalse(attempts.cleanup(self.leader,self.expected,key.task_id)['ok'])
        attempts.ack(self.leader,self.expected,key,finished['result_id'])
        self.assertTrue(control.close_round(self.leader,self.expected,self.round_id,'Reviewed diff and tests')['ok'])
        innocent = self.leader.paths.root/'user.txt'
        innocent.write_bytes(b'keep staged data')
        subprocess.run(['git','-C',str(self.leader.paths.root),'add','user.txt'],check=True,capture_output=True)
        index = self.leader.paths.root/'.git/index'
        original_index = index.read_bytes()
        with patch.object(attempts,'cleanup_task',side_effect=RuntimeError('unlink interrupted')):
            with self.assertRaisesRegex(RuntimeError,'unlink interrupted'):
                attempts.cleanup(self.leader,self.expected,key.task_id)
        self.assertFalse(attempts.collect(self.leader,key,grant)['ok'])
        self.assertEqual(attempts.collect(self.leader,key,None)['state'],'CLEANED')
        self.assertFalse(attempts.grant_read(self.leader,self.expected,key)['ok'])
        self.assertTrue(attempts.cleanup(self.leader,self.expected,key.task_id)['ok'])
        self.assertTrue(attempts.cleanup(self.leader,self.expected,key.task_id)['ok'])
        attempt_dir = self.leader.paths.data/key.task_id/'attempt-1'
        self.assertFalse((attempt_dir/'request.md').exists())
        self.assertFalse((attempt_dir/'result.md').exists())
        self.assertEqual(innocent.read_bytes(),b'keep staged data')
        self.assertEqual(index.read_bytes(),original_index)


if __name__ == '__main__':
    unittest.main()
