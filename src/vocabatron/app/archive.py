"""Reserved structures and independently verified per-PDF publication."""
from __future__ import annotations
import json
from pathlib import Path
import time
import uuid
import apsw
from ..domain import Problem, digest
from ..pdf import calibrate,export_pdf,verify_pdf
from ..storage import sha256,private_mkdir
from ..validation import validate_layout,public_structure_summary
from .database import one,rows,encode,require_fence
from .identity import identities,structure,restore_layout,DEDUPE_VERSION
from .exports import restore_one


def delivery_id(lesson_id,frozen,template_hash):
    import reportlab
    from ..requests import OUTPUT_RULES
    return digest({'version':'variant-delivery-v1','lesson_content_id':lesson_id,'frozen_version':frozen.version,
                   'template_sha256':template_hash,'font_sha256':sha256(Path(reportlab.__file__).parent/'fonts'/'Vera.ttf'),
                   'output_rules':OUTPUT_RULES,'layout_rules':'forward-connected-v1'})


class Archive:
    def __init__(self,library):self.library=library;self.db=library.db

    def history(self,family_id):
        result=self.db.all('SELECT s.* FROM structures s JOIN families f ON f.answer_set_id=s.answer_set_id WHERE f.id=? ORDER BY s.variant_number',(family_id,))
        for item in result:
            for key in ('crossings','geometry','layout'):item[key]=json.loads(item[key])
        return result

    def reserve(self,task_id,fence,lesson,layout,delivery):
        info=structure(lesson,layout);ids=identities(lesson,layout.size)
        with self.db.transaction() as c:
            task=require_fence(c,task_id,fence)
            if task['family_id']!=ids['structure_family_id']:raise Problem('TASK_INPUT_CHANGED','Task and structure family differ')
            if one(c,'SELECT id FROM structures WHERE answer_set_id=? AND (crossing_hash=? OR geometry_hash=?)',
                   (ids['answer_set_id'],info['crossing_hash'],info['geometry_hash'])):
                raise Problem('HISTORICAL_DUPLICATE','This structure already exists in the archive')
            number=one(c,'SELECT next_variant FROM answer_sets WHERE id=?',(ids['answer_set_id'],))['next_variant']
            c.execute('UPDATE answer_sets SET next_variant=next_variant+1 WHERE id=?',(ids['answer_set_id'],))
            sid=uuid.uuid4().hex;aid=uuid.uuid4().hex
            c.execute('INSERT INTO structures VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (sid,ids['structure_family_id'],ids['answer_set_id'],number,info['crossing_hash'],info['geometry_hash'],
                 encode(info['crossings']),encode(info['geometry']),encode(info['layout']),DEDUPE_VERSION,'DISCOVERED',task_id,time.time()))
            batch=self.library.batch(c,task)
            filename=f'Variant_{number:03d}_Lesson_{lesson.lesson}_Crossword.pdf'
            now=time.time()
            c.execute('INSERT INTO artifacts(id,structure_id,lesson_id,delivery_id,task_id,batch_id,filename,state,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (aid,sid,task['lesson_id'],delivery,task_id,batch['id'],filename,'RESERVED',now,now))
            c.execute('INSERT INTO exports VALUES(?,?,?,?,?,?)',(aid,batch['id'],filename,'PENDING',None,now))
            self.db.event(c,'variant',{'id':aid,'state':'RESERVED','variant_number':number},task_id)
            return one(c,'SELECT * FROM artifacts WHERE id=?',(aid,))

    def render(self,artifact,task_id,fence,lesson,frozen,template,profile,*,stage=None):
        stage=stage or (lambda *args:None)
        saved=self.db.one('SELECT * FROM structures WHERE id=?',(artifact['structure_id'],))
        layout=restore_layout(lesson,json.loads(saved['layout']))
        # Each attempt owns a new staging directory. Failed attempts never
        # overwrite an old PDF, and the discovered structure remains reserved.
        directory=self.library.config.runtime_root/'renders'/f'{task_id}-{fence}-{uuid.uuid4().hex}'
        private_mkdir(directory);output=directory/artifact['filename']
        stage('Rendering PDF')
        with self.db.transaction() as c:
            require_fence(c,task_id,fence)
            c.execute("UPDATE artifacts SET state='RENDERING',updated=? WHERE id=?",(time.time(),artifact['id']))
        started=time.perf_counter();export_pdf(template,output,lesson,layout,frozen,profile)
        export_seconds=time.perf_counter()-started
        stage('Validating PDF')
        with self.db.transaction() as c:
            require_fence(c,task_id,fence)
            c.execute("UPDATE artifacts SET state='VALIDATING',updated=? WHERE id=?",(time.time(),artifact['id']))
        started=time.perf_counter()
        report=verify_pdf(template,output,lesson,layout,frozen,profile,expected_name=artifact['filename'])
        report['structure']=public_structure_summary(validate_layout(lesson,layout))
        report['export_seconds']=export_seconds;report['validation_seconds']=time.perf_counter()-started
        report['delivery_id']=artifact['delivery_id'];report['layout_sha256']=digest(layout)
        from .. import __version__
        from ..services import dependencies
        report['generator_version']=__version__;report['dependencies']=dependencies()
        source_root=self.library.config.code_root/'src/vocabatron'
        report['generator_source_sha256']=digest([(str(p.relative_to(source_root)),sha256(p)) for p in sorted(source_root.rglob('*.py'))])
        report['selection_evidence']={'schema':frozen.schema_version,'run_id':frozen.selection_run_id,
                                      'evidence_sha256':frozen.evidence_sha256,'model_digest':frozen.model_digest}
        report['lesson_snapshot']=self.library.objects.put_json(lesson.model_dump(mode='json'))
        report['frozen_snapshot']=self.library.objects.put_json(frozen.model_dump(mode='json'))
        report['template_sha256']=sha256(template);report['template_object']=self.library.objects.put_file(template,max_bytes=self.library.config.upload_max_bytes)[0]
        report['profile_object']=self.library.objects.put_json(profile);report['manual_review']='NOT_PERFORMED'
        reference,file_hash=self.library.objects.put(output.read_bytes(),'.pdf')
        if file_hash!=report['sha256']:raise Problem('ARTIFACT_CHANGED','Export changed after validation')
        report_ref=self.library.objects.put_json(report)
        # Persist a recoverable verified checkpoint before the DB completion.
        checkpoint=self.library.config.data_root/'publication'/f'{artifact["id"]}.json'
        from ..storage import PrivateStore
        PrivateStore(self.library.config.data_root).json(checkpoint.relative_to(self.library.config.data_root),
            {'artifact_id':artifact['id'],'path':reference,'sha256':file_hash,'bytes':output.stat().st_size,
             'report':report_ref,'delivery_id':artifact['delivery_id'],'layout_sha256':digest(layout)})
        with self.db.transaction() as c:
            task=require_fence(c,task_id,fence)
            current=one(c,'SELECT * FROM artifacts WHERE id=?',(artifact['id'],))
            if current['state']!='AVAILABLE':
                c.execute("UPDATE artifacts SET state='AVAILABLE',path=?,sha256=?,bytes=?,report=?,updated=? WHERE id=?",
                          (reference,file_hash,output.stat().st_size,report_ref,time.time(),artifact['id']))
                c.execute("UPDATE structures SET state='AVAILABLE' WHERE id=?",(artifact['structure_id'],))
                c.execute('UPDATE tasks SET completed=completed+1,updated=? WHERE id=?',(time.time(),artifact['task_id']))
                self.db.event(c,'variant',{'id':artifact['id'],'state':'AVAILABLE'},task_id)
        published=self.db.one('SELECT * FROM artifacts WHERE id=?',(artifact['id'],))
        result=restore_one(self.library,published)
        # Only this attempt's transient directory is removed.
        import shutil
        shutil.rmtree(directory)
        return {'artifact_id':artifact['id'],'state':'AVAILABLE','export':result,'report':report}

    def recover_verified(self,artifact,task_id,fence,lesson):
        path=self.library.config.data_root/'publication'/f'{artifact["id"]}.json'
        if not path.exists():return False
        if path.is_symlink() or path.stat().st_size>32*1024:raise Problem('PUBLICATION_RECORD_INVALID','Invalid publication checkpoint')
        value=json.loads(path.read_bytes())
        if value['artifact_id']!=artifact['id'] or value['delivery_id']!=artifact['delivery_id']:
            raise Problem('PUBLICATION_RECORD_INVALID','Publication checkpoint belongs to different inputs')
        report=self.library.objects.read_json(value['report'])
        if report['sha256']!=value['sha256'] or report['delivery_id']!=artifact['delivery_id']:
            raise Problem('PUBLICATION_RECORD_INVALID','Verification evidence mismatch')
        import os
        fd=self.library.objects.open_verified(value['path'],value['sha256'])
        try:
            if os.fstat(fd).st_size!=value['bytes']:raise Problem('PUBLICATION_RECORD_INVALID','Saved PDF size differs from its checkpoint')
        finally:os.close(fd)
        saved=self.db.one('SELECT layout FROM structures WHERE id=?',(artifact['structure_id'],))
        layout=restore_layout(lesson,json.loads(saved['layout']))
        if digest(layout)!=value['layout_sha256']:raise Problem('PUBLICATION_RECORD_INVALID','Saved layout changed')
        with self.db.transaction() as c:
            require_fence(c,task_id,fence)
            if one(c,'SELECT state FROM artifacts WHERE id=?',(artifact['id'],))['state']!='AVAILABLE':
                c.execute("UPDATE artifacts SET state='AVAILABLE',path=?,sha256=?,bytes=?,report=?,updated=? WHERE id=?",
                    (value['path'],value['sha256'],value['bytes'],value['report'],time.time(),artifact['id']))
                c.execute("UPDATE structures SET state='AVAILABLE' WHERE id=?",(artifact['structure_id'],))
                c.execute('UPDATE tasks SET completed=completed+1 WHERE id=?',(artifact['task_id'],))
        restore_one(self.library,self.db.one('SELECT * FROM artifacts WHERE id=?',(artifact['id'],)))
        return True
