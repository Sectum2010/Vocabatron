"""Production CLI shares the web database, queue, history and resource policy."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import time
import uuid
from ..domain import Problem
from .config import load_config
from .library import Library
from .database import task_record,insert_task,one,encode


def execute(args):
    configuration=args.app_config or os.environ.get('VOCABATRON_CONFIG') or str(Path.cwd()/'.private/web/config.json')
    config=load_config(configuration);library=Library(config);library.initialize()
    command=args.command
    if command=='library':return library.list_lessons()
    if command=='status':
        task=library.db.one('SELECT * FROM tasks WHERE id=?',(args.task_id,))
        if not task:
            from ..storage import identifier
            suffix='/results/'+identifier(args.task_id)+'/manifest.json'
            migrations=library.db.all('SELECT source,evidence FROM migrations')
            historical=[json.loads(m['evidence']) for m in migrations if m['source'].endswith(suffix)]
            if historical:return {'status':'MIGRATED_LEGACY_RESULT','records':historical}
            raise Problem('NOT_FOUND','Task not found in the persistent queue')
        return {k:v for k,v in task.items() if k not in ('input_json','owner','fence')}
    if command in ('pause','resume','cancel','retry'):return library.control(args.task_id,command)
    if command=='metrics':return {'resources':library.db.one('SELECT value FROM telemetry WHERE singleton=1'),'preferences':library.preferences()}
    if command=='hold':
        ident=uuid.uuid4().hex
        with library.db.transaction() as c:c.execute('INSERT INTO holds VALUES(?,?,0,?)',(ident,time.time()+args.seconds,time.time()))
        return {'hold_id':ident,'status':'REQUESTED','message':'Wait for hold-status acknowledged before starting external inference.'}
    if command=='hold-status':
        value=library.db.one('SELECT * FROM holds WHERE id=?',(args.hold_id,))
        if not value:raise Problem('NOT_FOUND','Hold not found')
        return {**value,'acknowledged':bool(value['acknowledged']) and value['until']>time.time()}
    if command=='release-hold':
        with library.db.transaction() as c:c.execute('UPDATE holds SET until=? WHERE id=?',(time.time(),args.hold_id))
        return {'released':True}
    if command in ('private-test','benchmark-model'):
        raise Problem('QUEUE_REQUIRED','Production computation must use the persistent queue. Isolated synthetic and explicit private acceptance use the guarded test harness.')
    if command=='ingest':
        if args.source:path=Path(args.source).absolute()
        else:
            from ..storage import PrivateStore
            from ..services import configuration as old_configuration
            store=PrivateStore(args.private_dir);old=old_configuration(store);path=store.path(old.source)
        return library.source(path,path.name)
    selected=getattr(args,'lesson_id',None)
    if command in ('rebuild','verify'):
        task=library.db.one('SELECT lesson_id FROM tasks WHERE id=?',(args.task_id,))
        if task:selected=task['lesson_id']
    if not selected:
        values=library.list_lessons()['items']
        if len(values)!=1:raise Problem('LESSON_REQUIRED','Choose a lesson with --lesson-id; use the library command to list IDs')
        selected=values[0]['id']
    row,_=library.lesson(selected)
    if command=='generate':
        if args.seconds is not None or args.workers is not None:raise Problem('RESOURCE_POLICY','Use shared settings within the deployment ceilings; individual requests cannot bypass the governor')
        from ..domain import digest
        return library.submit({'idempotency_key':digest({'cli_task':args.task_id}) if args.task_id else uuid.uuid4().hex,'targets':[{'lesson_id':selected,'mode':'all' if args.all else 'count','count':None if args.all else (args.count if args.count is not None else library.preferences()['default_count'])}]})
    kind={'select':'prepare','rebuild':'restore','restore':'restore','check-transcript':'import','verify':'verify'}.get(command)
    if not kind:raise Problem('INPUT_INVALID','Unknown command')
    if command=='select' and args.new_version:raise Problem('IMMUTABLE_SELECTION','Existing clue evidence is immutable. A new source content version is required.')
    if kind=='verify':
        artifacts=library.db.all("SELECT id FROM artifacts WHERE lesson_id=? AND state='AVAILABLE'",(selected,))
        inputs=[{'artifact_id':a['id']} for a in artifacts]
    elif kind=='import':inputs=[{'source_id':row['source_id']}]
    else:inputs=[{'lesson_content_id':selected,'lesson_json':row['lesson_json']}]
    tasks=[]
    with library.db.transaction() as c:
        for value in inputs:
            task=task_record(kind,value,lesson_id=selected,family_id=row['family_id'],priority=20);insert_task(c,task);tasks.append(task['id'])
            library.db.event(c,'task',{'id':task['id'],'status':'QUEUED'},task['id'])
    return {'status':'QUEUED','tasks':tasks}


def worker_main():
    from .scheduler import Scheduler
    path=os.environ.get('VOCABATRON_CONFIG')
    if not path:raise SystemExit('VOCABATRON_CONFIG is required')
    Scheduler(load_config(path),path).run()


if __name__=='__main__':worker_main()
