from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import ct_install  # noqa: E402
import ct_platform  # noqa: E402
import ct_policy  # noqa: E402
import ct_rules  # noqa: E402
from ct_roles import load_roles, render_all  # noqa: E402


def snapshot(root, exclude=()):
    """Every directory and file under root (files with sha256), skipping excluded trees."""
    root = Path(os.path.abspath(root))
    skip = {Path(os.path.abspath(item)) for item in exclude}
    result = {}
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = [n for n in dirnames if here / n not in skip]
        for name in dirnames:
            result[(here / name).relative_to(root).as_posix()] = 'dir'
        for name in filenames:
            path = here / name
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


class IntegrationInstallTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / 'home'
        self.project = self.tmp / 'project'
        self.home.mkdir()
        self.project.mkdir()
        self.env = {}

    def fresh_home(self, name):
        self.home = self.tmp / f'home-{name}'
        self.home.mkdir()

    def cfg(self, name):
        return ct_platform.load_platform(name)

    def paths(self, name, scope='user'):
        return ct_platform.target_paths(self.cfg(name), scope, self.env, self.home, self.project)

    def install(self, name, scope='user', **kwargs):
        plan = ct_install.build_plan(ROOT, self.cfg(name), scope=scope, env=self.env, home=self.home,
                                     project_dir=self.project, python=sys.executable, **kwargs)
        return ct_install.apply_plan(plan, dry_run=False, replace_unmanaged=False, overwrite_modified=False)

    def uninstall(self, name, scope='user'):
        return ct_install.uninstall(self.cfg(name), scope=scope, env=self.env, home=self.home,
                                    project_dir=self.project, dry_run=False, remove_modified=False)

    def plan(self, scope='user', **kwargs):
        return ct_install.build_plan(ROOT, self.cfg('codex'), scope=scope, env=self.env,
                                     home=self.home, project_dir=self.project, **kwargs)

    def recover(self):
        return ct_install.recover(self.cfg('codex'), scope='user', env=self.env, home=self.home)

    def test_full_cycle_each_platform(self):
        for name in ct_platform.PLATFORMS:
            with self.subTest(platform=name):
                self.fresh_home(name)
                cfg = self.cfg(name)
                paths = self.paths(name)
                state_dir = paths['state_file'].parent
                before = snapshot(self.tmp, exclude=[state_dir])
                # ancestors of the state directory necessarily remain once state exists
                keep = {p.relative_to(self.tmp).as_posix() for p in state_dir.parents
                        if self.tmp in p.parents}
                self.install(name, default='off', global_rules=True)

                expected = render_all(load_roles(ROOT / 'agents/roles'), cfg)
                self.assertTrue(expected)
                for rel, data in expected:
                    self.assertEqual((paths['agents_dir'] / rel).read_bytes(), data, rel)
                self.assertEqual(ct_policy.load_state(paths['state_file'])['global_policy'], 'OFF')
                rules = paths['global_rules_file'].read_text(encoding='utf-8')
                self.assertEqual(rules.count(ct_rules.START), 1)
                self.assertEqual(rules.count(ct_rules.END), 1)

                self.uninstall(name)
                after = snapshot(self.tmp, exclude=[state_dir])
                for key in keep:
                    after.pop(key, None)
                    before.pop(key, None)
                self.assertEqual(after, before)

    def test_rules_block_points_to_installed_cli(self):
        for name in ct_platform.PLATFORMS:
            with self.subTest(platform=name):
                self.fresh_home(name)
                paths = self.paths(name)
                self.install(name, default='off', global_rules=True)
                rules = paths['global_rules_file'].read_text(encoding='utf-8')
                cli = (paths['skill_dir'] / 'scripts' / 'coding_team.py').as_posix()
                state = paths['state_file'].as_posix()
                self.assertIn(cli, rules)
                self.assertIn(f'--state \'{state}\'' if f"'{state}'" in rules else f'--state {state}', rules)
                self.uninstall(name)

    def test_project_and_user_installs_coexist(self):
        for name in ct_platform.PLATFORMS:
            with self.subTest(platform=name):
                self.fresh_home(name)
                user = self.paths(name, 'user')
                proj = self.paths(name, 'project')
                self.install(name, 'user')
                self.install(name, 'project')
                um = user['skill_dir'] / ct_install.MANIFEST_NAME
                pm = proj['skill_dir'] / ct_install.MANIFEST_NAME
                self.assertTrue(um.is_file())
                self.assertTrue(pm.is_file())
                self.assertNotEqual(um, pm)
                user_snap = snapshot(user['skill_dir'])
                user_agents = snapshot(user['agents_dir'])
                self.uninstall(name, 'project')
                self.assertFalse(pm.exists())
                self.assertTrue(um.is_file())
                self.assertEqual(snapshot(user['skill_dir']), user_snap)
                self.assertEqual(snapshot(user['agents_dir']), user_agents)
                self.uninstall(name, 'user')

    def test_codex_manifests_share_native_state_without_legacy_registry(self):
        self.install('codex', 'project')
        self.assertFalse(self.paths('codex')['state_file'].exists())
        self.install('codex')
        paths = self.paths('codex')
        for scope in ('user', 'project'):
            manifest = self.paths('codex', scope)['skill_dir'] / ct_install.MANIFEST_NAME
            data = json.loads(manifest.read_bytes())
            self.assertEqual(data['bridge_abi'], 1)
            self.assertEqual(data['component'], 'leader')
            self.assertEqual(data['state_path'], paths['state_file'].resolve().as_posix())
            self.assertEqual(data['install_lock'], paths['lock_file'].resolve().as_posix())
        self.assertFalse((paths['state_file'].parent / 'external-installations.json').exists())

    def test_desktop_replaces_managed_cli_files_without_legacy_state(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt):
                self.fresh_home(str(corrupt))
                old = b'legacy managed runtime bytes\n'
                legacy_plan = self.plan(default='off')
                legacy_plan.files = [('skill_dir', 'SKILL.md', b'legacy instructions\n'),
                                     ('skill_dir', 'scripts/ct_external.py', old),
                                     ('skill_dir', 'scripts/ct_winjob.py', old)]
                ct_install.apply_plan(legacy_plan, dry_run=False)
                paths = self.paths('codex')
                skill = paths['skill_dir']
                manifest_path = skill / ct_install.MANIFEST_NAME
                manifest = json.loads(manifest_path.read_bytes())
                clean = skill / 'scripts/ct_external.py'
                edited = skill / 'scripts/ct_winjob.py'
                edited.write_bytes(b'user modified legacy helper\n')
                for key in ('bridge_abi', 'component'):
                    manifest.pop(key, None)
                manifest['external_abi'] = 1
                manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
                self.assertFalse((skill / 'scripts/ct_bridge_store.py').exists())
                unmanaged = skill / 'personal-helper.py'
                unmanaged.write_bytes(b'personal bytes')
                legacy_paths = [paths['state_file'].parent / 'external-installations.json',
                                paths['state_file'].parent / 'external/v1/state.sqlite3']
                if corrupt:
                    for path in legacy_paths:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(b'not readable legacy state')
                before = snapshot(self.tmp)
                ct_install.apply_plan(self.plan(), dry_run=True)
                self.assertEqual(snapshot(self.tmp), before)
                result = self.install('codex')
                self.assertIn(clean.as_posix(), result['removed_stale'])
                self.assertIn(edited.as_posix(), result['kept_modified_stale'])
                self.assertFalse(clean.exists())
                self.assertEqual(edited.read_bytes(), b'user modified legacy helper\n')
                self.assertEqual(unmanaged.read_bytes(), b'personal bytes')
                for path in legacy_paths:
                    self.assertEqual(path.read_bytes() if path.exists() else None,
                                     b'not readable legacy state' if corrupt else None)
                self.assertFalse((self.project / '.coding-team/bridge').exists())

    def test_managed_setup_repairs_epochs_without_changing_native_choices(self):
        paths = self.paths('codex')
        paths['state_file'].parent.mkdir(parents=True)
        native = {'schema_version': 1, 'global_policy': 'OFF', 'chat_policies': {'chat': 'ON'},
                  'updated_at': 'preserved'}
        paths['state_file'].write_text(json.dumps(native), encoding='utf-8')
        self.install('codex')
        state = ct_policy.load_state(paths['state_file'])
        self.assertEqual({key: state[key] for key in native}, native)
        self.assertEqual(ct_policy.policy_stamp(paths['state_file'], 'chat', require_epochs=True).effective, 'ON')
        state['policy_epoch_v1']['fingerprint'] = 'broken'
        paths['state_file'].write_text(json.dumps(state), encoding='utf-8')
        self.install('codex')
        self.assertEqual(ct_policy.policy_stamp(paths['state_file'], 'chat', require_epochs=True).effective, 'ON')

    def test_codex_installation_cannot_mutate_under_a_different_state_binding(self):
        original = self.paths('codex')
        self.install('codex', default='off')
        manifest = original['skill_dir'] / ct_install.MANIFEST_NAME
        before = manifest.read_bytes()
        self.env = {'CODEX_HOME': str(self.tmp / 'other-codex-config')}
        operations = {'install': lambda: self.install('codex', default='on'),
                      'uninstall': lambda: self.uninstall('codex'), 'recover': self.recover}
        for name, operation in operations.items():
            with self.subTest(operation=name):
                with self.assertRaisesRegex(ValueError, 'different Codex policy state'):
                    operation()
                self.assertEqual(manifest.read_bytes(), before)
        self.assertFalse(self.paths('codex')['state_file'].exists())
        self.assertEqual(ct_policy.load_state(original['state_file'])['global_policy'], 'OFF')

    def test_desktop_worktree_registration_is_canonical_idempotent_and_fail_closed(self):
        import ct_bridge_install
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True, capture_output=True)
        child = self.project / 'child'
        child.mkdir()
        lock = self.paths('codex')['lock_file']
        with ct_install.locked(lock):
            ct_bridge_install.register_worktree(lock, child)
            ct_bridge_install.register_worktree(lock, self.project)
        index = lock.parent / 'bridge-worktrees.json'
        content = json.loads(index.read_bytes())
        self.assertEqual(content, {'schema_version': 1, 'worktrees': [self.project.resolve().as_posix()]})
        before = index.read_bytes()
        with ct_install.locked(lock):
            with self.assertRaises(ValueError):
                ct_bridge_install.register_worktree(lock, self.home)
        self.assertEqual(index.read_bytes(), before)
        content['worktrees'] = ['../relative-worktree']
        index.write_text(json.dumps(content), encoding='utf-8')
        with ct_install.locked(lock), self.assertRaises(ValueError):
            ct_bridge_install.guard(lock)

    def test_concurrent_desktop_registration_preserves_all_checkout_roots(self):
        import ct_bridge_install
        checkouts = [self.project, self.tmp / 'other checkout']
        for checkout in checkouts:
            subprocess.run(['git', 'init', '-q', str(checkout)], check=True, capture_output=True)
        lock = self.paths('codex')['lock_file']

        def register(checkout):
            with ct_install.locked(lock):
                ct_bridge_install.register_worktree(lock, checkout)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(register, checkouts * 3))
        index = json.loads((lock.parent / 'bridge-worktrees.json').read_bytes())
        self.assertEqual(index['worktrees'], sorted(path.resolve().as_posix() for path in checkouts))

    def test_desktop_registration_retries_after_atomic_write_failure(self):
        import ct_bridge_install
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True, capture_output=True)
        lock = self.paths('codex')['lock_file']
        index = lock.parent / 'bridge-worktrees.json'
        with ct_install.locked(lock):
            with mock.patch.object(ct_bridge_install, 'atomic_write', side_effect=OSError('fixture failure')):
                with self.assertRaises(OSError):
                    ct_bridge_install.register_worktree(lock, self.project)
            self.assertFalse(index.exists())
            ct_bridge_install.register_worktree(lock, self.project)
            ct_bridge_install.register_worktree(lock, self.project)
        self.assertEqual(json.loads(index.read_bytes())['worktrees'], [self.project.resolve().as_posix()])

    def test_recover_does_not_replace_desktop_component_during_open_round(self):
        import ct_bridge_install
        from ct_bridge_files import AttemptKey, locate_workspace, publish_file
        from ct_bridge_store import Store
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True, capture_output=True)
        self.install('codex', default='off')
        paths = self.paths('codex')
        lock = paths['lock_file']
        with ct_install.locked(lock):
            ct_bridge_install.register_worktree(lock, self.project)
        store = Store(locate_workspace(self.project))
        managed = paths['skill_dir'] / 'SKILL.md'
        old = managed.read_bytes()
        manifest = paths['skill_dir'] / ct_install.MANIFEST_NAME
        old_manifest = manifest.read_bytes()
        plan = self.plan()
        plan.files = [(label, rel, payload + b'\nfixture upgrade\n' if rel == 'SKILL.md' else payload)
                      for label, rel, payload in plan.files]
        write = ct_install.atomic_write

        class Interrupted(BaseException):
            pass

        def interrupt(path, payload):
            write(path, payload)
            if Path(path) == managed:
                raise Interrupted

        with mock.patch.object(ct_install, 'atomic_write', side_effect=interrupt):
            with self.assertRaises(Interrupted):
                ct_install.apply_plan(plan, dry_run=False)
        self.assertNotEqual(managed.read_bytes(), old)
        with store.write() as db:
            db.execute("INSERT INTO rounds (id,leader_epoch,leader_platform,leader_chat,leader_manifest,opened_at) "
                       "VALUES ('R-test',1,'codex','chat-test','fixture',1)")
        publish_file(store.paths, AttemptKey('CT-history', 1), 'request', b'old request\n')
        publish_file(store.paths, AttemptKey('CT-history', 1), 'result', b'old result\n')
        before = snapshot(self.tmp)
        with self.assertRaisesRegex(ValueError, 'DESKTOP_ROUND_OPEN'):
            self.recover()
        self.assertEqual(snapshot(self.tmp), before)
        with store.write() as db:
            db.execute("UPDATE rounds SET closed_at=2 WHERE id='R-test'")
        history = snapshot(store.paths.data)
        report = self.recover()
        self.assertIn(managed.as_posix(), report['restored'])
        self.assertEqual(managed.read_bytes(), old)
        self.assertEqual(manifest.read_bytes(), old_manifest)
        self.assertEqual(snapshot(store.paths.data), history)
        # Reinstall may be a no-op while busy; actual mutation and removal must wait.
        with store.write() as db:
            db.execute("UPDATE rounds SET closed_at=NULL WHERE id='R-test'")
        self.install('codex')
        with self.assertRaisesRegex(ValueError, 'DESKTOP_ROUND_OPEN'):
            ct_install.apply_plan(plan, dry_run=False)
        with self.assertRaisesRegex(ValueError, 'DESKTOP_ROUND_OPEN'):
            self.uninstall('codex')
        with store.write() as db:
            db.execute("UPDATE rounds SET closed_at=3 WHERE id='R-test'")
        history = snapshot(store.paths.data)
        self.uninstall('codex')
        self.assertEqual(snapshot(store.paths.data), history)
        self.assertTrue((lock.parent / 'bridge-worktrees.json').is_file())

    def test_native_lock_is_held_through_staging_publication_and_recovery(self):
        paths = self.paths('codex')
        original_write = ct_install.atomic_write
        calls = []

        class Interrupted(BaseException):
            pass

        def check_lock(path, data):
            with self.assertRaises(TimeoutError), ct_install.locked(paths['state_file'], timeout=0):
                self.fail('native writer raced the installer')
            calls.append(Path(path))
            original_write(path, data)
            if Path(path) == paths['state_file'] and interrupt:
                raise Interrupted

        interrupt = True
        with mock.patch.object(ct_install, 'atomic_write', side_effect=check_lock):
            with self.assertRaises(Interrupted):
                self.install('codex', default='off')
            interrupt = False
            self.recover()
        self.assertTrue(calls)
        self.assertFalse(paths['state_file'].exists())

    def test_upgrade_journal_restores_managed_files(self):
        self.install('codex', default='off', global_rules=True)
        paths = self.paths('codex')
        skill = paths['skill_dir']
        legacy = skill / 'scripts/ct_external.py'
        old = b'legacy managed bytes\n'
        legacy.write_bytes(old)
        manifest = skill / ct_install.MANIFEST_NAME
        data = json.loads(manifest.read_bytes())
        data['files']['skill_dir']['scripts/ct_external.py'] = hashlib.sha256(old).hexdigest()
        manifest.write_text(json.dumps(data), encoding='utf-8')
        old_manifest = manifest.read_bytes()
        old_policy = paths['state_file'].read_bytes()
        rules = paths['global_rules_file']
        rules.write_bytes(b'user prefix\n' + rules.read_bytes() + b'user suffix\n')
        old_rules = rules.read_bytes()
        write = ct_install.atomic_write

        class Interrupted(BaseException):
            pass

        def interrupt(path, payload):
            if Path(path) == manifest:
                raise Interrupted
            write(path, payload)

        with mock.patch.object(ct_install, 'atomic_write', side_effect=interrupt):
            with self.assertRaises(Interrupted):
                self.install('codex')
        self.assertFalse(legacy.exists())
        report = self.recover()
        self.assertIn(legacy.as_posix(), report['restored'])
        self.assertEqual(legacy.read_bytes(), old)
        self.assertEqual(manifest.read_bytes(), old_manifest)
        self.assertEqual(paths['state_file'].read_bytes(), old_policy)
        self.assertEqual(rules.read_bytes(), old_rules)

    def test_recover_restores_default_if_interrupted_after_policy_publication(self):
        paths = self.paths('codex')
        self.install('codex', default='off')
        state_path = paths['state_file']
        original = state_path.read_bytes()

        class Interrupted(BaseException):
            pass

        write_target = ct_install.atomic_write
        save_state = ct_policy.save_state

        def interrupt_write(path, data):
            write_target(path, data)
            if Path(path) == state_path:
                raise Interrupted

        def interrupt_save(path, state):
            save_state(path, state)
            if Path(path) == state_path:
                raise Interrupted

        with mock.patch.object(ct_install, 'atomic_write', side_effect=interrupt_write), \
                mock.patch.object(ct_policy, 'save_state', side_effect=interrupt_save):
            with self.assertRaises(Interrupted):
                self.install('codex', default='on')
        self.assertEqual(ct_policy.load_state(state_path)['global_policy'], 'ON')
        self.assertIn(state_path.as_posix(), self.recover()['restored'])
        self.assertEqual(state_path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
