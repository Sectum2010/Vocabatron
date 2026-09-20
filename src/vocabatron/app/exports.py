"""Descriptor-relative, no-overwrite PDF copies. Outputs is never the archive."""
from __future__ import annotations
import ctypes
import errno
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import time
from ..domain import Problem
from ..storage import fsync_dir,private_mkdir

def open_directory(path):
    """Open every component without following a swapped parent symlink."""
    path=Path(path)
    if not path.is_absolute() or '..' in path.parts:raise Problem('UNSAFE_PATH','Invalid export directory')
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            next_fd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=next_fd
        return fd
    except BaseException:os.close(fd);raise


def copy_export(library,artifact):
    config=library.config
    mapping=library.db.one('SELECT e.*,b.directory FROM exports e JOIN batches b ON b.id=e.batch_id WHERE e.artifact_id=?',(artifact['id'],))
    if not mapping:raise Problem('EXPORT_MAPPING_MISSING','The export mapping is missing')
    directory=mapping['directory'];filename=mapping['filename']
    if Path(directory).name!=directory or Path(filename).name!=filename or not filename.endswith('.pdf'):
        raise Problem('UNSAFE_PATH','Invalid server-generated export name')
    # The configured root is created administratively, after ignore verification.
    root_fd=open_directory(config.outputs_root);dest_fd=None;temporary=None
    try:
        try:os.mkdir(directory,mode=0o700,dir_fd=root_fd)
        except FileExistsError:pass
        dest_fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=root_fd)
        try:
            existing=os.open(filename,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=dest_fd)
        except FileNotFoundError:existing=None
        if existing is not None:
            try:
                info=os.fstat(existing)
                if not stat.S_ISREG(info.st_mode):raise Problem('EXPORT_CONFLICT','Export destination is not a regular file')
                h=hashlib.sha256()
                while chunk:=os.read(existing,1024*1024):h.update(chunk)
                if h.hexdigest()!=artifact['sha256']:raise Problem('EXPORT_CONFLICT','An existing export has different bytes. It was not overwritten.')
                return {'status':'AVAILABLE','reused':True,'directory':directory,'filename':filename}
            finally:os.close(existing)
        private_mkdir(config.runtime_root/'exports')
        out_fd,temporary=tempfile.mkstemp(prefix='export-',dir=config.runtime_root/'exports')
        if os.fstat(out_fd).st_dev!=os.fstat(dest_fd).st_dev:
            os.close(out_fd);raise Problem('EXPORT_FILESYSTEM','Private staging and Outputs must share a filesystem for atomic publication')
        source=library.objects.open_verified(artifact['path'],artifact['sha256'])
        try:
            h=hashlib.sha256()
            with os.fdopen(out_fd,'wb') as out:
                while chunk:=os.read(source,1024*1024):out.write(chunk);h.update(chunk)
                out.flush();os.fsync(out.fileno())
            if h.hexdigest()!=artifact['sha256']:raise Problem('ARTIFACT_CHANGED','Export copy hash mismatch')
        finally:os.close(source)
        # Linux renameat2 NOREPLACE gives a single, atomic no-overwrite boundary.
        libc=ctypes.CDLL(None,use_errno=True)
        result=libc.renameat2(-100,os.fsencode(temporary),dest_fd,os.fsencode(filename),1)
        if result:
            number=ctypes.get_errno()
            if number==errno.EEXIST:raise Problem('EXPORT_CONFLICT','An export appeared during publication; no file was overwritten')
            raise OSError(number,os.strerror(number))
        temporary=None;os.fsync(dest_fd);os.fsync(root_fd)
        # Directory replacement cannot redirect the open descriptors outside
        # their anchored inode. Detect a removed/replaced pathname for recovery.
        current=open_directory(config.outputs_root/directory)
        try:
            if (os.fstat(current).st_dev,os.fstat(current).st_ino)!=(os.fstat(dest_fd).st_dev,os.fstat(dest_fd).st_ino):
                raise Problem('EXPORT_DIRECTORY_CHANGED','Export directory changed; the internal original is safe')
        finally:os.close(current)
        return {'status':'AVAILABLE','reused':False,'directory':directory,'filename':filename}
    finally:
        if temporary is not None:
            try:os.unlink(temporary)
            except FileNotFoundError:pass
        if dest_fd is not None:os.close(dest_fd)
        os.close(root_fd)


def restore_one(library,artifact):
    try:
        result=copy_export(library,artifact);status='AVAILABLE';error=None
    except (Problem,OSError) as exc:
        error=exc.code if isinstance(exc,Problem) else 'EXPORT_IO_ERROR'
        status='CONFLICT' if error=='EXPORT_CONFLICT' else 'FAILED_RETRYABLE'
        result={'status':status,'error_code':error,'message':exc.message if isinstance(exc,Problem) else 'The verified internal file is safe; export can be retried.'}
    with library.db.transaction() as c:
        c.execute('UPDATE exports SET status=?,error_code=?,updated=? WHERE artifact_id=?',(status,error,time.time(),artifact['id']))
    return result
