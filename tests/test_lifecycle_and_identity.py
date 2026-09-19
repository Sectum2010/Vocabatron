"""Complete-set identity, retry and publication boundaries without Git mutations."""
import json
from pathlib import Path
import shutil

import pytest
from vocabatron import services
from vocabatron.domain import Problem,PrivateConfig,Layout,SolverOptions,digest
from vocabatron.execution import cancel_attempt
from vocabatron.storage import PrivateStore,sha256
from vocabatron.requests import request_identity
from vocabatron.manifest import validate_manifest
from vocabatron.pdf import calibrate
from vocabatron.optimization import optimize_pair,score,accept
from .helpers import lattice_witnesses,frozen_for,synthetic_template,synthetic_course
from .test_hardening_protocols import transport


@pytest.fixture(scope='module')
def production_pair(tmp_path_factory):
    s=PrivateStore(tmp_path_factory.mktemp('production_pair')/'store');s.path('sources').mkdir()
    original,a,b=lattice_witnesses(7)
    synthetic_course(s.path('sources/input.pdf'),[w.raw for w in original.words]);synthetic_template(s.path('sources/template.pdf'))
    s.json('config.json',{'lesson':3,'source':'sources/input.pdf','template':'sources/template.pdf',
        'prefixes':['VersionA','VersionB'],'solver':{'seconds_per_layout':90,'optimization_seconds':0}})
    services.import_lesson(s);adapter,_=transport();services.select_clues(s,adapter=adapter)
    services.generate(s,'base')
    return s


def checkpoint_copy(original,tmp_path):
    root=tmp_path/'store';shutil.copytree(original.root,root);s=PrivateStore(root)
    # These are real production checkpoints from the fixture, not witness layouts.
    for name in ('identity.json','layout-1.json','layout-2.json'):
        s.write(Path('tasks/retry')/name,s.path(Path('tasks/base')/name).read_bytes())
    return s


def test_export_failure_resumes_both_verified_layouts(production_pair,tmp_path,monkeypatch):
    s=checkpoint_copy(production_pair,tmp_path)
    def forbidden(*a,**kw):raise AssertionError('must not solve two restored verified layouts')
    monkeypatch.setattr(services,'solve_pair',forbidden)
    original=services.export_pdf
    def fail(*a,**kw):raise OSError('synthetic export failure')
    monkeypatch.setattr(services,'export_pdf',fail)
    with pytest.raises(OSError):services.generate(s,'retry')
    assert services.task_status(s,'retry')['stage']=='INTERNAL_ERROR'
    assert all(s.path(f'tasks/retry/layout-{i}.json').is_file() for i in (1,2))
    monkeypatch.setattr(services,'export_pdf',original)
    assert services.generate(s,'retry')['status']=='VERIFIED'
    assert services.task_status(s,'retry')['stage']=='COMPLETE'
    assert s.read('results/retry/metrics.json')['solver']['solver']=='NOT_RUN'


@pytest.mark.parametrize('stage',['CALIBRATING','OPTIMIZING','EXPORTING','EXPORTING_PDF_1','VALIDATING_PDF_1','EXPORTING_PDF_2','VALIDATING_PDF_2','PUBLISHING'])
def test_cancel_accepted_before_publication(production_pair,tmp_path,stage,monkeypatch):
    s=checkpoint_copy(production_pair,tmp_path)
    def progress(current):
        if current==stage:cancel_attempt(s,'retry')
    with pytest.raises(Problem,match='CANCELLED'):services.generate(s,'retry',progress=progress)
    assert not s.path('results/retry').exists()
    assert services.task_status(s,'retry')['stage']=='CANCELLED'
    assert services.generate(s,'retry')['status']=='VERIFIED'
    assert cancel_attempt(s,'retry')['status']=='COMPLETE'


def test_completion_survives_terminal_state_write_failure(production_pair,tmp_path,monkeypatch):
    s=checkpoint_copy(production_pair,tmp_path);original=s.json
    def fail(path,value):
        if str(path).endswith('/state.json') and value.get('stage')=='COMPLETE':raise PermissionError('simulated state write failure')
        return original(path,value)
    monkeypatch.setattr(s,'json',fail)
    assert services.generate(s,'retry')['status']=='VERIFIED'
    assert services.task_status(s,'retry')['stage']=='COMPLETE'


def test_atomic_publish_failure_has_no_result(production_pair,tmp_path,monkeypatch):
    s=checkpoint_copy(production_pair,tmp_path)
    def fail(*args):raise PermissionError('simulated directory publication failure')
    with monkeypatch.context() as m:
        m.setattr('vocabatron.storage.os.rename',fail)
        with pytest.raises(Problem,match="PUBLISH_FAILED"):services.generate(s,'retry')
    assert not s.path('results/retry').exists()
    assert services.generate(s,'retry')['status']=='VERIFIED'


@pytest.mark.parametrize('mode',['empty','missing_snapshot','wrong_task','duplicate_pdf','path','cross_object'])
def test_manifest_fail_closed(production_pair,tmp_path,mode):
    s=checkpoint_copy(production_pair,tmp_path);p=s.path('results/base/manifest.json');data=json.loads(p.read_text())
    if mode=='empty':data['snapshot_hashes']={}
    if mode=='missing_snapshot':data['snapshot_hashes'].pop('lesson.json')
    if mode=='wrong_task':data['task_id']='different'
    if mode=='duplicate_pdf':data['pdfs'][1]=data['pdfs'][0]
    if mode=='path':data['pdfs'][0]['name']='../escape.pdf'
    if mode=='cross_object':data['frozen_version']='different'
    p.write_text(json.dumps(data))
    with pytest.raises(Problem):services.verify_set(s,'base')


@pytest.mark.parametrize('mode',['prefix','lesson','frozen','template','style','seed','provenance'])
def test_delivery_identity_changes_and_execution_budget_does_not(production_pair,mode,monkeypatch):
    s=production_pair;config,lesson,frozen=services.load_inputs(s);th=sha256(s.path(config.template))
    original=request_identity(config,lesson,frozen,th)
    if mode=='prefix':config=config.model_copy(update={'prefixes':('OtherA','OtherB')})
    elif mode=='lesson':lesson=lesson.model_copy(update={'lesson':4})
    elif mode=='frozen':frozen=frozen.model_copy(update={'prompt_version':'changed'})
    elif mode=='template':th='b'*64
    elif mode=='style':monkeypatch.setitem(services.request_identity.__globals__['OUTPUT_RULES'],'entry_gap',6)
    elif mode=='seed':config=config.model_copy(update={'solver':config.solver.model_copy(update={'seed':38})})
    else:frozen=frozen.model_copy(update={'selection_run_id':'different'})
    assert request_identity(config,lesson,frozen,th)!=original
    cfg,les,fr=services.load_inputs(s)
    changed=cfg.model_copy(update={'solver':cfg.solver.model_copy(update={'workers':2,'seconds_per_layout':200,'optimization_seconds':5})})
    assert request_identity(cfg,les,fr,th)==request_identity(changed,les,fr,th)


def test_bounded_optimization_never_loses_pair_and_cancel_is_not_fallback():
    lesson,a,b=lattice_witnesses(7);typography=lambda layout:None
    pair,report=optimize_pair(lesson,(a,b),SolverOptions(optimization_seconds=0),typography)
    assert pair==(a,b) and report['status']=='NOT_RUN'
    pair,report=optimize_pair(lesson,(a,b),SolverOptions(optimization_seconds=.05),typography)
    assert all(score(lesson,p)>=score(lesson,q) for p,q in zip(pair,(a,b)))
    assert report['status']=='IMPROVED_NOT_PROVEN' and report['proof']=='NONE'
    with pytest.raises(Problem,match='CANCELLED'):
        optimize_pair(lesson,(a,b),SolverOptions(optimization_seconds=.05),typography,cancel=lambda:True)
    with pytest.raises(Problem):accept(lesson,a,b,b,typography)
    def rejected(layout):raise Problem('CLUE_OVERFLOW','synthetic typography failure')
    pair,report=optimize_pair(lesson,(a,b),SolverOptions(optimization_seconds=.05),rejected)
    assert pair==(a,b) and report['fallback_reasons']


@pytest.mark.parametrize('phase',['MODELING','HINTING_FIRST','SOLVING_FIRST','HINTING_SECOND','SOLVING_SECOND'])
def test_cancel_through_production_solver_phases(production_pair,tmp_path,phase):
    s=checkpoint_copy(production_pair,tmp_path)
    s.path('tasks/retry/layout-2.json').unlink()
    if phase in {'MODELING','HINTING_FIRST','SOLVING_FIRST'}:s.path('tasks/retry/layout-1.json').unlink()
    def progress(current):
        if current==phase:cancel_attempt(s,'retry')
    with pytest.raises(Problem,match='CANCELLED'):services.generate(s,'retry',progress=progress)
    assert services.task_status(s,'retry')['stage']=='CANCELLED'
    assert not s.path('results/retry').exists()


def test_corrupted_checkpoint_is_not_reused(production_pair,tmp_path,monkeypatch):
    s=checkpoint_copy(production_pair,tmp_path);layout=s.read('tasks/retry/layout-2.json')
    layout['placements'][0]['row']=-1;s.json('tasks/retry/layout-2.json',layout)
    called=[]
    def stop_at_solver(*a,**kw):
        called.append(kw.get('saved_first'))
        raise Problem('UNKNOWN','synthetic stop after validating checkpoint')
    monkeypatch.setattr(services,'solve_pair',stop_at_solver)
    with pytest.raises(Problem,match='UNKNOWN'):services.generate(s,'retry')
    assert len(called)==1 and called[0] is not None
    assert not s.path('results/retry').exists()


def test_completed_id_rejects_source_change_before_loading_inputs(production_pair,tmp_path):
    s=checkpoint_copy(production_pair,tmp_path);source=s.path('sources/input.pdf')
    source.write_bytes(source.read_bytes()+b'\n% synthetic source change\n')
    with pytest.raises(Problem,match='TASK_INPUT_CHANGED'):services.generate(s,'base')
    # Historical verification uses its own lesson, template and evidence snapshots.
    assert services.verify_set(s,'base')['status']=='VERIFIED'


def test_new_result_missing_model_evidence_cannot_claim_full_verification(production_pair,tmp_path):
    s=checkpoint_copy(production_pair,tmp_path)
    _,_,frozen=services.load_inputs(s)
    path=s.path(f'model/runs/{frozen.selection_run_id}/batch-0-attempt-0-response.json');path.unlink()
    with pytest.raises(Problem,match='MODEL_EVIDENCE_MISSING'):services.verify_set(s,'base')
    with pytest.raises(Problem,match='MODEL_EVIDENCE_MISSING'):services.generate(s,'base')
