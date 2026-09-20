"""Consistent SQLite snapshots and copied immutable objects; no cloud transport."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import time
import apsw
from ..domain import Problem
from ..storage import sha256,private_mkdir,fsync_dir


def backup(library,destination):
    destination=Path(destination)
    if destination.resolve()!=destination or destination.exists() or not destination.is_relative_to(library.config.data_root/'backups'):
        raise Problem('UNSAFE_PATH','Backup destination must be a new private backup directory')
    private_mkdir(destination);database=destination/'database.sqlite3'
    src=library.db.connect();dest=apsw.Connection(str(database))
    try:
        with dest.backup('main',src,'main') as operation:
            while not operation.done:operation.step(128);time.sleep(.01)
        if dest.execute('PRAGMA integrity_check').get!='ok':raise Problem('BACKUP_INVALID','Database backup integrity check failed')
        # Scan the snapshot, never a changing live DB, to establish its references.
        names=[row[0] for row in dest.execute("SELECT name FROM sqlite_schema WHERE type='table'")]
        references=set()
        def collect(value):
            if isinstance(value,str):
                import re
                if re.fullmatch(r'[a-f0-9]{2}/[a-f0-9]{64}\.(?:pdf|json|md|bin|txt)',value):references.add(value)
                elif value.startswith(('{','[')):
                    try:collect(json.loads(value))
                    except ValueError:pass
            elif isinstance(value,dict):
                for v in value.values():collect(v)
            elif isinstance(value,(list,tuple)):
                for v in value:collect(v)
        for name in names:
            for row in dest.execute('SELECT * FROM "'+name.replace('"','""')+'"'):collect(row)
        # A verified PDF may have a durable filesystem checkpoint immediately
        # before its database publication. Include this closure as well.
        publications=[]
        for path in sorted((library.config.data_root/'publication').glob('*.json')):
            if path.is_symlink() or path.stat().st_size>32*1024:raise Problem('BACKUP_INVALID','Invalid publication checkpoint')
            raw=path.read_bytes();collect(json.loads(raw));publications.append((path.name,raw))
        pending=list(references);seen=set()
        while pending:
            ref=pending.pop()
            if ref in seen:continue
            seen.add(ref)
            if ref.endswith('.json'):
                collect(library.objects.read_json(ref));pending.extend(references-seen)
        manifest=[]
        for name,raw in publications:
            target=destination/'publication'/name;private_mkdir(target.parent)
            with target.open('xb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
            manifest.append({'path':str(target.relative_to(destination)),'sha256':sha256(target),'bytes':len(raw)})
        for ref in sorted(references):
            origin=library.objects.path(ref);target=destination/'objects'/ref;private_mkdir(target.parent)
            fd=library.objects.open_verified(ref,origin.stem)
            with os.fdopen(fd,'rb') as incoming,target.open('xb') as out:
                shutil.copyfileobj(incoming,out,1024*1024);out.flush();os.fsync(out.fileno())
            manifest.append({'path':str(target.relative_to(destination)),'sha256':sha256(target),'bytes':target.stat().st_size})
        # All immutable selection evidence is retained so both current and legacy
        # clue references remain independently verifiable after restoration.
        core=library.config.data_root/'core'
        if core.exists():
            for origin in sorted(core.rglob('*')):
                if not origin.is_file() or origin.is_symlink():continue
                target=destination/'core'/origin.relative_to(core);private_mkdir(target.parent)
                with origin.open('rb') as incoming,target.open('xb') as out:
                    shutil.copyfileobj(incoming,out,1024*1024);out.flush();os.fsync(out.fileno())
                manifest.append({'path':str(target.relative_to(destination)),'sha256':sha256(target),'bytes':target.stat().st_size})
        dest.execute('PRAGMA journal_mode=DELETE')
        dest.close();dest=None
        manifest.append({'path':database.name,'sha256':sha256(database),'bytes':database.stat().st_size})
        record={'schema_version':1,'created':time.time(),'files':manifest,'scope':'Same-disk recovery; not protection against disk loss'}
        with (destination/'manifest.json').open('x') as out:json.dump(record,out);out.flush();os.fsync(out.fileno())
        fsync_dir(destination);return {'files':len(manifest),'path':str(destination)}
    finally:
        src.close()
        if dest is not None:dest.close()


def restore_snapshot(source,destination):
    """Explicit isolated restore; never replaces a running database."""
    source=Path(source);destination=Path(destination)
    if source.resolve()!=source or destination.resolve()!=destination or destination.exists():raise Problem('UNSAFE_PATH','Restore requires a new isolated directory')
    manifest=json.loads((source/'manifest.json').read_bytes());private_mkdir(destination)
    for item in manifest['files']:
        relative=Path(item['path'])
        if relative.is_absolute() or '..' in relative.parts:raise Problem('BACKUP_INVALID','Invalid backup reference')
        origin=source/relative
        if origin.resolve()!=origin or not origin.is_file() or sha256(origin)!=item['sha256']:raise Problem('BACKUP_INVALID','Backup file integrity mismatch')
        target=destination/relative;private_mkdir(target.parent);shutil.copyfile(origin,target)
    connection=apsw.Connection(str(destination/'database.sqlite3'))
    try:
        if connection.execute('PRAGMA integrity_check').get!='ok' or list(connection.execute('PRAGMA foreign_key_check')):
            raise Problem('BACKUP_INVALID','Restored database is inconsistent')
    finally:connection.close()
    return {'status':'VERIFIED','files':len(manifest['files']),'path':str(destination)}
