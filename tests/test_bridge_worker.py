"""Worker entry behavior against isolated installations, Git and bridge state."""
import io
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
try:
    import ct_bridge_worker as worker
except ImportError:
    worker = None


def remove_fixture(path):
    """Allow Windows file scanners to release a just-created Git directory."""
    for retry in range(6):
        try:
            shutil.rmtree(path)
            return
        except OSError as exc:
            if getattr(exc, 'winerror', None) != 32 or retry == 5:
                raise
            time.sleep(.1)


class WorkerEntryTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(worker, 'worker entry implementation is missing')
        from test_bridge_binding import BindingTests
        self.base = ROOT / ('.ct-worker-' + uuid.uuid4().hex)
        self.base.mkdir()
        self.addCleanup(remove_fixture, self.base)
        fixture = BindingTests('runTest')
        fixture.base = self.base
        fixture.home = self.base / "home Tiếng Việt's"
        fixture.workspace = self.base / "work Tiếng Việt's"
        fixture.workspace.mkdir()
        subprocess.run(['git', 'init', '-q', str(fixture.workspace)], check=True, capture_output=True)
        fixture.custom = fixture.home / 'custom-codex'
        fixture.env = {'CODEX_HOME': str(fixture.custom), 'CODEX_THREAD_ID': 'leader-chat'}
        self.fixture = fixture
        self.skill, _, _ = fixture.install('worker')
        self.env = {'ANTIGRAVITY_CONVERSATION_ID': 'worker-chat'}

    def invoke(self, command, payload=b'', extra=(), env=None):
        output = io.StringIO()
        code = worker.main([command, '--workspace', str(self.fixture.workspace), *extra],
                           stdin=io.BytesIO(payload), stdout=output, env=self.env if env is None else env,
                           source_root=self.skill, home=self.fixture.home)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1, output.getvalue())
        view = json.loads(lines[0])
        self.assertIs(type(view['ok']), bool)
        self.assertIn('state', view)
        self.assertIn('reason', view)
        return code, view

    def leader(self):
        import ct_bridge_control as control
        import ct_bridge_binding as binding
        import ct_policy
        import ct_storage
        skill, _, _ = self.fixture.install()
        ctx = self.fixture.context(skill)
        policy = ctx.own_binding().policy_path
        ct_policy.initialize_state(policy, 'ON')
        with ct_storage.locked(policy):
            ct_policy.initialize_policy_epochs(policy)
        selected = control.leader_on(ctx)
        self.assertTrue(selected['ok'], selected)
        expected = binding.LeaderRef(ctx.actor, selected['leader_epoch'])
        self.assertTrue(control.set_desktop(ctx, expected, True)['ok'])
        self.leader_ctx, self.expected = ctx, expected
        return control.invite(ctx, expected)['ticket']

    def test_status_is_read_only_and_reports_missing_leader(self):
        code, view = self.invoke('status')
        self.assertEqual(code, 0)
        self.assertEqual(view['state'], 'LEADER_NOT_SELECTED')
        self.assertFalse((self.fixture.workspace / '.coding-team').exists())

    def test_worker_join_status_help(self):
        import ct_bridge_control as control
        self.leader()
        opened = control.configure_peer(self.leader_ctx, 'antigravity', 'auto_off')
        self.assertTrue(opened['ok'], opened)
        code, joined = self.invoke('join')
        self.assertEqual(code, 0, joined)
        self.assertEqual(joined['state'], 'CONNECTED')
        code, status = self.invoke('connection-status')
        self.assertEqual(code, 0, status)
        self.assertEqual(status['role'], 'worker')
        self.assertTrue(status['connections'][0]['paired'])
        self.assertFalse(status['connections'][0]['waiter_ready'])
        code, other = self.invoke('connection-status', env={'ANTIGRAVITY_CONVERSATION_ID': 'other-chat'})
        self.assertEqual(code, 0, other)
        self.assertEqual(other['role'], 'unbound')
        self.assertFalse(other['connections'][0]['paired'])
        code, unknown = self.invoke('connection-status', env={})
        self.assertEqual(code, 0, unknown)
        self.assertEqual(unknown['role'], 'unknown')
        code, help_view = self.invoke('help', env={})
        self.assertEqual(code, 0, help_view)
        self.assertIn('/coding-team listen', json.dumps(help_view))

    def test_worker_connection_status_is_read_only_without_identity(self):
        code, view = self.invoke('connection-status', env={})
        self.assertEqual(code, 0, view)
        self.assertEqual(view['role'], 'unknown')
        self.assertFalse((self.fixture.workspace / '.coding-team').exists())
        code, view = self.invoke('join', env={})
        self.assertEqual(code, 2, view)
        self.assertEqual(view['reason'], 'UNKNOWN_CHAT')
        self.assertFalse((self.fixture.workspace / '.coding-team').exists())

    def test_worker_connection_status_preserves_context_on_store_failure(self):
        self.leader()
        database = self.fixture.workspace / '.coding-team/bridge/bridge.sqlite3'
        database.write_bytes(b'corrupt SQLite bytes')
        code, view = self.invoke('connection-status')
        self.assertEqual(code, 2, view)
        self.assertEqual(view['reason'], 'STORE_UNAVAILABLE')
        self.assertEqual(view['app'], 'antigravity')
        self.assertEqual(Path(view['workspace']), self.fixture.workspace)
        self.assertEqual(view['role'], 'unknown')
        self.assertEqual(view['connections'], [])

    def test_worker_help_requires_neither_workspace_nor_installation(self):
        output = io.StringIO()
        code = worker.main(['help'], stdout=output, env={}, source_root=self.base / 'missing',
                           home=self.fixture.home)
        self.assertEqual(code, 0, output.getvalue())
        self.assertIn('supported_pairs', json.loads(output.getvalue()))
        self.assertFalse((self.fixture.workspace / '.coding-team').exists())

    def test_worker_public_operations_reject_attempt_and_payload_selectors(self):
        payload_path = self.base / 'secret.json'
        payload_path.write_text('SECRET', encoding='utf-8')
        for command in ('join', 'connection-status', 'help'):
            for extra in (('--task-id', 'task'), ('--attempt', '1'),
                          ('--payload-file', str(payload_path))):
                with self.subTest(command=command, extra=extra):
                    code, view = self.invoke(command, extra=extra)
                    self.assertEqual(code, 2)
                    self.assertEqual(view['reason'], 'INVALID_ARGUMENTS')
                    self.assertNotIn('SECRET', json.dumps(view))
        self.assertEqual(payload_path.read_text(encoding='utf-8'), 'SECRET')

    def test_stdin_connect_consumes_invite_without_echoing_ticket(self):
        ticket = self.leader()
        code, view = self.invoke('connect', json.dumps({'ticket': ticket}).encode())
        self.assertEqual(code, 0, view)
        self.assertEqual(view['state'], 'CONNECTED')
        self.assertNotIn(ticket, json.dumps(view))
        self.assertFalse(self.invoke('connect', json.dumps({'ticket': ticket}).encode())[1]['ok'])

    def test_rejects_runtime_override_and_secret_arguments_without_echo(self):
        for extra in (('--chat-id', 'secret-runtime'), ('--ticket', 'SECRET-TICKET'),
                      ('--claim', 'SECRET-CLAIM'), ('--platform', 'codex')):
            code, view = self.invoke('connect', extra=extra)
            self.assertEqual(code, 2)
            self.assertEqual(view['reason'], 'INVALID_ARGUMENTS')
            self.assertNotIn('SECRET', json.dumps(view))

    def test_unknown_runtime_identity_fails_without_initializing_bridge(self):
        code, view = self.invoke('status', env={'ANTIGRAVITY_CHAT_ID': 'forged'})
        self.assertEqual(code, 2)
        self.assertEqual(view['reason'], 'UNKNOWN_CHAT')
        self.assertFalse((self.fixture.workspace / '.coding-team').exists())

    def test_payload_rejects_duplicates_extra_fields_and_invalid_utf8(self):
        for payload in (b'{"ticket":"a","ticket":"b"}', b'{"ticket":"a","chat_id":"b"}',
                        b'[]', b'{"ticket":123}', b'\xff', b'not JSON'):
            code, view = self.invoke('connect', payload)
            self.assertEqual(code, 2)
            self.assertEqual(view['reason'], 'INVALID_PAYLOAD')

    def test_payload_read_is_bounded(self):
        code, view = self.invoke('connect', b' ' * (1024 * 1024 + 1))
        self.assertEqual(code, 2)
        self.assertEqual(view['reason'], 'PAYLOAD_TOO_LARGE')

    def test_payload_file_connect_is_one_use_and_confined(self):
        ticket = self.leader()
        data = self.fixture.workspace / '.coding-team/bridge'
        payload = data / 'inbox' / (uuid.uuid4().hex + '.json')
        payload.write_text(json.dumps({'ticket': ticket}), encoding='utf-8')
        code, view = self.invoke('connect', extra=('--payload-file', str(payload)))
        self.assertEqual(code, 0, view)
        self.assertEqual(view['state'], 'CONNECTED')
        self.assertFalse(payload.exists())
        self.assertFalse(self.invoke('connect', extra=('--payload-file', str(payload)))[1]['ok'])
        outside = self.base / (uuid.uuid4().hex + '.json')
        outside.write_text(json.dumps({'ticket': ticket}), encoding='utf-8')
        self.assertFalse(self.invoke('connect', extra=('--payload-file', str(outside)))[1]['ok'])
        self.assertTrue(outside.exists())

    def test_payload_file_rejects_hardlink_without_removing_original(self):
        self.leader()
        original = self.base / 'private.json'
        original.write_bytes(b'{"ticket":"PRIVATE"}')
        payload = self.fixture.workspace / '.coding-team/bridge/inbox' / (uuid.uuid4().hex + '.json')
        os.link(original, payload)
        code, view = self.invoke('connect', extra=('--payload-file', str(payload)))
        self.assertEqual(code, 2)
        self.assertNotIn('PRIVATE', json.dumps(view))
        self.assertEqual(original.read_bytes(), b'{"ticket":"PRIVATE"}')

    def test_untrusted_exception_is_redacted(self):
        import ct_bridge_control as control
        with patch.object(control, 'connect', side_effect=ValueError('SECRET private path')):
            code, view = self.invoke('connect', b'{"ticket":"secret"}')
        self.assertEqual(code, 2)
        self.assertEqual(view['reason'], 'BRIDGE_OPERATION_FAILED')

    def test_status_read_failure_does_not_echo_backend_exception(self):
        import ct_bridge_control as control
        self.leader()
        with patch.object(control, '_policy', side_effect=OSError('SECRET private path')):
            code, view = self.invoke('status')
        self.assertEqual(code, 2)
        self.assertEqual(view['reason'], 'BRIDGE_OPERATION_FAILED')
        self.assertNotIn('SECRET', json.dumps(view))

    def test_real_entry_parser_emits_one_json_and_no_stderr(self):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/ct_bridge_worker.py'),
                                 'connect', '--ticket', 'SECRET'], capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, b'')
        self.assertEqual(json.loads(result.stdout)['reason'], 'INVALID_ARGUMENTS')
        self.assertNotIn(b'SECRET', result.stdout)

    def publish(self, mode='manual'):
        import ct_bridge_attempts as attempts
        import ct_bridge_control as control
        round_id = control.open_round(self.leader_ctx, self.expected)
        view = attempts.publish(self.leader_ctx, self.expected, round_id,
                                attempts.Request(uuid.uuid4().hex, b'Inspect the assigned file\n', mode=mode))
        self.assertTrue(view['ok'], view)
        return ('--task-id', view['task_id'], '--attempt', str(view['attempt_no']))

    def test_manual_begin_resume_finish_preserve_exact_result_and_receipt(self):
        ticket = self.leader()
        self.invoke('connect', json.dumps({'ticket': ticket}).encode())
        key_args = self.publish()
        code, begun = self.invoke('begin', b'{}', key_args)
        self.assertEqual(code, 0, begun)
        self.assertEqual(begun['request'], 'Inspect the assigned file\n')
        self.assertEqual(begun['kind'], 'READ')
        execution = begun['execution_ticket']
        paused = {'execution_ticket': execution, 'state': 'NEEDS_USER', 'result': 'Need approval'}
        self.assertEqual(self.invoke('finish', json.dumps(paused).encode(), key_args)[1]['state'], 'NEEDS_USER')
        self.assertEqual(self.invoke('resume', json.dumps({'execution_ticket': execution}).encode(), key_args)[1]['state'], 'RUNNING')
        payload = {'execution_ticket': execution, 'state': 'COMPLETED', 'result': 'Exact result\nTiếng Việt\n'}
        code, finished = self.invoke('finish', json.dumps(payload).encode(), key_args)
        self.assertEqual(code, 0, finished)
        self.assertEqual(finished['state'], 'COMPLETED')
        path = self.fixture.workspace / '.coding-team/bridge' / key_args[1] / 'attempt-1/result.md'
        self.assertEqual(path.read_bytes(), 'Exact result\nTiếng Việt\n'.encode())
        replay = self.invoke('finish', json.dumps(payload).encode(), key_args)[1]
        self.assertEqual(replay['result_id'], finished['result_id'])

    def test_listen_claim_can_only_begin_exact_attempt_once(self):
        import ct_bridge_control as control
        from ct_bridge_store import Store
        ticket = self.leader()
        self.invoke('connect', json.dumps({'ticket': ticket}).encode())
        self.assertTrue(control.set_auto(self.leader_ctx, self.expected, True)['ok'])
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.invoke, 'listen')
            try:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    with Store(self.leader_ctx.paths).read() as db:
                        if db.execute('SELECT 1 FROM waiter').fetchone():
                            break
                    time.sleep(.02)
                else:
                    self.fail('listener did not register')
                key_args = self.publish('auto')
                code, claimed = future.result(timeout=8)
                self.assertEqual(code, 0, claimed)
                self.assertEqual(claimed['state'], 'CLAIMED')
                self.assertEqual(claimed['task_id'], key_args[1])
                self.assertFalse(self.invoke('begin', b'{}', key_args)[1]['ok'])
                claim_payload = json.dumps({'claim': claimed['claim']}).encode()
                self.assertEqual(self.invoke('begin', claim_payload, key_args)[1]['state'], 'RUNNING')
                self.assertFalse(self.invoke('begin', claim_payload, key_args)[1]['ok'])
            finally:
                control.set_auto(self.leader_ctx, self.expected, False)
                future.result(timeout=8)

    def test_invalid_keys_and_finish_payloads_cannot_reach_attempt(self):
        for key_args in ((), ('--task-id', '../x', '--attempt', '1'),
                         ('--task-id', 'task', '--attempt', '0')):
            self.assertEqual(self.invoke('begin', b'{}', key_args)[1]['reason'], 'INVALID_ARGUMENTS')
        key_args = ('--task-id', 'task', '--attempt', '1')
        for payload in ({'execution_ticket': 'x', 'state': 'REVOKED', 'result': ''},
                        {'execution_ticket': 'x', 'state': 'COMPLETED', 'result': 4},
                        {'execution_ticket': 'x', 'state': 'COMPLETED', 'result': '\ud800'}):
            self.assertEqual(self.invoke('finish', json.dumps(payload).encode(), key_args)[1]['reason'], 'INVALID_PAYLOAD')


if __name__ == '__main__':
    unittest.main()
