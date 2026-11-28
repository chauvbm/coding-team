"""Offline Desktop bridge acceptance checks through real installed subprocesses.

No Desktop application is launched. Each case owns a fresh HOME, native policy,
Git checkout, and local Python listener, and never uses the invoking chat's state.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
SOURCE_CLI = ROOT / 'scripts/coding_team.py'


class DesktopBridgeIntegrationTests(unittest.TestCase):
    def setUp(self):
        if shutil.which('git') is None:
            self.skipTest('real Git is required')
        temporary = tempfile.TemporaryDirectory(prefix='ct-desktop-integration-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / "home Tiếng Việt's"
        self.workspace = self.base / "checkout Tiếng Việt's"
        self.home.mkdir()
        self.workspace.mkdir()
        # An allowlist of OS plumbing avoids passing any chat identity, app
        # settings, Git overrides, credentials, or inherited Python import paths.
        allowed = {'PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'COMSPEC',
                   'SYSTEMDRIVE', 'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE'}
        self.env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        self.env.update(HOME=str(self.home), USERPROFILE=str(self.home),
                        CODEX_HOME=str(self.home / 'custom-codex'),
                        APPDATA=str(self.home / 'AppData/Roaming'),
                        LOCALAPPDATA=str(self.home / 'AppData/Local'),
                        XDG_CONFIG_HOME=str(self.home / '.config'),
                        TEMP=str(self.base), TMP=str(self.base),
                        PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1',
                        GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=str(self.home / '.gitconfig'))
        self.git('init', '-q')
        (self.workspace / 'assigned.txt').write_bytes(b'original fixture source\n')
        self.git('add', 'assigned.txt')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture baseline')
        self.settings = self.home / '.gemini/settings.json'
        self.settings.parent.mkdir(parents=True)
        self.settings.write_bytes(b'{"fixture_setting":"preserve me"}\n')
        self.install('codex', '--default', 'on')
        self.policy = self.home / 'custom-codex/coding-team/state.json'
        policy_before_worker_install = self.policy.read_bytes()
        self.install('antigravity', '--desktop-worker')
        self.assertEqual(self.policy.read_bytes(), policy_before_worker_install)
        self.leader_cli = self.home / '.agents/skills/coding-team/scripts/coding_team.py'
        self.worker_cli = self.home / '.gemini/config/skills/coding-team-desktop-worker/scripts/ct_bridge_worker.py'
        self.assertTrue(self.leader_cli.is_file())
        self.assertTrue(self.worker_cli.is_file())
        self.protected = {self.policy: self.policy.read_bytes(),
                          self.settings: self.settings.read_bytes(),
                          self.workspace / 'assigned.txt': b'original fixture source\n'}
        # Pin installed artifacts: neither component may mutate its own runtime.
        for skill in (self.leader_cli.parents[1], self.worker_cli.parents[1]):
            manifest = skill / 'install-manifest.json'
            self.protected[manifest] = manifest.read_bytes()
            record = json.loads(manifest.read_bytes())
            for relative in record['files']['skill_dir']:
                path = skill / relative
                self.protected[path] = path.read_bytes()
        self.addCleanup(self.assert_protected)

    def git(self, *args):
        result = subprocess.run(['git', *args], cwd=self.workspace, env=self.env,
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        return result.stdout

    def assert_protected(self):
        for path, original in self.protected.items():
            self.assertEqual(path.read_bytes(), original, str(path.relative_to(self.base)))

    def assert_fixture_paths(self, value):
        if isinstance(value, dict):
            for key, child in value.items():
                if isinstance(child, str) and (key.endswith('_path') or key in
                        {'workspace', 'helper', 'manifest', 'rules_file', 'backup_dir'}):
                    path = Path(child)
                    if path.is_absolute():
                        self.assertTrue(path.resolve().is_relative_to(self.base),
                                        'helper emitted a path outside its fixture')
                self.assert_fixture_paths(child)
        elif isinstance(value, list):
            for child in value:
                self.assert_fixture_paths(child)

    def spawn(self, script, args, *, chat=None, worker=False):
        env = dict(self.env)
        if chat is not None:
            env['ANTIGRAVITY_CONVERSATION_ID' if worker else 'CODEX_THREAD_ID'] = chat
        return subprocess.Popen([sys.executable, str(script), *map(str, args)],
                                cwd=self.workspace, env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def finish_process(self, process, payload=None, timeout=20):
        try:
            stdout, stderr = process.communicate(
                None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()  # Only the exact Python fixture process we created.
            process.communicate(timeout=5)
            self.fail('fixture helper exceeded its bounded timeout')
        self.assertEqual(stderr, b'', 'helper wrote unexpected stderr')
        self.assertEqual(len(stdout.splitlines()), 1, 'helper must emit one JSON response')
        data = json.loads(stdout)
        self.assert_fixture_paths(data)
        return process.returncode, data

    def invoke(self, script, args, *, payload=None, chat=None, worker=False, ok=True):
        code, data = self.finish_process(self.spawn(script, args, chat=chat, worker=worker), payload)
        if ok:
            self.assertEqual(code, 0, {key: data.get(key) for key in ('state', 'reason', 'error')})
            if 'ok' in data:
                self.assertTrue(data['ok'], data.get('reason'))
        else:
            self.assertNotEqual(code, 0)
            self.assertFalse(data['ok'])
        return data

    def install(self, platform, *extra):
        return self.invoke(SOURCE_CLI, ['install', '--platform', platform, *extra])

    def control(self, text, *, chat='leader-chat', ok=True):
        return self.invoke(self.leader_cli, ['command', text, '--platform', 'codex'], chat=chat, ok=ok)

    def bridge(self, operation, payload, *, key=None, chat='leader-chat', ok=True):
        args = ['bridge', operation, '--platform', 'codex', '--workspace', self.workspace, '--payload-stdin']
        if key:
            args += ['--task-id', key[0], '--attempt', key[1]]
        return self.invoke(self.leader_cli, args, payload=payload, chat=chat, ok=ok)

    def wait_ready(self, payload, *, seconds=0, chat='leader-chat', ok=True):
        args = ['bridge', 'wait-ready', '--platform', 'codex', '--workspace', self.workspace,
                '--payload-stdin', '--wait-seconds', str(seconds)]
        return self.invoke(self.leader_cli, args, payload=payload, chat=chat, ok=ok)

    def worker(self, operation, payload=None, *, key=None, chat='worker-chat', ok=True):
        args = [operation, '--workspace', self.workspace]
        if key:
            args += ['--task-id', key[0], '--attempt', key[1]]
        return self.invoke(self.worker_cli, args, payload=payload, chat=chat, worker=True, ok=ok)

    def worker_id(self, chat='worker-chat'):
        status = self.worker('connection-status', chat=chat)
        workers = status.get('connections', [{}])[0].get('workers', [])
        self.assertEqual(len(workers), 1, status)
        self.assertEqual(status['role'], 'worker')
        return workers[0]['worker_id']

    def role_assignment(self, work_item_id, *, variant='ct-researcher', paths=('assigned.txt',),
                        snapshot=None, write_paths=(), test_paths=()):
        head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=self.workspace, env=self.env,
                              capture_output=True, check=True, timeout=20).stdout.decode().strip()
        snapshot = snapshot or {'head': head,
                                'inputs': {path: hashlib.sha256((self.workspace / path).read_bytes()).hexdigest()
                                           for path in paths}}
        return {'variant': variant, 'work_item_id': work_item_id, 'depends_on': {},
                'snapshot': snapshot, 'read_paths': list(paths),
                'write_paths': list(write_paths), 'check_outputs': [],
                'test_paths': list(test_paths)}

    def publish_assignment(self, round_id, worker_id, work_item_id, content, *, mode='manual',
                           variant='ct-researcher', paths=('assigned.txt',), task_id=None):
        request = {'publish_id': uuid.uuid4().hex, 'mode': mode, 'worker_id': worker_id,
                   'assignment': self.role_assignment(work_item_id, variant=variant, paths=paths),
                   'baseline': {}, 'content': content}
        if task_id:
            request['task_id'] = task_id
        result = self.bridge('publish', {'leader_epoch': self.epoch, 'round_id': round_id,
                                         'request': request})
        self.assertEqual(result['worker_id'], worker_id)
        self.assertEqual(result['work_item_id'], work_item_id)
        self.assertEqual(result['role'], variant)
        return result, (result['task_id'], result['attempt_no'])

    def native_cli(self):
        return self.home / '.gemini/config/skills/coding-team/scripts/coding_team.py'

    def native_command(self, text, *, chat='leader-chat', ok=True):
        return self.invoke(self.native_cli(), ['command', text, '--platform', 'antigravity'],
                           chat=chat, worker=True, ok=ok)

    def select_and_pair(self):
        selected = self.control('/coding-team leader on')
        self.epoch = selected['leader_epoch']
        self.assertEqual(self.control('/coding-team leader on')['leader_epoch'], self.epoch)
        self.control('/coding-team antigravity on')
        invite = self.control('/coding-team antigravity pair')
        match = re.search(r' ticket=([A-Za-z0-9_-]+)$', invite['prompt'])
        self.assertIsNotNone(match, 'pair prompt must include a transferable invite')
        ticket = match.group(1)
        connected = self.worker('connect', {'ticket': ticket})
        self.assertEqual(connected['state'], 'CONNECTED')
        self.assertNotIn(ticket, json.dumps(connected))
        self.assertEqual(self.worker('connect', {'ticket': ticket}, ok=False)['reason'],
                         'INVALID_OR_EXPIRED_TICKET')
        status = self.worker('status')
        self.assertTrue(status['paired'])
        self.assertTrue(status['ready'])
        self.assertEqual(status['effective_policy'], 'ON')
        self.assertEqual(Path(status['helper']), self.worker_cli)
        self.assertEqual(status['helper_sha256'], hashlib.sha256(self.worker_cli.read_bytes()).hexdigest())

    def open_round(self):
        return self.bridge('open-round', {'leader_epoch': self.epoch})['round_id']

    def publish(self, round_id, content, *, mode='manual', task_id=None):
        view, key = self.publish_assignment(round_id, self.worker_id(), 'fixture-work', content,
                                            mode=mode, task_id=task_id)
        self.assertRegex(view['task_id'], r'^CT-[0-9a-f]{32}$')
        return view, key

    def attempt_path(self, key, name):
        return self.workspace / '.coding-team/bridge' / key[0] / ('attempt-' + str(key[1])) / name

    def finish_attempt(self, key, begun, result, *, chat='worker-chat'):
        receipt = self.worker('finish', {'execution_ticket': begun['execution_ticket'],
                                        'state': 'COMPLETED', 'result': result}, key=key, chat=chat)
        self.assertEqual(receipt['state'], 'COMPLETED')
        self.assertEqual(receipt['result_sha256'], hashlib.sha256(result.encode()).hexdigest())
        self.assertEqual(self.attempt_path(key, 'result.md').read_bytes(), result.encode())
        collected = self.bridge('collect', {}, key=key)
        self.assertEqual(collected['result'], result)
        self.assertEqual(collected['result_id'], receipt['result_id'])
        return receipt

    def ack(self, key, receipt):
        evidence = {'task_id': key[0], 'attempt_no': key[1], 'snapshot': receipt['snapshot'],
                    'summary': 'Fixture output and scope verified.', 'scope_verified': True}
        result = self.bridge('ack', {'leader_epoch': self.epoch, 'result_id': receipt['result_id'],
                                     'evidence': evidence}, key=key)
        self.assertTrue(result.get('acknowledged', result.get('ok')))

    def complete_work_item(self, round_id, work_item_id, *, snapshot=None, outcome='accepted'):
        assignment = self.role_assignment(work_item_id, snapshot=snapshot)
        result = self.bridge('complete-work-item', {'leader_epoch': self.epoch, 'round_id': round_id,
                     'work_item_id': work_item_id, 'outcome': outcome,
                     'snapshot': assignment['snapshot'],
                     'evidence': 'Fixture result verified against assigned scope.'})
        self.assertTrue(result.get('ok'), result)
        return result

    def close_round(self, round_id):
        self.assertEqual(self.bridge('close-round', {'leader_epoch': self.epoch, 'round_id': round_id,
                         'evidence': 'Fixture result reviewed and reconciled'})['state'], 'ROUND_CLOSED')

    def test_public_controls_from_installed_helpers(self):
        """A public command implicitly selects this chat and starts one invitation."""
        activated = self.control('/coding-team agy auto on')
        self.assertTrue(activated['desktop_enabled'])
        self.assertTrue(activated['auto_enabled'])
        self.assertEqual(activated['state'], 'CONFIGURED')
        self.assertTrue(activated['invite_expires_at'])
        self.assertEqual(activated['leader_epoch'], 1)
        status = self.control('/coding-team status')
        self.assertEqual(status['inter_app']['role'], 'leader')
        self.assertEqual(status['inter_app']['connections'][0]['desktop_enabled'], True)
        worker = self.worker('connection-status')
        self.assertEqual(worker['role'], 'unbound')
        joined = self.worker('join')
        self.assertEqual(joined['state'], 'CONNECTED')
        worker_status = self.worker('connection-status')
        self.assertEqual(worker_status['role'], 'worker')
        self.assertTrue(worker_status['connections'][0]['paired'])
        other_status = self.worker('connection-status', chat='other-worker-chat')
        self.assertEqual(other_status['role'], 'unbound')
        unknown_status = self.worker('connection-status', chat=None)
        self.assertEqual(unknown_status['role'], 'unknown')

    def test_activation_wait_and_two_chat_status(self):
        """The same activation moves from invitation to AUTO_READY only after its worker listens."""
        activated = self.control('/coding-team agy auto on')
        epochs = {name: activated[name] for name in
                  ('leader_epoch', 'platform', 'join_id', 'desktop_epoch', 'auto_epoch')}
        before_join = self.wait_ready(epochs)
        self.assertEqual(before_join['state'], 'WAITING')
        self.assertFalse(before_join.get('paired', False))

        joined = self.worker('join')
        self.assertEqual(joined['state'], 'CONNECTED')
        waiting = self.wait_ready(epochs)
        self.assertTrue(waiting.get('paired'))
        self.assertFalse(waiting.get('waiter_ready'))
        self.assertEqual(waiting['state'], 'WAITING')

        listener = self.start_listener()
        ready = self.wait_ready(epochs)
        self.assertEqual(ready['state'], 'READY')
        connection = self.worker('connection-status')['connections'][0]
        self.assertTrue(connection['paired'])
        self.assertTrue(connection['waiter_ready'])
        self.assertEqual(connection['readiness'], 'AUTO_READY')
        # The same activation remains valid while the worker's attached listener is alive.
        self.assertEqual(ready['leader_epoch'], epochs['leader_epoch'])
        self.stop_listener(listener)

    def test_legacy_pair_and_receipts_readable(self):
        """A v1-style ticket pair and receipt remain visible to the public status command."""
        self.select_and_pair()
        status = self.control('/coding-team status')
        conn = status['inter_app']['connections'][0]
        self.assertTrue(conn['paired'])
        self.assertFalse(conn['auto_enabled'])
        round_id = self.open_round()
        _, key = self.publish(round_id, 'Read the fixture and return evidence.\n')
        begun = self.worker('begin', {}, key=key)
        turned_off = self.control('/coding-team agy off')
        self.assertFalse(turned_off['desktop_enabled'])
        result = 'v1-style receipt fixture result.\n'
        receipt = self.finish_attempt(key, begun, result)
        self.ack(key, receipt)
        self.complete_work_item(round_id, 'fixture-work')
        self.close_round(round_id)
        self.assertEqual(self.attempt_path(key, 'result.md').read_bytes(), result.encode())
        self.assertEqual(hashlib.sha256(self.attempt_path(key, 'result.md').read_bytes()).hexdigest(),
                         receipt['result_sha256'])
        status_after_receipt = self.control('/coding-team status')
        self.assertFalse(status_after_receipt['inter_app']['connections'][0]['desktop_enabled'])
        self.assertFalse(status_after_receipt['inter_app']['connections'][0]['auto_enabled'])
        reopened = self.control('/coding-team agy on')
        self.assertTrue(reopened['desktop_enabled'])
        self.assertFalse(reopened['auto_enabled'])
        self.assertTrue(reopened['invite_expires_at'])
        self.assertFalse(self.control('/coding-team status')['inter_app']['connections'][0]['paired'])

    def test_worker_only_and_native_rules_route_same_commands(self):
        """Worker-only help works, then the co-installed native component exposes shared help/status."""
        self.assertFalse(self.native_cli().exists())
        worker_help = self.worker('help', chat=None)
        self.assertIn('/coding-team listen', json.dumps(worker_help))
        worker_status = self.worker('connection-status', chat=None)
        self.assertEqual(worker_status['role'], 'unknown')

        self.install('antigravity')
        self.assertTrue(self.native_cli().is_file())
        native_help = self.native_command('/coding-team help')
        self.assertIn('/coding-team listen', json.dumps(native_help))
        native_status = self.native_command('/coding-team status')
        self.assertIn('inter_app', native_status)
        installed_worker_help = self.worker('help', chat=None)
        self.assertIn('/coding-team listen', json.dumps(installed_worker_help))

    def test_manual_installed_handoff_scoped_read_ack_and_wrong_chat(self):
        """Catches broken installed imports, cross-chat access, or unreviewed closure."""
        self.select_and_pair()
        round_id = self.open_round()
        request = 'Read assigned.txt and return evidence.\nTiếng Việt\n'
        published, key = self.publish(round_id, request)
        self.assertEqual(published['state'], 'WAITING_FOR_USER')
        self.assertIn('coding-team-desktop-worker', published['prompt'])
        self.assertIn(key[0], published['prompt'])
        self.assertEqual(Path(published['request_path']), self.attempt_path(key, 'request.md'))
        self.assertEqual(self.attempt_path(key, 'request.md').read_bytes(), request.encode())
        self.assertEqual(self.worker('begin', {}, key=key, chat='wrong-worker', ok=False)['reason'], 'WRONG_WORKER')
        begun = self.worker('begin', {}, key=key)
        self.assertEqual(begun['state'], 'RUNNING')
        self.assertEqual(begun['request'], request)
        self.assertEqual(begun['assignment']['read_paths'], ['assigned.txt'])
        self.assertEqual(begun['baseline'], {})
        self.assertEqual(begun['kind'], 'READ')
        self.assertFalse(self.worker('begin', {}, key=key, ok=False)['ok'])
        result = 'Evidence from fixture source.\nTiếng Việt preserved.\n'
        receipt = self.finish_attempt(key, begun, result)
        self.assertFalse(self.bridge('collect', {}, key=key, chat='observer-chat', ok=False)['ok'])
        grant = self.bridge('grant-read', {'leader_epoch': self.epoch}, key=key)['grant']
        scoped = self.bridge('collect', {'grant': grant}, key=key, chat='observer-chat')
        self.assertEqual(scoped['result'], result)
        self.assertFalse(self.bridge('collect', {'grant': grant + 'x'}, key=key,
                                     chat='observer-chat', ok=False)['ok'])
        self.assertFalse(self.bridge('ack', {'leader_epoch': self.epoch, 'result_id': receipt['result_id']},
                                     key=key, chat='observer-chat', ok=False)['ok'])
        busy = self.bridge('close-round', {'leader_epoch': self.epoch, 'round_id': round_id,
                           'evidence': 'not yet reviewed'}, ok=False)
        self.assertEqual(busy['reason'], 'ROUND_BUSY')
        self.ack(key, receipt)
        self.complete_work_item(round_id, 'fixture-work')
        self.close_round(round_id)
        self.assertEqual(self.git('status', '--porcelain'), b'')

    def start_listener(self):
        process = self.spawn(self.worker_cli, ['listen', '--workspace', self.workspace],
                             chat='worker-chat', worker=True)
        self.addCleanup(self.stop_listener, process)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                _, status = self.finish_process(process)
                self.fail('listener exited before readiness: ' + str(status.get('reason')))
            status = self.worker('status')
            if status['waiter_ready']:
                return process
            time.sleep(.05)
        self.fail('listener did not become ready within 10 seconds')

    @staticmethod
    def stop_listener(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)

    def auto_attempt(self, round_id, content, *, task_id=None):
        listener = self.start_listener()
        published, key = self.publish(round_id, content, mode='auto', task_id=task_id)
        self.assertEqual(published['state'], 'QUEUED')
        code, claimed = self.finish_process(listener, timeout=10)
        self.assertEqual(code, 0)
        self.assertEqual(claimed['state'], 'CLAIMED')
        self.assertEqual((claimed['task_id'], claimed['attempt_no']), key)
        self.assertFalse(self.worker('begin', {}, key=key, ok=False)['ok'])
        begun = self.worker('begin', {'claim': claimed['claim']}, key=key)
        self.assertEqual(begun['state'], 'RUNNING')
        self.assertEqual(begun['request'], content)
        self.assertFalse(self.worker('begin', {'claim': claimed['claim']}, key=key, ok=False)['ok'])
        return key, begun

    def test_auto_listener_same_chat_retry_preserves_context_and_old_files(self):
        """Catches fake readiness, claim bypass, new-chat retry, or file replacement."""
        self.select_and_pair()
        self.control('/coding-team agy auto on')
        status = self.worker('status')
        self.assertFalse(status['waiter_ready'])
        self.assertEqual(status['reason'], 'WAITER_NOT_READY')
        round_id = self.open_round()
        key1, begun1 = self.auto_attempt(round_id, 'First attempt: inspect assigned.txt.\n')
        receipt1 = self.finish_attempt(key1, begun1, 'First attempt evidence: original fixture source.\n')
        self.ack(key1, receipt1)
        old_request = self.attempt_path(key1, 'request.md').read_bytes()
        old_result = self.attempt_path(key1, 'result.md').read_bytes()
        retry = 'Retry with previous evidence: original fixture source.\nCheck it again in this same chat.\n'
        key2, begun2 = self.auto_attempt(round_id, retry, task_id=key1[0])
        self.assertEqual(key2, (key1[0], 2))
        self.assertEqual(self.worker('status')['leader']['chat_id'], 'leader-chat')
        self.assertEqual(self.worker('status', chat='new-worker-chat')['reason'], 'WRONG_WORKER')
        receipt2 = self.finish_attempt(key2, begun2, 'Retry confirms prior evidence.\n')
        self.ack(key2, receipt2)
        self.assertEqual(self.attempt_path(key1, 'request.md').read_bytes(), old_request)
        self.assertEqual(self.attempt_path(key1, 'result.md').read_bytes(), old_result)
        self.assertNotEqual(receipt1['result_id'], receipt2['result_id'])
        grant2 = self.bridge('grant-read', {'leader_epoch': self.epoch}, key=key2)['grant']
        self.assertFalse(self.bridge('collect', {'grant': grant2}, key=key1,
                                     chat='observer-chat', ok=False)['ok'])
        self.complete_work_item(round_id, 'fixture-work')
        self.close_round(round_id)
        self.control('/coding-team agy auto off')
        self.assertEqual(self.git('status', '--porcelain'), b'')

    def test_open_round_blocks_role_release_and_transfer_until_native_evidence(self):
        """Catches role theft while native participation is still outstanding."""
        self.select_and_pair()
        round_id = self.open_round()
        self.bridge('register-native', {'leader_epoch': self.epoch, 'round_id': round_id,
                                       'participant_id': 'fixture-native'})
        self.bridge('bind-native', {'leader_epoch': self.epoch, 'round_id': round_id,
                                   'participant_id': 'fixture-native', 'handle': 'fixture-handle'})
        for text, chat in (('/coding-team leader off', 'leader-chat'),
                           ('/coding-team leader on', 'next-leader-chat')):
            blocked = self.control(text, chat=chat, ok=False)
            self.assertEqual(blocked['state'], 'LEADER_BUSY')
            self.assertEqual(blocked['round_id'], round_id)
            self.assertEqual(blocked['round_ids'], [round_id])
        close = self.bridge('close-round', {'leader_epoch': self.epoch, 'round_id': round_id,
                            'evidence': 'unfinished native'}, ok=False)
        self.assertEqual(close['native_participants'], ['fixture-native'])
        self.bridge('finish-native', {'leader_epoch': self.epoch, 'round_id': round_id,
                                     'participant_id': 'fixture-native', 'evidence': 'fixture native done'})
        self.close_round(round_id)
        transferred = self.control('/coding-team leader on', chat='next-leader-chat')
        self.assertGreater(transferred['leader_epoch'], self.epoch)
        status = self.control('/coding-team antigravity status', chat='next-leader-chat')
        self.assertEqual(status['leader']['chat_id'], 'next-leader-chat')
        self.assertFalse(status['desktop_enabled'])
        self.assertFalse(status['auto_enabled'])
        self.assertFalse(status['paired'])
        self.assertFalse(self.control('/coding-team antigravity on', ok=False)['ok'])

    def test_installed_peer_many_workers_concurrent_and_revision_affinity(self):
        active = self.control('/coding-team agy auto on')
        self.epoch = active['leader_epoch']
        chats = [f'worker-{number}' for number in range(9)]
        workers = [self.worker('join', chat=chat)['worker_id'] for chat in chats]
        self.assertEqual(len(set(workers)), len(chats))
        round_id = self.open_round()
        listeners = [self.spawn(self.worker_cli, ['listen', '--workspace', self.workspace],
                                chat=chat, worker=True) for chat in chats]
        for listener in listeners:
            self.addCleanup(self.stop_listener, listener)
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            status = self.control('/coding-team status')['inter_app']['connections'][0]
            if sum(worker['waiter_ready'] for worker in status['workers']) == len(chats):
                break
            self.assertTrue(all(listener.poll() is None for listener in listeners))
            time.sleep(.10)
        else:
            self.fail('not all installed worker listeners reached readiness')
        requests = [self.publish_assignment(round_id, worker, f'work-{number}', f'Check {number}\n', mode='auto')
                    for number, worker in enumerate(workers)]
        running = []
        for chat, listener, (offer, key) in zip(chats, listeners, requests):
            code, claimed = self.finish_process(listener, timeout=30)
            self.assertEqual(code, 0, claimed)
            self.assertEqual((claimed['worker_id'], claimed['task_id'], claimed['attempt_no']),
                             (offer['worker_id'], *key))
            begun = self.worker('begin', {'claim': claimed['claim']}, key=key, chat=chat)
            self.assertEqual(begun['state'], 'RUNNING')
            running.append((chat, key, begun))
        observed = self.control('/coding-team status')['inter_app']['attempts']
        self.assertEqual(sum(row['state'] == 'RUNNING' for row in observed), 9)
        for chat, key, begun in reversed(running):
            receipt = self.finish_attempt(key, begun, begun['request'], chat=chat)
            self.ack(key, receipt)
            if chat != chats[0]:
                self.complete_work_item(round_id, begun['work_item_id'])
        self.control('/coding-team agy auto off')
        _, original, first = running[0]
        _, second = self.publish_assignment(round_id, workers[0], first['work_item_id'], 'Correct the first result',
                                             task_id=original[0])
        self.assertEqual(second, (original[0], 2))
        self.assertEqual(self.worker('begin', {}, key=second, chat=chats[1], ok=False)['reason'], 'WRONG_WORKER')
        begun = self.worker('begin', {}, key=second, chat=chats[0])
        self.ack(second, self.finish_attempt(second, begun, 'Corrected', chat=chats[0]))
        self.complete_work_item(round_id, first['work_item_id'])
        self.close_round(round_id)

    def test_independent_review_uses_runtime_identity_and_exact_snapshot(self):
        from test_task_ledger import LedgerTests, ledger

        self.epoch = self.control('/coding-team agy on')['leader_epoch']
        editor = self.worker('join')['worker_id']
        reviewer = self.worker('join', chat='review-chat')['worker_id']
        round_id = self.open_round()
        write = self.role_assignment('write', variant='ct-developer', write_paths=('owned.txt',))
        offer = self.bridge('publish', {'leader_epoch': self.epoch, 'round_id': round_id, 'request': {
            'publish_id': 'edit', 'mode': 'manual', 'worker_id': editor, 'assignment': write,
            'baseline': {'owned.txt': 'ABSENT'}, 'content': 'Create the owned file.'}})
        key = offer['task_id'], offer['attempt_no']
        begun = self.worker('begin', {}, key=key)
        (self.workspace / 'owned.txt').write_bytes(b'owned result')
        self.ack(key, self.finish_attempt(key, begun, 'Owned file created.'))
        accepted = self.role_assignment('write', paths=('owned.txt',))['snapshot']
        self.complete_work_item(round_id, 'write', snapshot=accepted)

        def projection(participant, scope, snapshot, *, ok=True):
            return self.bridge('ledger-evidence', {'leader_epoch': self.epoch, 'round_id': round_id,
                'participant_id': participant, 'review_scope': scope, 'snapshot': snapshot}, ok=ok)

        author = projection(offer['participant_id'], None, write['snapshot'])
        self.assertIn(author['actor_id'], author['writer_ids'])
        self.bridge('register-native', {'leader_epoch': self.epoch, 'round_id': round_id,
                    'participant_id': 'observer', 'observer': True})
        self.bridge('bind-native', {'leader_epoch': self.epoch, 'round_id': round_id,
                    'participant_id': 'observer', 'handle': 'actual-observer-handle'})
        self.assertEqual(projection('observer', ['owned.txt'], accepted, ok=False)['reason'], 'NO_EXECUTOR_EVIDENCE')
        self.bridge('finish-native', {'leader_epoch': self.epoch, 'round_id': round_id,
                    'participant_id': 'observer', 'evidence': 'Observation concluded'})

        reviewer_ids = []
        for label, chat, worker in (('self', 'worker-chat', editor), ('other', 'review-chat', reviewer)):
            assignment = self.role_assignment(label, variant='ct-reviewer', paths=('owned.txt',), snapshot=accepted)
            assignment['review_scope'] = ['owned.txt']
            response = self.bridge('publish', {'leader_epoch': self.epoch, 'round_id': round_id, 'request': {
                'publish_id': label, 'mode': 'manual', 'worker_id': worker, 'assignment': assignment,
                'baseline': {}, 'content': 'Review the exact snapshot.'}})
            key = response['task_id'], response['attempt_no']
            begun = self.worker('begin', {}, key=key, chat=chat)
            self.ack(key, self.finish_attempt(key, begun, 'Reviewed exact snapshot.', chat=chat))
            result = projection(begun['participant_id'], ['owned.txt'], accepted)
            self.assertEqual(projection(begun['participant_id'], ['owned.txt'], write['snapshot'],
                                       ok=False)['reason'], 'INVALID_REVIEW_EVIDENCE')
            reviewer_ids.append(result['actor_id'])
            self.complete_work_item(round_id, label, snapshot=accepted)
        self.assertEqual(reviewer_ids[0], author['actor_id'])
        self.assertNotIn(reviewer_ids[1], author['writer_ids'])
        fixture = LedgerTests('runTest')
        fixture.root, fixture.path = self.workspace, self.base / 'ledger.json'
        state = fixture.initial()
        state.update(revision=0, audit=[], delivery_files=['owned.txt'])
        ledger.event(state, 'init', 'installed bridge fixture')
        state['baseline']['writers'] = author['writer_ids']
        fixture.review(state, person=reviewer_ids[0])
        self.assertEqual(fixture.fixture_readiness(state)['delivery'], 'HOLD')
        fixture.review(state, person=reviewer_ids[1])
        self.assertEqual(fixture.fixture_readiness(state)['delivery'], 'PASS')
        self.close_round(round_id)

    def test_unknown_runtime_cannot_select_or_initialize_bridge(self):
        """Catches identity fallback to absent or fabricated inherited runtimes."""
        status = self.control('/coding-team leader status', chat=None)
        self.assertFalse(status['selected'])
        self.assertFalse((self.workspace / '.coding-team').exists())
        rejected = self.control('/coding-team leader on', chat=None, ok=False)
        self.assertEqual(rejected['reason'], 'UNKNOWN_CHAT')
        self.assertFalse((self.workspace / '.coding-team').exists())
        worker = self.worker('status', chat=None, ok=False)
        self.assertEqual(worker['reason'], 'UNKNOWN_CHAT')
        self.assertFalse((self.workspace / '.coding-team').exists())


if __name__ == '__main__':
    unittest.main()
