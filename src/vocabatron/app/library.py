"""One owner, immutable source bindings, versioned preferences and lazy requests."""
from __future__ import annotations
import json
import os
from pathlib import Path
import time
import uuid
from pydantic import BaseModel, ConfigDict, Field, model_validator
from ..domain import Lesson, FrozenClues, Problem, digest
from ..storage import PrivateStore, sha256
from .database import Database, encode, one, rows, task_record, insert_task, require_fence
from .objects import Objects
from .identity import identities

MAX_COUNT=9007199254740991  # Exact shared JSON/JavaScript integer representation.


class Target(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    lesson_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    mode: str = Field(pattern=r'^(count|all)$')
    count: int | None = Field(default=None,ge=1,le=MAX_COUNT)

    @model_validator(mode='after')
    def mode_count(self):
        if (self.mode=='count') != (self.count is not None):
            raise ValueError('Count mode needs a positive count; all mode has no count')
        return self


class GenerateRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    idempotency_key: str = Field(pattern=r'^[A-Za-z0-9_-]{12,80}$')
    targets: list[Target] = Field(min_length=1,max_length=256)

    @model_validator(mode='after')
    def unique(self):
        if len({t.lesson_id for t in self.targets})!=len(self.targets):raise ValueError('A lesson can appear only once')
        return self


class Library:
    def __init__(self,config):
        self.config=config;self.db=Database(config.database)
        self.objects=Objects(config.data_root/'objects')

    def initialize(self):
        self.db.initialize()

    def source(self,path,name,*,task=True):
        """Input is an already bounded server-owned upload, never a browser path."""
        path=Path(path);info=path.stat()
        if not 0<info.st_size<=self.config.upload_max_bytes:raise Problem('UPLOAD_SIZE','The document exceeds the upload size limit')
        with path.open('rb') as stream:
            if b'%PDF-' not in stream.read(1024):raise Problem('NOT_PDF','The uploaded bytes are not a PDF')
        # The raw upload has already been streamed to disk. A bounded copy is
        # stored independently; none of its PDF structures are parsed here.
        reference,key=self.objects.put_file(path,max_bytes=self.config.upload_max_bytes)
        name=Path(name.replace('\\','/')).name[:240] or 'Document.pdf'
        with self.db.transaction() as c:
            saved=one(c,'SELECT * FROM sources WHERE id=?',(key,))
            if saved:
                existing=one(c,"SELECT id,status FROM tasks WHERE kind='import' AND json_extract(input_json,'$.source_id')=? ORDER BY created DESC LIMIT 1",(key,))
                return {'source_id':key,'reused':True,'status':saved['status'],'task':existing}
            c.execute('INSERT INTO sources(id,path,name,bytes,status,created) VALUES(?,?,?,?,?,?)',
                      (key,reference,name,info.st_size,'QUEUED' if task else 'READY',time.time()))
            result={'source_id':key,'reused':False,'status':'QUEUED' if task else 'READY'}
            if task:
                record=task_record('import',{'source_id':key},priority=20);insert_task(c,record)
                self.db.event(c,'task',{'id':record['id'],'status':'QUEUED'},record['id']);result['task']={'id':record['id'],'status':'QUEUED'}
        return result

    def add_lesson(self,bundle,source_id,*,extra_binding=None,frozen_ref=None,c=None,fence=None):
        lesson=Lesson.model_validate(bundle['lesson']);ids=identities(lesson)
        lesson_ref=self.objects.put_json(bundle['lesson'])
        coverage_ref=self.objects.put_json(bundle['coverage'])
        transcript_ref=self.objects.put(bundle.get('transcript','').encode(),'.md')[0]
        binding={'source_id':source_id,'lesson_content_id':ids['lesson_content_id'],
                 'source_word_keys':ids['source_word_keys'],'source_lesson':lesson_ref,
                 'coverage':coverage_ref,'transcript':transcript_ref,
                 'pages':sorted({f.page for w in lesson.words for f in w.fields}),**(extra_binding or {})}
        def publish(conn):
            conn.execute('INSERT OR IGNORE INTO answer_sets(id) VALUES(?)',(ids['answer_set_id'],))
            conn.execute('INSERT OR IGNORE INTO families VALUES(?,?,?)',(ids['structure_family_id'],ids['answer_set_id'],encode(ids['rules'])))
            existing=one(conn,'SELECT * FROM lessons WHERE id=?',(ids['lesson_content_id'],))
            if existing and json.loads(existing['canonical'])!=ids['canonical']:
                raise Problem('CONTENT_HASH_CONFLICT','Content identity conflict')
            if not existing:
                conn.execute('INSERT INTO lessons(id,number,family_id,canonical,lesson_json,source_id,word_count,frozen_ref,created) VALUES(?,?,?,?,?,?,?,?,?)',
                    (ids['lesson_content_id'],lesson.lesson,ids['structure_family_id'],encode(ids['canonical']),lesson_ref,source_id,
                     len(lesson.words),encode(frozen_ref) if frozen_ref else None,time.time()))
            elif frozen_ref and not existing['frozen_ref']:
                # Preserve the exact lesson instance to which this first frozen
                # evidence belongs; canonical equality is separately checked.
                conn.execute('UPDATE lessons SET frozen_ref=?,lesson_json=? WHERE id=?',
                             (encode(frozen_ref),lesson_ref,ids['lesson_content_id']))
            conn.execute('INSERT OR IGNORE INTO source_bindings VALUES(?,?,?,?,?)',
                         (digest(binding),ids['lesson_content_id'],source_id,encode(binding),time.time()))
            return {'lesson_id':ids['lesson_content_id'],'reused':existing is not None,'number':lesson.lesson,'words':len(lesson.words)}
        if c is not None:return publish(c)
        with self.db.transaction() as connection:
            if fence:require_fence(connection,*fence)
            return publish(connection)

    def lesson(self,lesson_id):
        row=self.db.one('SELECT * FROM lessons WHERE id=?',(lesson_id,))
        if not row:raise Problem('NOT_FOUND','Lesson not found')
        return row,Lesson.model_validate(self.objects.read_json(row['lesson_json']))

    def frozen(self,reference):
        if not reference:raise Problem('SELECTION_REQUIRED','Clues are not ready yet')
        frozen=FrozenClues.model_validate(self.objects.read_json(reference['object']))
        return frozen,PrivateStore(self.config.data_root/'core')

    def list_lessons(self,query='',offset=0,limit=30,*,archived=False):
        value='%'+query.replace('%','\\%').replace('_','\\_')+'%'
        result=self.db.all("SELECT l.*, (SELECT COUNT(*) FROM artifacts a WHERE a.lesson_id=l.id AND a.state='AVAILABLE') AS variants FROM lessons l WHERE l.archived=? AND (CAST(l.number AS TEXT) LIKE ? ESCAPE '\\' OR COALESCE(l.display_name,'') LIKE ? ESCAPE '\\' OR l.id LIKE ? ESCAPE '\\') ORDER BY l.number,l.created LIMIT ? OFFSET ?",(int(archived),value,value,value,limit,offset))
        for row in result:
            row['content_short_id']=row['id'][:12]
            row['status']='Ready' if row['frozen_ref'] else 'Ready to prepare clues'
            row['sources']=self.db.all('SELECT DISTINCT s.id,s.name,b.binding FROM sources s JOIN source_bindings b ON b.source_id=s.id WHERE b.lesson_id=?',(row['id'],))
            for source in row['sources']:source['pages']=json.loads(source.pop('binding'))['pages']
            for key in ('canonical','lesson_json','frozen_ref'):row.pop(key,None)
        total=self.db.one("SELECT COUNT(*) n FROM lessons WHERE archived=? AND (CAST(number AS TEXT) LIKE ? ESCAPE '\\' OR COALESCE(display_name,'') LIKE ? ESCAPE '\\' OR id LIKE ? ESCAPE '\\')",(int(archived),value,value,value))['n']
        return {'items':result,'offset':offset,'limit':limit,'total':total}

    def preferences(self):
        row=self.db.one('SELECT * FROM preferences WHERE singleton=1')
        return {'version':row['version'],**json.loads(row['value'])}

    def update_preferences(self,value,version):
        permitted={'default_count','theme','background_prepare','search_slots','threads_per_search','document_slots'}
        if set(value)-permitted:raise Problem('INPUT_INVALID','Unknown preference')
        if 'default_count' in value and (type(value['default_count'])!=int or not 1<=value['default_count']<=MAX_COUNT):
            raise Problem('INPUT_INVALID',f'Default count must be an integer from 1 to {MAX_COUNT} (numeric representation limit)')
        if value.get('theme','system') not in ('system','light','dark'):raise Problem('INPUT_INVALID','Unknown theme')
        if 'background_prepare' in value and type(value['background_prepare'])!=bool:raise Problem('INPUT_INVALID','Expected a boolean')
        for key in ('search_slots','threads_per_search','document_slots'):
            if key in value and (type(value[key])!=int or not 1<=value[key]<=getattr(self.config.resources,key)):
                raise Problem('INPUT_INVALID','Requested concurrency exceeds the safe deployment ceiling')
        with self.db.transaction() as c:
            current=one(c,'SELECT * FROM preferences WHERE singleton=1')
            if current['version']!=version:raise Problem('PREFERENCE_CONFLICT','Settings changed on another device. Reload before saving.')
            updated={**json.loads(current['value']),**value}
            c.execute('UPDATE preferences SET value=?,version=version+1 WHERE singleton=1',(encode(updated),))
            self.db.event(c,'preferences',{'version':version+1})
        return {'version':version+1,**updated}

    def submit(self,request):
        if self.config.require_legacy_migration:
            record=self.db.one("SELECT evidence FROM migrations WHERE id='legacy-inventory'")
            if not record or json.loads(record['evidence']).get('status')!='VERIFIED':
                raise Problem('LEGACY_REVIEW_REQUIRED','Historical results must finish migration before new variants can be generated.')
        request=GenerateRequest.model_validate(request)
        payload={'targets':[t.model_dump() for t in sorted(request.targets,key=lambda t:t.lesson_id)]}
        request_hash=digest(payload);rid=uuid.uuid4().hex
        template_ref,template_hash=self.objects.put(self.config.template.read_bytes(),'.pdf')
        with self.db.transaction() as c:
            previous=one(c,'SELECT * FROM requests WHERE idempotency_key=?',(request.idempotency_key,))
            if previous:
                if previous['request_hash']!=request_hash:raise Problem('IDEMPOTENCY_CONFLICT','This submission key belongs to different inputs')
                return {'request_id':previous['id'],'reused':True,'tasks':rows(c,'SELECT id,status FROM tasks WHERE request_id=?',(previous['id'],))}
            c.execute('INSERT INTO requests VALUES(?,?,?,?,?)',(rid,request.idempotency_key,request_hash,encode(payload),time.time()))
            tasks=[]
            for target in request.targets:
                lesson=one(c,'SELECT * FROM lessons WHERE id=? AND archived=0',(target.lesson_id,))
                if not lesson:raise Problem('NOT_FOUND','A selected lesson is unavailable')
                inputs={'lesson_content_id':lesson['id'],'lesson_json':lesson['lesson_json'],
                        'frozen_ref':json.loads(lesson['frozen_ref']) if lesson['frozen_ref'] else None,
                        'template_ref':template_ref,'template_sha256':template_hash,'rules':'delivery-v1'}
                task=task_record('generate',inputs,request_id=rid,lesson_id=lesson['id'],family_id=lesson['family_id'],
                    target_type=target.mode,target_count=target.count,priority=10 if target.mode=='count' else 40)
                insert_task(c,task);tasks.append({'id':task['id'],'status':task['status']})
                self.db.event(c,'task',tasks[-1],task['id'])
            return {'request_id':rid,'reused':False,'tasks':tasks}

    def batch(self,c,task):
        if task['batch_id']:return one(c,'SELECT * FROM batches WHERE id=?',(task['batch_id'],))
        lesson=one(c,'SELECT * FROM lessons WHERE id=?',(task['lesson_id'],))
        number=lesson['next_batch'];length=12
        while length<64 and one(c,'SELECT id FROM lessons WHERE id<>? AND substr(id,1,?)=? LIMIT 1',
                               (lesson['id'],length,lesson['id'][:length])):length+=4
        directory=f'Lesson_{lesson["number"]:03d}__L_{lesson["id"][:length]}__Batch_{number:04d}'
        bid=uuid.uuid4().hex
        c.execute('UPDATE lessons SET next_batch=next_batch+1 WHERE id=?',(lesson['id'],))
        c.execute('INSERT INTO batches VALUES(?,?,?,?,?,?)',(bid,lesson['id'],number,directory,task['id'],time.time()))
        c.execute('UPDATE tasks SET batch_id=? WHERE id=?',(bid,task['id']))
        return one(c,'SELECT * FROM batches WHERE id=?',(bid,))

    def list_tasks(self,offset=0,limit=50):
        result=self.db.all('SELECT t.*, l.number AS lesson_number FROM tasks t LEFT JOIN lessons l ON l.id=t.lesson_id ORDER BY t.created DESC LIMIT ? OFFSET ?',(limit,offset))
        for task in result:
            for key in ('input_json','fence','owner'):task.pop(key,None)
            task['pending_variants']=self.db.one("SELECT COUNT(*) n FROM artifacts WHERE task_id=? AND state!='AVAILABLE'",(task['id'],))['n']
            if task['detail']:
                try:task['detail']=json.loads(task['detail'])
                except ValueError:pass
        return {'items':result,'total':self.db.one('SELECT COUNT(*) n FROM tasks')['n']}

    def control(self,task_id,action):
        if action not in ('pause','resume','cancel','retry'):raise Problem('INPUT_INVALID','Unknown task action')
        with self.db.transaction() as c:
            task=one(c,'SELECT * FROM tasks WHERE id=?',(task_id,))
            if not task:raise Problem('NOT_FOUND','Task not found')
            if task['status'] in ('COMPLETED','EXHAUSTED'):return {'id':task_id,'status':task['status']}
            if action in ('resume','retry'):
                if task['status']=='RUNNING':return {'id':task_id,'status':'RUNNING'}
                if task['status']=='CANCELLED':raise Problem('TASK_CANCELLED','Create a new request to generate further variants')
                status='QUEUED';intent='run'
                if action=='retry':
                    c.execute('UPDATE tasks SET retries=0 WHERE id=?',(task_id,))
                    if task['error_code'] in ('MODEL_SELECTION_INVALID','MODEL_DIGEST_CHANGED','OLLAMA_UNAVAILABLE','MODEL_REQUEST_TIMEOUT'):
                        checkpoint=one(c,'SELECT value FROM task_checkpoints WHERE task_id=?',(task_id,))
                        if checkpoint:
                            saved=json.loads(checkpoint['value'])
                            if not saved.get('frozen_ref'):
                                saved.pop('selection_run_id',None)
                                c.execute('UPDATE task_checkpoints SET value=? WHERE task_id=?',(encode(saved),task_id))
            else:
                intent=action
                status=task['status'] if task['status']=='RUNNING' else ('CANCELLED' if action=='cancel' else 'PAUSED_BY_USER')
            c.execute('UPDATE tasks SET intent=?,status=?,updated=?,eligible=0,error_code=NULL WHERE id=?',(intent,status,time.time(),task_id))
            self.db.event(c,'task',{'id':task_id,'status':status,'intent':intent},task_id)
            return {'id':task_id,'status':status,'intent':intent}

    def results(self,lesson_id,offset=0,limit=20):
        self.lesson(lesson_id)
        values=self.db.all("SELECT a.id,a.filename,a.bytes,a.sha256,a.state,a.batch_id,a.created,s.variant_number,s.crossings,b.number AS batch_number,e.status AS export_status FROM artifacts a JOIN structures s ON s.id=a.structure_id LEFT JOIN batches b ON b.id=a.batch_id LEFT JOIN exports e ON e.artifact_id=a.id WHERE a.lesson_id=? AND a.state='AVAILABLE' ORDER BY s.variant_number,a.created LIMIT ? OFFSET ?",(lesson_id,limit,offset))
        for value in values:value['crossing_count']=len(json.loads(value.pop('crossings')));value['pages']=2
        return {'items':values,'offset':offset,'limit':limit,'total':self.db.one("SELECT COUNT(*) n FROM artifacts WHERE lesson_id=? AND state='AVAILABLE'",(lesson_id,))['n']}
