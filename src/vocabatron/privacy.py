"""Local, scope-limited public-candidate and built-archive privacy checks."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tarfile
import zipfile

from .domain import Problem


PATTERNS={
    "private_key":re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github_token":re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    "cloud_access_key":re.compile(r"AKIA[A-Z0-9]{16}"),
    "api_secret":re.compile(r"\bsk-[A-Za-z0-9_-]{24,}"),
    "user_absolute_path":re.compile(r"/(?:home|Users)/[^/\s'\"]+/"),
    "tailnet_address":re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"),
    "tailnet_domain":re.compile(r"\b[A-Za-z0-9.-]+\.ts\.net\b"),
}


def allowed_name(name,*,package=False):
    p=Path(name)
    if p.is_absolute() or ".." in p.parts:return False
    if any(x.startswith(".private") or x in {".venv",".cache","__pycache__"} for x in p.parts):return False
    if p.suffix.lower() in {".pdf",".png",".jpg",".sqlite",".db",".log",".pyc"}:return False
    if package:return True
    return (name in {".gitignore","AGENTS.md","README.md","pyproject.toml","uv.lock","config.example.json"}
            or p.parts[0] in {"src","tests","docs"})


def inspect_text(name,data,private_terms=()):
    try:text=data.decode("utf-8")
    except UnicodeError:return [{"file":name,"rule":"unexpected_binary"}]
    issues=[{"file":name,"rule":label} for label,pattern in PATTERNS.items() if pattern.search(text)]
    for value in private_terms:
        if value and value in text:
            issues.append({"file":name,"rule":"private_value"});break
    return issues


def inspect_archive(path,private_terms=()):
    issues=[];count=0
    if path.suffix==".whl":
        with zipfile.ZipFile(path) as archive:items=[(name,archive.read(name)) for name in archive.namelist() if not name.endswith("/")]
    else:
        with tarfile.open(path) as archive:
            items=[]
            for member in archive.getmembers():
                if member.issym() or member.islnk():issues.append({"file":member.name,"rule":"archive_link"})
                if member.isfile():items.append((member.name,archive.extractfile(member).read()))
    for name,data in items:
        count+=1
        if not allowed_name(name,package=True):issues.append({"file":name,"rule":"private_archive_member"})
        issues.extend(inspect_text(name,data,private_terms))
    return {"archive":path.name,"files":count,"issues":issues}


def scan(project: Path,packages: Path | None=None):
    project=project.resolve()
    def git(*args):
        r=subprocess.run(["git",*args],cwd=project,env={**os.environ,"GIT_OPTIONAL_LOCKS":"0"},capture_output=True,check=True)
        return [x.decode() for x in r.stdout.split(b"\0") if x]
    tracked=git("ls-files","-z","--cached");untracked=git("ls-files","-z","--others","--exclude-standard")
    staged=git("diff","--cached","--name-only","-z")
    candidates=sorted(set(tracked+untracked));issues=[];private_terms=[]
    config=project/".private"/"config.json"
    if config.is_file():private_terms.extend(json.loads(config.read_text()).get("prefixes",[]))
    host=socket.gethostname()
    if len(host)>5:private_terms.append(host)
    lesson=project/".private"/"ingest"/"lesson.json"
    if lesson.is_file():
        data=json.loads(lesson.read_text())
        fragments=data.get("title",[])+[f for word in data.get("words",[]) for f in word.get("fields",[])]
        private_terms.extend(f["raw"] for f in fragments if len(f["raw"])>=40)
    for name in candidates:
        if not allowed_name(name):issues.append({"file":name,"rule":"unexpected_public_candidate"})
        p=project/name
        if p.is_symlink() or not p.resolve().is_relative_to(project):
            issues.append({"file":name,"rule":"public_symlink"});continue
        if p.is_file():issues.extend(inspect_text(name,p.read_bytes(),private_terms))
    private_tracked=[n for n in tracked+staged if not allowed_name(n)]
    archives=[]
    if packages:
        package_root=packages.resolve()
        if not package_root.is_relative_to(project):raise Problem("UNSAFE_PATH","打包扫描目录必须位于当前项目")
        for p in sorted(package_root.iterdir()):
            if p.name.endswith((".whl",".tar.gz")):
                report=inspect_archive(p,private_terms);archives.append(report);issues.extend(report["issues"])
    result={"status":"PASSED" if not issues and not private_tracked else "FAILED",
            "scope":"Git tracked + staged names + non-ignored untracked files; optional wheel/sdist members",
            "tracked_files":len(tracked),"staged_files":len(staged),"public_candidates":len(candidates),
            "candidate_files":candidates,"private_tracked_or_staged":private_tracked,"archives":archives,"issues":issues,
            "limits":"Pattern and known-project-value checks; not a guarantee for arbitrary unknown secrets."}
    if result["status"]!="PASSED":raise Problem("PRIVACY_SCAN_FAILED","公开候选或打包范围存在隐私风险",details=result)
    return result
