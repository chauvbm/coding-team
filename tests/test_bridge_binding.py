"""Installation and runtime trust tests, entirely inside isolated fixture homes."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

try:
    import ct_bridge_binding as binding
except ImportError:
    binding = None


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.home = self.base / 'home'
        self.workspace = self.base / 'project'
        self.workspace.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.workspace)], check=True,
                       capture_output=True)
        self.custom = self.home / 'custom-codex'
        self.env = {'CODEX_HOME': str(self.custom), 'CODEX_THREAD_ID': 'leader-chat'}

    def require_module(self):
        self.assertIsNotNone(binding, 'the managed runtime binding module must exist')

    def install(self, component='leader', scope='user'):
        platform = 'codex' if component == 'leader' else 'antigravity'
        if scope == 'project':
            skill = self.workspace / '.agents/skills' / (
                'coding-team' if component == 'leader' else 'coding-team-desktop-worker')
        elif component == 'leader':
            skill = self.home / '.agents/skills/coding-team'
        else:
            skill = self.home / '.gemini/config/skills/coding-team-desktop-worker'
        scripts = ['ct_bridge_binding', 'ct_bridge_files', 'ct_bridge_store',
                   'ct_platform', 'ct_storage', 'ct_bridge_install']
        scripts += ['coding_team', 'ct_bridge_cli', 'ct_bridge_control',
                    'ct_bridge_attempts', 'ct_policy'] if component == 'leader' else [
                        'ct_bridge_worker', 'ct_bridge_control', 'ct_bridge_attempts', 'ct_policy']
        files = {'SKILL.md': b'fixture skill\n'}
        files.update({f'scripts/{name}.py': f'# fixture {name}\n'.encode() for name in scripts})
        for name in (platform,):
            files[f'platforms/{name}.json'] = (ROOT / f'platforms/{name}.json').read_bytes()
        for rel, payload in files.items():
            target = skill / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        lock = (self.custom / 'coding-team/install.lock' if component == 'leader' else
                self.home / '.gemini/antigravity/coding-team-desktop-worker/install.lock')
        record = {'schema_version': 1, 'bridge_abi': 1, 'component': component,
                  'platform': platform, 'scope': scope, 'installed_at': 'fixture',
                  'install_lock': lock.as_posix(),
                  'files': {'skill_dir': {rel: hashlib.sha256(payload).hexdigest()
                                         for rel, payload in files.items()}}}
        if component == 'leader':
            record['state_path'] = (self.custom / 'coding-team/state.json').as_posix()
        manifest = skill / 'install-manifest.json'
        manifest.write_text(json.dumps(record), encoding='utf-8')
        return skill, manifest, record

    def context(self, skill, component='leader', env=None):
        return binding.ctx_from_runtime('codex' if component == 'leader' else 'antigravity',
                                        self.workspace, self.env if env is None else env,
                                        component=component, source_root=skill, home=self.home)

    def rewrite(self, manifest, record):
        manifest.write_text(json.dumps(record), encoding='utf-8')

    def test_runtime_identity_does_not_fall_back_to_other_chat_variables(self):
        self.require_module()
        skill, _, _ = self.install()
        ctx = self.context(skill, env={'CODEX_CHAT_ID': 'claimed', 'CHAT_ID': 'claimed'})
        self.assertIsNone(ctx.actor)
        with self.assertRaisesRegex(ValueError, 'UNKNOWN_CHAT'):
            ctx.require_actor()
        worker, _, _ = self.install('worker')
        ctx = self.context(worker, 'worker', {'ANTIGRAVITY_CHAT_ID': 'claimed'})
        self.assertIsNone(ctx.actor)

    def test_context_resolves_worktree_and_status_does_not_create_data(self):
        self.require_module()
        skill, _, _ = self.install()
        nested = self.workspace / 'src'
        nested.mkdir()
        before = sorted(str(p) for p in self.base.rglob('*'))
        ctx = binding.ctx_from_runtime('codex', nested, self.env, component='leader',
                                       source_root=skill, home=self.home)
        own = ctx.own_binding()
        self.assertEqual(ctx.paths.root, self.workspace)
        self.assertEqual(ctx.actor, binding.Actor('codex', 'leader-chat'))
        self.assertEqual(own.policy_path, self.custom / 'coding-team/state.json')
        self.assertEqual(sorted(str(p) for p in self.base.rglob('*')), before)

    def test_leader_mutation_rejects_worker_and_unsupported_platform(self):
        self.require_module()
        skill, _, _ = self.install('worker')
        ctx = self.context(skill, 'worker', {'ANTIGRAVITY_CONVERSATION_ID': 'worker-chat'})
        with self.assertRaisesRegex(ValueError, 'UNSUPPORTED_LEADER'):
            ctx.require_leader()
        with self.assertRaises(ValueError):
            binding.ctx_from_runtime('claude-code', self.workspace, {}, component='leader',
                                     source_root=skill, home=self.home)

    def test_selected_entry_point_must_be_in_its_exact_managed_component(self):
        self.require_module()
        skill, _, _ = self.install()
        stray = self.base / 'copied-skill'
        shutil.copytree(skill, stray)
        with self.assertRaisesRegex(ValueError, 'INSTALLATION_INCOMPATIBLE'):
            self.context(stray).own_binding()
        with self.assertRaises(ValueError):
            self.context(skill, 'worker', {}).own_binding()

    def test_project_binding_is_specific_to_the_current_worktree(self):
        self.require_module()
        skill, manifest, _ = self.install(scope='project')
        self.assertEqual(self.context(skill).own_binding().scope, 'project')
        other = self.base / 'other'
        other.mkdir()
        subprocess.run(['git', 'init', '-q', str(other)], check=True, capture_output=True)
        ctx = binding.ctx_from_runtime('codex', other, self.env, component='leader',
                                       source_root=skill, home=self.home)
        with self.assertRaises(ValueError):
            ctx.leader_binding(manifest)

    def test_worker_reads_custom_codex_home_from_activated_leader_manifest(self):
        self.require_module()
        _, leader_manifest, _ = self.install()
        worker, _, _ = self.install('worker')
        ctx = self.context(worker, 'worker', {'ANTIGRAVITY_CONVERSATION_ID': 'worker-chat'})
        own = ctx.own_binding()
        leader = ctx.leader_binding(leader_manifest)
        self.assertIsNone(own.policy_path)
        self.assertEqual(leader.policy_path, self.custom / 'coding-team/state.json')
        self.assertEqual(leader.install_lock, self.custom / 'coding-team/install.lock')
        binding.verify_compatibility(leader, own)

    def test_manifest_rejects_changed_runtime_missing_hash_and_unsafe_relpath(self):
        self.require_module()
        skill, manifest, record = self.install()
        script = skill / 'scripts/coding_team.py'
        original = script.read_bytes()
        script.write_bytes(b'# modified\n')
        with self.assertRaises(ValueError):
            self.context(skill).own_binding()
        script.write_bytes(original)
        del record['files']['skill_dir']['scripts/coding_team.py']
        self.rewrite(manifest, record)
        with self.assertRaises(ValueError):
            self.context(skill).own_binding()
        record['files']['skill_dir']['../outside.py'] = '0' * 64
        self.rewrite(manifest, record)
        with self.assertRaises(ValueError):
            self.context(skill).own_binding()

    def test_manifest_rejects_unrelated_policy_and_lock(self):
        self.require_module()
        skill, manifest, record = self.install()
        record['install_lock'] = (self.base / 'unrelated/install.lock').as_posix()
        self.rewrite(manifest, record)
        with self.assertRaises(ValueError):
            self.context(skill).own_binding()

    def test_leader_own_binding_rejects_different_runtime_codex_home(self):
        self.require_module()
        skill, _, _ = self.install()
        with self.assertRaises(ValueError):
            self.context(skill, env={'CODEX_THREAD_ID': 'leader-chat'}).own_binding()

    def test_journal_blocks_admission_but_allows_existing_attempt_verification(self):
        self.require_module()
        skill, _, _ = self.install()
        own = self.context(skill).own_binding()
        journal = own.install_lock.parent / 'install-journal.json'
        journal.parent.mkdir(parents=True)
        journal.write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'INSTALLATION_INCOMPATIBLE'):
            binding.verify_compatibility(own)
        binding.verify_compatibility(own, require_clean_journal=False)
        with binding.locked_bindings(own, require_clean_journal=False):
            self.assertTrue(Path(str(own.install_lock) + '.lock').exists())

    def test_binding_rechecks_runtime_inside_install_lock(self):
        self.require_module()
        skill, _, _ = self.install()
        own = self.context(skill).own_binding()
        (skill / 'scripts/coding_team.py').write_bytes(b'# changed after discovery\n')
        with self.assertRaises(ValueError):
            with binding.locked_bindings(own):
                self.fail('modified component acquired authority')

    def test_components_from_different_worktrees_cannot_share_authority(self):
        self.require_module()
        leader, _, _ = self.install(scope='project')
        other = self.base / 'second-project'
        other.mkdir()
        subprocess.run(['git', 'init', '-q', str(other)], check=True, capture_output=True)
        first = self.context(leader).own_binding()
        self.workspace = other
        worker, _, _ = self.install('worker', 'project')
        second = self.context(worker, 'worker', {'ANTIGRAVITY_CONVERSATION_ID': 'worker'}).own_binding()
        with self.assertRaisesRegex(ValueError, 'INSTALLATION_INCOMPATIBLE'):
            binding.verify_compatibility(first, second)

    def test_binding_pins_manifest_identity_after_discovery(self):
        self.require_module()
        skill, manifest, record = self.install()
        own = self.context(skill).own_binding()
        record['installed_at'] = 'another install'
        self.rewrite(manifest, record)
        with self.assertRaises(ValueError):
            own.verify(require_clean_journal=False)

    def test_manifest_rejects_duplicate_keys_and_boolean_abi(self):
        self.require_module()
        skill, manifest, record = self.install()
        record['bridge_abi'] = True
        self.rewrite(manifest, record)
        with self.assertRaises(ValueError):
            self.context(skill).own_binding()
        record['bridge_abi'] = 1
        manifest.write_text(json.dumps(record)[:-1] + ', "bridge_abi": 1}', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.context(skill).own_binding()

    def test_hardlinked_manifest_and_runtime_file_are_rejected(self):
        self.require_module()
        skill, manifest, _ = self.install()
        outside = self.base / 'outside-manifest.json'
        os.link(manifest, outside)
        with self.assertRaises(ValueError):
            self.context(skill).own_binding()
        outside.unlink()
        os.link(skill / 'scripts/coding_team.py', self.base / 'outside-entry.py')
        with self.assertRaises(ValueError):
            self.context(skill).own_binding()

    @unittest.skipUnless(os.name == 'nt', 'Windows junction test')
    def test_managed_script_directory_junction_is_rejected(self):
        self.require_module()
        skill, _, _ = self.install()
        outside = self.base / 'outside-scripts'
        shutil.move(str(skill / 'scripts'), outside)
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(skill / 'scripts'), str(outside)],
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        with self.assertRaises(ValueError):
            self.context(skill).own_binding()

    def test_manifest_and_managed_file_links_are_rejected(self):
        self.require_module()
        skill, manifest, _ = self.install()
        outside = self.base / 'saved-manifest.json'
        outside.write_bytes(manifest.read_bytes())
        manifest.unlink()
        try:
            manifest.symlink_to(outside)
        except OSError as exc:
            self.skipTest('symlink creation unavailable: ' + str(exc))
        with self.assertRaises(ValueError):
            self.context(skill).own_binding()


if __name__ == '__main__':
    unittest.main()
