"""Exercise native Sidecar registration against an isolated app config."""
from base64 import urlsafe_b64decode
from contextlib import nullcontext
from hashlib import sha256
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import ct_bridge_binding as binding
import ct_bridge_control as control
from ct_bridge_files import WorkspacePaths
import ct_bridge_sidecar as sidecar


RUNNER_MODULES = ('ct_bridge_sidecar.py', 'ct_bridge_binding.py', 'ct_bridge_control.py',
                  'ct_bridge_store.py', 'ct_bridge_files.py', 'ct_bridge_commands.py',
                  'ct_platform.py', 'ct_policy.py', 'ct_storage.py')


class SidecarTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='ct-sidecar-'))).resolve()
        self.config = self.home / '.gemini/config/config.json'
        self.config.parent.mkdir(parents=True)
        self.original = {'appearance': {'theme': 'dark'}, 'sidecars': {'existing': {'enabled': False}}}
        self.config.write_text(json.dumps(self.original), encoding='utf-8')
        workspace = self.home / 'worktree'
        transport = workspace / '.coding-team/bridge'
        paths = WorkspacePaths(workspace, transport, transport / 'bridge.sqlite3')
        source = workspace / '.agents/skills/coding-team'
        scripts = source / 'scripts'
        scripts.mkdir(parents=True)
        self.runner_bytes = {}
        for name in RUNNER_MODULES:
            data = (Path(sidecar.__file__).parent / name).read_bytes()
            (scripts / name).write_bytes(data)
            self.runner_bytes[name] = data
        manifest = {'files': {'skill_dir': {f'scripts/{name}': sha256(data).hexdigest()
                                             for name, data in self.runner_bytes.items()}}}
        digest = 'b' * 64
        installed = mock.Mock(skill_dir=source, _manifest_digest=digest)
        self.enterContext(mock.patch.object(binding.BridgeContext, 'own_binding', return_value=installed))
        self.enterContext(mock.patch.object(sidecar, '_read_manifest', create=True, return_value=(manifest, digest)))
        self.enterContext(mock.patch.object(sidecar, 'locked_bindings', create=True, return_value=nullcontext()))
        if os.name != 'nt':
            self.enterContext(mock.patch.object(sidecar, '_WINDOWS', create=True, new=True))
            self.enterContext(mock.patch.object(sidecar, '_replace_with_backup', create=True,
                                                side_effect=self.simulate_replace_with_backup))
        self.context = binding.BridgeContext(paths, binding.Actor('codex', 'leader-chat'), 'leader',
                                             source,
                                             {'CODEX_HOME': str(self.home / 'codex'), 'UNRELATED_SECRET': 'sentinel'},
                                             self.home)
        self.project = '058d01a6-daef-4365-b786-84d22849226a'
        self.chat = '17e50080-c944-491b-a811-f615cf29cbdb'
        self.epochs = {'leader_epoch': 3, 'desktop_epoch': 4, 'auto_epoch': 5}
        self.digest = 'a' * 64

    def submit(self, chat=None):
        return sidecar.submit_listener(self.context, self.project, chat or self.chat, self.epochs, None, self.digest)

    @staticmethod
    def simulate_replace_with_backup(config, draft, backup):
        backup.write_bytes(config.read_bytes())
        os.replace(draft, config)

    def complete(self, directory, *, blocked=None, configure=None, advance_time=0, app_api=None):
        manifest = json.loads((directory / 'sidecar.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['command'], sys.executable)
        self.assertEqual(manifest['restart_policy'], 'never')
        self.assertEqual(manifest['args'][:-3], ['-I', '-S', '-B'])
        archive = Path(manifest['args'][-3])
        self.assertEqual(archive.parent, directory)
        self.assertFalse(archive.is_relative_to(self.context.paths.root))
        with zipfile.ZipFile(archive) as runtime:
            self.assertEqual(set(runtime.namelist()), {'__main__.py', *self.runner_bytes})
            self.assertEqual({name: runtime.read(name) for name in self.runner_bytes}, self.runner_bytes)
        record = Path(manifest['args'][-2])
        request_id = manifest['args'][-1]
        raw = record.read_text(encoding='utf-8')
        self.assertNotIn('sentinel', raw)
        request = json.loads(raw)
        settings = json.loads(self.config.read_text(encoding='utf-8'))
        self.assertEqual(settings['sidecars'][directory.name], {'enabled': True})
        if configure is not None:
            configure(settings, directory.name)
            self.config.write_text(json.dumps(settings), encoding='utf-8')
        app_api = app_api or self.home / 'app' / ('agentapi.exe' if os.name == 'nt' else 'agentapi')
        runtime_time = time.time() + advance_time
        with (mock.patch.object(sidecar, 'locate_workspace', return_value=self.context.paths),
              mock.patch.object(control, 'listener_start_fence', create=True, side_effect=blocked) as fence,
              mock.patch.object(sidecar.shutil, 'which', return_value=str(app_api)),
              mock.patch.object(sidecar.time, 'time', return_value=runtime_time),
              mock.patch.object(sidecar.subprocess, 'run',
                                return_value=subprocess.CompletedProcess([], 0)) as deliver):
            sidecar.run_sidecar(record, request_id)
            sidecar.run_sidecar(record, request_id)
        if blocked or advance_time:
            deliver.assert_not_called()
        else:
            self.assertEqual(fence.call_args.args[0], self.context.__class__(
                self.context.paths, self.context.actor, 'leader', self.context.source_root,
                {'CODEX_HOME': self.context.env['CODEX_HOME']}, self.home))
            self.assertEqual(fence.call_args.args[1:], (self.epochs, self.chat, None))
            self.assertEqual(fence.call_args.kwargs, {'policy_digest': self.digest})
            deliver.assert_called_once()
            path, operations, target, prompt = deliver.call_args.args[0]
            self.assertEqual((path, operations, target), (str(app_api), 'send-message', self.chat))
            self.assertRegex(prompt, r'\A/coding-team listen startup ct1-[A-Za-z0-9_-]+\Z')
            token = prompt.removeprefix('/coding-team listen startup ct1-')
            self.assertEqual(json.loads(urlsafe_b64decode(token + '=' * (-len(token) % 4))),
                             {**self.epochs, 'chat_id': self.chat, 'worker_id': None, 'policy_digest': self.digest,
                              'expires_at': request['expires_at']})
            self.assertFalse(deliver.call_args.kwargs.get('shell', False))
            self.assertEqual(deliver.call_args.kwargs['stdout'], subprocess.DEVNULL)
            self.assertEqual(deliver.call_args.kwargs['stderr'], subprocess.DEVNULL)

    @unittest.skipUnless(os.name == 'nt', 'Antigravity supplies .BAT on Windows')
    def test_accepts_the_app_supplied_windows_agentapi_wrapper(self):
        wrapper = self.home / '.gemini/antigravity/bin/agentapi.BAT'
        wrapper.parent.mkdir(parents=True)
        wrapper.write_bytes(b'@echo off\r\nexit /b 0\r\n')
        wait = sidecar._wait_for_receipt

        def simulate_app(directory, timeout):
            self.complete(directory, app_api=wrapper)
            return wait(directory, timeout)

        with mock.patch.object(sidecar, '_wait_for_receipt', side_effect=simulate_app):
            self.assertEqual(self.submit()['state'], 'LISTENER_REQUESTED')

    @unittest.skipUnless(os.name == 'nt', 'Antigravity supplies .BAT on Windows')
    def test_windows_batch_argument_survives_and_path_decoy_is_rejected(self):
        wrapper = self.home / '.gemini/antigravity/bin/agentapi.BAT'
        wrapper.parent.mkdir(parents=True)
        wrapper.write_bytes(b'@echo off\r\necho(%~3\r\n')
        prompt = '/coding-team listen startup ct1-SF9a-Gd-a'
        with mock.patch.object(sidecar.shutil, 'which', return_value=str(wrapper)):
            target = sidecar._agentapi(self.config)
        sent = subprocess.run([str(target), 'send-message', self.chat, prompt],
                              capture_output=True, text=True, check=True, timeout=4)
        self.assertEqual(sent.stdout.strip(), prompt)

        decoy = self.home / 'unmanaged/agentapi.BAT'
        unsafe = self.home / 'hi%TEMP%/.gemini/antigravity/bin/agentapi.BAT'
        for candidate, config in ((decoy, self.config), (unsafe, self.home / 'hi%TEMP%/.gemini/config/config.json')):
            with mock.patch.object(sidecar.shutil, 'which', return_value=str(candidate)):
                with self.assertRaisesRegex(ValueError, 'SIDECAR_API_MISSING'):
                    sidecar._agentapi(config)

    def test_returns_submission_only_and_preserves_app_settings_changes(self):
        def normalize(settings, owned):
            settings['appearance']['theme'] = 'light'
            settings['sidecars']['existing']['enabled'] = True
            settings['sidecars'][owned].update({
                'schemaVersion': 1, 'projectId': '615dcc1b-4d95-4eb3-a61f-2ff470028909',
            })

        wait = sidecar._wait_for_receipt

        def simulate_app(directory, timeout):
            self.assertEqual(self.submit()['state'], 'LISTENER_PENDING')
            self.complete(directory, configure=normalize)
            return wait(directory, timeout)

        with mock.patch.object(sidecar, '_wait_for_receipt', side_effect=simulate_app):
            result = self.submit()
        self.assertEqual(result, {'ok': True, 'state': 'LISTENER_REQUESTED', 'ready': False})
        self.assertEqual(json.loads(self.config.read_text(encoding='utf-8')), {
            'appearance': {'theme': 'light'}, 'sidecars': {'existing': {'enabled': True}}})
        self.assertFalse(list((self.config.parent / 'sidecars').glob('*/sidecar.json')))
        self.assertFalse(list((self.config.parent / 'sidecars').glob('*/*.pyz')))
        self.assertEqual(self.submit()['state'], 'LISTENER_PENDING')

    def test_preserves_actual_displaced_settings_on_enable_and_cleanup(self):
        # A writer outside the coding-team lock updates config after the
        # application's final pre-replacement read, once for each direction.
        changed = set()
        replace = getattr(sidecar, '_replace_with_backup', self.simulate_replace_with_backup)

        def replace_after_concurrent_write(config, draft, backup):
            proposed = json.loads(draft.read_bytes())
            enabling = sidecar._sidecar_id(self.project, self.chat) in proposed.get('sidecars', {})
            if enabling not in changed:
                current = json.loads(config.read_bytes())
                if enabling:
                    current['appearance']['theme'] = 'light'
                else:
                    current['sidecars']['another'] = {'enabled': True}
                config.write_text(json.dumps(current), encoding='utf-8')
                changed.add(enabling)
            replace(config, draft, backup)

        wait = sidecar._wait_for_receipt

        def simulate_app(directory, timeout):
            self.assertEqual(json.loads(self.config.read_bytes())['appearance']['theme'], 'light')
            self.complete(directory)
            return wait(directory, timeout)

        with (mock.patch.object(sidecar, '_replace_with_backup', create=True,
                                side_effect=replace_after_concurrent_write),
              mock.patch.object(sidecar, '_wait_for_receipt', side_effect=simulate_app)):
            self.assertEqual(self.submit()['state'], 'LISTENER_REQUESTED')
        self.assertEqual(changed, {True, False})
        self.assertEqual(json.loads(self.config.read_bytes()), {
            'appearance': {'theme': 'light'},
            'sidecars': {'existing': {'enabled': False}, 'another': {'enabled': True}},
        })

    def test_retires_owned_expired_draft_without_restoring_an_old_config(self):
        wait = sidecar._wait_for_receipt
        with mock.patch.object(sidecar, '_wait_for_receipt', return_value=None):
            self.assertEqual(self.submit()['reason'], 'SIDECAR_TIMEOUT')
        directory = next((self.config.parent / 'sidecars').iterdir())
        # These are files left by an earlier build while committing config.
        (directory / 'config-before.json').write_text(json.dumps(self.original), encoding='utf-8')
        (directory / 'config-next.json').write_text(json.dumps(self.original), encoding='utf-8')
        later = {**self.original, 'appearance': {'theme': 'light'}}
        self.config.write_text(json.dumps(later), encoding='utf-8')

        def simulate_app(directory, timeout):
            self.complete(directory)
            return wait(directory, timeout)

        with (mock.patch.object(sidecar.time, 'time', return_value=time.time() + 121),
              mock.patch.object(sidecar, '_wait_for_receipt', side_effect=simulate_app)):
            self.assertEqual(self.submit()['state'], 'LISTENER_REQUESTED')
        self.assertEqual(json.loads(self.config.read_bytes()), later)
        self.assertFalse(list(directory.glob('config-*.json')))

    def test_replays_actual_backup_after_interrupted_replacement(self):
        wait = sidecar._wait_for_receipt
        with mock.patch.object(sidecar, '_wait_for_receipt', return_value=None):
            self.assertEqual(self.submit()['reason'], 'SIDECAR_TIMEOUT')
        directory = next((self.config.parent / 'sidecars').iterdir())
        (directory / 'config-00-before.json').write_text(json.dumps(self.original), encoding='utf-8')
        late = {**self.original, 'appearance': {'theme': 'light'}}
        (directory / 'config-00-displaced.json').write_text(json.dumps(late), encoding='utf-8')

        def simulate_app(directory, timeout):
            self.complete(directory)
            return wait(directory, timeout)

        with (mock.patch.object(sidecar.time, 'time', return_value=time.time() + 121),
              mock.patch.object(sidecar, '_wait_for_receipt', side_effect=simulate_app)):
            self.assertEqual(self.submit()['state'], 'LISTENER_REQUESTED')
        self.assertEqual(json.loads(self.config.read_bytes()), late)
        self.assertFalse(list(directory.glob('config-*.json')))

    def test_conflicting_late_writes_preserve_recovery_and_only_committed_config_arms(self):
        replace = getattr(sidecar, '_replace_with_backup', self.simulate_replace_with_backup)

        def collide(config, draft, backup):
            current = json.loads(config.read_bytes())
            current['appearance']['theme'] = 'light'
            config.write_text(json.dumps(current), encoding='utf-8')
            replace(config, draft, backup)
            current = json.loads(config.read_bytes())
            current['appearance']['theme'] = 'system'
            config.write_text(json.dumps(current), encoding='utf-8')

        with (mock.patch.object(sidecar, '_replace_with_backup', side_effect=collide),
              mock.patch.object(sidecar, '_wait_for_receipt', return_value=None) as wait):
            self.assertEqual(self.submit()['reason'], 'SIDECAR_CONFIG_CONFLICT')
        wait.assert_not_called()
        directory = next((self.config.parent / 'sidecars').iterdir())
        backup, = directory.glob('config-*-displaced.json')
        self.assertEqual(json.loads(backup.read_bytes())['appearance']['theme'], 'light')
        self.assertEqual(json.loads(self.config.read_bytes())['appearance']['theme'], 'system')
        manifest = json.loads((directory / 'sidecar.json').read_bytes())
        with (mock.patch.object(sidecar, '_WAIT', 0),
              mock.patch.object(sidecar, '_deliver') as deliver):
            sidecar.run_sidecar(manifest['args'][-2], manifest['args'][-1])
        deliver.assert_not_called()
        self.assertFalse((directory / 'claimed').exists())

    def test_archive_executes_without_importing_from_project_or_pythonpath(self):
        project = self.context.paths.root
        (project / 'ct_policy.py').write_text('raise RuntimeError("Unverified project import")\n',
                                              encoding='utf-8')
        wait = sidecar._wait_for_receipt

        def simulate_app(directory, timeout):
            manifest = json.loads((directory / 'sidecar.json').read_bytes())
            env = dict(os.environ, PATH=str(self.home / 'empty-bin'), PYTHONPATH=str(project))
            result = subprocess.run([manifest['command'], *manifest['args']], cwd=project, env=env,
                                    capture_output=True, text=True, timeout=5, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            return wait(directory, timeout)

        with mock.patch.object(sidecar, '_wait_for_receipt', side_effect=simulate_app):
            self.assertEqual(self.submit()['reason'], 'SIDECAR_API_MISSING')

    def test_rejects_runner_bytes_changed_after_the_install_manifest_check(self):
        before = self.config.read_bytes()
        script = self.context.source_root / 'scripts/ct_policy.py'
        script.write_bytes(b'# changed after expected digest was captured\n')
        with mock.patch.object(sidecar, '_wait_for_receipt', return_value=None) as wait:
            self.assertEqual(self.submit()['reason'], 'INSTALLATION_INCOMPATIBLE')
        wait.assert_not_called()
        self.assertEqual(self.config.read_bytes(), before)

    def test_rechecks_policy_before_send_and_does_not_publish_authority_errors(self):
        wait = sidecar._wait_for_receipt

        def simulate_app(directory, timeout):
            self.complete(directory, blocked=ValueError('STALE_POLICY'))
            return wait(directory, timeout)

        with mock.patch.object(sidecar, '_wait_for_receipt', side_effect=simulate_app):
            result = self.submit()
        self.assertFalse(result['ok'])
        self.assertFalse(result['ready'])
        self.assertEqual(result['reason'], 'STALE_POLICY')
        self.assertEqual(json.loads(self.config.read_text(encoding='utf-8')), self.original)

    def test_expired_or_missing_receipt_cleans_registration_and_fences_replay(self):
        first = []

        def simulate_timeout(directory, _timeout):
            manifest = json.loads((directory / 'sidecar.json').read_text(encoding='utf-8'))
            first.extend(manifest['args'][-2:])
            self.complete(directory, advance_time=121)
            return None

        before = time.time()
        with mock.patch.object(sidecar, '_wait_for_receipt', side_effect=simulate_timeout):
            result = self.submit()
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'SIDECAR_TIMEOUT')
        interrupted_receipt = Path(first[0]).parent / '.receipt.json.a123456_.tmp'
        interrupted_receipt.write_bytes(b'{')
        self.assertEqual(self.submit()['state'], 'LISTENER_PENDING')
        wait = sidecar._wait_for_receipt

        def simulate_retry(directory, timeout):
            with mock.patch.object(sidecar.subprocess, 'run') as deliver:
                sidecar.run_sidecar(Path(first[0]), first[1])
            deliver.assert_not_called()
            self.complete(directory)
            return wait(directory, timeout)

        with (mock.patch.object(sidecar.time, 'time', return_value=before + 121),
              mock.patch.object(sidecar, '_wait_for_receipt', side_effect=simulate_retry)):
            self.assertEqual(self.submit()['state'], 'LISTENER_REQUESTED')
        self.assertFalse(interrupted_receipt.exists())
        self.assertEqual(json.loads(self.config.read_text(encoding='utf-8')), self.original)

    def test_interrupted_initial_staging_does_not_claim_the_target_directory(self):
        target = self.config.parent / 'sidecars' / sidecar._sidecar_id(self.project, self.chat)
        with mock.patch.object(sidecar, 'atomic_write', side_effect=OSError('interrupted staging')):
            self.assertEqual(self.submit()['reason'], 'SIDECAR_CONFIG_ERROR')
        self.assertFalse(target.exists())
        self.assertEqual(json.loads(self.config.read_bytes()), self.original)

    def test_rejects_invalid_settings_and_hardlinks_without_touching_config(self):
        invalid = b'{"sidecars": {"duplicate": {}, "duplicate": {}}}'
        self.config.write_bytes(invalid)
        self.assertEqual(self.submit()['reason'], 'SIDECAR_CONFIG_INVALID')
        self.assertEqual(self.config.read_bytes(), invalid)
        valid = json.dumps(self.original).encode('utf-8')
        self.config.write_bytes(valid)
        linked = self.home / 'linked-settings.json'
        try:
            os.link(self.config, linked)
        except OSError:
            self.skipTest('hard links unavailable in isolated test directory')
        self.assertEqual(self.submit()['reason'], 'SIDECAR_CONFIG_CONFLICT')
        self.assertEqual(self.config.read_bytes(), valid)
        self.assertEqual(linked.read_bytes(), valid)

    def test_rejects_malformed_conversation_identity_before_config_writes(self):
        before = self.config.read_bytes()
        self.assertFalse(self.submit('../../other-chat')['ok'])
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse((self.config.parent / 'sidecars').exists())

    def test_unsupported_platform_never_creates_an_app_config(self):
        self.config.unlink()
        with (mock.patch.object(sidecar, '_WINDOWS', create=True, new=False),
              mock.patch.object(sidecar, '_wait_for_receipt', return_value=None)):
            self.assertEqual(self.submit()['reason'], 'SIDECAR_PLATFORM_UNAVAILABLE')
        self.assertFalse(self.config.exists())
        self.assertFalse((self.config.parent / 'sidecars').exists())
