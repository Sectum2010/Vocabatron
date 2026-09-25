"""Bounded local OCR over controlled PDFium images; no download code path."""
from __future__ import annotations
import csv
import io
import json
import hashlib
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from collections import Counter

from .documents import DOCUMENT_VERSION, positioned_blocks, compact
from .domain import Problem
from .paths import private_path,roots


def model_root():
    return private_path(os.environ.get('VOCABATRON_OCR_ROOT',roots()[1]/'ocr'))


def tesseract_runtime():
    root=model_root()/'tesseract'
    binary=root/'usr/bin/tesseract';data=root/'usr/share/tesseract-ocr/5/tessdata'
    if not binary.is_file() or not all((data/(lang+'.traineddata')).is_file() for lang in ('eng','osd')):
        raise Problem('OCR_NOT_PREPARED','Local OCR files are missing. Run the documented offline setup before importing scans.')
    manifest=model_root()/'tesseract-manifest.json'
    if not manifest.is_file():raise Problem('OCR_NOT_PREPARED','Local OCR integrity manifest is missing')
    records=json.loads(manifest.read_text()).get('weights',[])
    if {r.get('name') for r in records}!={'eng.traineddata','osd.traineddata'}:
        raise Problem('OCR_NOT_PREPARED','Local OCR language manifest is incomplete')
    for record in records:
        path=data/record['name']
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=record['sha256']:
            raise Problem('OCR_MODEL_CHANGED','A local OCR model failed its integrity check')
    env=dict(os.environ,LD_LIBRARY_PATH=str(root/'usr/lib/aarch64-linux-gnu'),TESSDATA_PREFIX=str(data),
             OMP_THREAD_LIMIT='1',OMP_NUM_THREADS='1')
    return str(binary),env


def render_page(source,number,dpi,limits):
    import pypdfium2 as pdfium
    if dpi not in (200,300,360):raise Problem('RESOURCE_LIMIT','Unsupported OCR rendering resolution')
    with pdfium.PdfDocument(str(source)) as pdf:
        page=pdf[number-1];width,height=page.get_size();scale=dpi/72
        if width*height*scale*scale>limits['render_pixels']:raise Problem('RESOURCE_LIMIT','OCR pixel budget exceeded')
        bitmap=page.render(scale=scale);image=bitmap.to_pil().copy();bitmap.close();page.close()
    return image,width,height


def deskew(image):
    import numpy as np
    from PIL import ImageOps
    small=ImageOps.grayscale(image);small.thumbnail((1000,1000))
    scores=[]
    for half in range(-6,7):
        angle=half/2
        values=np.asarray(small.rotate(angle,expand=False,fillcolor=255))<160
        scores.append((float(np.var(values.sum(axis=1))),angle))
    score,angle=max(scores);baseline=next(score for score,a in scores if a==0)
    if score<baseline*1.02:angle=0
    return (image.rotate(angle,expand=False,fillcolor='white') if angle else image),angle


def prepare_orientation(image,width,height,folder):
    """Keep the explicit inverse transform with the controlled raster."""
    source_size=[width,height];rotation=0
    binary,env=tesseract_runtime();path=folder/'orientation.png';image.save(path)
    with (folder/'orientation.txt').open('wb') as out:
        subprocess.run([binary,str(path),'stdout','--psm','0','-l','osd'],env=env,stdin=subprocess.DEVNULL,
            stdout=out,stderr=subprocess.DEVNULL,timeout=20,check=False)
    match=re.search(r'Rotate:\s*(90|180|270)',(folder/'orientation.txt').read_text())
    if match:
        rotation=int(match[1]);image=image.rotate(-rotation,expand=True)
        if rotation in (90,270):width,height=height,width
    image,skew=deskew(image)
    return image,width,height,{'rotation_degrees':rotation,'deskew_degrees':skew,'source_page_size':source_size,
        'upright_page_size':[width,height],'coordinates':'upright page points; source_polygon and source_bbox are original page points'}


def transform_point(point,transform,*,inverse=False):
    sw,sh=transform['source_page_size'];uw,uh=transform['upright_page_size']
    r=math.radians(transform['rotation_degrees']);a=math.radians(transform['deskew_degrees'])
    x,y=point
    if inverse:
        x,y=x-uw/2,y-uh/2
        x,y=math.cos(a)*x-math.sin(a)*y,math.sin(a)*x+math.cos(a)*y
        return [math.cos(r)*x+math.sin(r)*y+sw/2,-math.sin(r)*x+math.cos(r)*y+sh/2]
    x,y=x-sw/2,y-sh/2
    x,y=math.cos(r)*x-math.sin(r)*y,math.sin(r)*x+math.cos(r)*y
    return [math.cos(a)*x+math.sin(a)*y+uw/2,-math.sin(a)*x+math.cos(a)*y+uh/2]


def transformed_box(box,transform,*,inverse=False):
    x0,y0,x1,y1=box
    polygon=[transform_point(p,transform,inverse=inverse) for p in ((x0,y0),(x1,y0),(x1,y1),(x0,y1))]
    return [min(p[0] for p in polygon),min(p[1] for p in polygon),max(p[0] for p in polygon),max(p[1] for p in polygon)],polygon


def attach_source_coordinates(tokens,transform):
    for token in tokens:
        token['source_bbox'],token['source_polygon']=transformed_box(token['bbox'],transform,inverse=True)


class TesseractBackend:
    name='tesseract-5-tsv'

    def recognize(self,image,number,folder,limits,*,psm=6,view='standard',width=None,height=None):
        binary,env=tesseract_runtime();image_path=folder/f'{view}.png';out=folder/view
        image.save(image_path)
        with (folder/f'{view}.log').open('wb') as log:
            subprocess.run([binary,str(image_path),str(out),'-l','eng','--psm',str(psm),'tsv'],
                env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,timeout=min(45,limits['wall_seconds']),check=True)
        path=out.with_suffix('.tsv')
        if path.stat().st_size>limits['ipc_bytes']:raise Problem('RESOURCE_LIMIT','OCR token output exceeds its bound')
        sx=(width or image.width)/image.width;sy=(height or image.height)/image.height;tokens=[]
        for row in csv.DictReader(io.StringIO(path.read_text()),delimiter='\t',quoting=csv.QUOTE_NONE):
            if row['level']!='5' or not row['text'].strip():continue
            x,y,w,h=(int(row[k]) for k in ('left','top','width','height'))
            tokens.append({'id':f'p{number}t{len(tokens)}','page':number,'text':row['text'],
                'bbox':[x*sx,y*sy,(x+w)*sx,(y+h)*sy],'confidence':float(row['conf'])/100,
                'backend':self.name,'char_indices':[],'view':view})
        if sum(len(t['text']) for t in tokens)>limits['characters']:raise Problem('RESOURCE_LIMIT','OCR character limit exceeded')
        return tokens

    def extract(self,source,number,limits,*,variant=0):
        from PIL import ImageOps
        import tempfile
        started=time.monotonic();dpi=300 if variant<2 else 360
        image,width,height=render_page(source,number,dpi,limits)
        with tempfile.TemporaryDirectory(prefix='ocr-',dir=source.parent) as tmp:
            folder=Path(tmp)
            transform={'rotation_degrees':0,'deskew_degrees':0,'source_page_size':[width,height],
                'upright_page_size':[width,height],'coordinates':'upright page points; source_bbox is original page points'}
            if variant:
                image,width,height,transform=prepare_orientation(image,width,height,folder)
            if variant>=2:image=ImageOps.autocontrast(image.convert('L')).point(lambda p:255 if p>160 else 0)
            # Two fresh extraction passes with distinct segmentation and pixel
            # preprocessing. Disagreements stay attached to observed tokens.
            first=self.recognize(image,number,folder,limits,psm=6,view='standard',width=width,height=height)
            second=self.recognize(ImageOps.grayscale(image),number,folder,limits,psm=3,view='independent-layout',width=width,height=height)
            third=self.recognize(image,number,folder,limits,psm=11,view='sparse-layout',width=width,height=height) if variant>=2 else []
        attach_source_coordinates(first+second+third,transform)
        for token in first:
            x0,y0,x1,y1=token['bbox'];cx=(x0+x1)/2;cy=(y0+y1)/2
            nearby=[s for s in second+third if abs((s['bbox'][1]+s['bbox'][3])/2-cy)<max(5,y1-y0) and
                    max(x0,s['bbox'][0])<min(x1,s['bbox'][2])]
            token['independent_observations']=[{k:s[k] for k in ('text','bbox','confidence','view')} for s in [token,*nearby]]
            votes={}
            for observation in token['independent_observations']:
                if observation['confidence']>=.65:votes.setdefault(observation['text'],set()).add(observation['view'])
            agreed=[text for text,views in votes.items() if len(views)>=2]
            token['consensus']=len(agreed)==1
            if token['consensus'] and agreed[0]!=token['text']:
                token['primary_text']=token['text'];token['text']=agreed[0]
        return {'schema_version':DOCUMENT_VERSION,'page':number,'width':width,'height':height,'backend':self.name,
            'tokens':first,'blocks':positioned_blocks(first),'chars':[],'issues':[],'audit':[],
            'table_hints':[],'character_count':sum(len(t['text']) for t in first),'ocr_used':True,'blank':not first,
            'ocr_evidence':{'variant':variant,'dpi':dpi,**transform,
                'seconds':time.monotonic()-started,
                'secondary_tokens':second,'third_tokens':third,'network_isolation':os.environ.get('VOCABATRON_DOCUMENT_NETWORK')}}
