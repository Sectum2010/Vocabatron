"""Protocol, transaction and raw-index tests use only invented material."""
import io
import json
from pathlib import Path
import tarfile
import zipfile

import pytest
from pydantic import ValidationError

from vocabatron.clues import MODEL,Ollama,select
from vocabatron.domain import Problem,Lesson,digest,SolverOptions
from vocabatron.storage import PrivateStore,sha256
from vocabatron.evidence import verify_evidence
from vocabatron.privacy import scan,inspect_archive
from vocabatron.transcription import ordered_evidence
from vocabatron.domain import Fragment
from vocabatron import services
from .helpers import lesson_of,synthetic_handout,synthetic_template


def transport(fail_first=False,wrong_model=False):
    state={'calls':0}
    def request(endpoint,payload):
        if endpoint=='version':return {'version':'synthetic'}
        if endpoint=='tags':return {'models':[{'name':MODEL,'digest':'synthetic-digest'}]}
        if endpoint=='ps':return {'models':[]}
        if endpoint=='show':return {'model_info':{'synthetic.context_length':32768}}
        state['calls']+=1
        words=json.loads(payload['messages'][1]['content'])['untrusted_source_data']
        choices=[{'word_id':w['word_id'],'candidate_id':w['candidates'][0]['candidate_id']} for w in words]
        return {'model':'unexpected' if wrong_model else MODEL,'done':True,'done_reason':'stop',
            'eval_count':len(words),'message':{'content':'{' if fail_first and state['calls']==1 else json.dumps({'choices':choices})}}
    return Ollama(request),state


def setup_store(tmp_path):
    s=PrivateStore(tmp_path/'store');s.path('sources').mkdir()
    synthetic_handout(s.path('sources/input.pdf'));synthetic_template(s.path('sources/template.pdf'))
    s.json('config.json',{'lesson':3,'source':'sources/input.pdf','template':'sources/template.pdf',
        'prefixes':['VersionA','VersionB'],'solver':{'seconds_per_layout':60,'optimization_seconds':0}})
    return s


@pytest.mark.parametrize('changed',['ABCED','BACDE','ABCD','AB.CDE'])
def test_three_way_ordered_evidence(changed):
    chars=[{'text':c,'x0':i*10,'x1':i*10+8,'top':0,'bottom':10} for i,c in enumerate('ABCDE')]
    fragment=Fragment(page=1,bbox=(0,0,100,20),raw=changed,char_indices=tuple(range(5)))
    result=ordered_evidence(fragment,chars,chars)
    assert not result['table_order_match'] and result['poppler_order_match']


def test_only_whitespace_is_ignored_and_poppler_order_checked():
    chars=[{'text':c,'x0':i*10,'x1':i*10+8,'top':0,'bottom':10} for i,c in enumerate('ABCDE')]
    fragment=Fragment(page=1,bbox=(0,0,100,20),raw='ABC\n DE',char_indices=tuple(range(5)))
    assert ordered_evidence(fragment,chars,chars)['table_order_match']
    changed=[dict(c) for c in chars];changed[0]['text'],changed[1]['text']=changed[1]['text'],changed[0]['text']
    assert not ordered_evidence(fragment,chars,changed)['poppler_order_match']


def test_selection_runs_do_not_choose_largest_attempt(tmp_path):
    s=PrivateStore(tmp_path/'store');lesson=lesson_of(['AB','AC'])
    adapter,state=transport(fail_first=True);first=select(lesson,adapter,s)
    before={p:sha256(p) for p in s.path('model').rglob('*.json')}
    adapter,state=transport();second=select(lesson,adapter,s)
    a=verify_evidence(s,lesson,first);b=verify_evidence(s,lesson,second)
    assert a['adopted_responses']==['batch-0-attempt-1-response.json']
    assert b['adopted_responses']==['batch-0-attempt-0-response.json']
    assert first.choices==second.choices and first.version!=second.version
    assert all(sha256(p)==h for p,h in before.items())
    pointer=sha256(s.path('clues/current.json'));adapter,_=transport(wrong_model=True)
    with pytest.raises(Problem,match='MODEL_SELECTION_INVALID'):select(lesson,adapter,s,retries=1)
    assert sha256(s.path('clues/current.json'))==pointer


@pytest.mark.parametrize('mode',['missing','response','model','digest','lesson','cross_lesson'])
def test_model_evidence_tampering_is_rejected(tmp_path,mode):
    s=PrivateStore(tmp_path/'store');lesson=lesson_of(['AB','AC']);adapter,_=transport();frozen=select(lesson,adapter,s)
    root=s.path('model/runs')/frozen.selection_run_id
    if mode=='missing':(root/'batch-0-attempt-0-response.json').unlink()
    elif mode=='response':(root/'batch-0-attempt-0-response.json').write_text('{}')
    elif mode=='model':frozen=frozen.model_copy(update={'model':'other'})
    elif mode=='digest':frozen=frozen.model_copy(update={'model_digest':'other'})
    elif mode=='lesson':(root/'lesson.json').write_text('{}')
    else:lesson=lesson.model_copy(update={'lesson':4})
    with pytest.raises(Problem):verify_evidence(s,lesson,frozen)


def test_import_failure_and_check_do_not_replace_current(tmp_path,monkeypatch):
    s=setup_store(tmp_path);services.import_lesson(s);adapter,_=transport();services.select_clues(s,adapter=adapter)
    pointer=sha256(s.path('current.json'));config,lesson,frozen=services.load_inputs(s)
    hashes={p:sha256(p) for p in s.path('imports').rglob('*') if p.is_file()}
    services.check_lesson(s)
    assert sha256(s.path('current.json'))==pointer and all(sha256(p)==v for p,v in hashes.items())
    before=lesson.version
    newer=lesson.model_copy(update={'coverage_sha256':'different verifier diagnostics'})
    assert newer.version==before
    original=s.json
    def fail(path,value):
        if str(path)=='current.json':raise PermissionError('simulated pointer publication interruption')
        return original(path,value)
    monkeypatch.setattr(s,'json',fail)
    with pytest.raises(PermissionError):services.import_lesson(s)
    assert sha256(s.path('current.json'))==pointer
    assert services.load_inputs(s)[2].version==frozen.version


def fake_git(entries,blobs,*,unstable=False):
    calls=[];stage_calls=0
    def run(args,limit=None):
        nonlocal stage_calls
        calls.append(args)
        if args==['ls-files','--stage','-z']:
            stage_calls+=1
            result=b''.join(mode+b' '+oid.encode()+b' '+stage+b'\t'+name+b'\0' for mode,oid,stage,name in entries)
            return result+b'100644 '+b'c'*40+b' 0\tsrc/changed.py\0' if unstable and stage_calls%2==0 else result
        if args[:2]==['cat-file','-s']:return str(len(blobs[args[2]])).encode()
        if args[:2]==['cat-file','blob']:return blobs[args[2]]
        return b''
    return run,calls


@pytest.mark.parametrize('exists',[True,False])
def test_index_blob_scanned_even_when_worktree_safe_or_deleted(tmp_path,exists):
    (tmp_path/'src').mkdir();path=tmp_path/'src/module.py'
    if exists:path.write_text('SAFE_CURRENT_TEXT')
    oid='a'*40;secret=('ghp_'+'A'*36).encode()
    run,calls=fake_git([(b'100644',oid,b'0',b'src/module.py')],{oid:secret})
    with pytest.raises(Problem) as error:scan(tmp_path,runner=run)
    assert any(x['scope']=='INDEX' and x['rule']=='github_token' for x in error.value.details['issues'])
    assert ['cat-file','blob',oid] in calls


@pytest.mark.parametrize('mode,stage',[(b'120000',b'0'),(b'160000',b'0'),(b'100644',b'1')])
def test_index_special_entries_never_pass(tmp_path,mode,stage):
    run,_=fake_git([(mode,'a'*40,stage,b'src/file with\nnewline.py')],{'a'*40:b'safe'})
    with pytest.raises(Problem):scan(tmp_path,runner=run)


def test_index_unstable_and_missing_blob_fail_closed(tmp_path):
    run,_=fake_git([(b'100644','a'*40,b'0',b'src/a.py')],{'a'*40:b'safe'},unstable=True)
    with pytest.raises(Problem) as e:scan(tmp_path,runner=run)
    assert any(x['rule']=='UNSTABLE' for x in e.value.details['issues'])
    def missing(args,limit=None):
        if args[0]=='cat-file':raise OSError('missing object')
        return b'100644 '+b'a'*40+b' 0\tsrc/a.py\0' if '--stage' in args else b''
    with pytest.raises(Problem):scan(tmp_path,runner=missing)


def test_archive_limits_links_and_paths(tmp_path):
    wheel=tmp_path/'example.whl'
    with zipfile.ZipFile(wheel,'w',compression=zipfile.ZIP_DEFLATED) as z:z.writestr('src/large.txt','x'*10000)
    assert inspect_archive(wheel,max_file=100)['issues'][0]['rule']=='ARCHIVE_LIMIT'
    with zipfile.ZipFile(wheel,'w') as z:
        for i in range(5):z.writestr(f'src/{i}.txt','safe')
    assert inspect_archive(wheel,max_files=2)['issues']
    archive=tmp_path/'example.tar.gz'
    with tarfile.open(archive,'w:gz') as t:
        info=tarfile.TarInfo('../../escape');info.type=tarfile.SYMTYPE;info.linkname='/unsafe';t.addfile(info)
    assert inspect_archive(archive)['issues']


@pytest.mark.parametrize('value',[float('nan'),float('inf'),-1,901])
def test_execution_numeric_limits(value):
    with pytest.raises(ValidationError):SolverOptions(seconds_per_layout=value)


@pytest.mark.parametrize('mode',['main_letters','candidate_letters','candidate_order','field_exchange','punctuation','duplicate_cell'])
def test_ordered_parser_faults_with_equal_character_counts(tmp_path,monkeypatch,mode):
    import pdfplumber.table
    from vocabatron.ingest import extract_local
    source=tmp_path/'input.pdf';synthetic_handout(source)
    original=pdfplumber.table.Table.extract
    def changed(self,*args,**kwargs):
        rows=original(self,*args,**kwargs)
        locations=[(ri,ci,v) for ri,row in enumerate(rows) for ci,v in enumerate(row) if v]
        if mode=='field_exchange':
            a=next((r,c,v) for r,c,v in locations if 'definition.' in v)
            b=next((r,c,v) for r,c,v in locations if 'example.' in v)
            rows[a[0]][a[1]],rows[b[0]][b[1]]=b[2],a[2]
        else:
            match={'main_letters':'AB (noun)','candidate_letters':'SYNONYM:','candidate_order':'SYNONYM:',
                   'punctuation':'definition.','duplicate_cell':'ANTONYM:'}[mode]
            r,c,v=next((r,c,v) for r,c,v in locations if match in v)
            if mode=='main_letters':rows[r][c]=v.replace('AB','BA')
            elif mode=='candidate_letters':rows[r][c]=v.replace('made','mdae')
            elif mode=='candidate_order':rows[r][c]='SYNONYM: invented, made up phrase'
            elif mode=='punctuation':rows[r][c]=v.replace('.','')
            else:rows[r][c]=next(v for r,c,v in locations if v.startswith('SYNONYM:'))
        return rows
    monkeypatch.setattr(pdfplumber.table.Table,'extract',changed)
    with pytest.raises(Problem,match='TRANSCRIPTION_REQUIRES_DECISION'):extract_local(source,3)


def test_cancelled_selection_keeps_previous_pointer(tmp_path):
    from vocabatron.execution import Execution,CURRENT
    s=setup_store(tmp_path);services.import_lesson(s);adapter,_=transport();services.select_clues(s,adapter=adapter)
    before=sha256(s.path('current.json'));adapter,_=transport();original=adapter.transport
    def late(endpoint,payload):
        result=original(endpoint,payload)
        if endpoint=='chat':CURRENT.get().event.set()
        return result
    adapter.transport=late
    with pytest.raises(Problem,match='CANCELLED'):services.select_clues(s,new_version=True,adapter=adapter,task_id='cancel-selection')
    assert sha256(s.path('current.json'))==before
    assert services.task_status(s,'cancel-selection')['stage']=='CANCELLED'


def test_changed_batch_count_uses_only_adopted_run(tmp_path,monkeypatch):
    import vocabatron.clues as clues
    lesson=lesson_of(['AB','AC','AD']);s=PrivateStore(tmp_path/'store')
    adapter,_=transport();single=select(lesson,adapter,s)
    original=clues.budget
    def smaller(words):
        payload,options=original(words)
        options['num_ctx']=65536 if len(words)>1 else 4096
        return payload,options
    monkeypatch.setattr(clues,'budget',smaller)
    adapter,_=transport();multiple=select(lesson,adapter,s)
    assert len(verify_evidence(s,lesson,multiple)['adopted_responses'])==3
    assert len(verify_evidence(s,lesson,single)['adopted_responses'])==1


def test_model_timeout_is_finite_and_distinct_from_model_invalid():
    adapter=Ollama()
    class TimeoutOpener:
        def open(self,*args,**kwargs):raise TimeoutError('synthetic read timeout')
    adapter.opener=TimeoutOpener()
    with pytest.raises(Problem,match='MODEL_REQUEST_TIMEOUT'):adapter._request_local('version',timeout=1)
    for value in (float('inf'),float('nan'),0,601):
        with pytest.raises(Problem,match='INPUT_INVALID'):adapter.request('version',timeout=value)
