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

    def worker(self, operation, payload=None, *, key=None, chat='worker-chat', ok=True):
        args = [operation, '--workspace', self.workspace]
        if key:
            args += ['--task-id', key[0], '--attempt', key[1]]
        return self.invoke(self.worker_cli, args, payload=payload, chat=chat, worker=True, ok=ok)

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
        request = {'publish_id': uuid.uuid4().hex, 'content': content,
                   'kind': 'READ', 'scope': ['assigned.txt'], 'baseline': {}, 'mode': mode}
        if task_id:
            request['task_id'] = task_id
        view = self.bridge('publish', {'leader_epoch': self.epoch, 'round_id': round_id, 'request': request})
        self.assertRegex(view['task_id'], r'^CT-[0-9a-f]{32}$')
        return view, (view['task_id'], view['attempt_no'])

    def attempt_path(self, key, name):
        return self.workspace / '.coding-team/bridge' / key[0] / ('attempt-' + str(key[1])) / name

    def finish_attempt(self, key, begun, result):
        receipt = self.worker('finish', {'execution_ticket': begun['execution_ticket'],
                                        'state': 'COMPLETED', 'result': result}, key=key)
        self.assertEqual(receipt['state'], 'COMPLETED')
        self.assertEqual(receipt['result_sha256'], hashlib.sha256(result.encode()).hexdigest())
        self.assertEqual(self.attempt_path(key, 'result.md').read_bytes(), result.encode())
        collected = self.bridge('collect', {}, key=key)
        self.assertEqual(collected['result'], result)
        self.assertEqual(collected['result_id'], receipt['result_id'])
        return receipt

    def ack(self, key, receipt):
        self.assertTrue(self.bridge('ack', {'leader_epoch': self.epoch,
                                          'result_id': receipt['result_id']}, key=key)['acknowledged'])

    def close_round(self, round_id):
        self.assertEqual(self.bridge('close-round', {'leader_epoch': self.epoch, 'round_id': round_id,
                         'evidence': 'Fixture result reviewed and reconciled'})['state'], 'ROUND_CLOSED')

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
        self.assertEqual(begun['scope'], ['assigned.txt'])
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
                self.assertTrue(status['ready'])
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
