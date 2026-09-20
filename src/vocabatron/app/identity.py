"""Content and structure identities, independent of physical PDF packaging."""
from ..domain import Lesson, Layout, Placement, digest
from ..ingest import whitespace
from ..validation import validate_layout, canonical_geometry

RULES = {'grid': 20, 'alphabet': 'A-Z', 'forward_only': True, 'all_answers_once': True,
         'maximal_runs_only': True, 'connected_crossings': True, 'parallel_overlap': False}
DEDUPE_VERSION = 'letter-owner-dihedral-and-crossings-v1'
CONTENT_VERSION = 'ordered-source-fields-v1'


def canonical_content(lesson):
    def fragments(values): return [whitespace(f.raw) for f in values]
    return {'version': CONTENT_VERSION, 'lesson': lesson.lesson, 'words': [
        {'ordinal': w.ordinal, 'raw': whitespace(w.raw), 'letters': w.letters,
         'part_of_speech': whitespace(w.part_of_speech),
         **{key: fragments(getattr(w,key)) for key in ('pronunciation','forms','definition','examples','fields')},
         'candidates': [{'relation': c.relation, 'text': whitespace(c.text), 'segment': c.segment,
                         'source_label_and_text': whitespace(c.source.raw)} for c in w.candidates]}
        for w in lesson.words]}


def identities(lesson, size=20):
    answers = sorted(w.letters for w in lesson.words)
    content = canonical_content(lesson)
    rules = {**RULES, 'grid': size}
    return {'lesson_content_id': digest(content), 'canonical': content,
            'answer_set_id': digest(answers), 'structure_family_id': digest({'answers': answers, 'rules': rules}),
            'rules': rules, 'source_word_keys': {w.word_id: digest(w.letters) for w in lesson.words}}


def structure(lesson, layout):
    checked = validate_layout(lesson, layout)
    keys = {w.word_id: digest(w.letters) for w in lesson.words}
    cells = {cell: {'letter': info['letter'], 'owners': [(keys[w], i, d) for w,i,d in info['owners']]}
             for cell,info in checked['cells'].items()}
    crossings = []
    for a,ia,b,ib in checked['crossings']:
        pair = sorted(((keys[a],ia),(keys[b],ib)))
        crossings.append((*pair[0],*pair[1]))
    crossings.sort()
    geometry = canonical_geometry(cells)
    stable = {'size': layout.size, 'placements': [
        {'word_key': keys[p.word_id], 'row': p.row, 'col': p.col, 'direction': p.direction}
        for p in layout.placements]}
    return {'version': DEDUPE_VERSION, 'crossings': crossings, 'crossing_hash': digest(crossings),
            'geometry': geometry, 'geometry_hash': digest(geometry), 'layout': stable,
            'crossing_count': len(crossings)}


def restore_layout(lesson, saved):
    ids = {digest(w.letters): w.word_id for w in lesson.words}
    layout = Layout(lesson_version=lesson.version, size=saved['size'], placements=tuple(
        Placement(word_id=ids[p['word_key']], row=p['row'], col=p['col'], direction=p['direction'])
        for p in saved['placements']))
    validate_layout(lesson, layout)
    return layout


def restore_crossings(lesson, saved):
    ids = {digest(w.letters): w.word_id for w in lesson.words}
    result = []
    for a,ia,b,ib in saved:
        pair = sorted(((ids[a],ia),(ids[b],ib)))
        result.append((*pair[0],*pair[1]))
    return result
