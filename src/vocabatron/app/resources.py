"""Conservative host telemetry and atomic admission. No control of other programs."""
from __future__ import annotations
from collections import deque
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request
import psutil
from ..domain import Problem
from .database import encode, rows

GIB=1024**3


def idle_priority():
    """Only this process and its future descendants; never other applications."""
    if os.getpriority(os.PRIO_PROCESS,0)<19:os.nice(19-os.getpriority(os.PRIO_PROCESS,0))
    os.sched_setscheduler(0,os.SCHED_IDLE,os.sched_param(0))
    try:psutil.Process().ionice(psutil.IOPRIO_CLASS_IDLE)
    except (psutil.AccessDenied,AttributeError):pass


def pressure(path):
    result={}
    try:
        for line in Path(path).read_text().splitlines():
            name,*pairs=line.split();result[name]={k:float(v) for k,v in (p.split('=') for p in pairs)}
    except (OSError,ValueError):return None
    return result


def cgroup_metrics():
    try:
        group=next(x.split(':',2)[2] for x in Path('/proc/self/cgroup').read_text().splitlines() if x.startswith('0::'))
        root=Path('/sys/fs/cgroup')/group.lstrip('/')
        names=('cpu.stat','cpu.max','cpu.weight','cpu.idle','memory.current','memory.peak',
               'memory.events','memory.high','memory.max','memory.low','memory.min','memory.swap.max','io.stat','io.weight','io.max','pids.max','pids.current')
        result={name:(root/name).read_text().strip() if (root/name).exists() else None for name in names}
        result['scope']=group
        # Only a dedicated Vocabatron cgroup can be attributed to this application.
        result['dedicated']='vocabatron' in group
        if 'vocabatron.slice' in root.parts:
            slice_root=Path(*root.parts[:root.parts.index('vocabatron.slice')+1])
            result['application_slice']={name:(slice_root/name).read_text().strip() if (slice_root/name).exists() else None for name in names}
            result['application_slice']['scope']=str(slice_root.relative_to('/sys/fs/cgroup'))
        return result
    except (OSError,StopIteration):return None


def enforcement(cgroup):
    """Report the strictest live parent/worker limit, not a template value."""
    if not cgroup or not cgroup.get('dedicated'):return None
    groups=[cgroup]
    if cgroup.get('application_slice'):groups.append(cgroup['application_slice'])
    result={}
    for key in ('memory.high','memory.max','memory.swap.max'):
        values=[int(g[key]) for g in groups if str(g.get(key,'')).isdigit()]
        result[key]=min(values) if values else None
    quotas=[];known=True
    for group in groups:
        parts=(group.get('cpu.max') or '').split()
        if len(parts)!=2:known=False;continue
        try:
            if int(parts[1])<=0:known=False
            elif parts[0]!='max':quotas.append(int(parts[0])/int(parts[1]))
        except ValueError:known=False
    result.update(cpu_quota_cores=min(quotas) if quotas else None,cpu_quota_known=known,
        io_limits=[g.get('io.max') for g in groups],memory_scope='Minimum of the live worker and application slice limits')
    return result


class Telemetry:
    def __init__(self, disk_root, owned_pids=None, *, model_probe=True):
        self.disk_root=Path(disk_root);self.owned_pids=owned_pids or (lambda: [os.getpid()])
        self.model_probe=model_probe;self.previous=None;self.previous_own=None
        self.previous_cgroup=None;self.previous_throttle=None
        self.gpu=None;self.gpu_at=0;self.ollama=None;self.ollama_at=0
        self.ring=deque(maxlen=120)

    def sample(self):
        now=time.time();clock=time.monotonic()
        fields=list(map(int,Path('/proc/stat').read_text().splitlines()[0].split()[1:9]))
        cpu=None
        if self.previous:
            delta=[b-a for a,b in zip(self.previous,fields)];total=sum(delta)
            if total>0:cpu=100*(total-delta[3]-delta[4])/total
        self.previous=fields
        processes={};owned=set(self.owned_pids());cg=cgroup_metrics()
        if cg and cg.get('application_slice'):
            root=Path('/sys/fs/cgroup')/cg['application_slice']['scope']
            for name in ('vocabatron-worker.service','vocabatron-web.service'):
                try:owned.update(int(p) for p in (root/name/'cgroup.procs').read_text().split())
                except (OSError,ValueError):pass
        for pid in owned:
            try:
                parent=psutil.Process(pid)
                for proc in [parent,*parent.children(recursive=True)]:processes[proc.pid]=proc
            except (psutil.NoSuchProcess,psutil.AccessDenied):continue
        own_seconds=0;own_rss=0;max_rss=0;threads=0;own_inodes=set()
        for proc in processes.values():
            try:
                usage=proc.cpu_times();own_seconds+=usage.user+usage.system+usage.children_user+usage.children_system
                rss=proc.memory_info().rss;own_rss+=rss;max_rss=max(max_rss,rss);threads+=proc.num_threads()
                for fd in Path(f'/proc/{proc.pid}/fd').iterdir():
                    try:
                        value=os.readlink(fd)
                        if value.startswith('socket:['):own_inodes.add(value[8:-1])
                    except OSError:pass
            except (psutil.NoSuchProcess,psutil.AccessDenied,OSError):continue
        own_cpu=0.0
        if self.previous_own:
            elapsed=clock-self.previous_own[0]
            own_cpu=max(0,(own_seconds-self.previous_own[1])/elapsed/(os.cpu_count() or 1)*100)
        self.previous_own=(clock,own_seconds)
        group_memory=None;throttle=None
        if cg and cg.get('application_slice'):
            group=cg['application_slice']
            try:
                statistics=dict(line.split() for line in group['cpu.stat'].splitlines())
                seconds=int(statistics['usage_usec'])/1e6
                usec=int(statistics.get('throttled_usec',0))
                throttle={'nr_throttled':int(statistics.get('nr_throttled',0)),'throttled_usec':usec,
                          'recent_throttled_percent':None if self.previous_throttle is None else max(0,(usec-self.previous_throttle[1])/(clock-self.previous_throttle[0])/10000)}
                self.previous_throttle=(clock,usec)
                if self.previous_cgroup:
                    own_cpu=max(0,(seconds-self.previous_cgroup[1])/(clock-self.previous_cgroup[0])/(os.cpu_count() or 1)*100)
                self.previous_cgroup=(clock,seconds)
                group_memory=int(group['memory.current'])
            except (ValueError,KeyError,TypeError):pass
        memory={}
        for line in Path('/proc/meminfo').read_text().splitlines():
            key,value=line.split(':',1)
            if key in ('MemTotal','MemAvailable','SwapTotal','SwapFree'):memory[key]=int(value.split()[0])*1024
        if now-self.gpu_at>=5:
            self.gpu_at=now
            try:
                query=subprocess.run(['/usr/bin/nvidia-smi','--query-gpu=utilization.gpu,temperature.gpu','--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=2,check=True)
                values=query.stdout.strip().splitlines()[0].split(',')
                apps=subprocess.run(['/usr/bin/nvidia-smi','--query-compute-apps=pid','--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=2,check=True)
                compute=[int(p.strip()) for p in apps.stdout.splitlines() if p.strip().isdigit()]
                categories=[]
                for pid in compute:
                    try:comm=Path(f'/proc/{pid}/comm').read_text().strip()
                    except OSError:comm='unknown'
                    categories.append('shared_model' if comm.startswith('ollama') else 'other_compute')
                self.gpu={'status':'Available','utilization_percent':float(values[0]),'temperature_c':float(values[1]),
                          'compute_categories':categories,'memory':'Unified host memory; dedicated VRAM unavailable','sampled_at':now}
            except (OSError,ValueError,subprocess.SubprocessError,IndexError):self.gpu={'status':'Unavailable','sampled_at':now}
        own_client_ports=set();server_peers=[]
        try:
            for filename in ('/proc/net/tcp','/proc/net/tcp6'):
                for line in Path(filename).read_text().splitlines()[1:]:
                    parts=line.split()
                    if parts[3]!='01':continue
                    local=int(parts[1].split(':')[1],16);remote=int(parts[2].split(':')[1],16)
                    if remote==11434 and parts[9] in own_inodes:own_client_ports.add(local)
                    if local==11434:server_peers.append(remote)
            external_connections=sum(port not in own_client_ports for port in server_peers)
        except (OSError,ValueError,IndexError):external_connections=None
        if self.model_probe and now-self.ollama_at>=10:
            self.ollama_at=now
            try:
                opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open('http://127.0.0.1:11434/api/ps',timeout=1) as response:
                    value=json.loads(response.read(128*1024))
                models=value.get('models',[])
                self.ollama={'status':'Available','selected_model_loaded':any(m.get('name')=='gemma4:31b' for m in models),
                             'other_models_loaded':any(m.get('name')!='gemma4:31b' for m in models),'sampled_at':now}
            except (OSError,ValueError):self.ollama={'status':'Unavailable','sampled_at':now}
        disk=shutil.disk_usage(self.disk_root)
        result={'sampled_at':now,'logical_cpus':os.cpu_count(),'cpu_percent':cpu,'external_cpu_percent':None if cpu is None else max(0,cpu-own_cpu),
                'application_cpu_percent':own_cpu,'memory':memory,'disk_free_bytes':disk.free,
                'pressure':{key:pressure('/proc/pressure/'+key) for key in ('cpu','memory','io')},
                'application':{'rss_upper_bound_bytes':own_rss,'rss_max_process_bytes':max_rss,'cgroup_memory_bytes':group_memory,
                               'rss_scope':'sum of owned process RSS; shared pages may be counted more than once',
                               'threads':threads,'processes':len(processes)},'cgroup':cg,'gpu':self.gpu,
                'ollama':self.ollama,'external_model_connections':external_connections}
        self.ring.append({k:result[k] for k in ('sampled_at','cpu_percent','external_cpu_percent')})
        recent=[s['external_cpu_percent'] for s in self.ring if now-s['sampled_at']<=5 and s['external_cpu_percent'] is not None]
        result['external_cpu_recent_percent']=sum(recent)/len(recent) if recent else None
        result['application_quota_throttling']=throttle
        return result


def estimate(phase, *, words=(), pages=1, history=0, threads=1, model_loaded=False):
    placements=sum(40*(21-len(w.letters)) for w in words)
    shared=sum(sum(a==b for a in x.letters for b in y.letters) for i,x in enumerate(words) for y in words[i+1:])
    features={'words':len(words),'placements':placements,'matching_letter_pairs':shared,'pages':pages,'history':history,'threads':threads}
    if phase=='search':memory=int(384*1024**2+placements*170_000+shared*20_000+history*(16*1024+len(words)*2048)+threads*128*1024**2)
    elif phase=='inference':memory=256*1024**2
    elif phase=='import':memory=3*GIB+pages*16*1024**2  # Bounded OCR tree plus retained document evidence.
    elif phase=='verify':memory=768*1024**2
    else:memory=256*1024**2
    extra=(2*GIB if model_loaded else 26*GIB) if phase=='inference' else 0
    return {'phase':phase,'cpu':threads if phase=='search' else 1,'memory_bytes':memory,
            'host_extra_bytes':extra,'growth_bytes':memory,'documents':0 if phase in ('inference','search') else 1,
            'gpu':int(phase=='inference'),'disk_bytes':max(128*1024**2,pages*8*1024**2),
            'features':features,'confidence':'conservative upper bound; not a completion-time prediction'}


def spare_threads(snapshot,policy):
    if not snapshot or time.time()-snapshot.get('sampled_at',0)>policy.stale_seconds:return 0
    external=snapshot.get('external_cpu_percent')
    if external is None:return 0
    external=max(external,snapshot.get('external_cpu_recent_percent') or external)
    cores=snapshot.get('logical_cpus') or os.cpu_count() or 1
    return max(0,min(policy.cpu_threads,cores-math.ceil(cores*external/100)-policy.reserved_cpu_cores))


def pressure_reason(snapshot, policy, *, running=False, inference=False, now=None,cpu_only=False):
    now=now or time.time()
    if not snapshot or now-snapshot.get('sampled_at',0)>policy.stale_seconds:return None if running else 'Resource telemetry is stale'
    cpu=snapshot.get('external_cpu_percent')
    if cpu is None:return None if running else 'Collecting a stable resource sample'
    if snapshot['memory'].get('MemAvailable',0)<policy.host_memory_reserve_bytes:return 'Keeping memory available for other applications'
    memory_pressure=(snapshot.get('pressure',{}).get('memory') or {}).get('some',{}).get('avg10')
    if memory_pressure is not None and memory_pressure>policy.memory_psi_percent:return 'Host memory pressure is elevated'
    if snapshot.get('disk_free_bytes',0)<policy.disk_reserve_bytes:return 'Keeping the disk safety reserve'
    threshold=policy.external_cpu_stop_percent if running else policy.external_cpu_start_percent
    if cpu>threshold:return 'Other applications are using the CPU'
    for kind,limit in (('cpu',policy.cpu_psi_percent),('memory',policy.memory_psi_percent),('io',policy.io_psi_percent)):
        data=snapshot.get('pressure',{}).get(kind)
        if not data or 'some' not in data:
            if running:continue
            return f'{kind.upper()} pressure telemetry is unavailable'
        # Host CPU PSI includes our own idle-priority/affinity-limited workers.
        # It cannot be labelled external contention while ample CPU capacity is
        # free. Memory and I/O pressure always remain independent stop signals.
        if kind=='cpu' and cpu<policy.cpu_pressure_min_external_percent:continue
        if kind in ('cpu','io') and running:continue
        if data['some']['avg10']>limit:return f'Host {kind.upper()} pressure is elevated; waiting to start the next step'
    gpu=snapshot.get('gpu') or {}
    # A resident GPU context is not evidence that CPU-only work would contend.
    # Inference remains blocked by that context even while it is idle. CPU work
    # yields on actual GPU activity or missing utilization for a resident context.
    if (inference or not (cpu_only or policy.cpu_work_during_gpu_activity)) and 'other_compute' in gpu.get('compute_categories',[]) and (inference or gpu.get('utilization_percent',100)>policy.gpu_idle_percent):
        return 'Another GPU compute workload has priority'
    if inference:
        if gpu.get('status')!='Available' or now-gpu.get('sampled_at',0)>policy.stale_seconds+5:return 'Waiting for reliable GPU telemetry'
        # Own inference may use the GPU. Other HTTP requests and unknown compute
        # activity remain blockers; shared Ollama is never subtracted wholesale.
        if not running and gpu.get('utilization_percent',100)>policy.gpu_idle_percent:return 'Waiting for a safe inference window'
        if snapshot.get('external_model_connections')!=0:return 'Shared inference activity is busy or unknown'
        model=snapshot.get('ollama') or {}
        if model.get('status')!='Available' or now-model.get('sampled_at',0)>20:return 'Waiting for the local model service'
        if model.get('other_models_loaded'):return 'Another loaded model may be in use'
    return None


class Admission:
    def __init__(self, database, policy):
        self.db,self.policy=database,policy;self.idle_since=None;self.gpu_idle_since=None

    def observe(self, snapshot):
        now=time.time()
        if pressure_reason(snapshot,self.policy):self.idle_since=None
        elif self.idle_since is None:self.idle_since=now
        if pressure_reason(snapshot,self.policy,inference=True):self.gpu_idle_since=None
        elif self.gpu_idle_since is None:self.gpu_idle_since=now

    def reserve(self,c,task_id,fence,request,snapshot):
        reason=pressure_reason(snapshot,self.policy,inference=bool(request['gpu']))
        if reason:return reason
        now=time.time()
        if self.idle_since is None or now-self.idle_since<self.policy.idle_window_seconds:return 'Waiting for a stable idle window'
        if request['gpu'] and (self.gpu_idle_since is None or now-self.gpu_idle_since<self.policy.gpu_idle_window_seconds):return 'Waiting for a safe inference window'
        if rows(c,'SELECT id FROM holds WHERE until>?',(now,)):return 'An external workload has reserved this resource window'
        held=[json.loads(r['resources']) for r in rows(c,'SELECT resources FROM reservations')]
        if sum(r['cpu'] for r in held)+request['cpu']>spare_threads(snapshot,self.policy):return 'Waiting for spare CPU cores after the host reserve'
        if sum(r['documents'] for r in held)+request['documents']>self.policy.document_slots:return 'The document worker slot is reserved'
        if sum(r['gpu'] for r in held)+request['gpu']>1:return 'The local inference slot is reserved'
        if sum(r['memory_bytes'] for r in held)+request['memory_bytes']>self.policy.memory_max_bytes:return 'The application memory budget is reserved'
        # Host MemAvailable already includes resident models. Reserve only the
        # new model allocation and unconsumed application growth, never swap.
        application=snapshot.get('application',{})
        own=application.get('cgroup_memory_bytes')
        if own is None:own=application.get('rss_max_process_bytes',0)
        remaining_growth=max(0,sum(r['memory_bytes'] for r in held)-own)
        needed=request['memory_bytes']+request['host_extra_bytes']+remaining_growth+sum(r.get('host_extra_bytes',0) for r in held)
        if snapshot['memory']['MemAvailable']-needed<self.policy.host_memory_reserve_bytes:return 'Keeping host memory and growth headroom for other applications'
        if snapshot['disk_free_bytes']-sum(r['disk_bytes'] for r in held)-request['disk_bytes']<self.policy.disk_reserve_bytes:return 'The disk safety reserve would be exceeded'
        c.execute('INSERT INTO reservations VALUES(?,?,?,?)',(task_id,fence,encode(request),now))
        return None
