import itertools

import pytest
from ortools.sat.python import cp_model

from vocabatron.domain import Layout, Placement, Problem, SolverOptions, check_input
from vocabatron.solver import ExactModel, static_graph, solve_pair
from vocabatron.validation import validate_layout, validate_pair
from .helpers import lattice_witnesses, lesson_of


@pytest.mark.parametrize("count",[7,20,39])
def test_dynamic_two_witnesses_and_exact_complete_domain(count):
    lesson,a,b=lattice_witnesses(count)
    validate_pair(lesson,a,b)
    model=ExactModel(lesson)
    expected=sum(2*20*(21-len(w.letters)) for w in lesson.words)
    assert model.metrics["placements"]==expected
    model.hint(a)
    first,_=model.solve(SolverOptions(workers=1,seconds_per_layout=90))
    validate_layout(lesson,first)
    model.exclude_fingerprint(validate_layout(lesson,first)["crossings"])
    # The independent witness b is guaranteed distinct from a, not any arbitrary first.
    witness=b if validate_layout(lesson,first)["crossings"]==validate_layout(lesson,a)["crossings"] else a
    model.hint(witness)
    second,_=model.solve(SolverOptions(workers=1,seconds_per_layout=90))
    validate_pair(lesson,first,second)


def test_exhaustive_small_model_agrees_with_independent_validator():
    lesson=lesson_of(["AB","AC"]);model=ExactModel(lesson,size=3)
    lists=[model.placements[w.word_id] for w in lesson.words]
    legal=illegal=0
    for (xa,pa),(xb,pb) in itertools.product(*lists):
        layout=Layout(lesson_version=lesson.version,placements=(pa,pb),size=3)
        try:validate_layout(lesson,layout);expected=True;legal+=1
        except Problem:expected=False;illegal+=1
        model.model.clear_assumptions();model.model.add_assumptions([xa,xb])
        s=cp_model.CpSolver();s.parameters.num_search_workers=1
        status=s.solve(model.model)
        assert (status in (cp_model.FEASIBLE,cp_model.OPTIMAL))==expected
        if expected:
            actual={key for key,x in model.crossings.items() if s.boolean_value(x)}
            assert actual==set(validate_layout(lesson,layout)["crossings"])
    assert legal>0 and illegal>0


def test_bridge_and_sparse_graph():
    lesson=lesson_of(["AB","CD","BC"])
    graph=static_graph(lesson)
    assert lesson.words[1].word_id not in graph[lesson.words[0].word_id]
    model=ExactModel(lesson,size=4)
    layout,_=model.solve(SolverOptions(workers=1,seconds_per_layout=15))
    assert validate_layout(lesson,layout)["words"]==3


def test_static_disconnected():
    with pytest.raises(Problem,match="STATIC_GRAPH_DISCONNECTED"):
        ExactModel(lesson_of(["AB","XY"]))


@pytest.mark.parametrize("answers,code",[
    ([],"EMPTY_INPUT"),(["AB","ab"],"DUPLICATE_WORD"),(["A"],"SINGLE_LETTER"),
    (["A"*21],"WORD_TOO_LONG"),(["A-B"],"ANSWER_CHARACTERS"),(["A B"],"ANSWER_CHARACTERS"),
    (["CAFÉ"],"ANSWER_CHARACTERS"),(["CAN’T"],"ANSWER_CHARACTERS"),
])
def test_explicit_input_boundaries(answers,code):
    with pytest.raises(Problem,match=code):check_input(lesson_of(answers))


def test_no_candidates_and_repeated_letters():
    lesson=lesson_of(["AAA","AAAA"])
    model=ExactModel(lesson,size=5);layout,_=model.solve(SolverOptions(workers=1,seconds_per_layout=15))
    validate_layout(lesson,layout)
    bad=lesson.model_copy(update={"words":(lesson.words[0].model_copy(update={"candidates":()}),)})
    with pytest.raises(Problem,match="NO_CANDIDATE"):check_input(bad)


def test_exact_second_constraint_and_palindrome_exclusion():
    lesson=lesson_of(["ABA","ACA"])
    a=Layout(lesson_version=lesson.version,size=5,placements=(
        Placement(word_id=lesson.words[0].word_id,row=0,col=0,direction="across"),
        Placement(word_id=lesson.words[1].word_id,row=0,col=0,direction="down")))
    b=Layout(lesson_version=lesson.version,size=5,placements=(
        Placement(word_id=lesson.words[0].word_id,row=2,col=0,direction="across"),
        Placement(word_id=lesson.words[1].word_id,row=0,col=2,direction="down")))
    assert validate_layout(lesson,a)["crossings"]!=validate_layout(lesson,b)["crossings"]
    with pytest.raises(Problem,match="GEOMETRIC_COPY"):validate_pair(lesson,a,b)
    model=ExactModel(lesson,size=5);model.exclude_geometry(a)
    model.model.add_assumptions([model.lookup[p.word_id,p.row,p.col,int(p.direction=="down")] for p in b.placements])
    solver=cp_model.CpSolver();assert solver.solve(model.model)==cp_model.INFEASIBLE


def test_cancel_status_is_not_infeasible():
    lesson,a,b=lattice_witnesses(20);model=ExactModel(lesson)
    with pytest.raises(Problem) as err:model.solve(SolverOptions(workers=1,seconds_per_layout=.001),cancel=lambda:True)
    assert err.value.code=="CANCELLED"


def test_model_rejects_two_groups_despite_connected_potential_graph():
    l=lesson_of(["AB","AC","AD","AE"]);m=ExactModel(l,size=6)
    locations=[(0,0,0),(0,0,1),(4,4,0),(4,4,1)]
    m.model.add_assumptions([m.lookup[w.word_id,r,c,d] for w,(r,c,d) in zip(l.words,locations)])
    s=cp_model.CpSolver();s.parameters.num_search_workers=1
    assert s.solve(m.model)==cp_model.INFEASIBLE


def test_exhaustive_geometry_exclusion_does_not_overprune():
    l=lesson_of(["AB","BA"]);m=ExactModel(l,size=3)
    baseline=Layout(lesson_version=l.version,size=3,placements=(
        Placement(word_id=l.words[0].word_id,row=1,col=0,direction="across"),
        Placement(word_id=l.words[1].word_id,row=0,col=0,direction="down")))
    geometry=validate_layout(l,baseline)["geometry"];m.exclude_geometry(baseline)
    kept=removed=0
    for (xa,pa),(xb,pb) in itertools.product(*(m.placements[w.word_id] for w in l.words)):
        candidate=Layout(lesson_version=l.version,size=3,placements=(pa,pb))
        try:result=validate_layout(l,candidate);expected=result["geometry"]!=geometry
        except Problem:expected=False
        m.model.clear_assumptions();m.model.add_assumptions([xa,xb])
        s=cp_model.CpSolver();s.parameters.num_search_workers=1
        status=s.solve(m.model)
        assert (status in (cp_model.FEASIBLE,cp_model.OPTIMAL))==expected
        kept+=expected;removed+=not expected
    assert kept and removed


def test_partial_warm_hint_does_not_assign_missing_words_false():
    from vocabatron.solver import ExactModel
    from .helpers import lesson_of
    from vocabatron.domain import Layout,Placement
    lesson=lesson_of(['AB','AC']);model=ExactModel(lesson,size=3)
    partial=Layout(lesson_version=lesson.version,size=3,placements=(Placement(word_id=lesson.words[0].word_id,row=0,col=0,direction='across'),))
    model.hint(partial)
    hinted=set(model.model.proto.solution_hint.vars)
    assert hinted
    assert all(x.index not in hinted for x,p in model.placements[lesson.words[1].word_id])
    layout,metrics=model.solve(SolverOptions(seconds_per_layout=5,workers=1))
    assert len(layout.placements)==2
