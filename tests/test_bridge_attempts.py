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
        self.fixture = fixture
        self.worker_skill = worker_skill
        self.store = Store(self.leader.paths)
        self.policy = self.leader.own_binding().policy_path
        ct_policy.initialize_state(self.policy)
        ct_policy.initialize_policy_epochs(self.policy)
        self.expected = binding.LeaderRef(self.leader.actor, 1)
        self.round_id = 'R-attempt-fixture'
        subprocess.run(['git', '-C', str(fixture.workspace), '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-qm', 'baseline'],
                       check=True, capture_output=True)
        self.head = subprocess.check_output(['git', '-C', str(fixture.workspace), 'rev-parse', 'HEAD'],
                                            text=True).strip()
        with self.store.write() as db:
            db.execute('UPDATE group_state SET leader_platform=?,leader_chat=?,leader_manifest=?, '
                       'leader_epoch=1 WHERE id=1', ('codex', 'leader-chat', self.leader_manifest.as_posix()))
            db.execute("INSERT INTO peers(platform,enabled,desktop_epoch,auto_epoch) VALUES ('antigravity',1,1,1)")
            db.execute("INSERT INTO workers(worker_id,platform,chat_id,manifest,leader_epoch,registered_at) "
                       "VALUES ('worker-1','antigravity','worker-chat',?,1,?)", (self.worker_manifest.as_posix(), time.time()))
            db.execute('INSERT INTO rounds(id,leader_epoch,leader_platform,leader_chat,'
                       'leader_manifest,opened_at) VALUES(?,1,?,?,?,?)',
                       (self.round_id,'codex','leader-chat',self.leader_manifest.as_posix(),time.time()))

    def request(self, publish_id='publication-1', mode='manual', task_id=None):
        return self.role_request(publish_id, mode=mode, task_id=task_id)

    def add_worker(self, number):
        worker_id, chat = f'worker-{number}', f'worker-chat-{number}'
        with self.store.write() as db:
            db.execute("INSERT INTO workers(worker_id,platform,chat_id,manifest,leader_epoch,registered_at) "
                       "VALUES (?,'antigravity',?,?,1,?)", (worker_id, chat, self.worker_manifest.as_posix(), time.time()))
        return self.fixture.context(self.worker_skill, 'worker', {'ANTIGRAVITY_CONVERSATION_ID': chat})

    def role_request(self, publish_id, worker_id='worker-1', *, mode='manual',
                     item='item-1', path='source.txt', variant='ct-developer', task_id=None):
        edit = variant == 'ct-developer'
        return attempts.Request(
            publish_id=publish_id, mode=mode, worker_id=worker_id,
            assignment={'variant': variant, 'work_item_id': item, 'depends_on': {},
                        'snapshot': {'head': self.head, 'inputs': {}}, 'read_paths': [],
                        'write_paths': [path] if edit else [],
                        'check_outputs': [], 'test_paths': []},
            baseline={path: 'ABSENT'} if edit else {}, content=b'Exact request\n', task_id=task_id)

    def offer(self, *args, **kwargs):
        return attempts.publish(self.leader, self.expected, self.round_id,
                                self.role_request(*args, **kwargs))

    def test_role_assignment_is_authoritative_at_begin(self):
        offer = self.offer('assigned')
        self.assertEqual((offer['worker_id'], offer['role'], offer['work_item_id']),
                         ('worker-1', 'ct-developer', 'item-1'))
        started = attempts.begin(self.worker, self.key(offer), None)
        self.assertEqual((started['worker_id'], started['role'], started['work_item_id']),
                         ('worker-1', 'ct-developer', 'item-1'))

    def test_two_work_items_cannot_both_reserve_one_worker(self):
        barrier = threading.Barrier(2)

        def reserve(index):
            barrier.wait(timeout=5)
            return self.offer(f'race-{index}', item=f'item-{index}', path=f'source-{index}.txt')

        with ThreadPoolExecutor(max_workers=2) as pool:
            offers = list(pool.map(reserve, (1, 2)))
        self.assertEqual(sum(offer['ok'] for offer in offers), 1)

    def test_one_work_item_owner_across_disjoint_desktop_and_native_dispatches(self):
        import ct_bridge_control as control

        self.add_worker(2)
        self.add_worker(3)
        self.add_worker(4)

        def race(left, right):
            ready = threading.Barrier(2)

            def run(operation):
                ready.wait(timeout=10)
                return operation()

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(run, (left, right)))
            self.assertEqual(sum(value['ok'] for value in outcomes), 1, outcomes)
            loser = next(value for value in outcomes if not value['ok'])
            self.assertEqual(loser['reason'], 'WORK_ITEM_OWNED', outcomes)

        race(lambda: self.offer('one-a', item='same-read', variant='ct-researcher'),
             lambda: self.offer('one-b', 'worker-2', item='same-read', variant='ct-researcher'))
        native_assignment = self.role_request('native', item='mixed-read', variant='ct-researcher').assignment
        race(lambda: control.register_native(self.leader, self.expected, self.round_id, 'native', native_assignment),
             lambda: self.offer('two-b', 'worker-3', item='mixed-read', variant='ct-researcher'))
        spare = self.offer('spare', 'worker-4', item='distinct-read', variant='ct-researcher')
        self.assertTrue(spare['ok'], spare)
        with self.store.read() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM work_items').fetchone()[0], 3)
            self.assertEqual(db.execute('SELECT count(*) FROM participants').fetchone()[0], 3)

    def test_partial_publish_holds_reservation_and_other_workers_progress(self):
        second = self.add_worker(2)
        with patch.object(attempts, 'publish_file', side_effect=RuntimeError('publish interrupted')):
            with self.assertRaisesRegex(RuntimeError, 'publish interrupted'):
                self.offer('interrupted')
        self.assertFalse(self.offer('same-worker', item='item-2', path='other.txt')['ok'])
        self.assertFalse(self.offer('same-scope', 'worker-2', item='item-3')['ok'])
        independent = self.offer('independent', 'worker-2', item='item-4', path='other.txt')
        self.assertTrue(attempts.begin(second, self.key(independent), None)['ok'])
        self.assertEqual(self.offer('interrupted')['state'], 'WAITING_FOR_USER')

    def test_terminal_writer_can_rearm_before_ack_while_scope_is_held(self):
        import ct_bridge_control as control

        self.add_worker(2)
        key = self.key(self.publish())
        begin = attempts.begin(self.worker, key, None)
        attempts.finish(self.worker, key, begin['execution_ticket'], 'COMPLETED', b'Ready for review')
        self.enable_auto()
        status = control.worker_status(self.worker)
        self.assertEqual(status['reason'], 'WAITER_NOT_READY')
        self.assertTrue(status['connections'][0]['workers'][0]['review_pending'])
        pool = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.stop_listener, pool)
        waiter = pool.submit(attempts.listen, self.worker)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not control.worker_status(self.worker)['ready']:
            self.assertFalse(waiter.done())
            time.sleep(.05)
        self.assertTrue(control.worker_status(self.worker)['ready'])
        conflict = self.offer('before-ack', 'worker-2', item='other')
        self.assertEqual(conflict['reason'], 'SCOPE_CONFLICT')

    def publish(self, **kwargs):
        return attempts.publish(self.leader, self.expected, self.round_id, self.request(**kwargs))

    def key(self, view):
        from ct_bridge_files import AttemptKey
        return AttemptKey(view['task_id'], view['attempt_no'])

    def row(self, key):
        with self.store.read() as db:
            return dict(db.execute('SELECT * FROM attempts WHERE task_id=? AND attempt_no=?',
                                   (key.task_id, key.attempt_no)).fetchone())

    def evidence(self, key, **extra):
        return {'task_id': key.task_id, 'attempt_no': key.attempt_no,
                'snapshot': {'head': self.head, 'inputs': {}},
                'summary': 'Verified scoped result', 'scope_verified': True, **extra}

    def ack(self, key, result_id):
        return attempts.ack(self.leader, self.expected, key, result_id, self.evidence(key))

    def enable_auto(self):
        with self.store.write() as db:
            db.execute('UPDATE peers SET auto_enabled=1, auto_epoch=auto_epoch+1')

    def stop_listener(self, pool):
        with self.store.write() as db:
            db.execute('UPDATE peers SET auto_enabled=0,auto_epoch=auto_epoch+1')
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
            epoch = db.execute('SELECT auto_epoch FROM peers').fetchone()[0]
            db.execute('INSERT INTO waiter(worker_id,nonce_hash,leader_epoch,auto_epoch,desktop_epoch,'
                       "policy_stamp,expires_at) VALUES('worker-1',?,1,?,1,?,?)",
                       ('old-helper', epoch, stamp, time.time()+60))
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
        with self.assertRaisesRegex(ValueError, 'WRONG_WORKER'):
            attempts.resume(wrong,key,begun['execution_ticket'])
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
        evidence = self.evidence(key, confirmed_by='user', stopped=True, state='COMPLETED',
                                 result_sha256=hashlib.sha256(b'verified final').hexdigest())
        recovered = attempts.reconcile(self.leader,self.expected,key,evidence)
        self.assertTrue(recovered['ok'],recovered)
        self.assertEqual(recovered['state'],'COMPLETED')
        replay = attempts.reconcile(self.leader,self.expected,key,evidence)
        self.assertEqual(recovered['result_id'],replay['result_id'])
        self.assertTrue(self.ack(key, recovered['result_id'])['ok'])

    def test_many_workers_claim_only_assigned_attempt_and_run_together(self):
        for count in (1, 2, 3, 9):
            with self.subTest(count=count):
                self.run_worker_group(count)

    def run_worker_group(self, count):
        for number in range(getattr(self, 'registered_workers', 1) + 1, count + 1):
            self.add_worker(number)
        self.registered_workers = count
        workers = [self.worker, *(self.fixture.context(self.worker_skill, 'worker',
                    {'ANTIGRAVITY_CONVERSATION_ID': f'worker-chat-{n}'}) for n in range(2, count + 1))]
        self.enable_auto()
        pool = ThreadPoolExecutor(max_workers=count)
        self.addCleanup(self.stop_listener, pool)
        listeners = [pool.submit(attempts.listen, worker) for worker in workers]
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            with self.store.read() as db:
                if db.execute('SELECT count(*) FROM waiter').fetchone()[0] == count:
                    break
            time.sleep(.03)
        else:
            self.fail('listeners did not all register')
        keys = []
        for n in range(1, count + 1):
            offer = self.offer(f'queue-{count}-{n}', f'worker-{n}', mode='auto',
                               item=f'item-{count}-{n}', variant='ct-researcher')
            self.assertTrue(offer['ok'], (count, n, offer))
            keys.append(self.key(offer))
        claims = [listener.result(timeout=35) for listener in listeners]
        self.assertEqual([self.key(claim) for claim in claims], keys)
        starts = [attempts.begin(worker, key, claim['claim']) for worker, key, claim in zip(workers, keys, claims)]
        self.assertTrue(all(start['state'] == 'RUNNING' for start in starts), starts)
        self.assertTrue(all(self.row(key)['state'] == 'RUNNING' for key in keys))
        for worker, key, start in reversed(list(zip(workers, keys, starts))):
            result = attempts.finish(worker, key, start['execution_ticket'], 'COMPLETED',
                                     key.task_id.encode('ascii'))
            self.assertEqual(attempts.collect(self.leader, key, None)['result'], key.task_id)
            self.assertTrue(self.ack(key, result['result_id'])['ok'])
        pool.shutdown()

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
                control, 'operation_guard', wraps=control.operation_guard) as admission:
            future = pool.submit(attempts.listen, self.worker)
            self.assertTrue(polled.wait(timeout=8), 'the idle waiter never reached its read-only polling path')
            time.sleep(.55)
            self.assertEqual(admission.call_count, 1)
            with self.store.write() as db:
                db.execute('UPDATE peers SET auto_enabled=0,auto_epoch=auto_epoch+1')
            self.assertEqual(future.result(timeout=8)['state'], 'STOPPED')

    def test_publish_manual_stays_manual_after_auto_on(self):
        draft = self.publish()
        self.assertEqual(draft['state'], 'WAITING_FOR_USER')
        key = self.key(draft)
        self.enable_auto()
        begun = attempts.begin(self.worker, key, None)
        self.assertTrue(begun['ok'], begun)
        self.assertEqual(begun['request'], 'Exact request\n')
        self.assertEqual(begun['assignment']['write_paths'], ['source.txt'])
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
                             replace(self.request(), content=b'different'))

    def test_two_distinct_attempts_contend_for_one_slot(self):
        key1 = self.key(self.publish())
        gate = threading.Barrier(2)
        def start(key):
            gate.wait(timeout=5)
            return attempts.begin(self.worker, key, None)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(start, (key1, key1)))
        self.assertEqual(sum(view['ok'] for view in outcomes), 1)
        winner = next(view for view in outcomes if view['ok'])
        winner_key = self.key(winner)
        for state in ('NEEDS_USER', 'OUTCOME_UNKNOWN'):
            paused = attempts.finish(self.worker, winner_key, winner['execution_ticket'], state,
                                     b'Needs a response')
            self.assertEqual(paused['state'], state)
            self.assertFalse(self.offer('publication-2', item='second', path='other.txt')['ok'])
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
            db.execute('UPDATE peers SET desktop_epoch=desktop_epoch+2')
        self.assertFalse(attempts.begin(self.worker, key, None)['ok'])
        fresh = self.key(self.offer('fresh', item='another'))
        self.assertTrue(attempts.begin(self.worker, fresh, None)['ok'])

    def test_listen_claim_is_once_and_auto_revocation_can_be_reoffered_manually(self):
        key, claim = self.listen_and_publish()
        self.assertEqual(claim['state'], 'CLAIMED')
        self.assertNotEqual(self.row(key)['claim_hash'], claim['claim'])
        with self.store.write() as db:
            db.execute('UPDATE peers SET auto_enabled=0,auto_epoch=auto_epoch+1')
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
            db.execute('UPDATE peers SET enabled=0,desktop_epoch=desktop_epoch+1')
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
        self.add_worker(2)
        other = self.key(self.offer('other','worker-2',item='other', path='other.txt'))
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
        self.assertFalse(self.ack(key, 'wrong')['ok'])
        self.assertTrue(self.ack(key, finished['result_id'])['ok'])
        following = self.publish(publish_id='retry', task_id=key.task_id)
        self.assertEqual(following['attempt_no'],2)
        result = publish_file(self.leader.paths,key,'result',b'done')
        (self.leader.paths.data / result.relative_path).write_bytes(b'tampered')
        with self.assertRaises(ValueError):
            self.ack(key, finished['result_id'])

    def test_cancel_never_releases_started_writer_and_reconcile_requires_scoped_confirmation(self):
        key = self.key(self.publish())
        begun = attempts.begin(self.worker,key,None)
        self.assertFalse(attempts.cancel_pending(self.leader,self.expected,key)['ok'])
        self.assertFalse(attempts.reconcile(self.leader,self.expected,key,{'stopped':True})['ok'])
        evidence = self.evidence(key, confirmed_by='user', stopped=True)
        reconciled = attempts.reconcile(self.leader,self.expected,key,evidence)
        self.assertEqual(reconciled['state'],'CANCELED')
        self.assertFalse(attempts.resume(self.worker,key,begun['execution_ticket'])['ok'])
        pending = self.key(self.offer('pending',item='pending'))
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
        self.ack(key, finished['result_id'])
        self.assertTrue(control.complete_work_item(self.leader,self.expected,self.round_id,'item-1',
            outcome='accepted',snapshot={'head':self.head,'inputs':{'source.txt':'ABSENT'}},evidence='Verified')['ok'])
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
