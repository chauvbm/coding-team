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
                 'ct_policy', 'ct_bridge_control', 'ct_bridge_attempts', 'ct_bridge_install')
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
