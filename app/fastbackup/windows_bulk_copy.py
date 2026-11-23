"""Bare Windows copies for an admitted, immutable source and new destination.

No content checks or per-file filesystem admission here. The caller owns path
admission, capacity, cancellation and the separate verification/publication pass.
"""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import ctypes
from ctypes import wintypes as w
from functools import lru_cache


WORKERS = 16


class Parameters(ctypes.Structure):
    _fields_ = [('size', w.DWORD), ('flags', w.DWORD),
                ('cancel', ctypes.POINTER(w.BOOL)), ('callback', ctypes.c_void_p),
                ('context', ctypes.c_void_p)]


@lru_cache(maxsize=1)
def copy_api():
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CopyFile2.argtypes = [w.LPCWSTR, w.LPCWSTR, ctypes.POINTER(Parameters)]
    api.CopyFile2.restype = ctypes.c_long
    return api.CopyFile2


def copy_file(source, target, **kwargs):
    parameters = Parameters()
    parameters.size = ctypes.sizeof(parameters)
    # FAIL_IF_EXISTS (0x1) | COPY_SYMLINK (0x800) | SKIP_ALTERNATE_STREAMS (0x8000).
    # RESTARTABLE (0x2) is not set. An unexpected link is copied, not followed;
    # the separate destination verification rejects links before publication.
    parameters.flags = 0x8801
    result = copy_api()(str(source), str(target), ctypes.byref(parameters))
    if result < 0:
        raise OSError(f'CopyFile2 failed HRESULT 0x{result & 0xffffffff:08x}')


def copy_files(source, target, files, *, copier=copy_file, cancelled=lambda: False,
               completed=lambda size: None):
    """Bound queued work and join all writers before returning, even on failure."""
    entries = iter(files)
    pending = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        def submit():
            if cancelled():
                raise InterruptedError('Copy cancelled')
            entry = next(entries, None)
            if entry is None:
                return False
            name, size = entry
            pending[pool.submit(copier, source / name, target / name)] = size
            return True

        try:
            for _ in range(WORKERS * 2):
                if not submit():
                    break
            while pending:
                if cancelled():
                    raise InterruptedError('Copy cancelled')
                done, _ = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
                for future in done:
                    size = pending.pop(future)
                    future.result()
                    completed(size)
                for _ in done:
                    if not submit():
                        break
        finally:
            for future in pending:
                future.cancel()
            # Executor exit waits for active CopyFile2 calls. Never verify or
            # restart the app while a destination writer is still running.
