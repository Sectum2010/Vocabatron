"""Private paths, durable writes, exclusive execution and set-level publication."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import uuid

from .domain import Problem


def sha256(path: Path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def identifier(value: str):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise Problem("UNSAFE_IDENTIFIER", "文件前缀或任务 ID 不合法")
    return value


class PrivateStore:
    def __init__(self, root: Path):
        self.root = root.absolute()
        self._held=threading.local()
        project=Path(__file__).resolve().parents[2]
        if not any(self.root.is_relative_to(project/name) for name in (".private", ".cache")):
            raise Problem("UNSAFE_PATH", "私人目录必须位于当前项目的私人数据域内")
        if self.root.is_symlink() or self.root.resolve() != self.root:
            raise Problem("UNSAFE_PATH", "私人目录不能经过符号链接")
        private_mkdir(self.root)
        self.root.chmod(0o700)

    def path(self, relative: str | Path):
        relative = Path(relative)
        if relative.is_absolute() or ".." in relative.parts:
            raise Problem("UNSAFE_PATH", "路径必须位于私人目录内")
        p = self.root / relative
        if p.resolve() != p or not p.is_relative_to(self.root):
            raise Problem("UNSAFE_PATH", "路径含符号链接或越界")
        return p

    def write(self, relative, data: bytes):
        p = self.path(relative)
        if not Path(relative).parts:
            raise Problem("UNSAFE_PATH", "文件路径不能为空")
        if Path(relative).parts[0] == "sources":
            raise Problem("SOURCE_READ_ONLY", "应用不覆盖原件工作副本")
        if Path(relative).parts[0] == "results":
            raise Problem("RESULT_READ_ONLY", "已发布结果只能读取")
        private_mkdir(p.parent)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".write-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
            fsync_dir(p.parent)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return p

    def json(self, relative, value):
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        return self.write(relative, (json.dumps(value, ensure_ascii=False, indent=2)+"\n").encode())

    def read(self, relative):
        p=self.path(relative)
        if p.stat().st_size>32*1024*1024:raise Problem("RESOURCE_LIMIT", "JSON 文件超过上限")
        return json.loads(p.read_text())

    def immutable_json(self, relative, value):
        if self.path(relative).exists():
            from .domain import digest
            if digest(self.read(relative)) != digest(value):raise Problem("IMMUTABLE_CONFLICT", "不可变版本已存在不同内容")
            return self.path(relative)
        return self.json(relative,value)

    @contextlib.contextmanager
    def commit_lock(self):
        fd=os.open(self.path("publication.lock"),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX)
            yield
        finally:os.close(fd)

    @contextlib.contextmanager
    def exclusive(self):
        if getattr(self._held,"depth",0):
            self._held.depth+=1
            try:yield
            finally:self._held.depth-=1
            return
        p = self.path("execution.lock")
        fd = os.open(p, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Problem("BUSY", "已有本地任务执行；并发上限为一") from None
            self._held.depth=1
            yield
        finally:
            self._held.depth=0
            os.close(fd)

    def stage(self, task_id):
        task_id = identifier(task_id)
        relative = Path("staging") / f"{task_id}-{uuid.uuid4().hex}"
        private_mkdir(self.path(relative))
        return relative

    def publish(self, stage: Path, task_id: str, manifest: dict):
        task_id=identifier(task_id);stage=Path(stage)
        if (len(stage.parts)!=2 or stage.parts[0]!='staging' or
            not re.fullmatch(re.escape(task_id)+r'-[a-f0-9]{32}',stage.name)):
            raise Problem('UNSAFE_PATH','只能发布当前任务拥有的 staging 目录')
        source = self.path(stage)
        destination = self.path(Path("results") / identifier(task_id))
        if destination.exists():
            raise Problem("RESULT_EXISTS", "已有同名结果集；不会覆盖")
        if manifest.get("status") != "VERIFIED" or len(manifest.get("pdfs", [])) != 2:
            raise Problem("INCOMPLETE_SET", "只允许发布已验证的完整双份结果")
        names = [x["name"] for x in manifest["pdfs"]]
        if len(set(names)) != 2:
            raise Problem("INCOMPLETE_SET", "结果文件名必须不同")
        for item in manifest["pdfs"]:
            if Path(item["name"]).name != item["name"]:
                raise Problem("UNSAFE_PATH", "清单中的文件名越界")
            p = self.path(stage / item["name"])
            if not p.is_file() or sha256(p) != item["sha256"]:
                raise Problem("INCOMPLETE_SET", "清单文件缺失或哈希不符")
        from .manifest import validate_manifest
        validate_manifest(manifest,task_id)
        for name,expected in manifest["snapshot_hashes"].items():
            snapshot=self.path(stage/name)
            if not snapshot.is_file() or sha256(snapshot)!=expected:
                raise Problem("INCOMPLETE_SET","发布前必需快照缺失或哈希不符")
        self.json(stage / "manifest.json", manifest)
        for p in source.rglob("*"):
            if p.is_symlink() or p.resolve()!=p:
                raise Problem('UNSAFE_PATH','发布目录中不允许符号链接')
            if p.is_file():
                p.chmod(0o600)
                with p.open("rb") as f:
                    os.fsync(f.fileno())
        fsync_dir(source)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        from .execution import checkpoint
        with self.commit_lock():
            checkpoint(force_cancel=True)
            os.rename(source, destination)
        try:fsync_dir(destination.parent)
        except OSError:
            # Rename is the completion boundary. Files and manifest were fsynced;
            # a later directory-fsync/state failure must not revoke publication.
            from .execution import CURRENT
            context=CURRENT.get()
            if context:context.data["post_publish_directory_fsync"]="FAILED_AFTER_COMMIT"
        return destination


def private_mkdir(path):
    missing=[];current=Path(path)
    while not current.exists():missing.append(current);current=current.parent
    for p in reversed(missing):p.mkdir(mode=0o700)


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
