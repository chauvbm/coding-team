import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import ct_orchestration as orch  # noqa: E402

NOTE = 'unresolved SDD workspaces indicate unfinished progress, not a running process'
BASE_FACTS = {
    'level': 'L2', 'user_choice': None, 'safety_forbids_user_choice': False,
    'architecture_dispute': False, 'multi_contract_change': False, 'has_plan': False,
    'sequential_review_sufficient': False, 'superpowers_available': True, 'independent_issues': 0,
}

NOT_A_REPO = subprocess.CompletedProcess(
    ['git'], 128, '', 'fatal: not a git repository (or any of the parent directories): .git\n')


def make_sdd(workspace, plan='plan-a'):
    progress = Path(workspace) / '.superpowers' / 'sdd' / plan / 'progress.md'
    progress.parent.mkdir(parents=True, exist_ok=True)
    progress.write_text('# ledger\n', encoding='utf-8')
    return progress.resolve().as_posix()


def handoff_record(**overrides):
    record = {
        'task_id': 'task-1', 'reason': 'SDD stopped: blocked on design question',
        'from': 'superpowers-sdd', 'to': 'coding-team',
        'workers': [{'id': 'worker-1', 'terminated': True, 'evidence': 'runtime reports task stopped'}],
        'snapshot': {'head': 'abc1234', 'uncommitted': ['scripts/a.py'], 'untracked': []},
        'tests': {'commands': ['python -m unittest'], 'result': 'pass', 'snapshot': 'abc1234'},
        'findings': ['open: a.py edge case'], 'previous_ledger': '.superpowers/sdd/plan-a/progress.md',
        'acknowledged_sdd': [],
    }
    record.update(overrides)
    return record


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory()
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name).resolve()
        env = mock.patch.dict(os.environ, {'GIT_CEILING_DIRECTORIES': str(self.root.parent)})
        env.start()
        self.addCleanup(env.stop)
        self.ws = self.root / 'workspace'
        self.ws.mkdir()

    def state(self, workspace=None):
        return json.loads(orch.orchestration_file(workspace or self.ws).read_text(encoding='utf-8'))

    def test_unresolved_sdd_blocks_claim_and_is_not_called_running(self):
        progress = make_sdd(self.ws)
        self.assertEqual(orch.unresolved_sdd_workspaces(self.ws), [progress])
        report = orch.status(self.ws, 'task-1')
        self.assertFalse(report['can_claim'])
        self.assertEqual(report['note'], NOTE)
        self.assertEqual(report['unresolved_sdd'], [progress])
        self.assertTrue(any(progress in blocker for blocker in report['blockers']))
        with self.assertRaises(orch.OrchestrationConflict) as caught:
            orch.claim(self.ws, 'task-1', 'AUTO', 'gate-1', session_id='A')
        self.assertIn(progress, caught.exception.blockers)
        self.assertNotIn('task-1', orch.status(self.ws)['tasks'])

    def git(self, *args, cwd):
        subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=30)

    def make_repo(self, name='main'):
        if shutil.which('git') is None:
            self.skipTest('git is not installed')
        main = self.root / name
        main.mkdir()
        self.git('init', '-q', cwd=main)
        self.git('-c', 'user.email=test@example.invalid', '-c', 'user.name=Test',
                 'commit', '-q', '--allow-empty', '-m', 'initial', cwd=main)
        return main

    def test_ownership_shared_across_worktrees(self):
        main = self.make_repo()
        self.git('worktree', 'add', '-q', '../wt-b', cwd=main)
        wt_b = self.root / 'wt-b'
        expected = main / '.coding-team' / 'orchestration.json'
        self.assertEqual(orch.orchestration_file(wt_b).resolve(), expected.resolve())
        self.assertEqual(orch.orchestration_file(main).resolve(), expected.resolve())

        orch.claim(main, 'task-1', 'AUTO', 'gate-1', session_id='A')
        seen = orch.status(wt_b, 'task-1')
        self.assertEqual(seen['tasks']['task-1']['owner_session_id'], 'A')
        self.assertFalse(seen['can_claim'])
        with self.assertRaises(orch.OrchestrationConflict):
            orch.claim(wt_b, 'task-1', 'AUTO', 'gate-1', session_id='B')

        progress = make_sdd(wt_b)
        self.assertIn(progress, orch.unresolved_sdd_workspaces(main))
        with self.assertRaises(orch.OrchestrationConflict):
            orch.claim(main, 'task-2', 'AUTO', 'gate-1', session_id='A')

        self.assertTrue(expected.is_file())
        self.assertFalse(list((main / '.git').rglob('orchestration.json*')))
        self.assertFalse((wt_b / '.coding-team').exists())
        self.assertEqual((main / '.coding-team' / '.gitignore').read_bytes(), b'*\n')

    def test_non_git_workspace_uses_local_dir(self):
        self.assertEqual(orch.orchestration_file(self.ws).resolve(),
                         (self.ws / '.coding-team' / 'orchestration.json').resolve())
        orch.claim(self.ws, 'task-1', 'AUTO', 'gate-1', session_id='A')
        stored = json.loads((self.ws / '.coding-team' / 'orchestration.json').read_text(encoding='utf-8'))
        self.assertEqual(stored['schema_version'], 1)
        self.assertEqual(stored['tasks']['task-1']['orchestrator'], 'coding-team')

    def test_second_session_cannot_reclaim_same_task(self):
        first = orch.claim(self.ws, 'task-1', 'AUTO', 'gate-1', session_id='A')
        self.assertEqual(len(first['token']), 32)
        self.assertEqual(first['generation'], 1)
        self.assertEqual(first['orchestrator'], 'coding-team')
        with self.assertRaises(orch.OrchestrationConflict) as caught:
            orch.claim(self.ws, 'task-1', 'AUTO', 'gate-1', session_id='B')
        self.assertTrue(caught.exception.blockers)
        with self.assertRaises(orch.OrchestrationConflict):
            orch.claim(self.ws, 'task-1', 'AUTO', 'gate-1', session_id='B', token='0' * 32)
        updated = orch.claim(self.ws, 'task-1', 'AUTO', 'implementation', session_id=None, token=first['token'])
        self.assertEqual(updated['phase'], 'implementation')
        self.assertNotIn('token', updated)
        again = orch.claim(self.ws, 'task-1', 'FULL_TEAM', 'gate-2', session_id='A')
        self.assertEqual((again['mode'], again['phase'], again['generation']), ('FULL_TEAM', 'gate-2', 1))
        self.assertEqual(self.state()['tasks']['task-1']['owner_token'], first['token'])

    def test_session_less_owner_requires_token(self):
        first = orch.claim(self.ws, 'task-1', 'AUTO', 'gate-1', session_id='  ')
        self.assertIsNone(first['owner_session_id'])
        for session in (None, '', ' '):
            with self.subTest(session=session):
                with self.assertRaises(orch.OrchestrationConflict):
                    orch.claim(self.ws, 'task-1', 'AUTO', 'gate-2', session_id=session)
        with self.assertRaises(orch.OrchestrationConflict):
            orch.release(self.ws, 'task-1', session_id=None)
        updated = orch.claim(self.ws, 'task-1', 'AUTO', 'gate-2', session_id=None, token=first['token'])
        self.assertEqual(updated['phase'], 'gate-2')

    def test_takeover_requires_user_confirmation(self):
        first = orch.claim(self.ws, 'task-1', 'AUTO', 'gate-1', session_id='A')
        for confirmed in (False, None, 'yes', 1):
            with self.subTest(confirmed=confirmed):
                with self.assertRaises(ValueError):
                    orch.takeover(self.ws, 'task-1', session_id='B', reason='A lost', user_confirmed=confirmed)
        self.assertEqual(self.state()['tasks']['task-1']['generation'], 1)
        taken = orch.takeover(self.ws, 'task-1', session_id='B', reason='session A lost', user_confirmed=True)
        self.assertEqual(taken['generation'], 2)
        self.assertEqual(taken['owner_session_id'], 'B')
        self.assertNotEqual(taken['token'], first['token'])
        with self.assertRaises(orch.OrchestrationConflict):
            orch.claim(self.ws, 'task-1', 'AUTO', 'gate-2', session_id=None, token=first['token'])
        with self.assertRaises(orch.OrchestrationConflict):
            orch.claim(self.ws, 'task-1', 'AUTO', 'gate-2', session_id='A')
        self.assertEqual(orch.claim(self.ws, 'task-1', 'AUTO', 'gate-2', session_id=None,
                                    token=taken['token'])['phase'], 'gate-2')
        history = self.state()['tasks']['task-1']['history']
        self.assertEqual(len(history), 1)
        self.assertEqual({k: history[0][k] for k in ('generation', 'owner_session_id', 'orchestrator', 'reason')},
                         {'generation': 1, 'owner_session_id': 'A', 'orchestrator': 'coding-team',
                          'reason': 'session A lost'})
        self.assertIn('at', history[0])
        self.assertNotIn(first['token'], json.dumps(history))

    def test_status_never_exposes_token(self):
        first = orch.claim(self.ws, 'task-1', 'AUTO', 'gate-1', session_id='A')
        handed = orch.handoff(self.ws, handoff_record(
            **{'from': 'coding-team', 'to': 'superpowers-dpa', 'token': first['token']}), session_id=None)
        self.assertEqual(len(handed['token']), 32)
        taken = orch.takeover(self.ws, 'task-1', session_id='C', reason='resume', user_confirmed=True)
        report = orch.status(self.ws, 'task-1')
        text = json.dumps(report)
        for token in (first['token'], handed['token'], taken['token'],
                      self.state()['tasks']['task-1']['owner_token']):
            self.assertNotIn(token, text)
        self.assertNotIn('owner_token', text)
        view = report['tasks']['task-1']
        self.assertEqual((view['owner_session_id'], view['generation'], view['has_token']), ('C', 3, True))
        stored = orch.orchestration_file(self.ws).read_text(encoding='utf-8')
        self.assertNotIn(first['token'], stored)

    def test_handoff_validation(self):
        progress = make_sdd(self.ws)
        unresolved = [progress]
        valid = handoff_record(user_confirmed=True)
        orch.validate_handoff(valid, None, unresolved, session_id=None)

        def broken(mutate):
            record = copy.deepcopy(valid)
            mutate(record)
            return record

        cases = {
            'evidence': broken(lambda r: r['workers'][0].pop('evidence')),
            'terminated': broken(lambda r: r['workers'][0].update(terminated=False)),
            'untracked': broken(lambda r: r['snapshot'].pop('untracked')),
            'reason': broken(lambda r: r.pop('reason')),
            'to': broken(lambda r: r.update(to='superpowers-sdd')),
            'previous_ledger': broken(lambda r: r.pop('previous_ledger')),
        }
        for field, record in cases.items():
            with self.subTest(field=field):
                with self.assertRaises(ValueError) as caught:
                    orch.validate_handoff(record, None, unresolved, session_id=None)
                self.assertIn(field, str(caught.exception))

        with self.assertRaises(ValueError) as caught:
            orch.validate_handoff(handoff_record(), None, unresolved, session_id=None)
        self.assertIn('user_confirmed', str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            orch.validate_handoff(handoff_record(user_confirmed=True, acknowledged_sdd=['/elsewhere/progress.md']),
                                  None, unresolved, session_id=None)
        self.assertIn('acknowledged_sdd', str(caught.exception))

        (self.ws / 'other').mkdir()
        first = orch.claim(self.ws / 'other', 'task-1', 'AUTO', 'gate-1', session_id='A')
        current = self.state(self.ws / 'other')['tasks']['task-1']
        owned = handoff_record(**{'from': 'coding-team', 'to': 'superpowers-sdd'})
        with self.assertRaises(orch.OrchestrationConflict):
            orch.validate_handoff(owned, current, unresolved, session_id='B')
        with self.assertRaises(orch.OrchestrationConflict):
            orch.validate_handoff(dict(owned, user_confirmed=True), current, unresolved, session_id=None)
        with self.assertRaises(orch.OrchestrationConflict):
            orch.validate_handoff(dict(owned, **{'from': 'superpowers-dpa'}, token=first['token']),
                                  current, unresolved, session_id='A')
        orch.validate_handoff(dict(owned, token=first['token']), current, unresolved, session_id='B')
        orch.validate_handoff(owned, current, unresolved, session_id='A')
        with self.assertRaises(orch.OrchestrationConflict):
            orch.handoff(self.ws / 'other', owned, session_id='B')

    def test_valid_handoff_then_claim(self):
        progress = make_sdd(self.ws)
        with self.assertRaises(orch.OrchestrationConflict):
            orch.claim(self.ws, 'task-1', 'AUTO', 'gate-1', session_id='L')
        result = orch.handoff(self.ws, handoff_record(user_confirmed=True, acknowledged_sdd=[progress]),
                              session_id=None)
        self.assertEqual(result['termination_evidence'], 'caller-asserted')
        self.assertIs(result['from_verified'], False)
        self.assertEqual(result['orchestrator'], 'coding-team')
        self.assertEqual(result['generation'], 1)
        self.assertEqual(len(result['token']), 32)
        report = orch.status(self.ws, 'task-1')
        self.assertEqual(report['unresolved_sdd'], [])
        self.assertEqual(report['tasks']['task-1']['handoff_from'], 'superpowers-sdd')
        self.assertEqual(report['tasks']['task-1']['acknowledged_sdd'], [progress])
        claimed = orch.claim(self.ws, 'task-1', 'AUTO', 'gate-1', session_id=None, token=result['token'])
        self.assertEqual((claimed['mode'], claimed['phase']), ('AUTO', 'gate-1'))
        with self.assertRaises(orch.OrchestrationConflict):
            orch.claim(self.ws, 'task-1', 'AUTO', 'gate-1', session_id='other')

        back = orch.handoff(self.ws, handoff_record(**{'from': 'coding-team', 'to': 'superpowers-sdd',
                                                     'token': result['token']}), session_id=None)
        self.assertIs(back['from_verified'], True)
        self.assertNotEqual(back['token'], result['token'])
        self.assertEqual(back['generation'], 2)
        stored = self.state()['tasks']['task-1']
        self.assertNotIn('token', stored['handoff_record'])
        self.assertEqual(stored['history'][0]['generation'], 1)
        with self.assertRaises(orch.OrchestrationConflict):
            orch.claim(self.ws, 'task-1', 'AUTO', 'gate-2', session_id=None, token=result['token'])

    def test_sessionless_round_trip_through_sdd(self):
        first = orch.claim(self.ws, 'task-1', 'AUTO', 'gate-1', session_id=None)
        to_sdd = orch.handoff(self.ws, handoff_record(**{'from': 'coding-team', 'to': 'superpowers-sdd',
                                                       'token': first['token']}), session_id=None)
        self.assertEqual((to_sdd['orchestrator'], to_sdd['owner_session_id'], to_sdd['from_verified']),
                         ('superpowers-sdd', None, True))
        with self.assertRaises(orch.OrchestrationConflict):
            orch.handoff(self.ws, handoff_record(user_confirmed=True), session_id=None)
        back = orch.handoff(self.ws, handoff_record(token=to_sdd['token']), session_id=None)
        self.assertEqual((back['orchestrator'], back['generation'], back['from_verified']),
                         ('coding-team', 3, True))
        with self.assertRaises(orch.OrchestrationConflict):
            orch.claim(self.ws, 'task-1', 'AUTO', 'gate-2', session_id=None, token=to_sdd['token'])
        claimed = orch.claim(self.ws, 'task-1', 'AUTO', 'gate-2', session_id=None, token=back['token'])
        self.assertEqual(claimed['phase'], 'gate-2')
        self.assertEqual([entry['orchestrator'] for entry in self.state()['tasks']['task-1']['history']],
                         ['coding-team', 'superpowers-sdd'])

    def test_git_failures_fail_closed(self):
        main = self.make_repo()
        self.git('worktree', 'add', '-q', '../wt', cwd=main)
        wt = self.root / 'wt'
        orch.claim(main, 'task-1', 'AUTO', 'gate-1', session_id='A')
        dubious = subprocess.CompletedProcess(
            ['git'], 128, '', "fatal: detected dubious ownership in repository at '%s'\n" % wt)
        failures = {
            'dubious ownership': dict(return_value=dubious),
            'timeout': dict(side_effect=subprocess.TimeoutExpired(['git'], 10)),
            'permission': dict(side_effect=PermissionError('denied')),
            'not-a-repo with .git present': dict(return_value=NOT_A_REPO),
            'git missing with .git present': dict(side_effect=FileNotFoundError('git')),
        }
        for label, behaviour in failures.items():
            with self.subTest(failure=label):
                with mock.patch.object(orch.subprocess, 'run', **behaviour):
                    with self.assertRaises(orch.OrchestrationLocationError) as caught:
                        orch.claim(wt, 'task-1', 'AUTO', 'gate-1', session_id='B')
                    with self.assertRaises(orch.OrchestrationLocationError):
                        orch.status(wt, 'task-1')
                if label == 'dubious ownership':
                    self.assertIn('dubious ownership', str(caught.exception))
                self.assertFalse((wt / '.coding-team').exists())
        self.assertEqual(self.state(main)['tasks']['task-1']['owner_session_id'], 'A')

    def test_missing_git_or_not_a_repo_outside_any_repo_falls_back(self):
        expected = (self.ws / '.coding-team' / 'orchestration.json').resolve()
        for label, behaviour in {'git missing': dict(side_effect=FileNotFoundError('git')),
                                 'not a repo': dict(return_value=NOT_A_REPO)}.items():
            with self.subTest(case=label):
                with mock.patch.object(orch.subprocess, 'run', **behaviour):
                    self.assertEqual(orch.orchestration_file(self.ws).resolve(), expected)
                    self.assertEqual(orch.unresolved_sdd_workspaces(self.ws), [])
        with mock.patch.object(orch.subprocess, 'run', side_effect=FileNotFoundError('git')):
            orch.claim(self.ws, 'task-1', 'AUTO', 'gate-1', session_id='A')
        self.assertTrue(expected.is_file())

    def test_bare_main_worktree_rejected(self):
        source = self.make_repo('source')
        self.git('clone', '-q', '--bare', str(source), str(self.root / 'main.git'), cwd=self.root)
        self.git('worktree', 'add', '-q', str(self.root / 'wt'), cwd=self.root / 'main.git')
        with self.assertRaises(orch.OrchestrationLocationError) as caught:
            orch.claim(self.root / 'wt', 'task-1', 'AUTO', 'gate-1', session_id='A')
        self.assertIn('bare main worktree unsupported', str(caught.exception))
        self.assertFalse(list(self.root.rglob('.coding-team')))

    def test_route_order(self):
        def route(**facts):
            return orch.route(dict(BASE_FACTS, **facts))

        cases = [
            (0, 'superpowers-sdd', dict(level='L3', user_choice='superpowers-sdd')),
            (1, 'coding-team', dict(level='L3', user_choice='leader-direct', safety_forbids_user_choice=True)),
            (1, 'coding-team', dict(level='L3', has_plan=True, sequential_review_sufficient=True)),
            (2, 'coding-team', dict(architecture_dispute=True, independent_issues=3)),
            (3, 'coding-team', dict(level='L1', multi_contract_change=True, has_plan=True)),
            (4, 'superpowers-dpa', dict(independent_issues=2, has_plan=True, sequential_review_sufficient=True)),
            (5, 'superpowers-sdd', dict(level='L1', has_plan=True, sequential_review_sufficient=True)),
            (6, 'leader-direct', dict(level='L0')),
            (6, 'leader-direct', dict(level='L1', independent_issues=1)),
            (7, 'coding-team', dict(level='L2', has_plan=True)),
        ]
        for rule, expected, facts in cases:
            with self.subTest(rule=rule, facts=facts):
                result = route(**facts)
                self.assertEqual((result['rule'], result['orchestrator']), (rule, expected))
                self.assertTrue(result['reason'])
        self.assertEqual(sorted({rule for rule, _, _ in cases}), list(range(8)))

        rename = route(level='L1')
        self.assertEqual((rename['orchestrator'], rename['rule']), ('leader-direct', 6))
        auth = route(level='L3')
        self.assertEqual((auth['orchestrator'], auth['rule']), ('coding-team', 1))

        no_sp = route(superpowers_available=False, independent_issues=2)
        self.assertEqual((no_sp['orchestrator'], no_sp['rule']), ('coding-team', 4))
        no_sp = route(superpowers_available=False, has_plan=True, sequential_review_sufficient=True)
        self.assertEqual((no_sp['orchestrator'], no_sp['rule']), ('coding-team', 5))

        for key in BASE_FACTS:
            with self.subTest(missing=key):
                facts = dict(BASE_FACTS)
                facts.pop(key)
                with self.assertRaises(ValueError) as caught:
                    orch.route(facts)
                self.assertIn(key, str(caught.exception))
        bad = [dict(level='L4'), dict(user_choice='sdd'), dict(has_plan='yes'),
               dict(independent_issues=-1), dict(independent_issues=True)]
        for facts in bad:
            with self.subTest(bad=facts):
                with self.assertRaises(ValueError):
                    route(**facts)

    def test_gitignore_and_release(self):
        coding_dir = self.ws / '.coding-team'
        self.assertFalse(coding_dir.exists())
        orch.status(self.ws)
        self.assertFalse(coding_dir.exists())
        first = orch.claim(self.ws, 'task-1', 'AUTO', 'gate-1', session_id='A')
        self.assertEqual((coding_dir / '.gitignore').read_bytes(), b'*\n')
        (coding_dir / '.gitignore').write_bytes(b'*\n# user note\n')
        with self.assertRaises(orch.OrchestrationConflict):
            orch.release(self.ws, 'task-1', session_id='B')
        with self.assertRaises(orch.OrchestrationConflict):
            orch.release(self.ws, 'task-1', session_id=None, token='f' * 32)
        released = orch.release(self.ws, 'task-1', session_id=None, token=first['token'])
        self.assertTrue(released['released'])
        self.assertNotIn('task-1', orch.status(self.ws)['tasks'])
        self.assertTrue(orch.status(self.ws, 'task-1')['can_claim'])
        second = orch.claim(self.ws, 'task-1', 'AUTO', 'gate-1', session_id='B')
        self.assertTrue(orch.release(self.ws, 'task-1', session_id='B')['released'])
        self.assertNotEqual(second['token'], first['token'])
        self.assertEqual((coding_dir / '.gitignore').read_bytes(), b'*\n# user note\n')
        self.assertFalse(orch.release(self.ws, 'missing', session_id='B')['released'])

    def test_corrupt_state_fails_closed(self):
        path = orch.orchestration_file(self.ws)
        path.parent.mkdir(parents=True)
        path.write_bytes(b'{not json')
        with self.assertRaises(ValueError):
            orch.claim(self.ws, 'task-1', 'AUTO', 'gate-1', session_id='A')
        with self.assertRaises(ValueError):
            orch.status(self.ws)
        self.assertEqual(path.read_bytes(), b'{not json')

    def test_concurrent_claims_have_exactly_one_winner(self):
        script = (
            'import sys; sys.path.insert(0, sys.argv[1]); import ct_orchestration as o\n'
            'try:\n'
            '    o.claim(sys.argv[2], "task-race", "AUTO", "gate-1", session_id=sys.argv[3]); print("WON")\n'
            'except o.OrchestrationConflict:\n'
            '    print("LOST")\n'
        )
        processes = [subprocess.Popen([sys.executable, '-c', script, str(ROOT / 'scripts'), str(self.ws), 'S%d' % i],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for i in range(6)]
        outputs = []
        for process in processes:
            out, err = process.communicate(timeout=60)
            self.assertEqual(process.returncode, 0, err)
            outputs.append(out.strip())
        self.assertEqual(sorted(outputs), ['LOST'] * 5 + ['WON'])
        self.assertEqual(self.state()['tasks']['task-race']['generation'], 1)


if __name__ == '__main__':
    unittest.main()
