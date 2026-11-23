"""Explicit administrator actions launch the separate People worker."""
import os
import subprocess
import sys
import threading

from app.config import ROOT

_lock = threading.Lock()
_processes = {}


def launch_explicit(module, arguments, key, logfile):
    """Shared explicit process launch, never a polling scheduler.

    Arguments and module are server-owned; callers must authorize and persist work
    first. Advisory leases in each worker remain the cross-process authority.
    """
    if module not in ('face_detect.testing.automatic_people_fixture','face_detect.jobs.automatic_people_local'):
        raise ValueError('Unknown worker module')
    with _lock:
        for existing_key, process in list(_processes.items()):
            if process.poll() is not None:
                del _processes[existing_key]
        if key in _processes:
            return False
        logfile.parent.mkdir(parents=True, exist_ok=True)
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS) if os.name == 'nt' else 0
        # Worker advisory locks are the cross-process authority; this registry
        # merely avoids duplicate launches within a serving process.
        with logfile.open('ab') as output:
            process = subprocess.Popen(
                [sys.executable, '-m', module, *arguments],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                creationflags=flags, close_fds=True)
        _processes[key] = process
        return True
