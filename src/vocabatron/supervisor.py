"""Bounded, killable local jobs. Resource containment, not an OS security sandbox."""
from __future__ import annotations
import contextlib
import ctypes
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from pydantic import Field
from .domain import Record, Problem
from .execution import CURRENT, checkpoint, owner_identity


from .limits import Limits

DEFAULT_LIMITS=Limits()
_LAUNCHER=Path(__file__).with_name("worker_launcher.py")


def cleanup_abandoned(root):
    """Only tagged, owned runtime directories whose exact creator has exited."""
    for folder in root.glob('job-*'):
        marker=folder/'owner.json'
        if folder.is_symlink() or not marker.is_file() or marker.is_symlink():continue
        try:
            value=json.loads(marker.read_bytes())
            if value.get('format')!='vocabatron-resource-job-v1':continue
            owner=value['owner'];actual=owner_identity(owner['pid'])
            if actual!=owner:shutil.rmtree(folder)
        except (OSError,ValueError,KeyError):continue


def output_path(path):
    path=Path(path).absolute();project=Path(__file__).resolve().parents[2]
    if path.resolve()!=path or not any(path.is_relative_to(project/name) for name in (".private",".cache")):
        raise Problem("UNSAFE_PATH","输出必须位于本项目的私人任务目录")
    return path


def pdf_path(path, limits=DEFAULT_LIMITS):
    path=Path(path).absolute()
    if path.resolve()!=path or not path.is_file():raise Problem('UNSAFE_PATH','PDF 输入必须是无符号链接的普通文件')
    if not 0<path.stat().st_size<=limits.input_bytes:raise Problem('RESOURCE_LIMIT','PDF 文件大小超出限制')
    return path


def _stop(process):
    with contextlib.suppress(ProcessLookupError):os.killpg(process.pid,signal.SIGTERM)
    try:process.wait(timeout=.5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):os.killpg(process.pid,signal.SIGKILL)
        process.wait(timeout=2)
    # The guardian reaps the direct worker. Kill surviving process-group members too.
    with contextlib.suppress(ProcessLookupError):os.killpg(process.pid,signal.SIGKILL)
    # Linux subreaper adoption lets us wait only for descendants of this job's PGID.
    deadline=time.monotonic()+1
    while time.monotonic()<deadline:
        try:
            pid,_=os.waitpid(-process.pid,os.WNOHANG)
            if pid==0:time.sleep(.01)
        except ChildProcessError:break


def run_job(operation, payload, *, limits=None, cancel=None):
    context=CURRENT.get()
    limits=Limits.model_validate((limits or (context.pdf_limits if context else DEFAULT_LIMITS)).model_dump())
    checkpoint()
    root=Path(__file__).resolve().parents[2]/'.private'/'runtime'
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    cleanup_abandoned(root)
    job=Path(tempfile.mkdtemp(prefix='job-',dir=root));job.chmod(0o700)
    owner_fd=os.open(job/'owner.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(owner_fd,'w') as marker:json.dump({'format':'vocabatron-resource-job-v1','owner':owner_identity()},marker)
    proc=None;started=time.monotonic();streams=bytearray();error=None
    try:
        payload=dict(payload);delivery=None;preview_delivery=None
        for key in ('source','template','output'):
            if key not in payload:continue
            original=Path(payload[key]).absolute()
            target=job/(original.name if key=='output' else key+'.pdf')
            if key=='output' and operation=='export':
                if original.exists() or original.resolve()!=original:raise Problem('OUTPUT_EXISTS','不覆盖已有输出')
                output_path(original)
                delivery=(target,original)
            else:
                pdf_path(original,limits)
                fd=os.open(original,os.O_RDONLY|os.O_NOFOLLOW)
                with os.fdopen(fd,'rb') as f:
                    data=f.read(limits.input_bytes+1)
                if len(data)>limits.input_bytes:raise Problem('RESOURCE_LIMIT','输入读取超过上限')
                out=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o400)
                with os.fdopen(out,'wb') as f:f.write(data)
            payload[key]=str(target)
        if payload.get('preview_dir'):
            preview_delivery=output_path(payload['preview_dir'])
            if preview_delivery.resolve()!=preview_delivery:raise Problem('UNSAFE_PATH','预览路径含符号链接')
            payload['preview_dir']=str(job/'previews')
        request=json.dumps({'operation':operation,'payload':payload,'limits':limits.model_dump()},allow_nan=False).encode()
        if len(request)>limits.ipc_bytes:raise Problem('RESOURCE_LIMIT','子进程请求超过上限')
        fd=os.open(job/'request.json',os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'wb') as f:f.write(request)
        env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','LC_ALL':'C.UTF-8',
             'OPENBLAS_NUM_THREADS':'1','OMP_NUM_THREADS':'1','PYTHONHASHSEED':'0',
             'PYTHONPATH':str(Path(__file__).resolve().parents[1]),'TMPDIR':str(job)}
        launcher=str(_LAUNCHER)
        if ctypes.CDLL(None,use_errno=True).prctl(36,1,0,0,0)!=0:
            raise Problem('WORKER_FAILED','无法启用本任务后代回收')
        proc=subprocess.Popen([sys.executable,launcher,str(job),json.dumps(limits.model_dump())],
            cwd=job,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            start_new_session=True,close_fds=True,shell=False)
        selector=selectors.DefaultSelector()
        for pipe in (proc.stdout,proc.stderr):os.set_blocking(pipe.fileno(),False);selector.register(pipe,selectors.EVENT_READ)
        try:
            while proc.poll() is None or selector.get_map():
                checkpoint()
                if cancel and cancel():raise Problem('CANCELLED','受监督任务已取消')
                if time.monotonic()-started>limits.wall_seconds:raise Problem('RESOURCE_LIMIT','子进程墙钟时间超限')
                total=sum(p.lstat().st_size for p in job.rglob('*') if p.is_file() and not p.is_symlink())
                if total>limits.temporary_bytes:raise Problem('RESOURCE_LIMIT','任务临时文件总量超限')
                for key,_ in selector.select(.04):
                    chunk=os.read(key.fileobj.fileno(),min(65536,limits.stream_bytes+1-len(streams)))
                    if not chunk:selector.unregister(key.fileobj);continue
                    streams.extend(chunk)
                    if len(streams)>limits.stream_bytes:raise Problem('RESOURCE_LIMIT','子进程输出超限')
            proc.wait()
        finally:
            selector.close()
            _stop(proc)
            proc.stdout.close();proc.stderr.close()
        response=job/'response.json'
        if not response.is_file():
            code='RESOURCE_LIMIT' if proc.returncode in (-9,-24,-25,137,152,153) else 'WORKER_FAILED'
            raise Problem(code,'受监督任务未返回完整结果',details={'returncode':proc.returncode})
        if response.stat().st_size>limits.ipc_bytes:raise Problem('RESOURCE_LIMIT','子进程响应超限')
        result=json.loads(response.read_bytes())
        if not isinstance(result,dict) or set(result)-{'ok','result','code','metrics','error_details'}:raise Problem('WORKER_FAILED','子进程响应格式错误')
        if result.get('ok') is not True:
            if preview_delivery and (job/'previews').is_dir():
                from .storage import private_mkdir
                private_mkdir(preview_delivery)
                for source in (job/'previews').glob('*difference*.png'):
                    if source.is_file() and source.stat().st_size<=limits.file_bytes:
                        destination=preview_delivery/source.name
                        fd=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                        with os.fdopen(fd,'wb') as out,source.open('rb') as src:shutil.copyfileobj(src,out)
            code=result.get('code','WORKER_FAILED')
            if not isinstance(code,str) or not code.replace('_','').isalnum():code='WORKER_FAILED'
            raise Problem(code,'受监督操作失败；源数据未发布',details={'operation':operation,'worker':result.get('metrics'),'failure':result.get('error_details')})
        if proc.returncode!=0:raise Problem('WORKER_FAILED','子进程异常退出')
        checkpoint()
        from .storage import private_mkdir
        if delivery:
            source,destination=delivery
            if not source.is_file() or source.stat().st_size>limits.file_bytes:raise Problem('RESOURCE_LIMIT','输出大小不合法')
            private_mkdir(destination.parent)
            fd=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as out,source.open('rb') as src:shutil.copyfileobj(src,out);out.flush();os.fsync(out.fileno())
        if preview_delivery:
            private_mkdir(preview_delivery)
            for source in (job/'previews').glob('*.png'):
                fd=os.open(preview_delivery/source.name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                with os.fdopen(fd,'wb') as out,source.open('rb') as src:shutil.copyfileobj(src,out)
        context=CURRENT.get()
        if context:
            context.subprocesses.append({'operation':operation,'seconds':time.monotonic()-started,**result.get('metrics',{})})
        return result['result']
    finally:
        if proc is not None and proc.poll() is None:_stop(proc)
        shutil.rmtree(job)
