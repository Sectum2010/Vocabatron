"""Synthetic recovery corpus; no private vocabulary, remote services or model pulls."""
import json
import os
from pathlib import Path
import time
import uuid

import pytest
from PIL import Image,ImageDraw,ImageFont
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

from vocabatron.app.book import assemble
from vocabatron.app.database import task_record,insert_task,encode
from vocabatron.app.identity import identities,legacy_content
from vocabatron.app.jobs import Job
from vocabatron.app.library import Library
from vocabatron.app.resources import pressure_reason,spare_threads
from vocabatron.domain import Lesson,digest
from vocabatron.documents import PARSER_VERSION
from vocabatron.supervisor import run_job,Limits
from vocabatron.storage import sha256
from .test_web_app import library,client,quiet_sample,add_lesson


LINES=['Synthetic Vocabulary Lesson 3','1 ALPHA (noun)','An invented first item.',
       'SYNONYM: first, starting point','ANTONYM: last; final','This is a synthetic example.',
       '', '2 BETA (noun)','An invented second item.','SYNONYM: second, next item',
       'ANTONYM: initial','Another synthetic example.']


def handout(path,*,scan=False,rotation=0,skew=0,low=False,ordinals=True):
    text=[s if ordinals else s.removeprefix('1 ').removeprefix('2 ') for s in LINES]
    c=canvas.Canvas(str(path),pagesize=(600,766.667),invariant=1)
    if scan:
        image=Image.new('RGB',(1800,2300),'white');draw=ImageDraw.Draw(image)
        font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',42)
        for i,line in enumerate(text):draw.text((110,120+i*95),line,font=font,fill='black')
        if low:image=image.resize((750,959)).resize((1800,2300))
        if skew:image=image.rotate(skew,fillcolor='white')
        if rotation:image=image.rotate(rotation,expand=True,fillcolor='white')
        c.drawImage(ImageReader(image),0,0,width=600,height=766.667)
    else:
        c.setFont('Helvetica',14)
        for i,line in enumerate(text):c.drawString(110/3,766.667-(120+i*95)/3-14,line)
    c.showPage();c.save()


@pytest.fixture
def prepared_ocr(monkeypatch):
    value=os.environ.get('VOCABATRON_TEST_OCR_ROOT')
    if not value:pytest.skip('Prepared OCR assets must be explicitly selected; tests never download them')
    root=Path(value).resolve()
    assert (root/'tesseract-manifest.json').is_file()
    monkeypatch.setenv('VOCABATRON_OCR_ROOT',str(root))
    monkeypatch.setenv('VOCABATRON_LEGACY_ROOT',str(root.parent))
    return root


def import_job(library,path):
    uploaded=library.source(path,path.name);task_id=uploaded['task']['id'];fence=uuid.uuid4().hex
    with library.db.transaction() as c:
        c.execute("UPDATE tasks SET status='RUNNING',fence=?,started_at=? WHERE id=?",(fence,time.time(),task_id))
    outcome=Job(library,task_id,fence).run()
    with library.db.transaction() as c:c.execute('UPDATE tasks SET status=? WHERE id=?',(outcome['status'],task_id))
    return library.db.one('SELECT * FROM sources WHERE id=?',(uploaded['source_id'],)),outcome


@pytest.mark.parametrize('ordinals',[True,False])
def test_native_borderless_optional_fields_and_semicolon(library,ordinals):
    p=library.config.runtime_root/'source.pdf';p.parent.mkdir(parents=True,exist_ok=True);handout(p,ordinals=ordinals)
    source,outcome=import_job(library,p)
    assert source['status']=='READY'
    row=library.db.one('SELECT * FROM lessons');lesson=Lesson.model_validate(library.objects.read_json(row['lesson_json']))
    assert [w.raw for w in lesson.words]==['ALPHA','BETA']
    assert [c.text for c in lesson.words[0].candidates]==['first','starting point','last','final']
    summary=library.objects.read_json(source['report'])
    assert not summary['lessons'][0]['coverage']['ocr_used']
    assert summary['parser_version']==PARSER_VERSION


@pytest.mark.parametrize('variant',[{}, {'low':True}, {'rotation':90}, {'skew':2}])
def test_image_only_import_without_network_and_content_reuse(library,prepared_ocr,variant):
    root=library.config.runtime_root;root.mkdir(parents=True,exist_ok=True)
    native=root/'native.pdf';handout(native);source,_=import_job(library,native)
    before=library.db.one('SELECT id FROM lessons')['id']
    scan=root/'scan.pdf';handout(scan,scan=True,**variant)
    saved,outcome=import_job(library,scan)
    assert saved['status']=='READY',library.objects.read_json(saved['report'])
    assert library.db.one('SELECT count(*) n FROM lessons')['n']==1
    assert library.db.one('SELECT id FROM lessons')['id']==before
    assert outcome['document_workers']
    assert all(w['network_isolation']=='seccomp-network-denied-and-probed' for w in outcome['document_workers'])
    report=library.objects.read_json(saved['report'])
    assert report['lessons'][0]['coverage']['ocr_used']
    assert report['recovery_attempts']


@pytest.mark.parametrize('high_resolution',[False,True])
def test_docling_independent_local_structure(library,prepared_ocr,high_resolution):
    p=library.config.runtime_root/'scan.pdf';p.parent.mkdir(parents=True,exist_ok=True);handout(p,scan=True)
    if high_resolution:
        image=Image.new('RGB',(2500,3200),'white');draw=ImageDraw.Draw(image)
        font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',58)
        for i,line in enumerate(LINES):draw.text((40*300/72,(35+i*31.667)*300/72),line,font=font,fill='black')
        c=canvas.Canvas(str(p),pagesize=(600,766.667),invariant=1)
        c.drawImage(ImageReader(image),0,0,width=600,height=766.667);c.showPage();c.save()
    page=run_job('structured_page',{'source':str(p),'page_numbers':[1]},limits=Limits(address_bytes=12*1024**3))
    result=assemble(sha256(p),[page]);assert result['status']=='READY'
    assert page['ocr_evidence']['network_isolation']=='seccomp-network-denied-and-probed'
    assert len(result['lessons'][0]['lesson']['words'])==2
    assert page['table_hints']
    native=p.with_name('native.pdf');handout(native)
    extracted=run_job('book_pages',{'source':str(native),'page_numbers':[1]})
    original=assemble(sha256(native),extracted['pages'])
    assert identities(Lesson.model_validate(result['lessons'][0]['lesson']))['lesson_content_id']==identities(Lesson.model_validate(original['lessons'][0]['lesson']))['lesson_content_id']


def test_missing_models_fail_closed_without_network(library,monkeypatch):
    root=library.config.runtime_root;root.mkdir(parents=True,exist_ok=True)
    monkeypatch.setenv('VOCABATRON_OCR_ROOT',str(root/'missing-models'))
    p=root/'scan.pdf';handout(p,scan=True)
    from vocabatron.domain import Problem
    with pytest.raises(Problem,match='OCR_NOT_PREPARED'):run_job('ocr_page',{'source':str(p),'page_numbers':[1]})
    assert not (root/'missing-models').exists()


def test_parser_retry_once_keeps_history(library):
    p=library.config.runtime_root/'source.pdf';p.parent.mkdir(parents=True,exist_ok=True);handout(p)
    uploaded=library.source(p,p.name);old=uploaded['task']['id'];ref=library.objects.put_json({'status':'old synthetic failure'})
    with library.db.transaction() as c:
        c.execute("UPDATE sources SET status='NEEDS_ATTENTION',report=? WHERE id=?",(ref,uploaded['source_id']))
        c.execute("UPDATE tasks SET status='NEEDS_ATTENTION' WHERE id=?",(old,))
    made=library.retry_parser_upgrades();assert len(made)==1 and made[0]!=old
    assert library.retry_parser_upgrades()==[]
    assert library.db.one('SELECT status FROM tasks WHERE id=?',(old,))['status']=='NEEDS_ATTENTION'
    assert library.objects.read_json(ref)=={'status':'old synthetic failure'}


def test_document_hard_limit_is_terminal_and_retry_keeps_diagnostic(library):
    library.config=library.config.model_copy(update={'document_limits':library.config.document_limits.model_copy(update={'characters':10})})
    path=library.config.runtime_root/'limited.pdf';path.parent.mkdir(parents=True,exist_ok=True);handout(path)
    source,outcome=import_job(library,path)
    assert outcome['status']=='FAILED' and source['status']=='RECOVERY_UNAVAILABLE'
    report=library.objects.read_json(source['report'])
    assert report['recovery_attempts'][-1]['code']=='DOCUMENT_RESOURCE_LIMIT'
    old=library.db.one('SELECT * FROM tasks')
    assert old['status']=='FAILED' and library.db.one('SELECT count(*) n FROM lessons')['n']==0
    c=client(library);reply=c.post('api/tasks/'+old['id']+'/retry');assert reply.status_code==200,reply.text
    tasks=library.db.all('SELECT * FROM tasks');assert len(tasks)==2
    assert library.db.one('SELECT status FROM tasks WHERE id=?',(old['id'],))['status']=='FAILED'
    assert library.objects.read_json(source['report'])==report
    assert library.db.one('SELECT report FROM source_attempts WHERE task_id=?',(old['id'],))['report']==source['report']


def test_persistent_dismissal_does_not_delete_work(library):
    c=client(library);records=[]
    with library.db.transaction() as db:
        for status in ('COMPLETED','FAILED','NEEDS_ATTENTION','CANCELLED','RUNNING','QUEUED'):
            record=task_record('import',{});record['status']=status;insert_task(db,record);records.append(record)
    assert c.post('api/tasks/'+records[4]['id']+'/dismiss').status_code==400
    assert c.post('api/tasks/dismiss-completed').status_code==200
    assert {x['status'] for x in c.get('api/tasks').json()['items']}=={'RUNNING','QUEUED','FAILED','NEEDS_ATTENTION','CANCELLED'}
    for record in records[1:4]:assert c.post('api/tasks/'+record['id']+'/dismiss').status_code==200
    assert {x['status'] for x in c.get('api/tasks').json()['items']}=={'RUNNING','QUEUED'}
    assert c.get('api/tasks?history=true').json()['total']==6
    fresh=Library(library.config);fresh.initialize();assert fresh.list_tasks()['total']==2
    assert c.post('api/tasks/'+records[0]['id']+'/restore-activity').status_code==200
    assert fresh.list_tasks()['total']==3
    lid,_=add_lesson(library);source=library.db.one('SELECT id FROM sources')['id']
    assert c.post('api/sources/'+source+'/dismiss').status_code==200
    assert c.get('api/sources').json()['items']==[]
    assert len(c.get('api/sources?history=true').json()['items'])==1
    assert fresh.lesson(lid)


def test_own_pressure_does_not_restart_work_and_dynamic_capacity(library):
    policy=library.config.resources;sample=quiet_sample();sample['logical_cpus']=20
    sample['external_cpu_percent']=10;sample['pressure']['cpu']['some']['avg10']=80
    assert spare_threads(sample,policy)==8
    assert pressure_reason(sample,policy,running=True) is None
    sample['pressure']['io']['some']['avg10']=10
    assert pressure_reason(sample,policy,running=True) is None
    assert pressure_reason(sample,policy)
    sample['external_cpu_percent']=75
    assert spare_threads(sample,policy)==3
    sample['sampled_at']=0
    assert spare_threads(sample,policy)==0 and pressure_reason(sample,policy,running=True) is None


def test_legacy_semantic_alias_retains_saved_identity_and_frozen_binding(library):
    from .helpers import lesson_of
    original=lesson_of(['AB','AC']);lid,_=add_lesson(library,original)
    # Model a v1 database without rewriting its immutable original lesson.
    legacy_id=digest(legacy_content(original))
    with library.db.transaction() as c:
        c.execute('DELETE FROM source_bindings');c.execute('DELETE FROM semantic_aliases')
        c.execute('UPDATE lessons SET id=?,canonical=? WHERE id=?',(legacy_id,encode(legacy_content(original)),lid))
    library.initialize()
    changed=original.model_copy(update={'source_sha256':'new invented packaging'})
    _,source=next(iter([(s['id'],s) for s in library.db.all('SELECT * FROM sources')]))
    result=library.add_lesson({'lesson':changed.model_dump(mode='json'),'coverage':{'status':'VERIFIED'}},source['id'])
    assert result['reused'] and result['lesson_id']==legacy_id
    assert library.lesson(legacy_id)[1].source_sha256==original.source_sha256


def test_cross_page_open_entry_and_wrapped_candidates(library):
    p=library.config.runtime_root/'cross-page.pdf';p.parent.mkdir(parents=True,exist_ok=True)
    c=canvas.Canvas(str(p),pagesize=(600,760),invariant=1);c.setFont('Helvetica',14)
    for n,line in enumerate(['Vocabulary Lesson 8','1 ALPHA (noun)','SYNONYM: first,','starting point','ANTONYM: last; final']):
        c.drawString(40,720-n*30,line)
    c.showPage();c.setFont('Helvetica',14)
    for n,line in enumerate(['Vocabulary Lesson 8','The first item continues on this page.','2 BETA (noun)','SYNONYM: second','ANTONYM: initial']):
        c.drawString(40,720-n*30,line)
    c.showPage();c.save()
    source,_=import_job(library,p);assert source['status']=='READY'
    lesson=Lesson.model_validate(library.objects.read_json(library.db.one('SELECT lesson_json FROM lessons')['lesson_json']))
    assert len(lesson.words)==2 and lesson.words[0].examples[0].page==2
    assert [x.text for x in lesson.words[0].candidates]==['first','starting point','last','final']
    summary=library.objects.read_json(source['report']);full=library.objects.read_json(summary['full_report'])
    segments=full['lessons'][0]['coverage']['entries'][0]['candidate_segments']
    assert any(x['separator']==';' for x in segments)
    assert any(len(x['source_fragments'])==2 for x in segments)


def test_two_full_entry_columns(library):
    p=library.config.runtime_root/'columns.pdf';p.parent.mkdir(parents=True,exist_ok=True)
    c=canvas.Canvas(str(p),pagesize=(600,760),invariant=1);c.setFont('Helvetica',12)
    c.drawString(40,720,'Vocabulary Lesson 9')
    for x,lines in [(40,['1 ALPHA (noun)','SYNONYM: first','ANTONYM: last']),
                    (340,['2 BETA (noun)','SYNONYM: second','ANTONYM: initial'])]:
        for n,line in enumerate(lines):c.drawString(x,660-n*30,line)
    c.showPage();c.save();source,_=import_job(library,p)
    assert source['status']=='READY'
    lesson=Lesson.model_validate(library.objects.read_json(library.db.one('SELECT lesson_json FROM lessons')['lesson_json']))
    assert [c.text for c in lesson.words[0].candidates]==['first','last']
    assert [c.text for c in lesson.words[1].candidates]==['second','initial']


def test_false_giant_and_duplicate_table_are_hints_only():
    from types import SimpleNamespace
    from vocabatron.documents import table_hints
    good=SimpleNamespace(bbox=(40,50,550,710),cells=[(40,50,550,100),(40,100,550,710)])
    giant=SimpleNamespace(bbox=(0,-760,600,1520),cells=[(0,-760,600,0),(0,0,600,1520)])
    page=SimpleNamespace(width=600,height=760,find_tables=lambda:[giant,good,good])
    cells,audit=table_hints(page)
    assert len(cells)==2 and sum(a['accepted'] for a in audit)==1
    assert {a['reason'] for a in audit}=={None,'outside_page','duplicate_geometry'}


def test_mixed_native_and_scanned_entry_keeps_native_letters(library,prepared_ocr):
    root=library.config.runtime_root;root.mkdir(parents=True,exist_ok=True)
    p=root/'mixed.pdf';c=canvas.Canvas(str(p),pagesize=(600,760),invariant=1)
    c.setFont('Helvetica',14)
    for n,line in enumerate(['Vocabulary Lesson 3','1 ALPHA (noun)','SYNONYM: first','ANTONYM: last']):c.drawString(40,720-n*35,line)
    image=Image.new('RGB',(1800,600),'white');draw=ImageDraw.Draw(image)
    font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',42)
    for n,line in enumerate(['2 BETA (noun)','SYNONYM: second','ANTONYM: initial']):draw.text((120,50+n*140),line,font=font,fill='black')
    c.drawImage(ImageReader(image),0,210,width=600,height=200);c.showPage();c.save()
    source,_=import_job(library,p);assert source['status']=='READY'
    row=library.db.one('SELECT * FROM lessons');lesson=Lesson.model_validate(library.objects.read_json(row['lesson_json']))
    assert [w.raw for w in lesson.words]==['ALPHA','BETA']
    summary=library.objects.read_json(source['report']);full=library.objects.read_json(summary['full_report'])
    page=library.objects.read_json(full['normalized_evidence'])['pages'][0]
    assert {t['backend'] for t in page['tokens']}=={'native-pdfplumber-poppler','tesseract-5-tsv'}
    assert all('source_bbox' in t for t in page['tokens'])


def test_critical_ocr_disagreement_is_never_ready(library):
    from copy import deepcopy
    p=library.config.runtime_root/'source.pdf';p.parent.mkdir(parents=True,exist_ok=True);handout(p)
    extracted=run_job('book_pages',{'source':str(p),'page_numbers':[1]})
    page=deepcopy(extracted['pages'][0]);page['ocr_used']=True
    for t in page['tokens']:t.update(backend='synthetic-ocr',consensus=t['text']!='ALPHA')
    report=assemble(sha256(p),[page])
    assert report['status']!='READY'
    assert 'OCR_CRITICAL_DISAGREEMENT' in [i['code'] for i in report['lessons'][0]['coverage']['issues']]


def test_live_cgroup_parsing_uses_parent_limit():
    from vocabatron.app.resources import enforcement
    group={'dedicated':True,'cpu.max':'max 100000','memory.high':str(30*1024**3),'memory.max':str(46*1024**3),
           'memory.swap.max':'0','io.max':'','application_slice':{'cpu.max':'200000 100000',
           'memory.high':str(6*1024**3),'memory.max':str(8*1024**3),'memory.swap.max':'0','io.max':'259:0 rbps=20971520'}}
    limits=enforcement(group);assert limits['memory.max']==8*1024**3 and limits['cpu_quota_cores']==2
    group['application_slice']['cpu.max']='max 100000'
    assert enforcement(group)['cpu_quota_known'] and enforcement(group)['cpu_quota_cores'] is None
    assert enforcement({'dedicated':False}) is None


def test_coordinate_provenance_round_trip():
    from vocabatron.ocr import transform_point
    transform={'source_page_size':[600,760],'upright_page_size':[760,600],'rotation_degrees':90,'deskew_degrees':2}
    for p in ([0,0],[600,760],[30,500]):
        assert transform_point(transform_point(p,transform),transform,inverse=True)==pytest.approx(p)


def test_resource_yield_stops_children_after_wrapper_exits(tmp_path):
    import subprocess,sys,signal,psutil
    from vocabatron.app.dev_runner import stop_owned_group
    marker=tmp_path/'owned-child'
    child="import signal,time,os;signal.signal(signal.SIGTERM,signal.SIG_IGN);open(os.environ['OWNED_CHILD_MARKER'],'w').write(str(os.getpid()));time.sleep(30)"
    parent="import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',sys.argv[1]]);time.sleep(30)"
    proc=subprocess.Popen([sys.executable,'-c',parent,child],start_new_session=True,
        env={**os.environ,'OWNED_CHILD_MARKER':str(marker)})
    owned=None
    try:
        deadline=time.monotonic()+5
        while not marker.exists() and time.monotonic()<deadline:time.sleep(.02)
        assert marker.exists();owned=psutil.Process(int(marker.read_text()))
        os.killpg(proc.pid,signal.SIGTERM);proc.wait(timeout=3)
        assert owned.is_running() and owned.status()!=psutil.STATUS_ZOMBIE
        stop_owned_group(proc)
        deadline=time.monotonic()+3
        while owned.is_running() and owned.status()!=psutil.STATUS_ZOMBIE and time.monotonic()<deadline:time.sleep(.02)
        assert not owned.is_running() or owned.status()==psutil.STATUS_ZOMBIE
    finally:
        stop_owned_group(proc)
        if owned and owned.is_running() and owned.status()!=psutil.STATUS_ZOMBIE:owned.kill()
