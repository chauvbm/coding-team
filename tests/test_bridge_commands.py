"""Tests for the shared public inter-app command catalog."""
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import ct_bridge_commands as commands
import ct_policy


class BridgeCommandTests(unittest.TestCase):
    def test_public_command_grammar(self):
        self.assertEqual(commands.parse_public(' /TEAM-CODING AGY AUTO  OFF '),
                         ('antigravity', 'auto_off'))
        self.assertEqual(commands.parse_public('/coding-team listen'), (None, 'listen'))
        self.assertEqual(commands.parse_public('$coding-team AGY on'), ('antigravity', 'on'))
        self.assertIsNone(commands.parse_public('/coding-team antigravity on'))
        self.assertEqual(commands.parse_public('/coding-team claude auto on'),
                         ('claude-code', 'auto_on'))
        for invalid in ('/coding-team on\n/coding-team off',
                        '"/coding-team listen"', '/coding-team agy on extra',
                        '/coding-team auto on', '/coding-team agy agy on',
                        '/coding-team agy on --global'):
            self.assertIsNone(commands.parse_public(invalid), invalid)
        self.assertFalse(commands.is_public_family('/coding-team on'))
        self.assertTrue(commands.is_public_family('/coding-team agy on'))
        self.assertTrue(commands.is_public_family('/coding-team agy malformed'))
        self.assertFalse(commands.is_public_family('/coding-team antigravity on'))

    def test_help_shared_across_components(self):
        leader = commands.help_view('codex', 'leader')
        worker = commands.help_view('antigravity', 'worker')
        native = commands.help_view('codex', 'native')
        claude_native = commands.help_view('claude-code', 'native')
        self.assertEqual(leader['help'], worker['help'])
        self.assertEqual(len(leader['help']), 5)
        self.assertEqual(leader['supported_pairs'], [('codex', 'antigravity')])
        self.assertEqual(worker['supported_pairs'], [('codex', 'antigravity')])
        for view in (leader, worker):
            self.assertIn('/coding-team agy auto on', view['example'])
            self.assertIn('/coding-team listen', view['example'])
            self.assertIn('same Git worktree', view['example'])
        self.assertEqual(native['policy'], ct_policy.HELP_LINES)
        self.assertEqual(claude_native['available_pairs'], [])
        self.assertNotIn('claude', str(claude_native['supported_pairs']))
        self.assertEqual(claude_native['example'], '/coding-team status')
        self.assertEqual(commands.help_view('antigravity', 'worker')['available_pairs'],
                         [('codex', 'antigravity')])
        self.assertEqual(commands.help_view('codex', 'leader')['available_pairs'],
                         [('codex', 'antigravity')])
        self.assertTrue(commands.supports_pair('codex', 'antigravity'))
        self.assertFalse(commands.supports_pair('antigravity', 'codex'))
        self.assertFalse(commands.supports_pair('codex', 'codex'))
