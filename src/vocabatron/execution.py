"""Attempt-scoped lifecycle shared by CLI and a future single local worker."""
from __future__ import annotations
from contextvars import ContextVar
import contextlib
import math
import os
from pathlib import Path
import signal
import threading
import time
import uuid

from .domain import Problem

CURRENT = ContextVar('vocabatron_execution', default=None)
TERMINAL = {'COMPLETE','CANCELLED','INTERRUPTED','INTERNAL_ERROR','RESOURCE_LIMIT','UNKNOWN',
            'INFEASIBLE','MODEL_INVALID','WORKER_FAILED','PUBLISH_FAILED','INPUT_INVALID'}


def owner_identity(pid=None):
    pid = pid or os.getpid()
    try:
        stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
        if stat[0]=='Z':return None
        return {'pid':pid,'start_ticks':stat[19],
                'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    except (OSError,IndexError):
        return None


def checkpoint(*,force_cancel=False):
    context = CURRENT.get()
    if context: context.check(force_cancel=force_cancel)


class Execution:
    def __init__(self, store, task_id, identity, *, total_seconds=1800, stage_seconds=600, progress=None, pdf_limits=None):
        self.store,self.task_id,self.identity=store,task_id,identity
        self.attempt_id=uuid.uuid4().hex
        self.path=Path('tasks')/task_id
        self.attempt=self.path/'attempts'/self.attempt_id
        self.total_seconds,self.stage_seconds=total_seconds,stage_seconds
        if any(not math.isfinite(x) or x<=0 for x in (total_seconds,stage_seconds)):
            raise Problem('INPUT_INVALID','执行预算必须是有限正数')
        from .limits import Limits
        self.pdf_limits=Limits.model_validate((pdf_limits or Limits()).model_dump())
        self.started=self.stage_started=time.monotonic()
        self.stage='STARTING';self.data={};self.terminal=False
        self.event=threading.Event();self.stop=threading.Event();self.lock=threading.RLock()
        self.progress=progress;self.owner=owner_identity();self.owner['nonce']=self.attempt_id
        self.heartbeat_error=None;self.subprocesses=[];self.publication_committed=False
        self.cancel_path=store.path(self.attempt/'cancel.request')
        self._cancel_probe=0.0;self._cancel_seen=False

    def state(self):
        return {'schema_version':2,'task_id':self.task_id,'attempt_id':self.attempt_id,
                'identity':self.identity,'stage':self.stage,'heartbeat':time.time(),
                'owner':self.owner,'terminal':self.terminal,'data':self.data}

    def persist(self):
        state=self.state()
        self.store.json(self.attempt/'state.json',state)
        self.store.json(self.path/'state.json',state)

    def cancelled(self,*,force=False):
        if self.event.is_set():return True
        now=time.monotonic()
        # Avoid repeated path resolution/stat in every heuristic search node.
        # External requests are observed within 50ms; signals stay immediate.
        if force or now-self._cancel_probe>=.05:
            self._cancel_seen=self.cancel_path.exists();self._cancel_probe=now
        return self._cancel_seen

    def check(self,*,force_cancel=False):
        if self.cancelled(force=force_cancel): raise Problem('CANCELLED','当前执行尝试已取消')
        if self.heartbeat_error: raise Problem('WORKER_FAILED','无法持久保存任务心跳')
        if self.budget_exhausted():
            raise Problem('UNKNOWN','执行预算耗尽；未证明无解')

    def budget_exhausted(self):
        now=time.monotonic()
        return now-self.started>self.total_seconds or now-self.stage_started>self.stage_seconds

    def emit(self, stage, value=None):
        with self.lock:
            if self.terminal: return
            self.check();self.stage=stage;self.data=value or {};self.stage_started=time.monotonic()
            self.persist()
        if self.progress:self.progress(stage)

    def finish(self, stage, data=None):
        with self.lock:
            self.stage=stage;self.data=data or {};self.terminal=True
            self.persist()

    def _heartbeat(self):
        while not self.stop.wait(.4):
            with self.lock:
                if self.terminal:return
                try:self.persist()
                except OSError as exc:self.heartbeat_error=type(exc).__name__;return

    def __enter__(self):
        self.token=CURRENT.set(self)
        self.store.json(self.path/'active.json',{'attempt_id':self.attempt_id,'identity':self.identity})
        self.persist()
        self.thread=threading.Thread(target=self._heartbeat,name='vocabatron-heartbeat',daemon=True);self.thread.start()
        self.old_signals={}
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM,signal.SIGINT):
                self.old_signals[sig]=signal.getsignal(sig)
                signal.signal(sig,lambda *args:self.event.set())
        return self

    def __exit__(self, typ, exc, tb):
        self.stop.set();self.thread.join(timeout=2)
        try:
            if not self.terminal:
                code=exc.code if isinstance(exc,Problem) else 'CANCELLED' if typ is KeyboardInterrupt else 'INTERNAL_ERROR'
                # A durable complete set wins even if the final state write failed.
                if self.publication_committed or self.store.path(Path('results')/self.task_id/'manifest.json').is_file():code='COMPLETE'
                with contextlib.suppress(OSError):self.finish(code,{'error_type':typ.__name__ if typ else None})
        finally:
            for sig,previous in self.old_signals.items():signal.signal(sig,previous)
            CURRENT.reset(self.token)


def cancel_attempt(store,task_id,attempt_id=None):
    from .storage import identifier
    task_id=identifier(task_id)
    with store.commit_lock():
        if store.path(Path('results')/task_id/'manifest.json').is_file():return {'status':'COMPLETE','task_id':task_id}
        active=store.read(Path('tasks')/task_id/'active.json')
        target=identifier(attempt_id or active['attempt_id'])
        if target!=active['attempt_id']:raise Problem('ATTEMPT_CHANGED','取消请求指向旧的执行尝试')
        store.write(Path('tasks')/task_id/'attempts'/target/'cancel.request',b'cancel\n')
        current=CURRENT.get()
        if current and current.store.root==store.root and current.attempt_id==target:current.event.set()
        return {'status':'CANCEL_REQUESTED','task_id':task_id,'attempt_id':target}
