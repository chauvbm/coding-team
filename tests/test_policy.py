import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import ct_policy as policy  # noqa: E402
import ct_storage  # noqa: E402


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory()
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name)
        self.state = self.root / 'custom state' / 'policy.json'

    def test_new_grammar(self):
        expected = {'status': ('status', None), 'help': ('help', None),
                    'on': ('chat', 'ON'), 'off': ('chat', 'OFF'),
                    'on --global': ('global', 'ON'), 'off --global': ('global', 'OFF'),
                    'reset': ('chat', 'RESET')}
        self.assertEqual(len(expected), 7)
        for prefix in ('/coding-team', '$coding-team'):
            for suffix, result in expected.items():
                with self.subTest(prefix=prefix, suffix=suffix):
                    self.assertEqual(policy.parse_command(' \t' + prefix.upper() + '  ' + suffix.upper() + '\n'), result)

    def test_legacy_syntax_rejected_with_hint(self):
        cases = {'/coding team on': '/coding-team on',
                 '/coding-team on all': '/coding-team on --global',
                 '/coding-team off all': '/coding-team off --global',
                 '/coding-team inherit': '/coding-team reset'}
        for text, hint in cases.items():
            with self.subTest(text=text):
                with self.assertRaises(policy.LegacyCommandError) as caught:
                    policy.parse_command(text)
                self.assertIn(hint, caught.exception.hint)
                self.assertIsInstance(caught.exception, ValueError)

    def test_embedded_or_quoted_commands_rejected(self):
        for text in ('"/coding-team on"', "'/coding-team off'", '`/coding-team status`',
                     'please /coding-team on', '/coding-team on please', '/coding-team',
                     '/coding-team on --global now', '/coding-team on\n/coding-team off',
                     '/coding-team on\n--global', '/coding-teams on', '/coding-team-on',
                     '/coding-team on; echo yes', '', '   ', None):
            with self.subTest(text=text), self.assertRaises(ValueError):
                policy.parse_command(text)

    def test_initialize_default(self):
        state, created = policy.initialize_state(self.state, 'off')
        self.assertTrue(created)
        self.assertEqual(state['global_policy'], 'OFF')
        self.assertEqual(state['chat_policies'], {})
        policy.apply_command(self.state, '/coding-team on', 'first')
        policy.apply_command(self.state, '/coding-team off --global', None)
        state, created = policy.initialize_state(self.state, 'on')
        self.assertFalse(created)
        self.assertEqual(state['global_policy'], 'ON')
        self.assertEqual(state['chat_policies'], {'first': 'ON'})
        before = self.state.read_bytes()
        state, created = policy.initialize_state(self.state)
        self.assertFalse(created)
        self.assertEqual(self.state.read_bytes(), before)
        with self.assertRaises(ValueError):
            policy.initialize_state(self.state, 'maybe')
        fresh = self.root / 'fresh.json'
        self.assertEqual(policy.initialize_state(fresh)[0]['global_policy'], 'ON')

    def test_prepare_initialization_stages_bytes_and_preserves_counters(self):
        policy.initialize_state(self.state, 'off')
        with ct_storage.locked(self.state):
            policy.initialize_policy_epochs(self.state)
        policy.apply_command(self.state, '/coding-team on', 'explicit')
        old = self.state.read_bytes()
        with ct_storage.locked(self.state):
            self.assertIsNone(policy.prepare_initialization(self.state))
            payload = policy.prepare_initialization(self.state, 'on')
            self.assertEqual(self.state.read_bytes(), old)
            ct_storage.atomic_write(self.state, payload)
            self.assertEqual(policy.policy_stamp(self.state, 'inherited', require_epochs=True),
                             policy.PolicyStamp('ON', 0, 1))
            self.assertEqual(policy.policy_stamp(self.state, 'explicit', require_epochs=True),
                             policy.PolicyStamp('ON', 1, None))

    def test_managed_install_stages_policy_epochs_without_overriding_preferences(self):
        policy.initialize_state(self.state, 'off')
        policy.apply_command(self.state, '/coding-team on', 'explicit')
        before = self.state.read_bytes()
        with ct_storage.locked(self.state):
            payload = policy.prepare_initialization(self.state, repair_epochs=True)
            self.assertEqual(self.state.read_bytes(), before)
            ct_storage.atomic_write(self.state, payload)
            self.assertEqual(policy.policy_stamp(self.state, 'explicit', require_epochs=True),
                             policy.PolicyStamp('ON', 0, None))
            self.assertEqual(policy.policy_stamp(self.state, 'other', require_epochs=True),
                             policy.PolicyStamp('OFF', 0, 0))
            self.assertIsNone(policy.prepare_initialization(self.state, repair_epochs=True))

    def test_chat_precedence_and_reset(self):
        policy.initialize_state(self.state)
        policy.apply_command(self.state, '/coding-team off', 'a')
        policy.apply_command(self.state, '/coding-team off', 'b')
        policy.apply_command(self.state, '$coding-team reset', 'a')
        self.assertEqual(policy.load_state(self.state)['chat_policies'], {'b': 'OFF'})
        for value in ('on', 'off'):
            policy.apply_command(self.state, '/coding-team ' + value + ' --global', None)
            self.assertEqual(policy.load_state(self.state)['chat_policies'], {'b': 'OFF'})
            self.assertEqual(policy.apply_command(self.state, '/coding-team status', 'b')['effective_policy'], 'OFF')
            self.assertEqual(policy.apply_command(self.state, '/coding-team status', 'a')['effective_policy'], value.upper())
        self.assertEqual(policy.apply_command(self.state, '/coding-team status', 'a')['chat_policy'], 'INHERIT')

    def test_chat_precedence_mixed_on_and_off_overrides(self):
        policy.initialize_state(self.state)
        policy.apply_command(self.state, '/coding-team off', 'one')
        policy.apply_command(self.state, '$coding-team on', 'two')
        for value in ('off', 'on'):
            policy.apply_command(self.state, '/coding-team ' + value + ' --global', None)
            self.assertEqual(policy.load_state(self.state)['chat_policies'], {'one': 'OFF', 'two': 'ON'})
            status = lambda chat: policy.apply_command(self.state, '/coding-team status', chat)
            self.assertEqual(status('one')['effective_policy'], 'OFF')
            self.assertEqual(status('two')['effective_policy'], 'ON')
            self.assertEqual(status('three')['effective_policy'], value.upper())
            self.assertEqual(status('three')['chat_policy'], 'INHERIT')
        policy.apply_command(self.state, '/coding-team reset', 'one')
        self.assertEqual(policy.load_state(self.state)['chat_policies'], {'two': 'ON'})
        self.assertEqual(policy.apply_command(self.state, '/coding-team status', 'one')['chat_policy'], 'INHERIT')

    def test_status_unknown_and_noop_do_not_write(self):
        policy.initialize_state(self.state)
        before = self.state.read_bytes()
        result = policy.apply_command(self.state, '/coding-team status', None)
        self.assertEqual(result['chat_policy'], 'UNKNOWN')
        self.assertIsNone(result['effective_policy'])
        for identity in (None, '', '   ', ' bad '):
            with self.assertRaisesRegex(ValueError, 'trusted chat id'):
                policy.apply_command(self.state, '/coding-team off', identity)
        policy.apply_command(self.state, '/coding-team on --global', None)
        policy.apply_command(self.state, '/coding-team reset', 'absent')
        self.assertEqual(self.state.read_bytes(), before)

    def test_help_needs_no_state(self):
        result = policy.apply_command(self.state, '/coding-team help', None)
        self.assertEqual(result, {'help': policy.HELP_LINES})
        self.assertEqual(len(policy.HELP_LINES), 7)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.state.parent.exists())

    def install_legacy_epochs(self):
        self.state.parent.mkdir()
        self.state.write_text(json.dumps({
            'schema_version': 1, 'global_policy': 'ON', 'chat_policies': {'fixed': 'ON'},
            'updated_at': 'legacy',
        }), encoding='utf-8')
        with ct_storage.locked(self.state):
            stamp = policy.policy_stamp(self.state, 'inherited')
            policy.initialize_policy_epochs(self.state)
        self.assertEqual(stamp, policy.PolicyStamp('ON', 0, 0))

    def stamp(self, chat):
        with ct_storage.locked(self.state):
            return policy.policy_stamp(self.state, chat, require_epochs=True)

    def test_policy_updates_preserve_original_shape(self):
        self.install_legacy_epochs()
        self.assertEqual(policy.apply_command(self.state, '/coding-team status', 'fixed'), {
            'global_policy': 'ON', 'chat_id': 'fixed', 'chat_policy': 'ON', 'effective_policy': 'ON',
        })
        self.assertEqual(policy.load_state(self.state)['schema_version'], 1)
        before = self.stamp('fixed')
        policy.apply_command(self.state, '/coding-team off --global', None)
        policy.apply_command(self.state, '/coding-team on --global', None)
        self.assertEqual(self.stamp('fixed'), before)
        self.assertEqual(self.stamp('inherited'), policy.PolicyStamp('ON', 0, 2))

    def test_transient_off_fences_only_affected_owner(self):
        self.install_legacy_epochs()
        before = self.stamp('inherited')
        unaffected = self.stamp('fixed')
        policy.apply_command(self.state, '/coding-team off', 'inherited')
        policy.apply_command(self.state, '/coding-team on', 'inherited')
        self.assertEqual(self.stamp('inherited'), policy.PolicyStamp('ON', before.chat_epoch + 2, None))
        self.assertEqual(self.stamp('fixed'), unaffected)
        policy.apply_command(self.state, '/coding-team reset', 'inherited')
        self.assertEqual(self.stamp('inherited'), policy.PolicyStamp('ON', 3, 0))

    def test_legacy_writer_change_survives_native_updates_as_incompatible(self):
        self.install_legacy_epochs()
        state = policy.load_state(self.state)
        state['global_policy'] = 'OFF'
        state['updated_at'] = 'old-writer'
        self.state.write_text(json.dumps(state), encoding='utf-8')
        self.assertEqual(policy.apply_command(self.state, '/coding-team status', 'inherited')['effective_policy'], 'OFF')
        policy.apply_command(self.state, '/coding-team on --global', None)
        with self.assertRaises(policy.PolicyWriterIncompatible):
            self.stamp('inherited')
        with ct_storage.locked(self.state):
            policy.repair_policy_epochs(self.state)
        self.assertEqual(self.stamp('inherited').effective, 'ON')
        self.assertEqual(self.stamp('fixed').effective, 'ON')

    def test_missing_and_corrupt_state_fail_closed(self):
        for payload in (None, b'{broken', b'{}', b'[]',
                        b'{"schema_version":true,"global_policy":"ON","chat_policies":{}}',
                        b'{"schema_version":1,"global_policy":"OFF","chat_policies":{"x":"BAD"}}'):
            with self.subTest(payload=payload):
                self.state.parent.mkdir(parents=True, exist_ok=True)
                if payload is None:
                    self.state.unlink(missing_ok=True)
                else:
                    self.state.write_bytes(payload)
                for text in ('/coding-team status', '/coding-team off --global', '/coding-team off'):
                    with self.assertRaises((ValueError, OSError)):
                        policy.apply_command(self.state, text, 'c')
                    self.assertEqual(self.state.read_bytes() if self.state.exists() else None, payload)
                if payload is not None:
                    with self.assertRaises(ValueError):
                        policy.initialize_state(self.state, 'on')
                    self.assertEqual(self.state.read_bytes(), payload)

    def test_concurrent_updates_do_not_lose_chat_overrides(self):
        policy.initialize_state(self.state)
        script = ('import sys; sys.path.insert(0, sys.argv[1]); import ct_policy; '
                  'ct_policy.apply_command(sys.argv[2], "/coding-team off", sys.argv[3])')
        processes = []
        try:
            for index in range(10):
                processes.append(subprocess.Popen(
                    [sys.executable, '-c', script, str(ROOT / 'scripts'), str(self.state), 'fixture-' + str(index)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            for process in processes:
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stderr + stdout)
            self.assertEqual(policy.load_state(self.state)['chat_policies'],
                             {'fixture-' + str(index): 'OFF' for index in range(10)})
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=10)


if __name__ == '__main__':
    unittest.main()
