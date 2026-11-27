"""Control invariants exercised against isolated real SQLite worktrees."""
from pathlib import Path
from unittest import mock
import hashlib
import json
import shutil
import subprocess
import sys
import threading
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
try:
    import ct_bridge_control as control
except ModuleNotFoundError:
    control = None


class BridgeControlTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(control, 'bridge control implementation is missing')
        import ct_bridge_binding as binding
        import ct_bridge_files as files
        import ct_policy
        self.binding = binding
        self.base = Path(__file__).resolve().parents[1] / ('.ct-control-' + uuid.uuid4().hex)
        self.base.mkdir()
        self.addCleanup(self.cleanup_fixture)
        self.root = self.base / 'worktree'
        self.root.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True, capture_output=True)
        self.paths = files.locate_workspace(self.root)
        self.home = self.base / 'home'
        self.custom = self.home / 'custom-codex'
        # Use actual managed component discovery and locking. Payload scripts are
        # inert fixture bytes; this process imports the real source implementation.
        self.policy = self.custom / 'coding-team/state.json'
        ct_policy.initialize_state(self.policy, 'ON')
        with control.ct_storage.locked(self.policy):
            ct_policy.initialize_policy_epochs(self.policy)
        self.leader_manifest = self.install('leader')
        self.worker_manifest = self.install('worker')
        self.a = self.context('codex', 'chat-a')
        self.b = self.context('codex', 'chat-b')
        self.worker = self.context('antigravity', 'worker-a', component='worker')

    def cleanup_fixture(self):
        # Windows scanners may briefly hold a directory after Git exits.
        for attempt in range(30):
            try:
                shutil.rmtree(self.base)
                return
            except PermissionError:
                if attempt == 29:
                    raise
                time.sleep(.1)

    def context(self, platform, chat, component='leader'):
        env = {'CODEX_HOME': str(self.custom)}
        if chat:
            env['CODEX_THREAD_ID' if platform == 'codex' else 'ANTIGRAVITY_CONVERSATION_ID'] = chat
        return self.binding.ctx_from_runtime(platform, self.root, env, component=component,
            source_root=(self.worker_manifest if component == 'worker' else self.leader_manifest).parent,
            home=self.home)

    def install(self, component):
        platform = 'codex' if component == 'leader' else 'antigravity'
        skill = (self.home / '.agents/skills/coding-team' if component == 'leader' else
                 self.home / '.gemini/config/skills/coding-team-desktop-worker')
        names = ('ct_bridge_binding', 'ct_bridge_files', 'ct_bridge_store', 'ct_platform', 'ct_storage',
                 'ct_policy', 'ct_bridge_control', 'ct_bridge_attempts', 'ct_bridge_install', 'ct_bridge_commands')
        names += ('coding_team', 'ct_bridge_cli') if component == 'leader' else ('ct_bridge_worker',)
        files = {'SKILL.md': b'fixture skill\n'}
        files.update({f'scripts/{name}.py': f'# fixture {name}\n'.encode() for name in names})
        files[f'platforms/{platform}.json'] = (Path(__file__).resolve().parents[1] / 'platforms' / f'{platform}.json').read_bytes()
        for relative, data in files.items():
            path = skill / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        lock = (self.custom / 'coding-team/install.lock' if component == 'leader' else
                self.home / '.gemini/antigravity/coding-team-desktop-worker/install.lock')
        record = {'schema_version': 1, 'bridge_abi': 1, 'component': component, 'platform': platform,
                  'scope': 'user', 'install_lock': lock.as_posix(),
                  'files': {'skill_dir': {rel: hashlib.sha256(data).hexdigest() for rel, data in files.items()}}}
        if component == 'leader':
            record['state_path'] = self.policy.as_posix()
        manifest = skill / 'install-manifest.json'
        manifest.write_text(json.dumps(record), encoding='utf-8')
        return manifest

    def select(self, ctx=None):
        ctx = ctx or self.a
        view = control.leader_on(ctx)
        self.assertTrue(view['ok'], view)
        return self.binding.LeaderRef(ctx.actor, view['leader_epoch'])

    def configure(self, action='auto_on', ctx=None):
        self.assertTrue(hasattr(control, 'configure_peer'), 'atomic inter-app configuration is missing')
        return control.configure_peer(ctx or self.a, 'antigravity', action)

    def group(self):
        with control.Store(self.paths).read() as db:
            return dict(db.execute('SELECT * FROM group_state').fetchone())

    def test_configure_implicit_leader_and_modes(self):
        view = self.configure('auto_off')
        self.assertTrue(view['ok'], view)
        self.assertTrue(view['desktop_enabled'])
        self.assertFalse(view['auto_enabled'])
        first = self.group()
        self.assertRegex(first['invite_hash'], r'^listen:[0-9a-f]{32}$')
        self.assertNotIn('listen:', json.dumps(view))
        again = self.configure('on')
        self.assertEqual(again['pair_epoch'], first['pair_epoch'])
        self.assertEqual(self.group()['invite_hash'], first['invite_hash'])
        self.assertTrue(control.join_peer(self.worker)['ok'])
        paired = self.group()
        auto = self.configure()
        self.assertTrue(auto['auto_enabled'])
        self.assertEqual(auto['pair_epoch'], paired['pair_epoch'])
        self.assertTrue(self.configure('on')['auto_enabled'])
        self.assertFalse(self.configure('auto_off')['auto_enabled'])
        self.assertEqual(self.group()['worker_chat'], 'worker-a')

    def test_activate_during_drain_keeps_result_authority(self):
        for state in ('RUNNING', 'NEEDS_USER', 'OUTCOME_UNKNOWN', 'COMPLETED'):
            with self.subTest(state=state):
                self.assertTrue(self.configure()['ok'])
                self.assertTrue(control.join_peer(self.worker)['ok'])
                ref = self.binding.LeaderRef(self.a.actor, self.group()['leader_epoch'])
                rid = control.open_round(self.a, ref)
                self.attempt_fixture(ref, rid, state, 'auto', 'CT-' + state)
                if state == 'COMPLETED':
                    with control.Store(self.paths).write() as db:
                        db.execute("UPDATE attempts SET result_id='receipt' WHERE task_id=?", ('CT-' + state,))
                self.assertTrue(self.configure('off')['ok'])
                preserved = self.group()
                for action in ('on', 'auto_on', 'auto_off'):
                    denied = self.configure(action)
                    self.assertEqual(denied['reason'], 'LEADER_BUSY', denied)
                    self.assertEqual(self.group(), preserved)
                self.assertEqual(control.set_desktop(self.a, ref, True)['reason'], 'LEADER_BUSY')
                self.assertEqual(preserved['desktop_enabled'], 0)
                self.assertEqual(preserved['worker_chat'], 'worker-a')
                with control.Store(self.paths).write() as db:
                    db.execute("UPDATE attempts SET state='COMPLETED',ack_at=1200 WHERE task_id=?", ('CT-' + state,))
                self.assertTrue(control.close_round(self.a, ref, rid, 'review complete')['ok'])
                enabled = control.set_desktop(self.a, ref, True)
                self.assertTrue(enabled['ok'], enabled)
                self.assertFalse(self.group()['auto_enabled'])
                self.assertIsNone(self.group()['worker_chat'])
                self.assertGreater(self.group()['pair_epoch'], preserved['pair_epoch'])

    def test_join_only_matching_current_invite(self):
        self.assertTrue(self.configure()['ok'])
        clock = [1000.0]
        real = control.Store
        with mock.patch.object(control, 'Store', lambda paths: real(paths, clock=lambda: clock[0])):
            self.configure('off')
            self.configure()
            clock[0] = 1600.0
            self.assertEqual(control.join_peer(self.worker)['reason'], 'EXPIRED')
            renewed = self.configure()
            self.assertTrue(renewed['ok'])
            marker = self.group()['invite_hash']
            self.assertFalse(control.connect(self.worker, marker)['ok'])
            self.assertTrue(control.join_peer(self.worker)['ok'])
            self.assertTrue(control.join_peer(self.worker)['ok'])
            other = self.context('antigravity', 'worker-b', 'worker')
            self.assertEqual(control.join_peer(other)['reason'], 'WRONG_WORKER')
            self.assertEqual(control.join_peer(self.context('antigravity', None, 'worker'))['reason'], 'UNKNOWN_CHAT')
        ref = self.binding.LeaderRef(self.a.actor, self.group()['leader_epoch'])
        ticket = control.invite(self.a, ref)['ticket']
        self.assertEqual(control.join_peer(self.worker)['reason'], 'NO_INVITE')
        self.assertTrue(control.connect(self.worker, ticket)['ok'])

    def test_join_race_and_stale_observation(self):
        self.configure()
        # Change the authority at the same manifest location between observation
        # and the real lock acquisition. A stale listen cannot join the new chat.
        from contextlib import contextmanager
        real = control._guard
        @contextmanager
        def raced(ctx, **kwargs):
            self.assertTrue(self.configure(ctx=self.b)['ok'])
            with real(ctx, **kwargs) as result:
                yield result
        with mock.patch.object(control, '_guard', raced):
            self.assertEqual(control.join_peer(self.worker)['reason'], 'STALE_INVITE')
        gate = threading.Barrier(2)
        results = []
        def join(ctx):
            gate.wait()
            results.append(control.join_peer(ctx))
        threads = [threading.Thread(target=join, args=(ctx,)) for ctx in
                   (self.worker, self.context('antigravity', 'worker-b', 'worker'))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sum(result['ok'] for result in results), 1, results)
        self.assertIn(next(result['reason'] for result in results if not result['ok']), ('WRONG_WORKER', 'STALE_INVITE'))

    def test_connection_status_by_local_role(self):
        self.configure('auto_off')
        initial = control.connection_status(self.worker)
        self.assertEqual(initial['connections'][0]['readiness'], 'PAIRING')
        self.assertEqual(initial['connections'][0]['next_action'], '/coding-team listen')
        self.assertTrue(control.join_peer(self.worker)['ok'])
        before = self.paths.db.read_bytes()
        files = {p.relative_to(self.base).as_posix() for p in self.base.rglob('*')}
        for ctx, role in ((self.a, 'leader'), (self.b, 'unbound'), (self.worker, 'worker'),
                          (self.context('antigravity', 'other', 'worker'), 'unbound'),
                          (self.context('antigravity', None, 'worker'), 'unknown')):
            status = control.connection_status(ctx)
            self.assertEqual(status['role'], role, status)
            self.assertEqual(status['workspace'], str(self.root.resolve()))
            self.assertEqual(status['leader']['chat_id'], 'chat-a')
            self.assertNotIn('listen:', json.dumps(status))
            if ctx.component == 'worker' and role != 'worker':
                self.assertFalse(status['connections'][0]['paired'])
                self.assertFalse(status['connections'][0]['waiter_ready'])
        self.assertEqual(control.connection_status(self.worker)['connections'][0]['readiness'], 'MANUAL_READY')
        self.assertEqual(before, self.paths.db.read_bytes())
        self.configure()
        self.assertEqual(control.connection_status(self.worker)['connections'][0]['readiness'], 'WAITING')
        self.configure('auto_off')
        # No reads changed a byte or created a sidecar.
        self.assertEqual(files, {p.relative_to(self.base).as_posix() for p in self.base.rglob('*')})
        self.assertTrue(before)

    def test_wait_ready_only_for_current_activation(self):
        view = self.configure()
        ref = self.binding.LeaderRef(self.a.actor, view['leader_epoch'])
        epochs = {name: view[name] for name in ('pair_epoch', 'desktop_epoch', 'auto_epoch')}
        before = self.paths.db.read_bytes()
        waiting = control.wait_ready(self.a, ref, **epochs, wait_seconds=0)
        self.assertEqual(waiting['state'], 'WAITING', waiting)
        self.assertIsNone(self.group()['worker_chat'])
        self.assertEqual(before, self.paths.db.read_bytes())
        self.assertEqual(control.wait_ready(self.b, ref, **epochs, wait_seconds=0)['reason'], 'STALE_ACTIVATION')
        for invalid in (True, -1, 61, 1.5):
            self.assertEqual(control.wait_ready(self.a, ref, **epochs, wait_seconds=invalid)['reason'], 'INVALID_WAIT_SECONDS')
        self.assertTrue(control.join_peer(self.worker)['ok'])
        self.configure('auto_off')
        self.assertEqual(control.wait_ready(self.a, ref, **epochs, wait_seconds=0)['reason'], 'STALE_ACTIVATION')
        self.assertEqual(control.wait_ready(self.a, ref, **epochs, wait_seconds=0)['connections'], [])

    def test_wait_ready_observes_waiter_policy_and_expiry_without_writes(self):
        from dataclasses import asdict
        view = self.configure()
        control.join_peer(self.worker)
        ref = self.binding.LeaderRef(self.a.actor, view['leader_epoch'])
        epochs = {name: view[name] for name in ('pair_epoch', 'desktop_epoch', 'auto_epoch')}
        def listener_appears(_):
            with control.Store(self.paths).write() as db:
                row = db.execute('SELECT * FROM group_state').fetchone()
                stamp = control._policy(self.a.own_binding(), row)
                db.execute('''INSERT INTO waiter(id,nonce,leader_epoch,pair_epoch,auto_epoch,
                    desktop_epoch,policy_stamp,expires_at) VALUES(1,'private',?,?,?,?,?,?)''',
                    (row['leader_epoch'], row['pair_epoch'], row['auto_epoch'], row['desktop_epoch'],
                     json.dumps(asdict(stamp), sort_keys=True, separators=(',', ':')), time.time() + 30))
        with mock.patch.object(control.time, 'sleep', listener_appears):
            ready = control.wait_ready(self.a, ref, **epochs, wait_seconds=1)
        self.assertEqual(ready['state'], 'READY', ready)
        self.assertEqual(ready['connections'][0]['readiness'], 'AUTO_READY')
        self.assertNotIn('private', json.dumps(ready))
        before = self.paths.db.read_bytes()
        self.assertEqual(control.wait_ready(self.a, ref, **epochs, wait_seconds=0)['state'], 'READY')
        self.assertEqual(before, self.paths.db.read_bytes())
        with control.Store(self.paths).write() as db:
            db.execute('UPDATE waiter SET expires_at=0')
        self.assertEqual(control.wait_ready(self.a, ref, **epochs, wait_seconds=0)['state'], 'WAITING')
        import ct_policy
        ct_policy.apply_command(self.policy, '/coding-team off', 'chat-a')
        self.assertEqual(control.wait_ready(self.a, ref, **epochs, wait_seconds=0)['reason'], 'NATIVE_POLICY_OFF')
        self.assertTrue(self.configure('off')['ok'])

    def test_invalid_activation_is_nonmutating_and_policy_off_cannot_initialize(self):
        import ct_policy
        before = self.policy.read_bytes()
        files = {p.relative_to(self.base).as_posix() for p in self.base.rglob('*')}
        self.assertEqual(control.configure_peer(self.a, 'codex', 'on')['reason'], 'UNSUPPORTED_PAIR')
        self.assertEqual(control.configure_peer(self.a, 'claude-code', 'on')['reason'], 'UNSUPPORTED_PAIR')
        self.assertEqual(control.configure_peer(self.a, 'antigravity', 'garbage')['reason'], 'INVALID_ACTION')
        self.assertEqual(control.configure_peer(self.worker, 'antigravity', 'on')['reason'], 'UNSUPPORTED_LEADER')
        self.assertEqual(control.connection_status(self.context('antigravity', None, 'worker'))['role'], 'unknown')
        self.assertFalse(self.paths.db.exists())
        self.assertEqual(before, self.policy.read_bytes())
        self.assertEqual(files, {p.relative_to(self.base).as_posix() for p in self.base.rglob('*')})
        ct_policy.apply_command(self.policy, '/coding-team off', 'chat-a')
        self.assertEqual(self.configure()['reason'], 'NATIVE_POLICY_OFF')
        self.assertFalse(self.paths.db.exists())

    def test_off_preserves_real_receipt_and_blocks_pending_start(self):
        import ct_bridge_attempts as attempts
        from ct_bridge_files import AttemptKey
        view = self.configure('on')
        self.assertTrue(control.join_peer(self.worker)['ok'])
        ref = self.binding.LeaderRef(self.a.actor, view['leader_epoch'])
        rid = control.open_round(self.a, ref)
        first = attempts.publish(self.a, ref, rid, attempts.Request('running', b'private request'))
        second = attempts.publish(self.a, ref, rid, attempts.Request('pending', b'pending request'))
        key = AttemptKey(first['task_id'], first['attempt_no'])
        pending = AttemptKey(second['task_id'], second['attempt_no'])
        begun = attempts.begin(self.worker, key, None)
        self.assertTrue(begun['ok'], begun)
        self.assertEqual(control.connection_status(self.worker)['connections'][0]['readiness'], 'WORKING')
        self.assertTrue(self.configure('off')['ok'])
        self.assertFalse(attempts.begin(self.worker, pending, None)['ok'])
        self.assertEqual(self.configure('on')['reason'], 'LEADER_BUSY')
        finished = attempts.finish(self.worker, key, begun['execution_ticket'], 'COMPLETED', b'private final result')
        self.assertTrue(finished['ok'], finished)
        self.assertEqual(self.configure('on', self.b)['reason'], 'LEADER_BUSY')
        status = json.dumps(control.connection_status(self.a))
        self.assertNotIn('private', status)
        self.assertNotIn(begun['execution_ticket'], status)
        self.assertTrue(attempts.ack(self.a, ref, key, finished['result_id'])['ok'])
        self.assertTrue(control.close_round(self.a, ref, rid, 'review complete')['ok'])
        self.assertTrue(self.configure('on')['ok'])
        self.assertIsNone(self.group()['worker_chat'])

    def test_wait_ready_stops_at_expiry_or_changed_generation(self):
        clock = [1000.0]
        real = control.Store
        with mock.patch.object(control, 'Store', lambda paths: real(paths, clock=lambda: clock[0])):
            view = self.configure()
            ref = self.binding.LeaderRef(self.a.actor, view['leader_epoch'])
            epochs = {name: view[name] for name in ('pair_epoch', 'desktop_epoch', 'auto_epoch')}
            clock[0] = 1600.0
            before = self.paths.db.read_bytes()
            self.assertEqual(control.wait_ready(self.a, ref, **epochs, wait_seconds=60)['state'], 'EXPIRED')
            self.assertEqual(before, self.paths.db.read_bytes())
            view = self.configure()
            epochs = {name: view[name] for name in epochs}
            def transfer(_):
                self.assertTrue(self.configure(ctx=self.b)['ok'])
            with mock.patch.object(control.time, 'sleep', transfer):
                stale = control.wait_ready(self.a, ref, **epochs, wait_seconds=60)
            self.assertEqual(stale['reason'], 'STALE_ACTIVATION')
    def test_status_does_not_create_policy_or_db(self):
        before = self.policy.read_bytes()
        view = control.leader_status(self.a)
        desktop = control.desktop_status(self.a)
        self.assertFalse(view['selected'])
        self.assertFalse(desktop['desktop_enabled'])
        self.assertFalse(desktop['auto_enabled'])
        self.assertEqual(desktop['effective_policy'], 'ON')
        self.assertFalse(self.paths.db.exists())
        self.assertEqual(before, self.policy.read_bytes())

    def test_unselected_mutation_cannot_initialize_or_poison_bridge(self):
        invented = self.binding.LeaderRef(self.a.actor, 1)
        self.assertFalse(control.set_desktop(self.a, invented, True)['ok'])
        self.assertFalse(self.paths.db.exists())
        self.assertTrue(control.leader_on(self.a)['ok'])

    def test_only_idle_owner_can_release_and_same_owner_is_idempotent(self):
        ref = self.select()
        self.assertEqual(control.leader_on(self.a)['leader_epoch'], ref.epoch)
        self.assertFalse(control.leader_off(self.b)['ok'])
        rid = control.open_round(self.a, ref)
        index = json.loads((self.custom / 'coding-team/bridge-worktrees.json').read_text(encoding='utf-8'))
        self.assertEqual(index['worktrees'], [self.root.resolve().as_posix()])
        self.assertEqual(control.leader_off(self.a)['state'], 'LEADER_BUSY')
        self.assertTrue(control.close_round(self.a, ref, rid, 'review complete')['ok'])
        self.assertTrue(control.leader_off(self.a)['ok'])
        self.assertFalse(control.leader_status(self.a)['selected'])

    def test_old_leader_cannot_change_desktop_or_auto(self):
        old = self.select()
        self.assertTrue(control.set_desktop(self.a, old, True)['ok'])
        new = self.select(self.b)
        self.assertGreater(new.epoch, old.epoch)
        self.assertFalse(control.set_desktop(self.a, old, True)['ok'])
        self.assertFalse(control.set_auto(self.a, old, True)['ok'])
        self.assertFalse(control.desktop_status(self.b)['desktop_enabled'])
        self.assertFalse(control.desktop_status(self.b)['auto_enabled'])
        with self.assertRaises(ValueError):
            control.open_round(self.a, old)

    def test_native_registration_and_evidence_keep_round_busy(self):
        ref = self.select()
        rid = control.open_round(self.a, ref)
        self.assertTrue(control.register_native(self.a, ref, rid, 'native-1')['ok'])
        self.assertEqual(control.leader_on(self.b)['state'], 'LEADER_BUSY')
        self.assertFalse(control.close_round(self.a, ref, rid, 'review')['ok'])
        self.assertFalse(control.finish_native(self.a, ref, rid, 'native-1', '')['ok'])
        self.assertTrue(control.bind_native(self.a, ref, rid, 'native-1', 'host-handle')['ok'])
        self.assertFalse(control.bind_native(self.a, ref, rid, 'native-1', 'other-handle')['ok'])
        journal = self.custom / 'coding-team/install-journal.json'
        journal.write_text('{}', encoding='utf-8')
        with self.assertRaises(ValueError):
            control.open_round(self.a, ref)
        self.assertTrue(control.finish_native(self.a, ref, rid, 'native-1', 'native host confirmed stopped')['ok'])
        self.assertTrue(control.close_round(self.a, ref, rid, 'review complete')['ok'])
        journal.unlink()
        self.assertTrue(control.leader_on(self.b)['ok'])

    def test_ticket_expires_at_ten_minutes_and_is_single_use(self):
        ref = self.select()
        clock = [1000.0]
        real_store = control.Store
        with mock.patch.object(control, 'Store', lambda paths: real_store(paths, clock=lambda: clock[0])):
            offer = control.invite(self.a, ref)
            self.assertTrue(offer['ok'], offer)
            clock[0] = 1600.0
            self.assertFalse(control.connect(self.worker, offer['ticket'])['ok'])
            offer = control.invite(self.a, ref)
            clock[0] += 599.9
            self.assertTrue(control.connect(self.worker, offer['ticket'])['ok'])
            self.assertFalse(control.connect(self.worker, offer['ticket'])['ok'])

    def test_pair_requires_runtime_worker_and_fences_old_invites(self):
        ref = self.select()
        offer = control.invite(self.a, ref)
        self.assertFalse(control.connect(self.context('antigravity', None, 'worker'), offer['ticket'])['ok'])
        self.assertFalse(control.connect(self.a, offer['ticket'])['ok'])
        newer = control.invite(self.a, ref)
        self.assertFalse(control.connect(self.worker, offer['ticket'])['ok'])
        self.assertTrue(control.connect(self.worker, newer['ticket'])['ok'])
        rid = control.open_round(self.a, ref)
        self.assertFalse(control.invite(self.a, ref)['ok'])
        self.assertTrue(control.close_round(self.a, ref, rid, 'review complete')['ok'])
        self.select(self.b)
        self.assertFalse(control.connect(self.worker, newer['ticket'])['ok'])

    def test_transfer_and_round_open_serialize(self):
        # Both outcomes have an exact authority boundary: transfer cannot occur
        # after open; an old epoch cannot open after transfer.
        ref = self.select()
        rid = control.open_round(self.a, ref)
        self.assertEqual(control.leader_on(self.b)['state'], 'LEADER_BUSY')
        control.close_round(self.a, ref, rid, 'closed')
        ref = self.select(self.b)
        gate = threading.Barrier(2)
        results = []
        def open_old():
            gate.wait()
            try:
                results.append(('open', control.open_round(self.b, ref)))
            except ValueError:
                results.append(('stale', None))
        def transfer():
            gate.wait()
            results.append(('transfer', control.leader_on(self.a)))
        threads = [threading.Thread(target=open_old), threading.Thread(target=transfer)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        changed = next(value for name, value in results if name == 'transfer')
        self.assertEqual(len(results), 2)
        self.assertEqual(any(name == 'stale' for name, _ in results), changed['ok'])

    def attempt_fixture(self, ref, rid, state='WAITING_FOR_USER', mode='manual', task='CT-001'):
        with control.Store(self.paths).write() as db:
            group = db.execute('SELECT * FROM group_state').fetchone()
            db.execute('INSERT INTO tasks(id,round_id,pair_epoch,worker_platform,worker_chat) VALUES(?,?,?,?,?)',
                       (task, rid, group['pair_epoch'], 'antigravity', 'worker-a'))
            db.execute('''INSERT INTO attempts(task_id,attempt_no,publish_id,state,mode,kind,request_sha256,
                request_size,scope,baseline,desktop_epoch,auto_epoch,pair_epoch,leader_epoch,policy_stamp,created_at)
                VALUES(?,1,?,?,?,'READ',?,0,'[]','{}',?,?,?,?,?,1000)''',
                (task, task, state, mode, '0' * 64, group['desktop_epoch'], group['auto_epoch'],
                 group['pair_epoch'], ref.epoch, '{}'))

    def test_pending_review_and_unknown_keep_leader_busy(self):
        ref = self.select()
        rid = control.open_round(self.a, ref)
        self.attempt_fixture(ref, rid)
        for state in ('PUBLISHING', 'WAITING_FOR_USER', 'QUEUED', 'CLAIMED', 'RUNNING', 'NEEDS_USER', 'OUTCOME_UNKNOWN'):
            with self.subTest(state=state):
                with control.Store(self.paths).write() as db:
                    db.execute('UPDATE attempts SET state=?', (state,))
                self.assertFalse(control.close_round(self.a, ref, rid, 'review')['ok'])
                self.assertEqual(control.leader_on(self.b)['state'], 'LEADER_BUSY')
        with control.Store(self.paths).write() as db:
            db.execute("UPDATE attempts SET state='COMPLETED',result_id='receipt-1'")
        self.assertFalse(control.close_round(self.a, ref, rid, 'review')['ok'])
        with control.Store(self.paths).write() as db:
            db.execute('UPDATE attempts SET ack_at=1200')
        self.assertTrue(control.close_round(self.a, ref, rid, 'review')['ok'])
        self.assertTrue(control.leader_on(self.b)['ok'])

    def test_auto_off_revokes_only_auto_pending_and_does_not_revive(self):
        ref = self.select()
        control.set_desktop(self.a, ref, True)
        control.set_auto(self.a, ref, True)
        rid = control.open_round(self.a, ref)
        self.attempt_fixture(ref, rid, 'CLAIMED', 'auto')
        self.attempt_fixture(ref, rid, 'WAITING_FOR_USER', 'manual', 'CT-002')
        self.attempt_fixture(ref, rid, 'RUNNING', 'auto', 'CT-003')
        self.assertTrue(control.set_auto(self.a, ref, False)['ok'])
        control.set_auto(self.a, ref, True)
        with control.Store(self.paths).read() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT task_id,state FROM attempts ORDER BY task_id')],
                [('CT-001', 'REVOKED'), ('CT-002', 'WAITING_FOR_USER'), ('CT-003', 'RUNNING')])
        control.set_desktop(self.a, ref, False)
        with control.Store(self.paths).read() as db:
            self.assertEqual([row['state'] for row in db.execute('SELECT state FROM attempts ORDER BY task_id')],
                ['REVOKED', 'REVOKED', 'RUNNING'])

    def test_worker_status_requires_exact_paired_runtime_chat(self):
        ref = self.select()
        control.set_desktop(self.a, ref, True)
        ticket = control.invite(self.a, ref)['ticket']
        self.assertTrue(control.connect(self.worker, ticket)['ok'])
        self.assertTrue(hasattr(control, 'worker_status'), 'worker status must check the paired runtime actor')
        self.assertTrue(control.worker_status(self.worker)['ready'])
        stranger = self.context('antigravity', 'worker-other', 'worker')
        self.assertEqual(control.worker_status(stranger)['reason'], 'WRONG_WORKER')
        self.assertFalse(control.worker_status(stranger)['ready'])

    def test_other_scope_requires_explicit_idle_reselection(self):
        ref = self.select()
        project = self.root / '.agents/skills/coding-team'
        shutil.copytree(self.leader_manifest.parent, project)
        manifest = project / 'install-manifest.json'
        record = json.loads(manifest.read_text(encoding='utf-8'))
        record['scope'] = 'project'
        manifest.write_text(json.dumps(record), encoding='utf-8')
        other = self.binding.ctx_from_runtime('codex', self.root,
            {'CODEX_HOME': str(self.custom), 'CODEX_THREAD_ID': 'chat-a'},
            component='leader', source_root=project, home=self.home)
        self.assertFalse(control.set_desktop(other, ref, True)['ok'])
        selected = control.leader_on(other)
        self.assertTrue(selected['ok'])
        self.assertGreater(selected['leader_epoch'], ref.epoch)
        self.assertFalse(control.set_desktop(self.a, ref, True)['ok'])

    def test_idle_role_change_and_release_survive_removed_worker_component(self):
        ref = self.select()
        self.assertTrue(control.set_desktop(self.a, ref, True)['ok'])
        self.assertTrue(control.connect(self.worker, control.invite(self.a, ref)['ticket'])['ok'])
        shutil.rmtree(self.worker_manifest.parent)
        self.assertTrue(control.leader_on(self.b)['ok'])
        self.assertTrue(control.leader_off(self.b)['ok'])

    def test_status_exposes_pending_review_without_grants_or_request_content(self):
        ref = self.select()
        rid = control.open_round(self.a, ref)
        self.attempt_fixture(ref, rid, 'COMPLETED')
        with control.Store(self.paths).write() as db:
            db.execute("UPDATE attempts SET result_id='receipt-1',claim_hash='private',execution_hash='private'")
        status = control.leader_status(self.a)
        self.assertIn('attempts', status)
        self.assertEqual(status['attempts'][0]['state'], 'COMPLETED')
        self.assertTrue(status['attempts'][0]['review_pending'])
        self.assertEqual(status['attempts'][0]['task_id'], 'CT-001')
        self.assertNotIn('private', json.dumps(status))


if __name__ == '__main__':
    unittest.main()
