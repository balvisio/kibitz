"""Native process identity for supported macOS and Linux, including tool sandboxes."""
import ctypes
import os
from pathlib import Path
import sys
import time


class BSDInfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in
                ('flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid', 'rgid', 'svuid', 'svgid', 'reserved')]
    _fields_ += [('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)]
    _fields_ += [(name, ctypes.c_uint32) for name in ('nfiles', 'pgid', 'jobc', 'tty', 'tpgid', 'nice')]
    _fields_ += [('seconds', ctypes.c_uint64), ('micros', ctypes.c_uint64)]


_libproc = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True) if sys.platform == 'darwin' else None
if _libproc:
    _libproc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    _libproc.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]


def process(pid):
    pid = int(pid)
    if pid <= 0:
        return None
    try:
        if _libproc:
            info = BSDInfo()
            if _libproc.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
                return None
            return dict(parent=info.ppid, command=(info.name or info.comm).decode(errors='replace'),
                        started=' '.join(time.strftime('%a %b %e %H:%M:%S %Y', time.gmtime(info.seconds)).split()),
                        birth=(info.seconds, info.micros), tty=info.tty,
                        foreground=info.pgid == info.tpgid, stopped=info.status in (4, 5))
        if sys.platform == 'linux':
            head, tail = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)
            fields = tail.split()
            return dict(parent=int(fields[1]), command=head.split('(', 1)[1], started=fields[19],
                        birth=fields[19], tty=int(fields[4]) & 0xffffffff,
                        foreground=fields[2] == fields[5], stopped=fields[0] in 'ZTtX')
        raise RuntimeError('Kibitz requires macOS or Linux')
    except (OSError, ValueError, IndexError):
        return None


def ancestors(pid):
    records = {}
    while pid > 1 and pid not in records:
        info = process(pid)
        if not info:
            break
        records[pid] = info
        if Path(info['command']).name.startswith('tmux'):
            break
        pid = info['parent']
    return records


def terminal_processes(tty):
    device = os.stat(tty).st_rdev & 0xffffffff
    if _libproc:
        # PROC_TTY_ONLY: query this terminal, not the system-wide process table.
        count = _libproc.proc_listpids(3, device, None, 0)
        if count < 0:
            raise OSError('cannot list terminal processes')
        buffer = (ctypes.c_int * (count // ctypes.sizeof(ctypes.c_int) + 64))()
        size = _libproc.proc_listpids(3, device, buffer, ctypes.sizeof(buffer))
        if size < 0:
            raise OSError('cannot list terminal processes')
        pids = buffer[:size // ctypes.sizeof(ctypes.c_int)]
    else:
        pids = (int(path.name) for path in Path('/proc').iterdir() if path.name.isdigit())
    result = {}
    for pid in pids:
        info = process(pid)
        if info and info['tty'] == device:
            result[pid] = info
    return result
