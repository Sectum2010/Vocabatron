"""Bounded, linear source-fragment matching for the private publication audit."""
from collections import deque
import json
from pathlib import Path
import re
import hashlib
from .domain import Problem


class Fingerprints:
    def __init__(self,values):
        self.nodes=[{}];self.fail=[0];self.hit=[False]
        unique=set(filter(None,values))
        if len(unique)>200000 or sum(map(len,unique))>8*1024**2:
            raise Problem('PRIVACY_COVERAGE_LIMIT','Private fingerprint coverage exceeds its explicit bound')
        for text in unique:
            node=0
            for char in text:
                if char not in self.nodes[node]:
                    self.nodes[node][char]=len(self.nodes);self.nodes.append({});self.fail.append(0);self.hit.append(False)
                node=self.nodes[node][char]
            self.hit[node]=True
        queue=deque(self.nodes[0].values())
        while queue:
            node=queue.popleft()
            for char,next_node in self.nodes[node].items():
                fallback=self.fail[node]
                while fallback and char not in self.nodes[fallback]:fallback=self.fail[fallback]
                self.fail[next_node]=self.nodes[fallback].get(char,0)
                self.hit[next_node]|=self.hit[self.fail[next_node]];queue.append(next_node)
        self.count=len(unique)

    def matches(self,text):
        node=0
        # JSON/JavaScript source often escapes line endings. Source fingerprints
        # normalize only whitespace; punctuation and letter order stay exact.
        for char in re.sub(r'\s+',' ',text.replace('\\n',' ').replace('\\r',' ').replace('\\t',' ')):
            while node and char not in self.nodes[node]:node=self.fail[node]
            node=self.nodes[node].get(char,0)
            if self.hit[node]:return True
        return False


def source_fingerprints(project,known_values):
    values=[re.sub(r'\s+',' ',v) for v in known_values if v];coverage={'library_lessons':0,'source_strings':0,'source_bytes':0}
    config=Path(project)/'.private/web/config.json'
    if config.is_file():
        value=json.loads(config.read_text());database=Path(value['database']);root=Path(value['data_root'])
        if not database.is_absolute() or database.resolve()!=database or not database.is_relative_to(root):
            raise Problem('PRIVACY_COVERAGE_INVALID','Invalid private library location')
        if database.exists():
            import apsw
            connection=apsw.Connection(str(database),flags=apsw.SQLITE_OPEN_READONLY)
            connection.set_busy_timeout(5000)
            try:
                connection.execute('BEGIN')
                for canonical,lesson_ref in connection.execute('SELECT canonical,lesson_json FROM lessons'):
                    coverage['library_lessons']+=1;coverage['source_bytes']+=len(canonical.encode())
                    if coverage['source_bytes']>64*1024**2:raise Problem('PRIVACY_COVERAGE_LIMIT','Private source coverage exceeds its bound')
                    def visit(item):
                        if isinstance(item,str):
                            text=re.sub(r'\s+',' ',item).strip()
                            if len(text)>=40:
                                size=min(48,len(text));coverage['source_strings']+=1
                                for start in {0,(len(text)-size)//2,len(text)-size}:values.append(text[start:start+size])
                        elif isinstance(item,dict):
                            for v in item.values():visit(v)
                        elif isinstance(item,list):
                            for v in item:visit(v)
                    visit(json.loads(canonical))
                    if not re.fullmatch(r'[a-f0-9]{2}/[a-f0-9]{64}\.json',lesson_ref):
                        raise Problem('PRIVACY_COVERAGE_INVALID','Invalid saved lesson reference')
                    snapshot=root/'objects'/lesson_ref
                    if snapshot.resolve()!=snapshot or snapshot.stat().st_size>16*1024**2:
                        raise Problem('PRIVACY_COVERAGE_LIMIT','Saved lesson metadata cannot be safely covered')
                    raw=snapshot.read_bytes()
                    if hashlib.sha256(raw).hexdigest()!=snapshot.stem:
                        raise Problem('PRIVACY_COVERAGE_INVALID','Saved source metadata failed its integrity check')
                    # Headings are retained as source evidence but deliberately
                    # excluded from the packaging-independent content identity.
                    visit(json.loads(raw).get('title',[]))
                connection.execute('ROLLBACK')
            finally:connection.close()
    matcher=Fingerprints(values)
    return matcher,{**coverage,'fingerprints':matcher.count,'method':'All library lessons including archived, canonical fields and immutable source headings; first/middle/last 48-character windows from source strings of at least 40 characters; whitespace normalized; Aho-Corasick linear scan',
                    'limitations':'Short strings and arbitrarily rewritten source content are not claimed to be detected'}
