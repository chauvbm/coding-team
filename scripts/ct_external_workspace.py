"""Projection, frozen artifacts and guarded integration for an external attempt."""
from __future__ import annotations

import base64
import binascii
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
import difflib
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from typing import TYPE_CHECKING, Iterable

from ct_storage import LinkedPathError, reject_links

if TYPE_CHECKING:
    from ct_external_store import AttemptKey, Request, StopProof


_DIFF_LIMIT = 10 * 1024 * 1024
_BINARY_CONTROL = re.compile(rb'[\x00-\x08\x0b\x0c\x0e-\x1f]')
_OBJECT_ID = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_TASK = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z')
_WORKSPACE = re.compile(r'workspace-[a-z0-9_]{8}\Z')
_DEVICE = re.compile(r'(?:con|prn|aux|nul|conin\$|conout\$|com[0-9¹²³]|lpt[0-9¹²³])\Z')
_SHORT_NAME = re.compile(r'~[0-9]+(?:\.|$)')
_DENIED = frozenset({
    '.git', '.gitattributes', '.gitmodules', '.codex', '.agents', '.agent', '_agents', '_agent', '.gemini',
    '.antigravity', '.agy', '.mcp', '.claude', '.cursor', '.github', '.vscode', '.ssh',
    '.aws', '.gnupg', '.kube', '.docker', '.config', '.netrc', '_netrc', '.npmrc', '.pypirc',
    '.git-credentials', '.gitconfig', '.envrc', 'agents.md', 'gemini.md', 'claude.md',
    'antigravity.md', 'skills', 'hooks', 'plugins', 'brain', 'transcripts', 'graphify-out',
    'credentials', 'secrets', 'id_rsa', 'id_ed25519', 'id_ecdsa', 'id_dsa',
    'mcp.json', 'mcp_config.json', 'settings.json', 'settings.local.json', 'hooks.json',
    'agents.json', 'plugins.json', 'rules.json', 'skills.json',
    'coding-team.json', 'task-ledger.json', '.task-ledger.json',
})
_PRIVATE_SUFFIX = ('.pem', '.key', '.p12', '.pfx', '.kdbx', '.sqlite', '.sqlite3')
_PRIVATE_STEM = re.compile(r'(?:credentials?|secrets?|tokens?|service[-_]account|client[-_]secret)(?:[._-]|$)')


class WorkspaceError(ValueError):
    def __init__(self, reason: str, detail: str = ''):
        self.reason = reason
        super().__init__(f'{reason}: {detail}' if detail else reason)


def validate_relative_path(path: str) -> str:
    """Validate exact portable spelling before the filesystem is consulted."""
    if not isinstance(path, str) or not path or len(path) > 4096:
        raise WorkspaceError('INVALID_PATH')
    for part in path.split('/'):
        if (not part or part in {'.', '..'} or part[-1] in '. '
                or any(c in '\\:<>"|?*' or ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in part)
                or len(part.encode('utf-16-le')) > 510):
            raise WorkspaceError('INVALID_PATH', path)
        folded = part.casefold()
        if _DEVICE.fullmatch(folded.split('.', 1)[0].rstrip(' ')) or _SHORT_NAME.search(folded):
            raise WorkspaceError('INVALID_PATH', path)
        if (folded in _DENIED or folded.startswith(('.env.', '.env-', '.env_')) or folded == '.env'
                or folded.endswith(_PRIVATE_SUFFIX) or _PRIVATE_STEM.match(folded)):
            raise WorkspaceError('EXCLUDED_PATH', path)
    if len(path.encode('utf-8')) > 4096:
        raise WorkspaceError('INVALID_PATH')
    return path


def validate_relative_paths(paths: Iterable[str]) -> tuple[str, ...]:
    """Reject aliases in the complete scope, including file/directory collisions."""
    files = set()
    prefixes = {}
    for path in paths:
        validate_relative_path(path)
        if path in files:
            raise WorkspaceError('PATH_COLLISION', path)
        files.add(path)
        parts = path.split('/')
        for size in range(1, len(parts) + 1):
            prefix = '/'.join(parts[:size])
            if prefixes.setdefault(prefix.casefold(), prefix) != prefix:
                raise WorkspaceError('PATH_COLLISION', path)
    for path in files:
        if any(parent in files for parent in _parents(path)):
            raise WorkspaceError('PATH_COLLISION', path)
    return tuple(sorted(files))


def _parents(path):
    while '/' in path:
        path = path.rsplit('/', 1)[0]
        yield path


@dataclass(frozen=True)
class _LocalAttemptKey:
    task_id: str
    attempt_no: int


def _key(task, attempt):
    if not isinstance(task, str) or not _TASK.fullmatch(task) or type(attempt) is not int or not 1 <= attempt <= 2147483647:
        raise WorkspaceError('INVALID_ATTEMPT')
    try:
        from ct_external_store import AttemptKey
    except ModuleNotFoundError as exc:
        if exc.name != 'ct_external_store':
            raise
        AttemptKey = _LocalAttemptKey
    return AttemptKey(task, attempt)


def _attempt_key(root):
    task, attempt = root.parent.name, root.name
    if (root.parent.parent.name != 'attempts' or not task.startswith('task-')
            or not re.fullmatch(r'attempt-[1-9][0-9]{0,9}', attempt)):
        raise WorkspaceError('INVALID_ATTEMPT_ROOT')
    return _key(task[5:], int(attempt[8:]))


@dataclass(frozen=True)
class ManifestEntry:
    source: str
    file_type: str
    sha256: str | None
    editable: bool

    @property
    def present(self):
        return self.file_type == 'FILE'


@dataclass(frozen=True)
class ManifestRef:
    key: AttemptKey
    path: Path
    sha256: str
    source: Path
    commit: str
    kind: str
    base: Path
    project: Path
    entries: dict[str, ManifestEntry]

    @property
    def edit(self):
        return {path: entry for path, entry in self.entries.items() if entry.editable}


@dataclass(frozen=True)
class DiffEntry:
    path: str
    preimage: str | None
    postimage: str | None
    operation: str


@dataclass(frozen=True)
class FrozenDiff:
    key: AttemptKey
    path: Path
    sha256: str
    manifest_sha256: str
    entries: tuple[DiffEntry, ...]


@dataclass(frozen=True)
class ApplyReport:
    disposition: str
    reason: str
    conflicts: tuple[str, ...] = ()

    @property
    def status(self):
        return self.disposition


@dataclass(frozen=True)
class ApplyJournal:
    key: AttemptKey
    path: Path
    sha256: str
    source: Path
    commit: str
    index_path: Path
    index_sha256: str | None
    manifest_sha256: str
    artifact_sha256: str
    candidate_sha256: str
    entries: tuple[_JournalEntry, ...]


@dataclass(frozen=True)
class _JournalEntry:
    path: str
    preimage: bytes | None
    postimage: bytes | None


@dataclass(frozen=True)
class _FileState:
    data: bytes
    identity: tuple[int, int]


def _digest(data):
    return sha256(data).hexdigest() if data is not None else None


def _canonical(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _absolute(path):
    path = Path(os.path.abspath(path))
    try:
        reject_links(path, Path(path.anchor))
    except LinkedPathError as exc:
        raise WorkspaceError('LINKED_PATH') from exc
    return path


def _outside(path, source):
    if path == source or path in source.parents or source in path.parents:
        raise WorkspaceError('WORKSPACE_OVERLAP')


def _case_entry(path):
    """Find the exact entry, rejecting other spellings even on a case-sensitive host."""
    with os.scandir(path.parent) as entries:
        matches = [entry.name for entry in entries if entry.name.casefold() == path.name.casefold()]
    if matches and matches != [path.name]:
        raise WorkspaceError('PATH_COLLISION', path.name)
    return bool(matches)


def _plain(info, is_dir):
    if (getattr(info, 'st_file_attributes', 0) & 0x400
            or not (stat.S_ISDIR(info.st_mode) if is_dir else stat.S_ISREG(info.st_mode))
            or (not is_dir and info.st_nlink != 1)):
        raise WorkspaceError('UNSAFE_FILE_TYPE')


def _read_state(path, *, limit=None):
    """Read a regular, single-link file with a pinned path and immutable handle."""
    if os.name == 'nt':
        with _WindowsGuard() as guard:
            return guard.read(path, limit=limit)
    # Walk ancestor descriptors in root-to-leaf order and never follow a link.
    with ExitStack() as stack:
        fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        stack.callback(os.close, fd)
        try:
            for part in path.parts[1:-1]:
                fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                stack.callback(os.close, fd)
            names = [name for name in os.listdir(fd) if name.casefold() == path.name.casefold()]
            if names and names != [path.name]:
                raise WorkspaceError('PATH_COLLISION', path.name)
            first = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            _plain(first, False)
            raw = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except FileNotFoundError:
            return None
        with os.fdopen(raw, 'rb') as stream:
            opened = os.fstat(raw)
            _plain(opened, False)
            if limit is not None and opened.st_size > limit:
                raise WorkspaceError('DIFF_TOO_LARGE')
            data = stream.read() if limit is None else stream.read(limit + 1)
            current = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            fingerprint = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
            if fingerprint(first) != fingerprint(opened) or fingerprint(opened) != fingerprint(current):
                raise WorkspaceError('SOURCE_CHANGED')
            if limit is not None and len(data) > limit:
                raise WorkspaceError('DIFF_TOO_LARGE')
            return _FileState(data, (opened.st_dev, opened.st_ino))


def _data(state):
    return state.data if state else None


def _required_bytes(path, limit=None):
    state = _read_state(path, limit=limit)
    if state is None:
        raise WorkspaceError('ARTIFACT_MISSING')
    return state.data


def git_executable(workspace, env):
    """Resolve Git from absolute PATH entries outside the worktree."""
    name = 'git.exe' if os.name == 'nt' else 'git'
    root = next((parent for parent in (workspace, *workspace.parents) if (parent / '.git').exists()), workspace)
    for entry in os.get_exec_path(env):
        directory = Path(entry.strip('"'))
        if directory.is_absolute():
            candidate = (directory / name).resolve()
            if not candidate.is_relative_to(root) and candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    raise WorkspaceError('GIT_UNAVAILABLE')


def _git(source, *args):
    env = {name: value for name, value in os.environ.items() if not name.upper().startswith('GIT_')}
    env.update(GIT_OPTIONAL_LOCKS='0', GIT_NO_LAZY_FETCH='1', GIT_LITERAL_PATHSPECS='1', GIT_TERMINAL_PROMPT='0')
    executable = git_executable(source, env)
    try:
        return subprocess.run(
            (executable, '--no-replace-objects', '-c', 'core.fsmonitor=false', '-C', str(source), *args),
            env=env, check=True, capture_output=True, timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkspaceError('GIT_READ_FAILED') from exc


def _git_path(source, *args):
    try:
        return _absolute(Path(_git(source, *args).rstrip(b'\r\n').decode('utf-8')))
    except (UnicodeError, OSError) as exc:
        raise WorkspaceError('INVALID_GIT_ROOT') from exc


def _source_root(source):
    source = _absolute(source)
    if not source.is_dir() or _git_path(source, 'rev-parse', '--show-toplevel') != source:
        raise WorkspaceError('INVALID_GIT_ROOT')
    return source


def _git_entries(source, choice, commit, paths):
    if not paths:
        return {}
    if choice == 'GIT':
        records = _git(source, 'ls-tree', '-r', '-t', '-z', '--full-tree', commit)
    else:
        records = _git(source, 'ls-files', '--stage', '-z')
    expected = {path.casefold(): path for path in paths | _expected_directories(paths)}
    blobs = {}
    for record in records.split(b'\0'):
        if not record:
            continue
        metadata, raw_name = record.split(b'\t', 1)
        name = raw_name.decode('utf-8', errors='surrogateescape')
        for parent in _parents(name):
            selected = expected.get(parent.casefold())
            if selected is not None and (selected != parent or selected in paths):
                raise WorkspaceError('PATH_COLLISION', selected)
        selected = expected.get(name.casefold())
        if selected is None:
            continue
        if selected != name:
            raise WorkspaceError('PATH_COLLISION', selected)
        mode, second, third = metadata.decode('ascii').split()
        if name not in paths and choice == 'GIT' and mode == '040000' and second == 'tree':
            continue
        if (name not in paths or mode not in {'100644', '100755'} or name in blobs
                or (second != 'blob' if choice == 'GIT' else third != '0')):
            raise WorkspaceError('INVALID_GIT_ENTRY', name)
        object_id = third if choice == 'GIT' else second
        if not _OBJECT_ID.fullmatch(object_id):
            raise WorkspaceError('INVALID_GIT_ENTRY', name)
        blobs[name] = object_id
    return blobs


def _write_new(path, content):
    with path.open('xb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _publish(stage, target):
    if os.name == 'nt':
        _win_call(_MoveFileExW, str(stage), str(target), 8)  # MOVEFILE_WRITE_THROUGH, no replace
    else:
        os.link(stage, target, follow_symlinks=False)
        stage.unlink()
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _publish_document(path, payload):
    with tempfile.TemporaryDirectory(prefix='.publish-', dir=path.parent) as temporary:
        stage = Path(temporary) / path.name
        _write_new(stage, payload)
        _publish(stage, path)


def _json_document(path, expected_sha256=None):
    raw = _required_bytes(path)
    digest = _digest(raw)
    if expected_sha256 is not None and digest != expected_sha256:
        raise WorkspaceError('DIGEST_MISMATCH')
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get('version') != 1 or _canonical(payload) != raw:
            raise ValueError
    except (ValueError, UnicodeError) as exc:
        raise WorkspaceError('INVALID_ARTIFACT') from exc
    return payload, digest


def prepare(source: Path, request: Request, attempt_root: Path) -> ManifestRef:
    """Capture the granted scope and publish its manifest after verifying source stability."""
    try:
        return _prepare(source, request, attempt_root)
    except (OSError, UnicodeError) as exc:
        raise WorkspaceError('PROJECTION_FAILED') from exc


def _prepare(source, request, attempt_root):
    source, attempt_root = _source_root(source), _absolute(attempt_root)
    _outside(attempt_root, source)
    key = _attempt_key(attempt_root)
    commit = request.base
    if not isinstance(commit, str) or not _OBJECT_ID.fullmatch(commit):
        raise WorkspaceError('INVALID_BASE')
    if _git(source, 'rev-parse', '--verify', commit + '^{commit}').strip().decode('ascii') != commit:
        raise WorkspaceError('INVALID_BASE')
    kind, choices, edits = request.kind, dict(request.file_choices), frozenset(validate_relative_paths(request.edit_paths))
    if kind not in {'READ', 'EDIT', 'CHECK'} or (kind != 'EDIT' and edits):
        raise WorkspaceError('INVALID_REQUEST')
    if kind == 'CHECK' and choices:
        raise WorkspaceError('INVALID_REQUEST')
    if any(source_name not in {'GIT', 'INDEX', 'WORKTREE'} for source_name in choices.values()):
        raise WorkspaceError('INVALID_REQUEST')
    paths = validate_relative_paths(choices.keys() | edits)
    index = _git_path(source, 'rev-parse', '--path-format=absolute', '--git-path', 'index')
    index_paths = {name for name in paths if name not in edits and choices.get(name) == 'INDEX'}
    git_paths = {name for name in paths if name not in edits and choices.get(name) == 'GIT'}
    needs_index = bool(index_paths)
    index_before = _read_state(index) if needs_index else None
    head_before = _git(source, 'rev-parse', '--verify', 'HEAD')
    blobs_by_source = {'GIT': _git_entries(source, 'GIT', commit, git_paths),
                       'INDEX': _git_entries(source, 'INDEX', commit, index_paths)}
    states, entries = {}, {}
    attempt_root.mkdir(parents=True, exist_ok=True)
    manifest_path = attempt_root / 'manifest.json'
    if manifest_path.exists():
        raise WorkspaceError('PROJECTION_EXISTS')
    local = Path(tempfile.mkdtemp(prefix='workspace-', dir=attempt_root))
    try:
        base, project = local / 'base', local / 'project'
        base.mkdir()
        project.mkdir()
        for name in paths:
            choice = 'WORKTREE' if name in edits else choices[name]
            states[name] = _read_state(source / name)
            if choice == 'WORKTREE':
                content = _data(states[name])
            else:
                object_id = blobs_by_source[choice].get(name)
                content = _git(source, 'cat-file', 'blob', object_id) if object_id else None
            entries[name] = ManifestEntry(choice, 'ABSENT' if content is None else 'FILE',
                                          _digest(content), name in edits)
            for directory in (base, project):
                destination = directory / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                if content is not None:
                    _write_new(destination, content)
        if (any(_read_state(source / name) != states[name] for name in paths)
                or (needs_index and _read_state(index) != index_before)
                or _git(source, 'rev-parse', '--verify', 'HEAD') != head_before):
            raise WorkspaceError('SOURCE_CHANGED')
        payload = _canonical({'version': 1, 'key': asdict(key), 'source': str(source), 'commit': commit,
                              'kind': kind, 'directory': local.name,
                              'entries': {name: asdict(entry) for name, entry in entries.items()}})
        _publish_document(manifest_path, payload)
        return ManifestRef(key, manifest_path, _digest(payload), source, commit, kind, base, project, entries)
    except BaseException:
        shutil.rmtree(local)
        raise


def cleanup_unstarted(attempt_root: Path) -> None:
    """Remove prepared bytes for a verified never-started attempt under the caller's lock."""
    attempt_root = _absolute(attempt_root)
    _attempt_key(attempt_root)
    try:
        if not attempt_root.parent.exists() or not _case_entry(attempt_root):
            return
        _plain(attempt_root.lstat(), True)
        workspaces = []
        for path in attempt_root.iterdir():
            if not _WORKSPACE.fullmatch(path.name.rstrip('. ').casefold()):
                continue
            if not _WORKSPACE.fullmatch(path.name):
                raise WorkspaceError('PATH_COLLISION', path.name)
            path = _absolute(path)
            if path.parent != attempt_root:
                raise WorkspaceError('INVALID_ATTEMPT_ROOT')
            _plain(path.lstat(), True)
            for child in path.iterdir():
                if child.name not in {'base', 'project'}:
                    raise WorkspaceError('UNSAFE_WORKSPACE_LAYOUT')
                _inventory_tree(child)
            workspaces.append(path)
        for path in workspaces:
            _absolute(path)
            shutil.rmtree(path)
    except OSError as exc:
        raise WorkspaceError('CLEANUP_FAILED') from exc


def load_manifest(path: Path, expected_sha256: str) -> ManifestRef:
    path = _absolute(path)
    payload, digest = _json_document(path, expected_sha256)
    try:
        key = _key(**_key_arguments(payload['key']))
        if path.name != 'manifest.json' or _attempt_key(path.parent) != key:
            raise ValueError
        kind, commit, directory = payload['kind'], payload['commit'], payload['directory']
        if kind not in {'READ', 'EDIT', 'CHECK'} or not _OBJECT_ID.fullmatch(commit) or not _WORKSPACE.fullmatch(directory):
            raise ValueError
        source = _absolute(payload['source'])
        _outside(path, source)
        local = path.parent / directory
        entries = {}
        for name in validate_relative_paths(payload['entries']):
            entry = ManifestEntry(**payload['entries'][name])
            if (entry.source not in {'GIT', 'INDEX', 'WORKTREE'} or type(entry.editable) is not bool
                    or entry.file_type not in {'FILE', 'ABSENT'} or not _valid_image(entry.sha256)
                    or entry.present != (entry.sha256 is not None)
                    or (entry.editable and (entry.source != 'WORKTREE' or kind != 'EDIT'))):
                raise ValueError
            entries[name] = entry
        if kind == 'CHECK' and entries:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkspaceError('INVALID_MANIFEST') from exc
    return ManifestRef(key, path, digest, source, commit, kind, local / 'base', local / 'project', entries)


def _key_arguments(row):
    if set(row) != {'task_id', 'attempt_no'}:
        raise ValueError
    return {'task': row['task_id'], 'attempt': row['attempt_no']}


def _valid_image(value):
    return value is None or isinstance(value, str) and _SHA.fullmatch(value) is not None


def _tree(root, *, manifest=None, before=None):
    root = _absolute(root)
    if os.name == 'nt':
        with _WindowsGuard() as guard:
            return _scan_tree(root, guard, manifest, before)
    return _scan_tree(root, None, manifest, before)


def _inventory_tree(root, guard=None, *, paths=None, expected_dirs=None):
    files, directories = {}, set()
    pending = [root]
    while pending:
        current = pending.pop()
        if guard is not None:
            guard.tree_directory(current)
        _plain(current.lstat(), True)
        names = set()
        with os.scandir(current) as stream:
            for child in stream:
                path = Path(child.path)
                name = validate_relative_path(path.relative_to(root).as_posix())
                folded = child.name.casefold()
                if folded in names:
                    raise WorkspaceError('PATH_COLLISION', name)
                names.add(folded)
                # Windows DirEntry.stat omits the link count. Use the actual file metadata.
                info = path.lstat()
                if stat.S_ISDIR(info.st_mode):
                    _plain(info, True)
                    if expected_dirs is not None and name not in expected_dirs:
                        raise WorkspaceError('DIFF_OUTSIDE_SCOPE')
                    directories.add(name)
                    pending.append(path)
                else:
                    _plain(info, False)
                    if paths is not None and name not in paths:
                        raise WorkspaceError('DIFF_OUTSIDE_SCOPE')
                    files[name] = info.st_size
    if expected_dirs is not None and directories != expected_dirs:
        raise WorkspaceError('DIFF_OUTSIDE_SCOPE')
    return files, directories


def _scan_tree(root, guard, manifest, before):
    sizes, directories = _inventory_tree(root, guard, paths=manifest.entries if manifest else None,
                                         expected_dirs=_expected_directories(manifest.entries) if manifest else None)
    remaining = _DIFF_LIMIT
    if manifest is not None:
        editable = manifest.edit
        for path in manifest.entries.keys() - editable.keys():
            if (path in before) != (path in sizes) or (path in before and sizes[path] != len(before[path])):
                raise WorkspaceError('DIFF_OUTSIDE_SCOPE')
        if sum(max(0, sizes.get(path, 0) - len(before.get(path, b''))) for path in editable) > remaining:
            raise WorkspaceError('DIFF_TOO_LARGE')
    files = {}
    read = guard.read if guard is not None else _read_state
    for name in sorted(sizes):
        pre_size = len(before.get(name, b'')) if manifest is not None else 0
        limit = None if manifest is None else pre_size + (remaining if name in editable else 0)
        state = read(root / name, limit=limit)
        if state is None:
            raise WorkspaceError('SOURCE_CHANGED')
        files[name] = state.data
        if manifest is not None and name in editable:
            remaining -= max(0, len(state.data) - pre_size)
    return files, directories


def _expected_directories(names):
    return {parent for name in names for parent in _parents(name)}


def _baseline(manifest):
    files, directories = _tree(manifest.base)
    if (directories != _expected_directories(manifest.entries)
            or {path: _digest(content) for path, content in files.items()}
            != {path: entry.sha256 for path, entry in manifest.entries.items() if entry.present}):
        raise WorkspaceError('BASELINE_CHANGED')
    return files


def _artifact_location(path, manifest):
    path = _absolute(path)
    for root in (manifest.source, manifest.base.parent):
        _outside(path, root)
    return path


def _operation(before, after):
    if before == after:
        raise WorkspaceError('INVALID_DIFF_ENTRY')
    return 'CREATE' if before is None else 'DELETE' if after is None else 'MODIFY'


def _text_lines(value):
    if value is None:
        return []
    try:
        value.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise WorkspaceError('BINARY_DIFF') from exc
    if _BINARY_CONTROL.search(value):
        raise WorkspaceError('BINARY_DIFF')
    lines = value.split(b'\n')
    return [line + b'\n' for line in lines[:-1]] + ([lines[-1]] if lines[-1] else [])


def _render_patch(changes):
    patch = bytearray()
    for name, before, after in changes:
        encoded = name.encode('utf-8')
        patch.extend(b'diff --git a/' + encoded + b' b/' + encoded + b'\n')
        if before is None:
            patch.extend(b'new file mode 100644\n')
        elif after is None:
            patch.extend(b'deleted file mode 100644\n')
        if len(patch) > _DIFF_LIMIT:
            raise WorkspaceError('DIFF_TOO_LARGE')
        for line in difflib.diff_bytes(difflib.unified_diff, _text_lines(before), _text_lines(after),
                                       b'a/' + encoded if before is not None else b'/dev/null',
                                       b'b/' + encoded if after is not None else b'/dev/null'):
            patch.extend(line)
            if not line.endswith(b'\n'):
                patch.extend(b'\n\\ No newline at end of file\n')
            if len(patch) > _DIFF_LIMIT:
                raise WorkspaceError('DIFF_TOO_LARGE')
    return bytes(patch)


def _safe_stop(proof, key):
    claim = getattr(proof, 'runner_claim', '')
    basis = getattr(proof, 'basis', None)
    identity = getattr(proof, 'identity', None)
    if getattr(proof, 'key', None) != key or not isinstance(claim, str) or not claim.strip():
        return False
    if basis == 'NEVER_STARTED':
        return identity is None
    if basis not in ('JOB_EMPTY', 'BOOT_CHANGED') or identity is None:
        return False
    try:
        return (isinstance(identity.job_name, str) and bool(identity.job_name)
                and type(identity.pid) is int and 0 < identity.pid <= 0xFFFFFFFF
                and type(identity.creation_time) is int and 0 < identity.creation_time < 1 << 64
                and type(identity.session_id) is int and 0 <= identity.session_id <= 0xFFFFFFFF
                and isinstance(identity.boot_id, str) and bool(identity.boot_id))
    except AttributeError:
        return False


def freeze(manifest: ManifestRef, kind: str, result_root: Path, stop_proof: StopProof) -> FrozenDiff:
    """Publish a review patch and exact postimages from a stopped attempt."""
    if not _safe_stop(stop_proof, manifest.key):
        raise WorkspaceError('STOP_PROOF_REQUIRED')
    manifest = load_manifest(manifest.path, manifest.sha256)
    if not _safe_stop(stop_proof, manifest.key) or kind != manifest.kind:
        raise WorkspaceError('ATTEMPT_MISMATCH')
    result_root = _artifact_location(result_root, manifest)
    try:
        return _freeze(manifest, result_root)
    except OSError as exc:
        raise WorkspaceError('FREEZE_FAILED') from exc


def _freeze(manifest, result_root):
    before = _baseline(manifest)
    after, _ = _tree(manifest.project, manifest=manifest, before=before)
    changes = [(path, before.get(path), after.get(path)) for path in sorted(before.keys() | after.keys())
               if before.get(path) != after.get(path)]
    editable = manifest.edit
    if any(path not in editable for path, _, _ in changes):
        raise WorkspaceError('DIFF_OUTSIDE_SCOPE')
    if manifest.kind == 'EDIT' and not changes:
        raise WorkspaceError('DIFF_MISSING')
    patch = _render_patch(changes)
    entries = tuple(DiffEntry(path, _digest(pre), _digest(post), _operation(pre, post))
                    for path, pre, post in changes)
    result_root.parent.mkdir(parents=True, exist_ok=True)
    if result_root.exists():
        raise WorkspaceError('ARTIFACT_EXISTS')
    with tempfile.TemporaryDirectory(prefix='.freeze-', dir=result_root.parent) as staging:
        local = Path(staging)
        (local / 'blobs').mkdir()
        for entry, (_, _, content) in zip(entries, changes):
            if content is not None:
                blob = local / 'blobs' / entry.postimage
                if not blob.exists():
                    _write_new(blob, content)
        _write_new(local / 'changes.diff', patch)
        # Publish the trusted marker only after the result's content has landed.
        result_root.mkdir()
        (result_root / 'blobs').mkdir()
        for blob in (local / 'blobs').iterdir():
            _publish(blob, result_root / 'blobs' / blob.name)
        _publish(local / 'changes.diff', result_root / 'changes.diff')
        payload = _canonical({'version': 1, 'key': asdict(manifest.key), 'manifest_sha256': manifest.sha256,
                              'patch_sha256': _digest(patch), 'entries': [asdict(entry) for entry in entries]})
        artifact = result_root / 'artifact.json'
        _publish_document(artifact, payload)
    return FrozenDiff(manifest.key, artifact, _digest(payload), manifest.sha256, entries)


def _blob(result_root, digest):
    content = _required_bytes(result_root / 'blobs' / digest)
    if _digest(content) != digest:
        raise WorkspaceError('ARTIFACT_BLOB_CHANGED')
    return content


def load_frozen_diff(path: Path, expected_sha256: str) -> FrozenDiff:
    path = _absolute(path)
    payload, digest = _json_document(path, expected_sha256)
    try:
        key = _key(**_key_arguments(payload['key']))
        entries = tuple(DiffEntry(**entry) for entry in payload['entries'])
        if (path.name != 'artifact.json' or not _SHA.fullmatch(payload['manifest_sha256'])
                or validate_relative_paths(entry.path for entry in entries) != tuple(entry.path for entry in entries)
                or _digest(_required_bytes(path.parent / 'changes.diff', _DIFF_LIMIT)) != payload['patch_sha256']):
            raise ValueError
        for entry in entries:
            if (not _valid_image(entry.preimage) or not _valid_image(entry.postimage)
                    or entry.operation != _operation(entry.preimage, entry.postimage)):
                raise ValueError
        for postimage in {entry.postimage for entry in entries} - {None}:
            _blob(path.parent, postimage)
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkspaceError('INVALID_FROZEN_DIFF') from exc
    return FrozenDiff(key, path, digest, payload['manifest_sha256'], entries)


def _candidate_inputs(manifest, frozen):
    manifest = load_manifest(manifest.path, manifest.sha256)
    _artifact_location(frozen.path, manifest)
    frozen = load_frozen_diff(frozen.path, frozen.sha256)
    if (manifest.key != frozen.key or manifest.sha256 != frozen.manifest_sha256
            or manifest.kind != 'EDIT' or not frozen.entries):
        raise WorkspaceError('ARTIFACT_BINDING_MISMATCH')
    files = _baseline(manifest)
    editable = manifest.edit
    changes = []
    for entry in frozen.entries:
        pre = files.get(entry.path)
        if entry.path not in editable or entry.preimage != _digest(pre):
            raise WorkspaceError('INVALID_DIFF_PREIMAGE')
        post = _blob(frozen.path.parent, entry.postimage) if entry.postimage else None
        if post is None:
            del files[entry.path]
        else:
            files[entry.path] = post
        changes.append(_JournalEntry(entry.path, pre, post))
    patch = _render_patch((entry.path, entry.preimage, entry.postimage) for entry in changes)
    if patch != _required_bytes(frozen.path.parent / 'changes.diff', _DIFF_LIMIT):
        raise WorkspaceError('PATCH_MISMATCH')
    return manifest, frozen, files, tuple(changes)


def _tree_digest(files, directories):
    return _digest(_canonical({'version': 1, 'directories': sorted(directories),
                               'files': {name: _digest(data) for name, data in files.items()}}))


def candidate_digest(candidate_root: Path) -> str:
    """Hash the actual directory inventory and bytes of the candidate as tested."""
    try:
        return _tree_digest(*_tree(candidate_root))
    except OSError as exc:
        raise WorkspaceError('CANDIDATE_UNAVAILABLE') from exc


def expected_candidate_digest(manifest: ManifestRef, frozen_diff: FrozenDiff) -> str:
    _, _, files, _ = _candidate_inputs(manifest, frozen_diff)
    return _tree_digest(files, _expected_directories(files))


def build_candidate(manifest: ManifestRef, frozen_diff: FrozenDiff, candidate_root: Path) -> Path:
    manifest, frozen, files, _ = _candidate_inputs(manifest, frozen_diff)
    candidate_root = _artifact_location(candidate_root, manifest)
    _outside(candidate_root, frozen.path.parent)
    try:
        candidate_root.mkdir(parents=True)
        try:
            for name, content in files.items():
                target = candidate_root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                _write_new(target, content)
            if candidate_digest(candidate_root) != _tree_digest(files, _expected_directories(files)):
                raise WorkspaceError('CANDIDATE_CHANGED')
        except BaseException:
            shutil.rmtree(candidate_root)
            raise
    except OSError as exc:
        raise WorkspaceError('CANDIDATE_BUILD_FAILED') from exc
    return candidate_root


def _encode_image(content):
    if content is None:
        return None
    return {'sha256': _digest(content), 'bytes': base64.b64encode(content).decode('ascii')}


def _decode_image(row):
    if row is None:
        return None
    if set(row) != {'bytes', 'sha256'}:
        raise ValueError
    content = base64.b64decode(row['bytes'], validate=True)
    if _encode_image(content) != row:
        raise ValueError
    return content


def _journal_bytes(manifest, frozen, candidate, index_path, index_hash, changes):
    return _canonical({
        'version': 1, 'key': asdict(manifest.key), 'source': str(manifest.source), 'commit': manifest.commit,
        'manifest_sha256': manifest.sha256, 'artifact_sha256': frozen.sha256, 'candidate_sha256': candidate,
        'index_path': str(index_path), 'index_sha256': index_hash,
        'entries': [{'path': row.path, 'preimage': _encode_image(row.preimage), 'postimage': _encode_image(row.postimage)}
                    for row in changes],
    })


def read_apply_journal(source: Path, apply_journal: Path) -> ApplyJournal:
    """Read and validate journal bindings so a caller can match its durable attempt state."""
    source, path = _source_root(source), _absolute(apply_journal)
    _outside(path, source)
    payload, digest = _json_document(path)
    try:
        key = _key(**_key_arguments(payload['key']))
        if _attempt_key(path.parent) != key or source != _absolute(payload['source']):
            raise ValueError
        commit = payload['commit']
        index_path = _absolute(payload['index_path'])
        if (not _OBJECT_ID.fullmatch(commit) or not _valid_image(payload['index_sha256'])
                or not all(_SHA.fullmatch(payload[field]) for field in ('manifest_sha256', 'artifact_sha256', 'candidate_sha256'))):
            raise ValueError
        entries = tuple(_JournalEntry(row['path'], _decode_image(row['preimage']), _decode_image(row['postimage']))
                        for row in payload['entries'])
        if not entries or validate_relative_paths(row.path for row in entries) != tuple(row.path for row in entries):
            raise ValueError
        if any(row.preimage == row.postimage for row in entries):
            raise ValueError
        _render_patch((entry.path, entry.preimage, entry.postimage) for entry in entries)
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        raise WorkspaceError('INVALID_APPLY_JOURNAL') from exc
    return ApplyJournal(key, path, digest, source, commit, index_path, payload['index_sha256'],
                        payload['manifest_sha256'], payload['artifact_sha256'], payload['candidate_sha256'], entries)


def verify_apply_journal(source: Path, manifest: ManifestRef, frozen_diff: FrozenDiff,
                         tested_digest: str, apply_journal: Path) -> ApplyJournal:
    """Bind every journal image to the exact persisted and tested attempt artifacts."""
    manifest, frozen, files, changes = _candidate_inputs(manifest, frozen_diff)
    path = _artifact_location(apply_journal, manifest)
    _outside(path, frozen.path.parent)
    journal = read_apply_journal(source, path)
    if (path.parent != manifest.path.parent or journal.source != manifest.source or journal.commit != manifest.commit
            or journal.key != manifest.key or journal.manifest_sha256 != manifest.sha256
            or journal.artifact_sha256 != frozen.sha256 or journal.candidate_sha256 != tested_digest
            or tested_digest != _tree_digest(files, _expected_directories(files)) or journal.entries != changes):
        raise WorkspaceError('APPLY_JOURNAL_CONFLICT')
    return journal


def _head_matches(source, commit):
    return _git(source, 'rev-parse', '--verify', 'HEAD').strip().decode('ascii') == commit


def _states(guard, root, changes):
    states, conflicts = {}, []
    for entry in changes:
        try:
            states[entry.path] = guard.read(root / entry.path)
        except (OSError, WorkspaceError):
            conflicts.append(entry.path)
    return states, tuple(conflicts)


def _classify(guard, source, changes, index_matches, reason):
    # Hold every readable existing file until the full set has been classified.
    with ExitStack() as verification:
        observed, conflicts = {}, []
        for entry in changes:
            try:
                handle = verification.enter_context(guard.target(source / entry.path))
                observed[entry.path] = _data(guard.read_open(handle)) if handle is not None else None
            except (WorkspaceError, OSError):
                conflicts.append(entry.path)
                continue
            if observed[entry.path] not in (entry.preimage, entry.postimage):
                conflicts.append(entry.path)
        if index_matches and not conflicts:
            if all(observed[entry.path] == entry.postimage for entry in changes):
                return ApplyReport('APPLIED', 'POSTIMAGES_VERIFIED')
            if all(observed[entry.path] == entry.preimage for entry in changes):
                return ApplyReport('NOT_APPLIED', reason)
        return ApplyReport('RECONCILE_REQUIRED', reason if index_matches else 'INDEX_CHANGED', tuple(conflicts))


def _replace_image(guard, path, before, after):
    """Compare with the pinned preflight file ID and mutate through its writable handle."""
    target = guard.source / path
    if before is None:
        if after is None:
            raise WorkspaceError('INVALID_DIFF_ENTRY')
        with guard.target(target, write=True, create=True) as handle:
            _write_handle(handle, after)
            if guard.read_open(handle).data != after:
                raise WorkspaceError('POSTIMAGE_MISMATCH', path)
        return
    with guard.target(target, write=True, delete=after is None) as handle:
        if handle is None or guard.read_open(handle) != before:
            raise WorkspaceError('PREIMAGE_CONFLICT', path)
        if after is None:
            _delete_handle(handle)
        else:
            _write_handle(handle, after)
            if guard.read_open(handle).data != after:
                raise WorkspaceError('POSTIMAGE_MISMATCH', path)


@contextmanager
def _guarded_source(source):
    if os.name != 'nt':
        raise WorkspaceError('SOURCE_GUARD_UNAVAILABLE')
    # Git's writer lock serializes cooperating writers, including other attempts and Git itself.
    with _WindowsGuard(source) as guard:
        index_path = _git_path(source, 'rev-parse', '--path-format=absolute', '--git-path', 'index')
        with guard.index_lock(index_path), guard.target(index_path) as index_handle:
            yield guard, index_path, index_handle


def _index_hash(guard, path, handle):
    state = guard.read(path) if handle is None else guard.read_open(handle)
    return _digest(_data(state))


def apply_candidate(source: Path, manifest: ManifestRef, frozen_diff: FrozenDiff,
                    tested_digest: str, apply_journal: Path) -> ApplyReport:
    """Apply precisely the tested projection with a durable journal and Windows handles."""
    journal_may_exist = False
    try:
        source = _source_root(source)
        manifest, frozen, files, changes = _candidate_inputs(manifest, frozen_diff)
        path = _artifact_location(apply_journal, manifest)
        _outside(path, frozen.path.parent)
        if source != manifest.source or path.parent != manifest.path.parent:
            raise WorkspaceError('APPLY_BINDING_MISMATCH')
        journal_may_exist = path.exists()
        if tested_digest != _tree_digest(files, _expected_directories(files)):
            raise WorkspaceError('CANDIDATE_DIGEST_MISMATCH')
        with _guarded_source(source) as (guard, index_path, index):
            # Read again under the lock in case another cooperating caller published first.
            journal_may_exist = path.exists()
            index_hash = _index_hash(guard, index_path, index)
            if index is None and index_hash is not None:
                raise WorkspaceError('INDEX_CHANGED')
            journal = _journal_bytes(manifest, frozen, tested_digest, index_path, index_hash, changes)
            if journal_may_exist and _required_bytes(path) != journal:
                raise WorkspaceError('APPLY_JOURNAL_CONFLICT')
            if not _head_matches(source, manifest.commit):
                raise WorkspaceError('BASE_CHANGED')
            preflight, unsafe = _states(guard, source, changes)
            if unsafe or any(_data(preflight[entry.path]) != entry.preimage for entry in changes):
                if journal_may_exist:
                    return _classify(guard, source, changes, True, 'PREIMAGE_CONFLICT')
                conflicts = tuple(entry.path for entry in changes if entry.path in unsafe
                                  or _data(preflight[entry.path]) != entry.preimage)
                return ApplyReport('NOT_APPLIED', 'PREIMAGE_CONFLICT', conflicts)
            for entry in changes:
                guard.preflight(source / entry.path, preflight[entry.path], delete=entry.postimage is None)
            if _index_hash(guard, index_path, index) != index_hash:
                raise WorkspaceError('INDEX_CHANGED')
            if not journal_may_exist:
                try:
                    _publish_document(path, journal)
                except OSError:
                    journal_may_exist = path.exists() or path.is_symlink()
                    raise
                journal_may_exist = True
            try:
                for entry in changes:
                    _replace_image(guard, entry.path, preflight[entry.path], entry.postimage)
            except (WorkspaceError, OSError) as exc:
                reason = exc.reason if isinstance(exc, WorkspaceError) else 'GUARD_OR_WRITE_FAILED'
            else:
                reason = 'POSTIMAGE_CONFLICT'
            if not _head_matches(source, manifest.commit):
                return ApplyReport('RECONCILE_REQUIRED', 'BASE_CHANGED')
            return _classify(guard, source, changes, _index_hash(guard, index_path, index) == index_hash, reason)
    except (WorkspaceError, OSError) as exc:
        return ApplyReport('RECONCILE_REQUIRED' if journal_may_exist else 'NOT_APPLIED',
                           exc.reason if isinstance(exc, WorkspaceError) else 'GUARD_OR_WRITE_FAILED')


def reconcile_apply(source: Path, apply_journal: Path, *, expected_sha256: str | None = None) -> ApplyReport:
    """Recover a journal only while current bytes match its known postimages."""
    try:
        journal = read_apply_journal(source, apply_journal)
        if expected_sha256 is not None and journal.sha256 != expected_sha256:
            raise WorkspaceError('APPLY_JOURNAL_CONFLICT')
        source = journal.source
        changes = journal.entries
        with _guarded_source(source) as (guard, index_path, index):
            if _digest(_required_bytes(journal.path)) != journal.sha256:
                raise WorkspaceError('APPLY_JOURNAL_CONFLICT')
            if not _head_matches(source, journal.commit):
                raise WorkspaceError('BASE_CHANGED')
            if (index_path != journal.index_path or (index is None and journal.index_sha256 is not None)
                    or _index_hash(guard, index_path, index) != journal.index_sha256):
                raise WorkspaceError('INDEX_CHANGED')
            report = _classify(guard, source, changes, True, 'PREIMAGES_VERIFIED')
            if report.disposition != 'RECONCILE_REQUIRED':
                return report
            observed, conflicts = _states(guard, source, changes)
            for entry in changes:
                if entry.path in conflicts or _data(observed[entry.path]) != entry.postimage:
                    continue
                try:
                    _replace_image(guard, entry.path, observed[entry.path], entry.preimage)
                except (WorkspaceError, OSError):
                    continue
            if not _head_matches(source, journal.commit):
                return ApplyReport('RECONCILE_REQUIRED', 'BASE_CHANGED')
            return _classify(guard, source, changes, _index_hash(guard, index_path, index) == journal.index_sha256,
                             'ROLLBACK_VERIFIED')
    except (WorkspaceError, OSError) as exc:
        return ApplyReport('RECONCILE_REQUIRED', exc.reason if isinstance(exc, WorkspaceError) else 'GUARD_OR_WRITE_FAILED')


if os.name == 'nt':
    import ctypes as _ct
    from ctypes import wintypes as _wt

    _kernel = _ct.WinDLL('kernel32', use_last_error=True)

    def _win_function(name, returns, *parameters):
        function = getattr(_kernel, name)
        function.restype, function.argtypes = returns, parameters
        return function

    class _FileInfo(_ct.Structure):
        _fields_ = [('attributes', _wt.DWORD), ('created', _wt.FILETIME), ('accessed', _wt.FILETIME),
                    ('modified', _wt.FILETIME), ('volume', _wt.DWORD), ('size_high', _wt.DWORD),
                    ('size_low', _wt.DWORD), ('links', _wt.DWORD), ('file_high', _wt.DWORD), ('file_low', _wt.DWORD)]

    class _FileStandardInfo(_ct.Structure):
        _fields_ = [('allocation_size', _ct.c_longlong), ('end_of_file', _ct.c_longlong), ('links', _wt.DWORD),
                    ('delete_pending', _ct.c_ubyte), ('directory', _ct.c_ubyte)]

    _CreateFileW = _win_function('CreateFileW', _wt.HANDLE, _wt.LPCWSTR, _wt.DWORD, _wt.DWORD, _ct.c_void_p,
                                 _wt.DWORD, _wt.DWORD, _wt.HANDLE)
    _CloseHandle = _win_function('CloseHandle', _wt.BOOL, _wt.HANDLE)
    _GetFileType = _win_function('GetFileType', _wt.DWORD, _wt.HANDLE)
    _GetFileInformationByHandle = _win_function('GetFileInformationByHandle', _wt.BOOL, _wt.HANDLE,
                                                _ct.POINTER(_FileInfo))
    _GetFileInformationByHandleEx = _win_function('GetFileInformationByHandleEx', _wt.BOOL, _wt.HANDLE, _ct.c_int,
                                                  _ct.c_void_p, _wt.DWORD)
    _SetFileInformationByHandle = _win_function('SetFileInformationByHandle', _wt.BOOL, _wt.HANDLE, _ct.c_int,
                                                _ct.c_void_p, _wt.DWORD)
    _GetFinalPathNameByHandleW = _win_function('GetFinalPathNameByHandleW', _wt.DWORD, _wt.HANDLE, _wt.LPWSTR,
                                               _wt.DWORD, _wt.DWORD)
    _GetDriveTypeW = _win_function('GetDriveTypeW', _wt.UINT, _wt.LPCWSTR)
    _GetVolumeInformationW = _win_function('GetVolumeInformationW', _wt.BOOL, _wt.LPCWSTR, _wt.LPWSTR, _wt.DWORD,
                                           _ct.c_void_p, _ct.c_void_p, _ct.c_void_p, _wt.LPWSTR, _wt.DWORD)
    _SetFilePointerEx = _win_function('SetFilePointerEx', _wt.BOOL, _wt.HANDLE, _ct.c_longlong, _ct.c_void_p, _wt.DWORD)
    _ReadFile = _win_function('ReadFile', _wt.BOOL, _wt.HANDLE, _ct.c_void_p, _wt.DWORD,
                              _ct.POINTER(_wt.DWORD), _ct.c_void_p)
    _WriteFile = _win_function('WriteFile', _wt.BOOL, _wt.HANDLE, _ct.c_void_p, _wt.DWORD,
                               _ct.POINTER(_wt.DWORD), _ct.c_void_p)
    _SetEndOfFile = _win_function('SetEndOfFile', _wt.BOOL, _wt.HANDLE)
    _FlushFileBuffers = _win_function('FlushFileBuffers', _wt.BOOL, _wt.HANDLE)
    _MoveFileExW = _win_function('MoveFileExW', _wt.BOOL, _wt.LPCWSTR, _wt.LPCWSTR, _wt.DWORD)


_READ, _WRITE, _DELETE = 0x80000000, 0x40000000, 0x10000
_SHARE_READ, _SHARE_WRITE = 1, 2
_OPEN, _CREATE = 3, 1
_OPEN_REPARSE_POINT, _BACKUP_SEMANTICS, _WRITE_THROUGH, _DELETE_ON_CLOSE = 0x200000, 0x02000000, 0x80000000, 0x04000000
_CHUNK = 1 << 16


def _win_call(function, *args):
    result = function(*args)
    if not result:
        raise _ct.WinError(_ct.get_last_error())
    return result


@contextmanager
def _win_open(path, access, sharing, *, create=False, flags=0):
    handle = _CreateFileW(str(path), access, sharing, None, _CREATE if create else _OPEN,
                          _OPEN_REPARSE_POINT | flags, None)
    if handle == _ct.c_void_p(-1).value:
        raise _ct.WinError(_ct.get_last_error())
    try:
        yield handle
    finally:
        _win_call(_CloseHandle, handle)


def _check_handle(handle, path=None, *, directory=False):
    if _GetFileType(handle) != 1:  # FILE_TYPE_DISK
        raise WorkspaceError('UNSAFE_FILE_TYPE')
    info = _FileInfo()
    _win_call(_GetFileInformationByHandle, handle, _ct.byref(info))
    if (info.attributes & 0x400 or bool(info.attributes & 0x10) != directory
            or (not directory and info.links != 1)):
        raise WorkspaceError('UNSAFE_FILE_TYPE')
    if not directory:
        _check_single_stream(handle)
    if path is not None:
        buffer = _ct.create_unicode_buffer(32768)
        length = _win_call(_GetFinalPathNameByHandleW, handle, buffer, len(buffer), 0)
        if length >= len(buffer) or not buffer.value.startswith('\\\\?\\'):
            raise WorkspaceError('SOURCE_GUARD_UNAVAILABLE')
        actual = buffer.value[4:]
        expected = str(path)
        # Drive-letter case is a Win32 namespace detail. Relative names must match exactly.
        if (not directory and actual != expected and actual[2:] != expected[2:]) or Path(actual) != path:
            raise WorkspaceError('PATH_COLLISION', path.name)
    return info


def _check_single_stream(handle):
    # FILE_STREAM_INFO starts with two DWORDs and two LARGE_INTEGERs. A
    # single default stream fits easily in the aligned buffer.
    buffer = (_ct.c_longlong * 512)()
    if not _GetFileInformationByHandleEx(handle, 7, _ct.byref(buffer), _ct.sizeof(buffer)):
        error = _ct.get_last_error()
        if error == 38:  # ERROR_HANDLE_EOF, e.g. no streams on a directory
            return
        if error in {111, 122, 234}:
            raise WorkspaceError('ALTERNATE_DATA_STREAM')
        raise _ct.WinError(error)
    size = _ct.sizeof(buffer)
    raw = _ct.string_at(_ct.addressof(buffer), size)
    next_offset = int.from_bytes(raw[:4], 'little')
    name_size = int.from_bytes(raw[4:8], 'little')
    if next_offset or name_size > size - 24 or raw[24:24 + name_size] not in (b'', '::$DATA'.encode('utf-16-le')):
        raise WorkspaceError('ALTERNATE_DATA_STREAM')


class _WindowsGuard:
    def __init__(self, source=None):
        self.source = source
        self._stack = ExitStack()
        self._pinned = {}
        self._volumes = set()

    def __enter__(self):
        self._stack.__enter__()
        return self

    def __exit__(self, *exception):
        return self._stack.__exit__(*exception)

    def _check_volume(self, anchor):
        if anchor in self._volumes:
            return
        if len(anchor) != 3 or _GetDriveTypeW(anchor) != 3:  # DRIVE_FIXED
            raise WorkspaceError('SOURCE_GUARD_UNAVAILABLE')
        name = _ct.create_unicode_buffer(32)
        _win_call(_GetVolumeInformationW, anchor, None, 0, None, None, None, name, len(name))
        if name.value != 'NTFS':
            raise WorkspaceError('SOURCE_GUARD_UNAVAILABLE')
        self._volumes.add(anchor)

    def _pin(self, path):
        self._check_volume(path.anchor)
        for directory in (*reversed(path.parents), path):
            if directory in self._pinned:
                continue
            if directory.parent != directory and not _case_entry(directory):
                raise FileNotFoundError(directory)
            with ExitStack() as opening:
                handle = opening.enter_context(_win_open(directory, _READ, _SHARE_READ | _SHARE_WRITE,
                                                        flags=_BACKUP_SEMANTICS))
                _check_handle(handle, directory, directory=True)
                case_sensitive = _wt.DWORD()
                _win_call(_GetFileInformationByHandleEx, handle, 23, _ct.byref(case_sensitive), _ct.sizeof(case_sensitive))
                if case_sensitive.value:
                    raise WorkspaceError('CASE_SENSITIVE_DIRECTORY')
                self._stack.enter_context(opening.pop_all())
                self._pinned[directory] = handle
        return self._pinned[path]

    def tree_directory(self, path):
        _check_single_stream(self._pin(path))

    @contextmanager
    def target(self, path, *, write=False, create=False, delete=False):
        try:
            self._pin(path.parent)
        except FileNotFoundError:
            if write:
                raise WorkspaceError('SOURCE_PARENT_MISSING', path.parent.name) from None
            yield None
            return
        exists = _case_entry(path)
        if exists and create:
            raise WorkspaceError('PREIMAGE_CONFLICT', path.name)
        if not (exists or create):
            yield None
            return
        access = _READ | (_WRITE if write else 0) | (_DELETE if delete else 0)
        with _win_open(path, access, _SHARE_READ, create=create, flags=_WRITE_THROUGH if write else 0) as handle:
            info = _check_handle(handle, path)
            if write and info.attributes & 1:
                raise WorkspaceError('SOURCE_READ_ONLY', path.name)
            yield handle

    @contextmanager
    def index_lock(self, path):
        self._pin(path.parent)
        lock = path.with_name(path.name + '.lock')
        with _win_open(lock, _WRITE | _DELETE, 0, create=True, flags=_DELETE_ON_CLOSE) as handle:
            _check_handle(handle, lock)
            yield

    def preflight(self, path, previous, *, delete):
        # Ensure all non-creating writes can be guarded before any mutation. The final
        # opened handle rechecks ID and preimage after this check is released.
        try:
            self._pin(path.parent)
        except FileNotFoundError:
            raise WorkspaceError('SOURCE_PARENT_MISSING', path.parent.name) from None
        if previous is None:
            if _case_entry(path):
                raise WorkspaceError('PREIMAGE_CONFLICT', path.name)
            return
        with self.target(path, write=True, delete=delete) as handle:
            if handle is None or self.read_open(handle) != previous:
                raise WorkspaceError('PREIMAGE_CONFLICT', path.name)

    def read(self, path, *, limit=None):
        with self.target(path) as handle:
            return self.read_open(handle, limit=limit) if handle is not None else None

    def read_open(self, handle, *, limit=None):
        before = _check_handle(handle)
        size = (before.size_high << 32) | before.size_low
        if limit is not None and size > limit:
            raise WorkspaceError('DIFF_TOO_LARGE')
        _win_call(_SetFilePointerEx, handle, 0, None, 0)
        data = bytearray()
        while len(data) < size:
            chunk = _ct.create_string_buffer(min(_CHUNK, size - len(data)))
            count = _wt.DWORD()
            _win_call(_ReadFile, handle, chunk, len(chunk), _ct.byref(count), None)
            if not count.value:
                raise WorkspaceError('SOURCE_CHANGED')
            data.extend(chunk.raw[:count.value])
        after = _check_handle(handle)
        if (before.size_high, before.size_low, before.modified.dwHighDateTime, before.modified.dwLowDateTime) != (
                after.size_high, after.size_low, after.modified.dwHighDateTime, after.modified.dwLowDateTime):
            raise WorkspaceError('SOURCE_CHANGED')
        return _FileState(bytes(data), (after.volume, (after.file_high << 32) | after.file_low))


def _write_handle(handle, data):
    _win_call(_SetFilePointerEx, handle, 0, None, 0)
    for offset in range(0, len(data), _CHUNK):
        chunk = data[offset:offset + _CHUNK]
        count = _wt.DWORD()
        _win_call(_WriteFile, handle, chunk, len(chunk), _ct.byref(count), None)
        if count.value != len(chunk):
            raise WorkspaceError('SHORT_WRITE')
    _win_call(_SetEndOfFile, handle)
    _win_call(_FlushFileBuffers, handle)


def _delete_handle(handle):
    flag = _wt.BOOL(True)
    _win_call(_SetFileInformationByHandle, handle, 4, _ct.byref(flag), _ct.sizeof(flag))  # FileDispositionInfo
    status = _FileStandardInfo()
    _win_call(_GetFileInformationByHandleEx, handle, 1, _ct.byref(status), _ct.sizeof(status))  # FileStandardInfo
    if not status.delete_pending:
        raise WorkspaceError('DELETE_VERIFICATION_FAILED')
