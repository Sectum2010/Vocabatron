"""Owned HTTPS browser fixture using invented documents and real application routes.

Run only under the development resource guard. This does not install a service,
change Tailscale, or expose a test identity in the production application.
"""
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import uuid
from vocabatron.app.api import create_app
from vocabatron.app.config import AppConfig
from vocabatron.app.database import encode
from vocabatron.app.library import Library
from vocabatron.app.jobs import Job
from vocabatron.app.resources import idle_priority
from vocabatron.clues import select
from vocabatron.storage import PrivateStore
from .helpers import synthetic_course,synthetic_template
from .test_hardening_protocols import transport
from .test_web_generation import run_slices


def main():
    import uvicorn
    idle_priority();project=Path.cwd().resolve();parent=project/'.cache/browser-fixtures';parent.mkdir(parents=True,exist_ok=True)
    root=Path(tempfile.mkdtemp(prefix='invented-',dir=parent));data=root/'private';template=root/'template.pdf'
    synthetic_template(template);source=root/'Invented lesson.pdf';synthetic_course(source,['AB','AC'],3)
    config=AppConfig(code_root=project,data_root=data,runtime_root=data/'runtime',database=data/'database.sqlite3',
        outputs_root=root/'Outputs',static_root=project/'.cache/frontend-dist',legacy_root=root/'legacy',template=template,
        public_base_url='https://127.0.0.1:18766/vocabatron/',listen_port=18766,
        allowed_logins=('owner@example.test','other@example.test'),csrf_secret=secrets.token_hex(32))
    config.activate();library=Library(config);library.initialize();config.outputs_root.mkdir()
    task=library.source(source,source.name)['task'];assert run_slices(library,task['id'])=='COMPLETED'
    unresolved=root/'Unresolved invented.pdf';synthetic_course(unresolved,['AB','AB'],4)
    notice=library.source(unresolved,unresolved.name)['task'];assert run_slices(library,notice['id'])=='NEEDS_ATTENTION'
    row=library.db.one('SELECT * FROM lessons');lid=row['id'];row,lesson=library.lesson(lid)
    adapter,_=transport();frozen=select(lesson,adapter,PrivateStore(data/'core'))
    reference={'object':library.objects.put_json(frozen.model_dump(mode='json')),'lesson_object':row['lesson_json']}
    with library.db.transaction() as c:c.execute('UPDATE lessons SET frozen_ref=? WHERE id=?',(encode(reference),lid))
    request=library.submit({'idempotency_key':uuid.uuid4().hex,'targets':[{'lesson_id':lid,'mode':'count','count':1}]})
    assert run_slices(library,request['tasks'][0]['id'])=='COMPLETED'
    # Show real persisted waiting state without running a model or background solver.
    waiting=library.submit({'idempotency_key':uuid.uuid4().hex,'targets':[{'lesson_id':lid,'mode':'count','count':3}]})['tasks'][0]['id']
    with library.db.transaction() as c:
        c.execute("UPDATE tasks SET status='WAITING_FOR_RESOURCES',stage='Waiting for system resources',detail=? WHERE id=?",
                  (encode({'reason':'Other applications have priority. Saved results remain available.'}),waiting))
    key=root/'key.pem';cert=root/'cert.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),
        '-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    key.chmod(0o600)
    # The fixture path is private test output, never part of a public package.
    (parent/'current.json').write_text(json.dumps({'root':str(root),'source':str(source)}))
    application=create_app(config)
    async def fixture_proxy(scope,receive,send):
        # Browser-owned service worker requests do not inherit Playwright's
        # custom context headers. Simulate the authenticated proxy here, in the
        # isolated fixture only, so static requests have the same identity path.
        if scope['type']=='http' and not any(k.lower()==b'tailscale-user-login' for k,v in scope['headers']):
            scope={**scope,'headers':[*scope['headers'],(b'tailscale-user-login',b'owner@example.test')]}
        await application(scope,receive,send)
    uvicorn.run(fixture_proxy,host='127.0.0.1',port=18766,ssl_keyfile=str(key),ssl_certfile=str(cert),
                access_log=False,proxy_headers=False,limit_concurrency=64)


if __name__=='__main__':main()
