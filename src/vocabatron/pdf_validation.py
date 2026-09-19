"""Independent semantic and visible-ink oracle.

Trust boundary: original template, validated placements, frozen IDs, and the bundled
font/style. Neither the export plan nor the exporter is called here. A small
reference overlay is rendered by each renderer separately; pixels from different
renderers are never compared to one another.
"""
from collections import Counter
import io
import math
from pathlib import Path
import subprocess

import numpy as np
import pdfplumber
import pypdfium2 as pdfium
from PIL import Image
from pypdf import PdfReader,PdfWriter,Transformation
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

from .domain import Problem, selected_candidates
from .pdf import FONT,font,_inspect_pdf,_calibrate
from .storage import sha256
from .validation import validate_layout

VALIDATOR='visible-reference-v2'


def reference_items(lesson,layout,frozen,profile):
    checked=validate_layout(lesson,layout);choices=selected_candidates(lesson,frozen);font()
    items=[]
    def add(page,text,x,y,size,kind):
        if any(ord(c) not in font().face.charToGlyph for c in text):raise Problem('MISSING_GLYPH','字体不覆盖原文字符')
        asc,desc=pdfmetrics.getAscentDescent(FONT,size)
        items.append({'page':page,'text':text,'x':x,'top':y,'size':size,'kind':kind,
                      'bbox':[x,y,x+pdfmetrics.stringWidth(text,FONT,size),y+asc-desc]})
    for (row,col),cell in sorted(checked['cells'].items()):
        left,top,right,bottom=profile['grid'][row][col];height=bottom-top;width=right-left
        size=min(13,height*.45,width*.46)
        add(0,cell['letter'],(left+right-pdfmetrics.stringWidth(cell['letter'],FONT,size))/2,
            top+(height-size)/2+height*.045,size,'letter')
    for (r,c),number in checked['starts'].items():
        box=profile['grid'][r][c];add(0,str(number),box[0]+1.6,box[1]+1.3,min(6.2,(box[3]-box[1])*.24),'number')
    l,t,r,b=profile['lesson_blank'];size=min(11,b-t)
    add(0,str(lesson.lesson),(l+r-pdfmetrics.stringWidth(str(lesson.lesson),FONT,size))/2,t-1,size,'lesson')
    word_map={w.word_id:w for w in lesson.words}
    count=0
    for direction,box in zip(('across','down'),profile['columns']):
        entries=[]
        for p in sorted(layout.placements,key=lambda p:checked['starts'][p.row,p.col]):
            if p.direction!=direction:continue
            c=choices[p.word_id];n=checked['starts'][p.row,p.col]
            entries.append(f"{n}. ({'syn.' if c.relation=='SYNONYM' else 'ant.'}) {c.text}")
        count+=len(entries)
        width=box[2]-box[0]-24
        selected=None
        for size in (12.0,11.5,11.0):
            wrapped=[];possible=True
            for entry in entries:
                lines=[];line=''
                for token in entry.split(' '):
                    if pdfmetrics.stringWidth(token,FONT,size)>width:possible=False;break
                    proposed=(line+' '+token) if line else token
                    if line and pdfmetrics.stringWidth(proposed,FONT,size)>width:lines.append(line);line=token
                    else:line=proposed
                if line:lines.append(line)
                wrapped.append(lines)
            if possible and sum(len(lines)*size*1.35+5 for lines in wrapped)<=box[3]-box[1]-24:
                selected=wrapped;break
        if selected is None:raise Problem('CLUE_OVERFLOW','独立验证确认线索无法容纳')
        y=box[1]+12
        for lines in selected:
            for line in lines:add(1,line,box[0]+12,y,size,'clue');y+=size*1.35
            y+=5
    return items,count


def reference_pdf(template,items,profile,path):
    original=PdfReader(template);out=PdfWriter()
    for index,meta in enumerate(profile['pages']):
        buf=io.BytesIO();drawing=canvas.Canvas(buf,pagesize=(meta['width'],meta['height']),invariant=1)
        for item in items:
            if item['page']!=index:continue
            asc,_=pdfmetrics.getAscentDescent(FONT,item['size'])
            drawing.setFont(FONT,item['size']);drawing.drawString(item['x'],meta['height']-item['top']-asc,item['text'])
        drawing.showPage();drawing.save();buf.seek(0)
        page=out.add_page(original.pages[index]);x0,y0,x1,y1=meta['mediabox']
        transforms={0:(1,0,0,1,x0,y0),90:(0,1,-1,0,x1,y0),180:(-1,0,0,-1,x1,y1),270:(0,-1,1,0,x0,y1)}
        page.merge_transformed_page(PdfReader(buf).pages[0],Transformation(transforms[meta['rotation']%360]),expand=False)
    with path.open('xb') as stream:out.write(stream)


def key(ch):return ch['text'],*(round(ch[k],3) for k in ('x0','top','x1','bottom'))


def render(path,engine):
    if engine=='PDFium':
        with pdfium.PdfDocument(str(path)) as pdf:
            return [p.render(scale=2).to_pil().convert('RGB') for p in list(pdf)[:2]]
    prefix=path.parent/(path.stem+'-poppler')
    # Output files inherit the launcher's restrictive umask and file quota.
    subprocess.run(['/usr/bin/pdftoppm','-f','1','-l','2','-r','144','-png',str(path),str(prefix)],
                   check=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=60)
    return [Image.open(prefix.parent/(prefix.name+f'-{i}.png')).convert('RGB') for i in (1,2)]


def verify_local(template,output,lesson,layout,frozen,profile,*,expected_name,preview_dir=None):
    if output.name!=expected_name:raise Problem('PDF_FILENAME','最终文件名不符')
    actual_profile=_calibrate(template)
    # Normalize tuples from direct callers and lists after JSON transport.
    import json
    if json.loads(json.dumps(profile))!=json.loads(json.dumps(actual_profile)):raise Problem('TEMPLATE_CHANGED','模板坐标快照与原件不符')
    reader,security=_inspect_pdf(output)
    if len(reader.pages)!=2:raise Problem('PDF_PAGE_COUNT','结果必须恰好两页')
    if security['active_or_attached_keys']:raise Problem('PDF_ACTIVE_CONTENT','结果含主动内容')
    expected,count=reference_items(lesson,layout,frozen,actual_profile)
    reports=[]
    with pdfplumber.open(template) as original,pdfplumber.open(output) as final:
        for index,(base,page) in enumerate(zip(original.pages[:2],final.pages)):
            meta=profile['pages'][index];p=reader.pages[index]
            if list(map(float,p.mediabox))!=meta['mediabox'] or list(map(float,p.cropbox))!=meta['cropbox'] or p.rotation!=meta['rotation']:
                raise Problem('PDF_PAGE_GEOMETRY','最终页面几何变化')
            remaining=Counter(key(c) for c in base.chars);added=[]
            for ch in page.chars:
                if remaining[key(ch)]:remaining[key(ch)]-=1
                else:added.append(ch)
            if any(remaining.values()):raise Problem('TEMPLATE_TEXT_CHANGED','原模板字符变化')
            used=set()
            for item in [e for e in expected if e['page']==index]:
                x0,y0,x1,y1=item['bbox']
                indices=[i for i,ch in enumerate(added) if x0-.15<=(ch['x0']+ch['x1'])/2<=x1+.15 and y0-.15<=(ch['top']+ch['bottom'])/2<=y1+.15]
                chars=sorted((added[i] for i in indices),key=lambda ch:ch['x0'])
                if ''.join(ch['text'] for ch in chars)!=item['text']:raise Problem('PDF_TEXT_MISMATCH','新增文字顺序或内容不符')
                for i in indices:
                    if i in used:raise Problem('PDF_DUPLICATE_TEXT','字符重复')
                    used.add(i);ch=added[i]
                    if ch['x0']<x0-.15 or ch['x1']>x1+.15 or ch['top']<y0-.15 or ch['bottom']>y1+.15:
                        raise Problem('PDF_TEXT_POSITION','新增文字边界不符')
            if used!=set(range(len(added))):raise Problem('PDF_UNEXPECTED_TEXT','存在额外文字')
            reports.append({'page':index+1,'original_characters':len(base.chars),'added_characters':len(added),'text_status':'VERIFIED','renderers':{}})
    reference=output.parent/'reference.pdf';reference_pdf(template,expected,profile,reference)
    failures=[]
    for engine in ('PDFium','Poppler'):
        actual=render(output,engine);wanted=render(reference,engine);original=render(template,engine)
        for index,(a,b,base) in enumerate(zip(actual,wanted,original)):
            aa=np.asarray(a);bb=np.asarray(b);ss=np.asarray(base)
            if aa.shape!=bb.shape or aa.shape!=ss.shape:raise Problem('PDF_RENDER_GEOMETRY','渲染尺寸变化')
            difference=np.any(aa!=bb,axis=2)
            mask=np.zeros(difference.shape,dtype=bool)
            for item in (x for x in expected if x['page']==index):
                x0,y0,x1,y1=item['bbox']
                # Trusted font advance rectangle with a fixed 1pt overhang allowance.
                mask[max(0,math.floor(y0*2)-2):math.ceil(y1*2)+2,max(0,math.floor(x0*2)-2):math.ceil(x1*2)+2]=True
            outside=int(np.count_nonzero(np.any(bb!=ss,axis=2)&~mask))
            if outside:raise Problem('REFERENCE_TEMPLATE_OVERLAP','参考叠印超出预期几何范围')
            pixels=int(difference.sum())
            if pixels:
                diag=Path(preview_dir) if preview_dir else output.parent/'differences';diag.mkdir(mode=0o700,exist_ok=True,parents=True)
                Image.fromarray(np.where(difference[:,:,None],np.array([255,0,0],dtype=np.uint8),aa)).save(diag/f'{engine}-difference-{index+1}.png')
                ys,xs=np.where(difference)
                failures.append({'renderer':engine,'page':index+1,'pixels':pixels,'bbox':[int(xs.min()),int(ys.min()),int(xs.max()),int(ys.max())]})
            reports[index]['renderers'][engine]={'status':'VERIFIED','reference_difference_pixels':pixels,'outside_expected_geometry':outside,'scale':2}
            reports[index].update({'render_status':'VERIFIED','outside_character_masks':0,'mask_fraction':float(mask.mean())})
            if preview_dir and engine=='PDFium':
                Path(preview_dir).mkdir(mode=0o700,parents=True,exist_ok=True);a.save(Path(preview_dir)/f'page-{index+1}.png')
    if failures:raise Problem('PDF_GLYPH_MISMATCH','最终可见笔画像素与独立参考不符',details=failures)
    return {'status':'VERIFIED','validator':VALIDATOR,'filename':output.name,'sha256':sha256(output),'pages':reports,
            'clue_count':count,'manual_review':'NOT_PERFORMED','security':security}
