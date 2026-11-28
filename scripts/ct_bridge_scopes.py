"""Worktree scope reservations for the Desktop bridge."""
from __future__ import annotations

from bisect import bisect_left
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import file_digest
import os
from pathlib import Path
import re
import sqlite3

import ct_bridge_files as files
from ct_bridge_files import AttemptKey, BridgeError, WorkspacePaths, validate_relative_path
from ct_bridge_roles import RoleAssignment


_SHA256 = re.compile(r'[0-9a-f]{64}')
_HEAD = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})')
_HOLDER_WHERE = 'round_id=? AND participant_id=? AND task_id=? AND attempt_no=?'


@dataclass(frozen=True)
class ScopeHolder:
    round_id: str
    participant_id: str
    key: AttemptKey | None = None


@dataclass(frozen=True)
class ScopeSet:
    read_paths: tuple[str, ...] = ()
    write_paths: tuple[str, ...] = ()
    check_outputs: tuple[str, ...] = ()


def _component_key(part: str) -> str:
    # Fold uppercase equivalents too, including the Windows alias I / dotless i.
    return part.rstrip(' .').upper().casefold()


def _path_key(path: str) -> str:
    return '/'.join(_component_key(part) for part in path.split('/'))


def _parents(path: str):
    parent, sep, _ = path.rpartition('/')
    while sep:
        yield parent
        parent, sep, _ = parent.rpartition('/')


def _remember_spelling(spellings, path, key, *, reason):
    for display, folded in zip((path, *_parents(path)), (key, *_parents(key))):
        if spellings.setdefault(folded, display) != display:
            raise BridgeError(reason)


def _overlaps(key, keys, ordered):
    if key in keys or any(parent in keys for parent in _parents(key)):
        return True
    prefix = key + '/'
    position = bisect_left(ordered, prefix)
    return position < len(ordered) and ordered[position].startswith(prefix)


def _scope_entries(scope: ScopeSet):
    reads = set(map(validate_relative_path, scope.read_paths))
    writes = set(map(validate_relative_path, scope.write_paths))
    outputs = set(map(validate_relative_path, scope.check_outputs))
    if outputs & (reads | writes):
        raise BridgeError('UNSAFE_CHECK_OUTPUT')
    spellings = {}
    keys = {path: _path_key(path) for path in reads | writes | outputs}
    for path, key in sorted(keys.items()):
        _remember_spelling(spellings, path, key, reason='PATH_COLLISION')
    leaf_keys = set(keys.values())
    if any(parent in leaf_keys for key in leaf_keys for parent in _parents(key)):
        raise BridgeError('PATH_COLLISION')
    return sorted([('read', keys[path], path) for path in reads]
                  + [('write', keys[path], path) for path in writes | outputs])


def _fingerprints(value, reason):
    if not isinstance(value, Mapping):
        raise BridgeError(reason)
    for path, digest in value.items():
        validate_relative_path(path)
        if not isinstance(digest, str) or (digest != 'ABSENT' and not _SHA256.fullmatch(digest)):
            raise BridgeError(reason)
    return value


def _snapshot(snapshot):
    if not isinstance(snapshot, Mapping) or set(snapshot) != {'head', 'inputs'}:
        raise BridgeError('INVALID_SNAPSHOT')
    head = snapshot['head']
    if not isinstance(head, str) or not _HEAD.fullmatch(head):
        raise BridgeError('INVALID_SNAPSHOT')
    return head, _fingerprints(snapshot['inputs'], 'INVALID_SNAPSHOT')


def _literal_entry(parent: Path, parent_handle, name: str) -> bool:
    key = _component_key(name)
    entries = os.listdir(parent if os.name == 'nt' else parent_handle)
    matches = [entry for entry in entries if _component_key(entry) == key]
    if matches:
        if matches != [name]:
            raise BridgeError('PATH_COLLISION')
        return True
    if os.name == 'nt':
        # The filesystem can recognize additional aliases besides the portable fold.
        try:
            (parent / name).lstat()
        except FileNotFoundError:
            return False
        raise BridgeError('PATH_COLLISION')
    return False


def _location(guard, root, relative):
    """Pin every existing ancestor, including those of a future file."""
    *parents, name = relative.split('/')
    parent = root
    handle = guard.directory(parent)
    for component in parents:
        if not _literal_entry(parent, handle, component):
            return None
        parent /= component
        handle = guard.directory(parent)
    return (parent / name, handle) if _literal_entry(parent, handle, name) else None


def _tracked_keys(root):
    indexed = files._git(root, 'ls-files', '--cached', '-z')
    committed = files._git(root, 'ls-tree', '-r', '-z', '--name-only', 'HEAD')
    # A staged removal does not make a path from HEAD an eligible check output.
    return {_path_key(path.decode('utf-8', 'surrogateescape').replace('\\', '/'))
            for path in (indexed + committed).split(b'\0') if path}


def canonical_scopes(paths: WorkspacePaths, assignment: RoleAssignment) -> ScopeSet:
    """Resolve the declared file paths, including uncommitted snapshot inputs."""
    _, inputs = _snapshot(assignment.snapshot)
    scope = ScopeSet(
        tuple(sorted({*assignment.read_paths, *inputs})),
        tuple(sorted(assignment.write_paths)),
        tuple(sorted(assignment.check_outputs)),
    )
    entries = _scope_entries(scope)
    try:
        with files._Guard() as guard:
            for relative in sorted({entry[2] for entry in entries}):
                located = _location(guard, paths.root, relative)
                if located is not None and not guard.plain(located[0]):
                    raise BridgeError('FILE_CHANGED')
            if scope.check_outputs:
                tracked = _tracked_keys(paths.root)
                ordered = sorted(tracked)
                if any(_overlaps(_path_key(path), tracked, ordered) for path in scope.check_outputs):
                    raise BridgeError('UNSAFE_CHECK_OUTPUT')
    except OSError as exc:
        raise BridgeError('FILE_CHECK_FAILED') from exc
    return scope


def _holder_values(db, holder):
    if not db.in_transaction:
        raise BridgeError('SCOPE_TRANSACTION_REQUIRED')
    if holder.key is None:
        return holder.round_id, holder.participant_id, '', 0
    if not holder.key.task_id or holder.key.attempt_no <= 0:
        raise BridgeError('INVALID_ATTEMPT')
    return holder.round_id, holder.participant_id, holder.key.task_id, holder.key.attempt_no


def reserve(db: sqlite3.Connection, holder: ScopeHolder, scope: ScopeSet) -> None:
    """Reserve files in the caller's write transaction across all worktree rounds."""
    identity = _holder_values(db, holder)
    entries = _scope_entries(scope)
    if not entries:
        return
    rows = db.execute('SELECT round_id,participant_id,task_id,attempt_no,access,path_key,path '
                      'FROM scope_reservations ORDER BY path_key,access,round_id,participant_id,task_id,attempt_no')
    spellings = {}
    other_paths, other_writes = set(), set()
    for row in rows:
        access, key, path = row[4:7]
        _remember_spelling(spellings, path, key, reason='SCOPE_CONFLICT')
        if row[:4] != identity:
            other_paths.add(key)
            if access == 'write':
                other_writes.add(key)
    paths_ordered, writes_ordered = sorted(other_paths), sorted(other_writes)
    for access, key, path in entries:
        _remember_spelling(spellings, path, key, reason='SCOPE_CONFLICT')
        keys, ordered = (other_paths, paths_ordered) if access == 'write' else (other_writes, writes_ordered)
        if _overlaps(key, keys, ordered):
            raise BridgeError('SCOPE_CONFLICT')
    db.executemany('INSERT INTO scope_reservations '
                   '(round_id,participant_id,task_id,attempt_no,access,path_key,path) VALUES (?,?,?,?,?,?,?) '
                   'ON CONFLICT (round_id,participant_id,task_id,attempt_no,access,path_key) DO NOTHING',
                   [(*identity, *entry) for entry in entries])


def release(db: sqlite3.Connection, holder: ScopeHolder, *, access: str | None = None) -> None:
    identity = _holder_values(db, holder)
    if access is None:
        db.execute('DELETE FROM scope_reservations WHERE ' + _HOLDER_WHERE, identity)
    elif access in ('read', 'write'):
        db.execute('DELETE FROM scope_reservations WHERE ' + _HOLDER_WHERE + ' AND access=?', (*identity, access))
    else:
        raise BridgeError('INVALID_SCOPE_ACCESS')


def _stream_digest(stream, before):
    info = os.fstat(stream.fileno())
    files._plain(info)
    if files._identity(info) != files._identity(before):
        raise BridgeError('FILE_CHANGED')
    digest = file_digest(stream, 'sha256').hexdigest()
    if files._identity(os.fstat(stream.fileno())) != files._identity(info):
        raise BridgeError('FILE_CHANGED')
    return digest


def _digest(guard, root, relative):
    located = _location(guard, root, relative)
    if located is None:
        return 'ABSENT'
    target, parent = located
    if os.name == 'nt':
        import msvcrt

        # Share reads only while hashing, and check the exact Win32 name and streams.
        with files._win_open(target) as handle:
            files._win_check(handle, target)
            duplicate = files._win_duplicate(handle)
            try:
                raw = msvcrt.open_osfhandle(duplicate, os.O_RDONLY | os.O_BINARY)
            except BaseException:
                files._win_call(files._Close, duplicate)
                raise
            with os.fdopen(raw, 'rb') as stream:
                digest = _stream_digest(stream, os.fstat(raw))
            files._win_check(handle, target)
            return digest
    before = os.stat(target.name, dir_fd=parent, follow_symlinks=False)
    files._plain(before)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(target.name, flags, dir_fd=parent), 'rb') as stream:
        digest = _stream_digest(stream, before)
    current = os.stat(target.name, dir_fd=parent, follow_symlinks=False)
    if files._identity(current) != files._identity(before):
        raise BridgeError('FILE_CHANGED')
    return digest


def verify_baseline(paths: WorkspacePaths, *, snapshot: Mapping, scope: ScopeSet, baseline: Mapping) -> None:
    """Compare HEAD and the declared worktree files before handing off permission."""
    head, inputs = _snapshot(snapshot)
    baseline = _fingerprints(baseline, 'INVALID_BASELINE')
    reads, writes = set(scope.read_paths), set(scope.write_paths)
    if set(baseline) != writes:
        raise BridgeError('INVALID_BASELINE')
    if not inputs.keys() <= reads or not reads <= inputs.keys() | baseline.keys():
        raise BridgeError('INVALID_SNAPSHOT')
    _scope_entries(scope)
    if files._git(paths.root, 'rev-parse', '--verify', '--quiet', 'HEAD', allowed=(0, 1)).strip() != head.encode('ascii'):
        raise BridgeError('SNAPSHOT_CHANGED')
    try:
        with files._Guard() as guard:
            for path in sorted(inputs.keys() | baseline.keys()):
                actual = _digest(guard, paths.root, path)
                if path in inputs and actual != inputs[path]:
                    raise BridgeError('SNAPSHOT_CHANGED')
                if path in baseline and actual != baseline[path]:
                    raise BridgeError('BASELINE_CHANGED')
    except OSError as exc:
        raise BridgeError('FILE_CHECK_FAILED') from exc
    if files._git(paths.root, 'rev-parse', '--verify', '--quiet', 'HEAD', allowed=(0, 1)).strip() != head.encode('ascii'):
        raise BridgeError('SNAPSHOT_CHANGED')
