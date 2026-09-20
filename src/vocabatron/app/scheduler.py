"""Persistent, fair scheduler with fenced ownership and bounded owned processes."""
from __future__ import annotations
import contextlib
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid
import re
import shutil
from ..domain import Lesson,Problem
from ..execution import owner_identity
from ..storage import private_mkdir
from .database import one,rows,encode
from .library import Library
from .resources import Telemetry,Admission,estimate,pressure_reason,idle_priority

CLAIMABLE=('QUEUED','WAITING_FOR_RESOURCES','PAUSED_FOR_RESOURCES','RETRY_WAIT','PARTIALLY_COMPLETED','INTERRUPTED')


class Scheduler:
    def __init__(self,config,config_path):
        self.config,self.config_path=config,str(config_path)
        self.library=Library(config);self.db=self.library.db
        self.telemetry=Telemetry(config.data_root,lambda:[os.getpid()])
        self.admission=Admission(self.db,config.resources)
        self.active={};self.stop=False;self.started=time.monotonic();self.last_snapshot=None

    def clean_render_staging(self,task_id,fence):
        if not all(isinstance(v,str) and re.fullmatch('[a-f0-9]{32}',v) for v in (task_id,fence)):return
        for path in (self.config.runtime_root/'renders').glob(f'{task_id}-{fence}-*'):
            if path.is_dir() and not path.is_symlink() and path.resolve()==path:shutil.rmtree(path)

    def recover(self):
        """A late heartbeat alone is not permission to duplicate a live owner."""
        for task in self.db.all("SELECT * FROM tasks WHERE status IN ('RUNNING','PAUSING')"):
            if task['id'] in self.active:continue
            try:owner=json.loads(task['owner']) if task['owner'] else None
            except ValueError:owner=None
            actual=owner_identity(owner['pid']) if owner else None
            if actual and all(actual.get(k)==owner.get(k) for k in ('pid','start_ticks','boot_id')):
                continue
            with self.db.transaction() as c:
                current=one(c,'SELECT * FROM tasks WHERE id=?',(task['id'],))
                if current['fence']!=task['fence']:continue
                status='CANCELLED' if current['intent']=='cancel' else 'PAUSED_BY_USER' if current['intent']=='pause' else 'INTERRUPTED'
                c.execute('UPDATE tasks SET status=?,stage=?,fence=NULL,owner=NULL,lease_until=NULL,updated=? WHERE id=?',
                          (status,'Waiting to resume safely',time.time(),task['id']))
                c.execute('DELETE FROM family_locks WHERE task_id=? AND fence=?',(task['id'],task['fence']))
                c.execute('DELETE FROM reservations WHERE task_id=? AND fence=?',(task['id'],task['fence']))
                self.db.event(c,'task',{'id':task['id'],'status':status},task['id'])
            self.clean_render_staging(task['id'],task['fence'])

    def resources_for(self,task):
        inputs=json.loads(task['input_json'])
        if task['kind'] in ('restore','verify'):return estimate('verify' if task['kind']=='verify' else 'export')
        checkpoint=self.db.one('SELECT value FROM task_checkpoints WHERE task_id=?',(task['id'],))
        saved=json.loads(checkpoint['value']) if checkpoint else {}
        if task['kind']=='import':
            pages=saved.get('document_info',{}).get('pages',self.config.document_limits.pages)
            return estimate('import',pages=pages)
        reference=saved.get('frozen_ref') or inputs.get('frozen_ref')
        live=self.db.one('SELECT frozen_ref FROM lessons WHERE id=?',(task['lesson_id'],))
        if task['kind']=='prepare' and (live or {}).get('frozen_ref'):return estimate('verify')
        if task['kind']=='prepare' or (not reference and not (live or {}).get('frozen_ref')):
            loaded=bool(((self.last_snapshot or {}).get('ollama') or {}).get('selected_model_loaded'))
            return estimate('inference',model_loaded=loaded)
        lesson=Lesson.model_validate(self.library.objects.read_json(inputs['lesson_json']))
        count=self.db.one('SELECT COUNT(*) n FROM structures s JOIN families f ON f.answer_set_id=s.answer_set_id WHERE f.id=?',(task['family_id'],))['n']
        pending=self.db.one("SELECT id FROM artifacts WHERE task_id=? AND state!='AVAILABLE' LIMIT 1",(task['id'],))
        if pending or not saved.get('profile_ref'):return estimate('verify')
        if reference:
            from .archive import delivery_id
            frozen,_=self.library.frozen(reference)
            delivery=delivery_id(task['lesson_id'],frozen,inputs['template_sha256'])
            abandoned=self.db.one("SELECT a.id FROM artifacts a JOIN tasks t ON t.id=a.task_id WHERE a.lesson_id=? AND a.delivery_id=? AND a.state!='AVAILABLE' AND t.status IN ('CANCELLED','FAILED','NEEDS_ATTENTION') LIMIT 1",(task['lesson_id'],delivery))
            if abandoned:return estimate('verify')
        prefs=self.library.preferences()
        predicted=estimate('search',words=lesson.words,history=count,threads=min(prefs['threads_per_search'],self.config.resources.cpu_threads))
        # A timed-out search still provides a measured memory peak. It must not
        # count as a completed variant, but it can raise the safety estimate.
        samples=self.db.all("SELECT estimate,actual FROM resource_samples WHERE phase='search' ORDER BY id DESC LIMIT 32")
        ratios=[]
        for sample in samples:
            old=json.loads(sample['estimate']);actual=json.loads(sample['actual'])
            if old.get('memory_bytes') and actual.get('peak_rss_kib'):
                ratios.append(actual['peak_rss_kib']*1024/old['memory_bytes'])
        predicted['observed_safety_factor']=max([1,*[x*1.2 for x in ratios]])
        predicted['memory_bytes']=int(predicted['memory_bytes']*predicted['observed_safety_factor'])
        return predicted

    def claim(self):
        prefs=self.library.preferences()
        # Grow slowly after a sustained quiet window, shrink immediately on pressure.
        desired=min(prefs['search_slots'],self.config.resources.search_slots)
        slots=desired if self.admission.idle_since and time.time()-self.admission.idle_since>60 else 1
        if len(self.active)>=slots:return None
        tasks=self.db.all('SELECT * FROM tasks WHERE status IN ('+','.join('?' for _ in CLAIMABLE)+") AND intent='run' AND eligible<=? ORDER BY priority,updated,created LIMIT 32",(*CLAIMABLE,time.time()))
        for candidate in tasks:
            try:request=self.resources_for(candidate)
            except (Problem,ValueError,KeyError,OSError) as exc:
                # One damaged input must not restart the scheduler or prevent
                # independent lessons from making progress.
                code=exc.code if isinstance(exc,Problem) else 'TASK_INPUT_INVALID'
                with self.db.transaction() as c:
                    c.execute("UPDATE tasks SET status='NEEDS_ATTENTION',stage='Saved input needs attention',error_code=?,detail=?,updated=? WHERE id=? AND status=?",
                              (code,encode({'message':'The saved input could not be verified. Other tasks can continue.'}),time.time(),candidate['id'],candidate['status']))
                continue
            fence=uuid.uuid4().hex
            with self.db.transaction() as c:
                task=one(c,'SELECT * FROM tasks WHERE id=?',(candidate['id'],))
                if task['status'] not in CLAIMABLE or task['intent']!='run':continue
                if task['family_id'] and one(c,'SELECT task_id FROM family_locks WHERE family_id=?',(task['family_id'],)):
                    c.execute('UPDATE tasks SET eligible=? WHERE id=?',(time.time()+3,task['id']))
                    continue
                reason=self.admission.reserve(c,task['id'],fence,request,self.last_snapshot)
                if reason:
                    c.execute('UPDATE tasks SET eligible=? WHERE id=?',(time.time()+3,task['id']))
                    if task['status']!='WAITING_FOR_RESOURCES' or task['detail']!=encode({'reason':reason}):
                        c.execute("UPDATE tasks SET status='WAITING_FOR_RESOURCES',stage='Waiting for system resources',detail=? WHERE id=?",(encode({'reason':reason}),task['id']))
                    continue
                owner=owner_identity()
                c.execute("UPDATE tasks SET status='RUNNING',stage='Starting safely',fence=?,owner=?,attempt=attempt+1,lease_until=?,updated=?,error_code=NULL WHERE id=?",(fence,encode(owner),time.time()+15,time.time(),task['id']))
                if task['family_id']:c.execute('INSERT INTO family_locks VALUES(?,?,?,?)',(task['family_id'],task['id'],fence,encode(owner)))
                self.db.event(c,'task',{'id':task['id'],'status':'RUNNING'},task['id'])
                return task['id'],fence,request
        return None

    def launch(self,claim):
        task_id,fence,request=claim
        directory=self.config.runtime_root/'jobs'/f'{task_id}-{fence}';private_mkdir(directory)
        log=open(directory/'worker.log','xb',buffering=0);os.chmod(directory/'worker.log',0o600)
        env={**os.environ,'OPENBLAS_NUM_THREADS':'1','OMP_NUM_THREADS':'1','PYTHONUNBUFFERED':'1'}
        try:
            process=subprocess.Popen([sys.executable,'-m','vocabatron.app.job_process',self.config_path,task_id,fence,str(os.getpid())],
                cwd=self.config.code_root,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True,close_fds=True)
        except OSError:
            log.close()
            with self.db.transaction() as c:
                c.execute("UPDATE tasks SET status='RETRY_WAIT',stage='Worker could not start',fence=NULL,owner=NULL,eligible=? WHERE id=? AND fence=?",(time.time()+30,task_id,fence))
                c.execute('DELETE FROM reservations WHERE task_id=? AND fence=?',(task_id,fence))
                c.execute('DELETE FROM family_locks WHERE task_id=? AND fence=?',(task_id,fence))
            return
        log.close();owner=owner_identity(process.pid)
        with self.db.transaction() as c:
            c.execute('UPDATE tasks SET owner=? WHERE id=? AND fence=?',(encode(owner),task_id,fence))
            c.execute('UPDATE family_locks SET owner=? WHERE task_id=? AND fence=?',(encode(owner),task_id,fence))
        self.active[task_id]={'process':process,'fence':fence,'request':request,'directory':directory,
                              'owner':owner,'stop_reason':None,'stopping_at':None,'started':time.monotonic()}

    def stop_owned(self,task_id,reason):
        active=self.active[task_id]
        if active['stopping_at'] is not None:return
        active['stop_reason']=reason;active['stopping_at']=time.monotonic()
        with self.db.transaction() as c:
            c.execute("UPDATE tasks SET status='PAUSING',stage='Saving progress and releasing resources' WHERE id=? AND fence=? AND status='RUNNING'",(task_id,active['fence']))
        if owner_identity(active['process'].pid)==active['owner']:
            with contextlib.suppress(ProcessLookupError):os.killpg(active['process'].pid,signal.SIGTERM)

    def collect(self,task_id):
        active=self.active[task_id];process=active['process'];process.wait()
        path=active['directory']/'result.json'
        result={'status':'ERROR','code':'RESOURCE_LIMIT' if process.returncode in (-9,-24,-25) else 'WORKER_FAILED'}
        if path.is_file() and not path.is_symlink() and path.stat().st_size<1024*1024:
            try:result=json.loads(path.read_bytes())
            except ValueError:pass
        with self.db.transaction() as c:
            task=one(c,'SELECT * FROM tasks WHERE id=?',(task_id,))
            if task['fence']==active['fence']:
                code=result.get('code');eligible=0;retries=task['retries']
                if task['target_type']=='count' and task['target_count'] is not None and task['completed']>=task['target_count']:
                    status='COMPLETED'
                elif task['intent']=='cancel':status='CANCELLED'
                elif task['intent']=='pause':status='PAUSED_BY_USER'
                elif active['stop_reason']:
                    status=active['stop_reason'];eligible=time.time()+5
                elif result.get('status') in ('COMPLETED','PARTIALLY_COMPLETED','QUEUED','EXHAUSTED','NEEDS_ATTENTION'):
                    status=result['status']
                elif code in ('UNKNOWN','PAUSED_FOR_RESOURCES','RESOURCE_LIMIT','ATTEMPT_EXPIRED'):
                    status='PAUSED_FOR_RESOURCES' if code in ('PAUSED_FOR_RESOURCES','RESOURCE_LIMIT') else 'QUEUED'
                    eligible=time.time()+5
                elif code in ('OLLAMA_UNAVAILABLE','MODEL_REQUEST_TIMEOUT') and retries<2:
                    retries+=1;status='RETRY_WAIT';eligible=time.time()+min(5*2**retries,60)
                elif code=='PENDING_VARIANTS':status='RETRY_WAIT';eligible=time.time()+30
                else:status='FAILED' if code in ('WORKER_FAILED','INTERNAL_ERROR') else 'NEEDS_ATTENTION'
                detail={'message':result.get('message'),'error_code':code}
                if status=='EXHAUSTED':detail={'message':f'Requested {task["target_count"]}, generated {task["completed"]}. All remaining variants have been exhausted.' if task['target_type']=='count' else 'All valid variants for this lesson have already been generated. View saved variants.'}
                c.execute('UPDATE tasks SET status=?,stage=?,detail=?,eligible=?,retries=?,error_code=?,owner=NULL,lease_until=NULL,updated=? WHERE id=?',
                    (status,status.replace('_',' ').capitalize(),encode(detail),eligible,retries,code,time.time(),task_id))
                c.execute('DELETE FROM reservations WHERE task_id=? AND fence=?',(task_id,active['fence']))
                c.execute('DELETE FROM family_locks WHERE task_id=? AND fence=?',(task_id,active['fence']))
                self.db.event(c,'task',{'id':task_id,'status':status,'completed':task['completed']},task_id)
                request=active['request']
                c.execute('INSERT INTO resource_samples(phase,features,estimate,actual,completed,created) VALUES(?,?,?,?,?,?)',
                          (request['phase'],encode(request['features']),encode(request),encode(result),int(result.get('status')!='ERROR'),time.time()))
                c.execute('DELETE FROM resource_samples WHERE id < (SELECT COALESCE(MAX(id),0)-2000 FROM resource_samples)')
        self.clean_render_staging(task_id,active['fence'])
        del self.active[task_id]

    def tick(self):
        self.recover()
        self.last_snapshot=self.telemetry.sample();self.admission.observe(self.last_snapshot)
        holds=self.db.all('SELECT id FROM holds WHERE until>?',(time.time(),))
        if holds:
            self.admission.idle_since=None;self.admission.gpu_idle_since=None
        for task_id,active in list(self.active.items()):
            task=self.db.one('SELECT intent FROM tasks WHERE id=?',(task_id,))
            reason=pressure_reason(self.last_snapshot,self.config.resources,running=True,inference=bool(active['request']['gpu']))
            if task['intent']!='run':self.stop_owned(task_id,'CANCELLED' if task['intent']=='cancel' else 'PAUSED_BY_USER')
            elif reason or holds:self.stop_owned(task_id,'PAUSED_FOR_RESOURCES')
            elif time.monotonic()-active['started']>950:self.stop_owned(task_id,'PAUSED_FOR_RESOURCES')
            if active['stopping_at'] is not None and time.monotonic()-active['stopping_at']>3:
                if owner_identity(active['process'].pid)==active['owner']:
                    with contextlib.suppress(ProcessLookupError):os.killpg(active['process'].pid,signal.SIGKILL)
            if active['process'].poll() is not None:self.collect(task_id)
        with self.db.transaction() as c:
            snapshot={**self.last_snapshot,'reservations':[json.loads(r['resources']) for r in rows(c,'SELECT resources FROM reservations')],
                      'active_slots':len(self.active),'policy':self.config.resources.model_dump(),
                      'policy_statement':'Other applications always have priority. Shared GPU inference cannot be instantly preempted by client priority.'}
            c.execute('INSERT INTO telemetry VALUES(1,?,?) ON CONFLICT(singleton) DO UPDATE SET value=excluded.value,updated=excluded.updated',(encode(snapshot),time.time()))
            if holds and not self.active:
                # A closed HTTP connection alone cannot acknowledge a GPU hold.
                quiet=not pressure_reason(snapshot,self.config.resources,inference=True)
                if quiet:c.execute('UPDATE holds SET acknowledged=1 WHERE until>?',(time.time(),))
            c.execute('DELETE FROM holds WHERE until<?',(time.time()-86400,))
        if not self.stop:
            claim=self.claim()
            if claim:self.launch(claim)

    def run(self):
        idle_priority();self.library.initialize();private_mkdir(self.config.runtime_root)
        lock=os.open(self.config.runtime_root/'scheduler.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:setattr(self,'stop',True))
            self.recover()
            while not self.stop or self.active:
                if self.stop:
                    for task_id in self.active:self.stop_owned(task_id,'INTERRUPTED')
                self.tick();time.sleep(self.config.resources.sample_seconds)
        finally:os.close(lock)
