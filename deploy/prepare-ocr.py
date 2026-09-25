"""Explicit setup only. The production importer never imports this module.

Run under dev_runner. Download destinations must be inside the project .private
directory. Existing verified models are reused; no system installation occurs.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import urllib.request

PROJECT=Path(__file__).resolve().parents[1]
PACKAGES={'tesseract-ocr':'5.3.4-1build5','libtesseract5':'5.3.4-1build5',
          'liblept5':'1.82.0-3build4','tesseract-ocr-eng':'1:4.1.0-2','tesseract-ocr-osd':'1:4.1.0-2'}


def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def acquire(url,path,expected,size):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if path.exists():
        if path.is_symlink() or sha(path)!=expected:raise RuntimeError('Existing asset failed integrity verification: '+str(path))
        return
    temporary=path.with_suffix(path.suffix+'.partial')
    try:
        with urllib.request.urlopen(url,timeout=30) as incoming,temporary.open('xb') as out:
            copied=0
            while chunk:=incoming.read(1024*1024):
                copied+=len(chunk)
                if copied>size:raise RuntimeError('Download exceeded its pinned size')
                out.write(chunk)
            out.flush();os.fsync(out.fileno())
        if copied!=size or sha(temporary)!=expected:raise RuntimeError('Upstream asset integrity mismatch')
        temporary.rename(path)
    finally:
        if temporary.exists():temporary.unlink()


def tesseract(root):
    manifest=root/'tesseract-manifest.json';destination=root/'tesseract'
    if manifest.is_file():
        for record in json.loads(manifest.read_text())['weights']:
            if sha(Path(record['path']))!=record['sha256']:raise RuntimeError('Existing OCR language file changed')
        return
    if destination.exists():raise RuntimeError('Unverified existing installation; inspect it before setup')
    release=platform.freedesktop_os_release()
    if platform.machine()!='aarch64' or release.get('ID')!='ubuntu' or release.get('VERSION_CODENAME')!='noble':
        raise RuntimeError('This extracted executable bundle is verified only for Ubuntu Noble aarch64')
    records=[]
    with tempfile.TemporaryDirectory(prefix='tesseract-setup-',dir=root) as temporary:
        staging=Path(temporary)/'runtime';staging.mkdir()
        for package,version in PACKAGES.items():
            output=subprocess.check_output(['apt-cache','show',package+'='+version],text=True)
            fields={line.split(': ',1)[0]:line.split(': ',1)[1] for line in output.split('\n\n')[0].splitlines() if ': ' in line and not line.startswith(' ')}
            if fields.get('Architecture') not in ('arm64','all') or not fields.get('Filename','').startswith('pool/'):
                raise RuntimeError('Unexpected signed APT package metadata')
            url='https://ports.ubuntu.com/ubuntu-ports/'+fields['Filename']
            archive=root/'tesseract-debs'/Path(fields['Filename']).name
            acquire(url,archive,fields['SHA256'],int(fields['Size']))
            subprocess.run(['dpkg-deb','-x',str(archive),str(staging)],check=True)
            records.append({'package':package,'version':version,'source':url,'sha256':fields['SHA256'],
                'bytes':int(fields['Size']),'path':str(archive),'metadata':output})
        staging.rename(destination)
    weights=[]
    for lang in ('eng','osd'):
        path=destination/'usr/share/tesseract-ocr/5/tessdata'/(lang+'.traineddata')
        weights.append({'name':path.name,'sha256':sha(path),'bytes':path.stat().st_size,'path':str(path),'license':'Apache-2.0; see packaged copyright'})
    manifest.write_text(json.dumps({'source':'Ubuntu Noble signed APT metadata','installation':'project-local extraction',
        'records':records,'weights':weights},indent=2))


def structured(root):
    from importlib.metadata import version
    for package,expected in {'docling-slim':'2.130.0','rapidocr':'3.9.2','torch':'2.14.0+cpu','onnxruntime':'1.30.0'}.items():
        if version(package)!=expected:raise RuntimeError('Run the frozen project dependency sync before model setup')
    registry=json.loads((PROJECT/'deploy/ocr-models.json').read_text());records=[]
    for record in registry['models']:
        path=root/record['relative_path']
        if path.resolve()!=path or not path.is_relative_to(root):raise RuntimeError('Unsafe model destination')
        acquire(record['source'],path,record['sha256'],record['bytes'])
        records.append({**{k:v for k,v in record.items() if k!='relative_path'},'path':str(path)})
    (root/'docling-manifest.json').write_text(json.dumps({'framework':'docling-slim==2.130.0',
        'engine':'CPU-only torch + onnxruntime','records':records},indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=PROJECT/'.private/web/ocr')
    parser.add_argument('--acquire',action='store_true',help='Explicitly allow pinned dependency/model acquisition')
    args=parser.parse_args();root=args.root.absolute()
    if root.resolve()!=root or not root.is_relative_to(PROJECT/'.private'):raise ValueError('Use a project-private model directory without symlinks')
    if not args.acquire:
        print(json.dumps({'architecture':platform.machine(),'tesseract':shutil.which('tesseract'),
            'pdftotext':shutil.which('pdftotext'),'pdftoppm':shutil.which('pdftoppm'),'ghostscript':shutil.which('gs'),
            'private_root':str(root),'prepared_manifests':[p.name for p in root.glob('*manifest.json')]}));return
    from vocabatron.app.resources import idle_priority
    idle_priority();os.umask(0o077);root.mkdir(parents=True,exist_ok=True,mode=0o700)
    tesseract(root);structured(root)
    print(json.dumps({'prepared':True,'private_root':str(root),'network_allowed_during_setup_only':True}))


if __name__=='__main__':main()
