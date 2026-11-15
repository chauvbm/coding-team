"""Documentation consistency checks for SKILL.md, references, role files and the README.

These are static text checks (kind a: docs/schema). They say nothing about agent behaviour.
"""
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import ct_roles  # noqa: E402

LEGACY_BLOCK = re.compile(r'<!--\s*legacy-migration\s*-->.*?<!--\s*/legacy-migration\s*-->', re.S)
LINK = re.compile(r'(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)')
FENCE = re.compile(r'^(```|~~~).*?^\1[^\n]*$', re.S | re.M)
CODE_SPAN = re.compile(r'`[^`\n]*`')


def read(path):
    return Path(path).read_text(encoding='utf-8')


def doc_files(include_readme=True):
    """SKILL.md, references/*.md, agents/roles/*.md and (optionally) README.md."""
    files = [ROOT / 'SKILL.md']
    files += sorted((ROOT / 'references').glob('*.md'))
    files += sorted((ROOT / 'agents' / 'roles').glob('*.md'))
    if include_readme:
        files.append(ROOT / 'README.md')
    return [path for path in files if path.exists()]


class SkillDocsTests(unittest.TestCase):
    def test_role_ids_documented(self):
        """(a) docs: SKILL.md and the agent contracts name every shipped role id and variant."""
        roles = ct_roles.load_roles(ROOT / 'agents' / 'roles')
        self.assertTrue(roles)
        skill = read(ROOT / 'SKILL.md')
        contracts = read(ROOT / 'references' / 'agent-contracts.md')
        for role_id, role in roles.items():
            with self.subTest(role=role_id):
                self.assertIn(role_id, skill)
                self.assertIn(role_id, contracts)
                for variant in role['variants']:
                    self.assertIn(variant, contracts)

    def test_no_legacy_command_syntax(self):
        """(a) docs: old command syntax appears only inside the legacy-migration block."""
        files = doc_files(include_readme=True)
        self.assertGreater(len(files), 5)
        saw_block = False
        for path in files:
            raw = read(path)
            saw_block = saw_block or bool(LEGACY_BLOCK.search(raw))
            text = LEGACY_BLOCK.sub('', raw)
            self.assertNotIn('<!-- legacy-migration', text, '%s: unbalanced legacy-migration markers' % path.name)
            self.assertNotIn('<!-- /legacy-migration', text, '%s: unbalanced legacy-migration markers' % path.name)
            for needle in ('/coding team ', 'on all', 'off all', '`inherit`'):
                with self.subTest(file=path.name, needle=needle):
                    self.assertNotIn(needle, text)
        self.assertTrue(saw_block, 'the legacy-migration block was not found in any doc')

    def test_skill_is_platform_neutral(self):
        """(a) docs: SKILL.md names no platform or platform-only tool; platform detail lives in references."""
        text = read(ROOT / 'SKILL.md')
        for pattern in (r'claude', r'codex', r'antigravity', r'gemini', r'CLAUDE\.md', r'AGENTS\.md',
                        r'spawn_agent', r'subagent_type', r'WebFetch', r'WebSearch', r'\.claude/',
                        r'\.codex/', r'\.gemini/'):
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, text, re.I if pattern.islower() else 0))

    def test_relative_links_resolve(self):
        """(a) docs: every relative markdown link targets an existing file (and anchors an existing heading)."""
        checked = 0
        for path in doc_files(include_readme=True):
            text = CODE_SPAN.sub('', FENCE.sub('', read(path)))
            for target in LINK.findall(text):
                if re.match(r'^[a-z][a-z0-9+.-]*:', target, re.I) or target.startswith('#'):
                    continue
                relative, _, anchor = target.partition('#')
                resolved = (path.parent / relative).resolve()
                with self.subTest(file=path.name, link=target):
                    self.assertTrue(resolved.is_file(), 'broken link %s in %s' % (target, path.name))
                    if anchor and resolved.suffix == '.md':
                        self.assertIn(anchor, heading_slugs(read(resolved)))
                checked += 1
        self.assertGreater(checked, 5)

    def test_orchestration_doc_states_advisory_and_limits(self):
        """(a) docs: orchestration.md says routing is ADVISORY, evidence is caller-asserted, and keeps row order."""
        text = read(ROOT / 'references' / 'orchestration.md')
        for phrase in ('ADVISORY', 'caller-asserted', 'not a running process'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)
        lowered = text[text.index('Apply top to bottom'):].lower()
        positions = [lowered.find(word.lower()) for word in ('L3', 'architect', 'interface', 'independent',
                                                              'plan', 'small')]
        self.assertNotIn(-1, positions)
        self.assertEqual(sorted(positions), positions, 'routing conditions out of order: %s' % positions)

    def test_adaptive_planning_is_linked_and_platform_neutral(self):
        adaptive = ROOT / 'references' / 'adaptive-planning.md'
        self.assertTrue(adaptive.is_file())
        text = read(adaptive)
        for phrase in ('Leader personally writes and revises both', 'reviewers independently assess',
                       'user approval', 'OFF', 'SOLO', 'FULL_TEAM', 'do not intercept runtime tool calls'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase.lower(), text.lower())
        for path in (ROOT / 'SKILL.md', ROOT / 'README.md'):
            with self.subTest(file=path.name):
                self.assertIn('adaptive-planning.md', read(path))

    def test_readme_states_install_paths_and_guarantee_limits(self):
        """(a) docs: README names the repo, installs all three platforms and states the ADVISORY limits."""
        text = read(ROOT / 'README.md')
        for phrase in ('chauvbm/coding-team.git', '--recover', '--replace-unmanaged', '--overwrite-modified',
                       'granting any MCP tool makes `ct-researcher` guarantees ADVISORY',
                       'shell access makes no-delegation ADVISORY', 'references/orchestration.md',
                       'release.json', 'not yet verified', 'doctor'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase.lower(), text.lower())
        self.assertNotIn('coding-team-codex.git', text)
        for platform in ('claude-code', 'codex', 'antigravity'):
            with self.subTest(platform=platform):
                self.assertIn('install --platform %s --dry-run' % platform, text)
                self.assertIn('install --platform %s --default on' % platform, text)
        self.assertNotIn('| verified |', text.lower(), 'no platform may be claimed verified without a smoke record')

    def test_smoke_result_template_shape(self):
        """(a) schema: the smoke template has the six checks, each with status/evidence/reason_if_not_run."""
        data = json.loads(read(ROOT / 'docs' / 'smoke' / 'result-template.json'))
        for key in ('platform', 'runtime_version', 'date', 'checks'):
            self.assertIn(key, data)
        self.assertEqual(set(data['checks']), {'dispatch', 'model_override', 'isolation', 'no_nesting',
                                               'runtime_failure', 'routing_eval'})
        for name, check in data['checks'].items():
            with self.subTest(check=name):
                self.assertIn(check['status'], ('PASS', 'FAIL', 'NOT_RUN'))
                self.assertIn('evidence', check)
                self.assertIn('reason_if_not_run', check)
        guide = read(ROOT / 'docs' / 'smoke' / 'platform-smoke.md')
        for phrase in ('--cases <checkout>/tests/orchestration_cases.json', 'check_behavioral_results.py',
                       '--scope project', 'evaluations/<YYYY-MM-DD>-platform-smoke/<platform>.json'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, guide)


def heading_slugs(markdown):
    slugs = set()
    for line in FENCE.sub('', markdown).splitlines():
        match = re.match(r'^#{1,6}\s+(.*?)\s*#*\s*$', line)
        if match:
            slug = re.sub(r'[^\w\- ]', '', match.group(1).lower().replace('`', '')).strip().replace(' ', '-')
            slugs.add(slug)
    return slugs


if __name__ == '__main__':
    unittest.main()
