import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'scripts' / 'coding_team.py'
STRIP_PREFIXES = ('CODEX_', 'ANTIGRAVITY_')
STRIP_NAMES = {'CLAUDECODE', 'CLAUDE_CONFIG_DIR', 'CLAUDE_CODE_SESSION_ID',
               'CLAUDE_CODE_SUBAGENT_MODEL_FORCE', 'CLAUDE_CODE_ENTRYPOINT'}

BASE_FACTS = {
    'level': 'L2', 'user_choice': None, 'safety_forbids_user_choice': False,
    'architecture_dispute': False, 'multi_contract_change': False, 'has_plan': False,
    'sequential_review_sufficient': False, 'superpowers_available': True, 'independent_issues': 0,
}


def handoff_record(**overrides):
    record = {
        'task_id': 'task-1', 'reason': 'SDD stopped: blocked on design question',
        'from': 'superpowers-sdd', 'to': 'coding-team',
        'workers': [{'id': 'worker-1', 'terminated': True, 'evidence': 'runtime reports task stopped'}],
        'snapshot': {'head': 'abc1234', 'uncommitted': [], 'untracked': []},
        'tests': {'commands': ['python -m unittest'], 'result': 'pass', 'snapshot': 'abc1234'},
        'findings': [], 'previous_ledger': None, 'acknowledged_sdd': [],
    }
    record.update(overrides)
    return record


class CliTests(unittest.TestCase):
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
        self.agents = self.config_root / 'agents'
        self.state = self.config_root / 'coding-team' / 'state.json'

    def run_cli(self, *args, env=None, cwd=None, script=CLI):
        run_env = dict(self.env, **(env or {}))
        result = subprocess.run([sys.executable, str(script), *map(str, args)], env=run_env,
                                cwd=str(cwd or self.root), capture_output=True, text=True,
                                encoding='utf-8', timeout=120)
        self.assertEqual(result.stdout.count('\n'), 1, result.stdout + result.stderr)
        try:
            data = json.loads(result.stdout)
        except ValueError:
            self.fail('stdout is not one JSON object: %r %r' % (result.stdout, result.stderr))
        self.assertIsInstance(data, dict)
        return result.returncode, data, result.stdout

    def install(self, *extra):
        code, data, _ = self.run_cli('install', '--platform', 'claude-code', *extra)
        self.assertEqual(code, 0, data)
        return data

    def test_install_status_command_roundtrip(self):
        data = self.install('--default', 'on')
        self.assertEqual(data['state']['global_policy'], 'ON')
        session = {'CLAUDE_CODE_SESSION_ID': 's1'}
        code, data, _ = self.run_cli('status', '--platform', 'claude-code', env=session)
        self.assertEqual(code, 0, data)
        self.assertEqual((data['chat_id'], data['effective_policy']), ('s1', 'ON'))
        code, data, _ = self.run_cli('command', '/coding-team off', '--platform', 'claude-code', env=session)
        self.assertEqual(code, 0, data)
        self.assertEqual(data['effective_policy'], 'OFF')
        code, data, _ = self.run_cli('command', '/coding-team on all', '--platform', 'claude-code', env=session)
        self.assertEqual(code, 2, data)
        self.assertEqual(data['hint'], 'use /coding-team on --global')
        # --chat-id wins over the runtime variable; --state selects another file.
        code, data, _ = self.run_cli('status', '--platform', 'claude-code', '--chat-id', 'other',
                                     '--state', self.state, env=session)
        self.assertEqual((code, data['chat_id'], data['effective_policy']), (0, 'other', 'ON'))

    def test_ambiguous_platform_detection(self):
        both = {'CLAUDECODE': '1', 'CODEX_THREAD_ID': 'thread-1'}
        for args in (('status',), ('doctor',), ('check-config',)):
            with self.subTest(args=args):
                code, data, _ = self.run_cli(*args, env=both)
                self.assertEqual(code, 2, data)
                self.assertIn('--platform', data['error'])
        code, data, _ = self.run_cli('check-config', env={'CLAUDECODE': '1'})
        self.assertEqual((code, data['platform']), (0, 'claude-code'))

    def test_installed_copy_infers_platform_and_doctor_runs(self):
        self.install()
        installed = self.skill / 'scripts' / 'coding_team.py'
        self.assertTrue(installed.is_file())
        code, data, _ = self.run_cli('status', script=installed)
        self.assertEqual(code, 0, data)
        self.assertEqual(data['platform'], 'claude-code')
        self.assertEqual(data['chat_policy'], 'UNKNOWN')
        code, data, _ = self.run_cli('doctor', script=installed)
        self.assertEqual(code, 0, data)
        self.assertEqual(data['platform'], 'claude-code')
        self.assertTrue(data['healthy'])
        self.assertFalse((self.skill / 'scripts' / '__pycache__').exists())
        code, data, _ = self.run_cli('uninstall', '--platform', 'claude-code', script=installed)
        self.assertEqual(code, 0, data)
        self.assertFalse(self.skill.exists())

    def test_chat_unknown_status_and_global_still_works(self):
        self.install()
        code, data, _ = self.run_cli('status', '--platform', 'claude-code')
        self.assertEqual(code, 0, data)
        self.assertEqual((data['chat_id'], data['chat_policy'], data['effective_policy']),
                         (None, 'UNKNOWN', None))
        code, data, _ = self.run_cli('command', '/coding-team off --global', '--platform', 'claude-code')
        self.assertEqual((code, data['global_policy'], data['chat_policy']), (0, 'OFF', 'UNKNOWN'))
        before = self.state.read_bytes()
        code, data, _ = self.run_cli('command', '/coding-team on', '--platform', 'claude-code')
        self.assertEqual(code, 2, data)
        self.assertIn('chat id', data['error'])
        self.assertEqual(self.state.read_bytes(), before)
        # A blank runtime variable is not a chat id.
        code, data, _ = self.run_cli('status', '--platform', 'claude-code', env={'CLAUDE_CODE_SESSION_ID': ' '})
        self.assertEqual((code, data['chat_policy']), (0, 'UNKNOWN'))

    def test_doctor_reports_guarantees_force_env_and_journal(self):
        self.install()
        code, data, _ = self.run_cli('doctor', '--platform', 'claude-code')
        self.assertEqual(code, 0, data)
        self.assertTrue(data['healthy'])
        self.assertEqual(data['manifest'], 'ok')
        self.assertEqual(data['model_availability'], 'unverified')
        self.assertEqual((data['modified_files'], data['missing_files']), ([], []))
        self.assertFalse(data['journal_present'])
        self.assertEqual(data['force_model_env_active'], [])
        self.assertEqual(data['state'], 'ok')
        architect = data['agents']['ct-architect']
        self.assertEqual((architect['present'], architect['requested_tier'], architect['model_in_file']),
                         (True, 'strong', 'opus'))
        self.assertEqual(architect['guarantees']['no_file_write'], 'ENFORCED')
        self.assertEqual(data['agents']['ct-developer']['guarantees']['no_delegate'], 'ADVISORY')
        self.assertIn('ct-reviewer-verify', data['agents'])
        self.assertEqual(data['superpowers'], {'installed': 'not_found', 'enabled': None,
                                               'available_in_session': 'determined by Leader at runtime'})

        code, data, _ = self.run_cli('doctor', '--platform', 'claude-code',
                                     env={'CLAUDE_CODE_SUBAGENT_MODEL_FORCE': 'haiku'})
        self.assertEqual(data['force_model_env_active'], ['CLAUDE_CODE_SUBAGENT_MODEL_FORCE'])

        skill = self.config_root / 'plugins' / 'cache' / 'sp' / 'skills' / 'subagent-driven-development'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text('# sdd\n', encoding='utf-8')
        (self.config_root / 'settings.json').write_text(
            json.dumps({'enabledPlugins': {'superpowers@marketplace': False}}), encoding='utf-8')
        code, data, _ = self.run_cli('doctor', '--platform', 'claude-code')
        self.assertEqual((data['superpowers']['installed'], data['superpowers']['enabled']), ('found', False))

        agent = self.agents / 'ct-security.md'
        agent.unlink()
        code, data, _ = self.run_cli('doctor', '--platform', 'claude-code')
        self.assertEqual(code, 1, data)
        self.assertFalse(data['healthy'])
        self.assertFalse(data['agents']['ct-security']['present'])
        self.assertIn(agent.as_posix(), data['missing_files'])

        self.install('--overwrite-modified')
        journal = self.state.parent / 'install-journal.json'
        journal.write_text('{}', encoding='utf-8')
        code, data, _ = self.run_cli('doctor', '--platform', 'claude-code')
        self.assertEqual(code, 1, data)
        self.assertTrue(data['journal_present'])
        self.assertFalse(data['healthy'])

    def test_install_conflict_exit_codes(self):
        self.install()
        agent = self.agents / 'ct-architect.md'
        agent.write_bytes(agent.read_bytes() + b'\nuser edit\n')
        edited = agent.read_bytes()
        code, data, _ = self.run_cli('install', '--platform', 'claude-code')
        self.assertEqual(code, 2, data)
        self.assertEqual(data['error_type'], 'ModifiedTargetError')
        self.assertIn(agent.as_posix(), data['paths'])
        self.assertEqual(agent.read_bytes(), edited)
        code, data, _ = self.run_cli('install', '--platform', 'claude-code', '--dry-run')
        self.assertEqual(code, 0, data)
        self.assertTrue(data['dry_run'])
        self.assertIn(agent.as_posix(), data['conflicts']['modified'])
        self.assertEqual(agent.read_bytes(), edited)
        code, data, _ = self.run_cli('install', '--platform', 'claude-code', '--overwrite-modified')
        self.assertEqual(code, 0, data)
        self.assertIn(agent.as_posix(), data['modified_overwritten'])
        self.assertNotEqual(agent.read_bytes(), edited)
        code, data, _ = self.run_cli('install', '--platform', 'claude-code', '--recover')
        self.assertEqual((code, data['journal_removed']), (0, False))
        code, data, _ = self.run_cli('install', '--platform', 'claude-code', '--scope', 'project',
                                     '--project-dir', self.root, '--default', 'on')
        self.assertEqual(code, 2, data)
        code, data, _ = self.run_cli('install', '--platform', 'nope')
        self.assertEqual(code, 2, data)
        self.assertIn('error', data)

    def test_status_and_command_fail_closed_on_missing_or_corrupt_state(self):
        for args in (('status',), ('command', '/coding-team off --global'), ('command', '/coding-team status')):
            with self.subTest(state='missing', args=args):
                code, data, _ = self.run_cli(*args, '--platform', 'claude-code')
                self.assertEqual(code, 2, data)
                self.assertIs(data['automatic_use_allowed'], False)
                self.assertIn('error', data)
                self.assertFalse(self.state.exists())
        self.state.parent.mkdir(parents=True, exist_ok=True)  # a lock side-file may exist
        for corrupt in (b'{not json', b'{"schema_version": 1, "global_policy": "MAYBE", "chat_policies": {}}'):
            self.state.write_bytes(corrupt)
            for args in (('status',), ('command', '/coding-team off --global'),
                         ('command', '/coding-team on', '--chat-id', 'c1')):
                with self.subTest(state=corrupt, args=args):
                    code, data, _ = self.run_cli(*args, '--platform', 'claude-code')
                    self.assertEqual(code, 2, data)
                    self.assertIs(data['automatic_use_allowed'], False)
                    self.assertEqual(self.state.read_bytes(), corrupt)
        # Argument errors of status/command fail closed too.
        code, data, _ = self.run_cli('status', '--platform', 'claude-code', '--bogus')
        self.assertEqual((code, data['automatic_use_allowed']), (2, False))

    def test_command_chat_id_option(self):
        self.install()
        code, data, _ = self.run_cli('command', '/coding-team off', '--platform', 'claude-code',
                                     '--chat-id', 'c1', env={'CLAUDE_CODE_SESSION_ID': 'runtime'})
        self.assertEqual(code, 0, data)
        self.assertEqual((data['chat_id'], data['chat_policy'], data['effective_policy']), ('c1', 'OFF', 'OFF'))
        state = json.loads(self.state.read_text(encoding='utf-8'))
        self.assertEqual(state['chat_policies'], {'c1': 'OFF'})

    def test_help_is_one_json_object(self):
        for args in (('-h',), ('--help',), ('install', '--help'), ('orchestrator', '-h'), ('status', '-h')):
            with self.subTest(args=args):
                code, data, _ = self.run_cli(*args)
                self.assertEqual(code, 0, data)
                self.assertEqual(set(data), {'help'})
                self.assertIn('usage:', data['help'])

    def test_rejected_arguments_are_not_echoed(self):
        secret = 'SECRETTOKEN0123456789'
        for args in (('orchestrator', 'claim', '--task-id', 't1', '--mode', 'AUTO', '--phase', 'p', secret),
                     ('orchestrator', secret), ('status', '--platform', 'claude-code', secret, secret),
                     ('install', '--platform', secret)):
            with self.subTest(args=args[:2]):
                code, data, stdout = self.run_cli(*args, cwd=self.root)
                self.assertEqual(code, 2, data)
                self.assertNotIn(secret, stdout)
        code, data, _ = self.run_cli('status', '--platform', 'claude-code', secret, secret)
        self.assertIn('2 unrecognized argument(s) rejected', data['error'])

    def test_recover_rejects_other_install_flags(self):
        self.install()
        journal = self.state.parent / 'install-journal.json'
        journal.write_text('{}', encoding='utf-8')
        for flags in (('--dry-run',), ('--default', 'on'), ('--global-rules',), ('--docs-mcp', 'ctx'),
                      ('--replace-unmanaged',), ('--overwrite-modified',)):
            with self.subTest(flags=flags):
                code, data, _ = self.run_cli('install', '--platform', 'claude-code', '--recover', *flags)
                self.assertEqual(code, 2, data)
                self.assertIn('cannot be combined with ' + flags[0], data['error'])
                self.assertEqual(journal.read_text(encoding='utf-8'), '{}')

    def test_check_config(self):
        code, data, _ = self.run_cli('check-config', '--platform', 'claude-code')
        self.assertEqual(code, 0, data)
        self.assertEqual((data['valid'], data['availability_verified']), (True, False))
        config = json.loads((ROOT / 'platforms' / 'codex.json').read_text(encoding='utf-8'))
        source = self.root / 'input.json'
        source.write_text(json.dumps(config), encoding='utf-8')
        before = source.read_bytes()
        code, data, _ = self.run_cli('check-config', '--input', source)
        self.assertEqual((code, data['valid'], data['platform']), (0, True, 'codex'))
        self.assertEqual(source.read_bytes(), before)
        config['roles']['ct-architect']['tier'] = 'ultra'
        source.write_text(json.dumps(config), encoding='utf-8')
        before = source.read_bytes()
        code, data, _ = self.run_cli('check-config', '--input', source)
        self.assertEqual((code, data['valid']), (2, False))
        self.assertIn('tier', data['error'])
        self.assertEqual(source.read_bytes(), before)
        missing = self.root / 'absent.json'
        listing = sorted(p.name for p in self.root.iterdir())
        code, data, _ = self.run_cli('check-config', '--input', missing)
        self.assertEqual((code, data['valid']), (2, False))
        self.assertFalse(missing.exists())
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), listing)

    def test_orchestrator_cli(self):
        ws = self.root / 'workspace'
        progress = ws / '.superpowers' / 'sdd' / 'plan-a' / 'progress.md'
        progress.parent.mkdir(parents=True)
        progress.write_text('# ledger\n', encoding='utf-8')
        sdd = progress.resolve().as_posix()

        code, data, _ = self.run_cli('orchestrator', 'status', cwd=ws)
        self.assertEqual(code, 1, data)
        self.assertFalse(data['can_claim'])
        self.assertEqual(data['unresolved_sdd'], [sdd])

        record = self.root / 'handoff.json'
        record.write_text(json.dumps(handoff_record(acknowledged_sdd=[sdd])), encoding='utf-8')
        code, data, _ = self.run_cli('orchestrator', 'handoff', '--input', record, '--workspace', ws)
        self.assertEqual(code, 2, data)  # no record: user confirmation is required
        code, data, _ = self.run_cli('orchestrator', 'handoff', '--input', record, '--user-confirmed', cwd=ws)
        self.assertEqual(code, 0, data)
        token = data['token']
        self.assertTrue(token)
        self.assertEqual((data['from_verified'], data['termination_evidence']), (False, 'caller-asserted'))

        code, data, _ = self.run_cli('orchestrator', 'claim', '--task-id', 'task-1', '--mode', 'AUTO',
                                     '--phase', 'gate-1', '--token', token, cwd=ws)
        self.assertEqual(code, 0, data)
        self.assertEqual(data['phase'], 'gate-1')
        code, data, _ = self.run_cli('orchestrator', 'claim', '--task-id', 'task-1', '--mode', 'AUTO',
                                     '--phase', 'gate-2', '--session-id', 'other', cwd=ws)
        self.assertEqual(code, 2, data)
        self.assertTrue(data['blockers'])
        code, data, _ = self.run_cli('orchestrator', 'takeover', '--task-id', 'task-1', '--reason', 'lost',
                                     '--session-id', 'other', cwd=ws)
        self.assertEqual(code, 2, data)

        facts = self.root / 'facts.json'
        facts.write_text(json.dumps(BASE_FACTS), encoding='utf-8')
        code, data, _ = self.run_cli('orchestrator', 'route', '--input', facts, cwd=ws)
        self.assertEqual((code, data['orchestrator']), (0, 'coding-team'))

        code, data, stdout = self.run_cli('orchestrator', 'status', '--task-id', 'task-1', cwd=ws)
        self.assertIn('task-1', data['tasks'])
        self.assertNotIn(token, stdout)
        self.assertNotIn('owner_token', stdout)

        code, data, _ = self.run_cli('orchestrator', 'release', '--task-id', 'task-1', '--token', token, cwd=ws)
        self.assertEqual((code, data['released']), (0, True))
        code, data, _ = self.run_cli('orchestrator', 'status', '--workspace', self.root / 'missing')
        self.assertEqual(code, 2, data)
        self.assertIn('workspace', data['error'])


if __name__ == '__main__':
    unittest.main()
