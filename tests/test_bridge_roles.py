"""Structured role assignment contract tests."""
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import ct_bridge_roles as bridge_roles


class BridgeRoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = bridge_roles.load_contract(ROOT)

    def assignment(self, variant, **overrides):
        value = {
            'variant': variant,
            'work_item_id': 'item-1',
            'depends_on': {},
            'snapshot': {'head': 'a' * 40, 'inputs': {'src/main.py': 'b' * 64}},
            'read_paths': ['src/main.py'],
            'write_paths': [],
            'check_outputs': [],
            'test_paths': [],
        }
        value.update(overrides)
        return value

    def test_catalog_variants_derive_capabilities_and_kind(self):
        expectations = {
            'ct-architect': ('READ', 'ct-architect'),
            'ct-researcher': ('READ', 'ct-researcher'),
            'ct-developer': ('EDIT', 'ct-developer'),
            'ct-test-engineer': ('EDIT', 'ct-test-engineer'),
            'ct-reviewer': ('READ', 'ct-reviewer'),
            'ct-reviewer-verify': ('READ', 'ct-reviewer'),
            'ct-security': ('READ', 'ct-security'),
        }
        for variant, (kind, role) in expectations.items():
            with self.subTest(variant=variant):
                raw = self.assignment(variant)
                if variant == 'ct-developer':
                    raw['write_paths'] = ['src/main.py']
                elif variant == 'ct-test-engineer':
                    raw['test_paths'] = ['tests/test_x.py']
                    raw['write_paths'] = ['tests/test_x.py']
                elif variant == 'ct-reviewer-verify':
                    raw['check_outputs'] = ['test-results.txt']
                assignment = bridge_roles.validate_assignment(
                    self.contract, raw, verified_model=None)
                self.assertEqual(assignment.variant, variant)
                self.assertEqual(assignment.role, role)
                self.assertEqual(bridge_roles.assignment_kind(assignment), kind)
                self.assertEqual(assignment.capabilities,
                                 self.contract.roles[role]['variants'][variant])
                serialized = bridge_roles.assignment_to_dict(assignment)
                if variant in ('ct-architect', 'ct-researcher', 'ct-reviewer', 'ct-security'):
                    self.assertEqual(serialized['desktop_guarantees']['no_file_write'], 'ADVISORY')
                    self.assertEqual(serialized['desktop_guarantees']['no_delegate'], 'ADVISORY')
                if variant == 'ct-reviewer-verify':
                    self.assertEqual(serialized['desktop_guarantees']['no_file_write'], 'ADVISORY')
                    self.assertEqual(serialized['desktop_guarantees']['no_delegate'], 'ADVISORY')
                    self.assertEqual(serialized['desktop_guarantees']['command_limits'], 'ADVISORY')
                self.assertEqual(assignment.verified_model, 'unverified')
                self.assertEqual(serialized['variant'], variant)

    def test_eligible_scope_checks_and_pins(self):
        verify = bridge_roles.validate_assignment(self.contract, self.assignment(
            'ct-reviewer-verify', check_outputs=['test-results.txt'], read_paths=['src/main.py']),
            verified_model=None)
        self.assertEqual(verify.check_outputs, ('test-results.txt',))
        self.assertEqual(verify.role, 'ct-reviewer')
        reviewer = bridge_roles.validate_assignment(self.contract, self.assignment(
            'ct-reviewer', review_scope=['src/main.py']), verified_model=None)
        self.assertEqual(reviewer.review_scope, ('src/main.py',))
        pinned = self.assignment('ct-developer', required_model='model-x',
                                 write_paths=['src/main.py'])
        with self.assertRaises(ValueError):
            bridge_roles.validate_assignment(self.contract, pinned, verified_model=None)
        accepted = bridge_roles.validate_assignment(self.contract, pinned, verified_model='model-x')
        self.assertEqual(accepted.verified_model, 'model-x')
        self.assertEqual(bridge_roles.assignment_kind(accepted), 'EDIT')

    def test_ineligible_variant_scope_or_untrusted_fields_are_rejected(self):
        cases = [
            self.assignment('ct-reviewer', write_paths=['src/main.py']),
            self.assignment('ct-reviewer', check_outputs=['test-results.txt']),
            self.assignment('ct-test-engineer', write_paths=['src/main.py'], test_paths=['tests/test_x.py']),
            self.assignment('ct-test-engineer', write_paths=['src/main.py'], test_paths=['tests/test_x.py']),
            self.assignment('ct-unknown'),
            self.assignment('ct-developer', write_paths=['../outside.py']),
            self.assignment('ct-developer', write_paths=['C:/outside.py']),
            self.assignment('ct-developer', write_paths=['src/main.py'], extra_capabilities=['shell']),
            self.assignment('ct-security', review_scope=['src/other.py']),
            self.assignment('ct-developer', required_model='model-x'),
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    bridge_roles.validate_assignment(self.contract, raw, verified_model=None)

    def test_dependencies_snapshot_and_payload_shape_fail_closed(self):
        malformed = [
            self.assignment('ct-developer', snapshot={'head': 'bad', 'inputs': {}}),
            self.assignment('ct-developer', snapshot={'head': 'a' * 40, 'inputs': {'x': 'bad'}}),
            self.assignment('ct-developer', depends_on={'item-2': '../acceptance'}),
            self.assignment('ct-developer', depends_on={'item-1': 'acceptance-1'}),
            self.assignment('ct-developer', unexpected='ignored?'),
            self.assignment('ct-reviewer', review_scope=['src/main.py'], read_paths=[]),
        ]
        for raw in malformed:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    bridge_roles.validate_assignment(self.contract, raw, verified_model=None)


if __name__ == '__main__':
    unittest.main()
