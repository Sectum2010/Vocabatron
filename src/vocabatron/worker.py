"""Closed dispatch table for supervised document operations; never a shell or evaluator."""
import json
import os
import math
from pathlib import Path
import resource


def preflight(path,limits,page_numbers=None):
    from pypdf import PdfReader
    from .domain import Problem
    reader=PdfReader(path)
    if reader.is_encrypted:raise Problem('ENCRYPTED_PDF','加密 PDF')
    if len(reader.pages)>limits['pages']:raise Problem('RESOURCE_LIMIT','PDF 页数超限')
    if sum(len(v) for v in reader.xref.values())>limits['objects']:raise Problem('RESOURCE_LIMIT','PDF 对象数量超限')
    characters=0
    if page_numbers is not None and (not isinstance(page_numbers,list) or len(page_numbers)>8 or any(type(p)!=int or not 1<=p<=len(reader.pages) for p in page_numbers)):
        raise Problem('INPUT_INVALID','Invalid page group')
    pages=reader.pages if page_numbers is None else [reader.pages[p-1] for p in page_numbers]
    for page in pages:
        unit=float(page.get('/UserUnit',1))
        if not math.isfinite(unit) or unit<=0:raise Problem('RESOURCE_LIMIT','PDF 页面单位不合法')
        for box in (page.mediabox,page.cropbox):
            w,h=float(box.width)*unit,float(box.height)*unit
            if not all(math.isfinite(x) for x in (w,h)) or min(w,h)<=0 or max(w,h)>limits['page_points'] or w*h*4>limits['render_pixels']:
                raise Problem('RESOURCE_LIMIT','PDF 页面或渲染像素超限')
        characters+=len(page.extract_text() or '')
        if characters>limits['characters']:raise Problem('RESOURCE_LIMIT','PDF 全文字符数量超限')
    return reader


def dispatch(op,payload,limits):
    from .domain import FrozenClues,Lesson,Layout,Problem
    for key in ('source','template','output'):
        if key in payload and Path(payload[key]).is_file():preflight(payload[key],limits,payload.get('page_numbers') if op in ('book_pages','ocr_page','structured_page') else None)
    if op in ('ocr_page','structured_page'):
        from .ocr import TesseractBackend
        from .vocabulary import events
        if op=='structured_page':
            from .structured_ocr import DoclingBackend
            page=DoclingBackend().extract(Path(payload['source']),payload['page_numbers'][0],limits)
        else:page=TesseractBackend().extract(Path(payload['source']),payload['page_numbers'][0],limits,variant=payload.get('variant',0))
        page['events']=events(page)
        return page
    if op=='book_pages':
        from .app.book import extract_pages
        return extract_pages(Path(payload['source']),payload['page_numbers'],limits=limits,use_tables=payload.get('use_tables',True))
    if op=='extract':
        from .ingest import extract_local
        return extract_local(Path(payload['source']),payload['lesson'],limits=limits)
    if op=='calibrate':
        from .pdf import _calibrate
        return _calibrate(Path(payload['template']))
    if op=='inspect':
        from .pdf import _inspect_pdf
        reader,report=_inspect_pdf(Path(payload['source']))
        return {'pages':len(reader.pages),'security':report}
    if op in ('export','verify'):
        lesson=Lesson.model_validate(payload['lesson']);layout=Layout.model_validate(payload['layout'])
        frozen=FrozenClues.model_validate(payload['frozen']);profile=payload['profile']
        if op=='export':
            from .pdf import _export_pdf
            items,rows=_export_pdf(Path(payload['template']),Path(payload['output']),lesson,layout,frozen,profile)
            return {'items':items,'rows':rows}
        from .pdf_validation import verify_local
        return verify_local(Path(payload['template']),Path(payload['output']),lesson,layout,frozen,profile,
            expected_name=payload['expected_name'],preview_dir=payload.get('preview_dir'))
    if op=='ollama':
        from .clues import Ollama
        return Ollama()._request_local(payload['endpoint'],payload.get('payload'),payload.get('timeout',240))
    raise Problem('WORKER_FAILED','未知子进程操作')


def main(job,limits):
    from .domain import Problem
    try:
        request=json.loads((job/'request.json').read_bytes())
        result=dispatch(request['operation'],request['payload'],limits)
        response={'ok':True,'result':result}
    except (MemoryError,OverflowError):response={'ok':False,'code':'RESOURCE_LIMIT'}
    except Problem as exc:response={'ok':False,'code':exc.code,'error_details':exc.details}
    except Exception as exc:
        import traceback
        response={'ok':False,'code':'WORKER_FAILED','error_details':{'type':type(exc).__name__,'trace':traceback.format_exc(limit=12)}}
    response['metrics']={'peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        'network_isolation':os.environ.get('VOCABATRON_DOCUMENT_NETWORK','local-model-http-only'),
        'rss_scope':'single document or HTTP worker process, Linux ru_maxrss KiB','configured_native_threads':1,'observed_threads':len(list(Path('/proc/self/task').iterdir()))}
    encoded=json.dumps(response,ensure_ascii=False,allow_nan=False).encode()
    if len(encoded)>limits['ipc_bytes']:encoded=b'{"ok":false,"code":"RESOURCE_LIMIT"}'
    fd=os.open(job/'response.json',os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'wb') as f:f.write(encoded)
    return 0
