"""Checkpoint C: end-to-end tests of the unified CLI, always through a subprocess."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'scripts' / 'coding_team.py'
STRIP_PREFIXES = ('CODEX_', 'ANTIGRAVITY_')
STRIP_NAMES = {'CLAUDECODE', 'CLAUDE_CONFIG_DIR', 'CLAUDE_CODE_SESSION_ID',
               'CLAUDE_CODE_SUBAGENT_MODEL_FORCE', 'CLAUDE_CODE_ENTRYPOINT'}


def load_builder():
    spec = importlib.util.spec_from_file_location('runtime_builder', ROOT / 'scripts' / 'build_runtime_package.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IntegrationCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / 'home'
        self.home.mkdir()
        env = {k: v for k, v in os.environ.items()
               if k not in STRIP_NAMES and not k.startswith(STRIP_PREFIXES)}
        env.update(HOME=str(self.home), USERPROFILE=str(self.home),
                   GIT_CEILING_DIRECTORIES=str(self.root.parent), PYTHONIOENCODING='utf-8')
        self.env = env
        self.config_root = self.home / '.claude'
        self.skill = self.config_root / 'skills' / 'coding-team'
        self.state = self.config_root / 'coding-team' / 'state.json'

    # -- helpers ----------------------------------------------------------------------------

    def spawn(self, *args, env=None, cwd=None, script=CLI):
        run_env = dict(self.env, **(env or {}))
        return subprocess.Popen([sys.executable, str(script), *map(str, args)], env=run_env,
                                cwd=str(cwd or self.root), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding='utf-8')

    @staticmethod
    def finish(process):
        stdout, stderr = process.communicate(timeout=120)
        try:
            data = json.loads(stdout)
        except ValueError:
            raise AssertionError('stdout is not one JSON object: %r %r' % (stdout, stderr))
        return process.returncode, data

    def run_cli(self, *args, env=None, cwd=None, script=CLI):
        return self.finish(self.spawn(*args, env=env, cwd=cwd, script=script))

    def install(self, *extra, script=CLI, env=None):
        code, data = self.run_cli('install', '--platform', 'claude-code', *extra, script=script, env=env)
        self.assertEqual(code, 0, data)
        return data

    def git(self, cwd, *args):
        result = subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.com',
                                 '-c', 'commit.gpgsign=false', *args], cwd=str(cwd), env=self.env,
                                capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    # -- tests ------------------------------------------------------------------------------

    def test_concurrent_sessions_no_lost_updates(self):
        self.install('--default', 'on')
        # Phase 1: ten processes, each owning a distinct chat id.
        processes = []
        for index in range(10):
            word = 'off' if index % 2 == 0 else 'on'
            processes.append(self.spawn('command', '/coding-team ' + word, '--platform', 'claude-code',
                                        env={'CLAUDE_CODE_SESSION_ID': 's%d' % index}))
        for process in processes:
            code, data = self.finish(process)
            self.assertEqual(code, 0, data)
        # Phase 2, strictly after phase 1: a single global switch.
        code, data = self.run_cli('command', '/coding-team off --global', '--platform', 'claude-code',
                                  env={'CLAUDE_CODE_SESSION_ID': 's0'})
        self.assertEqual(code, 0, data)
        stored = json.loads(self.state.read_text(encoding='utf-8'))
        self.assertEqual(stored['chat_policies'],
                         {'s%d' % i: 'OFF' if i % 2 == 0 else 'ON' for i in range(10)})
        self.assertEqual(stored['global_policy'], 'OFF')

    def test_concurrent_claims_single_winner(self):
        workspace = self.root / 'workspace'
        workspace.mkdir()
        processes = {}
        for index in range(5):
            session = 'session-%d' % index
            processes[session] = self.spawn('orchestrator', 'claim', '--task-id', 't1', '--mode', 'AUTO',
                                            '--phase', 'p1', '--session-id', session, cwd=workspace)
        results = {session: self.finish(process) for session, process in processes.items()}
        winners = [session for session, (code, _) in results.items() if code == 0]
        losers = [session for session, (code, _) in results.items() if code == 2]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(len(losers), 4, results)
        code, data = self.run_cli('orchestrator', 'status', '--task-id', 't1', cwd=workspace)
        self.assertEqual(data['tasks']['t1']['owner_session_id'], winners[0])

    def test_orchestration_across_worktrees_via_cli(self):
        if shutil.which('git') is None:
            self.skipTest('git is not installed')
        main = self.root / 'repo'
        main.mkdir()
        self.git(main, 'init', '-q')
        (main / 'file.txt').write_text('x\n', encoding='utf-8')
        self.git(main, 'add', 'file.txt')
        self.git(main, 'commit', '-q', '-m', 'init')
        wt_a, wt_b = self.root / 'wt-a', self.root / 'wt-b'
        self.git(main, 'worktree', 'add', '-q', '-b', 'branch-a', str(wt_a))
        self.git(main, 'worktree', 'add', '-q', '-b', 'branch-b', str(wt_b))
        code, data = self.run_cli('orchestrator', 'claim', '--task-id', 'shared', '--mode', 'AUTO',
                                  '--phase', 'p1', '--session-id', 'A', cwd=wt_a)
        self.assertEqual(code, 0, data)
        code, data = self.run_cli('orchestrator', 'status', '--task-id', 'shared', cwd=wt_b)
        self.assertEqual(data['tasks']['shared']['owner_session_id'], 'A', data)
        self.assertFalse(data['can_claim'])
        code, data = self.run_cli('orchestrator', 'claim', '--task-id', 'shared', '--mode', 'AUTO',
                                  '--phase', 'p1', '--session-id', 'B', cwd=wt_b)
        self.assertEqual(code, 2, data)
        self.assertTrue(data['blockers'])
        # The record lives once, in the main worktree, not in either linked worktree.
        self.assertTrue((main / '.coding-team' / 'orchestration.json').is_file())
        self.assertFalse((wt_a / '.coding-team').exists())
        self.assertFalse((wt_b / '.coding-team').exists())

    def test_corrupt_files_fail_closed(self):
        self.install('--default', 'on')
        session = {'CLAUDE_CODE_SESSION_ID': 's1'}
        # Corrupt policy state: status exits 2 and the bytes stay untouched.
        corrupt = b'{not json'
        self.state.write_bytes(corrupt)
        code, data = self.run_cli('status', '--platform', 'claude-code', env=session)
        self.assertEqual(code, 2, data)
        self.assertEqual(self.state.read_bytes(), corrupt)
        # Corrupt orchestration state.
        workspace = self.root / 'workspace'
        (workspace / '.coding-team').mkdir(parents=True)
        orchestration = workspace / '.coding-team' / 'orchestration.json'
        broken = b'{"schema_version": 1, "tasks": ['
        orchestration.write_bytes(broken)
        code, data = self.run_cli('orchestrator', 'status', cwd=workspace)
        self.assertEqual(code, 2, data)
        self.assertEqual(orchestration.read_bytes(), broken)
        # Corrupt install manifest: doctor reports it as unhealthy.
        self.state.write_bytes(b'{"schema_version": 1, "global_policy": "ON", "chat_policies": {}}\n')
        manifest = self.skill / 'install-manifest.json'
        self.assertTrue(manifest.is_file())
        manifest.write_bytes(b'{broken manifest')
        code, data = self.run_cli('doctor', '--platform', 'claude-code')
        self.assertEqual(code, 1, data)
        self.assertEqual(data['manifest'], 'invalid')

    def test_install_from_extracted_runtime_package(self):
        builder = load_builder()
        archive_path = self.root / 'runtime.zip'
        builder.build_package(ROOT, archive_path, platform='claude-code')
        extracted = self.root / 'extracted'
        extracted.mkdir()
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(extracted)
        package = extracted / 'coding-team'
        self.assertEqual(sorted(p.name for p in (package / 'platforms').iterdir()), ['claude-code.json'])
        self.assertTrue((package / 'agents' / 'roles' / 'roles.json').is_file())
        script = package / 'scripts' / 'coding_team.py'
        self.assertTrue(script.is_file())
        self.install(script=script)
        self.assertTrue((self.skill / 'SKILL.md').is_file())
        code, data = self.run_cli('doctor', '--platform', 'claude-code')
        self.assertEqual(code, 0, data)
        self.assertTrue(data['healthy'], data)
        # The installed copy resolves its platform from its own manifest.
        code, data = self.run_cli('doctor', script=self.skill / 'scripts' / 'coding_team.py')
        self.assertEqual(code, 0, data)
        self.assertTrue(data['healthy'], data)


if __name__ == '__main__':
    unittest.main()
