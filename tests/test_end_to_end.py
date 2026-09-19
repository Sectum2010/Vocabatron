"""Normal production entries, protocol-only model mocks, real exact solver and PDF."""
from pathlib import Path
import pytest
from vocabatron import services
from vocabatron.storage import PrivateStore,sha256
from vocabatron.domain import Problem,SolverOptions,Layout
from vocabatron.validation import validate_pair
from .helpers import lattice_witnesses,synthetic_course,synthetic_template
from .test_hardening_protocols import transport


@pytest.mark.parametrize('count',[7,20,39])
def test_normal_pipeline_without_supplied_layout(tmp_path,count,monkeypatch):
    witness_lesson,a,b=lattice_witnesses(count)
    validate_pair(witness_lesson,a,b) # Establish existence; neither layout reaches production.
    s=PrivateStore(tmp_path/'store');s.path('sources').mkdir()
    synthetic_course(s.path('sources/input.pdf'),[w.raw for w in witness_lesson.words])
    synthetic_template(s.path('sources/template.pdf'))
    s.json('config.json',{'lesson':3,'source':'sources/input.pdf','template':'sources/template.pdf',
        'prefixes':['VersionA','VersionB'],'expected_words':count,
        'solver':{'seconds_per_layout':90,'optimization_seconds':0,'workers':4}})
    assert services.import_lesson(s)['words']==count
    adapter,_=transport();services.select_clues(s,adapter=adapter)
    config,lesson,frozen=services.load_inputs(s)
    assert services.generate(s,'normal')['status']=='VERIFIED'
    assert services.verify_set(s,'normal')['words']==count
    assert services.task_status(s,'normal')['stage']=='COMPLETE'
    def forbidden(*a,**kw):raise AssertionError('Idempotency/rebuild must not select or solve')
    monkeypatch.setattr(services,'solve_pair',forbidden)
    monkeypatch.setattr('vocabatron.clues.Ollama.request',forbidden)
    assert services.generate(s,'normal',SolverOptions(seconds_per_layout=120,optimization_seconds=0))['status']=='VERIFIED'
    assert services.rebuild(s,'normal','rebuilt')['status']=='VERIFIED'
    manifest=s.read('results/normal/manifest.json')
    assert [sha256(s.path('results/normal')/p['name']) for p in manifest['pdfs']]==[sha256(s.path('results/rebuilt')/p['name']) for p in manifest['pdfs']]
    s.json('config.json',config.model_copy(update={'prefixes':('DifferentA','DifferentB')}))
    with pytest.raises(Problem,match='TASK_INPUT_CHANGED'):services.generate(s,'normal')
    assert services.verify_set(s,'normal')['words']==count # historical snapshots survive current changes
