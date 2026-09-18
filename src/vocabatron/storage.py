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
        if self.root.is_symlink() or self.root.resolve() != self.root:
            raise Problem("UNSAFE_PATH", "私人目录不能经过符号链接")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
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
        if Path(relative).parts[0] == "sources":
            raise Problem("SOURCE_READ_ONLY", "应用不覆盖原件工作副本")
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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
        return json.loads(self.path(relative).read_text())

    @contextlib.contextmanager
    def exclusive(self):
        p = self.path("execution.lock")
        fd = os.open(p, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Problem("BUSY", "已有本地任务执行；并发上限为一") from None
            yield
        finally:
            os.close(fd)

    def stage(self, task_id):
        task_id = identifier(task_id)
        relative = Path("staging") / f"{task_id}-{uuid.uuid4().hex}"
        self.path(relative).mkdir(mode=0o700, parents=True)
        return relative

    def publish(self, stage: Path, task_id: str, manifest: dict):
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
        self.json(stage / "manifest.json", manifest)
        for p in source.rglob("*"):
            if p.is_file():
                p.chmod(0o600)
                with p.open("rb") as f:
                    os.fsync(f.fileno())
        fsync_dir(source)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.rename(source, destination)
        fsync_dir(destination.parent)
        return destination


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
