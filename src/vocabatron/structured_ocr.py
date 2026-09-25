"""Docling layout + local RapidOCR, activated only after cheaper recovery."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from .documents import DOCUMENT_VERSION,positioned_blocks,compact,raw_text
from .domain import Problem
from .ocr import model_root,render_page,TesseractBackend,prepare_orientation,attach_source_coordinates


class DoclingBackend:
    name='docling-heron-rapidocr-v4-cpu'

    def extract(self,source,number,limits):
        root=model_root();manifest=root/'docling-manifest.json';started=time.monotonic()
        if not manifest.is_file():raise Problem('OCR_NOT_PREPARED','Structured OCR has not been prepared locally')
        records=json.loads(manifest.read_text())['records']
        for record in records:
            path=Path(record['path'])
            if path.resolve()!=path or not path.is_relative_to(root) or not path.is_file():
                raise Problem('OCR_NOT_PREPARED','A required local structured OCR model is missing')
            if hashlib.sha256(path.read_bytes()).hexdigest()!=record['sha256']:
                raise Problem('OCR_MODEL_CHANGED','A local OCR model failed its integrity check')
        cache=source.parent/'model-cache';cache.mkdir(exist_ok=True)
        os.environ.update(HF_HOME=str(cache),XDG_CACHE_HOME=str(cache),TORCH_HOME=str(cache),
            HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',DO_NOT_TRACK='1')
        import torch
        from PIL import ImageOps
        torch.set_num_threads(1);torch.set_num_interop_threads(1)
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.accelerator_options import AcceleratorOptions,AcceleratorDevice
        from docling.datamodel.pipeline_options import PdfPipelineOptions,RapidOcrOptions
        from docling.document_converter import DocumentConverter,ImageFormatOption
        paths={r['role']:r['path'] for r in records if 'role' in r}
        options=PdfPipelineOptions(artifacts_path=root/'docling',do_table_structure=False,generate_parsed_pages=True,
            do_picture_classification=False,do_picture_description=False,do_code_enrichment=False,
            do_formula_enrichment=False,enable_remote_services=False,allow_external_plugins=False,
            accelerator_options=AcceleratorOptions(num_threads=1,device=AcceleratorDevice.CPU),
            ocr_options=RapidOcrOptions(lang=['en'],backend='onnxruntime',force_full_page_ocr=True,
                det_model_path=paths['det'],cls_model_path=paths['cls'],rec_model_path=paths['rec'],
                scale=1,rapidocr_params={'EngineConfig.onnxruntime.inter_op_num_threads':1,
                    'EngineConfig.onnxruntime.intra_op_num_threads':1}))
        options.layout_options.model_spec.revision='8f39ad3c0b4c58e9c2d2c84a38465abf757272d8'
        image,width,height=render_page(source,number,300,limits)
        with tempfile.TemporaryDirectory(prefix='structured-',dir=source.parent) as tmp:
            folder=Path(tmp);path=folder/'page.png'
            image,width,height,transform=prepare_orientation(image,width,height,folder)
            image.save(path)
            converter=DocumentConverter(allowed_formats=[InputFormat.IMAGE],format_options={InputFormat.IMAGE:ImageFormatOption(pipeline_options=options)})
            result=converter.convert(path,max_num_pages=1,max_file_size=limits['file_bytes'])
            if not result.pages:raise Problem('OCR_EXTRACTION_FAILED','Structured OCR did not return a page')
            page=result.pages[0];sx=width/page.size.width;sy=height/page.size.height;tokens=[]
            for cell in page.cells:
                box=cell.rect.to_bounding_box();b=[box.l*sx,box.t*sy,box.r*sx,box.b*sy]
                tokens.append({'id':f'p{number}t{len(tokens)}','page':number,'text':cell.text,'bbox':b,
                    'confidence':cell.confidence,'backend':self.name,'char_indices':[]})
            secondary=TesseractBackend().recognize(image,number,folder,limits,psm=6,view='tesseract-confirmation',width=width,height=height)
            alternate=TesseractBackend().recognize(ImageOps.grayscale(image),number,folder,limits,psm=3,
                view='tesseract-alternate',width=width,height=height)
            attach_source_coordinates(tokens+secondary+alternate,transform)
            for token in tokens:
                x0,y0,x1,y1=token['bbox']
                def within(view):
                    return [s for s in view if x0-2<=(s['bbox'][0]+s['bbox'][2])/2<=x1+2 and
                        y0-2<=(s['bbox'][1]+s['bbox'][3])/2<=y1+2]
                observed=within(secondary);other=within(alternate)
                token['independent_observations']=observed
                token['alternate_observations']=other
                token['consensus']=bool(observed) and compact(token['text'])==compact(raw_text(observed)) and token['confidence']>=.7 and all(s['confidence']>=.65 for s in observed)
                if not token['consensus'] and observed and other and compact(raw_text(observed))==compact(raw_text(other)) and all(s['confidence']>=.65 for s in observed+other):
                    # Choose an exact already-observed alternative; retain the
                    # structured engine's original punctuation and spelling.
                    token['primary_text']=token['text'];token['text']=raw_text(observed)
                    token['consensus']=True;token['selection']='two-positioned-tesseract-views'
            layouts=[]
            if page.predictions.layout:
                layouts=[{'label':str(c.label),'bbox':c.bbox.model_dump(mode='json'),'confidence':c.confidence} for c in page.predictions.layout.clusters]
        return {'schema_version':DOCUMENT_VERSION,'page':number,'width':width,'height':height,'backend':self.name,
            'tokens':tokens,'blocks':positioned_blocks(tokens),'chars':[],'issues':[],'audit':[],
            'table_hints':layouts,'character_count':sum(len(t['text']) for t in tokens),'ocr_used':True,'blank':not tokens,
            'ocr_evidence':{'seconds':time.monotonic()-started,'backend':self.name,'secondary_tokens':secondary,**transform,
                'alternate_tokens':alternate,'network_isolation':os.environ.get('VOCABATRON_DOCUMENT_NETWORK')}}
