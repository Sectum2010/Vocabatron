"""Complete placement-Boolean CP-SAT model with exact intersection channels."""
from __future__ import annotations

from collections import defaultdict
import random
import resource
import threading
import time

from ortools.sat.python import cp_model

from .domain import Layout, Placement, Problem, SolverOptions, check_input
from .validation import validate_layout, validate_pair
from .execution import checkpoint, CURRENT


def static_graph(lesson):
    graph={w.word_id:set() for w in lesson.words}
    for i,a in enumerate(lesson.words):
        for b in lesson.words[i+1:]:
            if set(a.letters)&set(b.letters):
                graph[a.word_id].add(b.word_id);graph[b.word_id].add(a.word_id)
    reached=set();stack=[lesson.words[0].word_id]
    while stack:
        node=stack.pop()
        if node not in reached:
            reached.add(node);stack.extend(graph[node]-reached)
    if len(reached)!=len(graph):
        raise Problem("STATIC_GRAPH_DISCONNECTED", "共享字母图不连通，无法构成所需整体")
    return graph


def _hint_legal(placed, candidate, words, size):
    """Heuristic only. Its domain never restricts the exact solver."""
    board={};edges=set()
    for p in [*placed,candidate]:
        dr,dc=(0,1) if p.direction=="across" else (1,0)
        sequence=[]
        for k,ch in enumerate(words[p.word_id].letters):
            cell=p.row+dr*k,p.col+dc*k
            if not (0<=cell[0]<size and 0<=cell[1]<size):
                return False
            if cell in board:
                old,ds=board[cell]
                if old!=ch or p.direction in ds:
                    return False
                ds.add(p.direction)
            else:
                board[cell]=(ch,{p.direction})
            if sequence:
                edges.add((sequence[-1],cell))
            sequence.append(cell)
    for r,c in board:
        for nxt in ((r+1,c),(r,c+1)):
            if nxt in board and ((r,c),nxt) not in edges:
                return False
    return True


def warm_hint(lesson,size,seed,seconds=8,exclude=None):
    start=time.perf_counter();rng=random.Random(seed)
    attempt_deadline=start+seconds
    words={w.word_id:w for w in lesson.words}
    root=max(lesson.words,key=lambda w:len(w.letters))
    best=[]
    def visit(placed,remaining):
        nonlocal best
        checkpoint()
        if len(placed)>len(best):best=placed[:]
        if time.perf_counter()>attempt_deadline:return None
        if not remaining:
            layout=Layout(lesson_version=lesson.version,placements=tuple(placed),size=size)
            try:
                validate_layout(lesson,layout)
                if exclude:validate_pair(lesson,exclude,layout)
                return layout
            except Problem:
                return None
        options=[]
        for wid in remaining:
            proposals=set()
            for p in placed:
                old=words[p.word_id]
                for i,a in enumerate(old.letters):
                    for j,b in enumerate(words[wid].letters):
                        if a!=b:continue
                        if p.direction=="across":
                            proposals.add((p.row-j,p.col+i,"down"))
                        else:
                            proposals.add((p.row+i,p.col-j,"across"))
            valid=[]
            for r,c,d in sorted(proposals):
                p=Placement(word_id=wid,row=r,col=c,direction=d)
                if _hint_legal(placed,p,words,size):valid.append(p)
            if valid:options.append((wid,valid))
        # An unplaceable word may become placeable after a bridge. Never reject it globally.
        options.sort(key=lambda item:(len(item[1]),-len(words[item[0]].letters),item[0]))
        for wid,ps in options:
            rng.shuffle(ps)
            for p in ps:
                found=visit(placed+[p],remaining-{wid})
                if found:return found
                if time.perf_counter()>attempt_deadline:return None
        return None
    # A centered longest word is ONLY a hint seed, never a hard constraint.
    first=Placement(word_id=root.word_id,row=size//2,col=(size-len(root.letters))//2,direction="across")
    found=None;attempts=0
    # Bounded restarts avoid spending the entire hint budget in one unlucky order.
    # Every restart still feeds the same complete exact model; no domain is removed.
    for attempt in range(4):
        attempts+=1;rng=random.Random(seed+attempt)
        attempt_deadline=min(start+seconds,time.perf_counter()+seconds/4)
        found=visit([first],set(words)-{root.word_id})
        if found or time.perf_counter()>=start+seconds:break
    # A locally valid partial assignment is useful to CP-SAT too. It is never
    # published or treated as a solution; all original constraints remain active.
    partial=Layout(lesson_version=lesson.version,placements=tuple(best),size=size) if best else None
    return found or partial,{"seconds":time.perf_counter()-start,"most_words_placed":len(best),
                            "bounded_restarts":attempts,"hint_complete":found is not None}


class ExactModel:
    def __init__(self,lesson,size=20,clue_costs=None,capacities=None,budget_check=None):
        check_input(lesson,size);static_graph(lesson)
        started=time.perf_counter()
        self.lesson,self.size=lesson,size
        m=self.model=cp_model.CpModel()
        by_cell=defaultdict(list);by_edge=defaultdict(list)
        self.placements={};self.positions={};self.crossings={};self.edges={}
        self.lookup={}
        for wi,word in enumerate(lesson.words):
            checkpoint()
            if budget_check:budget_check()
            ps=[]
            for d in (0,1):
                for r in range(size if not d else size-len(word.letters)+1):
                    for c in range(size-len(word.letters)+1 if not d else size):
                        x=m.new_bool_var(f"p{wi}_{r}_{c}_{d}")
                        p=Placement(word_id=word.word_id,row=r,col=c,direction="down" if d else "across")
                        ps.append((x,p));self.lookup[word.word_id,r,c,d]=x
                        for k,ch in enumerate(word.letters):
                            rr,cc=r+k*d,c+k*(1-d)
                            by_cell[rr,cc,d].append((x,ord(ch)-64))
                            if k<len(word.letters)-1:by_edge[rr,cc,d].append(x)
            m.add_exactly_one(x for x,p in ps)
            row=m.new_int_var(0,size-1,f"r{wi}");col=m.new_int_var(0,size-1,f"c{wi}");down=m.new_bool_var(f"d{wi}")
            m.add(row==sum(x*p.row for x,p in ps));m.add(col==sum(x*p.col for x,p in ps))
            m.add(down==sum(x for x,p in ps if p.direction=="down"))
            self.positions[word.word_id]=row,col,down
            self.placements[word.word_id]=ps
        occupied={}
        for r in range(size):
            for c in range(size):
                ds=[];letters=[]
                for d in (0,1):
                    occ=m.new_bool_var(f"o{r}_{c}_{d}")
                    m.add(occ==sum(x for x,ch in by_cell[r,c,d]))
                    letter=m.new_int_var(0,26,f"l{r}_{c}_{d}")
                    m.add(letter==sum(x*ch for x,ch in by_cell[r,c,d]))
                    ds.append(occ);letters.append(letter)
                m.add(letters[0]==letters[1]).only_enforce_if(ds)
                occ=m.new_bool_var(f"occupied{r}_{c}")
                m.add_max_equality(occ,ds);occupied[r,c]=occ
        for r in range(size):
            for c in range(size):
                for d in (0,1):
                    nxt=r+d,c+1-d
                    if nxt not in occupied:continue
                    # Both occupied endpoints require a registered answer across this edge.
                    m.add(occupied[r,c]+occupied[nxt]-1<=sum(by_edge[r,c,d]))
        for i,a in enumerate(lesson.words):
            checkpoint()
            if budget_check:budget_check()
            ra,ca,da=self.positions[a.word_id]
            for b in lesson.words[i+1:]:
                matches=[(ia,ib) for ia,ch in enumerate(a.letters) for ib,c in enumerate(b.letters) if c==ch]
                if not matches:continue
                rb,cb,db=self.positions[b.word_id]
                different=m.new_bool_var("different_direction")
                m.add(da!=db).only_enforce_if(different);m.add(da==db).only_enforce_if(different.Not())
                xs=[]
                for ia,ib in matches:
                    equal_r=m.new_bool_var("equal_row");equal_c=m.new_bool_var("equal_col")
                    er=ra+ia*da-rb-ib*db;ec=ca+ia*(1-da)-cb-ib*(1-db)
                    m.add(er==0).only_enforce_if(equal_r);m.add(er!=0).only_enforce_if(equal_r.Not())
                    m.add(ec==0).only_enforce_if(equal_c);m.add(ec!=0).only_enforce_if(equal_c.Not())
                    x=m.new_bool_var("crossing")
                    m.add_bool_and([different,equal_r,equal_c]).only_enforce_if(x)
                    m.add_bool_or([different.Not(),equal_r.Not(),equal_c.Not(),x])
                    key=(a.word_id,ia,b.word_id,ib) if a.word_id<b.word_id else (b.word_id,ib,a.word_id,ia)
                    self.crossings[key]=x;xs.append(x)
                edge=m.new_bool_var("edge");m.add(edge==sum(xs))
                self.edges[a.word_id,b.word_id]=edge
        n=len(lesson.words)
        balances=defaultdict(list)
        for (a,b),e in self.edges.items():
            ab=m.new_int_var(0,n-1,"flow");ba=m.new_int_var(0,n-1,"flow")
            m.add(ab<=(n-1)*e);m.add(ba<=(n-1)*e)
            balances[a].extend([ab,-ba]);balances[b].extend([ba,-ab])
        for i,w in enumerate(lesson.words):m.add(sum(balances[w.word_id])==(n-1 if i==0 else -1))
        if clue_costs and capacities:
            for d in (0,1):
                m.add(sum(clue_costs[w.word_id][d]*(self.positions[w.word_id][2] if d else (1-self.positions[w.word_id][2])) for w in lesson.words)<=capacities[d])
        self.metrics={"model_seconds":time.perf_counter()-started,
                      "placements":sum(len(x) for x in self.placements.values()),
                      "variables":len(m.proto.variables),"constraints":len(m.proto.constraints),
                      "peak_rss_kib":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}

    def hint(self,layout):
        self.model.clear_hints()
        if layout is None:return
        chosen={(p.word_id,p.row,p.col,int(p.direction=="down")) for p in layout.placements}
        supplied={p.word_id for p in layout.placements}
        for key,x in self.lookup.items():
            if key[0] in supplied:self.model.add_hint(x,int(key in chosen))

    def exclude_layout(self,layout):
        variables=[self.lookup[p.word_id,p.row,p.col,int(p.direction=="down")] for p in layout.placements]
        self.model.add(sum(variables)<=len(variables)-1)

    def exclude_fingerprint(self,crossings):
        present=set(crossings)
        self.model.add_bool_or([x.Not() if key in present else x for key,x in self.crossings.items()])

    def exclude_geometry(self,layout):
        """Forbid only concrete legal forward-spelled dihedral translations."""
        words={w.word_id:w for w in self.lesson.words}
        for swap in (False,True):
            for sr in (-1,1):
                for sc in (-1,1):
                    transformed=[];valid=True
                    for p in layout.placements:
                        coords=[]
                        for k,ch in enumerate(words[p.word_id].letters):
                            r=p.row+k*(p.direction=="down");c=p.col+k*(p.direction=="across")
                            a,b=(c,r) if swap else (r,c)
                            coords.append((sr*a,sc*b,ch))
                        coords.sort()
                        if "".join(x[2] for x in coords)!=words[p.word_id].letters:valid=False;break
                        d=int(coords[0][0]!=coords[-1][0])
                        transformed.append((p.word_id,coords[0][0],coords[0][1],d,len(coords)))
                    if not valid:continue
                    lowr=min(p[1] for p in transformed);lowc=min(p[2] for p in transformed)
                    highr=max(p[1]+(p[4]-1)*p[3] for p in transformed)
                    highc=max(p[2]+(p[4]-1)*(1-p[3]) for p in transformed)
                    for dr in range(-lowr,self.size-highr):
                        for dc in range(-lowc,self.size-highc):
                            variables=[self.lookup[w,r+dr,c+dc,d] for w,r,c,d,length in transformed]
                            self.model.add(sum(variables)<=len(variables)-1)

    def solve(self,options: SolverOptions,cancel=None):
        checkpoint()
        if cancel and cancel():raise Problem("CANCELLED","精确求解已取消")
        solver=cp_model.CpSolver();solver.parameters.num_search_workers=options.workers
        solver.parameters.random_seed=options.seed;solver.parameters.max_time_in_seconds=options.seconds_per_layout
        solver.parameters.repair_hint=True
        completed=threading.Event()
        context=CURRENT.get()
        def watch():
            while not completed.wait(.25):
                if (cancel and cancel()) or (context and (context.budget_exhausted() or context.heartbeat_error)):solver.stop_search();return
        if cancel or context:threading.Thread(target=watch,daemon=True).start()
        start=time.perf_counter()
        try:status=solver.solve(self.model)
        finally:completed.set()
        checkpoint()
        if cancel and cancel():raise Problem("CANCELLED","精确求解已取消")
        name=solver.status_name(status)
        metrics={"status":name,"seconds":time.perf_counter()-start,"workers":options.workers,
                 "hint_repair":True,
                 "seed":options.seed,"branches":solver.num_branches,"conflicts":solver.num_conflicts,
                 "peak_rss_kib":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
        if status not in (cp_model.FEASIBLE,cp_model.OPTIMAL):
            raise Problem("CANCELLED" if cancel and cancel() else name,"精确求解尚未返回可发布布局",details=metrics)
        layout=Layout(lesson_version=self.lesson.version,size=self.size,
                      placements=tuple(p for ps in self.placements.values() for x,p in ps if solver.boolean_value(x)))
        validate_layout(self.lesson,layout)
        return layout,metrics


def solve_pair(lesson,options=SolverOptions(),*,size=20,clue_costs=None,capacities=None,progress=None,cancel=None,saved_first=None,typography=None):
    emit=progress or (lambda stage,value:None)
    emit("MODELING",{})
    model=ExactModel(lesson,size,clue_costs,capacities)
    metrics={"model":model.metrics,"optimization":{"status":"NOT_RUN","reason":"feasibility first"}}
    first=saved_first
    if first is None:
        emit("HINTING_FIRST",{})
        hint,hm=warm_hint(lesson,size,options.seed);metrics["first_hint"]=hm;model.hint(hint)
        emit("SOLVING_FIRST",metrics)
        deadline=time.perf_counter()+options.seconds_per_layout
        rejected=0
        while True:
            checkpoint()
            remaining=deadline-time.perf_counter()
            if remaining<=0:raise Problem("UNKNOWN","第一份布局排版搜索预算耗尽")
            first,metrics["first"]=model.solve(options.model_copy(update={"seconds_per_layout":remaining}),cancel)
            try:
                if typography:typography(first)
                break
            except Problem as exc:
                if exc.code not in {"CLUE_OVERFLOW","CLUE_TOO_WIDE"}:raise
                model.exclude_layout(first);rejected+=1;model.hint(None)
        metrics["first_typography_rejected"]=rejected
        emit("FIRST_VERIFIED",first.model_dump(mode="json"))
    else:
        validate_layout(lesson,first);metrics["first"]={"status":"RESUMED_VERIFIED"}
    model.exclude_fingerprint(validate_layout(lesson,first)["crossings"])
    emit("HINTING_SECOND",{})
    hint,hm=warm_hint(lesson,size,options.seed+1,exclude=first);metrics["second_hint"]=hm;model.hint(hint)
    emit("SOLVING_SECOND",metrics)
    deadline=time.perf_counter()+options.seconds_per_layout;rejected=0
    while True:
        remaining=deadline-time.perf_counter()
        if remaining<=0:raise Problem("UNKNOWN","第二份预算耗尽，未标记双份完成",details=metrics)
        second,sm=model.solve(options.model_copy(update={"seconds_per_layout":remaining}),cancel)
        try:
            pair=validate_pair(lesson,first,second)
            if typography:typography(second)
            break
        except Problem as e:
            if e.code=="GEOMETRIC_COPY":model.exclude_geometry(second)
            elif e.code in {"CLUE_OVERFLOW","CLUE_TOO_WIDE"}:model.exclude_layout(second)
            else:raise
            rejected+=1;model.hint(None)
    metrics["second"]=sm;metrics["geometric_copies_rejected"]=rejected
    emit("SECOND_VERIFIED",second.model_dump(mode="json"))
    return (first,second),metrics,pair
