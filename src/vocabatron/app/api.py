"""Authenticated private control plane. Heavy work belongs to the persistent queue."""
from __future__ import annotations
import asyncio
from collections import defaultdict,deque
import contextlib
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import tempfile
import time
import shutil
from urllib.parse import urlsplit
from fastapi import FastAPI,Request,UploadFile,File,Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse,Response,StreamingResponse,FileResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel,ConfigDict,Field
from ..domain import Problem
from ..storage import private_mkdir
from .config import load_config
from .library import Library,GenerateRequest
from .database import encode,one,insert_task,task_record
from .job_process import english_message

COOKIE='vocabatron_session'
SAFE=re.compile(r'^[a-f0-9]{32,64}$')


class Boundary:
    def __init__(self,app,config):
        self.app,self.config=app,config
        self.rates=defaultdict(deque);self.streams=defaultdict(int)
        self.uploads=0

    def sign(self,text):
        return hmac.new(self.config.csrf_secret.get_secret_value().encode(),text.encode(),hashlib.sha256).hexdigest()

    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        headers=defaultdict(list)
        for key,value in scope['headers']:headers[key.lower()].append(value.decode('latin1'))
        async def reject(code,message,status):
            await JSONResponse({'code':code,'message':message},status_code=status,headers={'Cache-Control':'no-store'})(scope,receive,send)
        peer=(scope.get('client') or ('',0))[0]
        if peer not in ('127.0.0.1','::1','testclient'):
            return await reject('UNTRUSTED_PROXY','A trusted private connection is required.',403)
        login=headers.get(b'tailscale-user-login',[])
        if len(login)!=1 or login[0].casefold() not in self.config.allowed_logins:
            return await reject('ACCESS_DENIED','This Tailscale login is not authorized.',403)
        identity=login[0].casefold()
        expected=urlsplit(self.config.public_base_url).netloc
        if headers.get(b'host')!=[expected]:return await reject('INVALID_HOST','The private app address is required.',400)
        for name in (b'x-forwarded-host',b'x-forwarded-proto'):
            expected_value=expected if name==b'x-forwarded-host' else 'https'
            if name in headers and headers[name]!=[expected_value]:return await reject('INVALID_PROXY','Unexpected proxy metadata.',400)
        original=scope['path'];base=self.config.base_path.rstrip('/')
        if original==base:scope={**scope,'path':'/'}
        elif original.startswith(base+'/'):scope={**scope,'path':original[len(base):]}
        # Tailscale Serve may strip the configured mount prefix. Only known
        # application routes are accepted below; no arbitrary filesystem mapping.
        path=scope['path'];private=path.startswith('/api/')
        if private:
            category='read' if scope['method'] in ('GET','HEAD','OPTIONS') else 'write'
            # Several devices can share one login. Read-only polling, SSE and
            # PDF ranges have a separate allowance from state-changing calls.
            limit=960 if category=='read' else 120
            now=time.time();queue=self.rates[identity,category]
            while queue and now-queue[0]>60:queue.popleft()
            if len(queue)>=limit:return await reject('RATE_LIMIT','Too many requests. Try again shortly.',429)
            queue.append(now)
        cookie=Request(scope).cookies.get(COOKIE,'')
        try:
            issued,signature=cookie.split('.',1)
            valid=0<=time.time()-int(issued)<86400 and hmac.compare_digest(signature,self.sign(identity+':'+issued))
        except (ValueError,TypeError):valid=False
        if private and path!='/api/session' and not valid:
            return await reject('SESSION_REQUIRED','Reload the app to renew your session.',401)
        if scope['method'] not in ('GET','HEAD','OPTIONS'):
            if headers.get(b'origin')!=[self.config.origin]:return await reject('CSRF_ORIGIN','The request origin is not authorized.',403)
            token=headers.get(b'x-csrf-token',[])
            if not valid or len(token)!=1 or not hmac.compare_digest(token[0],self.sign('csrf:'+identity+':'+cookie)):
                return await reject('CSRF_TOKEN','Reload the app before making changes.',403)
        limit=self.config.upload_max_bytes*self.config.upload_max_files+1024**2 if path=='/api/uploads' else 512*1024
        lengths=headers.get(b'content-length',[])
        try:
            if len(lengths)>1 or (lengths and (int(lengths[0])<0 or int(lengths[0])>limit)):raise ValueError()
        except ValueError:return await reject('BODY_TOO_LARGE','This request exceeds the size limit.',413)
        used=0
        async def bounded_receive():
            nonlocal used
            message=await receive();used+=len(message.get('body',b''))
            if used>limit:raise Problem('BODY_TOO_LARGE','This request exceeds the size limit.')
            return message
        scope.setdefault('state',{})['login']=identity
        scope['state']['boundary']=self
        scope['state']['session_cookie']=cookie if valid else None
        streaming=path=='/api/events'
        uploading=path=='/api/uploads'
        if streaming and self.streams[identity]>=8:return await reject('STREAM_LIMIT','Too many live connections.',429)
        if uploading:
            if self.uploads>=2:return await reject('UPLOAD_BUSY','Two uploads are already in progress. Try again shortly.',429)
            required=(int(lengths[0]) if lengths else limit)*3+self.config.resources.disk_reserve_bytes
            if shutil.disk_usage(self.config.data_root).free<required:return await reject('STORAGE_RESERVE','There is not enough free space for this upload.',507)
        async def secure_send(message):
            if message['type']=='http.response.start':
                added=[(b'x-content-type-options',b'nosniff'),(b'referrer-policy',b'no-referrer'),
                    (b'permissions-policy',b'camera=(), microphone=(), geolocation=()'),
                    (b'content-security-policy',b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self'; font-src 'self' blob:; worker-src 'self' blob:; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")]
                if private:added.extend([(b'cache-control',b'no-store, private'),(b'vary',b'Tailscale-User-Login, Cookie')])
                message={**message,'headers':[(k,v) for k,v in message.get('headers',[]) if not(private and k.lower()==b'cache-control')]+added}
            await send(message)
        if streaming:self.streams[identity]+=1
        if uploading:self.uploads+=1
        try:await self.app(scope,bounded_receive,secure_send)
        finally:
            if streaming:self.streams[identity]-=1
            if uploading:self.uploads-=1


class PreferencesChange(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    version:int=Field(ge=1)
    value:dict

class LessonAction(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    lesson_id:str=Field(pattern=r'^[a-f0-9]{64}$')


def create_app(config=None):
    config=config or load_config();library=Library(config);library.initialize()
    app=FastAPI(title='Vocabatron',docs_url=None,redoc_url=None,openapi_url=None)
    app.state.library=library;app.add_middleware(Boundary,config=config)

    @app.exception_handler(Problem)
    async def problem(_request,exc):
        code=exc.code
        status=404 if code=='NOT_FOUND' else 413 if code in ('BODY_TOO_LARGE','UPLOAD_SIZE') else 409 if code.endswith('CONFLICT') else 400
        message=str(exc.message) if all(ord(c)<128 for c in str(exc.message)) else english_message(code)
        return JSONResponse({'code':code,'message':message},status_code=status)

    @app.exception_handler(RequestValidationError)
    async def invalid(_request,exc):
        return JSONResponse({'code':'INPUT_INVALID','message':'Check the request fields, counts, and selected lessons.'},status_code=422)

    @app.get('/api/session')
    def session(request:Request):
        boundary=request.state.boundary;issued=str(int(time.time()));login=request.state.login
        cookie=request.state.session_cookie or issued+'.'+boundary.sign(login+':'+issued)
        response=JSONResponse({'owner':'owner','csrf':boundary.sign('csrf:'+login+':'+cookie),'preferences':library.preferences()})
        response.set_cookie(COOKIE,cookie,max_age=86400,secure=True,httponly=True,samesite='strict',path=config.base_path)
        return response

    @app.get('/api/preferences')
    def preferences():return library.preferences()

    @app.patch('/api/preferences')
    def change_preferences(change:PreferencesChange):return library.update_preferences(change.value,change.version)

    @app.get('/api/lessons')
    def lessons(q:str=Query('',max_length=100),offset:int=Query(0,ge=0),limit:int=Query(30,ge=1,le=100),archived:bool=False):
        return library.list_lessons(q,offset,limit,archived=archived)

    @app.get('/api/lessons/{lesson_id}')
    def lesson(lesson_id:str):
        row,_=library.lesson(lesson_id)
        values=library.list_lessons(lesson_id,archived=bool(row['archived']))['items']
        if not values:raise Problem('NOT_FOUND','Lesson not found')
        return values[0]

    @app.patch('/api/lessons/{lesson_id}')
    async def edit_lesson(lesson_id:str,request:Request):
        value=await request.json()
        if not isinstance(value,dict) or set(value)-{'display_name','archived'}:raise Problem('INPUT_INVALID','Unknown lesson field')
        if 'display_name' in value and (not isinstance(value['display_name'],str) or len(value['display_name'])>100):raise Problem('INPUT_INVALID','Display name is too long')
        if 'archived' in value and type(value['archived'])!=bool:raise Problem('INPUT_INVALID','Expected a boolean')
        library.lesson(lesson_id)
        with library.db.transaction() as c:
            for key,val in value.items():c.execute('UPDATE lessons SET '+key+'=? WHERE id=?',(val,lesson_id))
            library.db.event(c,'library',{'id':lesson_id})
        return {'saved':True}

    @app.get('/api/lessons/{lesson_id}/results')
    def results(lesson_id:str,offset:int=Query(0,ge=0),limit:int=Query(20,ge=1,le=50)):
        return library.results(lesson_id,offset,limit)

    @app.get('/api/sources')
    def sources(offset:int=Query(0,ge=0)):
        return {'items':library.db.all('SELECT id,name,bytes,status,pages,report IS NOT NULL AS report FROM sources ORDER BY created DESC LIMIT 100 OFFSET ?',(offset,))}

    @app.get('/api/sources/{source_id}/report')
    def source_report(source_id:str):
        source=library.db.one('SELECT report FROM sources WHERE id=?',(source_id,))
        if not source or not source['report']:raise Problem('NOT_FOUND','Import details are not available yet')
        report=library.objects.read_json(source['report'])
        return {'issues':report.get('unresolved_sections',[])[:100],'lessons':[{'number':r.get('lesson',{}).get('lesson'),
                'status':r.get('coverage',{}).get('status'),'errors':r.get('coverage',{}).get('issues',[])} for r in report.get('lessons',[])][:256]}

    @app.get('/api/lessons/{lesson_id}/transcript')
    def transcript(lesson_id:str):
        library.lesson(lesson_id)
        binding=library.db.one('SELECT binding FROM source_bindings WHERE lesson_id=? ORDER BY created LIMIT 1',(lesson_id,))
        value=json.loads(binding['binding']);reference=value['transcript'];fd=library.objects.open_verified(reference,Path(reference).stem)
        with os.fdopen(fd,'rb') as stream:
            if os.fstat(stream.fileno()).st_size>4*1024**2:raise Problem('RESOURCE_LIMIT','Transcript exceeds the preview size limit')
            content=stream.read().decode('utf-8')
        return {'text':content,'source_id':value['source_id'],'pages':value['pages']}

    @app.post('/api/uploads')
    async def uploads(request:Request):
        private_mkdir(config.runtime_root/'uploads');results=[]
        async with request.form(max_files=config.upload_max_files,max_fields=0,max_part_size=config.upload_max_bytes) as form:
            files=form.getlist('files')
            if not 1<=len(files)<=config.upload_max_files or len(form.multi_items())!=len(files):raise Problem('INPUT_INVALID','Choose PDF files only')
            for file in files:
                if not hasattr(file,'read'):raise Problem('INPUT_INVALID','Expected an uploaded PDF')
                fd,tmp=tempfile.mkstemp(dir=config.runtime_root/'uploads',prefix='upload-')
                try:
                    count=0
                    with os.fdopen(fd,'wb') as out:
                        while chunk:=await file.read(1024*1024):
                            count+=len(chunk)
                            if count>config.upload_max_bytes:raise Problem('UPLOAD_SIZE','A PDF exceeds the upload size limit')
                            await run_in_threadpool(out.write,chunk)
                        await run_in_threadpool(out.flush);await run_in_threadpool(os.fsync,out.fileno())
                    results.append(await run_in_threadpool(library.source,Path(tmp),file.filename or 'Document.pdf'))
                finally:os.unlink(tmp)
        return {'items':results}

    @app.post('/api/generate')
    def generate(value:GenerateRequest):return library.submit(value)

    def enqueue(kind,lesson_id,inputs=None):
        row,_=library.lesson(lesson_id)
        with library.db.transaction() as c:
            previous=one(c,"SELECT id,status FROM tasks WHERE kind=? AND lesson_id=? AND status NOT IN ('COMPLETED','CANCELLED','EXHAUSTED','FAILED','NEEDS_ATTENTION')",(kind,lesson_id))
            if previous:return {**previous,'reused':True}
            task=task_record(kind,inputs or {'lesson_content_id':lesson_id,'lesson_json':row['lesson_json']},lesson_id=lesson_id,family_id=row['family_id'],priority=20)
            insert_task(c,task);library.db.event(c,'task',{'id':task['id'],'status':'QUEUED'},task['id'])
        return {'id':task['id'],'status':'QUEUED'}

    @app.post('/api/restore')
    def restore(value:LessonAction):return enqueue('restore',value.lesson_id)

    @app.post('/api/prepare')
    def prepare(value:LessonAction):return enqueue('prepare',value.lesson_id)

    @app.post('/api/artifacts/{artifact_id}/verify')
    def reverify(artifact_id:str):
        artifact=library.db.one("SELECT lesson_id FROM artifacts WHERE id=? AND state='AVAILABLE'",(artifact_id,))
        if not artifact:raise Problem('NOT_FOUND','Saved PDF not found')
        return enqueue('verify',artifact['lesson_id'],{'artifact_id':artifact_id})

    @app.get('/api/tasks')
    def tasks(offset:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=100)):return library.list_tasks(offset,limit)

    @app.post('/api/tasks/{task_id}/{action}')
    def task_control(task_id:str,action:str):return library.control(task_id,action)

    @app.get('/api/resources')
    def resources():
        row=library.db.one('SELECT value FROM telemetry WHERE singleton=1');sample=json.loads(row['value']) if row else None
        if sample:
            sample={**sample,'host_cpu_percent':sample.get('cpu_percent'),'app_cpu_percent':sample.get('application_cpu_percent'),
                    'memory_available_bytes':sample.get('memory',{}).get('MemAvailable'),'app_rss_bytes':sample.get('application',{}).get('cgroup_memory_bytes') if sample.get('application',{}).get('cgroup_memory_bytes') is not None else sample.get('application',{}).get('rss_upper_bound_bytes'),
                    'app_memory_scope':'Application cgroup, including its file cache; excludes the shared model service' if sample.get('application',{}).get('cgroup_memory_bytes') is not None else 'Process RSS sum; shared pages may be counted more than once',
                    'psi':sample.get('pressure'),'gpu':{**(sample.get('gpu') or {}),'utilization':(sample.get('gpu') or {}).get('utilization_percent')}}
        waiting=library.db.one("SELECT count(*) n FROM tasks WHERE status IN ('QUEUED','WAITING_FOR_RESOURCES','PAUSED_FOR_RESOURCES','RETRY_WAIT','PARTIALLY_COMPLETED')")['n']
        latest=library.db.one("SELECT detail FROM tasks WHERE status='WAITING_FOR_RESOURCES' ORDER BY updated DESC LIMIT 1")
        reason=json.loads(latest['detail']).get('reason') if latest and latest['detail'] else None
        return {'sample':sample,'stale_seconds':config.resources.stale_seconds,'policy':config.resources.model_dump(),'reason':reason,'waiting':waiting,'active':sample.get('active_slots',0) if sample else 0}

    @app.get('/api/maintenance')
    def maintenance():
        from .. import __version__
        return {'version':__version__,'saved_pdfs':library.db.one("SELECT count(*) n FROM artifacts WHERE state='AVAILABLE'")['n'],
            'pdf_bytes':library.db.one("SELECT coalesce(sum(bytes),0) n FROM (SELECT max(bytes) bytes FROM artifacts WHERE state='AVAILABLE' GROUP BY sha256)")['n'],
            'source_documents':library.db.one('SELECT count(*) n FROM sources')['n'],'database':'SQLite WAL / FULL',
            'archive_policy':'Verified originals and structure history are retained. Export copies can be restored from each lesson.'}

    @app.get('/api/events')
    async def events(request:Request):
        try:cursor=int(request.headers.get('last-event-id','0'))
        except ValueError:cursor=0
        async def feed():
            nonlocal cursor
            started=time.monotonic()
            while time.monotonic()-started<55:
                if await request.is_disconnected():return
                values=await run_in_threadpool(library.db.all,'SELECT seq,type,payload FROM events WHERE seq>? ORDER BY seq LIMIT 100',(cursor,))
                for item in values:
                    cursor=item['seq'];kind='preferences' if item['type']=='preferences' else 'message'
                    yield f'id: {cursor}\nevent: {kind}\ndata: {item["payload"]}\n\n'
                if not values:yield ': keepalive\n\n'
                await asyncio.sleep(2)
        return StreamingResponse(feed(),media_type='text/event-stream',headers={'X-Accel-Buffering':'no'})

    @app.api_route('/api/artifacts/{artifact_id}/pdf',methods=['GET','HEAD'])
    def pdf(artifact_id:str,request:Request,download:int=0):
        row=library.db.one("SELECT * FROM artifacts WHERE id=? AND state='AVAILABLE'",(artifact_id,))
        if not row:raise Problem('NOT_FOUND','Saved PDF not found')
        if not re.fullmatch(r'Variant_\d+_Lesson_\d+_Crossword\.pdf',row['filename']):raise Problem('ARTIFACT_CHANGED','Invalid saved filename')
        return stream_pdf(row,request,download)

    @app.api_route('/api/sources/{source_id}/pdf',methods=['GET','HEAD'])
    def original_pdf(source_id:str,request:Request,download:int=0):
        row=library.db.one('SELECT path,id AS sha256,bytes FROM sources WHERE id=?',(source_id,))
        if not row:raise Problem('NOT_FOUND','Source document not found')
        row['filename']='Source_'+source_id[:12]+'.pdf'
        return stream_pdf(row,request,download)

    def stream_pdf(row,request,download):
        fd=library.objects.open_verified(row['path'],row['sha256']);size=os.fstat(fd).st_size
        start,end,status=0,size-1,200
        try:
            requested=request.headers.get('range')
            if requested:
                match=re.fullmatch(r'bytes=(\d*)-(\d*)',requested)
                if not match or not any(match.groups()):raise ValueError()
                a,b=match.groups()
                if not a:start=max(0,size-int(b))
                else:start=int(a);end=min(end,int(b)) if b else end
                if start>end or start>=size:raise ValueError()
                if end-start+1>8*1024**2 or len(request.headers.getlist('range'))!=1:raise ValueError()
                status=206
            filename=row['filename']
            headers={'Accept-Ranges':'bytes','Content-Length':str(end-start+1),'ETag':'"'+row['sha256']+'"',
                     'Content-Disposition':('attachment' if download else 'inline')+'; filename="'+filename+'"'}
            if status==206:headers['Content-Range']=f'bytes {start}-{end}/{size}'
            if request.method=='HEAD':os.close(fd);return Response(status_code=status,media_type='application/pdf',headers=headers)
        except ValueError:
            os.close(fd);return Response(status_code=416,headers={'Content-Range':f'bytes */{size}'})
        except BaseException:os.close(fd);raise
        def chunks():
            try:
                os.lseek(fd,start,os.SEEK_SET);remaining=end-start+1
                while remaining:
                    chunk=os.read(fd,min(65536,remaining))
                    if not chunk:break
                    remaining-=len(chunk);yield chunk
            finally:os.close(fd)
        return StreamingResponse(chunks(),status_code=status,media_type='application/pdf',headers=headers)

    @app.get('/{path:path}')
    def static(path:str):
        if path.startswith('api/'):raise Problem('NOT_FOUND','API route not found')
        relative=path or 'index.html'
        if relative not in ('index.html','sw.js','manifest.webmanifest') and not relative.startswith(('assets/','icons/')):
            raise Problem('NOT_FOUND','Page not found')
        p=Path(relative)
        if p.is_absolute() or '..' in p.parts or '\\' in relative:raise Problem('NOT_FOUND','Asset not found')
        target=config.static_root/p
        if target.resolve()!=target or not target.is_file():raise Problem('NOT_FOUND','App assets are not built yet')
        cache='public, max-age=31536000, immutable' if relative.startswith('assets/') else 'no-cache'
        return FileResponse(target,headers={'Cache-Control':cache,'Service-Worker-Allowed':config.base_path} if relative=='sw.js' else {'Cache-Control':cache})
    return app


def main():
    import uvicorn
    from .resources import idle_priority
    config=load_config();idle_priority()
    uvicorn.run(create_app(config),host=config.listen_host,port=config.listen_port,proxy_headers=False,access_log=False,limit_concurrency=64,timeout_keep_alive=5)


if __name__=='__main__':main()
