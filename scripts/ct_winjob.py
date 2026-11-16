"""Windows process-tree containment for the external worker runner."""

from __future__ import annotations

from contextlib import ExitStack
import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import TYPE_CHECKING, Protocol
import uuid

if TYPE_CHECKING:
    from ct_external_store import AttemptKey, StopProof


class LaunchSpec(Protocol):
    executable: Path
    args: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    stdin_bytes: bytes


@dataclass(frozen=True)
class OwnerProcess:
    pid: int
    creation_time: int
    session_id: int
    boot_id: str


@dataclass(frozen=True)
class ProcessIdentity:
    job_name: str
    pid: int
    creation_time: int
    session_id: int
    boot_id: str


@dataclass(frozen=True)
class JobState:
    root_exit_code: int | None
    active_processes: int
    overflow: bool


@dataclass(frozen=True)
class CapturedOutput:
    exit_code: int
    stdout: bytes
    stderr: bytes
    overflow: bool


class JobError(RuntimeError):
    def __init__(self, code: str, winerror: int | None = None):
        self.code = code
        self.winerror = winerror
        super().__init__(code if winerror is None else f'{code} (Win32 {winerror})')


_DWORD_PTR = C.c_size_t
_KILL_ON_JOB_CLOSE = 0x2000
_BREAKAWAY_FLAGS = 0x0800 | 0x1000
_JOB_OBJECT_QUERY = 0x0004
_JOB_OBJECT_TERMINATE = 0x0008
_PROCESS_INSPECT = 0x00100000 | 0x1000  # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
_JOB_BASIC_ACCOUNTING = 1
_JOB_EXTENDED_LIMITS = 9
_WAIT_TIMEOUT = 258
_PRIVATE_NAME = re.compile(r'Local\\coding-team-[0-9a-f]{32}\Z')
_CHUNK = 65536
_OUTPUT_TIMEOUT = 3.0
_STDOUT_LIMIT = 1_048_576
_STDERR_LIMIT = 262_144
_PIPE_EOF = {38, 109, 232, 233}  # HANDLE_EOF, BROKEN_PIPE, NO_DATA, PIPE_NOT_CONNECTED


class _SecurityAttributes(C.Structure):
    _fields_ = [('length', W.DWORD), ('descriptor', W.LPVOID), ('inherit', W.BOOL)]


class _StartupInfo(C.Structure):
    _fields_ = [
        ('size', W.DWORD), ('reserved', W.LPWSTR), ('desktop', W.LPWSTR),
        ('title', W.LPWSTR), ('x', W.DWORD), ('y', W.DWORD), ('xsize', W.DWORD),
        ('ysize', W.DWORD), ('xchars', W.DWORD), ('ychars', W.DWORD),
        ('fill', W.DWORD), ('flags', W.DWORD), ('show_window', W.WORD),
        ('reserved_size', W.WORD), ('reserved_bytes', W.LPVOID),
        ('stdin', W.HANDLE), ('stdout', W.HANDLE), ('stderr', W.HANDLE),
    ]


class _StartupInfoEx(C.Structure):
    _fields_ = [('startup', _StartupInfo), ('attributes', W.LPVOID)]


class _ProcessInfo(C.Structure):
    _fields_ = [('process', W.HANDLE), ('thread', W.HANDLE),
                ('pid', W.DWORD), ('tid', W.DWORD)]


class _BasicLimits(C.Structure):
    # Windows uses 8-byte struct alignment for LARGE_INTEGER, including on x86.
    _fields_ = [
        ('process_time', C.c_int64), ('job_time', C.c_int64), ('flags', W.DWORD),
        ('min_working_set', C.c_size_t), ('max_working_set', C.c_size_t),
        ('active_process_limit', W.DWORD), ('affinity', _DWORD_PTR),
        ('priority', W.DWORD), ('scheduling', W.DWORD),
    ]


class _ExtendedLimits(C.Structure):
    _fields_ = [
        ('basic', _BasicLimits), ('io_counters', C.c_uint64 * 6),
        ('process_memory', C.c_size_t), ('job_memory', C.c_size_t),
        ('peak_process_memory', C.c_size_t), ('peak_job_memory', C.c_size_t),
    ]


class _Accounting(C.Structure):
    _fields_ = [
        ('times', C.c_int64 * 4), ('page_faults', W.DWORD), ('total', W.DWORD),
        ('active', W.DWORD), ('terminated', W.DWORD),
    ]


_api = None
if os.name == 'nt':
    import _winapi

    _api = C.WinDLL('kernel32', use_last_error=True)
    _ptr = C.POINTER

    def _bind(name, result, *args):
        fn = getattr(_api, name)
        fn.restype = result
        fn.argtypes = args

    _bind('CreateJobObjectW', W.HANDLE, _ptr(_SecurityAttributes), W.LPCWSTR)
    _bind('OpenJobObjectW', W.HANDLE, W.DWORD, W.BOOL, W.LPCWSTR)
    _bind('SetInformationJobObject', W.BOOL, W.HANDLE, C.c_int, W.LPVOID, W.DWORD)
    _bind('QueryInformationJobObject', W.BOOL, W.HANDLE, C.c_int, W.LPVOID, W.DWORD,
          _ptr(W.DWORD))
    _bind('TerminateJobObject', W.BOOL, W.HANDLE, W.UINT)
    _bind('IsProcessInJob', W.BOOL, W.HANDLE, W.HANDLE, _ptr(W.BOOL))
    _bind('CreateProcessW', W.BOOL, W.LPCWSTR, W.LPWSTR, _ptr(_SecurityAttributes),
          _ptr(_SecurityAttributes), W.BOOL, W.DWORD, W.LPVOID, W.LPCWSTR,
          _ptr(_StartupInfoEx), _ptr(_ProcessInfo))
    _bind('InitializeProcThreadAttributeList', W.BOOL, W.LPVOID, W.DWORD, W.DWORD,
          _ptr(C.c_size_t))
    _bind('UpdateProcThreadAttribute', W.BOOL, W.LPVOID, W.DWORD, _DWORD_PTR, W.LPVOID,
          C.c_size_t, W.LPVOID, _ptr(C.c_size_t))
    _bind('DeleteProcThreadAttributeList', None, W.LPVOID)
    _bind('CloseHandle', W.BOOL, W.HANDLE)
    _bind('SetHandleInformation', W.BOOL, W.HANDLE, W.DWORD, W.DWORD)
    _bind('GetCurrentProcess', W.HANDLE)
    _bind('OpenProcess', W.HANDLE, W.DWORD, W.BOOL, W.DWORD)
    _bind('ProcessIdToSessionId', W.BOOL, W.DWORD, _ptr(W.DWORD))
    _bind('GetProcessTimes', W.BOOL, W.HANDLE, *([_ptr(W.FILETIME)] * 4))
    _bind('WaitForSingleObject', W.DWORD, W.HANDLE, W.DWORD)
    _bind('GetExitCodeProcess', W.BOOL, W.HANDLE, _ptr(W.DWORD))
    _bind('ResumeThread', W.DWORD, W.HANDLE)
    _bind('TerminateProcess', W.BOOL, W.HANDLE, W.UINT)


def _windows():
    if _api is None:
        raise JobError('WINDOWS_UNAVAILABLE')


def _check(result, code):
    if not result:
        raise JobError(code, C.get_last_error())
    return result


def new_job_name() -> str:
    return f'Local\\coding-team-{uuid.uuid4().hex}'


def boot_id() -> str:
    """Read the kernel boot-environment GUID. Unsupported layouts fail closed."""
    _windows()
    try:
        query = C.WinDLL('ntdll').NtQuerySystemInformation
    except (AttributeError, OSError):
        raise JobError('BOOT_ID_UNAVAILABLE') from None
    query.restype = C.c_long
    query.argtypes = [C.c_int, W.LPVOID, W.ULONG, C.POINTER(W.ULONG)]
    # SystemBootEnvironmentInformation: GUID, FIRMWARE_TYPE, padding, ULONGLONG.
    # Its numeric class and binary layout are internal to Windows.
    data = (C.c_uint64 * 4)()
    length = W.ULONG()
    result = query(90, data, C.sizeof(data), C.byref(length))
    if result != 0 or length.value != C.sizeof(data):
        raise JobError('BOOT_ID_UNAVAILABLE')
    identity = uuid.UUID(bytes_le=C.string_at(data, 16))
    if not identity.int:
        raise JobError('BOOT_ID_UNAVAILABLE')
    return str(identity)


def _process_identity(handle, pid, boot):
    created, exited, kernel, user = (W.FILETIME() for _ in range(4))
    _check(_api.GetProcessTimes(handle, C.byref(created), C.byref(exited),
                               C.byref(kernel), C.byref(user)), 'PROCESS_IDENTITY_UNAVAILABLE')
    session = W.DWORD()
    _check(_api.ProcessIdToSessionId(pid, C.byref(session)), 'PROCESS_IDENTITY_UNAVAILABLE')
    creation_time = (created.dwHighDateTime << 32) | created.dwLowDateTime
    return OwnerProcess(pid, creation_time, session.value, boot)


def current_owner_process() -> OwnerProcess:
    boot = boot_id()
    return _process_identity(_api.GetCurrentProcess(), os.getpid(), boot)


def _same_process(handle, expected):
    actual = _process_identity(handle, expected.pid, expected.boot_id)
    return (actual.creation_time, actual.session_id) == (expected.creation_time, expected.session_id)


def _wait(handle, timeout, code):
    status = _api.WaitForSingleObject(handle, timeout)
    if status == 0:
        return True
    if status == _WAIT_TIMEOUT:
        return False
    raise JobError(code, C.get_last_error())


def _query(job, kind, result):
    _check(_api.QueryInformationJobObject(job, kind, C.byref(result), C.sizeof(result), None),
           'JOB_QUERY_FAILED')
    return result


def _accounting(job):
    return _query(job, _JOB_BASIC_ACCOUNTING, _Accounting())


def _is_member(process, job):
    member = W.BOOL()
    _check(_api.IsProcessInJob(process, job, C.byref(member)), 'JOB_ASSIGNMENT_FAILED')
    return bool(member.value)


def _terminate(job):
    _check(_api.TerminateJobObject(job, 1), 'JOB_TERMINATE_FAILED')


def _new_pipe(parent_cleanup, child_cleanup, *, input_pipe):
    address = rf'\\.\pipe\coding-team-{uuid.uuid4().hex}'
    direction = 2 if input_pipe else _winapi.PIPE_ACCESS_INBOUND  # PIPE_ACCESS_OUTBOUND
    mode = direction | _winapi.FILE_FLAG_FIRST_PIPE_INSTANCE | _winapi.FILE_FLAG_OVERLAPPED
    access = _winapi.GENERIC_READ if input_pipe else _winapi.GENERIC_WRITE
    # PIPE_REJECT_REMOTE_CLIENTS with a single overlapped server and synchronous client.
    parent = _winapi.CreateNamedPipe(address, mode, 0x8, 1, _CHUNK, _CHUNK, 0, _winapi.NULL)
    parent_cleanup.callback(_api.CloseHandle, parent)
    child = _winapi.CreateFile(address, access, 0, _winapi.NULL, _winapi.OPEN_EXISTING, 0, _winapi.NULL)
    child_cleanup.callback(_api.CloseHandle, child)
    connection = _winapi.ConnectNamedPipe(parent, overlapped=True)
    connection.GetOverlappedResult(True)
    _check(_api.SetHandleInformation(child, 1, 1), 'PIPE_IO_FAILED')  # HANDLE_FLAG_INHERIT
    return parent, child


def _prepare_attributes(stack, job_handles, child_handles):
    size = C.c_size_t()
    _api.InitializeProcThreadAttributeList(None, 2, 0, C.byref(size))
    if not size.value:
        raise JobError('JOB_ASSIGNMENT_FAILED', C.get_last_error())
    storage = C.create_string_buffer(size.value)
    _check(_api.InitializeProcThreadAttributeList(storage, 2, 0, C.byref(size)),
           'JOB_ASSIGNMENT_FAILED')
    stack.callback(_api.DeleteProcThreadAttributeList, storage)
    # ProcThreadAttributeValue(13/2, FALSE, TRUE, FALSE) in WinBase.h.
    for attribute, handles in ((0x2000D, job_handles), (0x20002, child_handles)):
        _check(_api.UpdateProcThreadAttribute(storage, 0, attribute, handles,
                                              C.sizeof(handles), None, None),
               'JOB_ASSIGNMENT_FAILED')
    return storage


def _environment_block(env):
    lines = []
    names = set()
    for key in sorted(env, key=str.upper):
        value = env[key]
        if not key or '=' in key or '\0' in key or '\0' in value or key.upper() in names:
            raise JobError('PROCESS_CREATE_FAILED')
        names.add(key.upper())
        lines.append(f'{key}={value}')
    return C.create_unicode_buffer('\0'.join(lines) + '\0\0')


class _Pipe:
    def __init__(self, handle, *, payload=None, limit=0):
        self.handle = handle
        self.payload = payload
        self.offset = 0
        self.limit = limit
        self.output = bytearray()
        self.overflow = False
        self.pending = None

    def close(self):
        if self.handle is None:
            return
        try:
            if self.pending is not None:
                self.pending.cancel()
                self.pending.GetOverlappedResult(True)
        except OSError as exc:
            if exc.winerror not in _PIPE_EOF | {995}:  # ERROR_OPERATION_ABORTED
                raise
        finally:
            _api.CloseHandle(self.handle)
            self.handle = None
            self.pending = None

    def advance(self):
        # One outstanding request per stream. Never wait for the child in this loop.
        if self.handle is None:
            return
        try:
            if self.pending is None:
                if self.payload is None:
                    self.pending, _ = _winapi.ReadFile(self.handle, _CHUNK, overlapped=True)
                elif self.offset < len(self.payload):
                    chunk = self.payload[self.offset:self.offset + _CHUNK]
                    self.pending, _ = _winapi.WriteFile(self.handle, chunk, overlapped=True)
                else:
                    self.close()  # EOF for the CLI after exactly the requested payload
                    return
            transferred, error = self.pending.GetOverlappedResult(False)
            if error == 996:  # ERROR_IO_INCOMPLETE
                return
            if error:
                raise C.WinError(error)
            if self.payload is None:
                if not transferred:
                    self.close()
                    return
                remaining = self.limit - len(self.output)
                self.overflow |= transferred > remaining
                self.output.extend(self.pending.getbuffer()[:remaining])
            else:
                self.offset += transferred
            self.pending = None
        except OSError as exc:
            if exc.winerror not in _PIPE_EOF:
                raise
            self.close()


class _Capture:
    def __init__(self, stdin, stdout, stderr, payload):
        self.stdin = _Pipe(stdin, payload=payload)
        self.stdout = _Pipe(stdout, limit=_STDOUT_LIMIT)
        self.stderr = _Pipe(stderr, limit=_STDERR_LIMIT)
        self.pipes = self.stdin, self.stdout, self.stderr
        self.stop = threading.Event()
        self.done = threading.Event()
        self.error = None
        self.thread = None

    @property
    def overflow(self):
        return self.stdout.overflow or self.stderr.overflow

    def start(self):
        thread = threading.Thread(target=self._run, name='coding-team-pipes', daemon=True)
        thread.start()
        self.thread = thread

    def _run(self):
        try:
            while not self.stop.is_set() and any(pipe.handle is not None for pipe in self.pipes):
                for pipe in self.pipes:
                    pipe.advance()
                self.stop.wait(.002)
        except OSError as exc:
            self.error = JobError('PIPE_IO_FAILED', exc.winerror)
        finally:
            self._close_pipes()
            self.done.set()

    def _close_pipes(self):
        for pipe in self.pipes:
            try:
                pipe.close()
            except OSError as exc:
                if exc.winerror not in _PIPE_EOF | {995}:
                    self.error = JobError('PIPE_IO_FAILED', exc.winerror)

    def close(self):
        self.stop.set()
        if self.thread is None:
            self._close_pipes()
        else:
            self.thread.join(_OUTPUT_TIMEOUT)
            if self.thread.is_alive():
                raise JobError('PIPE_IO_FAILED')


class JobHandle:
    """Runner-owned handles. Call lifecycle methods from one supervisor thread."""

    def __init__(self, job, process, thread, identity, capture):
        self._job = job
        self._process = process
        self._thread = thread
        self._identity = identity
        self._capture = capture
        self._stopping = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def identity(self) -> ProcessIdentity:
        return self._identity

    def resume(self) -> None:
        if self._thread is None or self._stopping:
            raise JobError('PROCESS_RESUME_FAILED')
        try:
            self._capture.start()
            previous = _api.ResumeThread(self._thread)
            if previous != 1:
                error = C.get_last_error() if previous == 0xFFFFFFFF else None
                raise JobError('PROCESS_RESUME_FAILED', error)
        except (JobError, RuntimeError) as exc:
            self.terminate()
            if isinstance(exc, JobError):
                raise
            raise JobError('PROCESS_RESUME_FAILED') from exc
        finally:
            _api.CloseHandle(self._thread)
            self._thread = None

    def poll(self) -> JobState:
        if self._job is None:
            raise JobError('JOB_QUERY_FAILED')
        exit_code = None
        if _wait(self._process, 0, 'JOB_QUERY_FAILED'):
            code = W.DWORD()
            _check(_api.GetExitCodeProcess(self._process, C.byref(code)), 'JOB_QUERY_FAILED')
            exit_code = code.value
        active = _accounting(self._job).active
        return JobState(exit_code, active, self._capture.overflow)

    def terminate(self) -> None:
        if self._job is None:
            raise JobError('JOB_TERMINATE_FAILED')
        self._stopping = True
        _terminate(self._job)

    def prove_stopped(self, key: AttemptKey, runner_claim: str) -> StopProof | None:
        if self._job is None or _accounting(self._job).active or not _wait(self._process, 0, 'JOB_QUERY_FAILED'):
            return None
        return _stop_proof(key, runner_claim, self._identity, 'JOB_EMPTY')

    def get_output(self) -> CapturedOutput:
        state = self.poll()
        if state.active_processes or state.root_exit_code is None:
            raise JobError('OUTPUT_NOT_READY')
        if self._capture.thread is None:
            return CapturedOutput(state.root_exit_code, b'', b'', False)
        if not self._capture.done.wait(_OUTPUT_TIMEOUT):
            raise JobError('PIPE_IO_FAILED')
        if self._capture.error is not None:
            raise self._capture.error
        return CapturedOutput(state.root_exit_code, bytes(self._capture.stdout.output),
                              bytes(self._capture.stderr.output), self._capture.overflow)

    def close(self) -> None:
        if self._job is None:
            return
        # Terminate even if a trusted observer still holds the named job open.
        # Last-handle close remains the fallback and the supervisor-crash path.
        try:
            if _accounting(self._job).active:
                self.terminate()
            deadline = time.monotonic() + _OUTPUT_TIMEOUT
            while _accounting(self._job).active and time.monotonic() < deadline:
                time.sleep(.01)
        finally:
            _api.CloseHandle(self._job)
            self._job = None
            if self._thread is not None:
                _api.CloseHandle(self._thread)
                self._thread = None
            try:
                _wait(self._process, 1000, 'JOB_QUERY_FAILED')
            finally:
                _api.CloseHandle(self._process)
                self._capture.close()


def spawn_suspended(launch: LaunchSpec, job_name: str) -> JobHandle:
    _windows()
    if not _PRIVATE_NAME.fullmatch(job_name):
        raise JobError('JOB_CREATE_FAILED')
    if not launch.executable.is_absolute() or not launch.cwd.is_absolute():
        raise JobError('PROCESS_CREATE_FAILED')
    executable = str(launch.executable)
    cwd = str(launch.cwd)
    argv = (executable, *launch.args)
    if any('\0' in value for value in (*argv, cwd)):
        raise JobError('PROCESS_CREATE_FAILED')
    command = C.create_unicode_buffer(subprocess.list2cmdline(argv))
    environment = _environment_block(launch.env)
    owner = current_owner_process()

    with ExitStack() as job_cleanup:
        C.set_last_error(0)
        job = _check(_api.CreateJobObjectW(None, job_name), 'JOB_CREATE_FAILED')
        last_error = C.get_last_error()
        job_cleanup.callback(_api.CloseHandle, job)
        if last_error == 183:  # ERROR_ALREADY_EXISTS: never change another job's limits.
            raise JobError('JOB_CREATE_FAILED', last_error)
        limits = _ExtendedLimits()
        limits.basic.flags = _KILL_ON_JOB_CLOSE
        _check(_api.SetInformationJobObject(job, _JOB_EXTENDED_LIMITS, C.byref(limits),
                                           C.sizeof(limits)), 'JOB_ASSIGNMENT_FAILED')

        with ExitStack() as temporary:
            try:
                stdin, child_in = _new_pipe(job_cleanup, temporary, input_pipe=True)
                stdout, child_out = _new_pipe(job_cleanup, temporary, input_pipe=False)
                stderr, child_err = _new_pipe(job_cleanup, temporary, input_pipe=False)
            except OSError as exc:
                raise JobError('PIPE_IO_FAILED', exc.winerror) from None
            startup = _StartupInfoEx()
            startup.startup.size = C.sizeof(startup)
            startup.startup.flags = 0x0100  # STARTF_USESTDHANDLES
            startup.startup.stdin, startup.startup.stdout, startup.startup.stderr = (
                child_in, child_out, child_err)
            job_handles = (W.HANDLE * 1)(job)
            child_handles = (W.HANDLE * 3)(child_in, child_out, child_err)
            attributes = _prepare_attributes(temporary, job_handles, child_handles)
            startup.attributes = C.addressof(attributes)
            info = _ProcessInfo()
            # EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW |
            # CREATE_UNICODE_ENVIRONMENT | CREATE_SUSPENDED.
            flags = 0x00080000 | 0x08000000 | 0x00000400 | 0x00000004
            _check(_api.CreateProcessW(executable, command, None, None, True, flags, environment,
                                       cwd, C.byref(startup), C.byref(info)), 'PROCESS_CREATE_FAILED')
            job_cleanup.callback(_api.CloseHandle, info.process)
            job_cleanup.callback(_api.CloseHandle, info.thread)
            # Only on setup failure, use the exact new process handle while it is
            # still suspended. This also covers contradictory membership results.
            job_cleanup.callback(_api.TerminateProcess, info.process, 1)
        if not _is_member(info.process, job):
            raise JobError('JOB_ASSIGNMENT_FAILED')
        process = _process_identity(info.process, info.pid, owner.boot_id)
        if process.session_id != owner.session_id:
            raise JobError('PROCESS_IDENTITY_UNAVAILABLE')
        identity = ProcessIdentity(**vars(process), job_name=job_name)
        capture = _Capture(stdin, stdout, stderr, launch.stdin_bytes)
        handle = JobHandle(job, info.process, info.thread, identity, capture)
        job_cleanup.pop_all()
        return handle


def _stop_proof(key, runner_claim, identity, basis):
    from ct_external_store import StopProof

    return StopProof(key=key, runner_claim=runner_claim, identity=identity, basis=basis)


def _valid_process(identity):
    try:
        boot = uuid.UUID(identity.boot_id)
    except (ValueError, AttributeError, TypeError):
        return False
    return (type(identity.pid) is int and 0 < identity.pid <= 0xFFFFFFFF
            and type(identity.creation_time) is int and 0 < identity.creation_time < 1 << 64
            and type(identity.session_id) is int and 0 <= identity.session_id <= 0xFFFFFFFF
            and boot.int != 0 and str(boot) == identity.boot_id)


def _owner_process_alive(owner_process: OwnerProcess, current_boot: str) -> bool | None:
    if current_boot != owner_process.boot_id:
        return False
    handle = _api.OpenProcess(_PROCESS_INSPECT, False, owner_process.pid)
    if not handle:
        # For a validated nonzero PID, ERROR_INVALID_PARAMETER reports absence.
        return False if C.get_last_error() == 87 else None
    try:
        if _wait(handle, 0, 'PROCESS_IDENTITY_UNAVAILABLE'):
            return False
        if not _same_process(handle, owner_process):
            return False
        return not _wait(handle, 0, 'PROCESS_IDENTITY_UNAVAILABLE')
    except JobError:
        return None
    finally:
        _api.CloseHandle(handle)


def owner_process_alive(owner_process: OwnerProcess) -> bool | None:
    """Check a persisted owner identity; None means Windows could not verify it."""
    if not isinstance(owner_process, OwnerProcess) or not _valid_process(owner_process):
        return None
    try:
        current_boot = boot_id()
    except JobError:
        return None
    return _owner_process_alive(owner_process, current_boot)


def recover_tree(identity: ProcessIdentity | None, owner_process: OwnerProcess, *,
                 key: AttemptKey, runner_claim: str) -> StopProof | None:
    """Recover a persisted job after the caller fences the exact runner claim.

    On the same boot, a missing job or missing independently identifiable root
    leaves recovery ambiguous. An old owner PID alone never authorizes a kill.
    """
    if not isinstance(owner_process, OwnerProcess) or not _valid_process(owner_process):
        return None
    if identity is not None and (
            not isinstance(identity, ProcessIdentity) or not _valid_process(identity)
            or not isinstance(identity.job_name, str) or not _PRIVATE_NAME.fullmatch(identity.job_name)
            or (identity.boot_id, identity.session_id) != (owner_process.boot_id, owner_process.session_id)):
        return None
    try:
        current = current_owner_process()
        if current.boot_id != owner_process.boot_id:
            return _stop_proof(key, runner_claim, identity, 'BOOT_CHANGED')
        if identity is None or current.session_id != owner_process.session_id:
            return None
        if _owner_process_alive(owner_process, current.boot_id) is not False:
            return None
        with ExitStack() as handles:
            process = _api.OpenProcess(_PROCESS_INSPECT, False, identity.pid)
            if not process:
                return None
            handles.callback(_api.CloseHandle, process)
            if not _same_process(process, identity):
                return None
            job = _api.OpenJobObjectW(_JOB_OBJECT_QUERY | _JOB_OBJECT_TERMINATE, False,
                                      identity.job_name)
            if not job:
                return None
            handles.callback(_api.CloseHandle, job)
            flags = _query(job, _JOB_EXTENDED_LIMITS, _ExtendedLimits()).basic.flags
            if not flags & _KILL_ON_JOB_CLOSE or flags & _BREAKAWAY_FLAGS or not _is_member(process, job):
                return None
            if _accounting(job).active:
                _terminate(job)
            deadline = time.monotonic() + _OUTPUT_TIMEOUT
            while time.monotonic() < deadline:
                accounting = _accounting(job)
                if accounting.total and not accounting.active and _wait(process, 0, 'JOB_QUERY_FAILED'):
                    return _stop_proof(key, runner_claim, identity, 'JOB_EMPTY')
                time.sleep(.01)
    except JobError:
        return None
    return None
