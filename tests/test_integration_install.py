import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest

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


if __name__ == '__main__':
    unittest.main()
