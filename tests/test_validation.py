import pytest
from pydantic import ValidationError

from vocabatron.domain import Layout, Placement, Problem
from vocabatron.validation import clue_rows, validate_clue_rows, validate_layout, validate_pair
from .helpers import frozen_for, lattice_witnesses, lesson_of


def test_common_start_has_one_number_two_clues():
    lesson,a,_=lattice_witnesses(7);result=validate_layout(lesson,a)
    assert len(result["starts"])<len(lesson.words)
    rows=clue_rows(lesson,a,frozen_for(lesson))
    assert sum(r["number"]==1 for r in rows)==2
    bad=dict(result["numbers"]);bad[a.placements[-1].word_id]=999
    with pytest.raises(Problem,match="INCORRECT_NUMBERING"):validate_layout(lesson,a,expected_numbers=bad)


@pytest.mark.parametrize("mutation",["omit","duplicate","shift","wrong_direction","bounds"])
def test_structural_error_injection(mutation):
    lesson,a,_=lattice_witnesses(7);ps=list(a.placements)
    if mutation=="omit":ps.pop()
    elif mutation=="duplicate":ps.append(ps[0])
    elif mutation=="shift":ps[0]=ps[0].model_copy(update={"col":1})
    elif mutation=="wrong_direction":ps[0]=ps[0].model_copy(update={"direction":"down"})
    else:ps[0]=ps[0].model_copy(update={"row":-1})
    with pytest.raises(Problem):validate_layout(lesson,a.model_copy(update={"placements":tuple(ps)}))


@pytest.mark.parametrize("answers,positions,code",[
    (["AB","BC"],[(0,0,"across"),(0,1,"across")],"PARALLEL_OVERLAP"),
    (["AB","CD"],[(0,0,"across"),(0,2,"across")],"UNREGISTERED_RUN"),
    (["AB","AC"],[(0,0,"across"),(1,0,"across")],"UNREGISTERED_RUN"),
    (["AB","AC"],[(0,0,"across"),(0,1,"down")],"CROSSING_LETTER"),
    (["AB","AC","DE","DF"],[(0,0,"across"),(0,0,"down"),(5,5,"across"),(5,5,"down")],"DISCONNECTED"),
])
def test_specific_illegal_geometry(answers,positions,code):
    l=lesson_of(answers);ps=tuple(Placement(word_id=w.word_id,row=r,col=c,direction=d) for w,(r,c,d) in zip(l.words,positions))
    with pytest.raises(Problem,match=code):validate_layout(l,Layout(lesson_version=l.version,placements=ps))


def test_fake_crossings_rejected_by_schema():
    l,a,b=lattice_witnesses(7)
    with pytest.raises(ValidationError):Layout.model_validate(a.model_dump()|{"crossings":[[1,2]]})


@pytest.mark.parametrize("field,value",[("text","invented replacement"),("relation","ANTONYM"),("word_id","other"),("number",999),("direction","down")])
def test_clue_injection(field,value):
    l,a,b=lattice_witnesses(7);f=frozen_for(l);rows=clue_rows(l,a,f);rows[0]=rows[0]|{field:value}
    with pytest.raises(Problem,match="CLUE_MISMATCH"):validate_clue_rows(l,a,f,rows)


def test_translation_and_transposition_not_second_versions():
    l,a,b=lattice_witnesses(7)
    translated=a.model_copy(update={"placements":tuple(p.model_copy(update={"row":p.row+5,"col":p.col+3}) for p in a.placements)})
    transposed=a.model_copy(update={"placements":tuple(p.model_copy(update={"row":p.col,"col":p.row,"direction":"down" if p.direction=="across" else "across"}) for p in a.placements)})
    for copy in (translated,transposed):
        with pytest.raises(Problem,match="SAME_CROSSINGS"):validate_pair(l,a,copy)
