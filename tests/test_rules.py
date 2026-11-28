import json
import os
from pathlib import Path
import shutil
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'
sys.path.insert(0, str(SCRIPTS))

import ct_policy  # noqa: E402
import ct_rules  # noqa: E402
from ct_storage import shell_argument  # noqa: E402


class RulesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.state = self.root / 'state.json'

    def block(self, platform='claude-code', skill=None):
        return ct_rules.render_rules_block(
            platform=platform, python=sys.executable, cli=SCRIPTS / 'coding_team.py',
            state=self.state, skill_dir=skill or self.root / 'skill')

    def test_upsert_idempotent_preserves_unrelated_bytes(self):
        agents = self.root / 'instructions' / 'CLAUDE.md'
        agents.parent.mkdir()
        prefix, suffix = b'\xef\xbb\xbf# unrelated\r\n\xff\r\n', b'\r\n# trailing\x80\n'
        agents.write_bytes(prefix + ct_rules.START.encode() + b'old' + ct_rules.END.encode() + suffix)
        block = self.block(skill=self.root / 'custom skill')
        self.assertTrue(ct_rules.upsert_block(agents, block))
        installed = agents.read_bytes()
        self.assertTrue(installed.startswith(prefix))
        self.assertTrue(installed.endswith(suffix))
        self.assertIn(shell_argument(Path(sys.executable).as_posix()).encode(), installed)
        self.assertIn(self.state.as_posix().encode(), installed)
        self.assertFalse(ct_rules.upsert_block(agents, block))
        self.assertEqual(agents.read_bytes(), installed)
        self.assertFalse(self.state.exists())

    def test_upsert_append_to_missing_and_unmarked(self):
        for original in (None, b'', b'unrelated', b'unrelated\n'):
            with self.subTest(original=original):
                agents = self.root / 'CLAUDE.md'
                agents.unlink(missing_ok=True)
                if original is not None:
                    agents.write_bytes(original)
                ct_rules.upsert_block(agents, self.block())
                installed = agents.read_bytes()
                self.assertTrue(installed.startswith(original or b''))
                self.assertEqual(installed.count(ct_rules.START.encode()), 1)
                ct_rules.upsert_block(agents, self.block())
                self.assertEqual(agents.read_bytes(), installed)

    def test_upsert_rejects_malformed_markers(self):
        start, end = ct_rules.START.encode(), ct_rules.END.encode()
        for original in (start, end, start + end + start + end, end + b'text' + start,
                         start + start + end, start + end + end):
            with self.subTest(original=original):
                agents = self.root / 'CLAUDE.md'
                agents.write_bytes(original)
                with self.assertRaises(ValueError):
                    ct_rules.upsert_block(agents, self.block())
                self.assertEqual(agents.read_bytes(), original)

    def test_remove_block_preserves_rest(self):
        agents = self.root / 'CLAUDE.md'
        self.assertFalse(ct_rules.remove_block(agents))
        for original in (b'', b'# one\n', b'\xef\xbb\xbf# a\r\n\xff\r\n', b'x\n\n\n',
                         b'unrelated', b'a\r\nb', b'a\r\nb\r\n', b'\xef\xbb\xbf# t\r\n\xff'):
            with self.subTest(original=original):
                agents.write_bytes(original)
                ct_rules.upsert_block(agents, self.block())
                inserted = agents.read_bytes()
                self.assertFalse(ct_rules.upsert_block(agents, self.block()))
                self.assertEqual(agents.read_bytes(), inserted)
                self.assertTrue(ct_rules.remove_block(agents))
                self.assertEqual(agents.read_bytes(), original)
                self.assertFalse(ct_rules.remove_block(agents))
        middle = b'top\n' + ct_rules.START.encode() + b'x' + ct_rules.END.encode() + b'\nbottom\n'
        agents.write_bytes(middle)
        self.assertTrue(ct_rules.remove_block(agents))
        self.assertEqual(agents.read_bytes(), b'top\n\nbottom\n')

    def test_block_contents(self):
        text = self.block()
        for needle in ('/coding-team on --global', '/coding-team off --global', '/coding-team reset',
                       '/coding-team help',
                       'Routing is ADVISORY: these instructions guide the Leader; '
                       'they do not enforce a single orchestrator.',
                       'Subagents/workers: these rules are Leader-only.',
                       'references/orchestration.md', 'File count alone is never the criterion',
                       '--platform', 'UNKNOWN', '--chat-id', '<exact command>'):
            self.assertIn(needle, text)
        self.assertNotIn('on all', text)
        self.assertNotIn('inherit', text)
        self.assertTrue(text.startswith(ct_rules.START) and text.endswith(ct_rules.END))
        order = [text.index(f'\n{n}. ') for n in range(1, 7)]
        self.assertEqual(order, sorted(order))

    def test_adaptive_review_precedes_approval(self):
        for platform in ('codex', 'claude-code', 'antigravity'):
            with self.subTest(platform=platform):
                text = self.block(platform=platform)
                self.assertIn((self.root / 'skill' / 'references/adaptive-planning.md').as_posix(), text)
                for phrase in ('Leader personally writes and revises both',
                               'at least two distinct independent subagents per artifact',
                               'current scoped agreement', 'Leader approves and continues',
                               'scope expansion or a major architectural change',
                               'Spec approval precedes Plan drafting',
                               'Plan approval precedes implementation',
                               'direct work or a checklist', 'OFF', 'SOLO'):
                    self.assertIn(phrase, text)
                self.assertNotIn('before asking the user to approve the Spec', text)

    def test_formal_work_routes_before_legacy_route(self):
        text = self.block(platform='codex')
        self.assertIn('When complex work needs a formal Spec or Plan', text)
        self.assertLess(text.index('When complex work needs a formal Spec or Plan'), text.index('Routing table'))
        self.assertIn('references/adaptive-planning.md', text)
        self.assertIn('FULL_TEAM', text)

    def test_shell_forms_and_labels(self):
        text = self.block()
        if os.name == 'nt':
            self.assertIn('use the one matching the shell you are running', text)
            for line in (l for l in text.splitlines() if l.startswith(('Check eligibility', 'Handle direct'))):
                self.assertIn("PowerShell: `& '", line)
                self.assertIn('Git Bash / POSIX shell: `', line)
            posix = re.findall(r'Git Bash / POSIX shell: `([^`]*)`', text)
            self.assertEqual(len(posix), 2)
            self.assertFalse(any(item.startswith('&') for item in posix))
        else:
            self.assertNotIn('PowerShell', text)
            self.assertNotIn('& ', text.split('Chat id contract')[0])

    @unittest.skipUnless(os.name == 'nt', 'inter-app Desktop setup on Windows')
    def test_desktop_rules_route_canonical_controls_in_each_chat(self):
        leader = self.block(platform='codex')
        worker = self.block(platform='antigravity')

        self.assertIn('/coding-team agy on|off', leader)
        self.assertIn('/coding-team agy auto on|off', leader)
        self.assertNotIn('/coding-team antigravity pair', leader)
        for command in ('/coding-team listen', '/coding-team status', '/coding-team help'):
            self.assertIn(command, worker)
        self.assertIn('coding-team-desktop-worker', worker)
        self.assertIn('ct_bridge_worker.py', worker)
        self.assertIn('connection-status', worker)
        self.assertNotIn('ct_bridge_worker.py', self.block(platform='claude-code'))

    @staticmethod
    def _forms(text):
        """Return {label: (status_command, command_template)} from a rendered block."""
        status_line = next(l for l in text.splitlines() if l.startswith('Check eligibility'))
        command_line = next(l for l in text.splitlines() if l.startswith('Handle direct'))
        if os.name == 'nt':
            pattern = r'(PowerShell|Git Bash / POSIX shell): `([^`]*)`'
            status = dict(re.findall(pattern, status_line))
            command = dict(re.findall(pattern, command_line))
            return {label: (status[label], command[label]) for label in status}
        return {'sh': (re.search(r'by running `([^`]*)`', status_line).group(1),
                       re.search(r'commands with `([^`]*)`', command_line).group(1))}

    @staticmethod
    def _shell_args(label):
        if label == 'PowerShell':
            shell = shutil.which('powershell') or shutil.which('pwsh')
            if shell is None:
                raise unittest.SkipTest('PowerShell not found')
            return [shell, '-NoProfile', '-NonInteractive', '-Command']
        if os.name != 'nt':
            return ['/bin/sh', '-c']
        candidates = [Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Git/bin/bash.exe']
        git = shutil.which('git')
        if git:
            candidates.append(Path(git).resolve().parents[1] / 'bin/bash.exe')
        for candidate in candidates:
            if candidate.is_file():
                return [str(candidate), '-c']
        raise unittest.SkipTest('Git Bash not found')

    @unittest.skipUnless((SCRIPTS / 'coding_team.py').exists(), 'CLI added in Task 10')
    def test_generated_commands_execute_with_special_paths(self):
        special = "spaces apostrophe' $(echo SUBSTITUTED) Nguyễn"
        if os.name != 'nt':
            special += ' "double quotes"'
        skill = self.root / special / 'skill'
        shutil.copytree(SCRIPTS, skill / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
        (skill / 'platforms').mkdir()
        shutil.copyfile(ROOT / 'platforms/claude-code.json', skill / 'platforms/claude-code.json')
        shutil.copytree(ROOT / 'agents/roles', skill / 'agents/roles')
        state = self.root / special / 'state file.json'
        text = ct_rules.render_rules_block(
            platform='claude-code', python=sys.executable, cli=skill / 'scripts/coding_team.py',
            state=state, skill_dir=skill)
        env = dict(os.environ, CLAUDE_CODE_SESSION_ID='fixture-shell')
        forms = self._forms(text)
        self.assertEqual(len(forms), 2 if os.name == 'nt' else 1)
        for label, (status_command, template) in forms.items():
            with self.subTest(shell=label):
                shell_args = self._shell_args(label)
                state.unlink(missing_ok=True)
                ct_policy.initialize_state(state)
                before = state.read_bytes()
                result = subprocess.run(shell_args + [status_command], env=env, capture_output=True,
                                        text=True, encoding='utf-8', timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                self.assertEqual(json.loads(result.stdout)['effective_policy'], 'ON')
                self.assertEqual(state.read_bytes(), before)
                command = template.replace(shell_argument('<exact command>'),
                                           shell_argument('/coding-team off'))
                result = subprocess.run(shell_args + [command], env=env, capture_output=True,
                                        text=True, encoding='utf-8', timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                self.assertEqual(json.loads(result.stdout)['effective_policy'], 'OFF')


if __name__ == '__main__':
    unittest.main()
