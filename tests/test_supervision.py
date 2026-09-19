"""Real process termination, bounded streams, finite resources and later recovery."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest
from pydantic import ValidationError
from reportlab.pdfgen import canvas

from vocabatron.domain import Problem
from vocabatron.supervisor import Limits,run_job
from vocabatron import supervisor,services
from vocabatron.execution import Execution,checkpoint,cancel_attempt,owner_identity
from vocabatron.storage import PrivateStore
from .helpers import synthetic_handout


def launcher(tmp_path,monkeypatch,body):
    path=tmp_path/'synthetic_worker.py'
    path.write_text('import os,sys,time,signal,json,subprocess,resource\nfrom pathlib import Path\njob=Path(sys.argv[1])\nlimits=json.loads(sys.argv[2])\n'+body)
    monkeypatch.setattr(supervisor,'_LAUNCHER',path)
    return path


@pytest.mark.parametrize('kind',['hang','output','temporary','cpu','memory','fds','crash'])
def test_supervised_real_faults_are_bounded(tmp_path,monkeypatch,kind):
    bodies={
        'hang':'time.sleep(10)\n',
        'output':'os.write(1,b"x"*10000)\ntime.sleep(10)\n',
        'temporary':'(job/"large").write_bytes(b"x"*10000)\ntime.sleep(10)\n',
        'cpu':'resource.setrlimit(resource.RLIMIT_CPU,(1,1))\nwhile True: pass\n',
        'memory':'resource.setrlimit(resource.RLIMIT_AS,(64*1024**2,64*1024**2))\na=bytearray(128*1024**2)\n',
        'fds':'resource.setrlimit(resource.RLIMIT_NOFILE,(16,16))\na=[open(job/"request.json") for _ in range(40)]\n',
        'crash':'os._exit(17)\n'}
    launcher(tmp_path,monkeypatch,bodies[kind]);started=time.monotonic()
    limits=Limits(wall_seconds=2 if kind=='cpu' else .4,stream_bytes=1024,temporary_bytes=8000)
    with pytest.raises(Problem) as e:run_job('inspect',{},limits=limits)
    assert e.value.code in {'RESOURCE_LIMIT','WORKER_FAILED'}
    assert time.monotonic()-started<4
    # Other concurrently running stores may legitimately have their own workers.
    for marker in (Path(__file__).resolve().parents[1]/'.private/runtime').glob('job-*/owner.json'):
        assert json.loads(marker.read_text())['owner']['pid']!=os.getpid()


def test_cancel_kills_and_reaps_descendant_group(tmp_path,monkeypatch):
    marker=tmp_path/'child.json'
    launcher(tmp_path,monkeypatch,
        f'child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"])\nPath({str(marker)!r}).write_text(json.dumps({{"parent":os.getpid(),"child":child.pid}}))\ntime.sleep(30)\n')
    with pytest.raises(Problem,match='CANCELLED'):run_job('inspect',{},cancel=marker.exists)
    pids=json.loads(marker.read_text())
    assert all(not Path(f'/proc/{pid}').exists() for pid in pids.values())


@pytest.mark.parametrize('mode',['oversize','pages','malformed','characters'])
def test_actual_pdf_ingress_limits(tmp_path,mode):
    source=tmp_path/'input.pdf';limits=Limits()
    if mode=='malformed':source.write_bytes(b'not a document')
    elif mode=='characters':synthetic_handout(source);limits=Limits(characters=10)
    else:
        c=canvas.Canvas(str(source),pagesize=(3000,3000) if mode=='oversize' else (612,792))
        for _ in range(3 if mode=='pages' else 1):c.drawString(10,10,'invented');c.showPage()
        c.save();limits=Limits(pages=2)
    with pytest.raises(Problem):run_job('extract',{'source':str(source),'lesson':3},limits=limits)
    good=tmp_path/'good.pdf';synthetic_handout(good)
    assert run_job('extract',{'source':str(good),'lesson':3})['coverage']['status']=='VERIFIED'


def test_cancel_before_and_during_parse_and_invalid_limit(tmp_path):
    source=tmp_path/'input.pdf';synthetic_handout(source)
    with pytest.raises(Problem,match='CANCELLED'):run_job('extract',{'source':str(source),'lesson':3},cancel=lambda:True)
    for value in (float('inf'),float('nan'),-1):
        with pytest.raises(ValidationError):Limits(wall_seconds=value)


def test_periodic_heartbeat_attempt_scoped_cancel_and_pid_identity(tmp_path):
    s=PrivateStore(tmp_path/'store')
    with pytest.raises(Problem,match='CANCELLED'):
        with Execution(s,'synthetic','fixed') as context:
            context.emit('PARSING');first=s.read('tasks/synthetic/state.json');time.sleep(.65)
            second=s.read('tasks/synthetic/state.json')
            assert second['heartbeat']>first['heartbeat'] and second['stage']=='PARSING'
            cancel_attempt(s,'synthetic',context.attempt_id);checkpoint()
    old=context.attempt_id
    assert services.task_status(s,'synthetic')['stage']=='CANCELLED'
    with Execution(s,'synthetic','fixed') as new:
        with pytest.raises(Problem,match='ATTEMPT_CHANGED'):cancel_attempt(s,'synthetic',old)
        new.check();new.finish('COMPLETE')
    state=s.read('tasks/synthetic/state.json');state.update({'stage':'MODELING','terminal':False})
    state['owner']['start_ticks']='impossible-start-time';s.json('tasks/synthetic/state.json',state)
    assert services.task_status(s,'synthetic')['stage']=='INTERRUPTED'


@pytest.mark.parametrize('sig',[signal.SIGTERM,signal.SIGKILL])
def test_real_task_owner_termination(tmp_path,sig):
    s=PrivateStore(tmp_path/'store');script=tmp_path/'owner.py'
    script.write_text('from pathlib import Path\nimport time\nfrom vocabatron.storage import PrivateStore\nfrom vocabatron.execution import Execution,checkpoint\n'+
        f's=PrivateStore(Path({str(s.root)!r}))\nwith Execution(s,"terminated","fixed") as context:\n context.emit("PARSING")\n while True:\n  checkpoint()\n  time.sleep(.03)\n')
    proc=subprocess.Popen([sys.executable,str(script)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        deadline=time.monotonic()+5
        while not s.path('tasks/terminated/state.json').exists() and time.monotonic()<deadline:time.sleep(.02)
        os.kill(proc.pid,sig);proc.wait(timeout=3)
        expected='CANCELLED' if sig==signal.SIGTERM else 'INTERRUPTED'
        assert services.task_status(s,'terminated')['stage']==expected
    finally:
        if proc.poll() is None:proc.kill();proc.wait()


def test_parent_killed_while_supervising_leaves_no_native_worker(tmp_path):
    s=PrivateStore(tmp_path/'store');marker=tmp_path/'ready.json'
    # Use the real guardian and a worker blocked in a native PDF operation by a
    # deliberately tiny wall budget in separate tests; here observe its process group.
    source=tmp_path/'input.pdf';synthetic_handout(source)
    script=tmp_path/'owner.py'
    script.write_text('from pathlib import Path\nfrom vocabatron.supervisor import run_job\n'+
        f'run_job("extract",{{"source":{str(source)!r},"lesson":3}})\n')
    proc=subprocess.Popen([sys.executable,str(script)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    descendants=[];deadline=time.monotonic()+3
    while proc.poll() is None and time.monotonic()<deadline:
        child_file=Path(f'/proc/{proc.pid}/task/{proc.pid}/children')
        if child_file.exists():descendants=[int(p) for p in child_file.read_text().split()]
        if descendants:break
        time.sleep(.005)
    if proc.poll() is None:proc.kill()
    proc.wait()
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        alive=[]
        for pid in descendants:
            p=Path(f'/proc/{pid}/stat')
            if p.exists() and p.read_text().rsplit(')',1)[1].split()[0]!='Z':alive.append(pid)
        if not alive:break
        time.sleep(.02)
    assert not alive


@pytest.mark.parametrize('failure',['stale_heartbeat','different_attempt'])
def test_status_does_not_trust_live_pid_without_matching_lease(tmp_path,failure):
    s=PrivateStore(tmp_path/'store')
    with Execution(s,'lease','fixed') as context:
        context.emit('MODELING');context.stop.set();context.thread.join(timeout=2)
        state=s.read('tasks/lease/state.json')
        if failure=='stale_heartbeat':state['heartbeat']=time.time()-30
        else:s.json('tasks/lease/active.json',{'attempt_id':'new-attempt','identity':'fixed'})
        s.json('tasks/lease/state.json',state)
        assert services.task_status(s,'lease')['stage']=='INTERRUPTED'
        context.finish('CANCELLED')
