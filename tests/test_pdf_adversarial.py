"""Mutate real content streams, leaving expected Unicode and manifest irrelevant."""
import io
from pathlib import Path

import pytest
from pypdf import PdfReader,PdfWriter
from pypdf.generic import ContentStream,FloatObject,NameObject,NumberObject,DecodedStreamObject
from reportlab.pdfgen import canvas

from vocabatron.pdf import calibrate,export_pdf,verify_pdf,plan
from vocabatron.domain import Problem
from .helpers import lattice_witnesses,synthetic_template,frozen_for


def damage_text(path,letter,mode):
    r=PdfReader(path);w=PdfWriter();w.append(r)
    page=w.pages[0];stream=ContentStream(page.get_contents(),w);changed=False;out=[]
    for operands,op in stream.operations:
        if not changed and op==b'Tj' and str(operands[0])==letter:
            changed=True
            if mode=='invisible':out.append(([NumberObject(3)],b'Tr'))
            if mode=='white':out.append(([NumberObject(1)]*3,b'rg'))
            if mode=='move':out.append(([NumberObject(12),NumberObject(0)],b'Td'))
            if mode=='wrong_glyph':operands=[type(operands[0])('Z')]
            if mode=='clip':
                out.extend([([],b'q'),([NumberObject(0)]*4,b're'),([],b'W'),([],b'n')])
            out.append((operands,op))
            if mode=='duplicate':out.append((operands,op))
            if mode=='invisible':out.append(([NumberObject(0)],b'Tr'))
            if mode=='white':out.append(([NumberObject(0)]*3,b'rg'))
            if mode=='clip':out.append(([],b'Q'))
        else:out.append((operands,op))
    assert changed
    stream.operations=out;page[NameObject('/Contents')]=w._add_object(stream)
    with path.open('wb') as f:w.write(f)


@pytest.fixture
def rendered(tmp_path):
    t=tmp_path/'template.pdf';synthetic_template(t);l,a,b=lattice_witnesses(7);f=frozen_for(l);p=calibrate(t)
    o=tmp_path/'Version.pdf';items,_=export_pdf(t,o,l,a,f,p)
    return t,o,l,a,f,p,items


@pytest.mark.parametrize('mode',['invisible','white','move','wrong_glyph','duplicate','clip'])
def test_content_stream_damage_rejected(rendered,mode):
    t,o,l,a,f,p,items=rendered
    damage_text(o,next(i['text'] for i in items if i['kind']=='letter'),mode)
    with pytest.raises(Problem):verify_pdf(t,o,l,a,f,p,expected_name=o.name)


@pytest.mark.parametrize('color',[0,1])
def test_partial_occlusion_and_nearby_template_damage(rendered,color,tmp_path):
    t,o,l,a,f,p,items=rendered
    item=next(i for i in items if i['kind']=='letter');left,top,right,bottom=item['bbox']
    b=io.BytesIO();c=canvas.Canvas(b,pagesize=(620,800));c.setFillColorRGB(color,color,color)
    c.rect(left+.2,800-(top+bottom)/2,(right-left)/2,.9,stroke=0,fill=1);c.save();b.seek(0)
    r=PdfReader(o);w=PdfWriter();w.append(r);w.pages[0].merge_page(PdfReader(b).pages[0])
    with o.open('wb') as out:w.write(out)
    with pytest.raises(Problem):verify_pdf(t,o,l,a,f,p,expected_name=o.name,preview_dir=tmp_path/'difference')
    assert list((tmp_path/'difference').glob('*difference*.png'))


def test_verifier_never_calls_production_plan_or_export(rendered,monkeypatch):
    t,o,l,a,f,p,items=rendered
    def forbidden(*args,**kwargs):raise AssertionError('independent verifier called producer')
    monkeypatch.setattr('vocabatron.pdf.plan',forbidden);monkeypatch.setattr('vocabatron.pdf.export_pdf',forbidden)
    # Assert the verifier module's dependency graph as well as real worker output.
    import inspect
    import vocabatron.pdf_validation as checker
    source=inspect.getsource(checker)
    assert 'plan(' not in source and 'export_pdf(' not in source
    assert verify_pdf(t,o,l,a,f,p,expected_name=o.name)['status']=='VERIFIED'


def test_wrong_visible_glyph_with_correct_unicode_mapping(rendered):
    from pypdf.generic import DictionaryObject,TextStringObject
    import pdfplumber
    t,o,l,a,f,p,items=rendered
    letter=next(i['text'] for i in items if i['kind']=='letter')
    r=PdfReader(o);w=PdfWriter();w.append(r);page=w.pages[0]
    resources=page['/Resources'];fonts=resources['/Font'];stream=ContentStream(page.get_contents(),w)
    current_font=None;current_size=None;out=[];changed=False
    for operands,op in stream.operations:
        if op==b'Tf':current_font,current_size=operands
        if not changed and op==b'Tj' and str(operands[0])==letter:
            changed=True;original=fonts[current_font].get_object();fault=DictionaryObject(dict(original))
            mapping=original['/ToUnicode'].get_data();replacement=ord('Z')
            import re
            mapping,count=re.subn(rb'(<5A>\s*)<005A>',lambda m:m[1]+f'<{ord(letter):04X}>'.encode(),mapping)
            assert count==1
            unicode=DecodedStreamObject();unicode.set_data(mapping);fault[NameObject('/ToUnicode')]=w._add_object(unicode)
            fonts[NameObject('/FaultFont')]=w._add_object(fault)
            out.extend([([NameObject('/FaultFont'),current_size],b'Tf'),([TextStringObject('Z')],b'Tj'),([current_font,current_size],b'Tf')])
        else:out.append((operands,op))
    assert changed;stream.operations=out;page[NameObject('/Contents')]=w._add_object(stream)
    with o.open('wb') as dest:w.write(dest)
    with pdfplumber.open(o) as actual:
        assert letter in [c['text'] for c in actual.pages[0].chars]
    with pytest.raises(Problem):verify_pdf(t,o,l,a,f,p,expected_name=o.name)


def test_transparent_text_and_gridline_cover_rejected(rendered,tmp_path):
    from pypdf.generic import DictionaryObject
    t,o,l,a,f,p,items=rendered
    letter=next(i['text'] for i in items if i['kind']=='letter')
    r=PdfReader(o);w=PdfWriter();w.append(r);page=w.pages[0];resources=page['/Resources']
    resources[NameObject('/ExtGState')]=DictionaryObject({NameObject('/Hidden'):DictionaryObject({NameObject('/ca'):FloatObject(0),NameObject('/CA'):FloatObject(0)})})
    stream=ContentStream(page.get_contents(),w);out=[];changed=False
    for operands,op in stream.operations:
        if not changed and op==b'Tj' and str(operands[0])==letter:
            out.extend([([],b'q'),([NameObject('/Hidden')],b'gs'),(operands,op),([],b'Q')]);changed=True
        else:out.append((operands,op))
    stream.operations=out;page[NameObject('/Contents')]=w._add_object(stream)
    with o.open('wb') as dest:w.write(dest)
    with pytest.raises(Problem):verify_pdf(t,o,l,a,f,p,expected_name=o.name)
