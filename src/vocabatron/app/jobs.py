"""One bounded queue slice per owned process; durable progress survives restarts."""
from __future__ import annotations
import json
import os
from pathlib import Path
import resource
import time
import uuid
from ..domain import Lesson,Problem,digest
from ..execution import Execution
from ..storage import PrivateStore,sha256
from ..supervisor import run_job
from ..clues import Ollama,select
from ..evidence import verify_evidence
from .archive import Archive,delivery_id
from .book import assemble
from .database import require_fence,one,rows,encode,insert_task,task_record
from .identity import identities,structure
from .resources import pressure_reason
from .search import next_structure
from .exports import restore_one


class TaskExecution(Execution):
    def __init__(self,library,task_id,fence):
        self.library,self.fence=library,fence;self.last_check=0;self.last_lease=0;self.stop_code=None
        super().__init__(PrivateStore(library.config.runtime_root/'executions'),task_id,fence,
            total_seconds=900,stage_seconds=650,pdf_limits=library.config.document_limits)

    def _poll(self):
        now=time.monotonic()
        if now-self.last_check<.2:return
        self.last_check=now
        task=self.library.db.one('SELECT fence,intent,status FROM tasks WHERE id=?',(self.task_id,))
        if not task or task['fence']!=self.fence or task['status']!='RUNNING':self.stop_code='ATTEMPT_EXPIRED'
        elif task['intent']!='run':self.stop_code='CANCELLED' if task['intent']=='cancel' else 'PAUSED_BY_USER'
        if self.stop_code:self.event.set()

    def cancelled(self,*,force=False):
        self._poll()
        return super().cancelled(force=force)

    def check(self,*,force_cancel=False):
        self._poll()
        if self.stop_code:raise Problem(self.stop_code,'This task attempt has been stopped')
        super().check(force_cancel=force_cancel)

    def persist(self):
        super().persist()
        if time.monotonic()-self.last_lease>=1:
            with self.library.db.transaction() as c:
                task=require_fence(c,self.task_id,self.fence,allow_intent=True)
                c.execute('UPDATE tasks SET lease_until=?,updated=? WHERE id=?',(time.time()+15,time.time(),self.task_id))
            self.last_lease=time.monotonic()

    def _heartbeat(self):
        while not self.stop.wait(.4):
            with self.lock:
                if self.terminal:return
                try:self.persist()
                except Exception as exc:self.heartbeat_error=type(exc).__name__;return

    def emit(self,stage,value=None):
        super().emit(stage,value)
        with self.library.db.transaction() as c:
            require_fence(c,self.task_id,self.fence)
            c.execute('UPDATE tasks SET stage=?,detail=?,updated=? WHERE id=?',(stage,encode(value or {}),time.time(),self.task_id))
            self.library.db.event(c,'task',{'id':self.task_id,'stage':stage,**(value or {})},self.task_id)


class Job:
    def __init__(self,library,task_id,fence):
        self.library,self.db,self.task_id,self.fence=library,library.db,task_id,fence
        self.task=self.db.one('SELECT * FROM tasks WHERE id=?',(task_id,))
        self.inputs=json.loads(self.task['input_json'])
        checkpoint=self.db.one('SELECT value FROM task_checkpoints WHERE task_id=?',(task_id,))
        self.saved=json.loads(checkpoint['value']) if checkpoint else {}

    def save(self,**values):
        self.saved.update(values)
        with self.db.transaction() as c:
            require_fence(c,self.task_id,self.fence)
            c.execute('INSERT INTO task_checkpoints VALUES(?,?) ON CONFLICT(task_id) DO UPDATE SET value=excluded.value',
                      (self.task_id,encode(self.saved)))

    def run(self):
        started=time.perf_counter()
        with TaskExecution(self.library,self.task_id,self.fence) as context:
            self.context=context
            if self.task['kind']=='import':outcome=self.import_document()
            elif self.task['kind']=='generate':outcome=self.generate()
            elif self.task['kind']=='prepare':self.prepare();outcome='COMPLETED'
            elif self.task['kind']=='restore':outcome=self.restore()
            elif self.task['kind']=='verify':outcome=self.reverify()
            else:raise Problem('INPUT_INVALID','Unknown persistent task kind')
            context.finish('COMPLETE',{'queue_outcome':outcome})
        return {'status':outcome,'seconds':time.perf_counter()-started,
                'peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                'memory_scope':'single queue-slice process lifetime peak; excludes shared Ollama',
                'document_workers':context.subprocesses}

    def import_document(self):
        source=self.db.one('SELECT * FROM sources WHERE id=?',(self.inputs['source_id'],))
        self.context.emit('Checking document')
        path=self.library.objects.path(source['path'])
        if sha256(path)!=source['id']:raise Problem('SOURCE_CHANGED','The uploaded source changed')
        if 'document_info' not in self.saved:
            self.save(document_info=run_job('inspect',{'source':str(path)}))
        count=self.saved['document_info']['pages'];chunks=self.saved.get('page_chunks',[])
        done=sum(len(self.library.objects.read_json(ref)['pages']) for ref in chunks)
        while done<count:
            numbers=list(range(done+1,min(done+8,count)+1))
            self.context.emit('Extracting text',{'pages_done':done,'pages_total':count})
            result=run_job('book_pages',{'source':str(path),'page_numbers':numbers})
            if result['source_sha256']!=source['id']:raise Problem('SOURCE_CHANGED','Extraction source mismatch')
            chunks.append(self.library.objects.put_json(result));self.save(page_chunks=chunks)
            done+=len(numbers)
        self.context.emit('Detecting lessons',{'pages_done':count,'pages_total':count})
        pages=[page for ref in chunks for page in self.library.objects.read_json(ref)['pages']]
        if sum(p['character_count'] for p in pages)>self.library.config.document_limits.characters:
            raise Problem('RESOURCE_LIMIT','The whole document exceeds its character limit')
        result=assemble(source['id'],pages,total_pages=count)
        self.context.emit('Validating source',{'lessons_found':len(result['lessons'])})
        complete_report=self.library.objects.put_json(result)
        summary={'status':result['status'],'full_report':complete_report,'unresolved_sections':result['unresolved_sections'],
                 'lessons':[{'lesson':{'lesson':b['lesson']['lesson']},'coverage':{'status':b['coverage']['status'],'issues':b['coverage']['issues']}} for b in result['lessons']]}
        report_ref=self.library.objects.put_json(summary);published=[]
        for bundle in result['lessons']:
            if bundle['coverage']['status']!='VERIFIED':continue
            self.context.emit('Building lesson library',{'lessons_done':len(published),'lessons_found':len(result['lessons'])})
            published.append(self.library.add_lesson(bundle,source['id'],extra_binding={'page_evidence':chunks},fence=(self.task_id,self.fence)))
        issues=result['unresolved_sections'] or len(published)!=len(result['lessons']) or not published
        status=('PARTIALLY_IMPORTED' if published else 'NEEDS_ATTENTION') if issues else 'READY'
        with self.db.transaction() as c:
            require_fence(c,self.task_id,self.fence)
            c.execute('UPDATE sources SET status=?,pages=?,report=? WHERE id=?',(status,count,report_ref,source['id']))
            self.db.event(c,'library',{'source_id':source['id'],'status':status,'lessons':published},self.task_id)
            prefs=json.loads(one(c,'SELECT value FROM preferences WHERE singleton=1')['value'])
            if prefs['background_prepare']:
                for item in published:
                    lesson=one(c,'SELECT * FROM lessons WHERE id=?',(item['lesson_id'],))
                    if lesson['frozen_ref'] or one(c,"SELECT id FROM tasks WHERE kind='prepare' AND lesson_id=? AND status NOT IN ('FAILED','CANCELLED')",(lesson['id'],)):continue
                    task=task_record('prepare',{'lesson_content_id':lesson['id'],'lesson_json':lesson['lesson_json']},
                                     lesson_id=lesson['id'],family_id=lesson['family_id'],priority=40)
                    insert_task(c,task)
        self.context.emit('Partially imported' if issues and published else 'Needs attention' if issues else 'Ready',{'lessons_imported':len(published)})
        return 'NEEDS_ATTENTION' if issues else 'COMPLETED'

    def prepare(self):
        row,original=self.library.lesson(self.task['lesson_id'])
        if row['frozen_ref']:
            reference=json.loads(row['frozen_ref'])
            frozen,store=self.library.frozen(reference)
            bound=Lesson.model_validate(self.library.objects.read_json(reference.get('lesson_object',row['lesson_json'])))
            if identities(bound)['lesson_content_id']!=self.task['lesson_id']:raise Problem('CONTENT_HASH_CONFLICT','Frozen content binding differs')
            verify_evidence(store,bound,frozen)
            self.save(frozen_ref=reference);return reference
        lesson=Lesson.model_validate(self.library.objects.read_json(self.inputs['lesson_json']))
        run_id=self.saved.get('selection_run_id') or uuid.uuid4().hex
        self.save(selection_run_id=run_id)
        self.context.emit('Preparing clues')
        def permit():
            self.context.check()
            snapshot=self.db.one('SELECT value FROM telemetry WHERE singleton=1')
            reason=pressure_reason(json.loads(snapshot['value']) if snapshot else None,self.library.config.resources,inference=True,running=True)
            if reason or self.db.one('SELECT id FROM holds WHERE until>?',(time.time(),)):
                raise Problem('PAUSED_FOR_RESOURCES',reason or 'An external workload has priority')
        store=PrivateStore(self.library.config.data_root/'core')
        frozen=select(lesson,Ollama(),store,publish=False,run_id=run_id,before_request=permit)
        evidence=verify_evidence(store,lesson,frozen)
        reference={'object':self.library.objects.put_json(frozen.model_dump(mode='json')),
                   'lesson_object':self.inputs['lesson_json'],'selection_run_id':frozen.selection_run_id,
                   'evidence_status':evidence['status']}
        with self.db.transaction() as c:
            require_fence(c,self.task_id,self.fence)
            current=one(c,'SELECT frozen_ref FROM lessons WHERE id=?',(self.task['lesson_id'],))
            if current['frozen_ref']:reference=json.loads(current['frozen_ref'])
            else:c.execute('UPDATE lessons SET frozen_ref=? WHERE id=?',(encode(reference),self.task['lesson_id']))
        self.save(frozen_ref=reference)
        return reference

    def generate(self):
        if self.task['target_type']=='count' and self.task['completed']>=self.task['target_count']:
            return 'COMPLETED'
        reference=self.saved.get('frozen_ref') or self.inputs.get('frozen_ref')
        if not reference:
            self.prepare()
            # Inference and search receive separate, freshly checked reservations.
            return 'QUEUED'
        frozen,store=self.library.frozen(reference)
        lesson=Lesson.model_validate(self.library.objects.read_json(reference.get('lesson_object',self.inputs['lesson_json'])))
        if identities(lesson)['lesson_content_id']!=self.task['lesson_id']:raise Problem('TASK_INPUT_CHANGED','Queued content identity changed')
        verify_evidence(store,lesson,frozen)
        template=self.library.objects.path(self.inputs['template_ref'])
        if sha256(template)!=self.inputs['template_sha256']:raise Problem('TASK_INPUT_CHANGED','Queued template changed')
        delivery=delivery_id(self.task['lesson_id'],frozen,self.inputs['template_sha256'])
        self.context.emit('Checking saved progress')
        if 'profile_ref' not in self.saved:
            self.save(profile_ref=self.library.objects.put_json(run_job('calibrate',{'template':str(template)})))
            return 'QUEUED'
        profile=self.library.objects.read_json(self.saved['profile_ref'])
        if profile['template_sha256']!=self.inputs['template_sha256']:raise Problem('TASK_INPUT_CHANGED','Template profile mismatch')
        archive=Archive(self.library)
        pending=self.db.one("SELECT * FROM artifacts WHERE task_id=? AND state!='AVAILABLE' ORDER BY created LIMIT 1",(self.task_id,))
        if pending:
            if not archive.recover_verified(pending,self.task_id,self.fence,lesson):
                archive.render(pending,self.task_id,self.fence,lesson,frozen,template,profile,stage=self.context.emit)
            return self.completion()
        # A later explicit request may finish a previously discovered PDF. It
        # remains credited to its original request and retains its original
        # number/batch; it never satisfies this request's new-variant count.
        abandoned=self.db.one("SELECT a.* FROM artifacts a JOIN tasks t ON t.id=a.task_id WHERE a.lesson_id=? AND a.delivery_id=? AND a.state!='AVAILABLE' AND t.status IN ('CANCELLED','FAILED','NEEDS_ATTENTION') ORDER BY a.created LIMIT 1",(self.task['lesson_id'],delivery))
        if abandoned:
            if not archive.recover_verified(abandoned,self.task_id,self.fence,lesson):
                archive.render(abandoned,self.task_id,self.fence,lesson,frozen,template,profile,stage=self.context.emit)
            return 'QUEUED'
        history=archive.history(self.task['family_id'])
        rejected=self.db.all('SELECT layout,reason FROM representation_rejections WHERE family_id=? AND delivery_id=?',(self.task['family_id'],delivery))
        rejected=[{'layout':json.loads(r['layout']),'reason':r['reason']} for r in rejected]
        previous=self.db.one('SELECT * FROM exhaustion WHERE family_id=? AND delivery_id=?',(self.task['family_id'],delivery))
        if previous and previous['history_hash']==digest(history):return 'EXHAUSTED'
        def reject(item):
            with self.db.transaction() as c:
                require_fence(c,self.task_id,self.fence)
                c.execute('INSERT OR IGNORE INTO representation_rejections VALUES(?,?,?,?,?)',
                    (self.task['family_id'],delivery,digest(item['layout']),encode(item['layout']),item['reason']))
        prefs=self.library.preferences();policy=self.library.config.resources
        try:
            layout,evidence=next_structure(lesson,frozen,profile,history,rejected,seconds=policy.search_seconds,
                threads=min(prefs['threads_per_search'],policy.cpu_threads),seed=37+self.task['attempt'],
                cancel=self.context.cancelled,rejected=reject,stage=self.context.emit)
        except Problem as exc:
            if exc.code not in ('CANCELLED','PAUSED_BY_USER','ATTEMPT_EXPIRED'):
                self.context.check()
                ref=self.library.objects.put_json(exc.details)
                self.save(search_evidence=ref)
                exc.details={'search_evidence':ref,'status':exc.code}
            raise
        self.save(search_evidence=self.library.objects.put_json(evidence))
        if layout is None:
            with self.db.transaction() as c:
                require_fence(c,self.task_id,self.fence)
                pending=one(c,"SELECT s.id FROM structures s JOIN families f ON f.answer_set_id=s.answer_set_id WHERE f.id=? AND s.state!='AVAILABLE' LIMIT 1",(self.task['family_id'],))
                if pending:raise Problem('PENDING_VARIANTS','Previously discovered variants must be completed before exhaustion can be declared')
                c.execute('INSERT INTO exhaustion VALUES(?,?,?,?,?) ON CONFLICT(family_id,delivery_id) DO UPDATE SET history_hash=excluded.history_hash,evidence=excluded.evidence,created=excluded.created',
                    (self.task['family_id'],delivery,digest(history),encode(evidence),time.time()))
            return 'EXHAUSTED'
        artifact=archive.reserve(self.task_id,self.fence,lesson,layout,delivery)
        # Rendering receives its own document-slot reservation in the next
        # durable slice. Search workers never consume an unreserved parser slot.
        return 'QUEUED'

    def completion(self):
        task=self.db.one('SELECT target_type,target_count,completed FROM tasks WHERE id=?',(self.task_id,))
        return 'COMPLETED' if task['target_type']=='count' and task['completed']>=task['target_count'] else 'PARTIALLY_COMPLETED'

    def restore(self):
        self.context.emit('Restoring exports')
        artifacts=self.db.all("SELECT * FROM artifacts WHERE lesson_id=? AND state='AVAILABLE' ORDER BY created",(self.task['lesson_id'],))
        done=set(self.saved.get('restored',[]));failures=[]
        for artifact in artifacts:
            self.context.check()
            if artifact['id'] in done:continue
            result=restore_one(self.library,artifact)
            if result['status']!='AVAILABLE':failures.append(result)
            else:done.add(artifact['id'])
            self.save(restored=sorted(done))
        self.context.emit('Exports restored' if not failures else 'Export conflict',{'restored':len(done),'issue_count':len(failures),'issues':failures[:100]})
        return 'COMPLETED' if not failures else 'NEEDS_ATTENTION'

    def reverify(self):
        from ..domain import FrozenClues
        from ..pdf import verify_pdf
        from .identity import restore_layout
        artifact=self.db.one("SELECT * FROM artifacts WHERE id=? AND state='AVAILABLE'",(self.inputs['artifact_id'],))
        if not artifact:raise Problem('NOT_FOUND','Saved PDF not found')
        old=self.library.objects.read_json(artifact['report'])
        lesson=Lesson.model_validate(self.library.objects.read_json(old['lesson_snapshot']))
        frozen=FrozenClues.model_validate(self.library.objects.read_json(old['frozen_snapshot']))
        saved=self.db.one('SELECT layout FROM structures WHERE id=?',(artifact['structure_id'],))
        layout=restore_layout(lesson,json.loads(saved['layout']))
        template=self.library.objects.path(old['template_object']);profile=self.library.objects.read_json(old['profile_object'])
        path=self.library.objects.path(artifact['path'])
        if sha256(path)!=artifact['sha256']:raise Problem('ARTIFACT_CHANGED','Saved PDF integrity check failed')
        self.context.emit('Validating saved PDF')
        report=verify_pdf(template,path,lesson,layout,frozen,profile,expected_name=path.name)
        self.save(reverification=self.library.objects.put_json(report))
        self.context.emit('Saved PDF verified')
        return 'COMPLETED'
