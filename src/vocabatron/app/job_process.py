"""Owned computation entry point; never imported by the HTTP request handler."""
import ctypes
import json
import os
from pathlib import Path
import resource
import signal
import sys
import time


def main():
    config_path,task_id,fence,expected_parent=sys.argv[1:]
    libc=ctypes.CDLL(None,use_errno=True)
    if libc.prctl(1,signal.SIGTERM,0,0,0)!=0 or os.getppid()!=int(expected_parent):return 75
    from .config import load_config
    from .resources import idle_priority
    config=load_config(config_path);idle_priority()
    os.environ['OMP_NUM_THREADS']='1';os.environ['OPENBLAS_NUM_THREADS']='1'
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    resource.setrlimit(resource.RLIMIT_NOFILE,(128,128))
    resource.setrlimit(resource.RLIMIT_FSIZE,(512*1024**2,512*1024**2))
    resource.setrlimit(resource.RLIMIT_AS,(config.resources.memory_max_bytes+4*1024**3,)*2)
    resource.setrlimit(resource.RLIMIT_CPU,(1800,1800))
    from ..domain import Problem
    from ..storage import PrivateStore
    from .library import Library
    from .jobs import Job
    started=time.perf_counter()
    try:result=Job(Library(config),task_id,fence).run()
    except Problem as exc:
        result={'status':'ERROR','code':exc.code,'message':english_message(exc.code)}
        if isinstance(exc.details,dict) and 'search_evidence' in exc.details:
            result['search_evidence']=exc.details['search_evidence']
    except (MemoryError,OverflowError):result={'status':'ERROR','code':'RESOURCE_LIMIT','message':'This attempt reached its resource limit'}
    except Exception as exc:
        result={'status':'ERROR','code':'INTERNAL_ERROR','error_type':type(exc).__name__,'message':'The attempt failed safely. Saved results remain available.'}
        import traceback
        traceback.print_exc()
    result.setdefault('seconds',time.perf_counter()-started)
    result.setdefault('peak_rss_kib',resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    result.setdefault('memory_scope','single queue-slice process lifetime peak; excludes shared Ollama')
    PrivateStore(config.runtime_root).json(f'jobs/{task_id}-{fence}/result.json',result)
    return 0


def english_message(code):
    messages={'UNKNOWN':'The search window ended. The remaining space has not been exhausted.',
              'MODEL_REQUEST_TIMEOUT':'The local model request timed out. A bounded retry will recheck resources.',
              'OLLAMA_UNAVAILABLE':'The local model service is temporarily unavailable.',
              'PAUSED_FOR_RESOURCES':'Paused to avoid interfering with other workloads.',
              'CANCELLED':'Task cancelled. Completed variants are retained.',
              'PAUSED_BY_USER':'Task paused. Saved progress is retained.',
              'MODEL_SELECTION_INVALID':'The model did not return a valid complete selection.',
              'PENDING_VARIANTS':'Previously discovered variants need to finish before exhaustion can be declared.',
              'RESOURCE_LIMIT':'This attempt reached its resource limit. Saved progress is retained.'}
    return messages.get(code,'This task needs attention. Its source and saved results have been preserved.')


if __name__=='__main__':raise SystemExit(main())
