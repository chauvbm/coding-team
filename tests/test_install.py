import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'
sys.path.insert(0, str(SCRIPTS))

import ct_install  # noqa: E402
import ct_platform  # noqa: E402
import ct_policy  # noqa: E402
import ct_rules  # noqa: E402
from ct_storage import LinkedPathError, shell_argument  # noqa: E402

AGENT_REL = {
    'claude-code': 'ct-architect.md',
    'codex': 'ct-architect.toml',
    'antigravity': 'ct-architect/agent.md',
}

KILL_SCRIPT = r'''
import json, os, sys
from pathlib import Path
args = json.loads(sys.argv[1])
sys.path.insert(0, args['scripts'])
import ct_install, ct_platform
cfg = ct_platform.load_platform(args['platform'])
plan = ct_install.build_plan(Path(args['source']), cfg, scope='user', env={}, home=Path(args['home']),
                             project_dir=None, default=None, global_rules=True, python=sys.executable)
dests = {os.path.abspath(Path(plan.paths[label]) / rel) for label, rel, _ in plan.files}
real = ct_install.atomic_write
count = [0]
def fake(path, payload):
    if os.path.abspath(path) in dests:
        count[0] += 1
        if count[0] == 2:
            os._exit(1)
    return real(path, payload)
ct_install.atomic_write = fake
ct_install.apply_plan(plan, dry_run=False, replace_unmanaged=False, overwrite_modified=False)
'''

LOCK_SCRIPT = r'''
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import ct_storage
with ct_storage.locked(sys.argv[2]):
    Path(sys.argv[3]).write_text('ready')
    time.sleep(2)
'''


def snapshot(root, exclude=()):
    """Every directory and file (hidden included) under root, files with sha256."""
    root = Path(root)
    skip = {Path(os.path.abspath(item)) for item in exclude}
    result = {}
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(os.path.abspath(dirpath))
        dirnames[:] = [name for name in dirnames if here / name not in skip]
        for name in dirnames:
            result[(here / name).relative_to(os.path.abspath(root)).as_posix()] = 'dir'
        for name in filenames:
            path = here / name
            if path in skip:
                continue
            key = path.relative_to(os.path.abspath(root)).as_posix()
            result[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


class InstallTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / 'home'
        self.project = self.tmp / 'project'
        self.home.mkdir()
        self.project.mkdir()
        self.env = {}

    # helpers -------------------------------------------------------------
    def cfg(self, name):
        return ct_platform.load_platform(name)

    def paths(self, name='claude-code', scope='user', home=None):
        return ct_platform.target_paths(self.cfg(name), scope, self.env, home or self.home, self.project)

    def plan(self, name='claude-code', source=ROOT, scope='user', home=None, default=None,
             global_rules=False, docs_mcp=()):
        return ct_install.build_plan(source, self.cfg(name), scope=scope, env=self.env,
                                     home=home or self.home, project_dir=self.project,
                                     default=default, global_rules=global_rules,
                                     python=sys.executable, docs_mcp=docs_mcp)

    def apply(self, plan, replace_unmanaged=False, overwrite_modified=False):
        return ct_install.apply_plan(plan, dry_run=False, replace_unmanaged=replace_unmanaged,
                                     overwrite_modified=overwrite_modified)

    def install(self, name='claude-code', **kwargs):
        flags = {k: kwargs.pop(k) for k in ('replace_unmanaged', 'overwrite_modified') if k in kwargs}
        return self.apply(self.plan(name, **kwargs), **flags)

    def uninstall(self, name='claude-code', scope='user', dry_run=False, remove_modified=False):
        return ct_install.uninstall(self.cfg(name), scope=scope, env=self.env, home=self.home,
                                    project_dir=self.project, dry_run=dry_run,
                                    remove_modified=remove_modified)

    def recover(self, name='claude-code'):
        return ct_install.recover(self.cfg(name), scope='user', env=self.env, home=self.home,
                                  project_dir=self.project)

    def copy_source(self):
        src = self.tmp / 'src'
        src.mkdir()
        for name in ('SKILL.md', 'LICENSE'):
            shutil.copy2(ROOT / name, src / name)
        for name in ('references', 'agents', 'scripts', 'platforms'):
            shutil.copytree(ROOT / name, src / name, ignore=shutil.ignore_patterns('__pycache__'))
        return src

    def backup_contains(self, backup_dir, data):
        self.assertIsNotNone(backup_dir)
        found = [p for p in Path(backup_dir).rglob('*') if p.is_file() and p.read_bytes() == data]
        self.assertTrue(found, f'backup in {backup_dir} lacks expected bytes')

    def make_link(self, link, real):
        """Junction on Windows (no privilege needed), symlink elsewhere; skip if neither works."""
        link = Path(link)
        if os.name == 'nt':
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(real)],
                                    capture_output=True, text=True)
            if result.returncode == 0:
                self.addCleanup(lambda: subprocess.run(['cmd', '/c', 'rmdir', str(link)], capture_output=True))
                return
        try:
            os.symlink(real, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('neither junction nor symlink available')
        self.addCleanup(lambda: os.path.lexists(link) and os.unlink(link))

    # tests ---------------------------------------------------------------
    def test_dry_run_changes_nothing(self):
        for name in ct_platform.PLATFORMS:
            with self.subTest(platform=name, case='empty home'):
                home = self.tmp / f'empty-{name}'
                home.mkdir()
                before = snapshot(self.tmp)
                result = ct_install.apply_plan(self.plan(name, home=home, default='off', global_rules=True),
                                               dry_run=True, replace_unmanaged=False, overwrite_modified=False)
                self.assertTrue(result['dry_run'])
                self.assertTrue(result['would_write'])
                self.assertFalse(result['journal_present'])
                self.assertEqual(snapshot(self.tmp), before)
                names = [p.name for p in home.rglob('*')]
                for bad in ('install.lock', 'install.lock.lock', 'backups', 'install-journal.json'):
                    self.assertNotIn(bad, names)
                self.assertFalse([n for n in names if n.startswith('.staging-')])
            with self.subTest(platform=name, case='installed and modified'):
                home = self.tmp / f'installed-{name}'
                home.mkdir()
                self.apply(self.plan(name, home=home, global_rules=True))
                agent = self.paths(name, home=home)['agents_dir'] / AGENT_REL[name]
                agent.write_bytes(agent.read_bytes() + b'\nuser edit\n')
                before = snapshot(self.tmp)
                result = ct_install.apply_plan(self.plan(name, home=home, default='on', global_rules=True),
                                               dry_run=True, replace_unmanaged=False, overwrite_modified=False)
                self.assertIn(agent.as_posix(), result['conflicts']['modified'])
                self.assertEqual(snapshot(self.tmp), before)
                names = [p.name for p in home.rglob('*')]
                self.assertNotIn('install-journal.json', names)
                self.assertNotIn('backups', names)
                self.assertFalse([n for n in names if n.startswith('.staging-')])

    def test_install_layout_per_platform(self):
        self.install('claude-code')
        claude = self.home / '.claude'
        self.assertTrue((claude / 'skills/coding-team/SKILL.md').is_file())
        self.assertTrue((claude / 'agents/ct-architect.md').is_file())
        self.assertTrue((claude / 'agents/ct-reviewer-verify.md').is_file())
        self.assertFalse((claude / 'skills/coding-team/agents/openai.yaml').exists())
        self.install('codex')
        self.assertTrue((self.home / '.codex/agents/ct-architect.toml').is_file())
        self.assertTrue((self.home / '.agents/skills/coding-team/agents/openai.yaml').is_file())
        self.install('antigravity')
        self.assertTrue((self.home / '.gemini/config/agents/ct-architect/agent.md').is_file())
        for name in ct_platform.PLATFORMS:
            with self.subTest(platform=name):
                skill = self.paths(name)['skill_dir']
                self.assertEqual([p.name for p in (skill / 'platforms').iterdir()], [f'{name}.json'])
                self.assertTrue((skill / 'agents/roles/roles.json').is_file())
                self.assertTrue((skill / 'references/workflow.md').is_file())
                self.assertTrue((skill / 'scripts/ct_install.py').is_file())
                self.assertFalse((skill / 'scripts/export_evaluation_inputs.py').exists())
                self.assertFalse((skill / 'scripts/build_runtime_package.py').exists())
                for path in skill.rglob('*.md'):
                    self.assertNotIn(b'\r\n', path.read_bytes())
                manifest = json.loads((skill / ct_install.MANIFEST_NAME).read_text(encoding='utf-8'))
                self.assertEqual(manifest['schema_version'], 1)
                self.assertEqual(manifest['platform'], name)
                self.assertEqual(manifest['scope'], 'user')
                self.assertIn('SKILL.md', manifest['files']['skill_dir'])
                self.assertIn(AGENT_REL[name], manifest['files']['agents_dir'])
                self.assertIsNone(manifest['rules_file'])

    def test_installed_agents_match_render_all(self):
        import ct_roles
        for name in ct_platform.PLATFORMS:
            with self.subTest(platform=name):
                self.install(name)
                rendered = ct_roles.render_all(ct_roles.load_roles(ROOT / 'agents/roles'), self.cfg(name))
                agents = self.paths(name)['agents_dir']
                for rel, data in rendered:
                    self.assertEqual((agents / rel).read_bytes(), data)

    def test_config_root_env_respected(self):
        alt = self.tmp / 'alt dir'
        self.env = {'CLAUDE_CONFIG_DIR': str(alt)}
        result = self.install('claude-code', default='off')
        self.assertTrue((alt / 'skills/coding-team/SKILL.md').is_file())
        self.assertTrue((alt / 'agents/ct-architect.md').is_file())
        self.assertEqual(result['state']['global_policy'], 'OFF')
        self.assertTrue((alt / 'coding-team/state.json').is_file())
        self.assertFalse((self.home / '.claude').exists())

    def test_second_install_is_noop(self):
        self.install('claude-code', global_rules=True, default='on')
        manifest = self.paths()['skill_dir'] / ct_install.MANIFEST_NAME
        first = manifest.read_bytes()
        before = snapshot(self.tmp)
        result = self.install('claude-code', global_rules=True, default='on')
        self.assertEqual(result['written'], [])
        self.assertIsNone(result['backup_dir'])
        self.assertTrue(result['unchanged'])
        self.assertEqual(manifest.read_bytes(), first)
        self.assertEqual(snapshot(self.tmp), before)

    def test_upgrade_unmodified_file_backs_up(self):
        src = self.copy_source()
        self.install('claude-code', source=src)
        agent = self.paths()['agents_dir'] / 'ct-architect.md'
        old = agent.read_bytes()
        role = src / 'agents/roles/ct-architect.md'
        role.write_bytes(role.read_bytes() + b'\nUpgraded guidance.\n')
        result = self.install('claude-code', source=src)
        self.assertIn(agent.as_posix(), result['written'])
        self.assertIn(b'Upgraded guidance.', agent.read_bytes())
        self.backup_contains(result['backup_dir'], old)
        self.assertEqual(result['modified_overwritten'], [])

    def test_stale_files_removed_or_kept_when_modified(self):
        src = self.copy_source()
        self.install('claude-code', source=src)
        skill = self.paths()['skill_dir']
        clean, edited = skill / 'references/workflow.md', skill / 'references/task-ledger.md'
        clean_bytes = clean.read_bytes()
        edited.write_bytes(b'user notes\n')
        (src / 'references/workflow.md').unlink()
        (src / 'references/task-ledger.md').unlink()
        result = self.install('claude-code', source=src)
        self.assertEqual(result['removed_stale'], [clean.as_posix()])
        self.assertEqual(result['kept_modified_stale'], [edited.as_posix()])
        self.assertFalse(clean.exists())
        self.assertEqual(edited.read_bytes(), b'user notes\n')
        self.backup_contains(result['backup_dir'], clean_bytes)

    def test_modified_file_conflict_by_default(self):
        self.install('claude-code')
        agent = self.paths()['agents_dir'] / 'ct-architect.md'
        agent.write_bytes(b'my own architect\n')
        before = snapshot(self.tmp)
        with self.assertRaises(ct_install.ModifiedTargetError) as caught:
            self.install('claude-code')
        self.assertIn(agent.as_posix(), caught.exception.paths)
        self.assertEqual(snapshot(self.tmp), before)
        result = self.install('claude-code', overwrite_modified=True)
        self.assertEqual(result['modified_overwritten'], [agent.as_posix()])
        self.assertNotEqual(agent.read_bytes(), b'my own architect\n')
        self.backup_contains(result['backup_dir'], b'my own architect\n')

    def test_unmanaged_target_requires_flag(self):
        skill = self.paths()['skill_dir']
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_bytes(b'someone else\n')
        before = snapshot(self.tmp)
        with self.assertRaises(ct_install.UnmanagedTargetError) as caught:
            self.install('claude-code')
        self.assertIn((skill / 'SKILL.md').as_posix(), caught.exception.paths)
        self.assertEqual(snapshot(self.tmp, exclude=[self.paths()['state_file'].parent]),
                         {k: v for k, v in before.items()})
        self.assertFalse(self.paths()['agents_dir'].exists())
        result = self.install('claude-code', replace_unmanaged=True)
        self.assertEqual((skill / 'SKILL.md').read_bytes(), (ROOT / 'SKILL.md').read_bytes().replace(b'\r\n', b'\n'))
        self.assertIn((skill / 'SKILL.md').as_posix(), result['replaced_unmanaged'])
        self.backup_contains(result['backup_dir'], b'someone else\n')

    def test_linked_target_refused(self):
        outside = self.tmp / 'outside'
        outside.mkdir()
        (outside / 'keep.md').write_bytes(b'keep\n')
        agents = self.paths()['agents_dir']
        agents.parent.mkdir(parents=True)
        self.make_link(agents, outside)
        before = snapshot(outside)
        for flags in ({}, {'replace_unmanaged': True, 'overwrite_modified': True}):
            with self.assertRaises(LinkedPathError):
                self.install('claude-code', **flags)
        result = ct_install.preview(self.plan('claude-code'))
        self.assertTrue(result['conflicts']['linked'])
        self.assertEqual(snapshot(outside), before)
        self.assertFalse(self.paths()['skill_dir'].exists())
        self.assertFalse(self.paths()['state_file'].parent.exists())

    def test_links_at_or_above_config_root_allowed(self):
        real = self.tmp / 'real claude'
        real.mkdir()
        self.make_link(self.home / '.claude', real)
        for rules in (False, True):
            with self.subTest(global_rules=rules):
                plan = self.plan('claude-code', global_rules=rules)
                self.assertEqual(ct_install.preview(plan)['conflicts']['linked'], [])
                self.apply(plan)
                self.assertTrue((real / 'skills/coding-team/SKILL.md').is_file())
                self.assertTrue((real / 'agents/ct-architect.md').is_file())
                self.assertTrue((real / 'coding-team/state.json').is_file())
                self.assertEqual((real / 'CLAUDE.md').is_file(), rules)
        self.uninstall()
        self.assertFalse((real / 'CLAUDE.md').exists())
        self.assertFalse((real / 'skills/coding-team').exists())

    def test_links_below_config_root_refused(self):
        for case in ('agents', 'skills', 'coding-team'):
            with self.subTest(link=case):
                self.home = self.tmp / f'home-{case}'
                outside = self.tmp / f'outside-{case}'
                outside.mkdir()
                (self.home / '.claude').mkdir(parents=True)
                self.make_link(self.home / '.claude' / case, outside)
                plan = self.plan('claude-code')
                self.assertTrue(ct_install.preview(plan)['conflicts']['linked'])
                with self.assertRaises(LinkedPathError):
                    self.apply(plan, replace_unmanaged=True, overwrite_modified=True)
                self.assertEqual(list(outside.iterdir()), [])

    def test_linked_rules_file_refused_only_with_global_rules(self):
        outside = self.tmp / 'outside'
        outside.mkdir()
        rules = self.paths()['global_rules_file']
        rules.parent.mkdir(parents=True)
        self.make_link(rules, outside)  # a directory junction named CLAUDE.md stands in for a linked file
        plan = self.plan('claude-code', global_rules=True)
        self.assertIn(rules.as_posix(), ct_install.preview(plan)['conflicts']['linked'])
        with self.assertRaises(LinkedPathError):
            self.apply(plan)
        self.assertFalse(self.paths()['skill_dir'].exists())
        plan = self.plan('claude-code')
        self.assertEqual(ct_install.preview(plan)['conflicts']['linked'], [])
        self.apply(plan)
        self.assertTrue((self.paths()['skill_dir'] / 'SKILL.md').is_file())
        self.uninstall()
        self.assertEqual(list(outside.iterdir()), [])

    def test_rollback_on_exception(self):
        src = self.copy_source()
        self.install('claude-code', source=src, global_rules=True)
        paths = self.paths()
        keep = [paths['backup_root']]
        after_first = snapshot(self.home, exclude=keep)
        role = src / 'agents/roles/ct-architect.md'
        role.write_bytes(role.read_bytes() + b'\nSecond version.\n')
        (src / 'SKILL.md').write_bytes((src / 'SKILL.md').read_bytes() + b'\nSecond version.\n')
        plan = self.plan('claude-code', source=src, global_rules=True)
        dests = {os.path.abspath(Path(plan.paths[label]) / rel) for label, rel, _ in plan.files}
        real, count = ct_install.atomic_write, [0]

        def failing(path, payload):
            if os.path.abspath(path) in dests:
                count[0] += 1
                if count[0] == 2:
                    raise OSError('fixture write failure')
            return real(path, payload)

        with mock.patch.object(ct_install, 'atomic_write', side_effect=failing):
            with self.assertRaises(ct_install.InstallRolledBack) as caught:
                self.apply(plan)
        self.assertEqual(snapshot(self.home, exclude=keep), after_first)
        self.assertFalse(paths['journal_file'].exists())
        self.assertFalse(list(paths['backup_root'].glob('.staging-*')))
        backup_dir = caught.exception.backup_dir
        self.assertTrue(Path(backup_dir).is_dir())
        self.assertIn(str(backup_dir), str(caught.exception))

    def failing_writes(self, plan, fail_from=2, fail_until=2):
        dests = {os.path.abspath(Path(plan.paths[label]) / rel) for label, rel, _ in plan.files}
        real, count = ct_install.atomic_write, [0]

        def failing(path, payload):
            if os.path.abspath(path) in dests:
                count[0] += 1
                if fail_from <= count[0] <= fail_until:
                    raise PermissionError('fixture locked file')
            return real(path, payload)
        return mock.patch.object(ct_install, 'atomic_write', side_effect=failing)

    def test_rollback_of_first_install_removes_created_dirs(self):
        before = snapshot(self.home)
        plan = self.plan('antigravity', global_rules=True)
        with self.failing_writes(plan):
            with self.assertRaises(ct_install.InstallRolledBack) as caught:
                self.apply(plan)
        self.assertIsNone(caught.exception.backup_dir)
        state_dir = self.paths('antigravity')['state_file'].parent
        self.assertEqual(snapshot(self.home, exclude=[state_dir]),
                         {'.gemini': 'dir', '.gemini/antigravity': 'dir'})
        self.assertFalse(self.paths('antigravity')['journal_file'].exists())
        self.assertFalse((state_dir / 'state.json').exists())
        self.assertNotEqual(snapshot(self.home), before)  # only the lock file remains in the state dir

    def test_incomplete_rollback_keeps_journal_for_recover(self):
        src = self.copy_source()
        self.install('claude-code', source=src)
        before = snapshot(self.home, exclude=[self.paths()['backup_root']])
        (src / 'SKILL.md').write_bytes(b'# new skill\n')
        role = src / 'agents/roles/ct-architect.md'
        role.write_bytes(role.read_bytes() + b'\nNew.\n')
        plan = self.plan('claude-code', source=src)
        with self.failing_writes(plan, fail_from=2, fail_until=99):
            with self.assertRaises(ct_install.IncompleteTransactionError) as caught:
                self.apply(plan)
        self.assertIn('--recover', str(caught.exception))
        self.assertTrue(caught.exception.paths)
        self.assertTrue(self.paths()['journal_file'].is_file())
        self.recover()
        self.assertEqual(snapshot(self.home, exclude=[self.paths()['backup_root']]), before)

    def after_journal(self, mutate, name='claude-code'):
        """Patch atomic_write so mutate() runs right after the journal is first written,
        i.e. after assessment and backups and before any target is changed."""
        journal = os.path.abspath(self.paths(name)['journal_file'])
        real, done = ct_install.atomic_write, [False]

        def hook(path, payload):
            result = real(path, payload)
            if not done[0] and os.path.abspath(path) == journal:
                done[0] = True
                mutate()
            return result
        return mock.patch.object(ct_install, 'atomic_write', side_effect=hook)

    def test_change_during_install_rolls_back_and_keeps_user_bytes(self):
        src = self.copy_source()
        paths = self.paths()
        paths['global_rules_file'].parent.mkdir(parents=True)
        paths['global_rules_file'].write_bytes(b'# my rules\n')
        self.install('claude-code', source=src)
        skill, agent = paths['skill_dir'] / 'SKILL.md', paths['agents_dir'] / 'ct-architect.md'
        skill_before = skill.read_bytes()
        (src / 'SKILL.md').write_bytes(b'# second version\n')
        role = src / 'agents/roles/ct-architect.md'
        role.write_bytes(role.read_bytes() + b'\nSecond.\n')

        def mutate():
            agent.write_bytes(b'edited during install\n')
            with open(paths['global_rules_file'], 'ab') as stream:
                stream.write(b'edited too\n')

        with self.after_journal(mutate):
            with self.assertRaises(ct_install.InstallRolledBack) as caught:
                self.install('claude-code', source=src, global_rules=True)
        self.assertIn(agent.as_posix(), str(caught.exception))
        self.assertEqual(agent.read_bytes(), b'edited during install\n')
        self.assertEqual(paths['global_rules_file'].read_bytes(), b'# my rules\nedited too\n')
        self.assertEqual(skill.read_bytes(), skill_before)
        self.assertFalse(paths['journal_file'].exists())

    def test_file_created_during_install_is_not_overwritten(self):
        src = self.copy_source()
        self.install('claude-code', source=src)
        skill_dir = self.paths()['skill_dir']
        skill_before = (skill_dir / 'SKILL.md').read_bytes()
        (src / 'SKILL.md').write_bytes(b'# second version\n')
        (src / 'references/aaa-new.md').write_bytes(b'new reference\n')
        created = skill_dir / 'references/aaa-new.md'
        with self.after_journal(lambda: created.write_bytes(b'user file\n')):
            with self.assertRaises(ct_install.InstallRolledBack):
                self.install('claude-code', source=src)
        self.assertEqual(created.read_bytes(), b'user file\n')
        self.assertEqual((skill_dir / 'SKILL.md').read_bytes(), skill_before)

    def test_change_during_uninstall_rolls_back(self):
        self.install('claude-code')
        paths = self.paths()
        skill, agent = paths['skill_dir'] / 'SKILL.md', paths['agents_dir'] / 'ct-architect.md'
        skill_before = skill.read_bytes()
        with self.after_journal(lambda: agent.write_bytes(b'edited during uninstall\n')):
            with self.assertRaises(ct_install.InstallRolledBack):
                self.uninstall()
        self.assertEqual(agent.read_bytes(), b'edited during uninstall\n')
        self.assertEqual(skill.read_bytes(), skill_before)
        self.assertTrue((paths['skill_dir'] / ct_install.MANIFEST_NAME).is_file())
        self.assertEqual(self.uninstall()['kept_modified'], [agent.as_posix()])

    def move_behind_link(self, directory, name):
        """Simulate a user moving a directory to dotfiles and linking it back."""
        real = self.tmp / name
        shutil.move(str(directory), str(real))
        (real / 'other-skill').mkdir()
        (real / 'other-skill/SKILL.md').write_bytes(b'other\n')
        self.make_link(directory, real)
        return real

    def assert_link_kept(self, link, real):
        self.assertTrue(os.path.lexists(link))
        self.assertEqual((link / 'other-skill/SKILL.md').read_bytes(), b'other\n')
        self.assertFalse((real / 'coding-team').exists())
        self.assertTrue((self.home / '.agents').is_dir())

    def test_uninstall_keeps_user_link_in_created_dirs(self):
        self.install('codex')
        skills = self.home / '.agents/skills'
        real = self.move_behind_link(skills, 'dotfiles-skills')
        self.assertEqual(self.uninstall('codex', dry_run=True)['kept_links'], [skills.as_posix()])
        result = self.uninstall('codex')
        self.assertEqual(result['kept_links'], [skills.as_posix()])
        self.assert_link_kept(skills, real)

    def test_recover_keeps_user_link_in_created_dirs(self):
        args = {'scripts': str(SCRIPTS), 'platform': 'codex', 'source': str(ROOT), 'home': str(self.home)}
        killed = subprocess.run([sys.executable, '-c', KILL_SCRIPT, json.dumps(args)],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(killed.returncode, 1, killed.stderr)
        skills = self.home / '.agents/skills'
        real = self.move_behind_link(skills, 'dotfiles-skills')
        result = self.recover('codex')
        self.assertEqual(result['kept_links'], [skills.as_posix()])
        self.assertEqual(result['deleted_new'], [(skills / 'coding-team/SKILL.md').as_posix()])
        self.assert_link_kept(skills, real)

    def test_rollback_keeps_user_link_in_created_dirs(self):
        plan = self.plan('codex')
        dests = {os.path.abspath(Path(plan.paths[label]) / rel) for label, rel, _ in plan.files}
        skills = self.home / '.agents/skills'
        real, count, holder = ct_install.atomic_write, [0], {}

        def failing(path, payload):
            if os.path.abspath(path) in dests:
                count[0] += 1
                if count[0] == 2:
                    holder['real'] = self.move_behind_link(skills, 'dotfiles-skills')
                    raise OSError('fixture write failure')
            return real(path, payload)

        with mock.patch.object(ct_install, 'atomic_write', side_effect=failing):
            with self.assertRaises(ct_install.InstallRolledBack) as caught:
                self.apply(plan)
        self.assertIn('linked directories left in place: ' + skills.as_posix(), str(caught.exception))
        self.assert_link_kept(skills, holder['real'])

    def test_corrupt_journal_refused(self):
        self.install('claude-code')
        journal = self.paths()['journal_file']
        journal.write_bytes(b'{broken')
        with self.assertRaises(ct_install.RecoveryUnsafeError):
            self.recover()
        self.assertEqual(journal.read_bytes(), b'{broken')

    def killed_install(self, new_reference=False):
        """Second install killed at its 2nd plan-file write; the 1st write is SKILL.md (an
        upgrade) or, with new_reference, a brand-new references/aaa-new.md."""
        src = self.copy_source()
        self.install('claude-code', source=src, global_rules=True)
        before = snapshot(self.home, exclude=[self.paths()['backup_root']])
        role = src / 'agents/roles/ct-architect.md'
        role.write_bytes(role.read_bytes() + b'\nKilled version.\n')
        if new_reference:
            (src / 'references/aaa-new.md').write_bytes(b'new reference\n')
        else:
            (src / 'SKILL.md').write_bytes((src / 'SKILL.md').read_bytes() + b'\nKilled version.\n')
        args = {'scripts': str(SCRIPTS), 'platform': 'claude-code', 'source': str(src), 'home': str(self.home)}
        result = subprocess.run([sys.executable, '-c', KILL_SCRIPT, json.dumps(args)],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue(self.paths()['journal_file'].is_file())
        return src, before

    def test_killed_process_leaves_journal_then_recover(self):
        src, before = self.killed_install()
        plan = self.plan('claude-code', source=src, global_rules=True)
        with self.assertRaises(ct_install.IncompleteTransactionError) as caught:
            self.apply(plan)
        self.assertIn('--recover', str(caught.exception))
        with self.assertRaises(ct_install.IncompleteTransactionError):
            self.uninstall()
        result = self.recover()
        self.assertTrue(result['journal_removed'])
        self.assertTrue(result['restored'])
        self.assertTrue(Path(result['backup_dir']).is_dir())
        paths = self.paths()
        self.assertFalse(paths['journal_file'].exists())
        self.assertEqual(snapshot(self.home, exclude=[paths['backup_root']]), before)
        self.assertEqual(self.recover()['journal_removed'], False)
        final = self.apply(plan)
        self.assertTrue(final['written'])

    def test_recover_deletes_new_file_only_if_unchanged(self):
        self.killed_install(new_reference=True)
        new = self.paths()['skill_dir'] / 'references/aaa-new.md'
        self.assertEqual(new.read_bytes(), b'new reference\n')
        result = self.recover()
        self.assertEqual(result['deleted_new'], [new.as_posix()])
        self.assertEqual(result['kept_unexpected'], [])
        self.assertFalse(new.exists())

    def test_recover_keeps_new_file_created_by_user_after_crash(self):
        self.killed_install(new_reference=True)
        new = self.paths()['skill_dir'] / 'references/aaa-new.md'
        new.write_bytes(b'user wrote this after the crash\n')
        result = self.recover()
        self.assertEqual(result['kept_unexpected'], [new.as_posix()])
        self.assertEqual(result['deleted_new'], [])
        self.assertTrue(result['journal_removed'])
        self.assertEqual(new.read_bytes(), b'user wrote this after the crash\n')

    def test_recover_keeps_existing_file_edited_after_crash(self):
        self.killed_install()
        skill = self.paths()['skill_dir'] / 'SKILL.md'
        skill.write_bytes(b'user edit after crash\n')
        result = self.recover()
        self.assertEqual(result['kept_unexpected'], [skill.as_posix()])
        self.assertEqual(skill.read_bytes(), b'user edit after crash\n')

    def test_recover_refuses_tampered_backup(self):
        self.killed_install()
        journal = json.loads(self.paths()['journal_file'].read_text(encoding='utf-8'))
        backups = [t['backup'] for t in journal['targets'] if t['backup']]
        self.assertTrue(backups)
        tampered = Path(backups[0])
        data = bytearray(tampered.read_bytes())
        data[0] ^= 0x01
        tampered.write_bytes(bytes(data))
        before = snapshot(self.tmp)
        with self.assertRaises(ct_install.RecoveryUnsafeError) as caught:
            self.recover()
        self.assertIn(tampered.as_posix(), caught.exception.paths)
        self.assertEqual(snapshot(self.tmp), before)
        tampered.unlink()
        with self.assertRaises(ct_install.RecoveryUnsafeError):
            self.recover()
        self.assertTrue(self.paths()['journal_file'].is_file())

    def test_concurrent_install_waits_for_lock(self):
        paths = self.paths()
        ready = self.tmp / 'ready'
        holder = subprocess.Popen([sys.executable, '-c', LOCK_SCRIPT, str(SCRIPTS), str(paths['lock_file']),
                                   str(ready)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(holder.wait)
        deadline = time.monotonic() + 30
        while not ready.exists():
            self.assertLess(time.monotonic(), deadline, 'lock holder did not start')
            time.sleep(0.02)
        start = time.monotonic()
        result = self.install('claude-code')
        self.assertGreaterEqual(time.monotonic() - start, 1.0)
        self.assertTrue(result['written'])
        self.assertEqual(holder.wait(timeout=30), 0)

    def test_scope_rules(self):
        with self.assertRaises(ValueError):
            self.plan('claude-code', scope='project', default='on')
        with self.assertRaises(ValueError):
            self.plan('claude-code', scope='project', global_rules=True)
        result = self.install('claude-code', scope='project')
        user = self.paths()
        self.assertFalse(user['state_file'].exists())
        self.assertFalse(user['global_rules_file'].exists())
        self.assertIsNone(result['state'])
        self.assertIsNone(result['rules_file'])
        self.assertTrue((self.project / '.claude/skills/coding-team/SKILL.md').is_file())
        self.assertTrue((self.project / '.claude/agents/ct-architect.md').is_file())

    def test_shared_project_skill_dir_other_platform_refused(self):
        self.install('codex', scope='project')
        with self.assertRaises(ValueError):
            self.install('antigravity', scope='project')

    def test_default_and_state_preservation(self):
        result = self.install('claude-code', default='off')
        state = self.paths()['state_file']
        self.assertEqual(result['state']['global_policy'], 'OFF')
        ct_policy.apply_command(state, '/coding-team on', 'chat-1')
        saved = state.read_bytes()
        result = self.install('claude-code')
        self.assertEqual(state.read_bytes(), saved)
        self.assertEqual(result['state']['chat_policies'], {'chat-1': 'ON'})
        result = self.install('claude-code', default='on')
        self.assertEqual(result['state']['global_policy'], 'ON')
        self.assertEqual(result['state']['chat_policies'], {'chat-1': 'ON'})
        self.backup_contains(result['backup_dir'], saved)

    def test_corrupt_state_fails_before_writing(self):
        state = self.paths()['state_file']
        state.parent.mkdir(parents=True)
        state.write_bytes(b'{not json')
        with self.assertRaises(ValueError):
            self.install('claude-code', default='on')
        self.assertEqual(state.read_bytes(), b'{not json')
        self.assertFalse(self.paths()['skill_dir'].exists())

    def test_global_rules_migration(self):
        rules = self.paths('codex')['global_rules_file']
        rules.parent.mkdir(parents=True)
        prefix, suffix = b'# mine\r\n\xff keep\n', b'\n# after\n'
        old = prefix + ct_rules.START.encode() + b'\nrun /coding team on all\n' + ct_rules.END.encode() + suffix
        rules.write_bytes(old)
        result = self.install('codex', global_rules=True)
        data = rules.read_bytes()
        self.assertEqual(data.count(ct_rules.START.encode()), 1)
        self.assertTrue(data.startswith(prefix))
        self.assertTrue(data.endswith(suffix))
        self.assertNotIn(b'/coding team on all', data)
        self.assertIn(b'/coding-team on --global', data)
        self.assertEqual(result['rules_file'], rules.as_posix())
        self.backup_contains(result['backup_dir'], old)
        self.assertEqual(self.install('codex', global_rules=True)['written'], [])

    def test_rules_helpers_match_ct_rules(self):
        block = ct_rules.render_rules_block(platform='codex', python=sys.executable, cli=self.tmp / 'c.py',
                                            state=self.tmp / 's.json', skill_dir=self.tmp)
        marked = b'a\n' + ct_rules.START.encode() + b'x' + ct_rules.END.encode() + b'\nb'
        for original in (b'', b'x', b'x\n', b'\xef\xbb\xbfa\r\nb\r\n', marked):
            with self.subTest(original=original):
                path = self.tmp / 'rules' / 'R.md'
                shutil.rmtree(path.parent, ignore_errors=True)
                path.parent.mkdir()
                if original:
                    path.write_bytes(original)
                ct_rules.upsert_block(path, block)
                upserted = path.read_bytes()
                self.assertEqual(ct_install._rules_after(original, block), upserted)
                ct_rules.remove_block(path)
                self.assertEqual(ct_install._rules_without(upserted), path.read_bytes())

    def test_docs_mcp_only_claude(self):
        tool = 'mcp__plugin_context7_context7__query-docs'
        self.install('claude-code', docs_mcp=[tool])
        self.assertIn(tool, (self.paths()['agents_dir'] / 'ct-researcher.md').read_text(encoding='utf-8'))
        manifest = json.loads((self.paths()['skill_dir'] / ct_install.MANIFEST_NAME).read_text(encoding='utf-8'))
        self.assertEqual(manifest['options']['docs_mcp'], [tool])
        with self.assertRaises(ValueError):
            self.plan('codex', docs_mcp=[tool])

    def test_uninstall_keeps_modified(self):
        paths = self.paths()
        self.install('claude-code', global_rules=True, default='on')
        self.assertTrue(paths['global_rules_file'].is_file())
        agent = paths['agents_dir'] / 'ct-architect.md'
        agent.write_bytes(b'mine\n')
        before = snapshot(self.tmp)
        preview = self.uninstall(dry_run=True)
        self.assertTrue(preview['dry_run'])
        self.assertIn(agent.as_posix(), preview['kept_modified'])
        self.assertEqual(snapshot(self.tmp), before)
        result = self.uninstall()
        self.assertEqual(result['kept_modified'], [agent.as_posix()])
        self.assertIn((paths['skill_dir'] / 'SKILL.md').as_posix(), result['removed'])
        self.assertTrue(result['rules_removed'])
        self.assertFalse(paths['global_rules_file'].exists())
        self.assertTrue(paths['state_file'].is_file())
        self.assertEqual(agent.read_bytes(), b'mine\n')
        self.assertFalse((paths['skill_dir'] / 'SKILL.md').exists())
        result = self.uninstall(remove_modified=True)
        self.assertEqual(result['removed'], [agent.as_posix()])
        self.backup_contains(result['backup_dir'], b'mine\n')
        left = snapshot(self.home, exclude=[paths['state_file'].parent])
        self.assertEqual(left, {'.claude': 'dir'})
        with self.assertRaises(ValueError):
            self.uninstall()

    def test_uninstall_keeps_existing_rules_file_bytes(self):
        rules = self.paths()['global_rules_file']
        rules.parent.mkdir(parents=True)
        rules.write_bytes(b'# user rules\n')
        self.install('claude-code', global_rules=True)
        self.uninstall()
        self.assertEqual(rules.read_bytes(), b'# user rules\n')

    def test_project_uninstall_restores_project_tree(self):
        before = snapshot(self.project)
        self.install('antigravity', scope='project')
        self.uninstall('antigravity', scope='project')
        self.assertEqual(snapshot(self.project), before)

    def test_uninstall_unmanaged_refused(self):
        with self.assertRaisesRegex(ValueError, 'not a coding-team managed installation'):
            self.uninstall()
        skill = self.paths()['skill_dir']
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_bytes(b'other\n')
        with self.assertRaisesRegex(ValueError, 'not a coding-team managed installation'):
            self.uninstall()
        self.assertEqual((skill / 'SKILL.md').read_bytes(), b'other\n')

    def test_tampered_manifest_paths_rejected(self):
        self.install('claude-code')
        victim = self.tmp / 'victim.txt'
        victim.write_bytes(b'victim\n')
        manifest_path = self.paths()['skill_dir'] / ct_install.MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['files']['agents_dir']['../../../victim.txt'] = hashlib.sha256(b'victim\n').hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        with self.assertRaises(ValueError):
            self.uninstall()
        with self.assertRaises(ValueError):
            self.install('claude-code')
        self.assertEqual(victim.read_bytes(), b'victim\n')

    def test_home_with_special_characters(self):
        self.home = self.tmp / "Nguyễn Văn A's home"
        self.home.mkdir()
        result = self.install('claude-code', global_rules=True, default='on')
        paths = self.paths()
        self.assertTrue((paths['skill_dir'] / 'SKILL.md').is_file())
        block = paths['global_rules_file'].read_text(encoding='utf-8')
        cli = (paths['skill_dir'] / 'scripts/coding_team.py').as_posix()
        self.assertIn(shell_argument(cli), block)
        self.assertIn(shell_argument(paths['state_file'].as_posix()), block)
        self.assertEqual(result['state']['global_policy'], 'ON')
        self.uninstall()
        self.assertFalse(paths['skill_dir'].exists())


if __name__ == '__main__':
    unittest.main()
