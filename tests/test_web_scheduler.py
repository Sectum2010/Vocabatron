"""Real owned process termination with simulated admission signals; no network."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid
import pytest
from vocabatron.app.database import encode
from vocabatron.app.resources import estimate
from vocabatron.app.scheduler import Scheduler
from vocabatron.execution import owner_identity
from .test_web_app import library,add_lesson,claim,quiet_sample


def ready(scheduler):
    scheduler.last_snapshot=quiet_sample()
    scheduler.admission.idle_since=time.time()-120
    scheduler.admission.gpu_idle_since=time.time()-120


def test_finite_request_precedes_all_and_busy_wait_keeps_retry_budget(library,monkeypatch):
    lid,_=add_lesson(library)
    def submit(mode):
        return library.submit({'idempotency_key':uuid.uuid4().hex,'targets':[{'lesson_id':lid,'mode':mode,'count':1 if mode=='count' else None}]})['tasks'][0]['id']
    all_id=submit('all');finite_id=submit('count')
    scheduler=Scheduler(library.config,'unused')
    monkeypatch.setattr(scheduler,'resources_for',lambda task:estimate('verify'))
    ready(scheduler);scheduler.last_snapshot['external_cpu_percent']=90
    assert scheduler.claim() is None
    assert library.db.one('SELECT sum(retries) n FROM tasks')['n']==0
    with library.db.transaction() as c:c.execute('UPDATE tasks SET eligible=0')
    ready(scheduler);selected=scheduler.claim()
    assert selected[0]==finite_id
    # The family lock prevents another request from concurrently searching the
    # same source family, even when a second worker slot could be available.
    assert scheduler.claim() is None
    assert library.db.one('SELECT status FROM tasks WHERE id=?',(all_id,))['status']=='WAITING_FOR_RESOURCES'


def test_real_owned_worker_yields_and_fresh_scheduler_waits(library,monkeypatch):
    lid,lesson=add_lesson(library);tid,fence=claim(library,lid)
    directory=library.config.runtime_root/'jobs'/f'{tid}-{fence}';directory.mkdir(parents=True)
    marker=directory/'ready'
    program='import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text("ready"); time.sleep(30)'
    child=subprocess.Popen([sys.executable,'-c',program,str(marker)],start_new_session=True)
    try:
        deadline=time.monotonic()+5
        while not marker.exists() and time.monotonic()<deadline:time.sleep(.01)
        assert marker.exists()
        owner=owner_identity(child.pid);request=estimate('search',words=lesson.words,threads=1)
        with library.db.transaction() as c:
            c.execute('UPDATE tasks SET owner=? WHERE id=?',(encode(owner),tid))
            c.execute('INSERT INTO reservations VALUES(?,?,?,?)',(tid,fence,encode(request),time.time()))
        scheduler=Scheduler(library.config,'unused');ready(scheduler)
        scheduler.active[tid]={'process':child,'fence':fence,'request':request,'directory':directory,'owner':owner,
            'stop_reason':None,'stopping_at':None,'started':time.monotonic()}
        busy=quiet_sample();busy['external_cpu_percent']=90
        monkeypatch.setattr(scheduler.telemetry,'sample',lambda:busy)
        started=time.monotonic();scheduler.tick();child.wait(timeout=3)
        scheduler.tick();latency=time.monotonic()-started
        assert latency<3
        assert not scheduler.active and not library.db.all('SELECT * FROM reservations')
        task=library.db.one('SELECT status,retries FROM tasks WHERE id=?',(tid,))
        assert task=={'status':'PAUSED_FOR_RESOURCES','retries':0}
        restarted=Scheduler(library.config,'unused');restarted.last_snapshot=quiet_sample()
        monkeypatch.setattr(restarted,'resources_for',lambda task:request)
        with library.db.transaction() as c:c.execute('UPDATE tasks SET eligible=0 WHERE id=?',(tid,))
        assert restarted.claim() is None
        assert restarted.admission.idle_since is None
    finally:
        if child.poll() is None:os.killpg(child.pid,signal.SIGKILL)
        child.wait()
