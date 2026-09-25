"""Persistent failure boundaries, exports and complete tiny search domains."""
import json
import os
import time
import uuid
from pathlib import Path
import pytest
from vocabatron.domain import Layout,Placement,Problem
from vocabatron.app.archive import Archive
from vocabatron.app.exports import restore_one
from vocabatron.app.database import encode
from vocabatron.app.scheduler import Scheduler
from vocabatron.app.backup import backup,restore_snapshot
from vocabatron.app.search import next_structure
from vocabatron.app.identity import structure
from .test_web_app import library,add_lesson,claim,client
from .helpers import frozen_for,lesson_of,synthetic_template


def artifact(library):
    lid,lesson=add_lesson(library);tid,fence=claim(library,lid)
    layout=Layout(lesson_version=lesson.version,placements=(Placement(word_id=lesson.words[0].word_id,row=0,col=0,direction='across'),Placement(word_id=lesson.words[1].word_id,row=0,col=0,direction='down')))
    record=Archive(library).reserve(tid,fence,lesson,layout,'synthetic-delivery')
    raw=b'%PDF-Synthetic HTTP and export fixture only\n';reference,key=library.objects.put(raw,'.pdf')
    with library.db.transaction() as c:
        c.execute("UPDATE artifacts SET path=?,sha256=?,bytes=?,state='AVAILABLE' WHERE id=?",(reference,key,len(raw),record['id']))
    library.config.outputs_root.mkdir(mode=0o700,exist_ok=True)
    return library.db.one('SELECT * FROM artifacts WHERE id=?',(record['id'],)),raw


def test_export_independent_copy_conflict_deletion_and_symlink(library):
    saved,raw=artifact(library);result=restore_one(library,saved);assert result['status']=='AVAILABLE',result
    path=library.config.outputs_root/result['directory']/result['filename']
    original=library.objects.path(saved['path']);assert path.stat().st_ino!=original.stat().st_ino
    before=library.db.one('SELECT next_variant FROM answer_sets')['next_variant']
    path.unlink();assert restore_one(library,saved)['status']=='AVAILABLE';assert path.read_bytes()==raw
    assert library.db.one('SELECT next_variant FROM answer_sets')['next_variant']==before
    path.write_bytes(b'User modified export')
    assert restore_one(library,saved)['status']=='CONFLICT';assert path.read_bytes()==b'User modified export';assert original.read_bytes()==raw
    path.unlink();path.symlink_to(original)
    assert restore_one(library,saved)['status']!='AVAILABLE';assert original.read_bytes()==raw
    path.unlink();os.mkfifo(path)
    assert restore_one(library,saved)['status']=='CONFLICT'


def test_export_stays_anonymous_until_verified_atomic_publication(library,monkeypatch):
    saved,raw=artifact(library);link=os.link;observed=[]
    def publish(source,destination,*,dst_dir_fd,follow_symlinks):
        descriptor=int(source.rsplit('/',1)[1]);info=os.fstat(descriptor)
        assert info.st_nlink==0 and info.st_mode & 0o777==0o600
        assert os.listdir(dst_dir_fd)==[]
        assert os.pread(descriptor,len(raw),0)==raw
        assert not (library.config.runtime_root/'exports').exists()
        observed.append(descriptor)
        return link(source,destination,dst_dir_fd=dst_dir_fd,follow_symlinks=follow_symlinks)
    monkeypatch.setattr(os,'link',publish)
    result=restore_one(library,saved);assert result['status']=='AVAILABLE',result
    path=library.config.outputs_root/result['directory']/result['filename']
    assert path.read_bytes()==raw and path.stat().st_nlink==1 and len(observed)==1
    with pytest.raises(OSError):os.fstat(observed[0])


def test_export_atomic_publication_preserves_racing_destination(library,monkeypatch):
    saved,raw=artifact(library);link=os.link
    def raced(source,destination,*,dst_dir_fd,follow_symlinks):
        descriptor=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600,dir_fd=dst_dir_fd)
        try:os.write(descriptor,b'An independently created export')
        finally:os.close(descriptor)
        return link(source,destination,dst_dir_fd=dst_dir_fd,follow_symlinks=follow_symlinks)
    monkeypatch.setattr(os,'link',raced)
    result=restore_one(library,saved);assert result['status']=='CONFLICT',result
    files=list(library.config.outputs_root.rglob('*'))
    assert len(files)==2 and files[1].read_bytes()==b'An independently created export'
    assert library.objects.path(saved['path']).read_bytes()==raw


def test_export_copy_failure_removes_anonymous_partial_bytes(library,monkeypatch):
    saved,raw=artifact(library)
    def interrupted(*_args,**_kwargs):raise OSError('Synthetic interrupted source read')
    monkeypatch.setattr(library.objects,'open_verified',interrupted)
    result=restore_one(library,saved);assert result['status']=='FAILED_RETRYABLE',result
    assert not list(library.config.outputs_root.rglob('*.pdf'))
    assert all(p.is_dir() for p in library.config.outputs_root.rglob('*'))
    assert library.objects.path(saved['path']).read_bytes()==raw


def test_pdf_range_head_private_and_no_render(library,monkeypatch):
    saved,raw=artifact(library)
    def forbid(*_args,**_kwargs):raise AssertionError('Reading a saved PDF must not parse, render or solve')
    monkeypatch.setattr('vocabatron.pdf.verify_pdf',forbid);monkeypatch.setattr('vocabatron.solver.ExactModel.solve',forbid)
    c=client(library);path='api/artifacts/'+saved['id']+'/pdf'
    response=c.get(path);assert response.content==raw;assert response.headers['content-type']=='application/pdf'
    assert c.head(path).content==b''
    response=c.get(path,headers={'Range':'bytes=0-4'});assert response.status_code==206 and response.content==raw[:5]
    assert c.get(path,headers={'Range':'bytes=0-1,3-4'}).status_code==416
    assert c.get(path,headers={'Range':'bytes=999999-'}).status_code==416
    assert c.get(path,headers={'Tailscale-User-Login':'unauthorized@example.test','Range':'bytes=0-4'}).status_code==403
    assert saved['filename'] in c.get(path+'?download=1').headers['content-disposition']


def test_recovery_checks_process_identity_not_stale_lease(library):
    from vocabatron.execution import owner_identity
    lid,_=add_lesson(library);tid,fence=claim(library,lid)
    with library.db.transaction() as c:c.execute('UPDATE tasks SET owner=?,lease_until=0 WHERE id=?',(encode(owner_identity()),tid))
    scheduler=Scheduler(library.config,'synthetic-config');scheduler.recover()
    assert library.db.one('SELECT status FROM tasks WHERE id=?',(tid,))['status']=='RUNNING'
    bad={**owner_identity(),'start_ticks':'not-this-process'}
    with library.db.transaction() as c:c.execute('UPDATE tasks SET owner=? WHERE id=?',(encode(bad),tid))
    scheduler.recover();row=library.db.one('SELECT status,fence FROM tasks WHERE id=?',(tid,))
    assert row=={'status':'INTERRUPTED','fence':None}


def test_completed_count_is_not_regenerated_after_crash(library,monkeypatch):
    from vocabatron.app.jobs import Job
    lid,_=add_lesson(library);tid,fence=claim(library,lid)
    with library.db.transaction() as c:c.execute('UPDATE tasks SET completed=target_count WHERE id=?',(tid,))
    job=Job(library,tid,fence)
    def fail():raise AssertionError('No inference after completed publication')
    job.prepare=fail
    assert job.generate()=='COMPLETED'


def test_cancel_keeps_reserved_tombstone_and_stops_publication(library):
    saved,_=artifact(library)
    result=library.control(saved['task_id'],'cancel');assert result['intent']=='cancel'
    assert library.db.one('SELECT count(*) n FROM structures')['n']==1
    assert library.db.one('SELECT count(*) n FROM artifacts')['n']==1


def test_online_backup_reference_closure_and_isolated_restore(library):
    saved,_=artifact(library)
    nested=library.objects.put_json({'pdf':saved['path']})
    with library.db.transaction() as c:c.execute('UPDATE artifacts SET report=? WHERE id=?',(nested,saved['id']))
    destination=library.config.data_root/'backups'/'invented-snapshot'
    backup(library,destination)
    restored=library.config.data_root/'restored-test';assert restore_snapshot(destination,restored)['status']=='VERIFIED'
    assert (restored/'objects'/saved['path']).read_bytes()==library.objects.path(saved['path']).read_bytes()
    assert (restored/'objects'/nested).is_file()
    with pytest.raises(Problem):restore_snapshot(destination,restored)


def test_complete_projected_enumeration_zero_one_and_multiple(library):
    from vocabatron.pdf import calibrate
    root=library.config.runtime_root;root.mkdir(parents=True,exist_ok=True);template=root/'invented-template.pdf';synthetic_template(template)
    profile=calibrate(template)
    for answers,size,expected in [(['AB','CD'],3,0),(['AB','AC'],3,1),(['ABA','ACA'],3,1),(['ABAB','ACAC'],4,4)]:
        lesson=lesson_of(answers);frozen=frozen_for(lesson);history=[]
        for _ in range(20):
            layout,evidence=next_structure(lesson,frozen,profile,history,[],seconds=4,threads=1,size=size)
            if layout is None:
                assert evidence['status']=='INFEASIBLE';break
            item=structure(lesson,layout);history.append(item)
        else:pytest.fail('Tiny finite domain did not terminate')
        # Palindromic endpoints are equivalent under reflection. Crossing indices
        # remain a separate class constraint; every returned representative must
        # also pass the full letter-owner dihedral comparison.
        assert len(history)==expected
        # Independent Cartesian enumeration of word placements uses no solver,
        # flow variables, hints or production no-good constraints.
        from itertools import product
        from vocabatron.validation import validate_layout
        domains=[]
        for word in lesson.words:
            domains.append([Placement(word_id=word.word_id,row=r,col=c,direction=direction)
                for direction in ('across','down') for r in range(size) for c in range(size)
                if (c if direction=='across' else r)+len(word.letters)<=size])
        crossed={s['crossing_hash'] for s in history};shapes={s['geometry_hash'] for s in history};valid=0
        for placements in product(*domains):
            candidate=Layout(lesson_version=lesson.version,size=size,placements=placements)
            try:validate_layout(lesson,candidate)
            except Problem:continue
            valid+=1;entry=structure(lesson,candidate)
            assert entry['crossing_hash'] in crossed or entry['geometry_hash'] in shapes
        assert bool(valid)==bool(expected)


def test_unknown_is_never_exhaustion(library,monkeypatch):
    from vocabatron.app import search
    from vocabatron.pdf import calibrate
    root=library.config.runtime_root;root.mkdir(parents=True,exist_ok=True);template=root/'invented-template.pdf';synthetic_template(template)
    lesson=lesson_of(['AB','AC']);profile=calibrate(template)
    def unknown(*args,**kwargs):raise Problem('UNKNOWN','Synthetic interrupted search')
    monkeypatch.setattr(search.ExactModel,'solve',unknown)
    monkeypatch.setattr(search,'warm_hint',lambda *args,**kwargs:(None,{'complete':False}))
    with pytest.raises(Problem,match='UNKNOWN') as caught:next_structure(lesson,frozen_for(lesson),profile,[],[],seconds=1,size=3)
    assert caught.value.details['status']=='UNKNOWN'
    assert caught.value.details['parameters']['seconds']==1
    assert caught.value.details['history_hash'] and caught.value.details['model']['placements']>0


def test_complete_hint_requires_independent_validation_and_full_history(library,monkeypatch):
    from vocabatron.app import search
    from vocabatron.pdf import calibrate
    root=library.config.runtime_root;root.mkdir(parents=True,exist_ok=True)
    template=root/'hint-template.pdf';synthetic_template(template);profile=calibrate(template)
    lesson=lesson_of(['AB','AC']);frozen=frozen_for(lesson)
    good=Layout(lesson_version=lesson.version,size=3,placements=(
        Placement(word_id=lesson.words[0].word_id,row=0,col=0,direction='across'),
        Placement(word_id=lesson.words[1].word_id,row=0,col=0,direction='down')))
    monkeypatch.setattr(search,'warm_hint',lambda *args,**kwargs:(good,{'complete':True}))
    monkeypatch.setattr(search,'history_hint',lambda *args,**kwargs:(good,{'complete':True}))
    result,proof=next_structure(lesson,frozen,profile,[],[],seconds=3,size=3)
    assert result==good and proof['status']=='VERIFIED_CANDIDATE' and not proof['solver_called']
    result,proof=next_structure(lesson,frozen,profile,[structure(lesson,good)],[],seconds=3,size=3)
    assert result is None and proof['status']=='INFEASIBLE'
    invalid=good.model_copy(update={'placements':(good.placements[0],good.placements[1].model_copy(update={'direction':'across'}))})
    monkeypatch.setattr(search,'warm_hint',lambda *args,**kwargs:(invalid,{'complete':True}))
    result,proof=next_structure(lesson,frozen,profile,[],[],seconds=3,size=3)
    assert result is not None and result!=invalid and proof['status']!='VERIFIED_CANDIDATE'
