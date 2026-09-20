from pathlib import Path
import random

from reportlab.pdfgen import canvas

from vocabatron.domain import Candidate, Choice, Fragment, FrozenClues, Layout, Lesson, Placement, Word


def lesson_of(answers,number=3):
    words=[]
    for i,text in enumerate(answers):
        wid=f"synthetic-{i}"
        fragment=Fragment(page=1,bbox=(0,0,100,20),raw="SYNONYM: invented clue")
        candidates=(Candidate(candidate_id=f"{wid}-choice",word_id=wid,relation="SYNONYM",text=f"created clue {i}",source=fragment,segment=0),)
        words.append(Word(word_id=wid,ordinal=str(i+1),raw=text,letters=text.upper(),part_of_speech="synthetic",candidates=candidates))
    return Lesson(lesson=number,source_sha256="synthetic-source",title=(),words=tuple(words),coverage_sha256="synthetic-coverage")


def frozen_for(lesson):
    return FrozenClues(lesson_version=lesson.version,source_sha256=lesson.source_sha256,
        choices=tuple(Choice(word_id=w.word_id,candidate_id=w.candidates[0].candidate_id) for w in lesson.words),
        model="synthetic-test-selector",model_digest="synthetic",ollama_version="synthetic",prompt_version="synthetic",parameters={})


def lattice_witnesses(count):
    """Two explicit witnesses, constructed before testing the production solver.

    A 2x2 symmetric block permits simultaneous interchange of two rows/columns.
    Other positions are independent, so this is not a whole-board symmetry.
    """
    rows,cols={7:(3,4),20:(10,10),39:(20,19)}[count]
    rng=random.Random(113)
    matrix=[[rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(cols)] for _ in range(rows)]
    matrix[0][0]=matrix[1][1]="A";matrix[0][1]=matrix[1][0]="B"
    for c in range(2,cols):matrix[1][c]=matrix[0][c]
    for r in range(2,rows):matrix[r][1]=matrix[r][0]
    answers=["".join(row) for row in matrix]+["".join(matrix[r][c] for r in range(rows)) for c in range(cols)]
    assert len(set(answers))==count
    lesson=lesson_of(answers)
    first=[];second=[]
    for r in range(rows):
        first.append(Placement(word_id=lesson.words[r].word_id,row=r,col=0,direction="across"))
        second.append(Placement(word_id=lesson.words[r].word_id,row=1-r if r<2 else r,col=0,direction="across"))
    for c in range(cols):
        first.append(Placement(word_id=lesson.words[rows+c].word_id,row=0,col=c,direction="down"))
        second.append(Placement(word_id=lesson.words[rows+c].word_id,row=0,col=1-c if c<2 else c,direction="down"))
    return lesson,Layout(lesson_version=lesson.version,placements=tuple(first)),Layout(lesson_version=lesson.version,placements=tuple(second))


def synthetic_template(path: Path,third=False):
    c=canvas.Canvas(str(path),pagesize=(620,800),invariant=1)
    c.setFont("Helvetica",11)
    c.drawString(45,765,"Name: __________________    Date: ______________")
    c.drawString(120,725,"Practice Crossword Lesson # _____")
    c.drawString(45,675,"Use the vocabulary to create a connected puzzle.")
    c.drawString(45,653,"Shade in any boxes that are left blank.")
    for i in range(21):
        c.line(55+i*24,50,55+i*24,530)
        c.line(55,50+i*24,535,50+i*24)
    c.showPage();c.setFont("Helvetica",12)
    c.drawString(120,710,"ACROSS");c.drawString(385,710,"DOWN")
    for x in (45,305,575):c.line(x,65,x,740)
    for y in (65,690,740):c.line(45,y,575,y)
    c.showPage()
    if third:c.drawString(45,740,"This additional synthetic page must be excluded.");c.showPage()
    c.save()


def synthetic_handout(path: Path):
    c=canvas.Canvas(str(path),pagesize=(612,792),invariant=1)
    c.setFont("Helvetica",11);c.drawString(70,748,"Invented Classroom Lesson 3 Words")
    for index,word in enumerate(("AB","AC")):
        top=700-index*90;bottom=top-90
        for x in (50,75,560):c.line(x,bottom,x,top)
        for y in (top,bottom):c.line(50,y,560,y)
        for y in (top-20,top-40,top-60):c.line(75,y,560,y)
        c.line(205,top-20,205,top);c.line(320,top-20,320,top)
        c.line(320,top-60,320,top-40)
        c.setFont("Helvetica",10)
        c.drawString(55,top-48,str(index+1));c.drawString(80,top-14,word+" (noun)")
        c.drawString(210,top-14,"[invented]");c.drawString(325,top-14,"FORMS: synthetic form")
        c.drawString(80,top-34,"A completely invented definition.")
        c.drawString(80,top-54,"SYNONYM: made up phrase, invented")
        c.drawString(325,top-54,"ANTONYM: created opposite")
        c.drawString(80,top-76,"This is a newly authored synthetic example.")
    c.showPage();c.save()


def synthetic_course(path,answers,number=3,*,positioned_heading=False):
    """Invented multipage source; layout witnesses are never encoded in the PDF."""
    c=canvas.Canvas(str(path),pagesize=(612,792),invariant=1)
    for page_start in range(0,len(answers),7):
        c.setFont('Helvetica',11)
        if positioned_heading:
            c.drawString(70,775,'Invented classroom')
            c.drawString(70,750,'Vocabulary');c.drawString(145,750,f'Lesson {number} Words')
            c.drawString(70,728,'An invented subtitle')
        else:c.drawString(70,748,f'Invented Classroom Lesson {number} Words')
        for index,word in enumerate(answers[page_start:page_start+7]):
            ordinal=page_start+index+1;top=700-index*90;bottom=top-90
            for x in (50,75,560):c.line(x,bottom,x,top)
            for y in (top,bottom):c.line(50,y,560,y)
            for y in (top-20,top-40,top-60):c.line(75,y,560,y)
            c.line(205,top-20,205,top);c.line(320,top-20,320,top);c.line(320,top-60,320,top-40)
            c.setFont('Helvetica',7)
            c.drawString(55,top-48,str(ordinal));c.drawString(80,top-14,word+' (noun)')
            c.drawString(210,top-14,'[invented]');c.drawString(325,top-14,'FORMS: synthetic form')
            c.drawString(80,top-34,'An entirely invented definition.')
            c.drawString(80,top-54,f'SYNONYM: invented clue {ordinal}, made up phrase')
            c.drawString(325,top-54,'ANTONYM: created opposite')
            c.drawString(80,top-76,'This is an authored synthetic example.')
        c.showPage()
    c.save()
