"""Optional bounded lexicographic improvement, always retaining the feasible pair."""
import time
from .domain import Layout,Placement,Problem
from .execution import checkpoint
from .validation import validate_layout,validate_pair


def score(lesson,layout):
    result=validate_layout(lesson,layout);cells=result['cells']
    lo_r=min(r for r,c in cells);hi_r=max(r for r,c in cells)
    lo_c=min(c for r,c in cells);hi_c=max(c for r,c in cells)
    return (len(result['crossings']),-(hi_r-lo_r+1)*(hi_c-lo_c+1),
            -(abs(lo_r+hi_r-(layout.size-1))+abs(lo_c+hi_c-(layout.size-1))))


def centered(lesson,layout):
    cells=validate_layout(lesson,layout)['cells'];rs=[r for r,c in cells];cs=[c for r,c in cells]
    dr=(layout.size-1-max(rs)-min(rs))//2;dc=(layout.size-1-max(cs)-min(cs))//2
    return layout.model_copy(update={'placements':tuple(p.model_copy(update={'row':p.row+dr,'col':p.col+dc}) for p in layout.placements)})


def accept(lesson,baseline,candidate,other,typography):
    validate_layout(lesson,candidate);validate_pair(lesson,candidate,other)
    typography(candidate)
    return score(lesson,candidate)>=score(lesson,baseline)


def optimize_pair(lesson,layouts,options,typography,*,cancel=None):
    before=[score(lesson,l) for l in layouts];best=list(layouts);seconds=options.optimization_seconds
    report={'status':'NOT_RUN','before':before,'after':before,'seconds':0,'proof':'NONE'}
    if seconds==0:return tuple(best),report
    start=time.perf_counter();deadline=start+seconds;failures=[]
    def budget_check():
        checkpoint()
        if cancel and cancel():raise Problem('CANCELLED','任务取消')
        if time.perf_counter()>=deadline:raise Problem('OPTIMIZATION_BUDGET','有界优化预算结束')
    try:
        # Centering preserves all crossings and area, so it improves only the last criterion.
        for index in (0,1):
            budget_check();candidate=centered(lesson,best[index])
            if accept(lesson,best[index],candidate,best[1-index],typography):best[index]=candidate
        from .solver import ExactModel
        for index in (0,1):
            budget_check()
            model=ExactModel(lesson,budget_check=budget_check)
            other=best[1-index]
            model.exclude_fingerprint(validate_layout(lesson,other)['crossings']);model.exclude_geometry(other)
            current=score(lesson,best[index]);cross=sum(model.crossings.values())
            model.model.add(cross>=current[0]);model.model.maximize(cross);model.hint(best[index])
            budget_check()
            candidates=[]
            candidate,metrics=model.solve(options.model_copy(update={'seconds_per_layout':deadline-time.perf_counter()}),cancel)
            candidates.append(candidate)
            # Explicit lexicographic phases, never a giant weighted sum. A bounded
            # feasible primary value is frozen only for this local improvement pass.
            for phase in ('compactness','centering'):
                for candidate in candidates:
                    candidate=centered(lesson,candidate)
                    try:
                        if accept(lesson,best[index],candidate,other,typography):best[index]=candidate
                        else:failures.append('DEGRADED_REJECTED')
                    except Problem as exc:
                        if exc.code not in {'CLUE_OVERFLOW','CLUE_TOO_WIDE','GEOMETRIC_COPY','SAME_FINGERPRINT'}:raise
                        failures.append(exc.code)
                candidates=[]
                budget_check()
                m=model.model;score_now=score(lesson,best[index]);m.add(cross==score_now[0])
                if phase=='compactness':
                    rs=[];cs=[];re=[];ce=[]
                    for word in lesson.words:
                        r,c,d=model.positions[word.word_id];length=len(word.letters)-1
                        rs.append(r);cs.append(c);re.append(r+length*d);ce.append(c+length*(1-d))
                    lr=m.new_int_var(0,19,'min_row');lc=m.new_int_var(0,19,'min_col')
                    hr=m.new_int_var(0,19,'max_row');hc=m.new_int_var(0,19,'max_col')
                    m.add_min_equality(lr,rs);m.add_min_equality(lc,cs);m.add_max_equality(hr,re);m.add_max_equality(hc,ce)
                    height=m.new_int_var(1,20,'height');width=m.new_int_var(1,20,'width')
                    m.add(height==hr-lr+1);m.add(width==hc-lc+1)
                    area=m.new_int_var(1,400,'area');m.add_multiplication_equality(area,[height,width]);m.minimize(area)
                else:
                    m.add(area<=-score_now[1]);dy=m.new_int_var(0,38,'center_y');dx=m.new_int_var(0,38,'center_x')
                    m.add_abs_equality(dy,lr+hr-19);m.add_abs_equality(dx,lc+hc-19);m.minimize(dx+dy)
                model.hint(best[index]);budget_check()
                candidate,metrics=model.solve(options.model_copy(update={'seconds_per_layout':deadline-time.perf_counter()}),cancel)
                candidates.append(candidate)
            for candidate in candidates:
                candidate=centered(lesson,candidate)
                if accept(lesson,best[index],candidate,other,typography):best[index]=candidate
    except Problem as exc:
        if exc.code=='CANCELLED':raise
        if exc.code not in {'OPTIMIZATION_BUDGET','UNKNOWN'}:failures.append('INTERNAL_FALLBACK:'+exc.code)
    except Exception as exc:
        failures.append('INTERNAL_FALLBACK:'+type(exc).__name__)
    checkpoint()
    after=[score(lesson,l) for l in best]
    report.update({'status':'IMPROVED_NOT_PROVEN' if after!=before else 'BOUNDED_NO_IMPROVEMENT',
        'after':after,'seconds':time.perf_counter()-start,'fallback_reasons':failures})
    validate_pair(lesson,*best)
    return tuple(best),report
