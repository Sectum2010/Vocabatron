"""Versioned import ladder and immutable diagnostics for every bounded attempt."""
from __future__ import annotations
import json
import time
from ..documents import PARSER_VERSION,positioned_blocks,compact
from ..domain import Problem
from ..storage import sha256
from ..supervisor import run_job
from ..vocabulary import assemble,events
from ..ocr import transformed_box
from .database import require_fence,encode,one,insert_task,task_record


def quality(result):
    return (sum(len(b['lesson']['words']) for b in result['lessons'] if b['coverage']['status']=='VERIFIED'),
            sum(len(b['lesson']['words']) for b in result['lessons']),
            -sum(len(b['coverage']['issues']) for b in result['lessons'])-len(result['unresolved_sections']))


def preserve_native(native,ocr):
    """OCR can supply missing image regions and grouping, never replace native letters."""
    if not any(compact(t['text']) for t in native['tokens']):return ocr
    transform=ocr['ocr_evidence'];tokens=[];additions=[]
    for token in native['tokens']:
        box,_=transformed_box(token['bbox'],transform)
        tokens.append({**token,'bbox':box,'source_bbox':token['bbox']})
    for candidate in ocr['tokens']:
        x0,y0,x1,y1=candidate['bbox']
        overlap=[t for t in tokens if compact(t['text']) and
            x0-2<=(t['bbox'][0]+t['bbox'][2])/2<=x1+2 and y0-2<=(t['bbox'][1]+t['bbox'][3])/2<=y1+2]
        if not overlap:additions.append({**candidate,'id':candidate['id']+'ocr'})
    tokens.extend(additions)
    result={**ocr,'tokens':tokens,'blocks':positioned_blocks(tokens),'chars':native['chars'],
            'native_evidence':{'issues':native['issues'],'independent_order':native.get('independent_order')},
            'issues':[i for i in native['issues'] if i['code'] not in ('NO_TEXT_LAYER','IMAGE_REGION_NEEDS_OCR')],
            'character_count':native['character_count']+sum(len(t['text']) for t in additions)}
    result['events']=events(result)
    return result


def import_document(job):
    started=time.monotonic()
    try:return _import_document(job)
    except Problem as exc:
        # A fixed document limit cannot improve merely by waiting for CPU.
        # Admission pauses and explicit cancellation retain their checkpoint.
        if exc.code!='RESOURCE_LIMIT':raise
        task=job.db.one('SELECT * FROM tasks WHERE id=?',(job.task_id,))
        timings={**job.saved.get('import_timings',{}),'processing_seconds':time.monotonic()-started,
            'resource_wait_seconds':task['resource_wait_seconds'],
            'queue_wait_seconds':task['queue_wait_seconds'] or 0,
            'total_elapsed_seconds':time.time()-task['created']}
        issue={'code':'DOCUMENT_RESOURCE_LIMIT','stage':job.context.stage,'message':exc.message}
        evidence={'status':'RECOVERY_UNAVAILABLE','parser_version':PARSER_VERSION,
            'lessons':[],'unresolved_sections':[issue],'recovery_attempts':[
                *job.saved.get('recovery_attempts',[]),{'backend':'document-worker',**issue,'diagnostic':exc.details}]}
        full=job.library.objects.put_json(evidence)
        report=job.library.objects.put_json({**evidence,'full_report':full,'timings':timings})
        with job.db.transaction() as c:
            require_fence(c,job.task_id,job.fence)
            c.execute('UPDATE sources SET status=?,report=?,parser_version=? WHERE id=?',
                ('RECOVERY_UNAVAILABLE',report,PARSER_VERSION,job.inputs['source_id']))
            c.execute('UPDATE source_attempts SET report=?,timings=? WHERE task_id=?',(report,encode(timings),job.task_id))
            job.db.event(c,'library',{'source_id':job.inputs['source_id'],'status':'RECOVERY_UNAVAILABLE'},job.task_id)
        job.context.emit('Recovery unavailable',{'message':'The document exceeded a configured safety limit. View import details.'})
        return 'FAILED'


def _import_document(job):
    library=job.library;db=job.db;objects=library.objects
    source=db.one('SELECT * FROM sources WHERE id=?',(job.inputs['source_id'],));path=objects.path(source['path'])
    if sha256(path)!=source['id']:raise Problem('SOURCE_CHANGED','The uploaded source changed')
    with db.transaction() as c:
        require_fence(c,job.task_id,job.fence)
        c.execute('INSERT OR IGNORE INTO source_attempts(task_id,source_id,parser_version,previous_report,created) VALUES(?,?,?,?,?)',
            (job.task_id,source['id'],PARSER_VERSION,source['report'],time.time()))
    if job.saved.get('parser_version')!=PARSER_VERSION:
        job.save(parser_version=PARSER_VERSION,legacy_page_chunks=job.saved.get('page_chunks',[]),page_chunks=[],
            recovery_attempts=[],recovered_pages={},positioned_recovery_done=False,import_timings={})
    timings=job.saved.get('import_timings',{});attempts=job.saved.get('recovery_attempts',[])
    def stage(name,key,call,detail=None):
        job.context.emit(name,detail);begin=time.monotonic()
        try:return call()
        finally:timings[key]=timings.get(key,0)+time.monotonic()-begin
    def parse(pages):
        metric={};begin=time.monotonic()
        job.context.emit('Finding vocabulary entries',{'pages_done':len(pages),'pages_total':count})
        result=assemble(source['id'],pages,total_pages=count,metrics=metric)
        timings['validation_seconds']=timings.get('validation_seconds',0)+metric['validation_seconds']
        timings['semantic_seconds']=timings.get('semantic_seconds',0)+time.monotonic()-begin-metric['validation_seconds']
        return result
    if 'document_info' not in job.saved:
        info=stage('Checking document','preflight_seconds',lambda:run_job('inspect',{'source':str(path)}))
        job.save(document_info=info,import_timings=timings)
    count=job.saved['document_info']['pages'];chunks=job.saved.get('page_chunks',[])
    done=sum(len(objects.read_json(ref)['pages']) for ref in chunks)
    while done<count:
        numbers=list(range(done+1,min(done+8,count)+1))
        extracted=stage('Reading native text','native_seconds',lambda:run_job('book_pages',{'source':str(path),'page_numbers':numbers}),{'pages_done':done,'pages_total':count})
        if extracted['source_sha256']!=source['id']:raise Problem('SOURCE_CHANGED','Extraction source mismatch')
        chunks.append(objects.put_json(extracted));done+=len(numbers)
        job.save(page_chunks=chunks,import_timings=timings)
    pages=[p for ref in chunks for p in objects.read_json(ref)['pages']]
    original_pages={p['page']:p for p in pages}
    recovered_pages=job.saved.get('recovered_pages',{})
    pages=[objects.read_json(recovered_pages[str(p['page'])]) if str(p['page']) in recovered_pages else p for p in pages]
    if sum(p['character_count'] for p in pages)>library.config.document_limits.characters:raise Problem('RESOURCE_LIMIT','Whole-document character budget exceeded')
    result=parse(pages)
    if result['status']!='READY' and not job.saved.get('positioned_recovery_done'):
        recovered=[]
        for group in range(0,count,8):
            numbers=list(range(group+1,min(group+8,count)+1))
            attempt=stage('Recovering positioned text','positioned_recovery_seconds',lambda:run_job('book_pages',{'source':str(path),'page_numbers':numbers,'use_tables':False}),{'pages_done':group,'pages_total':count})
            attempts.append({'backend':'native-positioned','object':objects.put_json(attempt)})
            recovered.extend(attempt['pages'])
        recovered_result=parse(recovered)
        if quality(recovered_result)>quality(result):pages,result=recovered,recovered_result
        recovered_pages.update({str(p['page']):objects.put_json(p) for p in pages})
        job.save(positioned_recovery_done=True,recovered_pages=recovered_pages,recovery_attempts=attempts,import_timings=timings)
    if result['status']!='READY':
        bad_pages={p for bundle in result['lessons'] if bundle['coverage']['status']!='VERIFIED' for p in bundle['coverage']['pages']}
        bad_pages.update(i['page'] for i in result['unresolved_sections'] if i.get('page'))
        if not bad_pages:bad_pages={p['page'] for p in pages if not p.get('blank')}
        # Re-reading an exact, independently matched native answer cannot fix
        # a repeated answer or an answer beyond the unchanged crossword rules.
        fixed_codes={'DUPLICATE_WORD','SINGLE_LETTER_REQUIRES_DECISION','WORD_TOO_LONG','ANSWER_CHARACTERS_REQUIRE_DECISION'}
        for p in pages:
            bundles=[b for b in result['lessons'] if p['page'] in b['coverage']['pages']]
            if bundles and not p['issues'] and not p.get('ocr_used') and not any(i.get('page')==p['page'] for i in result['unresolved_sections']):
                if all(all(i['code'] in fixed_codes for i in b['coverage']['issues']) for b in bundles):bad_pages.discard(p['page'])
        for index,selected in enumerate(list(pages)):
            native=original_pages[selected['page']]
            if native['page'] not in bad_pages:continue
            for operation,variant in [('ocr_page',0),('ocr_page',1),('ocr_page',2),('structured_page',0)]:
                key=f'{operation}:{native["page"]}:{variant}'
                if any(a.get('attempt_key')==key for a in attempts):continue
                name='Reading scanned text' if operation=='ocr_page' else 'Checking document structure locally'
                try:
                    page=stage(name,'ocr_seconds',lambda:run_job(operation,{'source':str(path),'page_numbers':[native['page']],'variant':variant}),
                        {'pages_done':index,'pages_total':count,'recovery_step':variant+1})
                    attempts.append({'attempt_key':key,'backend':page['backend'],'page':native['page'],'variant':variant,'object':objects.put_json(page)})
                    page=preserve_native(native,page)
                    proposed=[page if p['page']==native['page'] else p for p in pages]
                    proposed_result=parse(proposed)
                    if quality(proposed_result)>quality(result):pages,result=proposed,proposed_result
                    recovered_pages[str(native['page'])]=objects.put_json(pages[index])
                    job.save(recovered_pages=recovered_pages,recovery_attempts=attempts,import_timings=timings)
                    if result['status']=='READY':break
                    # A verified course on this page needs no more OCR even
                    # while another, unrelated page still requires recovery.
                    affected=[b for b in result['lessons'] if native['page'] in b['coverage']['pages']]
                    if affected and all(b['coverage']['status']=='VERIFIED' for b in affected) and not any(
                            i.get('page')==native['page'] for i in result['unresolved_sections']):break
                except Problem as exc:
                    if exc.code in ('CANCELLED','PAUSED_BY_USER','PAUSED_FOR_RESOURCES','ATTEMPT_EXPIRED'):raise
                    attempts.append({'attempt_key':key,'backend':operation,'page':native['page'],'variant':variant,'code':exc.code,'diagnostic':exc.details})
                job.save(import_timings=timings,recovery_attempts=attempts)
    job.context.emit('Validating source coverage',{'pages_done':count,'pages_total':count})
    final_evidence=objects.put_json({'parser_version':PARSER_VERSION,'pages':pages,'recovery_attempts':attempts})
    result.update(recovery_attempts=attempts,normalized_evidence=final_evidence)
    complete=objects.put_json(result);published=[];begin=time.monotonic()
    for bundle in result['lessons']:
        if bundle['coverage']['status']!='VERIFIED':continue
        job.context.emit('Adding verified lessons',{'lessons_done':len(published),'lessons_found':len(result['lessons'])})
        published.append(library.add_lesson(bundle,source['id'],extra_binding={'page_evidence':chunks,'normalized_evidence':final_evidence},fence=(job.task_id,job.fence)))
    timings['publication_seconds']=timings.get('publication_seconds',0)+time.monotonic()-begin
    issues=bool(result['unresolved_sections'] or len(published)!=len(result['lessons']) or not published)
    status=('PARTIALLY_IMPORTED' if published else 'NEEDS_ATTENTION') if issues else 'READY'
    infrastructure_failed=issues and any(a.get('code') in ('RESOURCE_LIMIT','OCR_NOT_PREPARED','OCR_MODEL_CHANGED','WORKER_FAILED') for a in attempts)
    if infrastructure_failed and not published:status='RECOVERY_UNAVAILABLE'
    timings['processing_seconds']=sum(timings.get(k,0) for k in ('preflight_seconds','native_seconds','semantic_seconds',
        'validation_seconds','positioned_recovery_seconds','ocr_seconds','publication_seconds'))
    task=db.one('SELECT * FROM tasks WHERE id=?',(job.task_id,))
    timings['resource_wait_seconds']=task['resource_wait_seconds']
    timings['queue_wait_seconds']=task['queue_wait_seconds'] if task['queue_wait_seconds'] is not None else max(0,(task['started_at'] or time.time())-task['created'])
    timings['total_elapsed_seconds']=time.time()-task['created']
    summary={'status':status,'parser_version':PARSER_VERSION,'full_report':complete,'timings':timings,'recovery_attempts':attempts,
        'unresolved_sections':result['unresolved_sections'],'lessons':[{'lesson':{'lesson':b['lesson']['lesson']},
        'coverage':{'status':b['coverage']['status'],'issues':b['coverage']['issues'],'ocr_used':b['coverage']['ocr_used']}} for b in result['lessons']]}
    report_ref=objects.put_json(summary)
    with db.transaction() as c:
        require_fence(c,job.task_id,job.fence)
        c.execute('UPDATE sources SET status=?,pages=?,report=?,parser_version=? WHERE id=?',(status,count,report_ref,PARSER_VERSION,source['id']))
        c.execute('UPDATE source_attempts SET report=?,timings=? WHERE task_id=?',(report_ref,encode(timings),job.task_id))
        db.event(c,'library',{'source_id':source['id'],'status':status,'lessons':published},job.task_id)
        prefs=json.loads(one(c,'SELECT value FROM preferences WHERE singleton=1')['value'])
        if prefs['background_prepare']:
            for item in published:
                lesson=one(c,'SELECT * FROM lessons WHERE id=?',(item['lesson_id'],))
                if lesson['frozen_ref'] or one(c,"SELECT id FROM tasks WHERE kind='prepare' AND lesson_id=? AND status NOT IN ('FAILED','CANCELLED')",(lesson['id'],)):continue
                insert_task(c,task_record('prepare',{'lesson_content_id':lesson['id'],'lesson_json':lesson['lesson_json']},lesson_id=lesson['id'],family_id=lesson['family_id'],priority=40))
    job.context.emit('Recovery unavailable' if infrastructure_failed else 'Needs attention' if issues else 'Ready',{'lessons_imported':len(published),'timings':timings})
    return 'FAILED' if infrastructure_failed else 'NEEDS_ATTENTION' if issues else 'COMPLETED'
