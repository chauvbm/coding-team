"""Orchestration fixture tests.

Three kinds of check are kept apart (each docstring names its kind):

(a) fixture and schema: the fixture does not contradict itself or the checker;
(b) Python logic: ``ct_orchestration.route(facts)`` returns the expected orchestrator;
(c) agent behaviour: NOT run in CI. It belongs to the blind smoke and evaluation runs on real runtimes.
"""
import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))

import ct_orchestration as orch  # noqa: E402
from check_behavioral_results import check_results  # noqa: E402

CASES_PATH = ROOT / 'tests' / 'orchestration_cases.json'
FACTS_PATH = ROOT / 'tests' / 'orchestration_facts.json'

REQUIRED_IDS = {
    'rename_symbol_10_files', 'auth_change_2_files_l3', 'planned_low_risk_sequential',
    'planned_low_risk_no_superpowers', 'three_independent_failures', 'architecture_dispute',
    'user_names_orchestrator', 'active_sdd_workspace', 'handoff_missing_worker_evidence',
    'worker_requests_sdd', 'l3_missing_security_report', 'l3_security_report_stale_snapshot',
}
OPERATIONS = {'equals', 'one_of', 'lte'}


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def golden_response(case):
    """Build the response a perfect evaluator would give, using only what the case's checks require."""
    response = {'id': case['id'], 'action': 'hold', 'gate': 'NOT_REQUIRED', 'operating_state': 'ACTIVE',
                'can_write_affected': True, 'ask_user': False, 'reset_counters': False,
                'repeat_arguments': False, 'rationale': 'golden response for ' + case['id']}
    checks = case['checks']
    for field, choices in checks.get('one_of', {}).items():
        response[field] = choices[0]
    for field, value in checks.get('equals', {}).items():
        response[field] = value
    for field, limit in checks.get('lte', {}).items():
        response[field] = limit
    return response


class OrchestrationFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = load(CASES_PATH)
        cls.cases = cls.fixture['cases']
        cls.facts = load(FACTS_PATH)

    def test_fixture_shape_and_unique_ids(self):
        """(a) fixture/schema: ids unique and complete, checks well formed, orchestrator values valid."""
        self.assertEqual({'schema_version', 'instructions', 'cases'}, set(self.fixture))
        self.assertEqual(1, self.fixture['schema_version'])
        self.assertTrue(self.fixture['instructions'].strip())
        ids = [case['id'] for case in self.cases]
        self.assertEqual(len(ids), len(set(ids)), 'duplicate case ids')
        self.assertEqual(REQUIRED_IDS, set(ids))
        self.assertEqual(12, len(ids))
        for case in self.cases:
            with self.subTest(case=case['id']):
                self.assertEqual({'id', 'input', 'checks'}, set(case))
                self.assertTrue(case['input'].strip())
                self.assertTrue(case['checks'])
                self.assertLessEqual(set(case['checks']), OPERATIONS)
                for fields in case['checks'].values():
                    self.assertTrue(fields)
                for field, choices in case['checks'].get('one_of', {}).items():
                    self.assertIsInstance(choices, list)
                    self.assertTrue(choices)
                    if field == 'orchestrator':
                        self.assertLessEqual(set(choices), set(orch.ORCHESTRATORS))
                expected = case['checks'].get('equals', {}).get('orchestrator')
                if expected is not None:
                    self.assertIn(expected, orch.ORCHESTRATORS)

    def test_active_sdd_workspace_expectations(self):
        """(a) fixture/schema: unresolved SDD progress is a HOLD, not evidence of a running process."""
        case = next(item for item in self.cases if item['id'] == 'active_sdd_workspace')
        equals = case['checks']['equals']
        self.assertIs(False, equals['accept_task'])
        self.assertIs(True, equals['ask_user'])
        self.assertEqual(['hold'], case['checks']['one_of']['action'])

    def test_golden_responses_satisfy_checker(self):
        """Validates fixture/checker consistency only; does not evaluate agent behaviour."""
        golden = [golden_response(case) for case in self.cases]
        self.assertEqual([], check_results(self.cases, golden))
        # The checker must also be able to reject a wrong answer for an orchestrator case.
        wrong = copy.deepcopy(golden)
        target = next(item for item in wrong if item['id'] == 'auth_change_2_files_l3')
        target['orchestrator'] = 'leader-direct'
        self.assertTrue(any('auth_change_2_files_l3' in failure for failure in check_results(self.cases, wrong)))

    def test_route_matches_fixture_expectations(self):
        """(b) Python logic: route(facts) gives the orchestrator the fixture expects for each fact set."""
        by_id = {case['id']: case for case in self.cases}
        for case_id, facts in self.facts.items():
            with self.subTest(case=case_id):
                self.assertIn(case_id, by_id)
                expected = by_id[case_id]['checks']['equals']['orchestrator']
                self.assertEqual(expected, orch.route(facts)['orchestrator'])

    def test_facts_cover_every_orchestrator_case(self):
        """(b) Python logic: every case that checks an orchestrator has facts, and nothing else has."""
        with_orchestrator = {case['id'] for case in self.cases
                             if 'orchestrator' in case['checks'].get('equals', {})}
        self.assertTrue(with_orchestrator)
        self.assertEqual(with_orchestrator, set(self.facts))


if __name__ == '__main__':
    unittest.main()
