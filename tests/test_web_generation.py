"""Real queue slices, solver, independent PDF validation and crash recovery.

Only model protocol responses are invented. No layout is given to the solver.
"""
import json
import uuid
import pytest
from vocabatron.app.jobs import Job
from vocabatron.app.archive import Archive
from vocabatron.app.database import encode
from vocabatron.clues import select
from vocabatron.storage import PrivateStore
from vocabatron.domain import Problem,Layout,Placement
from .test_web_app import library,add_lesson,claim
from .helpers import lesson_of,synthetic_template
from .test_hardening_protocols import transport


def prepared(library,answers):
    synthetic_template(library.config.template)
    lid,lesson=add_lesson(library,lesson_of(answers))
    model,_=transport();frozen=select(lesson,model,PrivateStore(library.config.data_root/'core'))
    reference={'object':library.objects.put_json(frozen.model_dump(mode='json')),
               'lesson_object':library.objects.put_json(lesson.model_dump(mode='json'))}
    with library.db.transaction() as c:c.execute('UPDATE lessons SET frozen_ref=? WHERE id=?',(encode(reference),lid))
    library.config.outputs_root.mkdir(exist_ok=True)
    return lid,lesson,frozen


def run_slices(library,tid,limit=40):
    for _ in range(limit):
        fence=uuid.uuid4().hex
        with library.db.transaction() as c:c.execute("UPDATE tasks SET status='RUNNING',intent='run',fence=? WHERE id=?",(fence,tid))
        outcome=Job(library,tid,fence).run()['status']
        with library.db.transaction() as c:c.execute('UPDATE tasks SET status=? WHERE id=?',(outcome,tid))
        if outcome in ('COMPLETED','EXHAUSTED','FAILED','NEEDS_ATTENTION','CANCELLED'):return outcome
    pytest.fail('Bounded synthetic queue failed to finish')


def test_requested_counts_real_queue_pdf_and_global_history(library,monkeypatch):
    lid,lesson,frozen=prepared(library,['AAAAAB','AAAAAC'])
    def no_model(*args,**kwargs):raise AssertionError('Generation from frozen evidence must not call the model')
    monkeypatch.setattr('vocabatron.clues.Ollama.request',no_model)
    total=0
    for count in (1,2,3,5):
        request=library.submit({'idempotency_key':uuid.uuid4().hex,'targets':[{'lesson_id':lid,'mode':'count','count':count}]})
        tid=request['tasks'][0]['id'];assert run_slices(library,tid)=='COMPLETED'
        assert library.db.one('SELECT completed FROM tasks WHERE id=?',(tid,))['completed']==count
        total+=count
        history=Archive(library).history(library.lesson(lid)[0]['family_id'])
        assert len(history)==total
        assert len({s['geometry_hash'] for s in history})==len({s['crossing_hash'] for s in history})==total
    assert [r['variant_number'] for r in history]==list(range(1,12))
    artifacts=library.db.all('SELECT * FROM artifacts')
    for item in artifacts:
        assert item['state']=='AVAILABLE'
        report=library.objects.read_json(item['report']);assert report['status']=='VERIFIED'
    assert len(list(library.config.outputs_root.rglob('*.pdf')))==11
    # Explicit saved-file validation and restoration use the original snapshot.
    item=artifacts[-1]
    from vocabatron.app.database import task_record,insert_task
    task=task_record('verify',{'artifact_id':item['id']},lesson_id=lid)
    with library.db.transaction() as c:insert_task(c,task)
    assert run_slices(library,task['id'])=='COMPLETED'


def test_publication_checkpoint_survives_cancel_and_counts_once(library,monkeypatch):
    from vocabatron.pdf import calibrate
    lid,lesson,frozen=prepared(library,['AB','AC']);tid,fence=claim(library,lid)
    archive=Archive(library)
    layout=Layout(lesson_version=lesson.version,placements=(Placement(word_id=lesson.words[0].word_id,row=0,col=0,direction='across'),Placement(word_id=lesson.words[1].word_id,row=0,col=0,direction='down')))
    item=archive.reserve(tid,fence,lesson,layout,'synthetic-checkpoint')
    original=library.objects.put_json
    def cancel_after_verification(value):
        ref=original(value)
        if isinstance(value,dict) and value.get('delivery_id')=='synthetic-checkpoint':
            library.control(tid,'cancel')
        return ref
    monkeypatch.setattr(library.objects,'put_json',cancel_after_verification)
    with pytest.raises(Problem):archive.render(item,tid,fence,lesson,frozen,library.config.template,calibrate(library.config.template))
    assert library.db.one('SELECT completed FROM tasks WHERE id=?',(tid,))['completed']==0
    monkeypatch.setattr(library.objects,'put_json',original)
    with library.db.transaction() as c:c.execute("UPDATE tasks SET status='RUNNING',intent='run' WHERE id=?",(tid,))
    def forbidden(*args,**kwargs):raise AssertionError('Durable verified publication must not render or solve again')
    monkeypatch.setattr('vocabatron.app.archive.export_pdf',forbidden)
    monkeypatch.setattr('vocabatron.app.archive.verify_pdf',forbidden)
    monkeypatch.setattr('vocabatron.solver.ExactModel.solve',forbidden)
    assert archive.recover_verified(item,tid,fence,lesson)
    assert archive.recover_verified(item,tid,fence,lesson)
    assert library.db.one('SELECT completed FROM tasks WHERE id=?',(tid,))['completed']==1
