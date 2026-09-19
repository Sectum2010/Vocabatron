"""Separate WORKTREE, raw INDEX blobs and bounded ARCHIVE audits; no Git writes."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import re
import selectors
import socket
import stat
import struct
import subprocess
import tarfile
import time
import zipfile

from .domain import Problem

PATTERNS={
    'private_key':re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'github_token':re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})'),
    'cloud_access_key':re.compile(r'AKIA[A-Z0-9]{16}'),
    'api_secret':re.compile(r'\bsk-[A-Za-z0-9_-]{24,}'),
    'user_absolute_path':re.compile(r'/(?:home|Users)/[^/\s\x27\x22]+/'),
    'tailnet_address':re.compile(r'\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b'),
    'tailnet_domain':re.compile(r'\b[A-Za-z0-9.-]+\.ts\.net\b'),
}
MAX_FILE=4*1024*1024
MAX_ARCHIVE_FILES=2000
MAX_ARCHIVE_TOTAL=32*1024*1024


def allowed_name(name,*,package=False):
    p=PurePosixPath(name)
    if not p.parts or p.is_absolute() or '..' in p.parts or '\\' in name:return False
    if any(x.startswith('.private') or x in {'.venv','.cache','__pycache__'} for x in p.parts):return False
    if p.suffix.lower() in {'.pdf','.png','.jpg','.sqlite','.db','.log','.pyc'}:return False
    if package:return True
    return name in {'.gitignore','AGENTS.md','README.md','pyproject.toml','uv.lock','config.example.json'} or p.parts[0] in {'src','tests','docs'}


def inspect_text(name,data,private_terms=()):
    try:text=data.decode('utf-8')
    except UnicodeError:return [{'file':name,'rule':'unexpected_binary'}]
    issues=[{'file':name,'rule':label} for label,pattern in PATTERNS.items() if pattern.search(text)]
    emails=re.findall(r'\b[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b',text)
    if any(not mail.endswith(('.invalid','@example.org','@example.com')) for mail in emails):issues.append({'file':name,'rule':'personal_email_in_content'})
    if any(value and value in text for value in private_terms):issues.append({'file':name,'rule':'private_value'})
    return issues


def inspect_archive(path,private_terms=(),*,max_files=MAX_ARCHIVE_FILES,max_file=MAX_FILE,max_total=MAX_ARCHIVE_TOTAL):
    issues=[];count=0;total=0;seen=set()
    def entry(name,size,stream,link=False):
        nonlocal count,total
        count+=1;total+=size
        if count>max_files or size>max_file or total>max_total:raise Problem('ARCHIVE_LIMIT','压缩包成员数量或解压容量超限')
        if name in seen:issues.append({'file':name,'rule':'duplicate_archive_member'})
        seen.add(name)
        if not allowed_name(name,package=True):issues.append({'file':name,'rule':'private_archive_member'})
        if link:issues.append({'file':name,'rule':'archive_link_or_special'});return
        if stream is None:return
        data=stream.read(max_file+1)
        if len(data)>max_file or len(data)!=size:raise Problem('ARCHIVE_LIMIT','压缩包实际成员长度不合法')
        issues.extend(inspect_text(name,data,private_terms))
    try:
        if path.is_symlink() or path.stat().st_size>max_total:
            raise Problem('ARCHIVE_LIMIT','压缩包输入大小或文件类型不合法')
        if path.suffix=='.whl':
            # Bound the central directory BEFORE ZipFile allocates every entry.
            # ZIP64-sized archives exceed this small local package audit policy.
            with path.open('rb') as stream:
                stream.seek(max(0,path.stat().st_size-65557));tail=stream.read(65557)
            at=tail.rfind(b'PK\x05\x06')
            if at<0 or len(tail)-at<22:raise Problem('ARCHIVE_LIMIT','ZIP 目录尾不完整')
            end=struct.unpack('<4s4H2LH',tail[at:at+22])
            if end[4]>max_files or end[5]>min(max_total,max_files*1024) or at+22+end[7]!=len(tail):
                raise Problem('ARCHIVE_LIMIT','ZIP 目录数量或元数据容量超限')
            with zipfile.ZipFile(path) as archive:
                for member in archive.infolist():
                    mode=member.external_attr>>16
                    if member.is_dir():entry(member.filename,0,None);continue
                    if stat.S_ISLNK(mode):entry(member.filename,member.file_size,None,True);continue
                    # Check declared limits before opening/decompressing a member.
                    if member.file_size>max_file:raise Problem('ARCHIVE_LIMIT','压缩包单成员过大')
                    with archive.open(member) as stream:entry(member.filename,member.file_size,stream)
        else:
            with tarfile.open(path,mode='r|*') as archive:
                for member in archive:
                    entry(member.name,member.size,archive.extractfile(member) if member.isfile() else None,
                          not (member.isfile() or member.isdir()))
    except (Problem,OSError,tarfile.TarError,zipfile.BadZipFile) as exc:
        issues.append({'file':path.name,'rule':exc.code if isinstance(exc,Problem) else 'archive_invalid'})
    return {'scope':'ARCHIVE','archive':path.name,'files':count,'uncompressed_bytes':total,'issues':issues}


def git_read(project,args,limit=MAX_FILE):
    """No textconv, filters, smudge, hooks or optional index locks."""
    env={**os.environ,'GIT_OPTIONAL_LOCKS':'0','GIT_TERMINAL_PROMPT':'0','GIT_NO_REPLACE_OBJECTS':'1'}
    proc=subprocess.Popen(['/usr/bin/git',*args],cwd=project,env=env,stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,shell=False,close_fds=True)
    selector=selectors.DefaultSelector();data=bytearray();errors=bytearray();started=time.monotonic()
    for pipe in (proc.stdout,proc.stderr):os.set_blocking(pipe.fileno(),False);selector.register(pipe,selectors.EVENT_READ)
    try:
        while selector.get_map():
            if time.monotonic()-started>30:raise Problem('INDEX_UNREADABLE','Git 只读操作超时')
            for key,_ in selector.select(.05):
                chunk=os.read(key.fileobj.fileno(),65536)
                if not chunk:selector.unregister(key.fileobj);continue
                target=data if key.fileobj is proc.stdout else errors;target.extend(chunk)
                if len(target)>limit:raise Problem('INDEX_UNREADABLE','Git 输出超过审计上限')
        if proc.wait(timeout=1)!=0:raise Problem('INDEX_UNREADABLE','Git 对象无法读取')
        return bytes(data)
    finally:
        if proc.poll() is None:proc.kill();proc.wait()
        selector.close();proc.stdout.close();proc.stderr.close()


def scan(project: Path,packages: Path|None=None,*,runner=None):
    project=project.resolve()
    if runner is None:
        actual=Path(os.fsdecode(git_read(project,["rev-parse","--show-toplevel"])).strip())
        if actual!=project:raise Problem("PRIVACY_SCAN_FAILED","审计入口必须是现有项目根目录")
    runner=runner or (lambda args,limit=MAX_FILE:git_read(project,args,limit))
    def read(args,limit=MAX_FILE):
        result=runner(args,limit=limit)
        if not isinstance(result,bytes) or len(result)>limit:raise Problem('INDEX_UNREADABLE','索引响应格式不合法')
        return result
    private_terms=[]
    config=project/'.private/config.json'
    if config.is_file():private_terms.extend(json.loads(config.read_text()).get('prefixes',[]))
    host=socket.gethostname()
    if len(host)>5:private_terms.append(host)
    lesson=project/'.private/ingest/lesson.json'
    if lesson.is_file():
        data=json.loads(lesson.read_text());fragments=data.get('title',[])+[f for w in data.get('words',[]) for f in w.get('fields',[])]
        private_terms.extend(f['raw'] for f in fragments if len(f['raw'])>=40)
    issues=[];archives=[];index=[];candidates=[];staged=[]
    for attempt in range(2):
        issues=[];index=[]
        try:
            before=read(['ls-files','--stage','-z'])
            untracked=read(['ls-files','--others','--exclude-standard','-z'])
            staged=read(['diff','--cached','--name-only','-z']).split(b'\0')
            for record in before.split(b'\0'):
                if not record:continue
                metadata,name=record.split(b'\t',1);mode,oid,stage=metadata.split(b' ')
                name=os.fsdecode(name);object_id=oid.decode('ascii')
                if not re.fullmatch('[a-f0-9]{40}|[a-f0-9]{64}',object_id):raise ValueError('object id')
                index.append(name)
                if not allowed_name(name):issues.append({'scope':'INDEX','file':name,'rule':'unexpected_public_candidate'})
                if mode not in (b'100644',b'100755') or stage!=b'0':
                    issues.append({'scope':'INDEX','file':name,'rule':'unsupported_mode_or_conflict'});continue
                size=int(read(['cat-file','-s',object_id],limit=100).strip())
                if not 0<=size<=MAX_FILE:raise Problem('INDEX_UNREADABLE','索引 blob 超过上限')
                blob=read(['cat-file','blob',object_id],limit=MAX_FILE)
                if len(blob)!=size:raise Problem('INDEX_UNREADABLE','索引 blob 长度不符')
                issues.extend({'scope':'INDEX',**i} for i in inspect_text(name,blob,private_terms))
            candidates=sorted(set(index+[os.fsdecode(n) for n in untracked.split(b'\0') if n]))
            for name in candidates:
                if not allowed_name(name):issues.append({'scope':'WORKTREE','file':name,'rule':'unexpected_public_candidate'})
                path=project/name
                if path.is_symlink() or path.resolve()!=path or not path.is_relative_to(project):
                    issues.append({'scope':'WORKTREE','file':name,'rule':'public_symlink_or_path'});continue
                if not path.exists():continue # The INDEX copy was still inspected above.
                if not path.is_file() or path.stat().st_size>MAX_FILE:
                    issues.append({'scope':'WORKTREE','file':name,'rule':'unsupported_file_or_size'});continue
                fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
                with os.fdopen(fd,'rb') as f:blob=f.read(MAX_FILE+1)
                if len(blob)>MAX_FILE:issues.append({'scope':'WORKTREE','file':name,'rule':'file_changed_or_size'});continue
                issues.extend({'scope':'WORKTREE',**i} for i in inspect_text(name,blob,private_terms))
            after=read(['ls-files','--stage','-z'])
            if before==after:break
            if attempt==1:issues.append({'scope':'INDEX','rule':'UNSTABLE'})
        except (Problem,ValueError,OSError) as exc:
            issues.append({'scope':'INDEX','rule':exc.code if isinstance(exc,Problem) else 'INDEX_UNREADABLE'});break
    if packages:
        package_root=packages.resolve()
        if not package_root.is_relative_to(project):raise Problem('UNSAFE_PATH','包目录须位于当前项目')
        for path in sorted(package_root.iterdir()):
            if path.name.endswith(('.whl','.tar.gz')):
                report=inspect_archive(path,private_terms);archives.append(report);issues.extend({'scope':'ARCHIVE',**x} for x in report['issues'])
    result={'status':'PASSED' if not issues else 'FAILED','scopes':['WORKTREE','INDEX','ARCHIVE' if packages else 'ARCHIVE_NOT_RUN'],
        'tracked_files':len(index),'staged_files':len([x for x in staged if x]),'public_candidates':len(candidates),
        'candidate_files':candidates,'index_blob_source':'git ls-files --stage -z + git cat-file blob OID',
        'metadata_scope':'Commit messages, authors and history intentionally excluded; no content email exemption.',
        'archives':archives,'issues':issues,'limits':'Bounded pattern and known-value audit; not arbitrary-secret detection.'}
    if issues:raise Problem('PRIVACY_SCAN_FAILED','公开内容审计失败或范围未完整读取',details=result)
    return result
