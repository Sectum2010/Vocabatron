"""Minimal Linux guardian. Apply limits before importing any document library."""
import ctypes
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import time


def main():
    job=Path(sys.argv[1]);limits=json.loads(sys.argv[2]);os.umask(0o077)
    libc=ctypes.CDLL(None,use_errno=True)
    # A separate guardian stays responsive even if a native parser is wedged.
    parent=(int(sys.argv[-1]) if '--worker' in sys.argv
            else json.loads((job/'owner.json').read_bytes())['owner']['pid'])
    def terminate(*args):
        signal.signal(signal.SIGTERM,signal.SIG_IGN)
        try:os.killpg(os.getpgrp(),signal.SIGTERM)
        except ProcessLookupError:pass
        time.sleep(.15)
        os.killpg(os.getpgrp(),signal.SIGKILL)
    signal.signal(signal.SIGTERM,terminate)
    libc.prctl(1,signal.SIGTERM,0,0,0)
    if os.getppid()!=parent:terminate()
    for key,value in ((resource.RLIMIT_CORE,0),(resource.RLIMIT_CPU,limits['cpu_seconds']),
        (resource.RLIMIT_AS,limits['address_bytes']),(resource.RLIMIT_NOFILE,limits['fds']),
        (resource.RLIMIT_FSIZE,limits['file_bytes'])):
        resource.setrlimit(key,(value,value))
    if '--worker' in sys.argv:
        # The guardian owns cancellation; default TERM kills this worker promptly.
        signal.signal(signal.SIGTERM,signal.SIG_DFL)
        from vocabatron.worker import main as worker
        return worker(job,limits)
    process=subprocess.Popen([sys.executable,__file__,str(job),json.dumps(limits),'--worker',str(os.getpid())],
        stdin=subprocess.DEVNULL,close_fds=True,shell=False)
    return process.wait()


if __name__=='__main__':sys.exit(main())
