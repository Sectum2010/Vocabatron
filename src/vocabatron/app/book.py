"""Bounded page extraction reusing the ordered field/word checks of the core."""
from __future__ import annotations
from collections import Counter
import re
import subprocess
import tempfile
from pathlib import Path
import pdfplumber
from ..domain import Fragment, Lesson, Problem, check_input, digest
from ..ingest import _fragment, word_from_fields
from ..storage import sha256
from ..transcription import ordered_evidence, poppler_pages, spatial_lines

TITLE=re.compile(r'\bLesson\s*(?:#\s*)?(\d+)\b',re.I)
METADATA=re.compile(r'^(?:(?:Vocabulary|Vocabulary Workbook|Vocabulary Lessons|Word List|Contents|Table of Contents|Name|Date|Class|Page)\b.*|\d+|Lesson\s*#?\s*\d+(?:\s*[-–—.:].*)?)$',re.I)
COLUMN=re.compile(r'^(?:No\.?|#|Word|Words|Meaning|Definition|Example|Examples|Forms|Synonyms?|Antonyms?|Pronunciation)$',re.I)


def extract_pages(source, page_numbers, *, limits):
    """Executed only inside the supervised document process. Global pages persist."""
    if not page_numbers or len(page_numbers)>8 or any(type(p)!=int or p<1 for p in page_numbers):
        raise Problem('INPUT_INVALID','Invalid bounded page group')
    with tempfile.NamedTemporaryFile(suffix='.xml',dir=source.parent) as out:
        subprocess.run(['/usr/bin/pdftotext','-f',str(min(page_numbers)),'-l',str(max(page_numbers)),
                        '-bbox-layout',str(source),out.name],stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=60)
        if Path(out.name).stat().st_size>limits['ipc_bytes']:raise Problem('RESOURCE_LIMIT','Text extraction output exceeds its limit')
        poppler=poppler_pages(Path(out.name).read_text())
    source_hash=sha256(source);results=[]
    with pdfplumber.open(source) as pdf:
        for number in page_numbers:
            page=pdf.pages[number-1];chars=page.chars;second=poppler[number-min(page_numbers)]
            owned=Counter();events=[];audit=[];issues=[];redundant=[]
            if len(chars)>limits['characters']:raise Problem('RESOURCE_LIMIT','Page character limit exceeded')
            tables=sorted(page.find_tables(),key=lambda t:(t.bbox[1],t.bbox[0]))
            if not chars:
                visible=bool(page.images or page.curves or page.rects or page.lines)
                if visible:issues.append({'code':'NO_TEXT_LAYER','page':number,'bbox':[0,0,page.width,page.height]})
                results.append({'page':number,'events':[],'issues':issues,'chars':[], 'audit':[], 'blank':not visible,'character_count':0})
                continue
            for ti,table in enumerate(tables):
                matrix=table.extract()
                starts=[i for i,row in enumerate(matrix) if row and row[0] and row[0].strip().isdigit()]
                # A title inside a table is a real boundary, not an ordinal reset.
                title_rows={i for i,row in enumerate(matrix) if i not in starts and row and row[0] and TITLE.search(row[0]) and
                            len([v for v in row if v and v.strip()])==1 and not re.search(r'\(.+?\)',row[0])}
                boundaries=sorted(set(starts)|title_rows|{len(matrix)})
                processed_rows=set()
                for start in starts:
                    end=next(b for b in boundaries if b>start);fields=[]
                    for ri in range(start,end):
                        processed_rows.add(ri)
                        for ci,raw in enumerate(matrix[ri]):
                            if raw is None:continue
                            bbox=table.rows[ri].cells[ci]
                            if bbox is None:continue
                            fragment=_fragment(page,bbox,raw)
                            evidence=ordered_evidence(fragment,chars,second);audit.append(evidence)
                            if not evidence['table_order_match'] or not evidence['poppler_order_match']:
                                issues.append({'code':'TRANSCRIPTION_REQUIRES_DECISION','page':number,'bbox':bbox})
                            if fragment.char_indices and all(owned[i] for i in fragment.char_indices):
                                redundant.append({'bbox':bbox,'char_indices':fragment.char_indices});continue
                            fields.append(fragment);owned.update(fragment.char_indices)
                    ordinal=matrix[start][0].strip()
                    try:
                        word=word_from_fields(fields,f'w-{digest([source_hash,number,ti,ordinal])[:16]}',ordinal,number)
                        event={'type':'word','word':word.model_dump(mode='json')}
                    except Problem as exc:
                        event={'type':'invalid_word','code':exc.code,'fields':[f.model_dump(mode='json') for f in fields],'ordinal':ordinal}
                    event.update({'page':number,'top':min((f.bbox[1] for f in fields),default=table.bbox[1]),'table':ti})
                    events.append(event)
                for ri,row in enumerate(matrix):
                    if ri in processed_rows:continue
                    for ci,raw in enumerate(row):
                        bbox=table.rows[ri].cells[ci]
                        if raw is None or bbox is None:continue
                        fragment=_fragment(page,bbox,raw)
                        if fragment.char_indices and all(owned[i] for i in fragment.char_indices):continue
                        if not raw.strip():owned.update(fragment.char_indices);continue
                        evidence=ordered_evidence(fragment,chars,second);audit.append(evidence)
                        owned.update(fragment.char_indices)
                        match=TITLE.search(raw)
                        if match:
                            events.append({'type':'title','lesson':int(match[1]),'fragment':fragment.model_dump(mode='json'),'page':number,'top':bbox[1]})
                        elif COLUMN.fullmatch(raw.strip()):
                            events.append({'type':'metadata','fragment':fragment.model_dump(mode='json'),'page':number,'top':bbox[1]})
                        else:issues.append({'code':'UNRESOLVED_TABLE_SECTION','page':number,'bbox':bbox,'text':raw[:500]})
            # Account for every remaining nonblank character, one spatial line at a time.
            groups=[]
            for index,ch in sorted(enumerate(chars),key=lambda pair:((pair[1]['top']+pair[1]['bottom'])/2,pair[1]['x0'])):
                if owned[index] or not ch['text'].strip():continue
                mid=(ch['top']+ch['bottom'])/2
                group=next((g for g in reversed(groups) if abs(g[0]-mid)<=2.5),None)
                if group is None:groups.append([mid,[index]])
                else:group[1].append(index)
            # A multi-line heading is one source region. Its descriptive lines
            # are retained, not discarded merely because only one line carries
            # the explicit lesson number. Gaps without a unique heading remain
            # independently classified and may require attention.
            gaps=[];bottom=0
            for table in tables:
                if table.bbox[1]>bottom:gaps.append((bottom,table.bbox[1]))
                bottom=max(bottom,table.bbox[3])
            if bottom<page.height:gaps.append((bottom,page.height))
            merged=[];taken=set()
            for low,high in gaps:
                members=[i for i,g in enumerate(groups) if low<=g[0]<high]
                indices=[index for i in members for index in groups[i][1]]
                if not indices:continue
                selected=[chars[i] for i in indices]
                region=(min(c['x0'] for c in selected)-.01,min(c['top'] for c in selected)-.01,
                        max(c['x1'] for c in selected)+.01,max(c['bottom'] for c in selected)+.01)
                # Many PDFs encode spaces only as positioning, not space
                # glyphs. Use the same text-layer spacing as the later checked
                # fragment so an adjacent word cannot hide a Lesson boundary.
                raw=page.crop(region).extract_text() or ''
                matches=TITLE.findall(raw)
                if tables and len(matches)==1:
                    merged.append([groups[members[0]][0],indices]);taken.update(members)
            groups=sorted([g for i,g in enumerate(groups) if i not in taken]+merged,key=lambda g:g[0])
            for _,indices in groups:
                subset=[chars[i] for i in indices]
                bbox=(min(c['x0'] for c in subset)-.01,min(c['top'] for c in subset)-.01,
                      max(c['x1'] for c in subset)+.01,max(c['bottom'] for c in subset)+.01)
                # pdfplumber's line text keeps word boundaries; char evidence proves its order.
                fragment=_fragment(page,bbox,page.crop(bbox).extract_text() or '')
                evidence=ordered_evidence(fragment,chars,second);audit.append(evidence);owned.update(fragment.char_indices)
                if not evidence['table_order_match'] or not evidence['poppler_order_match']:
                    issues.append({'code':'TRANSCRIPTION_REQUIRES_DECISION','page':number,'bbox':bbox})
                match=TITLE.search(fragment.raw)
                if match and tables:
                    events.append({'type':'title','lesson':int(match[1]),'fragment':fragment.model_dump(mode='json'),'page':number,'top':bbox[1]})
                elif METADATA.fullmatch(fragment.raw.strip()):
                    events.append({'type':'metadata','fragment':fragment.model_dump(mode='json'),'page':number,'top':bbox[1]})
                else:issues.append({'code':'UNASSIGNED_SOURCE_CONTENT','page':number,'bbox':bbox,'text':fragment.raw[:500]})
            bad=[i for i,ch in enumerate(chars) if ch['text'].strip() and owned[i]!=1]
            if bad:issues.append({'code':'SOURCE_OWNERSHIP','page':number,'char_indices':bad})
            for evidence in audit:
                if not evidence['table_order_match'] or not evidence['poppler_order_match']:
                    issue={'code':'TRANSCRIPTION_REQUIRES_DECISION','page':number,'bbox':evidence['bbox']}
                    if issue not in issues:issues.append(issue)
            if Counter(c for ch in chars for c in ch['text'] if not c.isspace())!=Counter(c for w in second for c in w['text'] if not c.isspace()):
                issues.append({'code':'SECOND_EXTRACTOR_MISMATCH','page':number})
            results.append({'page':number,'events':sorted(events,key=lambda e:e['top']),'issues':issues,'chars':chars,
                            'audit':audit,'redundant_inferred_cells':redundant,'character_count':len(chars),
                            'tables':[t.bbox for t in tables]})
    return {'source_sha256':source_hash,'pages':results}


def assemble(source_id, pages, *, total_pages=None):
    """Separate course failure from unrelated, fully checked courses."""
    from ..domain import Word
    courses=[];current=None;unassigned=[]
    observed=[p['page'] for p in pages]
    expected=list(range(1,(total_pages if total_pages is not None else max(observed,default=0))+1))
    if sorted(observed)!=expected:
        unassigned.append({'code':'SOURCE_PAGE_SEQUENCE','pages':sorted(set(expected)-set(observed)),
                           'duplicate_pages':[p for p,n in Counter(observed).items() if n>1]})
    for page in sorted(pages,key=lambda p:p['page']):
        page_courses=[];segments=[];segment_start=0;segment_course=current
        for event in page['events']:
            if event['type']=='title':
                if current is None or current['number']!=event['lesson']:
                    segments.append((segment_start,event['top'],segment_course))
                    current={'number':event['lesson'],'words':[],'titles':[],'issues':[],'pages':set()};courses.append(current)
                    segment_start=event['top'];segment_course=current
                current['titles'].append(Fragment.model_validate(event['fragment']))
            elif event['type'] in ('word','invalid_word'):
                if current is None:
                    unassigned.append({'code':'LESSON_BOUNDARY_UNCLEAR','page':page['page']});continue
                if current not in page_courses:page_courses.append(current)
                current['pages'].add(page['page'])
                if event['type']=='word':current['words'].append(Word.model_validate(event['word']))
                else:current['issues'].append({'code':event['code'],'page':event['page'],'ordinal':event['ordinal']})
        segments.append((segment_start,float('inf'),segment_course))
        # Localized extraction errors affect their source region. An unrelated
        # complete lesson on the same page can still be independently published.
        for issue in page['issues']:
            bbox=issue.get('bbox')
            if bbox is None and issue.get('char_indices'):
                chars=[page['chars'][i] for i in issue['char_indices']]
                bbox=[0,min(c['top'] for c in chars),0,max(c['bottom'] for c in chars)]
            if bbox is None:
                targets=page_courses
            else:
                targets=[]
                for low,high,course in segments:
                    if course is not None and bbox[1]<high and bbox[3]>low and course not in targets:targets.append(course)
            if targets:
                for course in targets:course['issues'].append(issue)
            else:unassigned.append(issue)
    results=[]
    for course in courses:
        words=course['words'];ordinals=[int(w.ordinal) for w in words]
        if ordinals!=list(range(1,len(words)+1)) or not words:
            course['issues'].append({'code':'ORDINAL_SEQUENCE','pages':sorted(course['pages'])})
        report={'validator_version':'ordered-book-v1','source_sha256':source_id,'pages':sorted(course['pages']),
                'issues':course['issues'],'manual_review':'NOT_PERFORMED'}
        lesson=Lesson(schema_version=2,lesson=course['number'],source_sha256=source_id,
                      title=tuple(course['titles']),words=tuple(words),coverage_sha256=digest(report))
        try:check_input(lesson)
        except Problem as exc:report['issues'].append({'code':exc.code})
        report['status']='NEEDS_ATTENTION' if report['issues'] else 'VERIFIED'
        transcript='\n\n'.join([f'Lesson {lesson.lesson}',*[f.raw for t in (lesson.title,*[w.fields for w in words]) for f in t]])
        results.append({'lesson':lesson.model_dump(mode='json'),'coverage':report,'transcript':transcript})
    return {'lessons':results,'unresolved_sections':unassigned,
            'status':'PARTIALLY_IMPORTED' if unassigned or any(r['coverage']['issues'] for r in results) else 'READY'}
