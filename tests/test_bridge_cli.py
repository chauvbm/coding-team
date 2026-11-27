"""Exact chat control routing and Bridge CLI argument validation."""

import json
from pathlib import Path
import subprocess
import sys
import unittest


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
                     '/coding-team antigravity pair; echo extra', '/coding-team agy on',
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


if __name__ == '__main__':
    unittest.main()
