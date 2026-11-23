"""Lower this process to Windows Low Integrity; never elevate or launch workers."""
import ctypes
from ctypes import wintypes
import sys


def restrict_process(launch=None, cwd=None, log_path=None):
    if sys.platform != 'win32':
        raise RuntimeError('This read-only preview requires Windows integrity isolation')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [wintypes.LPVOID]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.DuplicateTokenEx.argtypes = [wintypes.HANDLE,wintypes.DWORD,wintypes.LPVOID,ctypes.c_int,ctypes.c_int,ctypes.POINTER(wintypes.HANDLE)]
    advapi.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPVOID)]
    advapi.GetLengthSid.argtypes = [wintypes.LPVOID]
    advapi.GetLengthSid.restype = wintypes.DWORD
    advapi.SetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]

    class Label(ctypes.Structure):
        _fields_ = [('sid', wintypes.LPVOID), ('attributes', wintypes.DWORD)]

    token = wintypes.HANDLE()
    sid = wintypes.LPVOID()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x2b, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if launch:
            original=token
            token=wintypes.HANDLE()
            if not advapi.DuplicateTokenEx(original,0x02000000,None,2,1,ctypes.byref(token)):
                kernel.CloseHandle(original)
                raise ctypes.WinError(ctypes.get_last_error())
            kernel.CloseHandle(original)
        if not advapi.ConvertStringSidToSidW('S-1-16-4096', ctypes.byref(sid)):
            raise ctypes.WinError(ctypes.get_last_error())
        label = Label(sid, 0x20)
        if not advapi.SetTokenInformation(token, 25, ctypes.byref(label), ctypes.sizeof(label) + advapi.GetLengthSid(sid)):
            raise ctypes.WinError(ctypes.get_last_error())
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 25, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 25, buffer, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        current = ctypes.cast(buffer, ctypes.POINTER(Label)).contents
        name = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(current.sid, ctypes.byref(name)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if name.value != 'S-1-16-4096':
                raise RuntimeError('Read-only preview integrity isolation failed')
        finally:
            kernel.LocalFree(name)
        if launch:
            return launch_process(advapi,kernel,token,launch,cwd,log_path)
    finally:
        if sid:
            kernel.LocalFree(sid)
        kernel.CloseHandle(token)


def launch_process(advapi,kernel,token,command,cwd,log_path):
    import msvcrt
    import os
    import subprocess

    class Startup(ctypes.Structure):
        _fields_=[('cb',wintypes.DWORD),('reserved',wintypes.LPWSTR),('desktop',wintypes.LPWSTR),('title',wintypes.LPWSTR),
                  ('x',wintypes.DWORD),('y',wintypes.DWORD),('cx',wintypes.DWORD),('cy',wintypes.DWORD),
                  ('charsx',wintypes.DWORD),('charsy',wintypes.DWORD),('fill',wintypes.DWORD),('flags',wintypes.DWORD),
                  ('show',wintypes.WORD),('reserved_size',wintypes.WORD),('reserved_bytes',wintypes.LPVOID),
                  ('stdin',wintypes.HANDLE),('stdout',wintypes.HANDLE),('stderr',wintypes.HANDLE)]

    class Process(ctypes.Structure):
        _fields_=[('process',wintypes.HANDLE),('thread',wintypes.HANDLE),('pid',wintypes.DWORD),('tid',wintypes.DWORD)]

    advapi.CreateProcessAsUserW.argtypes=[wintypes.HANDLE,wintypes.LPCWSTR,wintypes.LPWSTR,wintypes.LPVOID,wintypes.LPVOID,
        wintypes.BOOL,wintypes.DWORD,wintypes.LPVOID,wintypes.LPCWSTR,ctypes.POINTER(Startup),ctypes.POINTER(Process)]
    with open(log_path,'ab',buffering=0) as output,open(os.devnull,'rb') as source:
        out=msvcrt.get_osfhandle(output.fileno());inp=msvcrt.get_osfhandle(source.fileno())
        os.set_handle_inheritable(out,True);os.set_handle_inheritable(inp,True)
        info=Startup(cb=ctypes.sizeof(Startup),flags=0x100,stdin=inp,stdout=out,stderr=out)
        process=Process()
        args=ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
        if not advapi.CreateProcessAsUserW(token,None,args,None,None,True,0x08000000,None,str(cwd),ctypes.byref(info),ctypes.byref(process)):
            raise ctypes.WinError(ctypes.get_last_error())
        kernel.CloseHandle(process.thread)
        kernel.CloseHandle(process.process)
        return process.pid
