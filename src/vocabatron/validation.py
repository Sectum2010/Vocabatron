"""Independent reconstruction; no imports from the solver or PDF drawing code."""
from __future__ import annotations

from collections import Counter, defaultdict
import json

from .domain import Layout, Lesson, Problem, check_input, selected_candidates


def canonical_geometry(cells):
    representations=[]
    for swap in (False,True):
        for sr in (-1,1):
            for sc in (-1,1):
                transformed=[]
                for (r,c),cell in cells.items():
                    a,b=(c,r) if swap else (r,c)
                    transformed.append((sr*a,sc*b,cell["letter"],tuple(sorted(x[0] for x in cell["owners"]))))
                lowr=min(x[0] for x in transformed);lowc=min(x[1] for x in transformed)
                representations.append(tuple(sorted((r-lowr,c-lowc,ch,ids) for r,c,ch,ids in transformed)))
    return min(representations)


def validate_layout(lesson: Lesson, layout: Layout, *, expected_numbers=None):
    check_input(lesson,layout.size)
    if layout.lesson_version != lesson.version:
        raise Problem("LAYOUT_VERSION", "布局不属于当前课表")
    words={w.word_id:w for w in lesson.words}
    if Counter(p.word_id for p in layout.placements) != Counter(words.keys()):
        raise Problem("ANSWER_COVERAGE", "布局必须恰好使用全部主词一次")
    cells={};registered={}
    for p in layout.placements:
        word=words[p.word_id]
        dr,dc=(0,1) if p.direction=="across" else (1,0)
        registered[(p.direction,p.row,p.col)]=word.letters
        for index,letter in enumerate(word.letters):
            coordinate=p.row+dr*index,p.col+dc*index
            if not all(0<=x<layout.size for x in coordinate):
                raise Problem("OUT_OF_BOUNDS", "答案超出网格")
            if coordinate not in cells:
                cells[coordinate]={"letter":letter,"owners":[]}
            cell=cells[coordinate]
            if cell["letter"] != letter:
                raise Problem("CROSSING_LETTER", "交叉字母不一致")
            if any(owner[2]==p.direction for owner in cell["owners"]):
                raise Problem("PARALLEL_OVERLAP", "同方向答案重叠")
            cell["owners"].append((p.word_id,index,p.direction))
    runs={}
    for direction,(dr,dc) in (("across",(0,1)),("down",(1,0))):
        for r,c in cells:
            if (r-dr,c-dc) in cells:
                continue
            characters=[];rr,cc=r,c
            while (rr,cc) in cells:
                characters.append(cells[rr,cc]["letter"])
                rr+=dr;cc+=dc
            if len(characters)>=2:
                runs[direction,r,c]="".join(characters)
    if runs != registered:
        raise Problem("UNREGISTERED_RUN", "实际网格有额外、黏连、缺失或拼写不符的最大连续条目")
    crossing=[];graph=defaultdict(set)
    for cell in cells.values():
        if len(cell["owners"])==2:
            a,b=sorted(cell["owners"])
            crossing.append((a[0],a[1],b[0],b[1]))
            graph[a[0]].add(b[0]);graph[b[0]].add(a[0])
    reached=set();stack=[next(iter(words))]
    while stack:
        item=stack.pop()
        if item not in reached:
            reached.add(item);stack.extend(graph[item]-reached)
    if reached!=set(words):
        raise Problem("DISCONNECTED", "全部答案未通过真实交叉连成整体")
    starts=sorted({(p.row,p.col) for p in layout.placements})
    numbers_by_start={cell:i+1 for i,cell in enumerate(starts)}
    numbers={p.word_id:numbers_by_start[p.row,p.col] for p in layout.placements}
    if expected_numbers is not None and numbers!=expected_numbers:
        raise Problem("INCORRECT_NUMBERING", "题号与真实起始格顺序不一致")
    return {"status":"VERIFIED","words":len(words),"occupied_cells":len(cells),
            "cells":cells,"numbers":numbers,"starts":numbers_by_start,
            "crossings":tuple(sorted(crossing)),"geometry":canonical_geometry(cells),
            "across":sum(p.direction=="across" for p in layout.placements),
            "down":sum(p.direction=="down" for p in layout.placements)}


def validate_pair(lesson,first,second):
    a=validate_layout(lesson,first);b=validate_layout(lesson,second)
    if a["crossings"]==b["crossings"]:
        raise Problem("SAME_CROSSINGS", "两份布局的真实交叉关系相同")
    if a["geometry"]==b["geometry"]:
        raise Problem("GEOMETRIC_COPY", "第二份仅为平移、旋转或镜像副本")
    return {"status":"VERIFIED","different_crossings":True,"different_geometry":True,
            "first_crossings":len(a["crossings"]),"second_crossings":len(b["crossings"]),
            "removed":len(set(a["crossings"])-set(b["crossings"])),
            "added":len(set(b["crossings"])-set(a["crossings"]))}


def clue_rows(lesson,layout,frozen):
    checked=validate_layout(lesson,layout)
    candidates=selected_candidates(lesson,frozen)
    return sorted([{"word_id":p.word_id,"number":checked["numbers"][p.word_id],
        "direction":p.direction,"relation":candidates[p.word_id].relation,"text":candidates[p.word_id].text}
        for p in layout.placements],key=lambda x:(x["direction"],x["number"]))


def validate_clue_rows(lesson,layout,frozen,rows):
    expected=clue_rows(lesson,layout,frozen)
    if sorted(rows,key=lambda x:(x["direction"],x["number"]))!=expected:
        raise Problem("CLUE_MISMATCH", "线索编号、方向、文本或原始标签不符")
    return {"status":"VERIFIED","count":len(rows)}


def public_structure_summary(result):
    return {k:v for k,v in result.items() if k in {"status","words","occupied_cells","across","down"}} | {
        "crossings":len(result["crossings"]),"start_numbers":len(result["starts"])}
