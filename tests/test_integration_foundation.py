"""Cross-module integration checks for ct_platform + ct_roles (Checkpoint A)."""
from pathlib import Path, PurePosixPath, PureWindowsPath
import sys
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import ct_platform
import ct_roles

ROLES_DIR = ct_platform.REPO_ROOT / 'agents' / 'roles'


def parse_frontmatter(data: bytes) -> dict:
    lines = data.decode('utf-8').split('\n')
    assert lines[0] == '---', 'missing frontmatter start'
    end = lines.index('---', 1)
    result, current = {}, None
    for line in lines[1:end]:
        if line.startswith('  - '):
            result.setdefault(current, []).append(line[4:].strip().strip('"'))
        elif ':' in line:
            key, _, value = line.partition(':')
            current = key.strip()
            value = value.strip()
            result[current] = value if value else []
    return result


def rendered_tools(cfg, relpath, data):
    fmt = cfg['agent_format']
    if fmt == 'claude-md':
        value = parse_frontmatter(data).get('tools', '')
        return [t.strip() for t in value.split(',') if t.strip()]
    if fmt == 'antigravity-md':
        value = parse_frontmatter(data).get('tools', [])
        return value if isinstance(value, list) else []
    return None  # codex-toml has no tool list


class FoundationIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.roles = ct_roles.load_roles(ROLES_DIR)
        cls.cfgs = {n: ct_platform.load_platform(n) for n in ct_platform.PLATFORMS}

    def each(self):
        return [(name, cfg, role_id, variant_id, caps)
                for name, cfg in self.cfgs.items()
                for role_id in ct_platform.ROLES
                for variant_id, caps in self.roles[role_id]['variants'].items()]

    def test_guarantees_consistent_with_rendered_tools(self):
        for name, cfg, role_id, variant_id, caps in self.each():
            with self.subTest(platform=name, variant=variant_id):
                relpath, data = ct_roles.render_agent(role_id, variant_id, self.roles, cfg)
                tools = rendered_tools(cfg, relpath, data)
                if tools is None:
                    tools = ct_roles.tools_for(caps, cfg)
                risky = set(cfg['tools']['shell']) | set(cfg['tools']['write']) \
                    | set(cfg['tools']['browser_testing'])
                g = ct_roles.guarantees(caps, cfg)
                if risky & set(tools):
                    self.assertNotEqual(g['no_file_write'], 'ENFORCED')
                    self.assertNotEqual(g['no_delegate'], 'ENFORCED')

    def test_rendered_tier_matches_platform_config(self):
        for name, cfg, role_id, variant_id, caps in self.each():
            with self.subTest(platform=name, variant=variant_id):
                relpath, data = ct_roles.render_agent(role_id, variant_id, self.roles, cfg)
                if cfg['agent_format'] == 'claude-md':
                    fm = parse_frontmatter(data)
                    tier = ct_platform.resolve_tier(cfg, role_id)
                    if tier['model'] is None:
                        self.assertNotIn('model', fm)
                        self.assertNotIn('effort', fm)
                    else:
                        self.assertEqual(fm.get('model'), tier['model'])
                        if tier['effort'] is None:
                            self.assertNotIn('effort', fm)
                        else:
                            self.assertEqual(fm.get('effort'), tier['effort'])
                elif cfg['agent_format'] == 'codex-toml':
                    parsed = tomllib.loads(data.decode('utf-8'))
                    self.assertNotIn('model', parsed)
                    self.assertNotIn('model_reasoning_effort', parsed)
                else:
                    fm = parse_frontmatter(data)
                    self.assertNotIn('model', fm)
                    self.assertNotIn('effort', fm)

    def test_every_role_has_tier_and_body(self):
        for name, cfg in self.cfgs.items():
            for role_id in ct_platform.ROLES:
                with self.subTest(platform=name, role=role_id):
                    self.assertIn(role_id, self.roles)
                    self.assertIn(cfg['roles'][role_id]['tier'], ct_platform.TIERS)
                    self.assertTrue(self.roles[role_id]['body'].strip())
                    self.assertEqual(ct_platform.resolve_tier(cfg, role_id)['tier'],
                                     cfg['roles'][role_id]['tier'])

    def test_rendered_paths_are_relative_and_safe(self):
        for name, cfg in self.cfgs.items():
            files = ct_roles.render_all(self.roles, cfg)
            self.assertTrue(files)
            for relpath, _ in files:
                with self.subTest(platform=name, path=relpath):
                    self.assertFalse(PurePosixPath(relpath).is_absolute())
                    self.assertFalse(PureWindowsPath(relpath).is_absolute())
                    self.assertFalse(PureWindowsPath(relpath).drive)
                    self.assertNotIn('..', PurePosixPath(relpath).parts)
                    self.assertNotIn('..', PureWindowsPath(relpath).parts)
                    self.assertNotIn(chr(92), relpath)


if __name__ == '__main__':
    unittest.main()
