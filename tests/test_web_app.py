"""Offline, invented inputs exercise the actual persistent and HTTP boundaries."""
import json
import os
from pathlib import Path
import time
import uuid
import apsw
import pytest
from fastapi.testclient import TestClient
from vocabatron.app.config import AppConfig,ResourcePolicy
from vocabatron.app.library import Library
from vocabatron.app.database import require_fence,encode,task_record,insert_task
from vocabatron.app.identity import identities,structure
from vocabatron.app.api import create_app
from vocabatron.app.archive import Archive
from vocabatron.app.resources import pressure_reason,Admission,estimate
from vocabatron.app.exports import copy_export
from vocabatron.domain import Lesson,Layout,Placement,Problem
from .helpers import lesson_of,synthetic_course


@pytest.fixture
def library(tmp_path,monkeypatch):
    root=tmp_path.resolve();data=root/'private';runtime=data/'runtime';template=root/'template.pdf';template.write_bytes(b'%PDF-invented-template')
    config=AppConfig(code_root=root,data_root=data,runtime_root=runtime,database=data/'library.sqlite3',outputs_root=root/'Outputs',
        static_root=root/'frontend',legacy_root=root/'legacy',template=template,public_base_url='https://workbook.example.test/vocabatron/',
        allowed_logins=('owner@example.test','other@example.test'),csrf_secret='synthetic-secret-never-a-real-credential-01234')
    for key,value in {'VOCABATRON_CODE_ROOT':root,'VOCABATRON_DATA_ROOT':data,'VOCABATRON_RUNTIME_ROOT':runtime,'VOCABATRON_LEGACY_ROOT':root/'legacy'}.items():monkeypatch.setenv(key,str(value))
    result=Library(config);result.initialize();return result


def add_lesson(library,lesson=None,source_bytes=b'%PDF-invented-source'):
    lesson=lesson or lesson_of(['AB','AC']);path=library.config.runtime_root/'upload.pdf';path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(source_bytes)
    source=library.source(path,'Invented lesson.pdf',task=False)
    saved=library.add_lesson({'lesson':lesson.model_dump(mode='json'),'coverage':{'status':'VERIFIED'},'transcript':'Invented only'},source['source_id'])
    return saved['lesson_id'],lesson


def claim(library,lesson_id):
    request=library.submit({'idempotency_key':uuid.uuid4().hex,'targets':[{'lesson_id':lesson_id,'mode':'count','count':2}]})
    tid=request['tasks'][0]['id'];fence=uuid.uuid4().hex
    with library.db.transaction() as c:c.execute("UPDATE tasks SET status='RUNNING',fence=? WHERE id=?",(fence,tid))
    return tid,fence


def client(library,login='owner@example.test'):
    c=TestClient(create_app(library.config),base_url=library.config.public_base_url,headers={'Tailscale-User-Login':login})
    session=c.get('api/session');assert session.status_code==200,session.text
    c.headers.update({'X-CSRF-Token':session.json()['csrf'],'Origin':library.config.origin})
    return c


def test_identity_packaging_and_word_ids_do_not_reset_history():
    original=lesson_of(['AB','AC']);new=original.model_dump(mode='json');new['source_sha256']='other-source';new['coverage_sha256']='other-coverage'
    for index,word in enumerate(new['words']):
        word['word_id']='new-word-'+str(index)
        for candidate in word['candidates']:
            candidate['word_id']=word['word_id'];candidate['candidate_id']='new-choice-'+str(index);candidate['source']['page']=9;candidate['source']['bbox']=[50,60,70,80]
    other=Lesson.model_validate(new)
    assert identities(original)['lesson_content_id']==identities(other)['lesson_content_id']
    assert identities(original)['structure_family_id']==identities(other)['structure_family_id']
    first=Layout(lesson_version=original.version,placements=(Placement(word_id=original.words[0].word_id,row=0,col=0,direction='across'),Placement(word_id=original.words[1].word_id,row=0,col=0,direction='down')))
    mirrored=Layout(lesson_version=other.version,placements=(Placement(word_id=other.words[0].word_id,row=5,col=6,direction='down'),Placement(word_id=other.words[1].word_id,row=5,col=6,direction='across')))
    assert structure(original,first)['geometry_hash']==structure(other,mirrored)['geometry_hash']
    assert structure(original,first)['crossing_hash']==structure(other,mirrored)['crossing_hash']


def test_database_runtime_and_durability(library):
    c=library.db.connect()
    assert c.execute('pragma journal_mode').get=='wal'
    assert c.execute('pragma synchronous').get==2
    assert c.execute('pragma foreign_keys').get==1
    assert tuple(map(int,apsw.sqlitelibversion().split('.')))>=(3,51,3)
    c.close()


def test_request_idempotency_counts_and_preferences(library):
    lid,_=add_lesson(library);request={'idempotency_key':'synthetic-submit-0001','targets':[{'lesson_id':lid,'mode':'count','count':9999}]}
    a=library.submit(request);b=library.submit(request)
    assert a['request_id']==b['request_id'] and b['reused']
    request['targets'][0]['count']=2
    with pytest.raises(Problem,match='IDEMPOTENCY_CONFLICT'):library.submit(request)
    p=library.preferences();assert p['default_count']==2
    library.update_preferences({'default_count':300},p['version'])
    with pytest.raises(Problem,match='PREFERENCE_CONFLICT'):library.update_preferences({'default_count':4},p['version'])
    assert library.preferences()['default_count']==300


def test_fenced_structure_reservation_and_history(library):
    lid,lesson=add_lesson(library);tid,fence=claim(library,lid)
    layout=Layout(lesson_version=lesson.version,placements=(Placement(word_id=lesson.words[0].word_id,row=0,col=0,direction='across'),Placement(word_id=lesson.words[1].word_id,row=0,col=0,direction='down')))
    archive=Archive(library);artifact=archive.reserve(tid,fence,lesson,layout,'synthetic-delivery')
    assert artifact['filename']=='Variant_001_Lesson_3_Crossword.pdf'
    with pytest.raises(Problem,match='HISTORICAL_DUPLICATE'):archive.reserve(tid,fence,lesson,layout,'synthetic-delivery')
    with library.db.transaction() as c:
        with pytest.raises(Problem,match='ATTEMPT_EXPIRED'):require_fence(c,tid,'outdated-token')
    assert library.db.one('SELECT next_batch FROM lessons WHERE id=?',(lid,))['next_batch']==2
    assert library.db.one('SELECT next_variant FROM answer_sets')['next_variant']==2
    assert len(archive.history(identities(lesson)['structure_family_id']))==1


def test_immutable_objects_detect_tampering(library):
    ref=library.objects.put_json({'invented':True});assert library.objects.read_json(ref)=={'invented':True}
    library.objects.path(ref).write_text('{"invented":false}')
    with pytest.raises(Problem,match='ARTIFACT_CHANGED'):library.objects.read_json(ref)


def test_auth_shared_owner_csrf_revocation_and_bounds(library):
    app=create_app(library.config)
    with TestClient(app,base_url=library.config.public_base_url) as anonymous:
        assert anonymous.get('api/session').status_code==403
        assert anonymous.get('api/session',headers={'Tailscale-User-Login':'intruder@example.test'}).status_code==403
        assert anonymous.get('api/session',headers=[('Tailscale-User-Login','owner@example.test'),('Tailscale-User-Login','intruder@example.test')]).status_code==403
    a=client(library);b=client(library,'other@example.test')
    assert a.patch('api/preferences',json={'version':1,'value':{'default_count':7}}).status_code==200
    assert b.get('api/preferences').json()['default_count']==7
    assert b.patch('api/preferences',json={'version':1,'value':{'default_count':3}}).status_code==409
    assert a.patch('api/preferences',json={'version':2,'value':{'default_count':2}},headers={'Origin':'https://evil.example.test'}).status_code==403
    assert a.get('api/preferences',headers={'Tailscale-User-Login':'revoked@example.test'}).status_code==403
    assert a.get('api/preferences',headers={'Host':'evil.example.test'}).status_code==400
    assert a.get('api/preferences').headers['cache-control'].startswith('no-store')
    assert a.get('.private/config.json').status_code==404
    assert a.post('api/generate',content=b'{}',headers={'Content-Length':'9999999999'}).status_code==413


def test_upload_repeated_bytes_does_not_reset(library):
    c=client(library);body=b'%PDF-entirely-invented'
    first=c.post('api/uploads',files={'files':('one.pdf',body,'application/pdf')})
    assert first.status_code==200,first.text
    second=c.post('api/uploads',files={'files':('renamed.pdf',body,'application/pdf')})
    assert second.json()['items'][0]['reused']
    assert library.db.one('SELECT count(*) n FROM tasks')['n']==1


def quiet_sample():
    return {'sampled_at':time.time(),'external_cpu_percent':0,'memory':{'MemAvailable':100*1024**3},'disk_free_bytes':100*1024**3,
        'pressure':{k:{'some':{'avg10':0}} for k in ('cpu','memory','io')},'gpu':{'status':'Available','sampled_at':time.time(),'utilization_percent':0,'compute_categories':[]},
        'ollama':{'status':'Available','sampled_at':time.time(),'other_models_loaded':False},'external_model_connections':0,'application':{'rss_upper_bound_bytes':0}}


def test_read_bursts_do_not_exhaust_state_change_allowance(library):
    c=client(library)
    for _ in range(965):last=c.get('api/preferences')
    assert last.status_code==429
    # Writes retain their own bound after a many-device read burst.
    prefs=library.preferences()
    result=c.patch('api/preferences',json={'version':prefs['version'],'value':{'default_count':3}})
    assert result.status_code==200 and result.json()['default_count']==3
    for _ in range(125):last=c.post('api/tasks/missing/pause')
    assert last.status_code==429


def test_governor_stale_busy_and_global_reservations(library):
    policy=ResourcePolicy();sample=quiet_sample()
    assert pressure_reason(sample,policy) is None
    assert pressure_reason({**sample,'sampled_at':0},policy)=='Resource telemetry is stale'
    assert pressure_reason({**sample,'external_cpu_percent':90},policy,running=True)
    assert pressure_reason({**sample,'external_model_connections':1},policy,inference=True)
    sample['gpu']['compute_categories']=['other_compute']
    assert pressure_reason(sample,policy) is None
    assert pressure_reason(sample,policy,inference=True)
    sample['gpu']['compute_categories']=[]
    admission=Admission(library.db,policy);admission.idle_since=time.time()-100
    a=task_record('restore',{});b=task_record('restore',{})
    with library.db.transaction() as c:
        insert_task(c,a);insert_task(c,b)
        assert admission.reserve(c,a['id'],'one',estimate('verify'),sample) is None
        assert admission.reserve(c,b['id'],'two',estimate('verify'),sample)=='The document worker slot is reserved'


def test_cpu_only_development_uses_host_headroom_and_keeps_other_guards():
    from vocabatron.app.dev_runner import CpuDevelopmentPolicy
    policy=CpuDevelopmentPolicy();sample=quiet_sample()
    sample['external_cpu_percent']=35
    sample['gpu'].update(compute_categories=['other_compute'],utilization_percent=25)
    assert pressure_reason(sample,policy,cpu_only=True) is None
    assert pressure_reason(sample,policy,cpu_only=True,inference=True)
    authorized=policy.model_copy(update={'cpu_work_during_gpu_activity':True})
    assert pressure_reason(sample,authorized) is None
    assert pressure_reason(sample,authorized,inference=True)
    assert pressure_reason(sample,ResourcePolicy(),cpu_only=True)
    sample['external_cpu_percent']=70
    assert pressure_reason(sample,policy,cpu_only=True,running=True)
    sample['external_cpu_percent']=12
    sample['pressure']['cpu']['some']['avg10']=8
    assert pressure_reason(sample,policy,cpu_only=True,running=True) is None
    sample['external_cpu_percent']=35
    assert pressure_reason(sample,policy,cpu_only=True,running=True)
    sample['external_cpu_percent']=12
    sample['pressure']['memory']['some']['avg10']=1
    assert pressure_reason(sample,policy,cpu_only=True,running=True)
    sample['pressure']['memory']['some']['avg10']=0
    sample['sampled_at']=0
    assert pressure_reason(sample,policy,cpu_only=True)=='Resource telemetry is stale'


def test_book_global_pages_and_independent_lessons(library):
    from pypdf import PdfReader,PdfWriter
    from vocabatron.supervisor import run_job
    from vocabatron.app.book import assemble
    root=library.config.runtime_root;root.mkdir(parents=True,exist_ok=True)
    paths=[root/'first.pdf',root/'second.pdf'];synthetic_course(paths[0],['AB','AC'],3);synthetic_course(paths[1],['DE','DF'],9)
    writer=PdfWriter()
    for path in paths:
        for page in PdfReader(path).pages:writer.add_page(page)
    combined=root/'combined.pdf'
    with combined.open('wb') as stream:writer.write(stream)
    result=run_job('book_pages',{'source':str(combined),'page_numbers':[1,2]})
    book=assemble(result['source_sha256'],result['pages'])
    assert book['status']=='READY',book
    assert [x['lesson']['lesson'] for x in book['lessons']]==[3,9]
    assert book['lessons'][1]['coverage']['pages']==[2]
    single=run_job('book_pages',{'source':str(paths[1]),'page_numbers':[1]})
    other=assemble(single['source_sha256'],single['pages'])
    assert identities(Lesson.model_validate(other['lessons'][0]['lesson']))['lesson_content_id']==identities(Lesson.model_validate(book['lessons'][1]['lesson']))['lesson_content_id']
