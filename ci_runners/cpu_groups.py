"""Host-local CPU mappings and durable executor reports, independent of runner IDs."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import contextlib

from ci_runners import central, inventory


def split(cpus: str, count: int) -> list[list[int]]:
    ranges = inventory.cpu_ranges(cpus)
    if ranges is None or not central.positive(count):
        raise central.Error('invalid CI CPU list or group count')
    values = [i for lo, hi in ranges for i in range(lo, hi+1)]
    if len(values) != len(set(values)) or len(values) != count * central.GROUP_CPUS:
        raise central.Error('CI CPU list must contain exactly four distinct logical CPUs per group')
    values.sort()
    return [values[i:i+4] for i in range(0, len(values), 4)]


class Ledger:
    def __init__(self, path: str, groups: list[list[int]]):
        flat = [c for group in groups for c in group]
        if (any(len(group) != 4 for group in groups) or len(set(flat)) != len(flat)
                or any(type(c) is not int or c < 0 or c > inventory.MAX_CPU for c in flat)):
            raise central.Error('CPU groups must contain four unique logical CPUs each')
        os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS config(id INTEGER PRIMARY KEY CHECK(id=1), groups_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS executions(
                id TEXT PRIMARY KEY, grant_json TEXT NOT NULL, metadata TEXT NOT NULL, cpus TEXT NOT NULL,
                state TEXT NOT NULL, pid INTEGER, started REAL, record TEXT NOT NULL DEFAULT '', cancelled INTEGER NOT NULL DEFAULT 0,
                result TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS held_groups(id INTEGER PRIMARY KEY, allocation TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reports(
                id TEXT PRIMARY KEY, allocation TEXT NOT NULL, kind TEXT NOT NULL,
                result TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0);
        ''')
        with self.transaction() as db:
            old = db.execute('SELECT groups_json FROM config WHERE id=1').fetchone()
            encoded = central.dumps(groups)
            if old and old[0] != encoded and db.execute("SELECT 1 FROM executions WHERE state!='released'").fetchone():
                raise central.Error('drain local allocations before changing CPU groups')
            db.execute('INSERT INTO config VALUES(1,?) ON CONFLICT(id) DO UPDATE SET groups_json=excluded.groups_json', (encoded,))
        self.groups = groups
        os.chmod(path, 0o600)

    def close(self):
        self.db.close()

    @contextlib.contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield self.db
            except BaseException:
                self.db.rollback()
                raise
            else:
                self.db.commit()

    @staticmethod
    def decode(row):
        if row is None:
            return None
        return {**dict(row), 'grant': json.loads(row['grant_json']), 'metadata': json.loads(row['metadata']),
                'cpus': json.loads(row['cpus']), 'result': json.loads(row['result'])}

    def get(self, aid):
        with self.lock:
            return self.decode(self.db.execute('SELECT * FROM executions WHERE id=?', (aid,)).fetchone())

    def active(self):
        with self.lock:
            return [self.decode(r) for r in self.db.execute("SELECT * FROM executions WHERE state!='released'")]

    def reserve(self, grant: dict, metadata: dict) -> dict:
        grant = central.validate_allocation(grant)
        aid = grant['allocation_id']
        with self.transaction() as db:
            old = self.decode(db.execute('SELECT * FROM executions WHERE id=?', (aid,)).fetchone())
            if old:
                if old['grant'] != grant or old['metadata'] != metadata:
                    raise central.Error('allocation already exists with different parameters')
                return old
            held = {r[0] for r in db.execute('SELECT id FROM held_groups')}
            free = [i for i in range(len(self.groups)) if i not in held]
            need = grant['cpu_groups'] if grant['backend'] == 'local' else 0
            if len(free) < need:
                raise central.Error('local CPU groups do not match the central reservation')
            chosen = free[:need]
            cpus = [c for i in chosen for c in self.groups[i]]
            db.execute('INSERT INTO executions(id,grant_json,metadata,cpus,state) VALUES(?,?,?,?,?)',
                       (aid, central.dumps(grant), central.dumps(metadata), central.dumps(cpus), 'reserved'))
            db.executemany('INSERT INTO held_groups VALUES(?,?)', [(i, aid) for i in chosen])
            return self.decode(db.execute('SELECT * FROM executions WHERE id=?', (aid,)).fetchone())

    def start_worker(self, aid, pid, started, record) -> bool:
        """An atomic launch claim, made by the worker before it starts a supervisor."""
        with self.transaction() as db:
            changed = db.execute("UPDATE executions SET pid=?,started=?,record=?,state='starting' WHERE id=? AND state='reserved' AND pid IS NULL AND cancelled=0",
                                 (pid, started, record, aid)).rowcount
            return bool(changed)

    def allocated(self, aid):
        with self.transaction() as db:
            row = db.execute('SELECT state FROM executions WHERE id=?', (aid,)).fetchone()
            if row is None:
                raise central.Error('unknown local allocation')
            if row[0] == 'released':
                return
            db.execute("UPDATE executions SET state='allocated' WHERE id=?", (aid,))
            db.execute('INSERT OR IGNORE INTO reports(id,allocation,kind,result) VALUES(?,?,?,?)',
                       (f'{aid}-allocated', aid, 'allocated', '{}'))

    def cancel(self, aid):
        with self.transaction() as db:
            db.execute('UPDATE executions SET cancelled=1 WHERE id=?', (aid,))

    def release(self, aid: str, result: dict, *, cleaned: bool, verify=None):
        """The caller must first prove that the scope/remote job and worker have ended."""
        if cleaned is not True or result.get('state') not in central.TERMINAL:
            raise central.Error('cannot release before process cleanup and a terminal result')
        with self.transaction() as db:
            row = db.execute('SELECT state,result FROM executions WHERE id=?', (aid,)).fetchone()
            if row is None:
                raise central.Error('unknown local allocation')
            if row[0] == 'released':
                if json.loads(row[1]) != result:
                    raise central.Error('conflicting release result')
                return
            # Local launch checks take this same SQLite write lock from inside the
            # scope. A delayed scope creation cannot start tests after release.
            if verify is not None and not verify():
                return
            encoded = central.dumps(result)
            db.execute("UPDATE executions SET state='released',result=? WHERE id=?", (encoded, aid))
            db.execute('DELETE FROM held_groups WHERE allocation=?', (aid,))
            db.execute('INSERT INTO reports(id,allocation,kind,result) VALUES(?,?,?,?)',
                       (f'{aid}-released', aid, 'released', encoded))

    def reports(self):
        with self.lock:
            return [{**dict(r), 'result': json.loads(r['result'])} for r in self.db.execute('SELECT * FROM reports WHERE delivered=0 ORDER BY rowid')]

    def delivered(self, ident):
        with self.transaction() as db:
            self.db.execute('UPDATE reports SET delivered=1 WHERE id=?', (ident,))
