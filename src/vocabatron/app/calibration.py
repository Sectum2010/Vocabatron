"""Bounded synthetic CPU calibration; call through the idle development guard."""
from concurrent.futures import ProcessPoolExecutor
import json
import multiprocessing
import os
import resource
import time
from ..domain import Lesson,Word,SolverOptions,Candidate,Fragment
from ..solver import ExactModel


def trial(threads):
    answers=['ABCD','BAEF','GHIG','ABG','BAH','CEI','DFG']
    lesson=Lesson(lesson=99,source_sha256='synthetic-calibration',coverage_sha256='synthetic',title=(),words=tuple(
        Word(word_id=f'created-{i}',ordinal=str(i+1),raw=a,letters=a,part_of_speech='synthetic',candidates=(
            Candidate(candidate_id=f'clue-{i}',word_id=f'created-{i}',relation='SYNONYM',text='Invented clue',
                      source=Fragment(page=1,bbox=(0,0,1,1),raw='SYNONYM: Invented clue'),segment=0),)) for i,a in enumerate(answers)))
    started=time.perf_counter()
    try:
        model=ExactModel(lesson,20,check_graph=False)
        _,metrics=model.solve(SolverOptions(workers=threads,seconds_per_layout=3,optimization_seconds=0),None)
        status=metrics['status']
    except Exception as exc:status=getattr(exc,'code',type(exc).__name__)
    return {'seconds':time.perf_counter()-started,'threads':threads,'status':status,
            'peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}


def main():
    records=[]
    for slots,threads in ((1,1),(1,2),(2,1)):
        started=time.perf_counter()
        with ProcessPoolExecutor(max_workers=slots,mp_context=multiprocessing.get_context('spawn')) as pool:
            values=list(pool.map(trial,[threads]*4))
        elapsed=time.perf_counter()-started
        completed=sum(v['status'] in ('OPTIMAL','FEASIBLE') for v in values)
        records.append({'slots':slots,'threads_per_search':threads,'total_cpu_budget':slots*threads,
                        'tasks':values,'wall_seconds':elapsed,'attempts_per_second':4/elapsed,
                        'completed_tasks':completed,'completed_tasks_per_second':completed/elapsed,
                        'memory_scope':'per worker peak; pool processes reused within this configuration'})
    print(json.dumps({'synthetic_only':True,'comparison':records,'automatic_ceiling_increase':False},indent=2))


if __name__=='__main__':main()
