"""Host-local resource leases and durable executor reports, independent of runner IDs."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import contextlib

from parallel import protocol


class Ledger:
    def __init__(self, path: str, groups: list[int], gpu_uuids=()):
        if not isinstance(groups, list) or any(type(g) is not int for g in groups) or groups != list(range(len(groups))):
            raise protocol.Error('CPU groups must be consecutive quantity slots')
        if (any(not isinstance(card, str) or not protocol.GPU_UUID.fullmatch(card) for card in gpu_uuids)
                or len(set(gpu_uuids)) != len(gpu_uuids)):
            raise protocol.Error('invalid local GPU UUID list')
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
        try:
            self._configure(groups, gpu_uuids)
        except Exception:
            self.db.close()
            raise
        self.groups = groups
        self.gpu_uuids = frozenset(gpu_uuids)
        os.chmod(path, 0o600)

    def _configure(self, groups, gpu_uuids):
        with self.transaction() as db:
            active = db.execute("SELECT 1 FROM executions WHERE state!='released'").fetchone()
            if 'gpu_uuids' not in {r['name'] for r in db.execute('PRAGMA table_info(config)')}:
                if active or db.execute('SELECT 1 FROM reports WHERE delivered=0').fetchone():
                    raise protocol.Error('drain local allocations and deliver reports before upgrading')
                db.execute("ALTER TABLE config ADD COLUMN gpu_uuids TEXT NOT NULL DEFAULT '[]'")
            db.execute('CREATE TABLE IF NOT EXISTS held_gpus(uuid TEXT PRIMARY KEY, allocation TEXT UNIQUE NOT NULL)')
            old = db.execute('SELECT groups_json,gpu_uuids FROM config WHERE id=1').fetchone()
            encoded = protocol.dumps(groups)
            cards = protocol.dumps(sorted(gpu_uuids))
            if old and tuple(old) != (encoded, cards) and active:
                raise protocol.Error('drain local allocations before changing CPU groups or GPU inventory')
            db.execute('''INSERT INTO config(id,groups_json,gpu_uuids) VALUES(1,?,?)
                       ON CONFLICT(id) DO UPDATE SET groups_json=excluded.groups_json,gpu_uuids=excluded.gpu_uuids''',
                       (encoded, cards))

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
        grant = protocol.validate_allocation(grant)
        aid = grant['allocation_id']
        with self.transaction() as db:
            old = self.decode(db.execute('SELECT * FROM executions WHERE id=?', (aid,)).fetchone())
            if old:
                if old['grant'] != grant or old['metadata'] != metadata:
                    raise protocol.Error('allocation already exists with different parameters')
                return old
            held = {r[0] for r in db.execute('SELECT id FROM held_groups')}
            free = [i for i in range(len(self.groups)) if i not in held]
            need = grant['cpu_groups'] if grant['backend'] == 'local' else 0
            if len(free) < need:
                raise protocol.Error('local CPU groups do not match the central reservation')
            chosen = free[:need]
            cpus = []  # Retained database field for compatibility; no CPU IDs are assigned.
            card = grant.get('gpu_uuid')
            if card is not None:
                if card not in self.gpu_uuids or db.execute('SELECT 1 FROM held_gpus WHERE uuid=?', (card,)).fetchone():
                    raise protocol.Error('authorized GPU is not available in the local inventory')
                db.execute('INSERT INTO held_gpus VALUES(?,?)', (card, aid))
            db.execute('INSERT INTO executions(id,grant_json,metadata,cpus,state) VALUES(?,?,?,?,?)',
                       (aid, protocol.dumps(grant), protocol.dumps(metadata), protocol.dumps(cpus), 'reserved'))
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
                raise protocol.Error('unknown local allocation')
            if row[0] == 'released':
                return
            db.execute("UPDATE executions SET state='allocated' WHERE id=?", (aid,))
            db.execute('INSERT OR IGNORE INTO reports(id,allocation,kind,result) VALUES(?,?,?,?)',
                       (f'{aid}-allocated', aid, 'allocated', '{}'))

    def cancel(self, aid):
        with self.transaction() as db:
            db.execute('UPDATE executions SET cancelled=1 WHERE id=?', (aid,))

    def cancel_or_close_unstarted(self, grant, result):
        """Fence a late start even when the granted hardware is no longer present."""
        grant = protocol.validate_allocation(grant)
        aid = grant['allocation_id']
        with self.transaction() as db:
            existing = db.execute('SELECT grant_json FROM executions WHERE id=?', (aid,)).fetchone()
            if existing:
                if json.loads(existing[0]) != grant:
                    raise protocol.Error('cancelled grant differs from the local lease')
                db.execute("UPDATE executions SET cancelled=1 WHERE id=? AND state!='released'", (aid,))
                return
            encoded = protocol.dumps(result)
            db.execute('''INSERT INTO executions(id,grant_json,metadata,cpus,state,cancelled,result)
                       VALUES(?,?,'{}','[]','released',1,?)''', (aid, protocol.dumps(grant), encoded))
            db.execute('INSERT INTO reports(id,allocation,kind,result) VALUES(?,?,?,?)',
                       (f'{aid}-released', aid, 'released', encoded))

    def release(self, aid: str, result: dict, *, cleaned: bool, verify=None):
        """The caller must first prove that the scope/remote job and worker have ended."""
        if cleaned is not True or result.get('state') not in protocol.TERMINAL:
            raise protocol.Error('cannot release before process cleanup and a terminal result')
        with self.transaction() as db:
            row = db.execute('SELECT state,result FROM executions WHERE id=?', (aid,)).fetchone()
            if row is None:
                raise protocol.Error('unknown local allocation')
            if row[0] == 'released':
                if json.loads(row[1]) != result:
                    raise protocol.Error('conflicting release result')
                return
            # Local launch checks take this same SQLite write lock from inside the
            # scope. A delayed scope creation cannot start tests after release.
            if verify is not None and not verify():
                return
            encoded = protocol.dumps(result)
            db.execute("UPDATE executions SET state='released',result=? WHERE id=?", (encoded, aid))
            db.execute('DELETE FROM held_groups WHERE allocation=?', (aid,))
            db.execute('DELETE FROM held_gpus WHERE allocation=?', (aid,))
            db.execute('INSERT INTO reports(id,allocation,kind,result) VALUES(?,?,?,?)',
                       (f'{aid}-released', aid, 'released', encoded))

    def reports(self):
        with self.lock:
            return [{**dict(r), 'result': json.loads(r['result'])} for r in self.db.execute('SELECT * FROM reports WHERE delivered=0 ORDER BY rowid')]

    def delivered(self, ident):
        with self.transaction() as db:
            self.db.execute('UPDATE reports SET delivered=1 WHERE id=?', (ident,))
