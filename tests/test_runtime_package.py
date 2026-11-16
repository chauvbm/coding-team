import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/build_runtime_package.py'
SPEC = importlib.util.spec_from_file_location('runtime_builder', SCRIPT)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class RuntimePackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'source'
        self.root.mkdir()
        for name in ('LICENSE', 'SKILL.md'):
            (self.root / name).write_bytes(b'example\r\n')
        for name in ('scripts', 'references', 'agents', 'tests', 'benchmarks', 'evaluations', 'graphify-out', '.git'):
            (self.root / name).mkdir()
            (self.root / name / 'marker.txt').write_text(name)
        (self.root / 'platforms').mkdir()
        for name in ('a', 'b'):
            (self.root / 'platforms' / (name + '.json')).write_text('{"platform": "%s"}' % name)
        cache = self.root / 'scripts/__pycache__'
        cache.mkdir()
        (cache / 'ignore.pyc').write_bytes(b'cache')
        backups = self.root / 'scripts/backups'
        backups.mkdir()
        (backups / 'private.txt').write_text('do not distribute')
        self.out = self.root.parent / 'runtime.zip'

    def test_only_operational_resources_and_matching_manifest(self):
        builder.build_package(self.root, self.out)
        with zipfile.ZipFile(self.out) as archive:
            names = set(archive.namelist())
            self.assertEqual(names, {'coding-team/' + n for n in ('LICENSE', 'SKILL.md',
                'scripts/marker.txt', 'references/marker.txt', 'agents/marker.txt',
                'platforms/a.json', 'platforms/b.json', 'runtime-package.json')})
            manifest = json.loads(archive.read('coding-team/runtime-package.json'))
            self.assertIsNone(manifest['source_git_head'])
            self.assertEqual(archive.read('coding-team/SKILL.md'), b'example\n')
            for name, metadata in manifest['files'].items():
                data = archive.read('coding-team/' + name)
                self.assertEqual(metadata, {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)})

    def test_platform_filter_keeps_one_config(self):
        builder.build_package(self.root, self.out, platform='b')
        with zipfile.ZipFile(self.out) as archive:
            names = set(archive.namelist())
            manifest = json.loads(archive.read('coding-team/runtime-package.json'))
        self.assertIn('coding-team/platforms/b.json', names)
        self.assertNotIn('coding-team/platforms/a.json', names)
        self.assertIn('coding-team/scripts/marker.txt', names)
        self.assertNotIn('platforms/a.json', manifest['files'])
        self.assertEqual(manifest['platform'], 'b')

    def test_release_packages_contain_external_runtime_and_install_without_creating_attempts(self):
        required = {
            'scripts/ct_antigravity.py', 'scripts/ct_external.py', 'scripts/ct_external_store.py',
            'scripts/ct_external_workspace.py', 'scripts/ct_winjob.py', 'references/control-policy.md',
            'references/antigravity-cli.md',
        }
        for platform in (None, 'codex'):
            with self.subTest(platform=platform):
                root = self.root.parent / (platform or 'all')
                root.mkdir()
                bundle = builder.build_package(SCRIPT.parents[1], root / 'runtime.zip', platform)
                with zipfile.ZipFile(bundle) as archive:
                    manifest = json.loads(archive.read('coding-team/runtime-package.json'))
                    self.assertTrue(required <= manifest['files'].keys(), manifest['files'].keys())
                    self.assertFalse({'tests', 'docs', 'giai-doan1', 'evaluations', 'graphify-out'}
                                     & {name.split('/', 1)[0] for name in manifest['files']})
                    self.assertFalse(any('fake_agy' in name for name in manifest['files']))
                    archive.extractall(root / 'extracted')

                home = root / 'home'
                home.mkdir()
                env = dict(os.environ, USERPROFILE=str(home), HOME=str(home), CODEX_HOME=str(home / '.codex'),
                           PYTHONIOENCODING='utf-8')
                cli = root / 'extracted/coding-team/scripts/coding_team.py'
                result = subprocess.run([sys.executable, str(cli), 'install', '--platform', 'codex'],
                                        cwd=root, env=env, capture_output=True, text=True, timeout=45)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                install = json.loads(result.stdout)
                installed_manifest = json.loads(Path(install['manifest']).read_bytes())
                self.assertEqual(installed_manifest['external_abi'], 1)
                self.assertTrue(required <= installed_manifest['files']['skill_dir'].keys())
                self.assertEqual(installed_manifest['state_path'], (home / '.codex/coding-team/state.json').as_posix())
                self.assertFalse((home / '.codex/coding-team/external').exists())

    def test_unknown_platform_rejected_before_output(self):
        for bad in ('c', '../a', ''):
            with self.assertRaises(ValueError): builder.build_package(self.root, self.out, platform=bad)
        self.assertFalse(self.out.exists())

    def test_existing_output_and_in_tree_output_are_preserved(self):
        self.out.write_bytes(b'keep')
        with self.assertRaises(FileExistsError): builder.build_package(self.root, self.out)
        self.assertEqual(self.out.read_bytes(), b'keep')
        with self.assertRaises(ValueError): builder.build_package(self.root, self.root / 'recursive.zip')

    def test_link_preflight_happens_before_output_allocation(self):
        target = (self.root / 'scripts/marker.txt').resolve()
        original = Path.is_symlink
        with mock.patch.object(Path, 'is_symlink', lambda p: p == target or original(p)), \
                mock.patch.object(builder.tempfile, 'mkdtemp') as allocate:
            with self.assertRaises(ValueError): builder.build_package(self.root)
            allocate.assert_not_called()
        self.assertFalse(self.out.exists())

    def test_publication_failure_leaves_no_partial_file(self):
        with mock.patch.object(builder.os, 'link', side_effect=OSError('no hard links')):
            with self.assertRaises(OSError): builder.build_package(self.root, self.out)
        self.assertFalse(self.out.exists())
        self.assertEqual(list(self.out.parent.glob('.runtime-*.zip')), [])

    def test_real_broken_link_is_rejected_before_output(self):
        link = self.root / 'scripts/linked'
        try: link.symlink_to(self.root / 'absent')
        except OSError as error: self.skipTest(str(error))
        with self.assertRaises(ValueError): builder.build_package(self.root, self.out)
        self.assertFalse(self.out.exists())


if __name__ == '__main__': unittest.main()
