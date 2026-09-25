"""Short SQLite transactions, WAL/FULL durability and fenced task ownership."""
from __future__ import annotations
from contextlib import contextmanager
import json
import os
from pathlib import Path
import time
import uuid
import apsw
from ..domain import Problem
from ..storage import private_mkdir

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_versions(version INTEGER PRIMARY KEY, applied REAL NOT NULL);
CREATE TABLE IF NOT EXISTS preferences(singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, path TEXT NOT NULL, name TEXT NOT NULL, bytes INTEGER NOT NULL, status TEXT NOT NULL, pages INTEGER, report TEXT, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS answer_sets(id TEXT PRIMARY KEY, next_variant INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS families(id TEXT PRIMARY KEY, answer_set_id TEXT NOT NULL REFERENCES answer_sets(id), rules TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lessons(id TEXT PRIMARY KEY, number INTEGER NOT NULL, family_id TEXT NOT NULL REFERENCES families(id), canonical TEXT NOT NULL, lesson_json TEXT NOT NULL, source_id TEXT NOT NULL REFERENCES sources(id), word_count INTEGER NOT NULL, frozen_ref TEXT, display_name TEXT, archived INTEGER NOT NULL DEFAULT 0, next_batch INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS source_bindings(id TEXT PRIMARY KEY, lesson_id TEXT NOT NULL REFERENCES lessons(id), source_id TEXT NOT NULL REFERENCES sources(id), binding TEXT NOT NULL, created REAL NOT NULL, UNIQUE(lesson_id,source_id,id));
CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, request_id TEXT REFERENCES requests(id), kind TEXT NOT NULL, lesson_id TEXT REFERENCES lessons(id), family_id TEXT REFERENCES families(id), input_json TEXT NOT NULL, target_type TEXT NOT NULL DEFAULT 'count', target_count INTEGER, completed INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, stage TEXT NOT NULL, detail TEXT, priority INTEGER NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, eligible REAL NOT NULL DEFAULT 0, attempt INTEGER NOT NULL DEFAULT 0, fence TEXT, owner TEXT, lease_until REAL, intent TEXT NOT NULL DEFAULT 'run', retries INTEGER NOT NULL DEFAULT 0, error_code TEXT, batch_id TEXT);
CREATE INDEX IF NOT EXISTS task_queue ON tasks(status,priority,eligible,updated);
CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, lesson_id TEXT NOT NULL REFERENCES lessons(id), number INTEGER NOT NULL, directory TEXT NOT NULL UNIQUE, task_id TEXT REFERENCES tasks(id), created REAL NOT NULL, UNIQUE(lesson_id,number));
CREATE TABLE IF NOT EXISTS structures(id TEXT PRIMARY KEY, family_id TEXT NOT NULL REFERENCES families(id), answer_set_id TEXT NOT NULL REFERENCES answer_sets(id), variant_number INTEGER NOT NULL, crossing_hash TEXT NOT NULL, geometry_hash TEXT NOT NULL, crossings TEXT NOT NULL, geometry TEXT NOT NULL, layout TEXT NOT NULL, dedupe_version TEXT NOT NULL, state TEXT NOT NULL, creator_task TEXT REFERENCES tasks(id), created REAL NOT NULL, UNIQUE(answer_set_id,crossing_hash), UNIQUE(answer_set_id,geometry_hash), UNIQUE(answer_set_id,variant_number));
CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY, structure_id TEXT NOT NULL REFERENCES structures(id), lesson_id TEXT NOT NULL REFERENCES lessons(id), delivery_id TEXT NOT NULL, task_id TEXT REFERENCES tasks(id), batch_id TEXT REFERENCES batches(id), filename TEXT NOT NULL, path TEXT, sha256 TEXT, bytes INTEGER, state TEXT NOT NULL, report TEXT, legacy_ref TEXT, created REAL NOT NULL, updated REAL NOT NULL, UNIQUE(structure_id,lesson_id,delivery_id));
CREATE TABLE IF NOT EXISTS exports(artifact_id TEXT PRIMARY KEY REFERENCES artifacts(id), batch_id TEXT NOT NULL REFERENCES batches(id), filename TEXT NOT NULL, status TEXT NOT NULL, error_code TEXT, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS family_locks(family_id TEXT PRIMARY KEY REFERENCES families(id), task_id TEXT NOT NULL REFERENCES tasks(id), fence TEXT NOT NULL, owner TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reservations(task_id TEXT PRIMARY KEY REFERENCES tasks(id), fence TEXT NOT NULL, resources TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS representation_rejections(family_id TEXT NOT NULL REFERENCES families(id), delivery_id TEXT NOT NULL, layout_hash TEXT NOT NULL, layout TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(family_id,delivery_id,layout_hash));
CREATE TABLE IF NOT EXISTS exhaustion(family_id TEXT NOT NULL REFERENCES families(id), delivery_id TEXT NOT NULL, history_hash TEXT NOT NULL, evidence TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(family_id,delivery_id));
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, type TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS telemetry(singleton INTEGER PRIMARY KEY CHECK(singleton=1), value TEXT NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS resource_samples(id INTEGER PRIMARY KEY AUTOINCREMENT, phase TEXT NOT NULL, features TEXT NOT NULL, estimate TEXT NOT NULL, actual TEXT NOT NULL, completed INTEGER NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS holds(id TEXT PRIMARY KEY, until REAL NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS migrations(id TEXT PRIMARY KEY, source TEXT NOT NULL, evidence TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS task_checkpoints(task_id TEXT PRIMARY KEY REFERENCES tasks(id), value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS semantic_aliases(id TEXT PRIMARY KEY, lesson_id TEXT NOT NULL REFERENCES lessons(id), canonical TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_attempts(task_id TEXT PRIMARY KEY REFERENCES tasks(id), source_id TEXT NOT NULL REFERENCES sources(id), parser_version TEXT NOT NULL, previous_report TEXT, report TEXT, timings TEXT, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS parser_retries(source_id TEXT NOT NULL REFERENCES sources(id), parser_version TEXT NOT NULL, task_id TEXT NOT NULL REFERENCES tasks(id), PRIMARY KEY(source_id,parser_version));
"""

def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class Database:
    def __init__(self, path):
        self.path = Path(path)
        version = tuple(map(int, apsw.sqlitelibversion().split('.')))
        if version < (3,51,3):
            raise Problem('SQLITE_RUNTIME_UNSAFE', 'A verified WAL-reset-fixed SQLite runtime is required')
        if self.path.resolve()!=self.path or self.path.is_symlink():
            raise Problem('UNSAFE_PATH', 'Invalid database path')

    def connect(self):
        c = apsw.Connection(str(self.path))
        c.set_busy_timeout(5000)
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('PRAGMA synchronous=FULL')
        c.execute('PRAGMA trusted_schema=OFF')
        c.execute('PRAGMA temp_store=MEMORY')
        return c

    def initialize(self):
        private_mkdir(self.path.parent)
        c=self.connect()
        try:
            mode = c.execute('PRAGMA journal_mode=WAL').get
            if str(mode).lower()!='wal': raise Problem('DATABASE_WAL_REQUIRED','WAL could not be enabled')
            with c:
                c.execute(SCHEMA)
                for table,columns in {'tasks':{'dismissed_at':'REAL','started_at':'REAL','yield_requested':'TEXT',
                                      'resource_wait_started':'REAL','resource_wait_seconds':'REAL NOT NULL DEFAULT 0','queue_wait_seconds':'REAL'},
                                      'sources':{'notice_dismissed_at':'REAL','parser_version':'TEXT'}}.items():
                    existing={row['name'] for row in rows(c,'PRAGMA table_info('+table+')')}
                    for name,kind in columns.items():
                        if name not in existing:c.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+kind)
                c.execute('INSERT OR IGNORE INTO schema_versions VALUES(1,?)',(time.time(),))
                c.execute('INSERT OR IGNORE INTO schema_versions VALUES(2,?)',(time.time(),))
                c.execute('INSERT OR IGNORE INTO preferences VALUES(1,1,?)', (encode({
                    'default_count':2,'theme':'system','background_prepare':False,
                    'search_slots':1,'threads_per_search':2,'document_slots':1}),))
        finally:c.close()
        self.path.chmod(0o600)

    @contextmanager
    def transaction(self):
        c = self.connect()
        try:
            c.execute('BEGIN IMMEDIATE')
            yield c
            c.execute('COMMIT')
        except BaseException:
            if not c.get_autocommit(): c.execute('ROLLBACK')
            raise
        finally:
            c.close()

    def all(self, sql, parameters=()):
        c = self.connect()
        try: return rows(c, sql, parameters)
        finally: c.close()

    def one(self, sql, parameters=()):
        result = self.all(sql,parameters)
        return result[0] if result else None

    def event(self, c, kind, payload, task_id=None):
        c.execute('INSERT INTO events(task_id,type,payload,created) VALUES(?,?,?,?)',
                  (task_id,kind,encode(payload),time.time()))
        # Durable current task state, not this bounded change feed, is authoritative.
        c.execute('DELETE FROM events WHERE seq < (SELECT COALESCE(MAX(seq),0)-5000 FROM events)')


def rows(c, sql, parameters=()):
    cursor = c.cursor().execute(sql, parameters)
    try: names = [col[0] for col in cursor.get_description()]
    except apsw.ExecutionCompleteError: return []
    return [dict(zip(names, row)) for row in cursor]


def one(c, sql, parameters=()):
    result = rows(c,sql,parameters)
    return result[0] if result else None


def require_fence(c, task_id, fence, *, allow_intent=False):
    task = one(c,'SELECT * FROM tasks WHERE id=?',(task_id,))
    if not task or task['fence']!=fence or task['status']!='RUNNING':
        raise Problem('ATTEMPT_EXPIRED','This task attempt no longer owns publication')
    if not allow_intent and task['intent']!='run':
        raise Problem('CANCELLED' if task['intent']=='cancel' else 'PAUSED_BY_USER','Task stopped before publication')
    return task


def task_record(kind, inputs, *, request_id=None, lesson_id=None, family_id=None, target_type='count', target_count=None, priority=20):
    now=time.time()
    return {'id':uuid.uuid4().hex,'kind':kind,'input_json':encode(inputs), 'request_id':request_id,
            'lesson_id':lesson_id,'family_id':family_id,'target_type':target_type,'target_count':target_count,
            'status':'QUEUED','stage':'Queued','priority':priority,'created':now,'updated':now}


def insert_task(c, task):
    columns=','.join(task)
    c.execute(f'INSERT INTO tasks({columns}) VALUES({",".join("?" for _ in task)})', tuple(task.values()))
