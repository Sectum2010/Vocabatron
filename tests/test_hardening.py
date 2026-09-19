"""Synthetic regression evidence. No private source data or Git mutations."""
from pathlib import Path
import hashlib
import io

import pytest
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

from vocabatron.domain import Problem
from vocabatron.ingest import ingest, check_transcription
from vocabatron.pdf import calibrate, export_pdf, plan, verify_pdf, wrap, FONT, font
from vocabatron.storage import PrivateStore
from .helpers import synthetic_handout, synthetic_template, lattice_witnesses, frozen_for


def tree_hash(root):
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file() and 'checks' not in p.parts}


def test_check_is_read_only(tmp_path):
    source=tmp_path/'input.pdf';synthetic_handout(source);s=PrivateStore(tmp_path/'store')
    lesson,_=ingest(source,3,s)
    transcript=Path(s.read('current.json')['lesson_path']).with_name('transcript.md')
    s.write(transcript,s.path(transcript).read_bytes()+b'\n')
    before=tree_hash(s.root)
    assert check_transcription(source,lesson,s)['status']=='VERIFIED'
    assert tree_hash(s.root)==before


def test_ordered_saved_fields_are_checked(tmp_path):
    source=tmp_path/'input.pdf';synthetic_handout(source);s=PrivateStore(tmp_path/'store')
    lesson,_=ingest(source,3,s)
    first=lesson.words[0]
    changed=first.model_copy(update={'raw':first.raw[::-1], 'letters':first.letters[::-1]})
    bad=lesson.model_copy(update={'words':(changed,*lesson.words[1:])})
    before=tree_hash(s.root)
    with pytest.raises(Problem):check_transcription(source,bad,s)
    assert tree_hash(s.root)==before


def test_width_falls_back_before_rejecting(tmp_path):
    t=tmp_path/'template.pdf';synthetic_template(t)
    lesson,a,b=lattice_witnesses(7);f=frozen_for(lesson);profile=calibrate(t)
    w=lesson.words[0];candidate=w.candidates[0].model_copy(update={'text':'W'*24})
    lesson=lesson.model_copy(update={'words':(w.model_copy(update={'candidates':(candidate,)}),*lesson.words[1:])})
    a=a.model_copy(update={'lesson_version':lesson.version});f=frozen_for(lesson)
    font()
    from reportlab.pdfbase import pdfmetrics
    width=(pdfmetrics.stringWidth('W'*24,FONT,12)+pdfmetrics.stringWidth('W'*24,FONT,11.5))/2
    profile={**profile,'columns':[[0,100,width+24,790],[310,100,310+width+24,790]]}
    items,_=plan(lesson,a,f,profile)
    assert any(x['kind']=='clue' and x['size']==11.5 for x in items)


def test_partial_visible_glyph_damage_is_rejected(tmp_path):
    t=tmp_path/'template.pdf';synthetic_template(t)
    l,a,b=lattice_witnesses(7);f=frozen_for(l);p=calibrate(t);o=tmp_path/'Version.pdf'
    items,_=export_pdf(t,o,l,a,f,p)
    item=next(i for i in items if i['kind']=='letter' and i['text'] not in 'I')
    # Keep text and almost all ink; the former any-ink test accepted this.
    import pypdfium2 as pdfium
    with pdfium.PdfDocument(str(o)) as doc:
        text=doc[0].get_textpage()
        boxes=[]
        for i in range(text.count_chars()):
            if text.get_text_range(i,1)==item['text']:
                left,bottom,right,top=text.get_charbox(i)
                boxes.append((abs(left-item['x'])+abs(800-top-item['top']),left,bottom,right,top))
        _,left,bottom,right,top=min(boxes)
    buf=io.BytesIO();c=canvas.Canvas(buf,pagesize=(620,800));c.setFillColorRGB(1,1,1)
    c.rect(left+.1,(top+bottom)/2,(right-left)/2,.7,stroke=0,fill=1);c.save();buf.seek(0)
    r=PdfReader(o);wri=PdfWriter();page=wri.add_page(r.pages[0]);page.merge_page(PdfReader(buf).pages[0]);wri.add_page(r.pages[1])
    o.write_bytes(b'')
    with o.open('wb') as out:wri.write(out)
    with pytest.raises(Problem):verify_pdf(t,o,l,a,f,p,expected_name=o.name)


@pytest.mark.parametrize('size',[11.5,11.0])
def test_readable_size_fallback_and_direction_specific_width(tmp_path,size):
    from vocabatron.pdf import clue_capacity
    from reportlab.pdfbase import pdfmetrics
    t=tmp_path/'template.pdf';synthetic_template(t);profile=calibrate(t);font()
    lesson,a,b=lattice_witnesses(7);word=lesson.words[0]
    candidate=word.candidates[0].model_copy(update={'text':'W'*24})
    lesson=lesson.model_copy(update={'words':(word.model_copy(update={'candidates':(candidate,)}),*lesson.words[1:])})
    a=a.model_copy(update={'lesson_version':lesson.version});f=frozen_for(lesson)
    width=pdfmetrics.stringWidth(candidate.text,FONT,size)+.02
    profile={**profile,'columns':[[0,100,width+24,790],[330,100,620,790]]}
    items,_=plan(lesson,a,f,profile)
    assert min(i['size'] for i in items if i['kind']=='clue' and i['direction']=='across')==size
    narrow={**profile,'columns':[[0,100,70,790],[100,100,620,790]]}
    costs,capacities=clue_capacity(lesson,f,narrow)
    assert costs[word.word_id][0]>capacities[0] and costs[word.word_id][1]<=capacities[1]
    impossible={**profile,'columns':[[0,100,70,790],[330,100,620,790]]}
    with pytest.raises(Problem,match='CLUE_OVERFLOW'):plan(lesson,a,f,impossible)
