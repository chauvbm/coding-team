from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
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

    def test_codex_manifests_and_registry_share_one_canonical_state(self):
        user = self.paths('codex')
        project = self.paths('codex', 'project')
        user_manifest = (user['skill_dir'] / ct_install.MANIFEST_NAME).resolve()
        project_manifest = (project['skill_dir'] / ct_install.MANIFEST_NAME).resolve()
        state = user['state_file'].resolve()
        registry = state.parent / 'external-installations.json'

        self.install('codex', 'project')
        self.assertFalse(state.exists())
        self.install('codex')
        for manifest in (user_manifest, project_manifest):
            with self.subTest(manifest=manifest):
                data = json.loads(manifest.read_bytes())
                self.assertEqual(data['schema_version'], 1)
                self.assertEqual(data['external_abi'], 1)
                self.assertEqual(data['state_path'], state.as_posix())
                self.assertEqual(data['install_lock'], user['lock_file'].resolve().as_posix())
        self.assertEqual(json.loads(registry.read_bytes()), {'schema_version': 1, 'installations': {
            user_manifest.as_posix(): 'user', project_manifest.as_posix(): 'project',
        }})

        legacy = json.loads(project_manifest.read_bytes())
        for key in ('external_abi', 'state_path', 'install_lock'):
            del legacy[key]
        project_manifest.write_text(json.dumps(legacy), encoding='utf-8')
        policy = state.read_bytes()
        self.install('codex', 'project')
        self.assertEqual(state.read_bytes(), policy)
        self.assertEqual(json.loads(project_manifest.read_bytes())['external_abi'], 1)
        self.uninstall('codex', 'project')
        self.assertEqual(ct_install.load_external_installations(state), {user_manifest: 'user'})
        self.uninstall('codex')
        self.assertEqual(ct_install.load_external_installations(state), {})

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

    @unittest.skipUnless(os.name == 'nt', 'Windows Codex external maintenance')
    def test_external_hook_for_mutation_runs_under_install_lock(self):
        paths = self.paths('codex')
        calls = []

        @contextmanager
        def maintenance(state, operation):
            self.assertEqual(state, paths['state_file'].resolve())
            with self.assertRaises(TimeoutError), ct_install.locked(paths['lock_file'], timeout=0):
                self.fail('maintenance must run with the existing install lock held')
            calls.append(operation)
            yield

        with mock.patch('ct_external.maintenance_locked', maintenance, create=True):
            plan = self.plan()
            ct_install.apply_plan(plan, dry_run=True)
            self.assertEqual(calls, [])
            self.install('codex')
            self.install('codex')
            ct_install.uninstall(self.cfg('codex'), scope='user', env=self.env, home=self.home, dry_run=True)
            self.uninstall('codex')
        self.assertEqual(calls, ['install', 'uninstall'])

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

    @unittest.skipUnless(os.name == 'nt', 'Windows Codex external maintenance')
    def test_legacy_recovery_keeps_journal_and_backups_until_writer_stops(self):
        paths = self.paths('codex')
        manifest = paths['skill_dir'] / ct_install.MANIFEST_NAME
        registry = paths['state_file'].parent / 'external-installations.json'
        self.install('codex', default='off')
        legacy = json.loads(manifest.read_bytes())
        for key in ('external_abi', 'state_path', 'install_lock'):
            del legacy[key]
        manifest.write_text(json.dumps(legacy), encoding='utf-8')
        registry.unlink()
        original_state = paths['state_file'].read_bytes()
        original_manifest = manifest.read_bytes()
        original_write = ct_install.atomic_write

        class Interrupted(BaseException):
            pass

        def interrupt(path, data):
            original_write(path, data)
            if Path(path) == registry:
                raise Interrupted

        with mock.patch.object(ct_install, 'atomic_write', side_effect=interrupt):
            with self.assertRaises(Interrupted):
                self.install('codex', default='on')
        journal = json.loads(paths['journal_file'].read_bytes())
        backup_dir = Path(journal['backup_dir'])
        self.assertTrue(backup_dir.is_dir())
        before = snapshot(self.tmp)
        backups = snapshot(backup_dir)

        @contextmanager
        def maintenance(state, operation):
            self.assertEqual((state, operation), (paths['state_file'].resolve(), 'recover'))
            if writer_active:
                raise RuntimeError('stop proof unavailable')
            yield

        with mock.patch('ct_external.maintenance_locked', maintenance, create=True):
            writer_active = True
            with self.assertRaisesRegex(RuntimeError, 'stop proof unavailable'):
                self.recover()
            self.assertEqual(snapshot(self.tmp), before)
            writer_active = False
            recovered = self.recover()

        self.assertTrue(recovered['journal_removed'])
        self.assertFalse(registry.exists())
        self.assertEqual(manifest.read_bytes(), original_manifest)
        self.assertEqual(paths['state_file'].read_bytes(), original_state)
        self.assertEqual(snapshot(backup_dir), backups)


@unittest.skipUnless(os.name == 'nt', 'Windows Codex packaged runner')
class InstalledPackageMaintenanceTests(unittest.TestCase):
    def setUp(self):
        from tests.fixtures.external.make_fake_bundle import make_fake_bundle

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        bundle = self.root / 'bundle'
        self.cli, self.env = make_fake_bundle(bundle)
        self.source_cli = bundle / 'extracted/coding-team/scripts/coding_team.py'
        self.state_dir = Path(self.env['CODEX_HOME']) / 'coding-team'
        self.workspace = self.root / 'workspace'
        self.other_project = self.root / 'other-project'
        for project in (self.workspace, self.other_project):
            project.mkdir()
            self.git(project, 'init', '-q')
            (project / 'input.txt').write_text('selected fixture\n', encoding='utf-8')
            self.git(project, 'add', 'input.txt')
            self.git(project, '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid',
                     '-c', 'commit.gpgsign=false', 'commit', '-q', '-m', 'base')

    def git(self, cwd, *args):
        run = subprocess.run(['git', *args], cwd=cwd, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        return run.stdout.strip()

    def run_cli(self, *args, source=False, cli=None, cwd=None, stdin=None):
        script = self.source_cli if source else cli or self.cli
        result = subprocess.run([sys.executable, str(script), *map(str, args), '--platform', 'codex'],
                                cwd=cwd or self.workspace, env=self.env, input=stdin,
                                capture_output=True, text=True, encoding='utf-8', timeout=55)
        try:
            return result.returncode, json.loads(result.stdout)
        except ValueError:
            self.fail(f'expected one JSON object, got {result.stdout!r} {result.stderr!r}')

    def control(self, verb, **kwargs):
        return self.run_cli('command', f'/coding-team antigravity {verb}', **kwargs)

    def attempt(self, operation, task, attempt=1, **kwargs):
        args = ('--request-stdin',) if operation == 'reserve' else ()
        code, report = self.run_cli('external', operation, '--workspace', self.workspace,
                                    '--task-id', task, '--attempt', attempt, *args, **kwargs)
        self.assertEqual(code, 0, report)
        return report

    def reserve_selected(self, task, request):
        options = self.attempt('discover', task)
        self.assertEqual(options['state'], 'MODEL_OPTIONS', options)
        payload = dict(request, catalog_id=options['catalog_id'], model_id='gemini-fixture-low')
        return self.attempt('reserve', task, stdin=json.dumps(payload))

    def make_ready(self):
        code, report = self.control('check')
        self.assertEqual(code, 0, report)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            status = self.attempt('status', report['task_id'], report['attempt_no'])
            if status['state'] in ('COMPLETED', 'FAILED', 'CANCELED', 'TIMED_OUT', 'RECONCILE_REQUIRED'):
                self.assertEqual(status['state'], 'COMPLETED', status)
                break
            time.sleep(0.1)
        else:
            self.fail('qualification did not finish')
        code, status = self.control('status')
        self.assertEqual((code, status['state']), (0, 'READY'), status)

    def start_blocked_worker(self, task, operation):
        barrier = self.root / (task + '-release')
        marker = self.root / (task + '-started')
        prompt = json.dumps({'barrier': str(barrier), 'spawn_marker': str(marker), 'barrier_timeout': 30})
        request = {'kind': 'READ', 'prompt': prompt,
                   'base': self.git(self.workspace, 'rev-parse', 'HEAD'),
                   'file_choices': {'input.txt': 'GIT'}, 'edit_paths': []}
        self.reserve_selected(task, request)
        ack = self.attempt('start', task)
        self.assertTrue(ack['acknowledged'], ack)
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            if not marker.exists():
                self.fail(f"detached fixture did not start: {self.attempt('status', task)!r}")
            operation()
            self.assertEqual(self.attempt('status', task)['state'], 'CANCELED')
        finally:
            barrier.touch()
        self.assertEqual(self.control('status')[1]['reason'], 'CHECK_REQUIRED')

    def test_packaged_upgrade_and_recover_stop_active_worker_and_invalidate_readiness(self):
        code, status = self.control('status')
        self.assertEqual((code, status['state']), (0, 'OFF'), status)
        before = snapshot(self.state_dir)
        code, doctor = self.run_cli('doctor')
        self.assertIn(code, (0, 1), doctor)
        self.assertEqual(doctor['antigravity_cli']['readiness'], 'OFF', doctor)
        code, preview = self.run_cli('install', '--dry-run', source=True)
        self.assertEqual(code, 0, preview)
        self.assertTrue(preview['dry_run'], preview)
        self.assertEqual(snapshot(self.state_dir), before)
        self.assertEqual(self.control('on')[0], 0)
        self.make_ready()

        # An otherwise identical reinstall must preserve qualification.
        code, result = self.run_cli('install', source=True)
        self.assertEqual(code, 0, result)
        self.assertEqual(self.control('status')[1]['state'], 'READY')

        def upgrade():
            source = self.source_cli.with_name('ct_external.py')
            source.write_bytes(source.read_bytes() + b'\n# packaged upgrade fixture\n')
            code, result = self.run_cli('install', source=True)
            self.assertEqual(code, 0, result)
            self.assertEqual(self.cli.with_name(source.name).read_bytes(), source.read_bytes())

        self.start_blocked_worker('upgrade-active', upgrade)
        self.make_ready()

        def recover():
            # Reproduce the journal left by a legacy installer after writing a reference.
            target = self.cli.parents[1] / 'references/control-policy.md'
            original = target.read_bytes()
            changed = original + b'\ninterrupted transaction\n'
            backup = self.state_dir / 'backups/recover-fixture/reference.md'
            backup.parent.mkdir(parents=True)
            backup.write_bytes(original)
            target.write_bytes(changed)
            journal = self.state_dir / 'install-journal.json'
            journal.write_text(json.dumps({'schema_version': 1, 'operation': 'install', 'platform': 'codex',
                                           'scope': 'user', 'created_dirs': [], 'backup_dir': backup.parent.as_posix(),
                                           'targets': [{'path': target.as_posix(), 'existed': True,
                                                        'backup': backup.as_posix(),
                                                        'backup_sha256': hashlib.sha256(original).hexdigest(),
                                                        'new_sha256': hashlib.sha256(changed).hexdigest()}]}),
                               encoding='utf-8')
            code, result = self.run_cli('install', '--recover', source=True)
            self.assertEqual(code, 0, result)
            self.assertTrue(result['journal_removed'], result)
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(backup.read_bytes(), original)

        self.start_blocked_worker('recover-active', recover)

    def test_managed_recover_can_close_lost_attempt_after_worktree_removal(self):
        import ct_storage
        import ct_winjob
        from ct_external_store import AttemptKey, Owner, Store

        self.assertEqual(self.control('on')[0], 0)
        self.make_ready()
        key = AttemptKey('lost-worktree', 1)
        request = {'kind': 'READ', 'prompt': 'read the fixture',
                   'base': self.git(self.workspace, 'rev-parse', 'HEAD'),
                   'file_choices': {'input.txt': 'GIT'}, 'edit_paths': []}
        self.reserve_selected(key.task_id, request)
        state_path = self.state_dir / 'state.json'
        store = Store(self.state_dir / 'external' / 'v1')
        owner = Owner('codex', self.env['CODEX_THREAD_ID'], str(self.workspace.resolve()), 'antigravity')
        previous = replace(ct_winjob.current_owner_process(), boot_id=str(uuid.uuid4()))
        job_name = ct_winjob.new_job_name()
        former_identity = ct_winjob.ProcessIdentity(job_name, previous.pid, previous.creation_time,
                                                     previous.session_id, previous.boot_id)
        # Seed the persisted evidence of a lost supervisor on an earlier boot.
        # No provider or live job is associated with this synthetic identity.
        with ct_storage.locked(self.state_dir / 'install.lock'), ct_storage.locked(state_path):
            stamp = ct_policy.policy_stamp(state_path, owner.chat_id, require_epochs=True)
            store.claim_start(key, owner, stamp)
            claim = store.run_snapshot(key, owner=owner).runner_claim
            store.claim_runner(key, claim, stamp, previous)
            store.admit_launch(key, claim, stamp, {'job_name': job_name})
            store.record_process(key, claim, former_identity)
            store.resume_process(key, claim, stamp, lambda: None)
            self.assertEqual(store.begin_maintenance().state, 'STOPPING')

        self.workspace.rename(self.root / 'removed-worktree')
        code, rejected = self.run_cli('external', 'reconcile', '--target', 'process',
                                      '--workspace', self.workspace, '--task-id', key.task_id,
                                      '--attempt', 1, cwd=self.other_project)
        self.assertEqual((code, rejected['reason']), (2, 'INVALID_WORKTREE'), rejected)
        code, recovered = self.run_cli('install', '--recover', source=True, cwd=self.other_project)
        self.assertEqual(code, 0, recovered)
        self.assertFalse(store.maintenance_pending())
        self.assertIsNone(store.active_attempt())
        outcome = store.run_snapshot(key, runner_claim=claim)
        self.assertEqual(outcome.view.state, 'CANCELED')
        self.assertEqual(outcome.stop_proof['basis'], 'BOOT_CHANGED')

    def test_registered_legacy_project_blocks_admission_from_other_checkout(self):
        args = ('install', '--scope', 'project', '--project-dir', self.other_project)
        code, installed = self.run_cli(*args, source=True)
        self.assertEqual(code, 0, installed)
        manifest = Path(installed['manifest'])
        document = json.loads(manifest.read_bytes())
        for key in ('external_abi', 'state_path', 'install_lock'):
            del document[key]
        manifest.write_text(json.dumps(document), encoding='utf-8')
        code, blocked = self.control('on')
        self.assertNotEqual(code, 0, blocked)
        self.assertEqual(blocked['reason'], 'INSTALLATION_INCOMPATIBLE')

        code, installed = self.run_cli(*args, source=True)
        self.assertEqual(code, 0, installed)
        self.assertEqual(self.control('on')[0], 0)
        self.make_ready()

        project_cli = manifest.parent / 'scripts/coding_team.py'
        code, result = self.control('on', cli=project_cli, cwd=self.other_project)
        self.assertEqual(code, 0, result)

        def try_other_checkout():
            code, busy = self.control('check', cli=project_cli, cwd=self.other_project)
            self.assertNotEqual(code, 0, busy)
            self.assertEqual(busy['reason'], 'BUSY')
            code, upgraded = self.run_cli('install', '--default', 'off', source=True)
            self.assertEqual(code, 0, upgraded)
            code, upgraded = self.run_cli('install', '--default', 'on', source=True)
            self.assertEqual(code, 0, upgraded)

        self.start_blocked_worker('shared-slot', try_other_checkout)


if __name__ == '__main__':
    unittest.main()
