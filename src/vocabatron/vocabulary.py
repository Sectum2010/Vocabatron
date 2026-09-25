"""Vocabulary anchors over positioned evidence, with cross-page open entries."""
from __future__ import annotations
from collections import Counter
import re
import time
from .documents import NativeTextBackend, PARSER_VERSION, visible
from .domain import Candidate, Fragment, Lesson, Problem, Word, check_input, digest
from .storage import sha256

TITLE=re.compile(r'\bLesson\s*(?:#\s*)?(\d+)\b',re.I)
HEADER=re.compile(r'^(?:(\d+)[.)]?\s+)?([A-Za-z]+)\s*\(([^()]+)\)(.*)$',re.S)
BARE=re.compile(r'^(?:(\d+)[.)]?\s+)?([A-Z][A-Z]+)$')
LABEL=re.compile(r'\b(SYNONYMS?|ANTONYMS?)\s*:',re.I)
METADATA=re.compile(r'^(?:Page\s+\d+|Name\b.*|Date\b.*|Class\b.*|Vocabulary|Word List|No\.?|#|Word|Words|Meaning|Definition|Example|Examples|Forms|Synonyms?|Antonyms?|Pronunciation)$',re.I)


def fragment(page,block):
    lookup={t['id']:t for t in page['tokens']}
    indices=tuple(i for key in block['token_ids'] for i in lookup[key].get('char_indices',[]))
    return Fragment(page=page['page'],bbox=tuple(block['bbox']),raw=block['text'],char_indices=indices)


def events(page):
    result=[]
    for block in page['blocks']:
        f=fragment(page,block);text=visible(f.raw).strip()
        event={'type':'field','fragment':f.model_dump(mode='json'),'block_id':block['id'],
               'page':page['page'],'top':f.bbox[1],'hint_bbox':block.get('hint_bbox')}
        title=TITLE.search(text);header=HEADER.fullmatch(' '.join(text.split()));bare=BARE.fullmatch(text)
        if block.get('metadata') or not text:event['type']='metadata'
        elif title:event.update(type='title',lesson=int(title[1]))
        elif re.match(r'^(?:SYN|ANT)[^\s:]{0,16}\s*:',text,re.I) and not LABEL.search(text):
            event['critical_issue']='CANDIDATE_LABEL_UNCERTAIN'
        elif header:event.update(type='entry',raw=header[2],pos=header[3],source_ordinal=header[1],tail=header[4])
        elif bare and not METADATA.fullmatch(text):event.update(type='entry',raw=bare[2],pos='',source_ordinal=bare[1],tail='')
        elif METADATA.fullmatch(text):event['type']='metadata'
        result.append(event)
    # Separate full entry columns from the smaller field columns of a table.
    # Only observed entry headers establish a column; forms/candidate labels
    # cannot create a second reading-order stream.
    anchors=sorted(e['fragment']['bbox'][0] for e in result if e['type']=='entry')
    breaks=[b for a,b in zip(anchors,anchors[1:]) if b-a>page['width']*.28]
    if breaks:
        first=min(e['top'] for e in result if e['type']=='entry')
        prefix=[e for e in result if e['top']<first]
        body=[e for e in result if e['top']>=first]
        result=prefix+sorted(body,key=lambda e:(sum(e['fragment']['bbox'][0]>=x-12 for x in breaks),e['top'],e['fragment']['bbox'][0]))
    return result


def extract_pages(source,page_numbers,*,limits,use_tables=True):
    if not page_numbers or len(page_numbers)>8 or any(type(p)!=int or p<1 for p in page_numbers):
        raise Problem('INPUT_INVALID','Invalid bounded page group')
    pages=NativeTextBackend().extract(source,page_numbers,limits,use_tables=use_tables)
    for page in pages:page['events']=events(page)
    return {'parser_version':PARSER_VERSION,'source_sha256':sha256(source),'pages':pages}


def entry_word(entry,source_id,number,order):
    fields=entry['fields'];wid=f'w-{digest([source_id,number,order,entry["raw"]])[:16]}'
    candidates=[];forms=[];pronunciation=[];definitions=[];examples=[];separators=[]
    candidate_fields=[];seen_candidates=False
    for f in fields:
        text=visible(f.raw).strip()
        if not text or text.isdigit() or HEADER.fullmatch(' '.join(text.split())) or BARE.fullmatch(text):continue
        labels=list(LABEL.finditer(text))
        if labels:
            seen_candidates=True
            for i,label in enumerate(labels):
                end=labels[i+1].start() if i+1<len(labels) else len(text)
                candidate_fields.append([label[1],text[label.end():end],f,[f]])
        elif re.match(r'^FORMS?\s*:',text,re.I):forms.append(f)
        elif text.startswith('[') or text.endswith(']'):pronunciation.append(f)
        elif not seen_candidates:definitions.append(f)
        else:
            previous=next((c for c in reversed(candidate_fields) if abs(c[2].bbox[0]-f.bbox[0])<12),None)
            if previous and not text.endswith(('.', '!', '?')) and (previous[1].rstrip().endswith((',', ';')) or
                    (text[:1].islower() and len(text.split())<5)) and (f.page>previous[2].page or f.bbox[1]-previous[2].bbox[3]<24):
                previous[1]+='\n'+text;previous[3].append(f)
            else:examples.append(f)
    tail=entry.get('tail','').strip()
    if tail:
        f=fields[0];match=re.search(r'\[[^\]]*\]',tail)
        if match:pronunciation.insert(0,f.model_copy(update={'raw':match[0]}))
        elif tail.startswith('['):pronunciation.insert(0,f.model_copy(update={'raw':tail}))
        match=re.search(r'FORMS?\s*:.*',tail,re.I)
        if match:forms.insert(0,f.model_copy(update={'raw':match[0]}))
    relation_count=Counter()
    for label,text,f,origins in candidate_fields:
        if len(origins)>1:f=f.model_copy(update={'raw':'\n'.join(o.raw for o in origins)})
        relation='SYNONYM' if label.upper().startswith('SYN') else 'ANTONYM'
        # Explicit delimiters followed by wraps are one boundary. Spaces never
        # split multi-word phrases. The original fragment remains unchanged.
        text=re.sub(r'([,;])\s*\n\s*',r'\1 ',text)
        parts=re.split(r'([,;]|\n+)',text)
        for i in range(0,len(parts),2):
            value=' '.join(parts[i].split())
            if not value:
                if i not in (0,len(parts)-1):raise Problem('CANDIDATE_BOUNDARY_REQUIRES_DECISION','A source list contains an empty segment')
                continue
            segment=relation_count[relation];relation_count[relation]+=1
            candidates.append(Candidate(candidate_id=f'{wid}-{relation.lower()}-{segment+1}',word_id=wid,
                relation=relation,text=value,source=f,segment=segment))
            separators.append({'candidate_id':candidates[-1].candidate_id,'separator':parts[i+1] if i+1<len(parts) else '',
                'source_page':f.page,'bbox':f.bbox,'source_fragments':[o.model_dump(mode='json') for o in origins]})
    word=Word(word_id=wid,ordinal=entry.get('source_ordinal') or str(order),raw=entry['raw'],letters=entry['raw'].upper(),
        part_of_speech=entry['pos'],fields=tuple(fields),forms=tuple(forms),pronunciation=tuple(pronunciation),
        definition=tuple(definitions),examples=tuple(examples),candidates=tuple(candidates))
    return word,{'source_order':order,'source_ordinal':entry.get('source_ordinal'),'headword':entry['raw'],
                 'blocks':entry['blocks'],'candidate_segments':separators,'extra_sections':[]}


def assemble(source_id,pages,*,total_pages=None,metrics=None):
    courses=[];current=None;entry=None;unresolved=[];pending=[];owners=[]
    observed=[p['page'] for p in pages]
    expected=list(range(1,(total_pages if total_pages is not None else max(observed,default=0))+1))
    if sorted(observed)!=expected:unresolved.append({'code':'SOURCE_PAGE_SEQUENCE','pages':sorted(set(expected)-set(observed)),
        'duplicate_pages':[p for p,n in Counter(observed).items() if n>1]})

    def finish_entry():
        nonlocal entry
        if entry is None:return
        try:
            word,proof=entry_word(entry,source_id,current['number'],len(current['words'])+1)
            current['words'].append(word);current['entries'].append(proof)
        except Problem as exc:current['issues'].append({'code':exc.code,'page':entry['fields'][0].page})
        entry=None

    for page in sorted(pages,key=lambda p:p['page']):
        page_issues=list(page['issues'])
        page_issues.extend({'code':e['critical_issue'],'page':page['page'],'bbox':e['fragment']['bbox']}
                           for e in page['events'] if e.get('critical_issue'))
        if page.get('ocr_used'):
            lookup={t['id']:t for t in page['tokens']}
            for event in page['events']:
                text=visible(event['fragment']['raw'])
                if event['type'] not in ('entry','title') and not LABEL.search(text):continue
                block=next(b for b in page['blocks'] if b['id']==event['block_id'])
                critical=[lookup[k] for k in block['token_ids']]
                if event['type']=='entry':critical=[t for t in critical if event['raw'] in t['text']]
                disputed=[t['id'] for t in critical if t['backend']!='native-pdfplumber-poppler' and not t.get('consensus')]
                if disputed:page_issues.append({'code':'OCR_CRITICAL_DISAGREEMENT','page':page['page'],'bbox':event['fragment']['bbox'],'token_ids':disputed})
        segments=[];start=0;segment_course=current
        first_entry=next((e['top'] for e in page['events'] if e['type']=='entry'),float('inf'))
        heading=next((e for e in page['events'] if e['type']=='title' and e['top']<first_entry),None)
        for event in page['events']:
            f=Fragment.model_validate(event['fragment']);text=visible(f.raw).strip()
            owner={'page':page['page'],'block_id':event['block_id'],'token_ids':next(b['token_ids'] for b in page['blocks'] if b['id']==event['block_id'])}
            owners.append(owner)
            if event['type']=='title':
                if current is None or current['number']!=event['lesson']:
                    finish_entry()
                    segments.append((start,event['top'],segment_course));start=event['top']
                    current={'number':event['lesson'],'titles':[],'words':[],'issues':[],'pages':set(),'entries':[]}
                    courses.append(current);segment_course=current
                current['titles'].extend(pending);pending=[];current['titles'].append(f)
                owner.update(owner='lesson_title',lesson=current['number'])
            elif event['type']=='metadata':owner['owner']='metadata'
            elif entry is None and heading and event['top']<first_entry and event['top']!=heading['top']:
                if current and heading['top']<event['top']:current['titles'].append(f)
                else:pending.append(f)
                owner['owner']='lesson_heading'
            elif event['type']=='entry':
                finish_entry()
                if current is None:
                    unresolved.append({'code':'LESSON_BOUNDARY_UNCLEAR','page':page['page'],'bbox':f.bbox});owner['owner']='unresolved';continue
                entry={**event,'fields':[f],'blocks':[(page['page'],event['block_id'])]}
                owner.update(owner='entry_header',lesson=current['number'],entry=len(current['words'])+1)
            elif entry is not None:
                if text.isdigit():
                    if entry.get('source_ordinal') and entry['source_ordinal']!=text:
                        current['issues'].append({'code':'ORDINAL_CONFLICT','page':page['page'],'bbox':f.bbox})
                    entry['source_ordinal']=text
                entry['fields'].append(f);entry['blocks'].append((page['page'],event['block_id']))
                owner.update(owner='entry_field',lesson=current['number'],entry=len(current['words'])+1)
            else:
                unresolved.append({'code':'UNASSIGNED_SOURCE_CONTENT','page':page['page'],'bbox':f.bbox,'text':f.raw});owner['owner']='unresolved'
            if current:current['pages'].add(page['page'])
        segments.append((start,float('inf'),segment_course))
        for issue in page_issues:
            bbox=issue.get('bbox');targets=[]
            for low,high,course in segments:
                if course is not None and (bbox is None or bbox[1]<high and bbox[3]>low) and course not in targets:targets.append(course)
            if not targets:unresolved.append(issue)
            for course in targets:course['issues'].append(issue)
    finish_entry()
    validation_started=time.monotonic()
    for p in pages:
        expected_tokens=Counter(t['id'] for t in p['tokens'])
        assigned=Counter(t for o in owners if o['page']==p['page'] for t in o['token_ids'])
        if assigned!=expected_tokens:unresolved.append({'code':'SOURCE_OWNERSHIP','page':p['page']})
    results=[]
    for c in courses:
        explicit=[(e['source_order'],int(e['source_ordinal'])) for e in c['entries'] if e['source_ordinal']]
        if any(a!=b for a,b in explicit):c['issues'].append({'code':'ORDINAL_SEQUENCE','pages':sorted(c['pages'])})
        report={'validator_version':PARSER_VERSION,'source_sha256':source_id,'pages':sorted(c['pages']),
            'issues':c['issues'],'entries':c['entries'],'token_ownership':[o for o in owners if o.get('lesson')==c['number']],
            'ocr_used':any(p.get('ocr_used') for p in pages if p['page'] in c['pages']),'manual_review':'NOT_PERFORMED'}
        lesson=Lesson(schema_version=2,lesson=c['number'],source_sha256=source_id,title=tuple(c['titles']),
            words=tuple(c['words']),coverage_sha256=digest(report))
        try:check_input(lesson)
        except Problem as exc:report['issues'].append({'code':exc.code})
        report['status']='NEEDS_ATTENTION' if report['issues'] else 'VERIFIED'
        results.append({'lesson':lesson.model_dump(mode='json'),'coverage':report,
            'transcript':'\n\n'.join([f'Lesson {lesson.lesson}',*[f.raw for f in lesson.title],*[f.raw for w in lesson.words for f in w.fields]])})
    if metrics is not None:metrics['validation_seconds']=time.monotonic()-validation_started
    return {'parser_version':PARSER_VERSION,'lessons':results,'unresolved_sections':unresolved,'token_ownership':owners,
            'status':'PARTIALLY_IMPORTED' if unresolved or any(r['coverage']['issues'] for r in results) or not results else 'READY'}
