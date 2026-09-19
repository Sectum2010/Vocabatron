"""Ordered spatial evidence; whitespace alone may differ, never character order."""
from __future__ import annotations
import re
import xml.etree.ElementTree as ET
from .domain import Problem


def canonical(text):
    return re.sub(r'\s+','',text)


def spatial_lines(chars, tolerance=2.5):
    lines=[]
    for ch in sorted(chars,key=lambda c:((c['top']+c['bottom'])/2,c['x0'])):
        mid=(ch['top']+ch['bottom'])/2
        found=next((line for line in reversed(lines) if abs(mid-line[0])<=tolerance),None)
        if found is None:lines.append([mid,[ch]])
        else:found[1].append(ch)
    return [''.join(c['text'] for c in sorted(line,key=lambda c:c['x0'])) for _,line in lines]


def poppler_pages(xml):
    root=ET.fromstring(xml)
    pages=[]
    for page in root.iter():
        if page.tag.rsplit('}',1)[-1]!='page':continue
        words=[]
        for word in page.iter():
            if word.tag.rsplit('}',1)[-1]!='word':continue
            words.append({'text':word.text or '', 'x0':float(word.attrib['xMin']),
                'x1':float(word.attrib['xMax']),'top':float(word.attrib['yMin']),'bottom':float(word.attrib['yMax'])})
        pages.append(words)
    return pages


def ordered_evidence(fragment, chars, other_words):
    selected=[chars[i] for i in fragment.char_indices]
    source='\n'.join(spatial_lines(selected))
    x0,y0,x1,y1=fragment.bbox
    second=[w for w in other_words if x0<=(w['x0']+w['x1'])/2<x1 and y0<=(w['top']+w['bottom'])/2<y1]
    other='\n'.join(spatial_lines(second))
    values=[canonical(x) for x in (source,fragment.raw,other)]
    return {'bbox':fragment.bbox,'source_order_sha256':__import__('hashlib').sha256(values[0].encode()).hexdigest(),
            'table_order_match':values[0]==values[1], 'poppler_order_match':values[0]==values[2],
            'source_lines':spatial_lines(selected),'poppler_lines':spatial_lines(second)}


def same_content(saved, current):
    # Coordinates/ownership, field membership and candidate sequence are part of the assertion.
    def normalize(value):
        if isinstance(value,dict):
            return {k:normalize(v) for k,v in value.items() if k not in {'coverage_sha256','schema_version'}}
        if isinstance(value,list):return [normalize(v) for v in value]
        if isinstance(value,str):return re.sub(r'\s+',' ',value).strip()
        return value
    if normalize(saved.model_dump(mode='json'))!=normalize(current.model_dump(mode='json')):
        raise Problem('TRANSCRIPTION_REQUIRES_DECISION','有序转录或字段归属与保存内容不一致')
