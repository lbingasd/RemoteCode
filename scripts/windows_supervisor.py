"""Console-free Windows process supervisor; logs lifecycle metadata, never child output."""
import ctypes
from ctypes import wintypes
import logging
from logging.handlers import RotatingFileHandler
import os
import subprocess
import time

from common import write


class ProcessJob:
    """Kill child processes when the supervisor's job handle is closed."""
    def __init__(self):
        class Basic(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                        ('flags', wintypes.DWORD), ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t),
                        ('active', wintypes.DWORD), ('affinity', ctypes.c_size_t),
                        ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', IO), ('process_memory', ctypes.c_size_t),
                        ('job_memory', ctypes.c_size_t), ('peak_process_memory', ctypes.c_size_t), ('peak_job_memory', ctypes.c_size_t)]
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.SetInformationJobObject.restype = wintypes.BOOL
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.AssignProcessToJobObject.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def assign(self, child):
        if not self.api.AssignProcessToJobObject(self.handle, int(child._handle)):
            error = ctypes.get_last_error()
            child.kill()
            child.wait()
            raise ctypes.WinError(error)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def supervise(root, command, workspace, retry_delay=5, component='ide'):
    import msvcrt
    if component not in ('ide', 'qq'):
        raise ValueError('Unknown supervised component')
    label = 'IDE' if component == 'ide' else 'QQ Bot'
    logs = root / 'logs'
    logs.mkdir(exist_ok=True)
    with (logs / f'{component}-supervisor.lock').open('a+b') as lock:
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return  # Another supervisor owns this runtime.
        logger = logging.Logger(f'{component}-supervisor')
        handler = RotatingFileHandler(logs / f'{component}-supervisor.log', maxBytes=1024 * 1024, backupCount=2, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        logger.addHandler(handler)
        attempts = 0
        while True:
            attempts += 1
            job = None
            try:
                job = ProcessJob()
                child = subprocess.Popen(command, cwd=workspace, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         creationflags=subprocess.CREATE_NO_WINDOW)
                job.assign(child)
                write(logs / f'{component}-supervisor.json', {'supervisor_pid': os.getpid(), 'child_pid': child.pid,
                                                    'attempts': attempts, 'phase': 'running'})
                logger.info('%s started pid=%s attempt=%s', label, child.pid, attempts)
                code = child.wait()
                logger.warning('%s exited code=%s; restarting in %ss', label, code, retry_delay)
            except OSError as exc:
                logger.error('%s launch failed type=%s winerror=%s', label, type(exc).__name__, getattr(exc, 'winerror', None))
            finally:
                if job:
                    job.close()
            write(logs / f'{component}-supervisor.json', {'supervisor_pid': os.getpid(), 'child_pid': None,
                                                'attempts': attempts, 'phase': 'restarting'})
            time.sleep(retry_delay)
