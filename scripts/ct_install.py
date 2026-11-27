"""Transactional install/uninstall of the coding-team skill, agents and global rules block.

Every change to a target is preceded by a backup and an install journal; Python errors
roll back automatically, a killed process leaves the journal for `recover`.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys

import ct_policy
import ct_rules
from ct_platform import resolve_config_root, target_paths
from ct_roles import load_roles, render_all
from ct_storage import LinkedPathError, atomic_write, locked, reject_links

MANIFEST_NAME = 'install-manifest.json'
LOCK_TIMEOUT = 30
ROOT_LABELS = ('skill_dir', 'agents_dir')
_RETIRED_SCRIPTS = {'ct_antigravity.py', 'ct_winjob.py', 'ct_external.py',
                    'ct_external_store.py', 'ct_external_workspace.py'}
_EXCLUDED_SCRIPTS = {'export_evaluation_inputs.py', 'build_runtime_package.py'} | _RETIRED_SCRIPTS
_WORKER_SCRIPTS = ('ct_bridge_worker.py', 'ct_bridge_files.py', 'ct_bridge_store.py',
                   'ct_bridge_binding.py', 'ct_bridge_control.py', 'ct_bridge_attempts.py',
                   'ct_bridge_install.py', 'ct_bridge_commands.py', 'ct_storage.py',
                   'ct_platform.py', 'ct_policy.py')
_SHA = re.compile(r'[0-9a-f]{64}')
_RECOVER_HINT = 'run `coding_team.py install --recover`'


class _PathsError(ValueError):
    def __init__(self, message, paths=()):
        self.paths = [str(p) for p in paths]
        super().__init__(message + (': ' + ', '.join(self.paths) if self.paths else ''))


class UnmanagedTargetError(_PathsError):
    """Target exists but is not managed by coding-team (needs replace_unmanaged)."""


class ModifiedTargetError(_PathsError):
    """Managed target was edited by the user (needs overwrite_modified)."""


class IncompleteTransactionError(_PathsError):
    """An earlier transaction left a journal, or a rollback could not finish."""


class RecoveryUnsafeError(_PathsError):
    """Journal or backups cannot be trusted; nothing was restored."""


class InstallRolledBack(RuntimeError):
    def __init__(self, message, backup_dir=None):
        super().__init__(message)
        self.backup_dir = backup_dir


@dataclass
class InstallPlan:
    platform: str
    scope: str
    paths: dict
    files: list            # [(root label, POSIX relpath, bytes)]
    rules_block: str | None = None
    state_default: str | None = None
    options: dict = field(default_factory=dict)


@dataclass
class _Target:
    path: Path
    data: bytes | None     # new bytes; None deletes (ignored when action is set)
    old: bytes | None      # current bytes; None if absent
    label: str             # path inside the backup directory
    action: object = None  # callable replacing the default write/delete


# --- small helpers -------------------------------------------------------------------------

def _require(condition, message):
    """Explicit validation (not `assert`, which `python -O` strips)."""
    if not condition:
        raise ValueError(message)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _posix(path):
    return Path(path).as_posix()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _read(path):
    """Bytes of an existing regular file, None if absent; a directory is an error."""
    path = Path(path)
    if path.is_dir():
        raise UnmanagedTargetError('target is a directory, not a file', [_posix(path)])
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _safe_rel(rel):
    if (not isinstance(rel, str) or not rel or '\\' in rel or ':' in rel
            or PurePosixPath(rel).is_absolute()
            or any(part in ('', '.', '..') for part in rel.split('/'))):
        raise ValueError(f'unsafe relative path {rel!r}')
    return rel


def _inside(path, root):
    path, root = Path(os.path.abspath(path)), Path(os.path.abspath(root))
    return path == root or root in path.parents


def _stop_at(path, paths):
    """Where link checks stop (inclusive), Ruling R7.

    Links at or above the config root are the user's chosen location and allowed; under
    the config root checks stop at the first component strictly below it. Paths outside
    the config root (Codex skill dir, project scope) stop at their deepest managed root.
    """
    path = Path(os.path.abspath(path))
    base = Path(os.path.abspath(paths['config_root']))
    if base in path.parents:
        return base / path.relative_to(base).parts[0]
    best = None
    for root in paths['managed_roots']:
        root = Path(os.path.abspath(root))
        if _inside(path, root) and (best is None or len(root.parts) > len(best.parts)):
            best = root
    if best is None:
        raise LinkedPathError('path is outside the managed roots: ' + _posix(path))
    return best


def _check_links(paths_list, paths):
    for path in paths_list:
        reject_links(path, _stop_at(path, paths))


def _linked(paths_list, paths):
    """Read-only variant of _check_links: the linked paths, in order."""
    found = []
    for path in paths_list:
        try:
            _check_links([path], paths)
        except LinkedPathError:
            found.append(_posix(path))
    return found


def _resolve_paths(cfg, scope, env, home, project_dir, desktop_worker=False):
    paths = dict(target_paths(cfg, scope, env, home, project_dir))
    paths['config_root'] = resolve_config_root(cfg, env, Path(home))
    if desktop_worker:
        if cfg['platform'] != 'antigravity':
            raise ValueError('--desktop-worker requires --platform antigravity')
        skill = Path(paths['skill_dir']).with_name('coding-team-desktop-worker')
        state_root = Path(paths['state_file']).parent.with_name('coding-team-desktop-worker')
        paths.update(skill_dir=skill, agents_dir=skill, global_rules_file=None,
                     state_file=state_root / 'state.json', backup_root=state_root / 'backups',
                     lock_file=state_root / 'install.lock', journal_file=state_root / 'install-journal.json',
                     managed_roots=[skill, state_root], bridge_component='worker')
    elif cfg['platform'] == 'codex':
        paths['bridge_component'] = 'leader'
    return paths


def _write_new(path, data):
    """Durably create a new file (backups and staging); never overwrites."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _is_link(path):
    """True for a symlink, junction or other reparse point (the path itself only)."""
    try:
        reject_links(path, path)
        return False
    except LinkedPathError:
        return True


def _rmdir_if_empty(directory, kept_links=None):
    """rmdir an empty real directory. Never a link: on Windows rmdir removes a junction or
    directory symlink even when its target is not empty. Links are reported in kept_links."""
    if _is_link(directory):
        if kept_links is not None and _posix(directory) not in kept_links:
            kept_links.append(_posix(directory))
        return False
    try:
        Path(directory).rmdir()
        return True
    except OSError:
        return False


def _prune(directory, root, include_root, kept_links=None):
    """Remove empty directories from directory up to root; stop at the first link."""
    directory, root = Path(directory), Path(root)
    while _inside(directory, root):
        if directory == root and not include_root:
            return
        if not _rmdir_if_empty(directory, kept_links) or directory == root:
            return
        directory = directory.parent


def _missing_parents(path):
    found, current = [], Path(path).parent
    while not current.exists() and current != current.parent:
        found.append(_posix(current))
        current = current.parent
    return found


def _dir_allowed(directory, paths):
    """created_dirs entries may only be managed roots, their ancestors or descendants."""
    roots = [paths['skill_dir'], paths['agents_dir']]
    if paths['global_rules_file'] is not None:
        roots.append(Path(paths['global_rules_file']).parent)
    return any(_inside(directory, r) or _inside(r, directory) for r in roots)


def _rules_after(original, block):
    """Pure equivalent of ct_rules.upsert_block (checked against it by tests)."""
    payload = block.encode('utf-8')
    found = ct_rules._markers(original)
    if found:
        return original[:found[0]] + payload + original[found[1]:]
    return original + (b'\n' if original else b'') + payload + b'\n'


def _rules_without(original):
    """Pure equivalent of ct_rules.remove_block."""
    found = ct_rules._markers(original)
    if not found:
        return original
    before, after = original[:found[0]], original[found[1]:]
    if after in (b'', b'\n'):
        after = b''
        if before.endswith(b'\n'):
            before = before[:-1]
    return before + after


def _json_bytes(data):
    return (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8')


# --- plan ----------------------------------------------------------------------------------

def _skill_files(source_root, cfg):
    root = Path(source_root)
    rels = ['SKILL.md', 'LICENSE']
    for sub in ('references', 'agents/roles'):
        rels += sorted(p.relative_to(root).as_posix() for p in (root / sub).rglob('*')
                       if p.is_file() and '__pycache__' not in p.parts
                       and p.relative_to(root).as_posix() != 'references/antigravity-cli.md')
    rels += sorted(f'scripts/{p.name}' for p in (root / 'scripts').glob('*.py')
                   if p.is_file() and p.name not in _EXCLUDED_SCRIPTS)
    if cfg['platform'] == 'codex':
        rels.append('agents/openai.yaml')
    files = [(_safe_rel(rel), (root / rel).read_bytes().replace(b'\r\n', b'\n')) for rel in rels]
    rel = f'platforms/{cfg["platform"]}.json'
    source = root / rel
    data = source.read_bytes().replace(b'\r\n', b'\n') if source.is_file() else None
    if data is None or json.loads(data.decode('utf-8-sig')) != cfg:
        data = (json.dumps(cfg, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    files.append((rel, data))
    return files


def build_plan(source_root, cfg, *, scope, env, home, project_dir=None, default=None,
               global_rules=False, python=None, docs_mcp=(), desktop_worker=False) -> InstallPlan:
    docs_mcp = list(docs_mcp or ())
    if desktop_worker and (default is not None or global_rules or docs_mcp):
        raise ValueError('--desktop-worker cannot manage native policy, rules or docs MCP')
    if scope == 'project' and (default is not None or global_rules):
        raise ValueError('--scope project cannot be combined with --default or --global-rules')
    if docs_mcp and cfg['platform'] != 'claude-code':
        raise ValueError('--docs-mcp is only supported on claude-code')
    if default is not None and str(default).upper() not in ('ON', 'OFF'):
        raise ValueError('default must be on or off')
    paths = _resolve_paths(cfg, scope, env, home, project_dir, desktop_worker)
    source_root = Path(source_root)
    if desktop_worker:
        resources = [('SKILL.md', source_root / 'workers/antigravity/SKILL.md'),
                     ('LICENSE', source_root / 'LICENSE')]
        resources += [('scripts/' + name, source_root / 'scripts' / name) for name in _WORKER_SCRIPTS]
        resources.append(('platforms/antigravity.json', source_root / 'platforms/antigravity.json'))
        files = [('skill_dir', rel, path.read_bytes().replace(b'\r\n', b'\n'))
                 for rel, path in resources]
        return InstallPlan(cfg['platform'], scope, paths, files, options={'desktop_worker': True})
    roles = load_roles(source_root / 'agents' / 'roles')
    agents = render_all(roles, cfg, {'docs_lookup': docs_mcp} if docs_mcp else None)
    files = [('skill_dir', rel, data) for rel, data in _skill_files(source_root, cfg)]
    files += [('agents_dir', _safe_rel(rel), data) for rel, data in agents]
    block = None
    if global_rules:
        skill = Path(paths['skill_dir'])
        block = ct_rules.render_rules_block(
            platform=cfg['platform'], python=python or sys.executable,
            cli=skill / 'scripts' / 'coding_team.py', state=paths['state_file'], skill_dir=skill)
    return InstallPlan(cfg['platform'], scope, paths, files, block,
                       None if default is None else str(default).lower(), {'docs_mcp': docs_mcp})


# --- manifest ------------------------------------------------------------------------------

def _load_manifest(skill_dir):
    path = Path(skill_dir) / MANIFEST_NAME
    raw = _read(path)
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode('utf-8'))
        _require(isinstance(data, dict) and data.get('schema_version') == 1, 'schema_version must be 1')
        _require(isinstance(data.get('platform'), str) and data.get('scope') in ('user', 'project')
                 and isinstance(data.get('installed_at'), str), 'bad platform/scope/installed_at')
        files = data.get('files')
        _require(isinstance(files, dict) and set(files) <= set(ROOT_LABELS), 'bad files')
        for entries in files.values():
            _require(isinstance(entries, dict), 'bad files entry')
            for rel, digest in entries.items():
                _safe_rel(rel)
                _require(isinstance(digest, str) and _SHA.fullmatch(digest), f'bad hash for {rel!r}')
        _require(data.get('rules_file') is None or isinstance(data['rules_file'], str), 'bad rules_file')
        dirs = data.setdefault('created_dirs', [])
        _require(isinstance(dirs, list) and all(isinstance(d, str) for d in dirs), 'bad created_dirs')
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f'invalid install manifest {_posix(path)}: {exc}') from None
    return data


def _check_identity(manifest, platform, scope, skill_dir):
    if manifest['platform'] != platform or manifest['scope'] != scope:
        raise ValueError(f'{_posix(skill_dir)} is managed by a {manifest["platform"]} '
                         f'({manifest["scope"]} scope) installation; uninstall it first')


def _bridge_binding(paths):
    result = {'bridge_abi': 1, 'component': paths['bridge_component'],
              'install_lock': Path(paths['lock_file']).resolve().as_posix()}
    if paths['bridge_component'] == 'leader':
        result['state_path'] = Path(paths['state_file']).resolve().as_posix()
    return result


def _check_bridge_binding(record, paths):
    if 'bridge_component' in paths and record:
        expected = _bridge_binding(paths)
        # Legacy manifests may have state/lock bindings but no Desktop ABI yet.
        if any(key in record and record[key] != value for key, value in expected.items()):
            raise ValueError('installation is bound to a different Codex policy state or install lock '
                             'or Desktop component; use the existing installation configuration')


def _journal_guard(paths):
    if Path(paths['journal_file']).exists():
        raise IncompleteTransactionError(
            f'an earlier install/uninstall did not finish; {_RECOVER_HINT}', [_posix(paths['journal_file'])])


# --- assessment (read-only) ----------------------------------------------------------------

def _assess(plan):
    paths = plan.paths
    skill_dir = Path(paths['skill_dir'])
    manifest = _load_manifest(skill_dir)
    if manifest:
        _check_identity(manifest, plan.platform, plan.scope, skill_dir)
        _check_bridge_binding(manifest, paths)
    known = manifest['files'] if manifest else {}
    r = {'targets': [], 'written': [], 'unchanged': [], 'removed_stale': [], 'kept_modified_stale': [],
         'modified_overwritten': [], 'replaced_unmanaged': [], 'state_change': False,
         'conflicts': {'unmanaged': [], 'modified': [], 'linked': []}}

    def linked(path):
        found = _linked([path], paths)
        r['conflicts']['linked'] += found
        return bool(found)

    new_files = {label: {} for label in ROOT_LABELS}
    for label, rel, data in plan.files:
        dest = Path(paths[label]) / rel
        new_files[label][rel] = _sha(data)
        if linked(dest):
            continue
        old, digest = _read(dest), known.get(label, {}).get(rel)
        if old is not None and digest is None:
            r['conflicts']['unmanaged'].append(_posix(dest))
            bucket = 'replaced_unmanaged'
        elif old is not None and _sha(old) != digest:
            r['conflicts']['modified'].append(_posix(dest))
            bucket = 'modified_overwritten'
        else:
            bucket = None
        if old == data:
            r['unchanged'].append(_posix(dest))
            continue
        r['targets'].append(_Target(dest, data, old, f'{label}/{rel}'))
        r['written'].append(_posix(dest))
        if bucket:
            r[bucket].append(_posix(dest))

    for label in ROOT_LABELS:
        for rel, digest in known.get(label, {}).items():
            if rel in new_files[label]:
                continue
            dest = Path(paths[label]) / rel
            if linked(dest):
                continue
            old = _read(dest)
            if old is None:
                continue
            if _sha(old) == digest:
                r['targets'].append(_Target(dest, None, old, f'{label}/{rel}'))
                r['removed_stale'].append(_posix(dest))
            else:
                r['kept_modified_stale'].append(_posix(dest))
                new_files[label][rel] = digest

    rules_file = manifest.get('rules_file') if manifest else None
    created = bool(manifest.get('rules_file_created')) if manifest else False
    if plan.rules_block is not None and not linked(paths['global_rules_file']):
        path = Path(paths['global_rules_file'])
        old = _read(path)
        new = _rules_after(old or b'', plan.rules_block)
        if new != old:
            r['targets'].append(_Target(path, new, old, f'rules/{path.name}'))
            r['written'].append(_posix(path))
        if rules_file != _posix(path):
            rules_file, created = _posix(path), old is None
    r['rules_file'] = rules_file

    leader = paths.get('bridge_component') == 'leader'
    manages_state = paths.get('bridge_component') != 'worker' and (
        plan.scope == 'user' or (leader and Path(paths['state_file']).exists()))
    if manages_state and not linked(paths['state_file']):
        state_path = Path(paths['state_file'])
        old = _read(state_path)
        if leader:
            payload = ct_policy.prepare_initialization(state_path, plan.state_default, repair_epochs=True)
            if payload is not None:
                r['state_change'] = True
                r['targets'].append(_Target(state_path, payload, old, f'state/{state_path.name}'))
        else:
            if old is not None:
                state = ct_policy.load_state(state_path)
            default = plan.state_default
            if old is None or (default is not None and state['global_policy'] != default.upper()):
                r['state_change'] = True
                r['targets'].append(_Target(state_path, None, old, f'state/{state_path.name}',
                                            lambda: ct_policy.initialize_state(state_path, default)))

    manifest_path = skill_dir / MANIFEST_NAME
    created_dirs = set(manifest['created_dirs']) if manifest else set()
    for target in r['targets'] + [_Target(manifest_path, b'', None, '')]:
        if target.old is None:
            created_dirs.update(_missing_parents(target.path))
    content = {'schema_version': 1, 'platform': plan.platform, 'scope': plan.scope,
               'installed_at': manifest['installed_at'] if manifest else None,
               'options': plan.options, 'files': new_files, 'rules_file': rules_file,
               'rules_file_created': created, 'created_dirs': sorted(created_dirs)}
    if 'bridge_component' in paths:
        content.update(_bridge_binding(paths))
    file_change = any(t.action is None for t in r['targets'])
    if file_change or content != manifest:
        content['installed_at'] = _now()
        old = _read(manifest_path)
        r['targets'].append(_Target(manifest_path, _json_bytes(content), old,
                                    f'skill_dir/{MANIFEST_NAME}'))
    r['created_dirs'] = sorted(created_dirs - (set(manifest['created_dirs']) if manifest else set()))
    return r


def preview(plan) -> dict:
    """Read-only report of what apply_plan would do if every conflict were permitted."""
    r = _assess(plan)
    targets = r['targets']
    linked = r['conflicts']['linked']
    r['conflicts']['linked'] = list(dict.fromkeys(_linked(_plan_link_set(plan), plan.paths) + linked))
    return {
        'would_write': [_posix(t.path) for t in targets if t.data is not None or t.action],
        'would_backup': [_posix(t.path) for t in targets if t.old is not None],
        'would_remove': [_posix(t.path) for t in targets if t.data is None and not t.action],
        'unchanged': r['unchanged'],
        'kept_modified_stale': r['kept_modified_stale'],
        'conflicts': r['conflicts'],
        'journal_present': Path(plan.paths['journal_file']).exists(),
    }


# --- transaction ---------------------------------------------------------------------------

def _stamp(backup_root):
    base = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    stamp, n = base, 0
    while (backup_root / stamp).exists() or (backup_root / f'.staging-{stamp}').exists():
        n += 1
        stamp = f'{base}-{n}'
    return stamp


def _verify_backups(entries, backup_root):
    bad = []
    for entry in entries:
        if not entry['existed']:
            continue
        backup = entry.get('backup')
        try:
            ok = (isinstance(backup, str) and _inside(backup, backup_root)
                  and _sha(Path(backup).read_bytes()) == entry.get('backup_sha256'))
        except OSError:
            ok = False
        if not ok:
            bad.append(str(backup))
    return bad


def _restore_entry(entry):
    path = Path(entry['path'])
    current = _read(path)
    ours = (current is not None and entry.get('new_sha256') is not None
            and _sha(current) == entry['new_sha256'])
    if entry['existed']:
        data = Path(entry['backup']).read_bytes()
        if current == data:
            return None
        if current is not None and not ours:
            return 'kept'
        atomic_write(path, data)
        return 'restored'
    if current is not None:
        if not ours:
            return 'kept'
        path.unlink()
        return 'deleted'
    return None


def _restore(entries, created_dirs, paths, kept_links=None):
    """Undo journaled targets (newest first).

    A target is only overwritten or deleted while it still holds exactly what the
    transaction wrote (`new_sha256`) or is absent; anything else was changed by someone
    after the transaction and is kept and reported (Ruling R9). Created directories that
    are now links are kept and reported in kept_links.
    Returns (restored, deleted_new, kept_unexpected, failed).
    """
    restored, deleted, kept, failed = [], [], [], []
    outputs = {'restored': restored, 'deleted': deleted, 'kept': kept}
    for entry in reversed(entries):
        try:
            result = _restore_entry(entry)
            if result is not None:
                outputs[result].append(entry['path'])
        except Exception:  # keep going; the journal stays for a later recover
            failed.append(entry['path'])
    for directory in sorted(created_dirs, key=lambda d: len(Path(d).parts), reverse=True):
        if _dir_allowed(directory, paths):
            _rmdir_if_empty(directory, kept_links)
    return restored, deleted, kept, failed


def _discard_staging(staging, backup_root):
    shutil.rmtree(staging, ignore_errors=True)
    _rmdir_if_empty(backup_root)


@contextmanager
def _component_lock(paths):
    """Keep native policy fenced through assessment, staging, publication and undo."""
    with locked(paths['lock_file'], timeout=LOCK_TIMEOUT):
        if paths.get('bridge_component') == 'leader':
            _check_links([paths['state_file'], _lock_path(paths['state_file'])], paths)
            with locked(paths['state_file'], timeout=LOCK_TIMEOUT):
                yield
        else:
            yield


def _desktop_guard(paths):
    if 'bridge_component' in paths:
        from ct_bridge_install import guard
        guard(Path(paths['lock_file']))


def _transaction(paths, operation, platform, scope, targets, created_dirs, cleanup=None, *, default=None):
    """Stage, back up, journal and apply targets; roll back on Exception. Returns backup dir."""
    if not targets:
        return None
    backup_root = Path(paths['backup_root'])
    journal_path = Path(paths['journal_file'])
    stamp = _stamp(backup_root)
    staging = backup_root / f'.staging-{stamp}'
    backup_dir = backup_root / stamp if any(t.old is not None for t in targets) else None
    entries, started = [], 0
    try:
        staging.mkdir(parents=True)
        for index, target in enumerate(targets):
            if target.data is not None:
                _write_new(staging / f'{index:04d}', target.data)
            entry = {'path': _posix(target.path), 'existed': target.old is not None,
                     'backup': None, 'backup_sha256': None,
                     'new_sha256': None if target.data is None else _sha(target.data)}
            if target.old is not None:
                backup = backup_dir / target.label
                _write_new(backup, target.old)
                entry['backup'], entry['backup_sha256'] = _posix(backup), _sha(target.old)
            entries.append(entry)
        if _verify_backups(entries, backup_root):
            raise OSError('backup verification failed')
        journal = {'schema_version': 1, 'operation': operation, 'platform': platform, 'scope': scope,
                   'started_at': _now(), 'backup_dir': backup_dir and _posix(backup_dir),
                   'staging_dir': _posix(staging), 'created_dirs': created_dirs, 'targets': entries}
        if 'bridge_component' in paths:
            journal.update(_bridge_binding(paths))
        atomic_write(journal_path, _json_bytes(journal))
        for target, entry in zip(targets, entries):
            # Compare with the bytes backed up before touching this target.
            if _read(target.path) != target.old:
                raise ModifiedTargetError('target changed while the transaction was running; '
                                          'it was left as is', [_posix(target.path)])
            started += 1
            if target.action is not None:
                target.action()
                # Legacy platform actions only expose their written bytes after the write.
                after = _read(target.path)
                entry['new_sha256'] = None if after is None else _sha(after)
                atomic_write(journal_path, _json_bytes(journal))
            elif target.data is not None:
                atomic_write(target.path, target.data)
            elif os.path.lexists(target.path):
                Path(target.path).unlink()
        if cleanup:
            cleanup()
        journal_path.unlink()
    except Exception as exc:
        touched = entries[:started]
        failed = _verify_backups(touched, backup_root)  # never restore from an unverified backup
        unexpected, links = [], []
        if touched and not failed:
            _, _, unexpected, failed = _restore(touched, created_dirs, paths, links)
        if failed:
            raise IncompleteTransactionError(
                f'{operation} failed ({exc}) and rollback is incomplete; journal kept at '
                f'{_posix(journal_path)}; {_RECOVER_HINT}', failed) from exc
        try:
            journal_path.unlink(missing_ok=True)
        finally:
            _discard_staging(staging, backup_root)
        kept = f'; backup kept at {_posix(backup_dir)}' if backup_dir and backup_dir.exists() else ''
        if unexpected:
            kept += '; changed by someone else and left as is: ' + ', '.join(unexpected)
        if links:
            kept += '; linked directories left in place: ' + ', '.join(links)
        raise InstallRolledBack(f'{operation} failed and was rolled back: {exc}{kept}',
                                backup_dir and _posix(backup_dir)) from exc
    _discard_staging(staging, backup_root)
    return backup_dir and _posix(backup_dir)


def _lock_path(path):
    return Path(str(path) + '.lock')


def _infra_paths(paths, *, rules=False, state=False):
    """Paths touched by this component; native state only belongs to a Leader."""
    items = [paths['lock_file'], _lock_path(paths['lock_file']), paths['journal_file'],
             paths['backup_root'], Path(paths['skill_dir']) / MANIFEST_NAME]
    if paths.get('bridge_component') == 'leader':
        items += [paths['state_file'], _lock_path(paths['state_file']),
                  Path(paths['lock_file']).parent / 'bridge-worktrees.json']
    elif paths.get('bridge_component') == 'worker':
        items.append(Path(paths['lock_file']).parent / 'bridge-worktrees.json')
    elif state:
        items.append(paths['state_file'])
    if rules and paths['global_rules_file'] is not None:
        items.append(paths['global_rules_file'])
    return items


def _plan_link_set(plan):
    dests = [Path(plan.paths[label]) / rel for label, rel, _ in plan.files]
    return _infra_paths(plan.paths, rules=plan.rules_block is not None, state=plan.scope == 'user') + dests


def apply_plan(plan, *, dry_run, replace_unmanaged=False, overwrite_modified=False) -> dict:
    if dry_run:
        return dict(preview(plan), dry_run=True)
    paths = plan.paths
    _check_links(_plan_link_set(plan), paths)
    with _component_lock(paths):
        _check_links(_plan_link_set(plan), paths)
        _journal_guard(paths)
        r = _assess(plan)
        conflicts = r['conflicts']
        if conflicts['linked']:
            raise LinkedPathError('linked path rejected: ' + ', '.join(conflicts['linked']))
        if conflicts['unmanaged'] and not replace_unmanaged:
            raise UnmanagedTargetError('targets exist but are not managed by coding-team '
                                       '(use --replace-unmanaged)', conflicts['unmanaged'])
        if conflicts['modified'] and not overwrite_modified:
            raise ModifiedTargetError('managed targets were modified (use --overwrite-modified)',
                                      conflicts['modified'])
        backup_dir = None
        if r['targets']:
            stale = [t for t in r['targets'] if t.data is None and t.action is None]
            label_roots = {label: Path(paths[label]) for label in ROOT_LABELS}

            def cleanup():
                for target in stale:
                    root = label_roots[target.label.split('/', 1)[0]]
                    _prune(target.path.parent, root, include_root=False)

            _desktop_guard(paths)
            backup_dir = _transaction(paths, 'install', plan.platform, plan.scope, r['targets'],
                                      r['created_dirs'], cleanup, default=plan.state_default)
        state = None
        if plan.scope == 'user' and paths.get('bridge_component') != 'worker':
            state = ct_policy.load_state(paths['state_file'])
    return {'dry_run': False, 'written': r['written'], 'unchanged': r['unchanged'],
            'removed_stale': r['removed_stale'], 'kept_modified_stale': r['kept_modified_stale'],
            'modified_overwritten': r['modified_overwritten'],
            'replaced_unmanaged': r['replaced_unmanaged'], 'backup_dir': backup_dir,
            'manifest': _posix(Path(paths['skill_dir']) / MANIFEST_NAME),
            'rules_file': r['rules_file'], 'state': state}


# --- recover -------------------------------------------------------------------------------

def _load_journal(path):
    try:
        journal = json.loads(Path(path).read_bytes().decode('utf-8'))
        _require(isinstance(journal, dict) and journal.get('schema_version') == 1, 'schema_version must be 1')
        _require(isinstance(journal.get('targets'), list) and isinstance(journal.get('created_dirs'), list),
                 'bad targets/created_dirs')
        for entry in journal['targets']:
            _require(isinstance(entry, dict) and isinstance(entry.get('path'), str)
                     and isinstance(entry.get('existed'), bool), 'bad target entry')
            digest = entry.get('new_sha256')
            _require(digest is None or (isinstance(digest, str) and _SHA.fullmatch(digest)), 'bad new_sha256')
        _require(all(isinstance(d, str) for d in journal['created_dirs']), 'bad created_dirs')
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise RecoveryUnsafeError(f'install journal is unreadable ({exc}); inspect it manually',
                                  [_posix(path)]) from None
    return journal


def recover(cfg, *, scope, env, home, project_dir=None, desktop_worker=False) -> dict:
    """Restore every journaled target from verified backups, then drop journal and staging."""
    paths = _resolve_paths(cfg, scope, env, home, project_dir, desktop_worker)
    roots, journal_path = paths['managed_roots'], Path(paths['journal_file'])
    _check_links(_infra_paths(paths), paths)
    with _component_lock(paths):
        _check_links(_infra_paths(paths), paths)
        if 'bridge_component' in paths:
            try:
                current_manifest = _load_manifest(paths['skill_dir'])
            except ValueError:
                # A recovery journal can repair a manifest whose current bytes are unreadable.
                current_manifest = None
            _check_bridge_binding(current_manifest, paths)
        if not journal_path.exists():
            return {'restored': [], 'deleted_new': [], 'kept_unexpected': [], 'kept_links': [],
                    'journal_removed': False, 'backup_dir': None}
        journal = _load_journal(journal_path)
        _check_bridge_binding(journal, paths)
        if (journal.get('platform'), journal.get('scope')) != (cfg['platform'], scope):
            raise RecoveryUnsafeError('journal belongs to a different platform or scope; '
                                      'rerun with the matching --platform/--scope/--project-dir', [journal_path])
        entries = journal['targets']
        outside = [e['path'] for e in entries if not any(_inside(e['path'], r) for r in roots)]
        if outside:
            raise RecoveryUnsafeError('journal targets are outside the managed roots of this '
                                      'platform/scope; rerun with the matching --scope/--project-dir', outside)
        _check_links([e['path'] for e in entries], paths)
        bad = _verify_backups(entries, paths['backup_root'])
        if bad:
            raise RecoveryUnsafeError('backups are missing or do not match the journal; nothing restored', bad)
        links = []
        _desktop_guard(paths)
        restored, deleted, unexpected, failed = _restore(entries, journal['created_dirs'], paths, links)
        if failed:
            raise IncompleteTransactionError(f'recovery incomplete; journal kept; {_RECOVER_HINT} again',
                                             failed)
        # The journal blocks Desktop admission until restoration finishes under this lock.
        journal_path.unlink()
        staging = journal.get('staging_dir')
        if (isinstance(staging, str) and _inside(staging, paths['backup_root'])
                and Path(staging).name.startswith('.staging-')):
            _discard_staging(Path(staging), Path(paths['backup_root']))
    return {'restored': restored, 'deleted_new': deleted, 'kept_unexpected': unexpected,
            'kept_links': links, 'journal_removed': True, 'backup_dir': journal.get('backup_dir')}


# --- uninstall -----------------------------------------------------------------------------

def uninstall(cfg, *, scope, env, home, project_dir=None, dry_run=False, remove_modified=False,
              desktop_worker=False) -> dict:
    paths = _resolve_paths(cfg, scope, env, home, project_dir, desktop_worker)
    skill_dir = Path(paths['skill_dir'])
    if not dry_run:
        _check_links(_infra_paths(paths), paths)
    with nullcontext() if dry_run else _component_lock(paths):
        if not dry_run:
            _check_links(_infra_paths(paths), paths)
            _journal_guard(paths)
        manifest = _load_manifest(skill_dir)
        if manifest is None:
            raise ValueError(f'not a coding-team managed installation: {_posix(skill_dir)}')
        _check_identity(manifest, cfg['platform'], scope, skill_dir)
        _check_bridge_binding(manifest, paths)
        targets, removed, kept, missing = [], [], [], []
        kept_files = {label: {} for label in ROOT_LABELS}
        for label in ROOT_LABELS:
            for rel, digest in manifest['files'].get(label, {}).items():
                dest = Path(paths[label]) / rel
                _check_links([dest], paths)
                old = _read(dest)
                if old is None:
                    missing.append(_posix(dest))
                elif _sha(old) == digest or remove_modified:
                    targets.append(_Target(dest, None, old, f'{label}/{rel}'))
                    removed.append(_posix(dest))
                else:
                    kept.append(_posix(dest))
                    kept_files[label][rel] = digest
        rules_removed = False
        rules = manifest.get('rules_file')
        if rules and paths['global_rules_file'] is not None and _posix(paths['global_rules_file']) == rules:
            rules_path = Path(paths['global_rules_file'])
            _check_links([rules_path], paths)
            old = _read(rules_path)
            new = _rules_without(old) if old is not None else old
            if new != old:
                rules_removed = True
                if new == b'' and manifest.get('rules_file_created'):
                    new = None  # Ruling R2: the file existed only because install created it
                targets.append(_Target(rules_path, new, old, f'rules/{rules_path.name}'))
        manifest_path = skill_dir / MANIFEST_NAME
        reduced = None
        if kept:
            reduced = dict(manifest, files=kept_files, rules_file=None, rules_file_created=False)
            reduced = _json_bytes(reduced)
        old = _read(manifest_path)
        if old != reduced:
            targets.append(_Target(manifest_path, reduced, old, f'skill_dir/{MANIFEST_NAME}'))
        created_dirs = sorted((d for d in manifest['created_dirs'] if _dir_allowed(d, paths)),
                              key=lambda d: len(Path(d).parts), reverse=True)
        result = {'dry_run': dry_run, 'removed': removed, 'kept_modified': kept, 'missing': missing,
                  'rules_removed': rules_removed, 'kept_links': [], 'backup_dir': None}
        if dry_run:
            result['journal_present'] = Path(paths['journal_file']).exists()
            result['kept_links'] = [d for d in created_dirs if not kept and _is_link(d)]
            return result

        def cleanup():
            for path in removed:
                label = 'skill_dir' if _inside(path, skill_dir) else 'agents_dir'
                _prune(Path(path).parent, paths[label], label == 'skill_dir', result['kept_links'])
            if not kept:
                _prune(skill_dir, skill_dir, True, result['kept_links'])
                for directory in created_dirs:
                    _rmdir_if_empty(directory, result['kept_links'])

        if targets:
            _desktop_guard(paths)
            result['backup_dir'] = _transaction(paths, 'uninstall', cfg['platform'], scope, targets, [], cleanup)
    return result
