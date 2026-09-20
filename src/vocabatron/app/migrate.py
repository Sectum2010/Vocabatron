"""Read-only legacy discovery and idempotent import into the new private archive."""
from __future__ import annotations
import json
import os
from pathlib import Path
import time
import uuid
from ..domain import Problem,digest
from ..storage import PrivateStore,sha256,private_mkdir
from ..services import _read_result
from ..pdf import verify_pdf
from ..evidence import verify_evidence
from .database import one,encode,task_record,insert_task
from .identity import identities,structure,DEDUPE_VERSION
from .exports import restore_one


def copy_evidence(library,store,lesson,frozen):
    relative=Path('model/runs')/frozen.selection_run_id if frozen.schema_version==2 else Path('model')/lesson.version
    source=store.path(relative);target=library.config.data_root/'core'/relative
    if source.is_dir():
        for path in source.rglob('*'):
            if not path.is_file() or path.is_symlink():continue
            if path.stat().st_size>32*1024**2:raise Problem('RESOURCE_LIMIT','Legacy evidence exceeds its size limit')
            dest=target/path.relative_to(source);private_mkdir(dest.parent)
            if dest.exists():
                if sha256(dest)!=sha256(path):raise Problem('IMMUTABLE_CONFLICT','Legacy evidence identity conflict')
            else:
                with path.open('rb') as incoming, dest.open('xb') as out:
                    while chunk:=incoming.read(1024*1024):out.write(chunk)
                    out.flush();os.fsync(out.fileno())
                dest.chmod(0o600)
    return verify_evidence(PrivateStore(library.config.data_root/'core'),lesson,frozen)


def migrate(library,source_paths,*,progress=lambda value:None):
    """Caller must own resource admission. Only explicitly known real inputs match.

    All result roots under legacy are considered, including prior acceptance
    domains. Source bytes, snapshot hashes, layout and PDF verification establish
    provenance; a directory name alone is never evidence of success.
    """
    known={sha256(Path(p)):Path(p) for p in source_paths}
    expected_template=sha256(library.config.template);candidates=[];unresolved=[];imported=[]
    for path in sorted(library.config.legacy_root.rglob('results/*/manifest.json')):
        if path.is_relative_to(library.config.data_root) or path.is_symlink():continue
        if path.stat().st_size>1024**2:unresolved.append({'path':str(path),'code':'MANIFEST_SIZE'});continue
        value=json.loads(path.read_bytes())
        if value.get('source_sha256') not in known:continue
        # Known real data with incomplete/failed evidence must be reported, not
        # silently removed from the historical uniqueness claim.
        if value.get('status')!='VERIFIED' or value.get('template_sha256')!=expected_template:
            unresolved.append({'path':str(path),'code':'LEGACY_PROVENANCE_UNCONFIRMED'});continue
        candidates.append((value.get('schema_version',1),path))
    for _,path in sorted(candidates,key=lambda value:(-value[0],str(value[1]))):
        key=digest({'manifest_path':str(path),'sha256':sha256(path)})
        previous=library.db.one('SELECT id FROM migrations WHERE id=?',(key,))
        if previous:continue
        store=PrivateStore(path.parent.parent.parent);task_name=path.parent.name
        try:
            root,manifest,lesson,frozen,layouts=_read_result(store,task_name,allow_legacy_missing_source=True)
            original=known[manifest['source_sha256']]
            provenance=verify_evidence(store,lesson,frozen)
            profile=store.read(root/'template-profile.json');template=store.path(root/'template.pdf')
            reports=[]
            for item,layout in zip(manifest['pdfs'],layouts):
                pdf=store.path(root/item['name'])
                if sha256(pdf)!=item['sha256']:raise Problem('PDF_CHANGED','Legacy PDF hash mismatch')
                progress({'stage':'Validating legacy PDF','source_manifest':str(path)})
                report=verify_pdf(template,pdf,lesson,layout,frozen,profile,expected_name=item['name'])
                reports.append(report)
            evidence=copy_evidence(library,store,lesson,frozen)
            source=library.source(original,original.name,task=False)
            lesson_ref=library.objects.put_json(lesson.model_dump(mode='json'))
            frozen_ref={'object':library.objects.put_json(frozen.model_dump(mode='json')),'lesson_object':lesson_ref,
                        'selection_run_id':frozen.selection_run_id,'evidence_status':evidence['status']}
            saved=library.add_lesson({'lesson':lesson.model_dump(mode='json'),'coverage':{'status':'VERIFIED','legacy_manifest':str(path),'sha256':sha256(path)},
                'transcript':'\n\n'.join(f.raw for w in lesson.words for f in w.fields)},source['source_id'],frozen_ref=frozen_ref,
                extra_binding={'legacy_manifest':str(path),'legacy_manifest_sha256':sha256(path)})
            ids=identities(lesson);pending=[]
            for item,layout,report in zip(manifest['pdfs'],layouts,reports):
                info=structure(lesson,layout);pdf=store.path(root/item['name'])
                reference,file_hash=library.objects.put_file(pdf,max_bytes=library.config.upload_max_bytes)
                report.update({'lesson_snapshot':lesson_ref,'frozen_snapshot':frozen_ref['object'],
                    'template_object':library.objects.put_file(template,max_bytes=library.config.upload_max_bytes)[0],
                    'profile_object':library.objects.put_json(profile),'legacy_manifest':str(path),'legacy_manifest_sha256':sha256(path),
                    'legacy_pdf_sha256':file_hash,'legacy_manifest_object':library.objects.put(path.read_bytes(),'.json')[0],
                    'model_evidence':provenance,
                    'historical_generator_source':manifest.get('generator_source_sha256') or 'NOT_RECORDED_IN_LEGACY_MANIFEST'})
                pending.append((info,reference,file_hash,library.objects.put_json(report),pdf.stat().st_size,item['name']))
            with library.db.transaction() as c:
                if one(c,'SELECT id FROM migrations WHERE id=?',(key,)):continue
                task=task_record('migration',{'manifest':str(path)},lesson_id=saved['lesson_id'],family_id=ids['structure_family_id'])
                task.update(status='COMPLETED',stage='Legacy results verified');insert_task(c,task)
                task={**task,'batch_id':None};batch=None;artifact_ids=[]
                for info,reference,file_hash,report_ref,size,old_name in pending:
                    existing=one(c,'SELECT * FROM structures WHERE answer_set_id=? AND (crossing_hash=? OR geometry_hash=?)',
                                 (ids['answer_set_id'],info['crossing_hash'],info['geometry_hash']))
                    if existing:sid=existing['id'];number=existing['variant_number']
                    else:
                        number=one(c,'SELECT next_variant FROM answer_sets WHERE id=?',(ids['answer_set_id'],))['next_variant'];sid=uuid.uuid4().hex
                        c.execute('UPDATE answer_sets SET next_variant=next_variant+1 WHERE id=?',(ids['answer_set_id'],))
                        c.execute('INSERT INTO structures VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(sid,ids['structure_family_id'],ids['answer_set_id'],number,
                            info['crossing_hash'],info['geometry_hash'],encode(info['crossings']),encode(info['geometry']),encode(info['layout']),DEDUPE_VERSION,'AVAILABLE',task['id'],time.time()))
                    delivery='legacy-'+file_hash
                    artifact=one(c,'SELECT id FROM artifacts WHERE structure_id=? AND lesson_id=? AND sha256=?',(sid,saved['lesson_id'],file_hash))
                    if artifact:artifact_ids.append(artifact['id']);continue
                    if batch is None:batch=library.batch(c,task)
                    aid=uuid.uuid4().hex;filename=f'Variant_{number:03d}_Lesson_{lesson.lesson}_Crossword.pdf';now=time.time()
                    # A different rendering of one historical structure gets its
                    # own batch if a filename would collide. It never gets a new
                    # structure number or generation completion credit.
                    if one(c,'SELECT id FROM artifacts WHERE batch_id=? AND filename=?',(batch['id'],filename)):
                        c.execute('UPDATE tasks SET batch_id=NULL WHERE id=?',(task['id'],));batch=library.batch(c,task)
                    c.execute('INSERT INTO artifacts(id,structure_id,lesson_id,delivery_id,task_id,batch_id,filename,path,sha256,bytes,state,report,legacy_ref,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (aid,sid,saved['lesson_id'],delivery,task['id'],batch['id'],filename,reference,file_hash,size,'AVAILABLE',report_ref,encode({'manifest':str(path),'name':old_name,'sha256':file_hash}),now,now))
                    c.execute('INSERT INTO exports VALUES(?,?,?,?,?,?)',(aid,batch['id'],filename,'PENDING',None,now));artifact_ids.append(aid)
                c.execute('INSERT INTO migrations VALUES(?,?,?,?)',(key,str(path),encode({'manifest_sha256':sha256(path),'artifact_ids':artifact_ids,'model_evidence':provenance}),time.time()))
                library.db.event(c,'library',{'lesson_id':saved['lesson_id']})
            for aid in artifact_ids:restore_one(library,library.db.one('SELECT * FROM artifacts WHERE id=?',(aid,)))
            imported.append({'manifest':str(path),'artifact_ids':artifact_ids})
        except (Problem,ValueError,KeyError,OSError) as exc:
            unresolved.append({'path':str(path),'code':exc.code if isinstance(exc,Problem) else type(exc).__name__})
    report={'imported':imported,'unresolved':unresolved,'unique_structures':library.db.one('SELECT count(*) n FROM structures')['n'],
            'model_calls':0,'solver_calls':0,'generation_render_calls':0,'pdf_validation':'Independent dual-renderer validation',
            'status':'VERIFIED' if not unresolved else 'NEEDS_ATTENTION'}
    reference=library.objects.put_json(report)
    with library.db.transaction() as c:
        c.execute("INSERT INTO migrations VALUES('legacy-inventory',?,?,?) ON CONFLICT(id) DO UPDATE SET source=excluded.source,evidence=excluded.evidence,created=excluded.created",
                  (str(library.config.legacy_root),encode({'status':report['status'],'report_object':reference}),time.time()))
    return {**report,'report_object':reference}
