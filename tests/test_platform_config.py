import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import ct_platform


def cfg(name='claude-code'):
    return ct_platform.load_platform(name)


class PlatformConfigTests(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory()
        self.addCleanup(self.fixture.cleanup)
        self.tmp = Path(self.fixture.name)
        self.home = self.tmp / 'home'

    def test_shipped_platforms_validate(self):
        for name in ct_platform.PLATFORMS:
            data = ct_platform.read_json_unique(ct_platform.REPO_ROOT / 'platforms' / f'{name}.json')
            result = ct_platform.validate_platform(data)
            self.assertIs(result['valid'], True)
            self.assertIs(result['availability_verified'], False)
            self.assertEqual(result['platform'], name)
        self.assertEqual(len(ct_platform.ROLES), 6)
        self.assertEqual(set(ct_platform.TIERS), {'inherit', 'strong', 'standard', 'light'})

    def test_custom_model_and_inherit_valid(self):
        data = cfg()
        data['tiers']['strong'] = {'model': 'organization-private-model', 'effort': 'xhigh'}
        data['tiers']['light'] = {'model': None, 'effort': None}
        self.assertTrue(ct_platform.validate_platform(data)['valid'])

    def test_schema_rejects_bad_shapes(self):
        def set_tier(key, value):
            def fn(d):
                d['tiers']['strong'][key] = value
            return fn

        cases = {
            'missing tier': lambda d: d['tiers'].pop('light'),
            'extra tier': lambda d: d['tiers'].update(ultra={'model': 'x', 'effort': None}),
            'inherit not null': lambda d: d['tiers'].update(inherit={'model': 'opus', 'effort': None}),
            'missing role': lambda d: d['roles'].pop('ct-security'),
            'extra role': lambda d: d['roles'].update({'ct-extra': {'tier': 'light'}}),
            'old role': lambda d: d['roles'].update({'QA / Reviewer': d['roles'].pop('ct-reviewer')}),
            'model empty': set_tier('model', ''),
            'model blank': set_tier('model', '  '),
            'model bool': set_tier('model', True),
            'model int': set_tier('model', 12),
            'effort word': set_tier('effort', 'maximum'),
            'effort case': set_tier('effort', 'HIGH'),
            'effort list': set_tier('effort', []),
            'effort outside vocab': set_tier('effort', 'ultra'),
            'static always': lambda d: d.update(static_agent_model='always'),
            'project abs': lambda d: d['project_paths'].update(skill_dir='/abs/skills'),
            'project dotdot': lambda d: d['project_paths'].update(agents_dir='../agents'),
            'placeholder user': lambda d: d['paths'].update(skill_dir='{user}/skills'),
            'schema v1': lambda d: d.update(schema_version=1),
            'unknown key': lambda d: d.update(read_only_mcp_patterns=[]),
        }
        for label, fn in cases.items():
            with self.subTest(label):
                data = copy.deepcopy(cfg())
                fn(data)
                with self.assertRaises(ValueError):
                    ct_platform.validate_platform(data)

    def test_duplicate_keys_rejected(self):
        for text in ('{"a": 1, "a": 2}', '{"a": {"b": 1, "b": 2}}', '{"a": [{"b": 1, "b": 2}]}'):
            path = self.tmp / 'dup.json'
            path.write_text(text, encoding='utf-8')
            with self.subTest(text):
                with self.assertRaises(ValueError):
                    ct_platform.read_json_unique(path)
        ok = self.tmp / 'ok.json'
        ok.write_bytes(b'\xef\xbb\xbf{"a": 1}')
        self.assertEqual(ct_platform.read_json_unique(ok), {'a': 1})

    def test_config_root_env_and_empty_env(self):
        data = cfg()
        custom = self.tmp / 'cfg'
        paths = ct_platform.target_paths(data, 'user', {'CLAUDE_CONFIG_DIR': str(custom)}, self.home, None)
        self.assertEqual(paths['skill_dir'], custom / 'skills' / 'coding-team')
        for blank in ('', '  '):
            paths = ct_platform.target_paths(data, 'user', {'CLAUDE_CONFIG_DIR': blank}, self.home, None)
            self.assertEqual(paths['skill_dir'], self.home / '.claude' / 'skills' / 'coding-team')
        self.assertEqual(ct_platform.resolve_config_root(data, {}, self.home), self.home / '.claude')

    def test_project_scope_paths(self):
        project = self.tmp / 'proj'
        paths = ct_platform.target_paths(cfg(), 'project', {}, self.home, project)
        self.assertEqual(paths['skill_dir'], project / '.claude' / 'skills' / 'coding-team')
        self.assertEqual(paths['agents_dir'], project / '.claude' / 'agents')
        self.assertIsNone(paths['global_rules_file'])
        self.assertEqual(paths['state_file'], self.home / '.claude' / 'coding-team' / 'state.json')
        for root in paths['managed_roots']:
            self.assertIsInstance(root, Path)
        with self.assertRaises(ValueError):
            ct_platform.target_paths(cfg(), 'project', {}, self.home, None)
        with self.assertRaises(ValueError):
            ct_platform.target_paths(cfg(), 'machine', {}, self.home, project)

    def test_user_scope_paths_all_platforms(self):
        paths = ct_platform.target_paths(cfg('codex'), 'user', {}, self.home, None)
        self.assertEqual(paths['skill_dir'], self.home / '.agents' / 'skills' / 'coding-team')
        self.assertEqual(paths['global_rules_file'], self.home / '.codex' / 'AGENTS.md')
        paths = ct_platform.target_paths(cfg('antigravity'), 'user', {}, self.home, None)
        self.assertEqual(paths['agents_dir'], self.home / '.gemini' / 'config' / 'agents')
        self.assertEqual(paths['state_file'],
                         self.home / '.gemini' / 'antigravity' / 'coding-team' / 'state.json')
        self.assertIn(paths['skill_dir'], paths['managed_roots'])
        self.assertIn(paths['state_file'].parent, paths['managed_roots'])
        self.assertIn(paths['global_rules_file'].parent, paths['managed_roots'])

    def test_antigravity_read_tools_narrow(self):
        agy = cfg('antigravity')
        self.assertEqual(agy['tools']['read'], ['send_message', 'view_file'])
        self.assertEqual(agy['tools']['docs_lookup'], ['read_url_content', 'search_web'])
        for name in ct_platform.PLATFORMS:
            for key, items in cfg(name)['tools'].items():
                self.assertNotIn('schedule', items, (name, key))
                self.assertNotIn('generate_image', items, (name, key))

    def test_aux_files_next_to_state(self):
        paths = ct_platform.target_paths(cfg(), 'user', {}, self.home, None)
        parent = paths['state_file'].parent
        self.assertEqual(paths['backup_root'], parent / 'backups')
        self.assertEqual(paths['lock_file'], parent / 'install.lock')
        self.assertEqual(paths['journal_file'], parent / 'install-journal.json')

    def test_detect_platform(self):
        self.assertEqual(ct_platform.detect_platform({'CLAUDECODE': '1'}), 'claude-code')
        self.assertEqual(ct_platform.detect_platform({'CODEX_THREAD_ID': 't'}), 'codex')
        self.assertIsNone(ct_platform.detect_platform({}))
        self.assertIsNone(ct_platform.detect_platform({'CLAUDECODE': '1', 'CODEX_THREAD_ID': 't'}))
        self.assertIsNone(ct_platform.detect_platform({'CLAUDECODE': ''}))
        self.assertIsNone(ct_platform.detect_platform({'CLAUDECODE': '  '}))

    def test_resolve_tier(self):
        data = cfg()
        self.assertEqual(ct_platform.resolve_tier(data, 'ct-architect'),
                         {'tier': 'strong', 'model': 'opus', 'effort': 'high'})
        self.assertEqual(ct_platform.resolve_tier(data, 'ct-architect', 'light')['model'], 'haiku')
        self.assertEqual(ct_platform.resolve_tier(data, 'ct-architect', 'inherit'),
                         {'tier': 'inherit', 'model': None, 'effort': None})
        with self.assertRaises(ValueError):
            ct_platform.resolve_tier(data, 'ct-architect', 'huge')
        with self.assertRaises(ValueError):
            ct_platform.resolve_tier(data, 'ct-unknown')

    def test_chat_id_from_env(self):
        data = cfg()
        self.assertEqual(ct_platform.chat_id_from_env(data, {'CLAUDE_CODE_SESSION_ID': ' abc '}), 'abc')
        for blank in ('', '  '):
            self.assertIsNone(ct_platform.chat_id_from_env(data, {'CLAUDE_CODE_SESSION_ID': blank}))
        self.assertIsNone(ct_platform.chat_id_from_env(data, {}))
        agy = cfg('antigravity')
        env = {'ANTIGRAVITY_CHAT_ID': 'b', 'ANTIGRAVITY_CONVERSATION_ID': 'a'}
        self.assertEqual(ct_platform.chat_id_from_env(agy, env), 'a')
        env = {'ANTIGRAVITY_CONVERSATION_ID': ' ', 'ANTIGRAVITY_CHAT_ID': 'b'}
        self.assertEqual(ct_platform.chat_id_from_env(agy, env), 'b')


if __name__ == '__main__':
    unittest.main()
