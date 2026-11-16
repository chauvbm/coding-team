"""Local process and pipe fixture, with every marker confined to the test cwd."""

import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def write_json(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data), encoding='utf-8')
    tmp.replace(path)


def main(mode, *args):
    if mode == 'supervisor':
        from dataclasses import asdict
        from types import SimpleNamespace

        sys.path.insert(0, args[0])
        import ct_winjob

        cwd = Path.cwd()
        launch = SimpleNamespace(executable=Path(sys.executable),
                                 args=(__file__, args[2]), cwd=cwd,
                                 env=os.environ.copy(), stdin_bytes=b'')
        with ct_winjob.spawn_suspended(launch, args[1]) as job:
            write_json(cwd / 'identities.json', dict(worker=asdict(job.identity()),
                       owner=asdict(ct_winjob.current_owner_process())))
            job.resume()
            time.sleep(20)
    elif mode in ('root', 'root-wait'):
        subprocess.Popen([sys.executable, __file__, 'child'])
        print('root', flush=True)
        if mode == 'root-wait':
            time.sleep(20)
    elif mode == 'child':
        print('child', flush=True)
        write_json(Path('child.json'), {'pid': os.getpid()})
        for _ in range(1000):
            if Path('release').exists():
                Path('late-write').write_text('child wrote', encoding='utf-8')
                return
            time.sleep(.02)
    elif mode == 'input':
        sys.stdout.buffer.write(sys.stdin.buffer.read())
        sys.stderr.buffer.write(b'diagnostic')
    elif mode == 'blocked':
        Path('ready').touch()
        time.sleep(20)
    elif mode == 'flood':
        for _ in range(48):
            os.write(1, b'o' * 32768)
            os.write(2, b'e' * 32768)
    elif mode == 'inherit':
        set_event = ctypes.WinDLL('kernel32', use_last_error=True).SetEvent
        set_event.argtypes = [ctypes.c_void_p]
        set_event(int(args[0]))
    elif mode == 'breakaway':
        try:
            child = subprocess.Popen([sys.executable, __file__, 'child'],
                                     creationflags=subprocess.CREATE_BREAKAWAY_FROM_JOB)
        except PermissionError:
            print('blocked', flush=True)
        else:
            child.kill()
            child.wait()
            raise RuntimeError('child unexpectedly allowed to break away')
    else:
        raise ValueError(mode)


if __name__ == '__main__':
    main(*sys.argv[1:])
