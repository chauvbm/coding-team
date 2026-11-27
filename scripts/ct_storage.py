"""Shared storage primitives: OS lock, atomic write, shell quoting, link guard."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import shlex
import stat
import tempfile
import time


class LinkedPathError(ValueError):
    """Raised when a path or one of its ancestors is a symlink, junction or reparse point."""


@contextmanager
def locked(path, timeout=5.0):
    """OS lock releases on process exit; retain its file to avoid inode races."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + '.lock', 'a+b') as stream:
        deadline = time.monotonic() + timeout
        while True:
            try:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('policy lock deadline exceeded')
                time.sleep(0.02)
        try:
            # Windows can lock past EOF. Initialize only while owning the range.
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b'0')
                stream.flush()
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def atomic_write(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def shell_argument(value):
    """Quote one literal argument for the bootstrap's host shell."""
    if os.name == 'nt':
        return "'" + str(value).replace("'", "''") + "'"
    return shlex.quote(str(value))


def _reject_link(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    reparse = getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
    if path.is_symlink() or reparse or (hasattr(path, 'is_junction') and path.is_junction()):
        raise LinkedPathError('linked path rejected: ' + str(path))


def reject_links(path, stop_at):
    """Reject path and every existing ancestor up to and including stop_at if linked."""
    path = Path(os.path.abspath(path))
    stop_at = Path(os.path.abspath(stop_at))
    if stop_at != path and stop_at not in path.parents:
        raise LinkedPathError('path is not under ' + str(stop_at))
    current = path
    while True:
        _reject_link(current)
        if current == stop_at:
            return
        current = current.parent
