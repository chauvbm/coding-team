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
