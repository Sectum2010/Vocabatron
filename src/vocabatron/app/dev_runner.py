"""Local build/test guard. Commands run only in quiet windows at idle priority."""
from __future__ import annotations
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import psutil
from pydantic import Field
from .config import ResourcePolicy
from .resources import Telemetry,pressure_reason,idle_priority


class CpuDevelopmentPolicy(ResourcePolicy):
    """Bounded, explicitly authorized CPU testing can use host spare capacity.

    Percentages are of the whole host. A bounded affinity budget is available;
    memory pressure triggers yield, while transient CPU/I/O stalls do not discard
    a running regression. New commands still wait for quiet admission.
    Production and inference admission keep their separately configured policy.
    """
    external_cpu_start_percent: float = Field(default=50,gt=0,le=50)
    external_cpu_stop_percent: float = Field(default=65,gt=0,le=65)
    cpu_pressure_min_external_percent: float = Field(default=30,ge=0,le=30)


def sample_summary(sample):
    return {'host_cpu_percent':sample.get('cpu_percent'),
            'external_cpu_percent':sample.get('external_cpu_percent'),
            'application_cpu_percent':sample.get('application_cpu_percent'),
            'memory_available_gib':sample.get('memory',{}).get('MemAvailable',0)/1024**3,
            'application_rss_gib':sample.get('application',{}).get('rss_upper_bound_bytes',0)/1024**3,
            'pressure':{k:(v or {}).get('some',{}).get('avg10') for k,v in sample.get('pressure',{}).items()}}


def stop_owned_group(process):
    """The wrapper can exit before signal-aware children; stop its whole group."""
    with contextlib.suppress(ProcessLookupError):os.killpg(process.pid,signal.SIGKILL)
    process.wait()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--max-seconds',type=int,default=1800)
    parser.add_argument('--idle-seconds',type=float,default=20)
    parser.add_argument('--cpu-only',action='store_true',help='Explicitly authorized CPU-only testing while unrelated GPU work continues')
    parser.add_argument('--cpu-budget',type=int,choices=range(1,9),default=4)
    parser.add_argument('command',nargs=argparse.REMAINDER)
    args=parser.parse_args();command=args.command
    if command and command[0]=='--':command=command[1:]
    if not command:parser.error('A command is required')
    idle_priority()
    os.environ.update({'OPENBLAS_NUM_THREADS':'1','OMP_NUM_THREADS':'1','VOCABATRON_THREAD_BUDGET':str(args.cpu_budget),
                       'UV_CONCURRENT_DOWNLOADS':'2','UV_CONCURRENT_BUILDS':'1','UV_CONCURRENT_INSTALLS':'1'})
    if args.cpu_only:os.environ.update({'CUDA_VISIBLE_DEVICES':'','NVIDIA_VISIBLE_DEVICES':'void'})
    # A hard affinity ceiling also covers native build/test workers which do not
    # expose a thread setting. It affects only this command's descendants.
    cpus=sorted(os.sched_getaffinity(0));loads=psutil.cpu_percent(interval=1,percpu=True)
    selected=sorted(cpus,key=lambda cpu:(loads[cpu],cpu))[:args.cpu_budget]
    os.sched_setaffinity(0,set(selected))
    telemetry=Telemetry(Path.cwd(),model_probe=False)
    # Include this command's full CPU budget in the headroom decision. Keep at
    # least 30% of the host free at launch and 15% while running on a 20 CPU host.
    policy=CpuDevelopmentPolicy() if args.cpu_only else ResourcePolicy()
    if args.cpu_only:
        host_count=os.cpu_count() or 1
        policy=policy.model_copy(update={
            'external_cpu_start_percent':max(0,min(50,70-100*args.cpu_budget/host_count)),
            'external_cpu_stop_percent':max(0,min(65,85-100*args.cpu_budget/host_count))})
    deadline=time.monotonic()+args.max_seconds;quiet=None;process=None;last_reason=None
    stopping=False;stop_at=0;attempts=0;last_report=0;cpu_busy_since=None
    project_root=Path(__file__).resolve().parents[3]
    lock_path=project_root/'.cache'/'development.lock';lock_path.parent.mkdir(mode=0o700,exist_ok=True)
    lock=os.open(lock_path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    # Development commands share one affinity budget and serialize heavy
    # work. Independent build/test launches cannot bypass the global budget.
    while True:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
        except BlockingIOError:
            if time.monotonic()>=deadline:os.close(lock);return 75
            time.sleep(1)
    def cleanup(*_):
        if process is not None:
            stop_owned_group(process)
        os.close(lock)
        raise SystemExit(130)
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,cleanup)
    while time.monotonic()<deadline:
        sample=telemetry.sample();reason=pressure_reason(sample,policy,running=process is not None,cpu_only=args.cpu_only)
        # A single CPU-only load spike is not evidence of sustained contention.
        # Pressure, low memory, missing data and severe host load still stop now.
        if args.cpu_only and process is not None and reason=='Other applications are using the CPU' and sample.get('cpu_percent',100)<85:
            if cpu_busy_since is None:cpu_busy_since=time.monotonic()
            if time.monotonic()-cpu_busy_since<3:
                headroom=policy.model_copy(update={'external_cpu_stop_percent':100})
                reason=pressure_reason(sample,headroom,running=True,cpu_only=True)
        else:cpu_busy_since=None
        if sample.get('application',{}).get('rss_upper_bound_bytes',0)>4*1024**3:reason='The development command reached its 4 GiB memory ceiling'
        if time.monotonic()-last_report>=30:
            print(json.dumps({'development_guard':'SAMPLE','cpu_budget':args.cpu_budget,'affinity':selected,**sample_summary(sample)}),flush=True)
            last_report=time.monotonic()
        if reason:
            quiet=None
            if reason!=last_reason:print(json.dumps({'development_guard':'WAITING','reason':reason,**sample_summary(sample)}),flush=True)
            if process is not None and not stopping:
                os.killpg(process.pid,signal.SIGTERM);stopping=True;stop_at=time.monotonic()
        elif quiet is None:quiet=time.monotonic()
        last_reason=reason
        if process is not None:
            if stopping and time.monotonic()-stop_at>3:
                with contextlib.suppress(ProcessLookupError):os.killpg(process.pid,signal.SIGKILL)
            result=process.poll()
            if result is not None:
                process.wait()
                stop_owned_group(process)
                if not stopping:return result
                print(json.dumps({'development_guard':'YIELDED','response_seconds':time.monotonic()-stop_at}),flush=True)
                process=None;stopping=False;quiet=None
                # After an actual resource yield, leave restart to the caller.
                # Repeated full-suite restarts waste capacity and hide evidence.
                return 75
        elif not reason and quiet is not None and time.monotonic()-quiet>=args.idle_seconds:
            attempts+=1
            print(json.dumps({'development_guard':'STARTING','attempt':attempts,'cpu_budget':args.cpu_budget,'affinity':selected,
                              'start_threshold_percent':policy.external_cpu_start_percent,'stop_threshold_percent':policy.external_cpu_stop_percent,
                              'policy':'SCHED_IDLE, Nice=19, idle I/O',**sample_summary(sample)}),flush=True)
            process=subprocess.Popen(command,start_new_session=True,stdin=subprocess.DEVNULL)
        time.sleep(1)
    if process is not None:
        stop_owned_group(process)
    print(json.dumps({'development_guard':'WAIT_TIMEOUT','not_an_application_test_failure':True}),flush=True)
    return 75


if __name__=='__main__':raise SystemExit(main())
