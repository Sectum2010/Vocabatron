import io
import json
from pathlib import Path
import zipfile

import pytest
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

from vocabatron.domain import Problem
from vocabatron.pdf import FONT, calibrate, export_pdf, plan, verify_pdf, wrap
from vocabatron.privacy import allowed_name, inspect_archive, inspect_text
from vocabatron.storage import PrivateStore, identifier, sha256
from vocabatron.services import _export_set, rebuild, verify_set, task_status
from vocabatron.domain import PrivateConfig
from .helpers import frozen_for, lattice_witnesses, synthetic_template


@pytest.fixture
def material(tmp_path):
    template=tmp_path/"template.pdf";synthetic_template(template,third=True)
    lesson,a,b=lattice_witnesses(7);frozen=frozen_for(lesson);profile=calibrate(template)
    output=tmp_path/"VersionA_Lesson_3_Crossword.pdf"
    export_pdf(template,output,lesson,a,frozen,profile)
    return template,output,lesson,a,b,frozen,profile


def test_pdf_actual_bytes_and_original_template(material,tmp_path):
    t,o,l,a,b,f,p=material
    report=verify_pdf(t,o,l,a,f,p,expected_name=o.name,preview_dir=tmp_path/"previews")
    assert len(PdfReader(o).pages)==2
    assert report["clue_count"]==7
    assert all(r["outside_character_masks"]==0 for r in report["pages"])
    assert all(r["mask_fraction"]<.1 for r in report["pages"])
    assert (tmp_path/"previews/page-2.png").is_file()


@pytest.mark.parametrize("count",[20,39])
def test_dynamic_large_two_pdf_exports(tmp_path,count):
    t=tmp_path/"template.pdf";synthetic_template(t)
    l,a,b=lattice_witnesses(count);f=frozen_for(l);p=calibrate(t)
    for index,layout in enumerate((a,b)):
        out=tmp_path/f"Version{index}_Lesson_3_Crossword.pdf"
        export_pdf(t,out,l,layout,f,p)
        report=verify_pdf(t,out,l,layout,f,p,expected_name=out.name)
        assert report["clue_count"]==count


def overlay(output,page_index,draw):
    original=PdfReader(output);buffer=io.BytesIO();c=canvas.Canvas(buffer,pagesize=(620,800));draw(c);c.showPage();c.save();buffer.seek(0)
    writer=PdfWriter()
    for page in original.pages:writer.add_page(page)
    writer.pages[page_index].merge_page(PdfReader(buffer).pages[0])
    with output.open("wb") as f:writer.write(f)


@pytest.mark.parametrize("mode",["duplicate_crossing","outside_text","black_block","third_page","changed_letter","wrong_number"])
def test_pdf_error_injections(material,mode):
    t,o,l,a,b,f,p=material
    if mode=="third_page":
        reader=PdfReader(o);writer=PdfWriter()
        for page in reader.pages:writer.add_page(page)
        writer.add_blank_page(620,800)
        with o.open("wb") as stream:writer.write(stream)
    elif mode=="black_block":overlay(o,0,lambda c:c.rect(1,1,30,30,fill=1))
    elif mode=="outside_text":overlay(o,1,lambda c:c.drawString(610,500,"overflow"))
    else:
        items,_=plan(l,a,f,p)
        item=next(i for i in items if i["kind"]==("number" if mode=="wrong_number" else "letter"))
        from reportlab.pdfbase import pdfmetrics
        def draw(c):
            c.setFont(FONT,item["size"])
            ascent,_=pdfmetrics.getAscentDescent(FONT,item["size"])
            text=item["text"] if mode=="duplicate_crossing" else "Z" if mode=="changed_letter" else "99"
            c.drawString(item["x"],800-item["top"]-ascent,text)
        overlay(o,0,draw)
    with pytest.raises(Problem):verify_pdf(t,o,l,a,f,p,expected_name=o.name)


def test_missing_glyph_and_unreadable_overflow():
    with pytest.raises(Problem,match="MISSING_GLYPH"):wrap("constructed 🧬 clue",200,11)
    with pytest.raises(Problem,match="CLUE_TOO_WIDE"):wrap("X"*100,50,11)


def test_clue_column_overflow(material):
    t,o,l,a,b,f,p=material
    changed=dict(p);changed["columns"]=[[45,100,305,115],[305,100,575,115]]
    with pytest.raises(Problem,match="CLUE_OVERFLOW"):plan(l,a,f,changed)


def test_atomic_whole_set_and_rebuild_without_model_solver(material,tmp_path,monkeypatch):
    t,o,l,a,b,f,p=material
    store=PrivateStore(tmp_path/"store")
    config=PrivateConfig(lesson=3,source="sources/source.pdf",template="sources/template.pdf",prefixes=("VersionA","VersionB"))
    result=_export_set(store,"complete",config,l,f,(a,b),p,t,{})
    assert result["status"]=="VERIFIED"
    def forbidden(*args,**kwargs):raise AssertionError("Rebuild must not call model or solver")
    monkeypatch.setattr("vocabatron.clues.Ollama.request",forbidden)
    monkeypatch.setattr("vocabatron.services.solve_pair",forbidden)
    assert rebuild(store,"complete","rebuilt")["status"]=="VERIFIED"
    assert verify_set(store,"rebuilt")["pdf_count"]==2
    assert task_status(store,"rebuilt")["stage"]=="COMPLETE"
    store.json("tasks/complete/state.json",{"stage":"EXPORTING","heartbeat":0})
    assert task_status(store,"complete")["stage"]=="COMPLETE"
    from vocabatron import services
    changed=l.model_copy(update={"lesson":4})
    monkeypatch.setattr(services,"load_inputs",lambda s:(config,changed,frozen_for(changed)))
    with pytest.raises(Problem,match="ACCEPTANCE_INPUT_MISMATCH"):
        services.private_acceptance(store,"complete")


def test_half_set_never_published(tmp_path,monkeypatch):
    s=PrivateStore(tmp_path/"private");stage=s.stage("incomplete")
    s.write(stage/"a.pdf",b"synthetic")
    manifest={"status":"VERIFIED","pdfs":[{"name":"a.pdf","sha256":sha256(s.path(stage/"a.pdf"))},{"name":"b.pdf","sha256":"missing"}]}
    with pytest.raises(Problem,match="INCOMPLETE_SET"):s.publish(stage,"incomplete",manifest)
    assert not s.path("results/incomplete").exists()
    s.write(stage/"b.pdf",b"synthetic second");manifest["pdfs"][1]["sha256"]=sha256(s.path(stage/"b.pdf"))
    import os
    def crash(*args):raise OSError("simulated interruption before directory publication")
    monkeypatch.setattr(os,"rename",crash)
    with pytest.raises(OSError):s.publish(stage,"incomplete",manifest)
    assert not s.path("results/incomplete").exists()
    assert s.path(stage/"a.pdf").exists() and s.path(stage/"b.pdf").exists()


def test_path_boundaries_and_private_archives(tmp_path):
    s=PrivateStore(tmp_path/"private")
    with pytest.raises(Problem):s.path("../escape")
    outside=tmp_path/"outside";outside.mkdir();s.path("link").symlink_to(outside,target_is_directory=True)
    with pytest.raises(Problem):s.path("link/file.json")
    for value in ("../../bad","with space","bad;command","bad/name"):
        with pytest.raises(Problem):identifier(value)
    with pytest.raises(Problem,match="SOURCE_READ_ONLY"):s.write("sources/source.pdf",b"bad")
    assert not allowed_name(".private/lesson.json")
    archive=tmp_path/"bad.whl"
    with zipfile.ZipFile(archive,"w") as z:z.writestr(".private/lesson.json","synthetic secret")
    assert inspect_archive(archive)["issues"]
    assert inspect_text("example.py",b"PUBLIC_TEXT",["PUBLIC_TEXT"])


def test_tracked_private_file_and_synthetic_credential_are_rejected(tmp_path,monkeypatch):
    import subprocess
    from vocabatron.privacy import scan
    private=tmp_path/".private";private.mkdir();(private/"example.json").write_text("synthetic private content")
    def git_read(command,**kwargs):
        names=b".private/example.json\0" if "--cached" in command else b""
        return subprocess.CompletedProcess(command,0,stdout=names)
    monkeypatch.setattr(subprocess,"run",git_read)
    with pytest.raises(Problem,match="PRIVACY_SCAN_FAILED"):scan(tmp_path)
    synthetic=("ghp_"+"A"*36).encode()
    assert inspect_text("synthetic.txt",synthetic)[0]["rule"]=="github_token"
