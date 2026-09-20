"""Immutable private objects and descriptor-based integrity checks."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from ..domain import Problem
from ..storage import private_mkdir, fsync_dir


class Objects:
    def __init__(self, root):
        self.root = Path(root)
        if self.root.resolve()!=self.root: raise Problem('UNSAFE_PATH','Object root contains a symlink')
        private_mkdir(self.root)
        self._verified = {}

    def path(self, relative):
        p=Path(relative)
        if p.is_absolute() or '..' in p.parts or not p.parts:
            raise Problem('UNSAFE_PATH','Invalid private object reference')
        path=self.root/p
        if path.resolve()!=path: raise Problem('UNSAFE_PATH','Private object path contains a symlink')
        return path

    def put(self, data: bytes, suffix='.bin'):
        if suffix not in ('.bin','.pdf','.json','.md','.txt'):
            raise ValueError('Unsupported object type')
        key=hashlib.sha256(data).hexdigest()
        relative=f'{key[:2]}/{key}{suffix}'
        target=self.path(relative);private_mkdir(target.parent)
        if target.exists():
            fd=self.open_verified(relative,key)
            os.close(fd);return relative,key
        fd,tmp=tempfile.mkstemp(prefix='.object-',dir=target.parent)
        try:
            with os.fdopen(fd,'wb') as stream:
                stream.write(data);stream.flush();os.fsync(stream.fileno())
            try:os.link(tmp,target,follow_symlinks=False)
            except FileExistsError:
                checked=self.open_verified(relative,key);os.close(checked)
            fsync_dir(target.parent)
        finally:os.unlink(tmp)
        return relative,key

    def put_json(self, value):
        from .database import encode
        return self.put(encode(value).encode(),'.json')[0]

    def put_file(self, path, *, max_bytes, suffix='.pdf'):
        if suffix not in ('.pdf','.bin'):raise ValueError('Unsupported object type')
        source=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        fd,tmp=tempfile.mkstemp(prefix='.incoming-',dir=self.root)
        stream=os.fdopen(fd,'wb')
        try:
            info=os.fstat(source)
            if not stat.S_ISREG(info.st_mode) or not 0<info.st_size<=max_bytes:
                raise Problem('RESOURCE_LIMIT','Input is not a bounded regular file')
            h=hashlib.sha256();total=0
            with stream as out:
                while chunk:=os.read(source,1024*1024):
                    total+=len(chunk)
                    if total>max_bytes:raise Problem('RESOURCE_LIMIT','Input grew beyond its size limit')
                    h.update(chunk);out.write(chunk)
                out.flush();os.fsync(out.fileno())
            key=h.hexdigest();relative=f'{key[:2]}/{key}{suffix}'
            target=self.path(relative);private_mkdir(target.parent)
            try:os.link(tmp,target,follow_symlinks=False)
            except FileExistsError:
                checked=self.open_verified(relative,key);os.close(checked)
            fsync_dir(target.parent)
            return relative,key
        finally:
            os.close(source)
            stream.close()
            os.unlink(tmp)

    def read_json(self, relative):
        key=Path(relative).stem
        fd=self.open_verified(relative,key)
        with os.fdopen(fd,'rb') as stream:
            if os.fstat(stream.fileno()).st_size>128*1024**2:raise Problem('RESOURCE_LIMIT','Private JSON exceeds its bound')
            return json.load(stream)

    def open_verified(self, relative, expected):
        path=self.path(relative)
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):raise Problem('UNSAFE_PATH','Expected a regular artifact')
            identity=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
            if self._verified.get((relative,expected))!=identity:
                h=hashlib.sha256()
                while chunk:=os.read(fd,1024*1024):h.update(chunk)
                if h.hexdigest()!=expected:raise Problem('ARTIFACT_CHANGED','The private artifact failed its integrity check')
                after=os.fstat(fd)
                if (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns)!=identity:
                    raise Problem('ARTIFACT_CHANGED','The artifact changed while reading')
                os.lseek(fd,0,os.SEEK_SET)
                if len(self._verified)>512:self._verified.clear()
                self._verified[(relative,expected)]=identity
            return fd
        except BaseException:
            os.close(fd);raise
