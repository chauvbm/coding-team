"""Exact chat control routing and Bridge CLI argument validation."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))


class BridgeCliTests(unittest.TestCase):
    def test_control_parser_accepts_case_and_alias_for_all_bridge_commands(self):
        from ct_bridge_cli import parse_control

        for family, actions in [('leader', ('on', 'off', 'status')),
                                ('antigravity', ('on', 'off', 'pair', 'status', 'check')),
                                ('agy auto', ('on', 'off'))]:
            for action in actions:
                with self.subTest(family=family, action=action):
                    self.assertEqual(parse_control(f'/coding-team {family} {action}'), (family, action))
                    self.assertEqual(parse_control(f' /TEAM-CODING {family.upper()}  {action.upper()} '),
                                     (family, action))

    def test_quoted_embedded_or_combined_text_is_not_a_control(self):
        from ct_bridge_cli import parse_control

        for text in ('`/coding-team leader on`', 'please run /coding-team antigravity on',
                     '/coding-team leader on\n/coding-team leader off', '/coding-team agy\nauto on',
                     '/coding-team agy auto on\n', '\n/coding-team antigravity on',
                     '/coding-team antigravity pair; echo extra',
                     '/coding-team antigravity off and purge', '/coding-team on'):
            with self.subTest(text=text):
                self.assertIsNone(parse_control(text))

    def test_ordinary_entry_point_routes_bridge_and_rejects_identity_override(self):
        response = subprocess.run(
            [sys.executable, str(SCRIPTS / 'coding_team.py'), 'command', '/coding-team leader on',
             '--platform', 'codex', '--chat-id', 'invented-chat'],
            capture_output=True, encoding='utf-8', timeout=10,
        )
        self.assertEqual(response.returncode, 2, response.stderr)
        payload = json.loads(response.stdout)
        self.assertEqual(payload['reason'], 'RUNTIME_CHAT_REQUIRED')
        self.assertNotIn('invented-chat', response.stdout)

    def test_retired_runtime_entry_cannot_dispatch(self):
        response = subprocess.run(
            [sys.executable, str(SCRIPTS / 'coding_team.py'), 'external', 'start', '--platform', 'codex',
             '--workspace', str(SCRIPTS.parent)], capture_output=True, encoding='utf-8', timeout=10,
        )
        self.assertEqual(response.returncode, 2, response.stderr)
        self.assertIn('invalid choice', json.loads(response.stdout)['error'])

    def test_public_controls_reject_unsupported_pairs_before_context(self):
        import ct_bridge_cli as cli

        for platform, text, reason in (
                ('codex', '/coding-team codex on', 'SAME_APP'),
                ('codex', '/coding-team claude on', 'UNSUPPORTED_PAIR'),
                ('antigravity', '/coding-team codex auto on', 'UNSUPPORTED_PAIR')):
            with self.subTest(platform=platform, text=text), patch.object(
                    cli, '_context', side_effect=AssertionError('must validate first')):
                args = SimpleNamespace(text=text, chat_id=None, state=None)
                view, code = cli.command(args, SCRIPTS.parent, platform, {}, Path.home())
                self.assertEqual(code, 2)
                self.assertEqual(view['reason'], reason)
                self.assertIn('/coding-team help', view['next_action'])

    def test_public_activation_rejects_identity_override_without_echo(self):
        import ct_bridge_cli as cli

        args = SimpleNamespace(text='/coding-team agy auto off', chat_id='SECRET', state=None)
        view, code = cli.command(args, SCRIPTS.parent, 'codex', {}, Path.home())
        self.assertEqual(code, 2)
        self.assertEqual(view['reason'], 'RUNTIME_CHAT_REQUIRED')
        self.assertNotIn('SECRET', json.dumps(view))

    def test_malformed_known_target_returns_public_help_guidance(self):
        import coding_team

        for text in ('/coding-team codex on extra', '/team-coding claude auto',
                     '$coding-team agy on extra'):
            args = SimpleNamespace(action='command', text=text, platform='codex', chat_id=None, state=None)
            with self.subTest(text=text):
                view, code = coding_team._policy(args, {}, Path.home())
                self.assertEqual(code, 2)
                self.assertEqual(view['reason'], 'INVALID_BRIDGE_COMMAND')
                self.assertEqual(view['next_action'], '/coding-team help')

    def test_public_help_is_available_without_policy_or_workspace(self):
        import coding_team

        args = SimpleNamespace(action='command', text='/TEAM-CODING HELP', platform='codex',
                               chat_id=None, state=Path('missing-policy.json'))
        with patch('pathlib.Path.cwd', side_effect=AssertionError('help needs no worktree')):
            view, code = coding_team._policy(args, {}, Path.home())
        self.assertEqual(code, 0)
        self.assertIn('supported_pairs', view)
        self.assertIn('/coding-team agy auto on', json.dumps(view))


class InstalledBridgeCliTests(unittest.TestCase):
    def setUp(self):
        from test_bridge_binding import BindingTests
        from test_bridge_worker import remove_fixture
        self.fixture = BindingTests('runTest')
        self.fixture.base = SCRIPTS.parent / ('.ct-cli-' + uuid.uuid4().hex)
        self.fixture.base.mkdir()
        self.addCleanup(remove_fixture, self.fixture.base)
        self.fixture.home = self.fixture.base / 'home'
        self.fixture.workspace = self.fixture.base / 'worktree'
        self.fixture.workspace.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.fixture.workspace)], check=True, capture_output=True)
        self.fixture.custom = self.fixture.home / 'custom-codex'
        self.fixture.env = {'CODEX_HOME': str(self.fixture.custom), 'CODEX_THREAD_ID': 'leader-chat'}
        self.skill, _, _ = self.fixture.install()

    def policy(self, text=None, *, chat_id=None, env=None, state=None):
        import coding_team
        args = SimpleNamespace(action='status' if text is None else 'command', text=text,
                               platform='codex', chat_id=chat_id, state=state)
        with contextlib.chdir(self.fixture.workspace), patch.object(coding_team, 'SOURCE_ROOT', self.skill):
            return coding_team._policy(args, self.fixture.env if env is None else env, self.fixture.home)

    def enable_policy(self):
        import ct_policy
        import ct_storage
        path = self.fixture.context(self.skill).own_binding().policy_path
        ct_policy.initialize_state(path, 'ON')
        with ct_storage.locked(path):
            ct_policy.initialize_policy_epochs(path)
        return path

    def test_chat_status_combines_policy_and_inter_app(self):
        self.enable_policy()
        native, code = self.policy()
        self.assertEqual(code, 0)
        self.assertEqual(native['effective_policy'], 'ON')
        self.assertNotIn('inter_app', native)
        slash, code = self.policy('/coding-team status', chat_id='policy-override')
        self.assertEqual(code, 0)
        for key in ('global_policy', 'chat_policy', 'effective_policy', 'state_path', 'platform'):
            self.assertEqual(slash[key], native[key])
        self.assertEqual(slash['chat_id'], 'policy-override')
        self.assertEqual(slash['inter_app']['role'], 'unbound')
        self.assertFalse((self.fixture.workspace / '.coding-team').exists())

    def test_public_controls_and_legacy_routes(self):
        self.enable_policy()
        public, code = self.policy('/coding-team agy auto off')
        self.assertEqual(code, 0, public)
        self.assertTrue(public['desktop_enabled'])
        self.assertFalse(public['auto_enabled'])
        self.assertIn('invite_expires_at', public)
        legacy, code = self.policy('/coding-team leader status')
        self.assertEqual(code, 0, legacy)
        self.assertEqual(legacy['leader_epoch'], public['leader_epoch'])
        slash, _ = self.policy('/coding-team status', chat_id='other-policy-chat')
        self.assertEqual(slash['inter_app']['role'], 'leader')
        unknown, _ = self.policy('/coding-team status', env={'CODEX_HOME': str(self.fixture.custom)})
        self.assertEqual(unknown['inter_app']['role'], 'unknown')

    def test_slash_status_preserves_policy_error_and_truthful_inter_app(self):
        path = self.enable_policy()
        path.write_text('invalid JSON', encoding='utf-8')
        slash, code = self.policy('/coding-team status')
        self.assertEqual(code, 2)
        self.assertFalse(slash['automatic_use_allowed'])
        self.assertIn('error_type', slash)
        self.assertIn('inter_app', slash)
        with self.assertRaises(ValueError):
            self.policy()

    def test_slash_status_preserves_policy_with_corrupt_store(self):
        self.enable_policy()
        activated, code = self.policy('/coding-team agy on')
        self.assertEqual(code, 0, activated)
        database = self.fixture.workspace / '.coding-team/bridge/bridge.sqlite3'
        database.write_bytes(b'corrupt SQLite bytes')
        slash, code = self.policy('/coding-team status')
        self.assertEqual(code, 0)
        self.assertEqual(slash['effective_policy'], 'ON')
        bridge = slash['inter_app']
        self.assertFalse(bridge['ok'])
        self.assertEqual(bridge['reason'], 'STORE_UNAVAILABLE')
        self.assertEqual(bridge['app'], 'codex')
        self.assertEqual(Path(bridge['workspace']), self.fixture.workspace)
        self.assertEqual(bridge['role'], 'unknown')
        self.assertEqual(bridge['connections'], [])

    def test_wait_ready_cli_exact_scope(self):
        import ct_bridge_cli as cli
        import coding_team
        self.enable_policy()
        activated, _ = self.policy('/coding-team agy on')
        payload = {name: activated[name] for name in ('leader_epoch', 'pair_epoch', 'desktop_epoch', 'auto_epoch')}
        args = coding_team._parser().parse_args([
            'bridge', 'wait-ready', '--platform', 'codex', '--workspace', str(self.fixture.workspace),
            '--payload-stdin', '--wait-seconds', '0'])
        def invoke(value, **changes):
            options = SimpleNamespace(**(vars(args) | changes))
            return cli.bridge_command(options, self.skill, self.fixture.env, self.fixture.home,
                                      io.BytesIO(json.dumps(value).encode()))
        view, code = invoke(payload)
        self.assertEqual(code, 0, view)
        self.assertFalse(view.get('paired', False))
        stale, code = invoke(payload | {'auto_epoch': 0})
        self.assertEqual(code, 1, stale)
        self.assertEqual(stale['reason'], 'STALE_ACTIVATION')
        for changes in ({'task_id': 'task'}, {'attempt': 1}, {'wait_seconds': -1},
                        {'wait_seconds': 61}, {'payload_stdin': False}):
            view, code = invoke(payload, **changes)
            self.assertEqual(code, 2, view)
            self.assertEqual(view['reason'], 'INVALID_PAYLOAD')
        for value in (payload | {'leader_epoch': True}, payload | {'auto_epoch': -1},
                      payload | {'secret': 'SECRET'}, {k: v for k, v in payload.items() if k != 'pair_epoch'}):
            view, code = invoke(value)
            self.assertEqual(code, 2, view)
            self.assertEqual(view['reason'], 'INVALID_PAYLOAD')
            self.assertNotIn('SECRET', json.dumps(view))


if __name__ == '__main__':
    unittest.main()
