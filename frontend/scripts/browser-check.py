"""Owned synthetic TLS fixture and browser tests; run from the project root under dev_runner.

On Ubuntu ARM64, missing WebKit libraries are downloaded and extracted into the
project cache. No packages are installed and no system configuration is changed.
"""
import os,sys,subprocess,time,urllib.request,ssl,signal,json,shlex
from pathlib import Path
root=Path.cwd();logs=root/'.cache/browser-checks';deps=root/'.cache/browser-deps';deps.mkdir(parents=True,exist_ok=True);logs.mkdir(parents=True,exist_ok=True)
for package,library in [('libavif16','libavif.so.16'),('libgav1-1','libgav1.so.1'),('libyuv0','libyuv.so.0')]:
 if not (deps/'usr/lib/aarch64-linux-gnu'/library).exists():
  subprocess.run(['apt','download',package],cwd=deps,check=True)
  packages=list(deps.glob(package+'_*.deb'));assert len(packages)==1
  subprocess.run(['dpkg-deb','-x',str(packages[0]),str(deps)],check=True)
env={**os.environ,'LD_LIBRARY_PATH':str(deps/'usr/lib/aarch64-linux-gnu'),
     'LIBGL_ALWAYS_SOFTWARE':'1','GALLIUM_DRIVER':'llvmpipe','WEBKIT_DISABLE_COMPOSITING_MODE':'1'}
bundle=Path(subprocess.check_output(['node','--input-type=module','-e',"import {webkit} from 'playwright';process.stdout.write(webkit.executablePath());"],cwd=root/'frontend',text=True)).parent/'minibrowser-wpe'
launcher=deps/'webkit-launcher.sh'
launcher.write_text("#!/bin/sh\n"+"\n".join('export '+key+'='+shlex.quote(str(value)) for key,value in {
 'WEBKIT_EXEC_PATH':bundle/'bin','WEBKIT_INJECTED_BUNDLE_PATH':bundle/'lib',
 'WEBKIT_INSPECTOR_RESOURCES_PATH':bundle/'share','WEBKIT_FORCE_COMPLEX_TEXT':'1',
 'LD_LIBRARY_PATH':str(bundle/'lib')+':'+str(bundle/'sys/lib')+':'+str(deps/'usr/lib/aarch64-linux-gnu')
}.items())+'\nexec '+shlex.quote(str(bundle/'bin/MiniBrowser'))+' "$@"\n')
launcher.chmod(0o700);env['VOCABATRON_TEST_WEBKIT_LAUNCHER']=str(launcher)
# Playwright validates shared libraries before launching; it does not install or
# change any system package. Only this subprocess tree receives the search path.
server_log=open(logs/'browser-fixture.log','wb')
server=subprocess.Popen(['.venv/bin/python','-m','tests.web_fixture'],cwd=root,env=env,stdout=server_log,stderr=subprocess.STDOUT)
try:
 context=ssl._create_unverified_context();ready=False
 for _ in range(120):
  if server.poll() is not None:raise RuntimeError('Synthetic fixture exited before readiness')
  try:
   req=urllib.request.Request('https://127.0.0.1:18766/vocabatron/api/session',headers={'Tailscale-User-Login':'owner@example.test'})
   with urllib.request.urlopen(req,context=context,timeout=1) as response:ready=response.status==200
   if ready:break
  except OSError:pass
  time.sleep(.5)
 if not ready:raise RuntimeError('Synthetic fixture did not become ready')
 with (logs/'browser-tests.log').open('wb') as log:
  code=subprocess.call(['npm','run','test:e2e'],cwd=root/'frontend',env=env,stdout=log,stderr=subprocess.STDOUT)
 sys.exit(code)
finally:
 server.terminate()
 try:server.wait(timeout=10)
 except subprocess.TimeoutExpired:server.kill();server.wait()
 server_log.close()
