"""Read-only discovery and trust checks for the two Desktop bridge components.

Discovery never creates policy, database, or lock files. Mutating callers acquire
``locked_bindings`` before the native policy lock and the SQLite transaction.
Peer manifest paths come from the activated worktree record, never peer payloads.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Mapping

from ct_platform import load_platform, target_paths
from ct_storage import locked, reject_links

MANIFEST_NAME = 'install-manifest.json'
BRIDGE_ABI = 2
_SHA = re.compile(r'[0-9a-f]{64}', re.ASCII)
_COMMON = {'SKILL.md', 'scripts/ct_bridge_binding.py', 'scripts/ct_bridge_files.py',
           'scripts/ct_bridge_store.py', 'scripts/ct_platform.py', 'scripts/ct_storage.py',
           'scripts/ct_policy.py', 'scripts/ct_bridge_control.py',
           'scripts/ct_bridge_attempts.py', 'scripts/ct_bridge_install.py',
           'scripts/ct_bridge_roles.py', 'scripts/ct_bridge_scopes.py',
           'scripts/ct_bridge_upgrade.py', 'scripts/ct_roles.py',
           'agents/roles/roles.json', 'agents/roles/ct-architect.md', 'agents/roles/ct-researcher.md',
           'agents/roles/ct-developer.md', 'agents/roles/ct-test-engineer.md',
           'agents/roles/ct-reviewer.md', 'agents/roles/ct-security.md'}
_REQUIRED = {'leader': _COMMON | {'scripts/coding_team.py', 'scripts/ct_bridge_cli.py',
                                 'platforms/codex.json'},
             'worker': _COMMON | {'scripts/ct_bridge_worker.py', 'platforms/antigravity.json'}}
_CATALOG = 'scripts/ct_bridge_commands.py'


class BindingError(ValueError):
    """Stable bridge reason code with no untrusted manifest contents."""

    def __init__(self, reason='INSTALLATION_INCOMPATIBLE'):
        self.reason = reason
        super().__init__(reason)


def _require(condition):
    if not condition:
        raise BindingError()


def _canonical(path):
    path = Path(os.path.abspath(path))
    reject_links(path, Path(path.anchor))
    return path.resolve()


def _record_path(value):
    _require(isinstance(value, str) and Path(value).is_absolute())
    result = _canonical(value)
    _require(value == result.as_posix())
    return result


def _relative(value):
    _require(isinstance(value, str) and bool(value) and '\\' not in value
             and ':' not in value and not PurePosixPath(value).is_absolute()
             and all(part not in ('', '.', '..') for part in value.split('/')))
    return value


def _ordinary_file(path):
    info = path.stat()
    _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1)


def _unique_keys(pairs):
    values = {}
    for key, value in pairs:
        _require(key not in values)
        values[key] = value
    return values


def _read_manifest(skill_dir, component, *, require_catalog=False):
    _require(component in _REQUIRED)
    skill_dir = _canonical(skill_dir)
    manifest_path = skill_dir / MANIFEST_NAME
    reject_links(manifest_path, skill_dir)
    _require(manifest_path.is_file())
    _ordinary_file(manifest_path)
    raw = manifest_path.read_bytes()
    data = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_unique_keys)
    _require(isinstance(data, dict) and type(data.get('schema_version')) is int
             and data['schema_version'] == 1 and type(data.get('bridge_abi')) is int
             and data['bridge_abi'] == BRIDGE_ABI and data.get('component') == component
             and data.get('platform') == ('codex' if component == 'leader' else 'antigravity')
             and data.get('scope') in ('user', 'project'))
    files = data.get('files')
    _require(isinstance(files, dict) and isinstance(files.get('skill_dir'), dict))
    entries = files['skill_dir']
    _require(_REQUIRED[component] <= entries.keys())
    if require_catalog:
        _require(_CATALOG in entries)
    for relative, digest in entries.items():
        _relative(relative)
        _require(isinstance(digest, str) and _SHA.fullmatch(digest) is not None)
        path = skill_dir / relative
        reject_links(path, skill_dir)
        _ordinary_file(path)
        _require(path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest)
    return data, hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class Actor:
    platform: str
    chat_id: str

    def __post_init__(self):
        if (self.platform not in ('codex', 'antigravity')
                or not isinstance(self.chat_id, str) or not self.chat_id.strip()
                or self.chat_id != self.chat_id.strip() or self.chat_id.upper() == 'UNKNOWN'):
            raise BindingError('UNKNOWN_CHAT')


@dataclass(frozen=True)
class LeaderRef:
    actor: Actor
    epoch: int

    def __post_init__(self):
        if not isinstance(self.actor, Actor) or type(self.epoch) is not int or self.epoch < 1:
            raise BindingError('INVALID_LEADER')


@dataclass(frozen=True)
class ComponentBinding:
    skill_dir: Path
    component: str
    scope: str
    install_lock: Path
    policy_path: Path | None
    _manifest_digest: str | None = field(default=None, repr=False, compare=False)
    _workspace_root: Path | None = field(default=None, repr=False, compare=False)

    @property
    def manifest_path(self):
        return self.skill_dir / MANIFEST_NAME

    @property
    def journal_path(self):
        return self.install_lock.parent / 'install-journal.json'

    @property
    def platform(self):
        return 'codex' if self.component == 'leader' else 'antigravity'

    def verify(self, *, require_clean_journal=True):
        """Recheck file identity, including after acquiring the install lock.

        Existing attempts may finish during recovery with
        ``require_clean_journal=False``; incompatible or changed bytes never pass.
        """
        try:
            data, digest = _read_manifest(self.skill_dir, self.component)
            _require(data['scope'] == self.scope)
            _require(_record_path(data.get('install_lock')) == self.install_lock)
            policy = _record_path(data.get('state_path')) if self.component == 'leader' else None
            _require(policy == self.policy_path)
            if self._manifest_digest is not None:
                _require(digest == self._manifest_digest)
            _canonical(self.install_lock)
            _canonical(Path(str(self.install_lock) + '.lock'))
            _canonical(self.journal_path)
            if self.policy_path is not None:
                _canonical(self.policy_path)
                _canonical(Path(str(self.policy_path) + '.lock'))
            if require_clean_journal:
                _require(not self.journal_path.exists())
            return self
        except (OSError, ValueError, TypeError, KeyError):
            raise BindingError() from None


@dataclass(frozen=True)
class BridgeContext:
    paths: object
    actor: Actor | None
    component: str
    source_root: Path
    env: Mapping[str, str]
    home: Path

    def require_actor(self):
        if self.actor is None:
            raise BindingError('UNKNOWN_CHAT')
        return self.actor

    def require_leader(self):
        actor = self.require_actor()
        if actor.platform != 'codex' or self.component != 'leader':
            raise BindingError('UNSUPPORTED_LEADER')
        return actor

    def _binding(self, manifest_path, component, *, own=False):
        try:
            supplied = Path(manifest_path)
            _require(supplied.is_absolute() and supplied.name == MANIFEST_NAME)
            manifest_path = _canonical(supplied)
            skill = manifest_path.parent
            # Establish placement before reading any configuration from the peer.
            # Both scopes use the app's managed skill name; another checkout is
            # never a project-scope peer for this worktree.
            name = 'coding-team' if component == 'leader' else 'coding-team-desktop-worker'
            user_root = (self.home / '.agents/skills' if component == 'leader' else
                         self.home / '.gemini/config/skills')
            candidates = {_canonical(user_root / name),
                          _canonical(self.paths.root / '.agents/skills' / name)}
            _require(skill in candidates)
            data, digest = _read_manifest(skill, component, require_catalog=own)
            platform = 'codex' if component == 'leader' else 'antigravity'
            cfg = load_platform(platform, skill)
            env = dict(self.env)
            policy = None
            if component == 'leader':
                policy = _record_path(data.get('state_path'))
                # The worker cannot infer a custom CODEX_HOME from its own runtime.
                # It may adopt only the exact state/lock pair recorded by the Leader.
                if self.component == 'worker':
                    _require(policy.name == 'state.json' and policy.parent.name == 'coding-team')
                    env['CODEX_HOME'] = str(policy.parent.parent)
            paths = target_paths(cfg, data['scope'], env, self.home, self.paths.root)
            expected_skill = paths['skill_dir']
            expected_lock = paths['lock_file']
            if component == 'worker':
                expected_skill = expected_skill.with_name('coding-team-desktop-worker')
                expected_lock = expected_lock.parent.parent / 'coding-team-desktop-worker' / 'install.lock'
            _require(skill == _canonical(expected_skill))
            install_lock = _record_path(data.get('install_lock'))
            _require(install_lock == _canonical(expected_lock))
            if component == 'leader':
                _require(policy == _canonical(paths['state_file']))
            if own:
                _require(component == self.component and skill == _canonical(self.source_root))
                if self.actor is not None:
                    _require(self.actor.platform == platform)
            binding = ComponentBinding(skill, component, data['scope'], install_lock,
                                       policy, digest, self.paths.root)
            return binding.verify(require_clean_journal=False)
        except (OSError, ValueError, TypeError, KeyError):
            raise BindingError() from None

    def own_binding(self):
        """Verify exactly the component containing the invoked entry point."""
        return self._binding(Path(self.source_root) / MANIFEST_NAME, self.component, own=True)

    def leader_binding(self, manifest_path: str | Path):
        return self._binding(manifest_path, 'leader')

    def worker_binding(self, manifest_path: str | Path):
        return self._binding(manifest_path, 'worker')

    def discover_worker(self):
        """Select the installed worktree worker before falling back to user scope."""
        for parent in (self.paths.root / '.agents/skills', self.home / '.gemini/config/skills'):
            root = _canonical(parent / 'coding-team-desktop-worker')
            manifest = root / MANIFEST_NAME
            if root.exists():
                return self.worker_binding(manifest)
        return None


def ctx_from_runtime(platform, workspace, env, *, component, source_root, home):
    """Resolve runtime identity and a real Git worktree without writing anything."""
    if platform not in ('codex', 'antigravity') or component not in ('leader', 'worker'):
        raise BindingError('UNSUPPORTED_PLATFORM')
    from ct_bridge_files import locate_workspace

    paths = locate_workspace(Path(workspace))
    key = 'CODEX_THREAD_ID' if platform == 'codex' else 'ANTIGRAVITY_CONVERSATION_ID'
    value = env.get(key)
    actor = None
    if isinstance(value, str) and value.strip() and value.strip().upper() != 'UNKNOWN':
        actor = Actor(platform, value.strip())
    return BridgeContext(paths, actor, component, _canonical(source_root), dict(env), _canonical(home))


def verify_compatibility(*bindings, require_clean_journal=True):
    """Verify the supplied components; initial Leader selection needs no worker."""
    roots = {getattr(b, '_workspace_root', None) for b in bindings
             if getattr(b, '_workspace_root', None) is not None}
    _require(len(roots) <= 1)
    for binding in bindings:
        binding.verify(require_clean_journal=require_clean_journal)
    return tuple(bindings)


@contextmanager
def locked_bindings(*bindings, require_clean_journal=True):
    """Hold install locks in Leader then worker order; policy and DB locks follow.

    Callers pass only their own component and the peer recorded in the same
    worktree. Discovery and read-only status should use verification directly.
    """
    bindings = verify_compatibility(*bindings, require_clean_journal=require_clean_journal)
    ordered = sorted(bindings, key=lambda b: (b.component != 'leader', b.install_lock.as_posix()))
    with ExitStack() as stack:
        seen = set()
        for binding in ordered:
            if binding.install_lock not in seen:
                stack.enter_context(locked(binding.install_lock, timeout=30))
                seen.add(binding.install_lock)
        verify_compatibility(*bindings, require_clean_journal=require_clean_journal)
        yield bindings
