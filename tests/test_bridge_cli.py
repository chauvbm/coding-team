"""Exact chat control routing and Bridge CLI argument validation."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from dataclasses import dataclass


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))


class BridgeCliTests(unittest.TestCase):
    def test_wait_ready_uses_peer_join_scope_and_rejects_old_epoch(self):
        import ct_bridge_cli as cli
        import ct_bridge_control as control
        from ct_bridge_binding import Actor
        actor = Actor('codex', 'trusted-runtime')
        ctx = SimpleNamespace(require_leader=lambda: actor)
        args = SimpleNamespace(operation='wait-ready', platform='codex', workspace=SCRIPTS.parent,
                               task_id=None, attempt=None, payload_stdin=True, wait_seconds=0)
        payload = {'leader_epoch': 3, 'platform': 'antigravity', 'join_id': 'join-1',
                   'desktop_epoch': 2, 'auto_epoch': 0}
        def wait(ctx_arg, expected, *, platform, join_id, desktop_epoch, auto_epoch, wait_seconds):
            self.assertIs(ctx_arg, ctx)
            self.assertEqual(expected.actor, actor)
            self.assertEqual(expected.epoch, 3)
            self.assertEqual((platform, join_id, desktop_epoch, auto_epoch, wait_seconds),
                             ('antigravity', 'join-1', 2, 0, 0))
            return {'ok': True, 'state': 'WAITING', 'reason': 'NOT_PAIRED'}
        with patch.object(cli, '_context', return_value=ctx), patch.object(control, 'wait_ready', wait):
            view, code = cli.bridge_command(args, SCRIPTS.parent, {}, Path.home(),
                                           io.BytesIO(json.dumps(payload).encode()))
            self.assertEqual(code, 0, view)
            for bad in (payload | {'pair_epoch': 1}, payload | {'desktop_epoch': True},
                        payload | {'join_id': None}, payload | {'platform': 'codex'}):
                view, code = cli.bridge_command(args, SCRIPTS.parent, {}, Path.home(),
                                               io.BytesIO(json.dumps(bad).encode()))
                self.assertEqual(code, 2, view)
                self.assertEqual(view['reason'], 'INVALID_PAYLOAD')

    def test_collect_without_native_slot_is_key_scoped(self):
        import ct_bridge_cli as cli
        import ct_bridge_attempts as attempts
        seen = []
        ctx = SimpleNamespace(own_binding=lambda: None)
        def collect(ctx_arg, key, grant, *, wait_seconds):
            seen.append((key.task_id, key.attempt_no, grant, wait_seconds))
            return {'ok': True, 'state': 'RUNNING', 'reason': None, 'task_id': key.task_id}
        args = SimpleNamespace(operation='collect', platform='codex', workspace=SCRIPTS.parent,
                               task_id='task-A', attempt=2, payload_stdin=True, wait_seconds=0)
        with patch.object(cli, '_context', return_value=ctx), patch.object(attempts, 'collect', collect):
            view, code = cli.bridge_command(args, SCRIPTS.parent, {}, Path.home(), io.BytesIO(b'{}'))
        self.assertEqual(code, 0, view)
        self.assertEqual(view['task_id'], 'task-A')
        self.assertEqual(seen, [('task-A', 2, None, 0)])

    def test_manual_prompt_binds_which_tab_to_task(self):
        import ct_bridge_cli as cli
        from types import SimpleNamespace
        ctx = SimpleNamespace(paths=SimpleNamespace(root=Path('C:/fixture'), data=Path('C:/fixture/.coding-team/bridge')))
        args = SimpleNamespace(operation='publish', platform='codex', workspace=SCRIPTS.parent,
                               task_id=None, attempt=None, payload_stdin=True, wait_seconds=0)
        result = {'ok': True, 'state': 'WAITING_FOR_USER', 'reason': None, 'task_id': 'task-A',
                  'attempt_no': 2, 'worker_id': 'worker-A', 'worker_label': 'Worker A'}
        with patch.object(cli, '_context', return_value=ctx), patch.object(cli, '_mutate', return_value=result):
            view, code = cli.bridge_command(args, SCRIPTS.parent, {}, Path.home(), io.BytesIO(b'{}'))
        self.assertEqual(code, 0, view)
        self.assertIn('worker-A', view['prompt'])
        self.assertIn('Worker A', view['prompt'])
        self.assertIn('task-A', view['prompt'])
        self.assertIn('attempt 2', view['prompt'])

    def test_structured_publication_preserves_assignment_without_derived_authority(self):
        import ct_bridge_cli as cli

        @dataclass
        class Request:
            publish_id: str
            mode: str
            worker_id: str
            assignment: dict
            baseline: dict
            content: bytes
            task_id: str | None = None
            replaces_participant_id: str | None = None
            transfer_evidence: dict | None = None

        raw = {'publish_id': 'pub', 'mode': 'manual', 'worker_id': 'worker-key',
               'assignment': {'variant': 'ct-reviewer', 'work_item_id': 'review'},
               'baseline': {}, 'content': 'exact\nTiếng Việt\n'}
        request = cli._request(raw, Request)
        self.assertEqual(request.content, 'exact\nTiếng Việt\n'.encode())
        self.assertEqual(request.assignment, {'variant': 'ct-reviewer', 'work_item_id': 'review'})
        for field in ('actor', 'role', 'chat_id', 'leader_epoch', 'kind', 'scope', 'verified_model'):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'INVALID_REQUEST'):
                cli._request(raw | {field: 'SECRET'}, Request)

    def test_v2_internal_operations_are_registered(self):
        import coding_team
        for operation in ('reserve-leader', 'finish-leader', 'complete-work-item', 'ledger-evidence',
                          'rollback-store'):
            with self.subTest(operation=operation):
                args = coding_team._parser().parse_args([
                    'bridge', operation, '--platform', 'codex', '--workspace', str(SCRIPTS.parent),
                    '--payload-stdin'])
                self.assertEqual(args.operation, operation)

    def test_worker_payload_cannot_supply_identity_role_or_epoch(self):
        import ct_bridge_worker as worker
        for operation, minimal in (('begin', {}), ('resume', {'execution_ticket': 'token'}),
                                   ('finish', {'execution_ticket': 'token', 'state': 'COMPLETED', 'result': ''})):
            args = SimpleNamespace(operation=operation, payload_file=None)
            for field in ('actor', 'worker_id', 'chat_id', 'role', 'leader_epoch', 'verified_model'):
                with self.subTest(operation=operation, field=field), self.assertRaises(ValueError):
                    worker._payload(args, None, io.BytesIO(json.dumps(minimal | {field: 'SECRET'}).encode()))

    def test_mutation_payload_rejects_unused_attempt_selectors_and_bad_epoch(self):
        import ct_bridge_cli as cli
        from ct_bridge_binding import Actor
        ctx = SimpleNamespace(require_leader=lambda: Actor('codex', 'runtime-chat'))
        for operation, payload in (
                ('open-round', {}), ('close-round', {'round_id': 'round', 'evidence': 'done'}),
                ('rollback-store', {}),
                ('ledger-evidence', {'round_id': 'round', 'participant_id': 'participant',
                                     'review_scope': [], 'snapshot': {}})):
            for changes in ({'task_id': 'task'}, {'attempt': 1}):
                args = SimpleNamespace(operation=operation, task_id=None, attempt=None, wait_seconds=0)
                args.__dict__.update(changes)
                with self.subTest(operation=operation, changes=changes), self.assertRaisesRegex(ValueError, 'INVALID_PAYLOAD'):
                    cli._mutate(ctx, args, payload | {'leader_epoch': 1})

    def test_strict_json_rejects_duplicates_constants_invalid_utf8_and_limit(self):
        import ct_bridge_cli as cli
        for raw in (b'{"leader_epoch":1,"leader_epoch":2}', b'{"x":NaN}', b'{"x":Infinity}',
                    b'\xff', b'[]', b' ' * (cli._PAYLOAD_LIMIT + 1)):
            with self.subTest(prefix=raw[:20]), self.assertRaisesRegex(ValueError, 'INVALID_PAYLOAD'):
                cli._payload(io.BytesIO(raw), True)
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
        self.fixture = BindingTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
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
        payload = {name: activated[name] for name in ('leader_epoch', 'platform', 'join_id', 'desktop_epoch', 'auto_epoch')}
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
        stale, code = invoke(payload | {'auto_epoch': payload['auto_epoch'] + 1})
        self.assertEqual(code, 1, stale)
        self.assertEqual(stale['reason'], 'STALE_ACTIVATION')
        for changes in ({'task_id': 'task'}, {'attempt': 1}, {'wait_seconds': -1},
                        {'wait_seconds': 61}, {'payload_stdin': False}):
            view, code = invoke(payload, **changes)
            self.assertEqual(code, 2, view)
            self.assertEqual(view['reason'], 'INVALID_PAYLOAD')
        for value in (payload | {'leader_epoch': True}, payload | {'auto_epoch': -1},
                      payload | {'secret': 'SECRET'}, {k: v for k, v in payload.items() if k != 'join_id'}):
            view, code = invoke(value)
            self.assertEqual(code, 2, view)
            self.assertEqual(view['reason'], 'INVALID_PAYLOAD')
            self.assertNotIn('SECRET', json.dumps(view))


if __name__ == '__main__':
    unittest.main()
