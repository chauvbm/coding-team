import copy
from pathlib import Path
import sys
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import ct_platform
import ct_roles

ROLES_DIR = ct_platform.REPO_ROOT / 'agents' / 'roles'


def cfg(name='claude-code'):
    return ct_platform.load_platform(name)


def roles():
    return ct_roles.load_roles(ROLES_DIR)


def variant_caps(catalog):
    result = {}
    for role in catalog.values():
        result.update(role['variants'])
    return result


def rendered(platform, mcp_tools=None, catalog=None):
    catalog = catalog if catalog is not None else roles()
    return {path: data.decode('utf-8')
            for path, data in ct_roles.render_all(catalog, cfg(platform), mcp_tools)}


def frontmatter(text):
    assert text.startswith('---\n'), text[:20]
    end = text.index('\n---\n', 4)
    return text[4:end + 1]


class RoleCatalogTests(unittest.TestCase):
    def test_catalog_matches_platform_roles(self):
        catalog = roles()
        self.assertEqual(set(catalog), set(ct_platform.ROLES))
        ledger = {r['ledger_role'] for r in catalog.values() if r['ledger_role'] is not None}
        self.assertEqual(ledger, {'Architect', 'Developer', 'QA'})
        self.assertEqual(catalog['ct-architect']['ledger_role'], 'Architect')
        self.assertEqual(catalog['ct-developer']['ledger_role'], 'Developer')
        self.assertEqual(catalog['ct-reviewer']['ledger_role'], 'QA')
        caps = variant_caps(catalog)
        self.assertEqual(caps, {
            'ct-architect': ['read'],
            'ct-researcher': ['read', 'docs_lookup'],
            'ct-developer': ['read', 'write:assigned', 'shell'],
            'ct-test-engineer': ['read', 'write:tests', 'shell', 'browser_testing'],
            'ct-reviewer': ['read'],
            'ct-reviewer-verify': ['read', 'shell:checks'],
            'ct-security': ['read'],
        })
        for role_id, role in catalog.items():
            self.assertTrue(role['description'].strip(), role_id)
            self.assertTrue(role['body'].strip(), role_id)
            self.assertNotIn('\r', role['body'])
            for cap_list in role['variants'].values():
                self.assertTrue(set(cap_list) <= set(ct_roles.CAPABILITIES))

    def test_load_roles_rejects_bad_catalog(self):
        import json
        import tempfile
        good = json.loads((ROLES_DIR / 'roles.json').read_text(encoding='utf-8'))
        bad_variants = [
            lambda d: d['roles']['ct-architect']['variants'].update({'ct-architect': ['read', 'delegate']}),
            lambda d: d['roles'].pop('ct-security'),
            lambda d: d['roles']['ct-architect'].update({'ledger_role': 'Specialist'}),
            lambda d: d.update({'schema_version': 2}),
        ]
        for mutate in bad_variants:
            data = copy.deepcopy(good)
            mutate(data)
            with tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                for role_id in ct_platform.ROLES:
                    (tmp / f'{role_id}.md').write_text('body\n', encoding='utf-8')
                (tmp / 'roles.json').write_text(json.dumps(data), encoding='utf-8')
                with self.assertRaises(ValueError):
                    ct_roles.load_roles(tmp)


class GuaranteeTests(unittest.TestCase):
    def setUp(self):
        self.caps = variant_caps(roles())

    def g(self, variant, platform='claude-code', mcp_tools=None):
        return ct_roles.guarantees(self.caps[variant], cfg(platform), mcp_tools)

    def test_guarantee_matrix(self):
        arch = self.g('ct-architect')
        self.assertEqual(arch['no_file_write'], 'ENFORCED')
        self.assertEqual(arch['no_delegate'], 'ENFORCED')
        self.assertEqual(arch['write_scope'], 'N/A')
        self.assertEqual(arch['command_limits'], 'N/A')
        dev = self.g('ct-developer')
        self.assertEqual(dev['no_delegate'], 'ADVISORY')
        self.assertEqual(dev['write_scope'], 'ADVISORY')
        self.assertEqual(dev['no_file_write'], 'N/A')
        verify = self.g('ct-reviewer-verify')
        self.assertEqual(verify['no_file_write'], 'ADVISORY')
        self.assertEqual(verify['no_delegate'], 'ADVISORY')
        self.assertEqual(verify['command_limits'], 'ADVISORY')
        self.assertEqual(self.g('ct-test-engineer')['no_delegate'], 'ADVISORY')
        self.assertEqual(self.g('ct-test-engineer')['no_file_write'], 'N/A')
        self.assertEqual(self.g('ct-reviewer')['no_file_write'], 'ENFORCED')
        for variant in self.caps:
            result = self.g(variant)
            self.assertEqual(set(result), {'no_file_write', 'write_scope', 'command_limits', 'no_delegate'})
            for value in self.g(variant, 'codex').values():
                self.assertIn(value, ('ADVISORY', 'N/A'))
        codex_arch = self.g('ct-architect', 'codex')
        self.assertEqual(codex_arch['no_file_write'], 'ADVISORY')
        self.assertEqual(codex_arch['no_delegate'], 'ADVISORY')
        agy = cfg('antigravity')
        researcher = self.caps['ct-researcher']
        self.assertEqual(ct_roles.guarantees(researcher, agy)['no_file_write'], 'ENFORCED')
        self.assertIn('read_url_content', ct_roles.tools_for(researcher, agy))
        self.assertIn('search_web', ct_roles.tools_for(researcher, agy))
        self.assertFalse(ct_roles.inherits_mcp(researcher, agy))
        self.assertTrue(ct_roles.inherits_mcp(self.caps['ct-test-engineer'], agy))
        self.assertFalse(ct_roles.inherits_mcp(self.caps['ct-test-engineer'], cfg()))
        text = rendered('antigravity')['ct-researcher/agent.md']
        self.assertIn('inheritMcp: false\n', frontmatter(text))
        tester = rendered('antigravity')['ct-test-engineer/agent.md']
        self.assertIn('inheritMcp: true\n', frontmatter(tester))

    def test_any_mcp_tool_downgrades_guarantees(self):
        researcher = self.caps['ct-researcher']
        claude = cfg()
        for tool in ('mcp__plugin_context7_context7__query-docs', 'mcp__x__run_command'):
            mcp = {'docs_lookup': [tool]}
            self.assertIn(tool, ct_roles.tools_for(researcher, claude, mcp))
            self.assertEqual(ct_roles.extra_mcp_tools(researcher, mcp), [tool])
            result = ct_roles.guarantees(researcher, claude, mcp)
            self.assertEqual(result['no_file_write'], 'ADVISORY')
            self.assertEqual(result['no_delegate'], 'ADVISORY')
            text = rendered('claude-code', mcp)['ct-researcher.md']
            self.assertIn(tool, frontmatter(text))
            self.assertIn('- no_file_write: ADVISORY\n', text)
            arch = self.caps['ct-architect']
            self.assertEqual(ct_roles.extra_mcp_tools(arch, mcp), [])
            self.assertNotIn(tool, ct_roles.tools_for(arch, claude, mcp))
            self.assertEqual(ct_roles.guarantees(arch, claude, mcp)['no_file_write'], 'ENFORCED')
            self.assertEqual(ct_roles.guarantees(arch, claude, mcp)['no_delegate'], 'ENFORCED')
        plain = ct_roles.guarantees(researcher, claude)
        self.assertEqual(plain['no_file_write'], 'ENFORCED')
        self.assertEqual(plain['no_delegate'], 'ENFORCED')
        self.assertEqual(ct_roles.guarantees(researcher, claude, {})['no_file_write'], 'ENFORCED')
        with self.assertRaises(ValueError):
            ct_roles.extra_mcp_tools(researcher, {'delegate': ['mcp__x__y']})

    def test_mcp_tool_names_must_be_plain_identifiers(self):
        researcher = self.caps['ct-researcher']
        claude = cfg()
        bad_names = ['mcp__x__y, Agent', 'mcp__x__y,Agent', 'mcp__x__y\nmodel: opus',
                     'mcp__x__y\n---\n', 'mcp x', '-mcp', '"mcp"', 'mcp__x__y\r', '']
        for name in bad_names:
            mcp = {'docs_lookup': [name]}
            with self.assertRaises(ValueError, msg=repr(name)):
                ct_roles.extra_mcp_tools(researcher, mcp)
            with self.assertRaises(ValueError, msg=repr(name)):
                ct_roles.tools_for(researcher, claude, mcp)
            with self.assertRaises(ValueError, msg=repr(name)):
                ct_roles.render_all(roles(), claude, mcp)
        ok = 'mcp__plugin_context7_context7__query-docs'
        self.assertEqual(ct_roles.extra_mcp_tools(researcher, {'docs_lookup': [ok]}), [ok])

    def test_antigravity_policy_follows_resolved_tools(self):
        agy = cfg('antigravity')
        catalog = roles()
        for tool in agy['tools']['shell'] + agy['tools']['browser_testing']:
            mcp = {'docs_lookup': [tool]}
            text = ct_roles.render_agent('ct-researcher', 'ct-researcher', catalog, agy, mcp)[1].decode('utf-8')
            head = frontmatter(text)
            self.assertIn(f'  - {tool}\n', head)
            self.assertIn('commandExecutionPolicy: auto\n', head, tool)
            self.assertIn('- no_file_write: ADVISORY\n', text)
        plain = ct_roles.render_agent('ct-researcher', 'ct-researcher', catalog, agy)[1].decode('utf-8')
        self.assertIn('commandExecutionPolicy: off\n', frontmatter(plain))
        other = ct_roles.render_agent('ct-researcher', 'ct-researcher', catalog, agy,
                                      {'docs_lookup': ['mcp__docs__lookup']})[1].decode('utf-8')
        self.assertIn('commandExecutionPolicy: off\n', frontmatter(other))

    def test_read_only_never_gets_shell_or_delegate(self):
        claude = cfg()
        self.assertEqual(ct_roles.tools_for(self.caps['ct-architect'], claude), ['Read', 'Grep', 'Glob'])
        self.assertEqual(ct_roles.tools_for(self.caps['ct-reviewer'], claude), ['Read', 'Grep', 'Glob'])
        self.assertEqual(ct_roles.tools_for(self.caps['ct-test-engineer'], claude),
                         ['Read', 'Grep', 'Glob', 'Edit', 'Write', 'NotebookEdit', 'Bash', 'PowerShell'])
        for platform in ct_platform.PLATFORMS:
            conf = cfg(platform)
            for variant, caps in self.caps.items():
                tools = ct_roles.tools_for(caps, conf)
                self.assertEqual(len(tools), len(set(tools)), (platform, variant))
                for banned in ('Agent', 'invoke_subagent', 'define_subagent'):
                    self.assertNotIn(banned, tools, (platform, variant))
                # Delegate tools are filtered even when smuggled in through MCP.
                delegate = conf['tools']['delegate']
                smuggled = ct_roles.tools_for(caps, conf, {cap: list(delegate) for cap in caps})
                for tool in delegate:
                    self.assertNotIn(tool, smuggled, (platform, variant))
        agy = rendered('antigravity')
        for path, text in agy.items():
            self.assertNotIn('invoke_subagent', frontmatter(text), path)
            self.assertNotIn('define_subagent', frontmatter(text), path)
        for path, text in rendered('claude-code').items():
            tools_line = [l for l in frontmatter(text).splitlines() if l.startswith('tools:')]
            self.assertEqual(len(tools_line), 1, path)
            self.assertNotIn('Agent', tools_line[0], path)


class RenderTests(unittest.TestCase):
    def test_claude_frontmatter(self):
        files = dict(ct_roles.render_all(roles(), cfg()))
        data = files['ct-architect.md']
        self.assertFalse(data.startswith(b'\xef\xbb\xbf'))
        self.assertNotIn(b'\r', data)
        text = data.decode('utf-8')
        self.assertTrue(text.startswith('---\n'))
        head = frontmatter(text)
        self.assertIn('name: ct-architect\n', head)
        self.assertIn('description: "', head)
        self.assertIn('tools: Read, Grep, Glob\n', head)
        self.assertIn('model: opus\n', head)
        self.assertIn('effort: high\n', head)
        researcher = frontmatter(files['ct-researcher.md'].decode('utf-8'))
        self.assertIn('model: haiku\n', researcher)
        self.assertIn('effort: low\n', researcher)
        for path, raw in files.items():
            self.assertNotIn(b'isolation:', raw, path)
            self.assertFalse(raw.startswith(b'\xef\xbb\xbf'), path)
            self.assertNotIn(b'\r', raw, path)
        # A platform that omits static models never writes model/effort.
        omit = copy.deepcopy(cfg())
        omit['static_agent_model'] = 'omit'
        text = ct_roles.render_agent('ct-architect', 'ct-architect', roles(), omit)[1].decode('utf-8')
        self.assertNotIn('model:', frontmatter(text))
        self.assertNotIn('effort:', frontmatter(text))
        # An inherit tier (null model) also writes no model.
        inherit = copy.deepcopy(cfg())
        inherit['roles']['ct-architect']['tier'] = 'inherit'
        text = ct_roles.render_agent('ct-architect', 'ct-architect', roles(), inherit)[1].decode('utf-8')
        self.assertNotIn('model:', frontmatter(text))

    def test_codex_toml_parses_without_model_or_sandbox(self):
        files = dict(ct_roles.render_all(roles(), cfg('codex')))
        self.assertEqual(set(files), {f'{v}.toml' for v in variant_caps(roles())})
        for path, raw in files.items():
            self.assertNotIn(b'\r', raw)
            self.assertFalse(raw.startswith(b'\xef\xbb\xbf'))
            data = tomllib.loads(raw.decode('utf-8'))
            for key in ('name', 'description', 'developer_instructions'):
                self.assertIn(key, data, path)
            for key in ('model', 'model_reasoning_effort', 'sandbox_mode'):
                self.assertNotIn(key, data, path)
            self.assertEqual(path, data['name'] + '.toml')
            self.assertTrue(data['developer_instructions'].startswith(ct_roles.WORKER_CLAUSE))
        catalog = roles()
        tricky_bodies = [
            "Keep ''' and C:\\Users\\x and \\n literally.\nQuote: \"x\" and \"\"\" too.\n",
            "No triple quotes but a backslash \\ and 'single' quotes.\n",
            "Ends with quotes ''\n",
            "Tab\tand unicode Nguyễn and DEL\x7f and NUL-free.\n",
        ]
        for body in tricky_bodies:
            catalog['ct-architect']['body'] = body
            path, raw = ct_roles.render_agent('ct-architect', 'ct-architect', catalog, cfg('codex'))
            instructions = tomllib.loads(raw.decode('utf-8'))['developer_instructions']
            self.assertTrue(instructions.startswith(ct_roles.WORKER_CLAUSE), repr(body))
            self.assertTrue(instructions.endswith('\n' + body), repr(body))

    def test_antigravity_flags(self):
        files = rendered('antigravity')
        self.assertEqual(set(files), {f'{v}/agent.md' for v in variant_caps(roles())})
        for path, text in files.items():
            head = frontmatter(text)
            for line in ('mainAgent: false', 'subagent: true', 'hidden: false',
                         'inheritCustomizations: true'):
                self.assertIn(line + '\n', head, path)
            self.assertNotIn('model:', text.split('\n---\n', 1)[0], path)
            self.assertIn('\ntools:\n', '\n' + head + '\n', path)
        self.assertIn('commandExecutionPolicy: off\n', frontmatter(files['ct-architect/agent.md']))
        self.assertIn('commandExecutionPolicy: off\n', frontmatter(files['ct-reviewer/agent.md']))
        self.assertIn('commandExecutionPolicy: auto\n', frontmatter(files['ct-developer/agent.md']))
        self.assertIn('commandExecutionPolicy: auto\n', frontmatter(files['ct-reviewer-verify/agent.md']))
        self.assertIn('tools:\n  - send_message\n  - view_file\n', frontmatter(files['ct-architect/agent.md']))

    def test_worker_clause_and_guarantees_present(self):
        expected = ('You are a coding-team worker, not the Leader. Leader-only rules, routing policy and '
                    'orchestrator skills (coding-team, subagent-driven-development, dispatching-parallel-agents, '
                    'executing-plans, requesting-code-review) do not apply to you and must not be invoked. '
                    'Never dispatch, spawn or define agents, and never start another AI agent or CLI session '
                    'through a shell. Use technique skills (test-driven-development, systematic-debugging, '
                    'verification-before-completion, receiving-code-review) only when your handoff names them.')
        self.assertEqual(ct_roles.WORKER_CLAUSE, expected)
        catalog = roles()
        caps = variant_caps(catalog)
        for platform in ct_platform.PLATFORMS:
            conf = cfg(platform)
            for role_id, role in catalog.items():
                for variant in role['variants']:
                    _, raw = ct_roles.render_agent(role_id, variant, catalog, conf)
                    text = raw.decode('utf-8')
                    if platform == 'codex':
                        text = tomllib.loads(text)['developer_instructions']
                    else:
                        text = text.split('\n---\n', 1)[1].lstrip('\n')
                    self.assertTrue(text.startswith(expected), (platform, variant))
                    header = f'## Runtime guarantees ({platform})\n'
                    self.assertIn(header, text)
                    self.assertLess(text.index(expected), text.index(header))
                    for key, level in ct_roles.guarantees(caps[variant], conf).items():
                        self.assertIn(f'\n- {key}: {level}\n', text, (platform, variant, key))
                    self.assertIn(role['body'], text)
        with self.assertRaises(ValueError):
            ct_roles.render_agent('ct-architect', 'ct-reviewer-verify', catalog, cfg())
        with self.assertRaises(ValueError):
            ct_roles.render_agent('ct-unknown', 'ct-unknown', catalog, cfg())

    def test_render_all_count(self):
        catalog = roles()
        for platform in ct_platform.PLATFORMS:
            files = ct_roles.render_all(catalog, cfg(platform))
            self.assertEqual(len(files), 7, platform)
            self.assertEqual(len({p for p, _ in files}), 7, platform)
            for path, raw in files:
                self.assertIsInstance(raw, bytes)
                self.assertFalse(Path(path).is_absolute())
                self.assertNotIn('..', Path(path).parts)
                self.assertNotIn('\\', path)
            self.assertEqual(files, ct_roles.render_all(catalog, cfg(platform)))


if __name__ == '__main__':
    unittest.main()
