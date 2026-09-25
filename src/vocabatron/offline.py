"""Fail-closed Linux network boundary inherited by document subprocesses."""
import ctypes
import errno
import os
from pathlib import Path


def deny_network():
    lib=ctypes.CDLL('libseccomp.so.2',use_errno=True)
    lib.seccomp_init.argtypes=[ctypes.c_uint32];lib.seccomp_init.restype=ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes=[ctypes.c_char_p];lib.seccomp_syscall_resolve_name.restype=ctypes.c_int
    lib.seccomp_rule_add.argtypes=[ctypes.c_void_p,ctypes.c_uint32,ctypes.c_int,ctypes.c_uint]
    lib.seccomp_load.argtypes=[ctypes.c_void_p];lib.seccomp_release.argtypes=[ctypes.c_void_p]
    context=lib.seccomp_init(0x7fff0000)
    if not context:raise RuntimeError('Document network isolation is unavailable')
    try:
        for name in ('socket','socketpair','connect','bind','listen','accept','accept4','sendto','sendmsg','sendmmsg','recvfrom','recvmsg','recvmmsg'):
            number=lib.seccomp_syscall_resolve_name(name.encode())
            if number>=0 and lib.seccomp_rule_add(context,0x00050000|errno.EPERM,number,0)!=0:
                raise RuntimeError('Document network isolation could not be configured')
        if lib.seccomp_load(context)!=0:raise RuntimeError('Document network isolation could not be enforced')
    finally:lib.seccomp_release(context)
    # Probe the kernel boundary, not a Python monkeypatch or environment flag.
    import socket
    for family in (socket.AF_INET,socket.AF_INET6):
        try:s=socket.socket(family,socket.SOCK_STREAM)
        except PermissionError:continue
        else:s.close();raise RuntimeError('Document network isolation failed its self-check')
    os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',DO_NOT_TRACK='1')
    return 'seccomp-network-denied-and-probed'
