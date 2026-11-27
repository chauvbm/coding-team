"""Local, immutable Markdown transport for the Desktop bridge.

SQLite owns publication decisions. This module only establishes worktree identity,
guards the managed filesystem boundary, and publishes complete files without replace.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid

from ct_storage import LinkedPathError, reject_links


MAX_FILE_BYTES = 1024 * 1024
_IGNORE = b'# coding-team Desktop bridge v1; local transport only\n*\n'
_TASK = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z')
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_DEVICE = re.compile(r'(?:con|prn|aux|nul|conin\$|conout\$|com[0-9¹²³]|lpt[0-9¹²³])\Z', re.I)
_SHORT = re.compile(r'~[0-9]+(?:\.|$)')
_ATTEMPT = re.compile(r'attempt-([1-9][0-9]{0,9})\Z')
_TEMP = re.compile(r'\.(?:request\.md|result\.md|gitignore)\.[0-9a-f]{32}\.tmp\Z')
_INBOX = re.compile(r'[0-9a-f]{32}\.json\Z')


class BridgeError(ValueError):
    """A stable reason, without echoing paths, secrets, or operating-system text."""

    def __init__(self, reason: str, detail: str = ''):
        self.reason = reason if isinstance(reason, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', reason) else 'BRIDGE_ERROR'
        # Callers may pass untrusted OS exception text. Do not expose it in CLI logs.
        self.detail = 'operation rejected' if detail else ''
        super().__init__(self.reason + (': ' + self.detail if self.detail else ''))


@dataclass(frozen=True)
class WorkspacePaths:
    root: Path
    data: Path
    db: Path


@dataclass(frozen=True)
class AttemptKey:
    task_id: str
    attempt_no: int


@dataclass(frozen=True)
class FileRef:
    relative_path: str
    size: int
    sha256: str


def validate_relative_path(value: str) -> str:
    """Validate a portable READ/EDIT scope without consulting the filesystem."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise BridgeError('INVALID_PATH')
    for part in value.split('/'):
        if (not part or part in {'.', '..'} or part[-1] in '. '
                or any(c in '\\:<>"|?*' or ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in part)
                or len(part.encode('utf-16-le')) > 510
                or _DEVICE.fullmatch(part.split('.', 1)[0].rstrip(' '))
                or _SHORT.search(part.casefold())):
            raise BridgeError('INVALID_PATH')
        if part.casefold() in {'.git', '.coding-team'}:
            raise BridgeError('RESERVED_PATH')
    if len(value.encode('utf-8')) > 4096:
        raise BridgeError('INVALID_PATH')
    return value


def _absolute(path):
    path = Path(os.path.abspath(path))
    if os.name == 'nt' and (len(path.anchor) != 3 or not path.anchor[0].isalpha()):
        raise BridgeError('UNSUPPORTED_WORKSPACE')
    try:
        reject_links(path, Path(path.anchor))
    except (LinkedPathError, OSError) as exc:
        raise BridgeError('LINKED_PATH') from exc
    return path


def _git(root, *arguments, allowed=(0,)):
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith('GIT_')}
    env.update(GIT_OPTIONAL_LOCKS='0', GIT_NO_LAZY_FETCH='1', GIT_TERMINAL_PROMPT='0')
    # check-ignore accepts literal paths directly and rejects pathspec magic.
    if arguments and arguments[0] == 'ls-files':
        env['GIT_LITERAL_PATHSPECS'] = '1'
    executable = None
    name = 'git.exe' if os.name == 'nt' else 'git'
    boundary = next((parent for parent in (root, *root.parents) if (parent / '.git').exists()), root)
    for entry in os.get_exec_path(env):
        directory = Path(entry.strip('"'))
        if directory.is_absolute():
            candidate = (directory / name).resolve()
            if not candidate.is_relative_to(boundary) and candidate.is_file() and os.access(candidate, os.X_OK):
                executable = str(candidate)
                break
    if executable is None:
        raise BridgeError('GIT_UNAVAILABLE')
    try:
        result = subprocess.run((executable, '--no-replace-objects', '-c', 'core.fsmonitor=false',
                                 '-C', str(root), *arguments), env=env, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BridgeError('GIT_READ_FAILED') from exc
    if result.returncode not in allowed:
        raise BridgeError('GIT_READ_FAILED')
    return result.stdout


def locate_workspace(path: Path) -> WorkspacePaths:
    """Resolve the actual checkout, including linked Git worktrees, without writes."""
    source = _absolute(path)
    if not source.is_dir():
        raise BridgeError('INVALID_WORKSPACE')
    try:
        raw = _git(source, 'rev-parse', '--show-toplevel').rstrip(b'\r\n').decode('utf-8')
        root = _absolute(Path(raw))
        if not root.is_absolute() or root not in (source, *source.parents):
            raise BridgeError('INVALID_WORKSPACE')
        with _Guard() as guard:
            guard.directory(source)
            guard.directory(root)
        if _git(root, 'rev-parse', '--is-inside-work-tree').strip() != b'true':
            raise BridgeError('INVALID_WORKSPACE')
    except (UnicodeError, OSError) as exc:
        raise BridgeError('INVALID_WORKSPACE') from exc
    data = root / '.coding-team' / 'bridge'
    return WorkspacePaths(root, data, data / 'bridge.sqlite3')


def _validate_paths(paths):
    if (not isinstance(paths, WorkspacePaths) or not isinstance(paths.root, Path)
            or not paths.root.is_absolute() or paths != locate_workspace(paths.root)):
        raise BridgeError('INVALID_WORKSPACE_PATHS')


def _plain(info, *, directory=False):
    if (getattr(info, 'st_file_attributes', 0) & 0x400
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)):
        raise BridgeError('UNSAFE_FILE_TYPE')


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class _Guard:
    """Pin directories; use no-follow descriptors on POSIX and no-delete handles on NT."""

    def __init__(self):
        self.stack = ExitStack()
        self.directories = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    def _case(self, path):
        names = [entry for entry in os.listdir(path.parent) if entry.casefold() == path.name.casefold()]
        if names and names != [path.name]:
            raise BridgeError('PATH_COLLISION')
        return bool(names)

    def directory(self, path, *, create=False):
        path = _absolute(path)
        if path in self.directories:
            return self.directories[path]
        if path.parent != path:
            parent = self.directory(path.parent)
            exists = self._case(path)
            if not exists and create:
                try:
                    if os.name == 'nt':
                        # Inherit the worktree ACL. Python 3.13's special 0700
                        # ACL excludes restricted Windows tokens from the result.
                        path.mkdir(mode=0o777)
                    else:
                        os.mkdir(path.name, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    pass
            elif not exists:
                raise FileNotFoundError()
        _plain(path.lstat(), directory=True)
        if os.name == 'nt':
            handle = self.stack.enter_context(_win_open(path, directory=True))
            _win_check(handle, path, directory=True)
            descriptor = handle
        else:
            if path.parent == path:
                descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            else:
                descriptor = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            self.stack.callback(os.close, descriptor)
            _plain(os.fstat(descriptor), directory=True)
        self.directories[path] = descriptor
        return descriptor

    def plain(self, path):
        try:
            descriptor = self.directory(path.parent)
            if not self._case(path):
                return False
            info = path.lstat() if os.name == 'nt' else os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
            _plain(info)
            if os.name == 'nt':
                with _win_open(path, attributes_only=True) as handle:
                    _win_check(handle, path)
            return True
        except FileNotFoundError:
            return False

    def read(self, path, *, limit=MAX_FILE_BYTES):
        descriptor = self.directory(path.parent)
        if not self._case(path):
            raise FileNotFoundError()
        if os.name == 'nt':
            with _win_open(path) as handle:
                content = _win_read(handle, path, limit)
        else:
            before = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
            _plain(before)
            raw = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
            with os.fdopen(raw, 'rb') as stream:
                opened = os.fstat(raw)
                _plain(opened)
                if opened.st_size > limit:
                    raise BridgeError('FILE_TOO_LARGE')
                content = stream.read(limit + 1)
                after = os.fstat(raw)
                current = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
                if not (_identity(before) == _identity(opened) == _identity(after) == _identity(current)):
                    raise BridgeError('FILE_CHANGED')
        if len(content) > limit:
            raise BridgeError('FILE_TOO_LARGE')
        return content

    def publish(self, path, content):
        descriptor = self.directory(path.parent)
        if self._case(path):
            if self.read(path) != content:
                raise BridgeError('FILE_CONFLICT')
            return
        temporary = path.with_name('.' + path.name.lstrip('.') + '.' + uuid.uuid4().hex + '.tmp')
        raw = None
        temporary_identity = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
            raw = os.open(temporary, flags, 0o600) if os.name == 'nt' else os.open(temporary.name, flags, 0o600, dir_fd=descriptor)
            temporary_identity = os.fstat(raw)
            _plain(temporary_identity)
            with os.fdopen(raw, 'wb') as stream:
                raw = None
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                if os.name == 'nt':
                    # Unlike replace(), Win32 rename fails when the destination exists.
                    os.rename(temporary, path)
                else:
                    _posix_publish(descriptor, temporary.name, path.name)
            except FileExistsError:
                if self.read(path) != content:
                    raise BridgeError('FILE_CONFLICT') from None
        finally:
            if raw is not None:
                os.close(raw)
            try:
                current = temporary.lstat()
                if temporary_identity and (current.st_dev, current.st_ino) == (temporary_identity.st_dev, temporary_identity.st_ino):
                    if os.name == 'nt':
                        temporary.unlink()
                    else:
                        os.unlink(temporary.name, dir_fd=descriptor)
            except FileNotFoundError:
                pass


def _posix_publish(directory, source, target):
    """Use native no-replace rename to avoid exposing a two-hardlink intermediate."""
    import ctypes
    library = ctypes.CDLL(None, use_errno=True)
    name, flags = ('renameatx_np', 4) if sys.platform == 'darwin' else ('renameat2', 1)
    operation = getattr(library, name, None)
    if operation is None:
        raise BridgeError('ATOMIC_PUBLISH_UNAVAILABLE')
    operation.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    operation.restype = ctypes.c_int
    if operation(directory, os.fsencode(source), directory, os.fsencode(target), flags):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def check_plain_file(path, stop_at) -> bool:
    """False for absence; reject links, aliases, directories and multiply-linked files."""
    path, stop_at = _absolute(path), _absolute(stop_at)
    if not path.is_relative_to(stop_at):
        raise BridgeError('PATH_OUTSIDE_WORKSPACE')
    try:
        with _Guard() as guard:
            return guard.plain(path)
    except OSError as exc:
        raise BridgeError('FILE_CHECK_FAILED') from exc


@contextmanager
def database_guard(paths: WorkspacePaths):
    """Protect the SQLite location while a helper opens a local DB connection."""
    if (not isinstance(paths, WorkspacePaths) or not isinstance(paths.root, Path)
            or not paths.root.is_absolute() or paths.data != paths.root / '.coding-team' / 'bridge'
            or paths.db != paths.data / 'bridge.sqlite3'):
        raise BridgeError('INVALID_WORKSPACE_PATHS')
    with _Guard() as guard, ExitStack() as handles:
        try:
            guard.directory(paths.data)
        except FileNotFoundError:
            present = False
        else:
            try:
                marker = guard.read(paths.data / '.gitignore', limit=len(_IGNORE))
            except FileNotFoundError:
                raise BridgeError('UNOWNED_TRANSPORT') from None
            if marker != _IGNORE:
                raise BridgeError('UNOWNED_TRANSPORT')
            present = guard.plain(paths.db)
            if present and os.name == 'nt':
                pinned = handles.enter_context(_win_open(paths.db, attributes_only=True, pin=True))
                _win_check(pinned, paths.db)
            # SQLite must be free to remove these rollback files during commit.
            for suffix in ('-journal', '-wal', '-shm'):
                guard.plain(paths.db.with_name(paths.db.name + suffix))
        yield present


def _tracked(paths):
    entries = _git(paths.root, 'ls-files', '-z', '--', '.coding-team').split(b'\0')
    for entry in entries:
        folded = entry.lower()
        if folded in (b'.coding-team', b'.coding-team/bridge') or folded.startswith(b'.coding-team/bridge/'):
            raise BridgeError('TRACKED_TRANSPORT')


def _ignored(paths, path):
    relative = path.relative_to(paths.root).as_posix()
    if not _git(paths.root, 'check-ignore', '--no-index', '--', relative, allowed=(0, 1)):
        raise BridgeError('TRANSPORT_NOT_IGNORED')


def _validate_tree(paths, guard):
    """Owned marker permits only the bridge's documented names and crash temporaries."""
    for entry in paths.data.iterdir():
        if entry.name in {'.gitignore', 'bridge.sqlite3', 'bridge.sqlite3-wal', 'bridge.sqlite3-shm', 'bridge.sqlite3-journal'} or _TEMP.fullmatch(entry.name):
            guard.plain(entry)  # disappearance is safe during concurrent SQLite cleanup
        elif entry.name == 'inbox':
            guard.directory(entry)
            for payload in entry.iterdir():
                if not _INBOX.fullmatch(payload.name):
                    raise BridgeError('UNOWNED_TRANSPORT')
                guard.plain(payload)
        elif _TASK.fullmatch(entry.name) and not _DEVICE.fullmatch(entry.name) and entry.name.casefold() != 'inbox':
            guard.directory(entry)
            for attempt in entry.iterdir():
                match = _ATTEMPT.fullmatch(attempt.name)
                if not match or int(match[1]) > 2147483647:
                    raise BridgeError('UNOWNED_TRANSPORT')
                guard.directory(attempt)
                for artifact in attempt.iterdir():
                    if artifact.name not in {'request.md', 'result.md'} and not _TEMP.fullmatch(artifact.name):
                        raise BridgeError('UNOWNED_TRANSPORT')
                    guard.plain(artifact)
        else:
            raise BridgeError('UNOWNED_TRANSPORT')


def verify_layout(paths: WorkspacePaths, *, create: bool) -> None:
    """Inspect an owned layout; create only managed directories on an explicit write."""
    _validate_paths(paths)
    _tracked(paths)
    try:
        with _Guard() as guard:
            guard.directory(paths.root)
            parent = paths.data.parent
            try:
                guard.directory(parent)
            except FileNotFoundError:
                if not create:
                    return
                guard.directory(parent, create=True)
            try:
                guard.directory(paths.data)
            except FileNotFoundError:
                if not create:
                    return
                guard.directory(paths.data, create=True)
                guard.publish(paths.data / '.gitignore', _IGNORE)
            try:
                if guard.read(paths.data / '.gitignore', limit=len(_IGNORE)) != _IGNORE:
                    raise BridgeError('UNOWNED_TRANSPORT')
            except FileNotFoundError:
                raise BridgeError('UNOWNED_TRANSPORT') from None
            _validate_tree(paths, guard)
            for suffix in ('', '-journal', '-wal', '-shm'):
                guard.plain(paths.db.with_name(paths.db.name + suffix))
            _ignored(paths, paths.db)
            _ignored(paths, paths.data / 'probe' / 'attempt-1' / 'request.md')
            if create:
                guard.directory(paths.data / 'inbox', create=True)
    except OSError as exc:
        raise BridgeError('LAYOUT_CHECK_FAILED') from exc


def _target(paths, key, kind):
    if (not isinstance(key, AttemptKey) or not isinstance(key.task_id, str) or not _TASK.fullmatch(key.task_id)
            or _DEVICE.fullmatch(key.task_id) or key.task_id.casefold() == 'inbox'
            or type(key.attempt_no) is not int or not 1 <= key.attempt_no <= 2147483647):
        raise BridgeError('INVALID_ATTEMPT')
    if kind not in ('request', 'result'):
        raise BridgeError('INVALID_FILE_KIND')
    return paths.data / key.task_id / ('attempt-' + str(key.attempt_no)) / (kind + '.md')


def _validate_content(content):
    if not isinstance(content, bytes):
        raise BridgeError('INVALID_CONTENT')
    if len(content) > MAX_FILE_BYTES:
        raise BridgeError('FILE_TOO_LARGE')
    try:
        content.decode('utf-8')
    except UnicodeError as exc:
        raise BridgeError('INVALID_UTF8') from exc


def publish_file(paths: WorkspacePaths, key: AttemptKey, kind: str, data: bytes) -> FileRef:
    """Publish complete UTF-8 bytes once; retries require exactly identical bytes."""
    target = _target(paths, key, kind)
    _validate_content(data)
    verify_layout(paths, create=True)
    try:
        with _Guard() as guard:
            guard.directory(target.parent.parent, create=True)
            guard.directory(target.parent, create=True)
            _tracked(paths)
            _ignored(paths, target)
            guard.publish(target, data)
            if guard.read(target) != data:
                raise BridgeError('FILE_CHANGED')
    except OSError as exc:
        raise BridgeError('FILE_PUBLISH_FAILED') from exc
    return FileRef(target.relative_to(paths.data).as_posix(), len(data), sha256(data).hexdigest())


def read_file(paths: WorkspacePaths, key: AttemptKey, kind: str, expected_sha256: str) -> bytes:
    """Read only complete, plain UTF-8 files matching the authoritative digest."""
    target = _target(paths, key, kind)
    if not isinstance(expected_sha256, str) or not _SHA.fullmatch(expected_sha256):
        raise BridgeError('INVALID_DIGEST')
    verify_layout(paths, create=False)
    try:
        with _Guard() as guard:
            content = guard.read(target)
    except FileNotFoundError:
        raise BridgeError('FILE_MISSING') from None
    except OSError as exc:
        raise BridgeError('FILE_READ_FAILED') from exc
    _validate_content(content)
    if sha256(content).hexdigest() != expected_sha256:
        raise BridgeError('DIGEST_MISMATCH')
    return content


def consume_payload_file(paths: WorkspacePaths, path: Path) -> bytes:
    """Consume one direct inbox UUID file, preserving rejected transport bytes.

    JSON/schema authentication belongs to the worker. Successful transport validation
    consumes the file before returning, including when its JSON is subsequently invalid.
    """
    target = Path(path)
    if (not target.is_absolute() or target.parent != paths.data / 'inbox'
            or not _INBOX.fullmatch(target.name) or str(target) != os.path.abspath(target)):
        raise BridgeError('INVALID_PAYLOAD_PATH')
    verify_layout(paths, create=False)
    try:
        with _Guard() as guard:
            descriptor = guard.directory(target.parent)
            if not guard._case(target):
                raise BridgeError('PAYLOAD_MISSING')
            if os.name == 'nt':
                # No write/delete sharing: another consumer cannot open the same
                # file for deletion, and path replacement is blocked until close.
                with _win_open(target, delete=True) as handle:
                    content = _win_read(handle, target, MAX_FILE_BYTES)
                    _validate_content(content)
                    disposition = _ct.c_ubyte(1)
                    _win_call(_SetInfo, handle, 4, _ct.byref(disposition), _ct.sizeof(disposition))
            else:
                import fcntl
                raw = os.open(target.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
                with os.fdopen(raw, 'rb') as stream:
                    fcntl.flock(raw, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    before = os.fstat(raw)
                    _plain(before)
                    if before.st_size > MAX_FILE_BYTES:
                        raise BridgeError('FILE_TOO_LARGE')
                    content = stream.read(MAX_FILE_BYTES + 1)
                    current = os.stat(target.name, dir_fd=descriptor, follow_symlinks=False)
                    if not (_identity(before) == _identity(os.fstat(raw)) == _identity(current)):
                        raise BridgeError('FILE_CHANGED')
                    _validate_content(content)
                    os.unlink(target.name, dir_fd=descriptor)
            return content
    except FileNotFoundError:
        raise BridgeError('PAYLOAD_MISSING') from None
    except OSError as exc:
        raise BridgeError('PAYLOAD_UNAVAILABLE') from exc


def cleanup_task(paths: WorkspacePaths, task_id: str, keys) -> None:
    """Delete only explicit, DB-owned request/result files after caller authorization.

    The caller must commit its closed-task tombstone and revoke content grants first.
    Empty directories and crash temporaries remain. Missing files make retries safe.
    Every supplied target is checked before the first deletion; no recursive deletion
    or discovery of deletion targets is performed here.
    """
    _target(paths, AttemptKey(task_id, 1), 'request')
    try:
        supplied = tuple(keys)
    except TypeError as exc:
        raise BridgeError('INVALID_ATTEMPT') from exc
    targets = []
    for key in supplied:
        for kind in ('request', 'result'):
            target = _target(paths, key, kind)
            if key.task_id != task_id:
                raise BridgeError('INVALID_ATTEMPT')
            if target not in targets:
                targets.append(target)
    verify_layout(paths, create=False)
    try:
        with _Guard() as guard, ExitStack() as opened:
            retained = []
            for target in targets:
                try:
                    descriptor = guard.directory(target.parent)
                    if not guard._case(target):
                        continue
                    if os.name == 'nt':
                        handle = opened.enter_context(_win_open(target, delete=True))
                        _win_check(handle, target)
                        retained.append(handle)
                    else:
                        import fcntl
                        raw = os.open(target.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
                        opened.callback(os.close, raw)
                        fcntl.flock(raw, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        info = os.fstat(raw)
                        _plain(info)
                        current = os.stat(target.name, dir_fd=descriptor, follow_symlinks=False)
                        if _identity(info) != _identity(current):
                            raise BridgeError('FILE_CHANGED')
                        retained.append((target, descriptor, raw))
                except FileNotFoundError:
                    continue
            # A second index check occurs with existing file names pinned on NT.
            _tracked(paths)
            for item in retained:
                if os.name == 'nt':
                    disposition = _ct.c_ubyte(1)
                    _win_call(_SetInfo, item, 4, _ct.byref(disposition), _ct.sizeof(disposition))
                else:
                    target, descriptor, raw = item
                    current = os.stat(target.name, dir_fd=descriptor, follow_symlinks=False)
                    if _identity(current) != _identity(os.fstat(raw)):
                        raise BridgeError('FILE_CHANGED')
                    os.unlink(target.name, dir_fd=descriptor)
    except OSError as exc:
        raise BridgeError('CLEANUP_FAILED') from exc


if os.name == 'nt':
    import ctypes as _ct
    from ctypes import wintypes as _wt

    _kernel = _ct.WinDLL('kernel32', use_last_error=True)

    def _api(name, returns, *parameters):
        function = getattr(_kernel, name)
        function.restype, function.argtypes = returns, parameters
        return function

    class _FileInfo(_ct.Structure):
        _fields_ = [('attributes', _wt.DWORD), ('created', _wt.FILETIME), ('accessed', _wt.FILETIME),
                    ('modified', _wt.FILETIME), ('volume', _wt.DWORD), ('size_high', _wt.DWORD),
                    ('size_low', _wt.DWORD), ('links', _wt.DWORD), ('file_high', _wt.DWORD), ('file_low', _wt.DWORD)]

    _CreateFile = _api('CreateFileW', _wt.HANDLE, _wt.LPCWSTR, _wt.DWORD, _wt.DWORD, _ct.c_void_p,
                       _wt.DWORD, _wt.DWORD, _wt.HANDLE)
    _Close = _api('CloseHandle', _wt.BOOL, _wt.HANDLE)
    _Info = _api('GetFileInformationByHandle', _wt.BOOL, _wt.HANDLE, _ct.POINTER(_FileInfo))
    _InfoEx = _api('GetFileInformationByHandleEx', _wt.BOOL, _wt.HANDLE, _ct.c_int, _ct.c_void_p, _wt.DWORD)
    _SetInfo = _api('SetFileInformationByHandle', _wt.BOOL, _wt.HANDLE, _ct.c_int, _ct.c_void_p, _wt.DWORD)
    _FinalPath = _api('GetFinalPathNameByHandleW', _wt.DWORD, _wt.HANDLE, _wt.LPWSTR, _wt.DWORD, _wt.DWORD)
    _FileType = _api('GetFileType', _wt.DWORD, _wt.HANDLE)
    _Process = _api('GetCurrentProcess', _wt.HANDLE)
    _Duplicate = _api('DuplicateHandle', _wt.BOOL, _wt.HANDLE, _wt.HANDLE, _wt.HANDLE, _ct.POINTER(_wt.HANDLE),
                      _wt.DWORD, _wt.BOOL, _wt.DWORD)

    def _win_call(function, *args):
        result = function(*args)
        if not result:
            raise _ct.WinError(_ct.get_last_error())
        return result

    @contextmanager
    def _win_open(path, *, directory=False, attributes_only=False, delete=False, pin=False):
        access = (0x80 if directory or attributes_only else 0x80000000) | (0x10000 if delete else 0)
        sharing = 3 if pin or directory else (7 if attributes_only else 1)
        handle = _CreateFile(str(path), access, sharing, None, 3, 0x200000 | (0x02000000 if directory else 0), None)
        if handle == _ct.c_void_p(-1).value:
            raise _ct.WinError(_ct.get_last_error())
        try:
            yield handle
        finally:
            _win_call(_Close, handle)

    def _win_duplicate(handle):
        duplicate = _wt.HANDLE()
        process = _Process()
        _win_call(_Duplicate, process, handle, process, _ct.byref(duplicate), 0, False, 2)
        return duplicate.value

    def _win_read(handle, path, limit):
        _win_check(handle, path)
        import msvcrt
        # Give the CRT a duplicate, so the caller retains handle ownership.
        duplicate = _win_duplicate(handle)
        try:
            raw = msvcrt.open_osfhandle(duplicate, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            _win_call(_Close, duplicate)
            raise
        with os.fdopen(raw, 'rb') as stream:
            before = os.fstat(raw)
            _plain(before)
            if before.st_size > limit:
                raise BridgeError('FILE_TOO_LARGE')
            content = stream.read(limit + 1)
            if len(content) > limit:
                raise BridgeError('FILE_TOO_LARGE')
            if _identity(before) != _identity(os.fstat(raw)):
                raise BridgeError('FILE_CHANGED')
        _win_check(handle, path)
        return content

    def _win_check(handle, path, *, directory=False):
        info = _FileInfo()
        _win_call(_Info, handle, _ct.byref(info))
        if (_FileType(handle) != 1 or info.attributes & 0x400 or bool(info.attributes & 0x10) != directory
                or (not directory and info.links != 1)):
            raise BridgeError('UNSAFE_FILE_TYPE')
        buffer = _ct.create_unicode_buffer(32768)
        length = _win_call(_FinalPath, handle, buffer, len(buffer), 0)
        if length >= len(buffer) or not buffer.value.startswith('\\\\?\\'):
            raise BridgeError('UNSUPPORTED_WORKSPACE')
        actual, expected = buffer.value[4:], str(path)
        if actual[2:] != expected[2:] or actual[:2].casefold() != expected[:2].casefold():
            raise BridgeError('PATH_COLLISION')
        if directory:
            sensitive = _wt.DWORD()
            _win_call(_InfoEx, handle, 23, _ct.byref(sensitive), _ct.sizeof(sensitive))
            if sensitive.value:
                raise BridgeError('CASE_SENSITIVE_DIRECTORY')
        else:
            streams = (_ct.c_longlong * 512)()
            _win_call(_InfoEx, handle, 7, _ct.byref(streams), _ct.sizeof(streams))
            raw = _ct.string_at(_ct.addressof(streams), _ct.sizeof(streams))
            next_offset, size = int.from_bytes(raw[:4], 'little'), int.from_bytes(raw[4:8], 'little')
            if next_offset or size > len(raw) - 24 or raw[24:24 + size] not in (b'', '::$DATA'.encode('utf-16-le')):
                raise BridgeError('ALTERNATE_DATA_STREAM')
