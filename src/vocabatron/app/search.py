"""Complete projected structural search; auxiliary assignments are never variants."""
from __future__ import annotations
import hashlib
import importlib.metadata
import json
import time
import random
from ..domain import Problem, SolverOptions, digest
from ..solver import ExactModel, warm_hint, _hint_legal
from ..domain import Layout,Placement
from ..execution import checkpoint
from ..validation import validate_layout
from ..pdf import plan, clue_capacity
from .identity import restore_layout, restore_crossings, structure, RULES, DEDUPE_VERSION


def history_hint(lesson,size,history,seconds,seed):
    """Try bounded one-word moves as hints for the unchanged complete model.

    Every returned hint is independently validated against all saved classes.
    Failure says nothing about the remaining solution space.
    """
    started=time.monotonic();rng=random.Random(seed);words={w.word_id:w for w in lesson.words};attempts=0
    known_cross={item['crossing_hash'] for item in history};known_shapes={item['geometry_hash'] for item in history}
    sources=list(history[-8:]);rng.shuffle(sources)
    for saved in sources:
        old=restore_layout(lesson,saved['layout']);order=list(old.placements);rng.shuffle(order)
        for removed in order:
            checkpoint()
            remaining=[p for p in old.placements if p.word_id!=removed.word_id];word=words[removed.word_id];proposals=set()
            for other in remaining:
                for i,a in enumerate(words[other.word_id].letters):
                    for j,b in enumerate(word.letters):
                        if a==b:
                            proposals.add((other.row-j,other.col+i,'down') if other.direction=='across' else (other.row+i,other.col-j,'across'))
            proposals=list(sorted(proposals));rng.shuffle(proposals)
            for row,col,direction in proposals:
                if time.monotonic()-started>=seconds:return None,{'kind':'bounded history move','attempts':attempts,'complete':False}
                attempts+=1;candidate=Placement(word_id=word.word_id,row=row,col=col,direction=direction)
                if not _hint_legal(remaining,candidate,words,size):continue
                layout=Layout(lesson_version=lesson.version,size=size,placements=tuple([*remaining,candidate]))
                try:entry=structure(lesson,layout)
                except Problem:continue
                if entry['crossing_hash'] not in known_cross and entry['geometry_hash'] not in known_shapes:
                    return layout,{'kind':'bounded history move','attempts':attempts,'complete':True,'seconds':time.monotonic()-started}
    return None,{'kind':'bounded history move','attempts':attempts,'complete':False,'seconds':time.monotonic()-started}


def next_structure(lesson, frozen, profile, history, rejections, *, seconds=90, threads=1, seed=37,
                   size=20, cancel=None, rejected=None, stage=None):
    stage=stage or (lambda *args:None)
    stage('Building crossword model')
    costs,capacities=clue_capacity(lesson,frozen,profile)
    model=ExactModel(lesson,size,costs,capacities,check_graph=False)
    for item in history:
        model.exclude_fingerprint(restore_crossings(lesson,item['crossings']))
        model.exclude_geometry(restore_layout(lesson,item['layout']))
    for item in rejections:model.exclude_layout(restore_layout(lesson,item['layout']))
    stage('Finding a new arrangement')
    previous=restore_layout(lesson,history[-1]['layout']) if history else None
    hint,hint_metrics=history_hint(lesson,size,history,min(2,seconds/10),seed) if history else (None,{})
    if hint is None:
        hint,hint_metrics=warm_hint(lesson,size,seed,seconds=min(6,seconds/5),exclude=previous)
    model.hint(hint)
    deadline=time.monotonic()+seconds
    evidence={'rules':{**RULES,'grid':size},'dedupe_version':DEDUPE_VERSION,
              'solver_version':importlib.metadata.version('ortools'),
              'parameters':{'threads':threads,'seconds':seconds,'seed':seed},
              'history_hash':digest(history),'rejection_hash':digest(rejections),
              'model':model.metrics,'hint':hint_metrics,'proof_kind':'traceable CP-SAT conclusion; not a formal proof certificate'}
    stage('Finding a new arrangement',{'hint_complete':len(hint.placements)==len(lesson.words) if hint else False,
                                      'hint_kind':hint_metrics.get('kind','bounded warm start')})
    while True:
        if cancel and cancel():raise Problem('CANCELLED','Search stopped')
        remaining=deadline-time.monotonic()
        if remaining<=0:raise Problem('UNKNOWN','Search slice ended without an exhaustion proof',details=evidence)
        try:
            layout,metrics=model.solve(SolverOptions(workers=threads,seed=seed,seconds_per_layout=min(remaining,900),optimization_seconds=0),cancel)
        except Problem as exc:
            if exc.code=='INFEASIBLE':
                evidence.update({'status':'INFEASIBLE','remaining_model_sha256':hashlib.sha256(str(model.model.proto).encode()).hexdigest(),
                                 'solver_metrics':exc.details,'completed_at':time.time()})
                return None,evidence
            if exc.code not in ('CANCELLED','PAUSED_BY_USER','ATTEMPT_EXPIRED'):
                raise Problem(exc.code,exc.message,details={**evidence,'status':exc.code,
                              'solver_metrics':exc.details,'completed_at':time.time()}) from exc
            raise
        candidate=structure(lesson,layout)
        # No typography representation may erase another representative of its
        # class. Only this exact placement tuple is blocked on layout overflow.
        try:plan(lesson,layout,frozen,profile)
        except Problem as exc:
            if exc.code not in ('CLUE_OVERFLOW','CLUE_TOO_WIDE'):raise
            item={'layout':candidate['layout'],'reason':exc.code}
            if rejected:rejected(item)
            rejections=[*rejections,item];evidence['rejection_hash']=digest(rejections)
            model.exclude_layout(layout);model.hint(None);continue
        if any(candidate['crossing_hash']==old['crossing_hash'] or candidate['geometry_hash']==old['geometry_hash'] for old in history):
            raise Problem('HISTORY_EXCLUSION_FAILED','Independent history comparison rejected the solver result')
        return layout,{**evidence,'status':metrics['status'],'solver_metrics':metrics}
