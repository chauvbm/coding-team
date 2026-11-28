"""Component-scoped Desktop worktree index and installer admission fence.

Callers hold the component's ``ct_storage.locked(install_lock)`` throughout
registration/admission or mutation. The index contains only canonical checkout
roots, never legacy CLI state or process records.
"""
from __future__ import annotations

import json
from pathlib import Path

from ct_storage import atomic_write, reject_links

INDEX_NAME = 'bridge-worktrees.json'
MAX_INDEX_BYTES = 1024 * 1024


class DesktopRoundOpen(ValueError):
    reason = 'DESKTOP_ROUND_OPEN'


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate Desktop worktree index key')
        result[key] = value
    return result


def _index(install_lock):
    lock = Path(install_lock)
    if not lock.is_absolute():
        raise ValueError('Desktop component install lock must be absolute')
    path = lock.parent / INDEX_NAME
    reject_links(path, lock.parent)
    try:
        info = path.stat()
    except FileNotFoundError:
        return path, []
    if not path.is_file() or info.st_nlink != 1 or info.st_size > MAX_INDEX_BYTES:
        raise ValueError('unsafe Desktop worktree index: ' + str(path))
    try:
        data = json.loads(path.read_bytes(), object_pairs_hook=_unique)
    except (ValueError, UnicodeError) as exc:
        raise ValueError('invalid Desktop worktree index: ' + str(path)) from exc
    if (not isinstance(data, dict) or set(data) != {'schema_version', 'worktrees'}
            or type(data['schema_version']) is not int or data['schema_version'] != 1
            or not isinstance(data['worktrees'], list)):
        raise ValueError('invalid Desktop worktree index: ' + str(path))
    roots = data['worktrees']
    for root in roots:
        if (not isinstance(root, str) or not Path(root).is_absolute()
                or Path(root).resolve().as_posix() != root):
            raise ValueError('Desktop worktree index requires canonical absolute roots')
    if len(set(roots)) != len(roots):
        raise ValueError('duplicate Desktop worktree root')
    return path, roots


def register_worktree(install_lock: Path, workspace: Path) -> None:
    """Record an actual checkout root atomically under the held component lock."""
    from ct_bridge_files import locate_workspace

    path, roots = _index(install_lock)
    if (Path(install_lock).parent / 'install-journal.json').exists():
        raise ValueError('INSTALL_RECOVERY_REQUIRED: component has an install journal')
    root = locate_workspace(workspace).root.resolve().as_posix()
    if root in roots:
        return
    payload = (json.dumps({'schema_version': 1, 'worktrees': sorted([*roots, root])},
                          indent=2, ensure_ascii=False) + '\n').encode('utf-8')
    if len(payload) > MAX_INDEX_BYTES:
        raise ValueError('Desktop worktree index exceeds size limit')
    atomic_write(path, payload)


def guard(install_lock: Path) -> None:
    """Reject component replacement while any registered Desktop round is open."""
    _, roots = _index(install_lock)
    if not roots:
        return
    from ct_bridge_files import locate_workspace
    from ct_bridge_store import inspect_store

    for root in roots:
        paths = locate_workspace(Path(root))
        if paths.root.resolve().as_posix() != root:
            raise ValueError('registered Desktop worktree no longer resolves to its root: ' + root)
        state = inspect_store(paths)
        if state.open_rounds:
            raise DesktopRoundOpen('DESKTOP_ROUND_OPEN: close and reconcile rounds before changing '
                                   + root + ': ' + ', '.join(state.open_rounds))
        if state.maintenance and state.version != 1:
            raise ValueError('RECOVERY_IN_PROGRESS: complete Desktop store recovery before changing ' + root)
