"""Request one Antigravity Desktop listener through its documented Sidecar API."""
from __future__ import annotations

from base64 import urlsafe_b64encode
from hashlib import sha256
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from uuid import uuid4
from zipfile import ZipFile

from ct_bridge_binding import Actor, BridgeContext, _read_manifest, locked_bindings
import ct_bridge_control as control
from ct_bridge_files import locate_workspace
from ct_storage import LinkedPathError, atomic_write, locked, reject_links


_WINDOWS = os.name == 'nt'
_TTL = 120
_WAIT = 25
_SEND_TIMEOUT = 15
_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_NONCE = re.compile(r'[0-9a-f]{32}')
_DIGEST = re.compile(r'[0-9a-f]{64}')
_REASON = re.compile(r'[A-Z][A-Z0-9_]{0,63}')
_EPOCHS = {'leader_epoch', 'desktop_epoch', 'auto_epoch'}
_RECORD = 'request.json'
_MANIFEST = 'sidecar.json'
_RECEIPT = 'receipt.json'
_ARMED = 'armed'
_CLAIM = 'claimed'
_BACKUP = 'config-before.json'
_DRAFT = 'config-next.json'
_RUNNER = 'listener.pyz'
_CONFIG_ATTEMPTS = 3
_CONFIG_SLOTS = 12
_CONFIG_LIMIT = 2 * 1024 * 1024
_ABSENT = object()
_RUNTIME_SCRIPTS = ('ct_bridge_sidecar.py', 'ct_bridge_binding.py', 'ct_bridge_control.py',
                    'ct_bridge_store.py', 'ct_bridge_files.py', 'ct_bridge_commands.py',
                    'ct_platform.py', 'ct_policy.py', 'ct_storage.py')


def _result(state, reason=None):
    result = {'ok': reason is None, 'state': state, 'ready': False}
    if reason is not None:
        result['reason'] = reason
    return result


def _reason(error, fallback):
    value = getattr(error, 'reason', str(error))
    return value if isinstance(value, str) and _REASON.fullmatch(value) else fallback


def _valid(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _unique(items):
    result = dict(items)
    if len(result) != len(items):
        raise ValueError('duplicate key')
    return result


def _invalid_number(_value):
    raise ValueError('invalid JSON number')


def _decode(raw, error):
    try:
        data = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_unique, parse_constant=_invalid_number)
        if isinstance(data, dict):
            return data
    except (ValueError, RecursionError):
        pass
    raise ValueError(error)


def _encode(data):
    return (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def _read(path, limit):
    with path.open('rb') as file:
        data = file.read(limit + 1)
    if len(data) > limit:
        raise ValueError('SIDECAR_CONFIG_INVALID')
    return data


def _ordinary(path, root):
    reject_links(path, root)
    try:
        info = path.stat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('SIDECAR_CONFIG_CONFLICT')


def _settings_data(raw):
    data = _decode(raw, 'SIDECAR_CONFIG_INVALID')
    if 'sidecars' in data and not isinstance(data['sidecars'], dict):
        raise ValueError('SIDECAR_CONFIG_INVALID')
    return data


def _settings(path):
    _ordinary(path, path.parent)
    try:
        raw = _read(path, _CONFIG_LIMIT)
    except FileNotFoundError:
        return None, {}
    return raw, _settings_data(raw)


def _sidecar_id(project_id, chat_id):
    digest = sha256(f'{project_id}/{chat_id}'.encode('ascii')).hexdigest()
    return 'coding-team-listen-' + digest[:24]


def _stage_paths(directory):
    reject_links(directory, directory.parent.parent)
    files = {_RECORD, _MANIFEST, _RECEIPT, _ARMED, _CLAIM, _BACKUP, _DRAFT, _RUNNER}
    files.update(path.name for index in range(_CONFIG_SLOTS) for path in _config_step(directory, index))
    for child in directory.iterdir():
        transient = re.fullmatch(r'\.(.+)\.[a-z0-9_]{8}\.tmp', child.name)
        if child.name not in files and (transient is None or transient[1] not in files):
            raise ValueError('SIDECAR_CONFIG_CONFLICT')
        _ordinary(child, directory)


def _record_at(directory):
    try:
        path = directory / _RECORD
        _ordinary(path, directory.parent.parent)
        request = _decode(_read(path, 16 * 1024), 'SIDECAR_CONFIG_CONFLICT')
    except FileNotFoundError:
        raise ValueError('SIDECAR_CONFIG_CONFLICT') from None
    project, chat = request.get('project_id'), request.get('chat_id')
    deadline = request.get('expires_at')
    epochs = request.get('epochs')
    if (request.get('schema') != 1 or not _valid(_UUID, project) or not _valid(_UUID, chat)
            or _sidecar_id(project, chat) != directory.name or not _valid(_NONCE, request.get('nonce'))
            or type(deadline) not in (int, float) or not math.isfinite(deadline)
            or not _valid(_DIGEST, request.get('policy_digest')) or not isinstance(epochs, dict)
            or epochs.keys() != _EPOCHS or any(type(epoch) is not int or epoch < 0 for epoch in epochs.values())
            or type(request.get('had_sidecars')) is not bool):
        raise ValueError('SIDECAR_CONFIG_CONFLICT')
    return request


def _replace_with_backup(config, draft, backup):
    """Windows preserves the file actually replaced, including a racing App write."""
    if os.name != 'nt':
        raise ValueError('SIDECAR_PLATFORM_UNAVAILABLE')
    import ctypes
    from ctypes import wintypes

    replace = ctypes.WinDLL('kernel32', use_last_error=True).ReplaceFileW
    replace.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                        wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID)
    replace.restype = wintypes.BOOL
    if not replace(str(config), str(draft), str(backup), 0, None, None):
        raise ctypes.WinError(ctypes.get_last_error())


def _sidecar_entry(data, name):
    entries = data.get('sidecars', {})
    if name not in entries:
        return None
    entry = entries[name]
    if not isinstance(entry, dict) or type(entry.get('enabled')) is not bool:
        raise ValueError('SIDECAR_CONFIG_CONFLICT')
    return entry


def _other_settings(data, name, request):
    if _sidecar_entry(data, name) is None:
        return data
    result = dict(data)
    entries = dict(data['sidecars'])
    del entries[name]
    if entries or request['had_sidecars']:
        result['sidecars'] = entries
    else:
        del result['sidecars']
    return result


def _merge_settings(before, displaced, current):
    """Replay a late edit only where the newest version can keep both changes."""
    if displaced == before or displaced == current:
        return current
    if current == before:
        return displaced
    if isinstance(displaced, dict) and isinstance(current, dict) and (isinstance(before, dict) or before is _ABSENT):
        baseline = {} if before is _ABSENT else before
        merged = {}
        for key in sorted(baseline.keys() | displaced.keys() | current.keys()):
            value = _merge_settings(baseline.get(key, _ABSENT), displaced.get(key, _ABSENT),
                                    current.get(key, _ABSENT))
            if value is not _ABSENT:
                merged[key] = value
        return merged
    raise ValueError('SIDECAR_CONFIG_CONFLICT')


def _config_step(directory, index):
    prefix = f'config-{index:02d}-'
    return tuple(directory / (prefix + part + '.json') for part in ('before', 'draft', 'displaced'))


def _replay_settings(raw, data, directory, request):
    name = directory.name
    replacements = []
    for index in range(_CONFIG_SLOTS):
        expected, _, backup = _config_step(directory, index)
        if backup.is_file():
            replacements.append((_read(expected, _CONFIG_LIMIT), _read(backup, _CONFIG_LIMIT)))
    if raw is None and replacements:
        # Windows may have moved the previous config before a failed rename.
        # Recreate it exclusively, then replay edits from preceding swaps too.
        data = _settings_data(replacements[-1][1])
    newest = _other_settings(data, name, request)
    for before, actual in replacements:
        if before != actual:
            baseline = _other_settings(_settings_data(before), name, request)
            concurrent = _other_settings(_settings_data(actual), name, request)
            newest = _merge_settings(baseline, concurrent, newest)
    return newest


def _write_settings(config, directory, raw, data):
    for index in range(_CONFIG_SLOTS):
        expected, draft, backup = _config_step(directory, index)
        if not any(part.exists() for part in (expected, draft, backup)):
            break
    else:
        raise ValueError('SIDECAR_CONFIG_CONFLICT')
    payload = _encode(data)
    if len(payload) > _CONFIG_LIMIT:
        raise ValueError('SIDECAR_CONFIG_INVALID')
    atomic_write(expected, raw if raw is not None else b'')
    atomic_write(draft, payload)
    if raw is not None:
        _replace_with_backup(config, draft, backup)
        return
    try:
        if os.name == 'nt':
            # A Windows rename without replacement only succeeds if absent.
            os.rename(draft, config)
        else:
            os.link(draft, config)
            draft.unlink()
    except FileExistsError:
        # The App created its own settings first. The next read merges from it.
        return


def _clear_config_steps(directory):
    for index in range(_CONFIG_SLOTS):
        before, draft, backup = _config_step(directory, index)
        for child in (backup, draft, before):
            _ordinary(child, directory)
            child.unlink(missing_ok=True)
    for name in (_BACKUP, _DRAFT):
        child = directory / name
        _ordinary(child, directory)
        child.unlink(missing_ok=True)


def _update_settings(config, directory, request, *, enabled):
    """Reconcile with the App's actual writes before clearing recovery files."""
    name = directory.name
    for attempt in range(_CONFIG_ATTEMPTS + 1):
        raw, current = _settings(config)
        current_entry = _sidecar_entry(current, name)
        desired = dict(_replay_settings(raw, current, directory, request))
        if enabled:
            if current_entry is not None and not current_entry['enabled']:
                raise ValueError('SIDECAR_DISABLED')
            entries = dict(desired.get('sidecars', {}))
            # The App can assign a project to the Sidecar independently of the conversation target.
            entries[name] = current_entry or {'enabled': True}
            desired['sidecars'] = entries
        if desired == current:
            _clear_config_steps(directory)
            return
        if attempt == _CONFIG_ATTEMPTS:
            break
        _write_settings(config, directory, raw, desired)
    raise ValueError('SIDECAR_CONFIG_CONFLICT')


def _retire(config, directory, request, *, expired=False):
    _stage_paths(directory)
    (directory / _ARMED).unlink(missing_ok=True)
    _update_settings(config, directory, request, enabled=False)
    (directory / _MANIFEST).unlink(missing_ok=True)
    (directory / _RUNNER).unlink(missing_ok=True)
    if expired:
        with TemporaryDirectory(prefix='coding-team-retired-', dir=config.parent) as holding:
            directory.rename(Path(holding) / directory.name)


def _wait_for_receipt(directory, seconds):
    end = time.monotonic() + seconds
    path = directory / _RECEIPT
    while True:
        try:
            _ordinary(path, directory)
            receipt = _decode(_read(path, 1024), 'SIDECAR_CONFIG_CONFLICT')
        except FileNotFoundError:
            if time.monotonic() >= end:
                return None
            time.sleep(0.25)
            continue
        return receipt


def _runtime_archive(ctx):
    """Copy only the verified Python closure into an isolated executable archive."""
    binding = ctx.own_binding()
    output = BytesIO()
    with locked_bindings(binding), ZipFile(output, 'w') as snapshot:
        manifest, digest = _read_manifest(binding.skill_dir, 'leader', require_catalog=True)
        if digest != binding._manifest_digest:
            raise ValueError('INSTALLATION_INCOMPATIBLE')
        entries = manifest['files']['skill_dir']
        for name in _RUNTIME_SCRIPTS:
            relative = 'scripts/' + name
            source = binding.skill_dir / relative
            _ordinary(source, binding.skill_dir)
            data = source.read_bytes()
            if entries.get(relative) != sha256(data).hexdigest():
                raise ValueError('INSTALLATION_INCOMPATIBLE')
            snapshot.writestr(name, data)
        snapshot.writestr('__main__.py', 'import sys\nfrom ct_bridge_sidecar import run_sidecar\n'
                          'if len(sys.argv) == 3:\n    run_sidecar(sys.argv[1], sys.argv[2])\n')
    return output.getvalue()


def _stage(ctx, config, directory, project_id, chat_id, epochs, worker_id, policy_digest):
    _, data = _settings(config)
    if not _WINDOWS:
        raise ValueError('SIDECAR_PLATFORM_UNAVAILABLE')
    had_sidecars = 'sidecars' in data
    if directory.name in data.get('sidecars', {}):
        raise ValueError('SIDECAR_CONFIG_CONFLICT')
    archive = _runtime_archive(ctx)
    record = {
        'schema': 1,
        'nonce': uuid4().hex,
        'expires_at': time.time() + _TTL,
        'project_id': project_id,
        'chat_id': chat_id,
        'workspace': str(ctx.paths.root),
        'leader_chat': ctx.require_leader().chat_id,
        'source_root': str(ctx.source_root),
        'codex_home': ctx.env.get('CODEX_HOME'),
        'epochs': epochs,
        'worker_id': worker_id,
        'policy_digest': policy_digest,
        'had_sidecars': had_sidecars,
    }
    manifest = {
        'display_name': 'Coding Team listener',
        'description': 'Request a listener in the selected Antigravity chat once',
        'command': sys.executable,
        'args': ['-I', '-S', '-B', str(directory / _RUNNER), str(directory / _RECORD), record['nonce']],
        'restart_policy': 'never',
    }
    directory.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='coding-team-stage-', dir=config.parent) as holding:
        staged = Path(holding) / directory.name
        staged.mkdir()
        atomic_write(staged / _RECORD, _encode(record))
        atomic_write(staged / _RUNNER, archive)
        atomic_write(staged / _MANIFEST, _encode(manifest))
        staged.rename(directory)
    _update_settings(config, directory, record, enabled=True)
    (directory / _ARMED).touch(exist_ok=False)
    return record


def submit_listener(ctx, project_id, chat_id, epochs, worker_id, policy_digest):
    """Register briefly, wait for handoff evidence, and remove only our Sidecar."""
    if not _valid(_UUID, project_id) or not _valid(_UUID, chat_id) or not _valid(_DIGEST, policy_digest):
        return _result('LISTENER_UNAVAILABLE', 'INVALID_SIDECAR_TARGET')
    if (not isinstance(epochs, dict) or epochs.keys() != _EPOCHS
            or any(type(epoch) is not int or epoch < 0 for epoch in epochs.values())):
        return _result('LISTENER_UNAVAILABLE', 'INVALID_SIDECAR_TARGET')
    root = ctx.home / '.gemini/config'
    config = root / 'config.json'
    directory = root / 'sidecars' / _sidecar_id(project_id, chat_id)
    lock = root / 'coding-team-listener'
    try:
        ctx.require_leader()
        reject_links(directory, ctx.home)
        if not root.is_dir() or directory.is_relative_to(ctx.paths.root):
            return _result('LISTENER_UNAVAILABLE', 'SIDECAR_CONFIG_MISSING')
        _ordinary(root / 'coding-team-listener.lock', ctx.home)
        with locked(lock):
            if directory.exists():
                previous = _record_at(directory)
                if time.time() < previous['expires_at']:
                    return _result('LISTENER_PENDING')
                _retire(config, directory, previous, expired=True)
            request = _stage(ctx, config, directory, project_id, chat_id, epochs, worker_id, policy_digest)
        try:
            receipt = _wait_for_receipt(directory, _WAIT)
        finally:
            with locked(lock):
                if _record_at(directory)['nonce'] != request['nonce']:
                    raise ValueError('SIDECAR_CONFIG_CONFLICT')
                _retire(config, directory, request)
        if receipt is None:
            return _result('LISTENER_UNAVAILABLE', 'SIDECAR_TIMEOUT')
        if receipt.get('nonce') != request['nonce'] or not _valid(_REASON, receipt.get('outcome')):
            return _result('LISTENER_UNAVAILABLE', 'SIDECAR_CONFIG_CONFLICT')
        if receipt['outcome'] == 'SUBMITTED':
            return _result('LISTENER_REQUESTED')
        return _result('LISTENER_UNAVAILABLE', receipt['outcome'])
    except LinkedPathError:
        return _result('LISTENER_UNAVAILABLE', 'SIDECAR_CONFIG_CONFLICT')
    except TimeoutError:
        return _result('LISTENER_UNAVAILABLE', 'SIDECAR_CONFIG_BUSY')
    except (ValueError, OSError, RecursionError) as exc:
        return _result('LISTENER_UNAVAILABLE', _reason(exc, 'SIDECAR_CONFIG_ERROR'))


def _agentapi(config):
    candidate = Path(shutil.which('agentapi') or '')
    if not candidate.is_absolute():
        raise ValueError('SIDECAR_API_MISSING')
    if os.name == 'nt' and candidate.suffix.lower() != '.exe':
        app_bin = config.parents[1] / 'antigravity/bin'
        if (candidate.parent != app_bin or candidate.name.lower() not in ('agentapi.bat', 'agentapi.cmd')
                or any(char in str(candidate) for char in '%!&|<>^"\n\r')):
            raise ValueError('SIDECAR_API_MISSING')
        _ordinary(candidate, config.parents[2])
    return candidate


def _deliver(directory, config, request):
    _, data = _settings(config)
    entry = _sidecar_entry(data, directory.name)
    if entry is None or not entry['enabled'] or not (directory / _MANIFEST).is_file():
        return 'SIDECAR_STALE_REQUEST'
    agentapi = _agentapi(config)
    workspace = locate_workspace(Path(request['workspace']))
    if workspace.root != Path(request['workspace']) or agentapi.is_relative_to(workspace.root):
        return 'SIDECAR_STALE_REQUEST'
    codex_home = request['codex_home']
    env = {'CODEX_HOME': codex_home} if codex_home else {}
    ctx = BridgeContext(workspace, Actor('codex', request['leader_chat']), 'leader',
                        Path(request['source_root']), env, config.parents[2])
    control.listener_start_fence(ctx, request['epochs'], request['chat_id'], request['worker_id'],
                                 policy_digest=request['policy_digest'])
    if time.time() >= request['expires_at'] or not (directory / _ARMED).is_file():
        return 'SIDECAR_STALE_REQUEST'
    startup = {**request['epochs'], **{key: request[key] for key in
              ('chat_id', 'worker_id', 'policy_digest', 'expires_at')}}
    framed = json.dumps(startup, separators=(',', ':')).encode('ascii')
    # The Windows app provides a batch wrapper. Keep message data on one shell-safe line.
    token = urlsafe_b64encode(framed).rstrip(b'=').decode('ascii')
    prompt = '/coding-team listen startup ct1-' + token
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    result = subprocess.run([str(agentapi), 'send-message', request['chat_id'], prompt],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=_SEND_TIMEOUT, creationflags=flags, check=False)
    return 'SUBMITTED' if result.returncode == 0 else 'SIDECAR_SEND_FAILED'


def _ready_to_deliver(directory, request):
    deadline = time.monotonic() + _WAIT
    armed = directory / _ARMED
    while time.time() < request['expires_at'] and (directory / _MANIFEST).is_file():
        _ordinary(armed, directory)
        if armed.is_file():
            return True
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    return False


def run_sidecar(record_path, nonce):
    """Entry point launched by Antigravity with its supplied agentapi PATH."""
    path = Path(record_path)
    directory = path.parent
    if (not path.is_absolute() or path.name != _RECORD or not _valid(_NONCE, nonce)
            or [part.name for part in directory.parents][:3] != ['sidecars', 'config', '.gemini']):
        return
    try:
        reject_links(path, Path(path.anchor))
        request = _record_at(directory)
        if request['nonce'] != nonce or not _ready_to_deliver(directory, request):
            return
        (directory / _CLAIM).touch(exist_ok=False)
    except (ValueError, OSError):
        return
    try:
        outcome = _deliver(directory, directory.parents[1] / 'config.json', request)
    except subprocess.TimeoutExpired:
        outcome = 'SIDECAR_TIMEOUT'
    except (ValueError, OSError, TimeoutError, KeyError, TypeError) as exc:
        outcome = _reason(exc, 'SIDECAR_AUTHORITY_CHANGED')
    try:
        if time.time() < request['expires_at'] and _record_at(directory)['nonce'] == nonce:
            atomic_write(directory / _RECEIPT, _encode({'nonce': nonce, 'outcome': outcome}))
    except (ValueError, OSError):
        # Submission may have succeeded. The caller observes a timeout and retains the claim.
        pass
