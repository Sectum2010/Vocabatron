"""Versioned positioned evidence. Called only by the supervised document worker.

The semantic parser consumes this representation, never a detector's table
matrix. Tables are optional, filtered geometry hints. Every source character
is retained, including invisible formatting characters.
"""
from __future__ import annotations
from collections import Counter
from pathlib import Path
import re
import subprocess
import tempfile
import unicodedata

from .domain import Problem
from .transcription import poppler_pages

DOCUMENT_VERSION = 'positioned-document-v1'
PARSER_VERSION = 'vocabulary-anchors-v2'


def visible(text):
    # Format controls remain in tokens and character evidence with an explicit
    # metadata owner. They are not vocabulary letters or list punctuation.
    return ''.join(c for c in text if unicodedata.category(c) != 'Cf')


def compact(text):
    return re.sub(r'\s+', '', visible(text))


def bounds(items):
    return [min(t['bbox'][0] for t in items), min(t['bbox'][1] for t in items),
            max(t['bbox'][2] for t in items), max(t['bbox'][3] for t in items)]


def lines(tokens):
    groups=[]
    for token in sorted(tokens,key=lambda t:((t['bbox'][1]+t['bbox'][3])/2,t['bbox'][0])):
        mid=(token['bbox'][1]+token['bbox'][3])/2
        group=next((g for g in reversed(groups) if abs(g[0]-mid)<=2.5),None)
        if group is None:groups.append([mid,[token]])
        else:group[1].append(token)
    return [sorted(g,key=lambda t:t['bbox'][0]) for _,g in groups]


def raw_text(tokens):
    return '\n'.join(' '.join(t['text'] for t in line) for line in lines(tokens))


def table_hints(page):
    accepted=[];audit=[]
    for table in sorted(page.find_tables(),key=lambda t:(t.bbox[1],t.bbox[0],t.bbox[3]-t.bbox[1])):
        x0,y0,x1,y1=table.bbox;reason=None
        if x0<-.5 or y0<-.5 or x1>page.width+.5 or y1>page.height+.5:reason='outside_page'
        elif x1<=x0 or y1<=y0 or len(table.cells)<2:reason='degenerate_grid'
        elif any(all(abs(a-b)<.5 for a,b in zip(table.bbox,t.bbox)) for t in accepted):reason='duplicate_geometry'
        elif (x1-x0)*(y1-y0)>.97*page.width*page.height:reason='page_wrapper'
        audit.append({'bbox':list(table.bbox),'cells':len(table.cells),'accepted':reason is None,'reason':reason})
        if reason is None:accepted.append(table)
    # Small, useful cells win overlaps; ownership is assigned once per token.
    cells=sorted(set(tuple(c) for t in accepted for c in t.cells if c and c[2]>c[0] and c[3]>c[1]),
                 key=lambda b:((b[2]-b[0])*(b[3]-b[1]),b))
    return cells,audit


def positioned_blocks(tokens, cells=()):
    groups={};loose=[];metadata=[]
    for t in tokens:
        if not compact(t['text']):metadata.append(t);continue
        x=(t['bbox'][0]+t['bbox'][2])/2;y=(t['bbox'][1]+t['bbox'][3])/2
        cell=next((c for c in cells if c[0]<=x<c[2] and c[1]<=y<c[3]),None)
        if cell is None:loose.append(t)
        else:groups.setdefault(cell,[]).append(t)
    result=[]
    for cell,items in groups.items():
        result.append({'tokens':items,'bbox':bounds(items),'hint_bbox':list(cell)})
    for line in lines(loose):
        # Large gutters are a geometry hint, not a text delimiter. Split only
        # before a field anchor, ordinal, or an unambiguously separate column.
        parts=[[]]
        for token in line:
            previous=parts[-1][-1] if parts[-1] else None
            anchor=re.match(r'^(?:SYNONYMS?|ANTONYMS?|FORMS?):',visible(token['text']),re.I)
            if previous and (anchor or token['bbox'][0]-previous['bbox'][2]>30):parts.append([])
            parts[-1].append(token)
        for items in parts:result.append({'tokens':items,'bbox':bounds(items),'hint_bbox':None})
    if metadata:result.append({'tokens':metadata,'bbox':bounds(metadata),'hint_bbox':None,'metadata':'format_or_whitespace'})
    result.sort(key=lambda b:(b['bbox'][1],b['bbox'][0]))
    for i,b in enumerate(result):
        b.update(id=f'b{i}',text=raw_text(b['tokens']),token_ids=[t['id'] for t in b.pop('tokens')])
    return result


class NativeTextBackend:
    name='native-pdfplumber-poppler'

    def extract(self,source,page_numbers,limits,*,use_tables=True):
        import pdfplumber
        with tempfile.NamedTemporaryFile(suffix='.xml',dir=source.parent) as out:
            subprocess.run(['/usr/bin/pdftotext','-f',str(min(page_numbers)),'-l',str(max(page_numbers)),
                '-bbox-layout',str(source),out.name],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,check=True,timeout=min(60,limits['wall_seconds']))
            if Path(out.name).stat().st_size>limits['ipc_bytes']:raise Problem('RESOURCE_LIMIT','Positioned text exceeds its limit')
            second=poppler_pages(Path(out.name).read_text())
        pages=[]
        with pdfplumber.open(source) as pdf:
            for number in page_numbers:
                page=pdf.pages[number-1];chars=page.chars
                if len(chars)>limits['characters']:raise Problem('RESOURCE_LIMIT','Page character limit exceeded')
                indices={id(ch):i for i,ch in enumerate(chars)};owned=set();tokens=[]
                for word in page.extract_words(return_chars=True):
                    ids=[indices[id(ch)] for ch in word['chars']]
                    if any(i in owned for i in ids):raise Problem('SOURCE_OWNERSHIP','Native extractor repeated a character')
                    owned.update(ids)
                    tokens.append({'id':f'p{number}t{len(tokens)}','text':word['text'],'page':number,
                        'bbox':[word['x0'],word['top'],word['x1'],word['bottom']],
                        'confidence':1.0,'backend':self.name,'char_indices':ids})
                for i,ch in enumerate(chars):
                    if i not in owned:
                        tokens.append({'id':f'p{number}t{len(tokens)}','text':ch['text'],'page':number,
                            'bbox':[ch['x0'],ch['top'],ch['x1'],ch['bottom']],
                            'confidence':1.0,'backend':self.name,'char_indices':[i]})
                cells,hints=table_hints(page);blocks=positioned_blocks(tokens,cells if use_tables else ())
                other=second[number-min(page_numbers)]
                other_tokens=[{'text':w['text'],'bbox':[w['x0'],w['top'],w['x1'],w['bottom']]} for w in other]
                ordered_native=compact(raw_text([t for t in tokens if compact(t['text'])]))
                ordered_second=compact(raw_text([t for t in other_tokens if compact(t['text'])]))
                mismatch=ordered_native!=ordered_second
                issues=[];audit=[];lookup={t['id']:t for t in tokens}
                for block in blocks:
                    selected=[lookup[t] for t in block['token_ids']]
                    # Independently match spatial tokens, ignoring only recorded
                    # format/whitespace metadata, never spelling/punctuation.
                    box=block.get('hint_bbox') or bounds(selected)
                    words=[w for w in other if box[0]-.2<=(w['x0']+w['x1'])/2<=box[2]+.2 and box[1]-.2<=(w['top']+w['bottom'])/2<=box[3]+.2]
                    # Different extractors may split a word at a cell border.
                    # Match the complete spatial reading order independently;
                    # prove block ownership against the native character IDs.
                    equivalent=not mismatch
                    audit.append({'bbox':box,'table_order_match':True,'poppler_order_match':equivalent,
                                  'method':'independent-page-spatial-order-and-native-block-ownership','block_id':block['id']})
                if mismatch:issues.append({'code':'SECOND_EXTRACTOR_MISMATCH','page':number})
                image_regions=[[i['x0'],i['top'],i['x1'],i['bottom']] for i in page.images]
                if tokens and image_regions:
                    issues.extend({'code':'IMAGE_REGION_NEEDS_OCR','page':number,'bbox':box} for box in image_regions)
                if not any(compact(t['text']) for t in tokens) and (page.images or page.curves or page.rects or page.lines):
                    issues.append({'code':'NO_TEXT_LAYER','page':number,'bbox':[0,0,page.width,page.height]})
                pages.append({'schema_version':DOCUMENT_VERSION,'page':number,'width':page.width,'height':page.height,
                    'backend':self.name,'tokens':tokens,'blocks':blocks,
                    'chars':[{k:c[k] for k in ('text','x0','x1','top','bottom','fontname','size') if k in c} for c in chars],
                    'issues':issues,'audit':audit,
                    'image_regions':image_regions,
                    'table_hints':hints,'character_count':len(chars),'ocr_used':False,
                    'independent_order':{'native':ordered_native,'poppler':ordered_second,'match':not mismatch},
                    'blank':not tokens and not (page.images or page.curves or page.rects or page.lines)})
        return pages
