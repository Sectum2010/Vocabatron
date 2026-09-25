"""Whole-document boundaries and independent lesson failure, invented sources."""
from pathlib import Path
from pypdf import PdfReader,PdfWriter,Transformation
from reportlab.pdfgen import canvas
from vocabatron.app.book import assemble
from vocabatron.app.identity import identities
from vocabatron.domain import Lesson
from vocabatron.supervisor import run_job
from .test_web_app import library
from .helpers import synthetic_course


def extract(path):
    info=run_job('inspect',{'source':str(path)})
    return run_job('book_pages',{'source':str(path),'page_numbers':list(range(1,info['pages']+1))})


def test_two_lessons_same_page_and_one_invalid_course(library):
    root=library.config.runtime_root;root.mkdir(parents=True,exist_ok=True)
    first=root/'first.pdf';second=root/'second.pdf'
    synthetic_course(first,['AB','AC'],3);synthetic_course(second,['DE','DE'],9)
    writer=PdfWriter();page=writer.add_blank_page(width=612,height=792)
    page.merge_transformed_page(PdfReader(first).pages[0],Transformation().scale(.7).translate(0,230))
    page.merge_transformed_page(PdfReader(second).pages[0],Transformation().scale(.7).translate(0,-90))
    combined=root/'same-page.pdf';writer.write(combined)
    result=extract(combined);book=assemble(result['source_sha256'],result['pages'])
    assert [x['lesson']['lesson'] for x in book['lessons']]==[3,9]
    assert [x['coverage']['status'] for x in book['lessons']]==['VERIFIED','NEEDS_ATTENTION']
    assert book['status']=='PARTIALLY_IMPORTED'
    # A damaged source region in the second course cannot invalidate the first.
    p=result['pages'][0];title=next(e for e in p['events'] if e['type']=='title' and e['lesson']==9)
    p['issues'].append({'code':'TRANSCRIPTION_REQUIRES_DECISION','page':1,'bbox':[0,title['top']+20,400,title['top']+30]})
    assert assemble(result['source_sha256'],result['pages'])['lessons'][0]['coverage']['status']=='VERIFIED'


def test_continuation_missing_page_and_nontext_content(library):
    root=library.config.runtime_root;root.mkdir(parents=True,exist_ok=True)
    source=root/'continued.pdf';synthetic_course(source,['AB','AC','AD','AE','AF','AG','AH','AI','AJ'],3)
    result=extract(source)
    # A continuation does not depend on the presence of a repeated heading.
    for e in result['pages'][1]['events']:
        if e['type']=='title':e['type']='metadata'
    book=assemble(result['source_sha256'],result['pages']);assert book['status']=='READY'
    assert len(book['lessons'][0]['lesson']['words'])==9
    missing=assemble(result['source_sha256'],result['pages'][:1],total_pages=2)
    assert missing['status']=='PARTIALLY_IMPORTED'
    assert missing['unresolved_sections'][0]['code']=='SOURCE_PAGE_SEQUENCE'
    image=root/'no-text.pdf';c=canvas.Canvas(str(image));c.rect(40,40,200,200,fill=1);c.showPage();c.save()
    blank=extract(image)
    assert blank['pages'][0]['issues'][0]['code']=='NO_TEXT_LAYER'
    assert assemble(blank['source_sha256'],blank['pages'])['status']=='PARTIALLY_IMPORTED'


def test_multiline_heading_with_positioned_spaces_retains_all_source(library):
    root=library.config.runtime_root;root.mkdir(parents=True,exist_ok=True)
    source=root/'positioned-heading.pdf';synthetic_course(source,['AB','AC'],positioned_heading=True)
    result=extract(source);book=assemble(result['source_sha256'],result['pages'])
    assert book['status']=='READY' and len(book['lessons'])==1
    title='\n'.join(f['raw'] for f in book['lessons'][0]['lesson']['title'])
    assert 'Invented classroom' in title and 'An invented subtitle' in title and 'Lesson 3' in title
    assert all(x['table_order_match'] and x['poppler_order_match'] for p in result['pages'] for x in p['audit'])
