"""Durable scheduling ledger. All resource decisions are short SQLite transactions.

The network outbox commits with the allocation. A lost acknowledgement never makes
an allocation free. CPU release and the completion of its GitHub job are independent.
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Mapping

from ci_runners import central

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(version INTEGER NOT NULL);
INSERT INTO meta SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM meta);
CREATE TABLE IF NOT EXISTS pools(name TEXT PRIMARY KEY, total INTEGER NOT NULL, enabled INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS hosts(name TEXT PRIMARY KEY, seen REAL NOT NULL, drained INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS runners(
 tag TEXT PRIMARY KEY, pool TEXT NOT NULL REFERENCES pools(name), host TEXT NOT NULL,
 online INTEGER NOT NULL DEFAULT 0, busy INTEGER NOT NULL DEFAULT 1, seen REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS batches(
 id TEXT PRIMARY KEY, source TEXT NOT NULL, digest TEXT NOT NULL, created REAL NOT NULL,
 last_scheduled REAL NOT NULL DEFAULT 0, state TEXT NOT NULL, max_parallel INTEGER NOT NULL,
 retries INTEGER NOT NULL, cancel_key TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(
 id TEXT PRIMARY KEY, batch TEXT NOT NULL REFERENCES batches(id), part TEXT NOT NULL,
 eligible TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL, deadline REAL NOT NULL,
 wait_remaining REAL NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, result TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS allocations(
 id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id), pool TEXT NOT NULL,
 runner TEXT NOT NULL, host TEXT NOT NULL, groups INTEGER NOT NULL CHECK(groups > 0),
 state TEXT NOT NULL, run_id INTEGER, run_attempt INTEGER, created REAL NOT NULL, updated REAL NOT NULL,
 released INTEGER NOT NULL DEFAULT 0, runner_done INTEGER NOT NULL DEFAULT 0,
 allocated_at REAL, conclusion TEXT, artifacts TEXT NOT NULL DEFAULT '[]',
 cancelled INTEGER NOT NULL DEFAULT 0, result TEXT NOT NULL DEFAULT '{}');
CREATE UNIQUE INDEX IF NOT EXISTS one_runner_allocation ON allocations(runner) WHERE runner_done = 0;
CREATE UNIQUE INDEX IF NOT EXISTS one_task_allocation ON allocations(task) WHERE runner_done = 0 OR released = 0;
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, allocation TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notices(id TEXT PRIMARY KEY, source TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox(
 id INTEGER PRIMARY KEY AUTOINCREMENT, dedupe TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
 payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 available REAL NOT NULL, error TEXT NOT NULL DEFAULT '');
"""


class Store:
    def __init__(self, path: str, *, clock=time.time):
        self.clock = clock
        if path != ':memory:':
            os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript(SCHEMA)
            if self.db.execute('SELECT version FROM meta').fetchone()[0] != 1:
                raise central.Error('unsupported scheduler database version')
        if path != ':memory:':
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

    def _enqueue(self, db, key, kind, payload):
        db.execute('INSERT OR IGNORE INTO outbox(dedupe,kind,payload,available) VALUES(?,?,?,?)',
                   (key, kind, central.dumps(payload), self.clock()))

    def configure(self, pools: Mapping[str, int], runners: list[dict]):
        """Install explicit pool/driver mappings; refuse destructive changes while busy."""
        for name, total in pools.items():
            if name not in central.DEVICES or not central.positive(total) or total > central.DEVICE_GROUPS[name]:
                raise central.Error('invalid pool capacity')
        names = set()
        for r in runners:
            if (r.get('pool') not in pools or r.get('tag') not in central.runner_names(r['pool'])
                    or not isinstance(r.get('host'), str) or not central.ID.fullmatch(r['host'])
                    or r['tag'] in names):
                raise central.Error('invalid or duplicate runner mapping')
            names.add(r['tag'])
        if any(sum(r['pool'] == p for r in runners) != total for p, total in pools.items()):
            raise central.Error('register one runner per group')
        if any(len({r['host'] for r in runners if r['pool'] == p}) != 1 for p in pools if p != 'slurm'):
            raise central.Error('a local device must belong to one physical host')
        with self.transaction() as db:
            for a in db.execute('SELECT * FROM allocations WHERE released=0 OR runner_done=0'):
                match = next((r for r in runners if r['tag'] == a['runner']), None)
                if not match or match['host'] != a['host'] or match['pool'] != a['pool']:
                    raise central.Error('drain active allocations before changing runner mappings')
            for p, total in pools.items():
                used = db.execute('SELECT COALESCE(SUM(groups),0) FROM allocations WHERE pool=? AND released=0', (p,)).fetchone()[0]
                if used > total:
                    raise central.Error('cannot shrink a pool below its reservations')
            db.execute('UPDATE pools SET enabled=0')
            for p, total in pools.items():
                db.execute('INSERT INTO pools VALUES(?,?,1) ON CONFLICT(name) DO UPDATE SET total=excluded.total,enabled=1', (p, total))
            db.execute('UPDATE runners SET online=0,busy=1')
            for old in db.execute('SELECT tag FROM runners').fetchall():
                if old['tag'] not in names:
                    db.execute('DELETE FROM runners WHERE tag=?', (old['tag'],))
            for r in runners:
                db.execute('INSERT INTO runners(tag,pool,host) VALUES(?,?,?) ON CONFLICT(tag) DO UPDATE SET pool=excluded.pool,host=excluded.host',
                           (r['tag'], r['pool'], r['host']))

    def heartbeat(self, host: str, runners: list[dict], *, drained: bool = False):
        """Called only for an authenticated host; it cannot report another host's runners."""
        if type(drained) is not bool or not isinstance(runners, list):
            raise central.Error('invalid heartbeat')
        with self.transaction() as db:
            known = {r[0] for r in db.execute('SELECT tag FROM runners WHERE host=?', (host,))}
            if not known:
                raise central.Error('unknown execution host')
            seen = set()
            for r in runners:
                if (not isinstance(r, dict) or r.get('tag') not in known or r['tag'] in seen
                        or type(r.get('online')) is not bool or type(r.get('busy')) is not bool):
                    raise central.Error('heartbeat names an invalid runner')
                seen.add(r['tag'])
                db.execute('UPDATE runners SET online=?,busy=?,seen=? WHERE tag=?',
                           (r['online'], r['busy'], self.clock(), r['tag']))
            for missing in known - seen:
                db.execute('UPDATE runners SET online=0 WHERE tag=?', (missing,))
            db.execute('INSERT INTO hosts VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET seen=excluded.seen,drained=excluded.drained',
                       (host, self.clock(), drained))

    def submit(self, source: dict, plan: dict, eligible: Mapping[str, list[str]], *, max_parallel=16, retries=1) -> str:
        plan = central.load_plan(plan)
        key = central.source_key(source)
        if not central.positive(max_parallel) or retries not in (0, 1):
            raise central.Error('invalid project limits')
        for p in plan['parts']:
            choices = eligible.get(p['id'], [])
            if (not choices or any(d not in central.DEVICES or p['device'] and p['device'] != d
                                   or p['cores'] > central.DEVICE_GROUPS[d] * 4 for d in choices)):
                raise central.Error(f'{p["id"]}: no eligible device satisfies the request')
        digest = central.fingerprint([source, plan, eligible, max_parallel, retries])
        cancel_key = central.dumps([source['repository'].lower(), source['context'], source['concurrency_key']])
        now = self.clock()
        with self.transaction() as db:
            previous = db.execute('SELECT digest FROM batches WHERE id=?', (key,)).fetchone()
            if previous:
                if previous[0] != digest:
                    raise central.Error('source batch already submitted with different content')
                return key
            # A late old submission must not cancel a newer PR batch.
            older, superseded = [], False
            if source.get('cancel_in_progress'):
                order = (source['run_id'], source['run_attempt'])
                for row in db.execute('SELECT id,source FROM batches WHERE cancel_key=?', (cancel_key,)).fetchall():
                    src = json.loads(row['source'])
                    if (src['run_id'], src['run_attempt']) > order:
                        superseded = True
                    else:
                        older.append(row['id'])
            db.execute('INSERT INTO batches(id,source,digest,created,state,max_parallel,retries,cancel_key) VALUES(?,?,?,?,?,?,?,?)',
                       (key, central.dumps(source), digest, now, 'pending', min(max_parallel, plan['max_parallel']), retries, cancel_key))
            for p in plan['parts']:
                task = central.fingerprint([key, p['id']])[:32]
                db.execute('INSERT INTO tasks(id,batch,part,eligible,state,created,deadline,wait_remaining) VALUES(?,?,?,?,?,?,?,?)',
                           (task, key, central.dumps(p), central.dumps(eligible[p['id']]), 'queued', now,
                            now + p['queue_minutes'] * 60, p['queue_minutes'] * 60))
            db.execute("UPDATE notices SET state='accepted' WHERE id=?", (key,))
            self._enqueue(db, f'pending:{key}', 'status', {'batch_id': key, 'state': 'pending'})
            for old in older:
                self._cancel(db, old)
            if superseded:
                self._cancel(db, key)
            self._batch_end(db, key)
        return key

    def _cancel(self, db, batch):
        db.execute("UPDATE tasks SET state='cancelled',result=? WHERE batch=? AND state='queued'",
                   (central.dumps({'state': 'cancelled', 'reason': 'source cancelled'}), batch))
        for a in db.execute('SELECT a.* FROM allocations a JOIN tasks t ON a.task=t.id WHERE t.batch=? AND (a.released=0 OR a.runner_done=0)', (batch,)).fetchall():
            db.execute('UPDATE allocations SET cancelled=1 WHERE id=?', (a['id'],))
            self._enqueue(db, f'cancel:{a["id"]}', 'cancel', {'allocation_id': a['id']})
        self._batch_end(db, batch)

    def cancel(self, batch: str):
        with self.transaction() as db:
            if not db.execute('SELECT 1 FROM batches WHERE id=?', (batch,)).fetchone():
                raise central.Error('unknown batch')
            self._cancel(db, batch)

    def reserve(self, *, heartbeat_ttl=120) -> dict | None:
        now = self.clock()
        with self.transaction() as db:
            expired = db.execute("SELECT id,batch FROM tasks WHERE state='queued' AND deadline<=?", (now,)).fetchall()
            for t in expired:
                db.execute("UPDATE tasks SET state='error',result=? WHERE id=?", (central.dumps({'state': 'error', 'reason': 'queue deadline exceeded'}), t['id']))
                self._batch_end(db, t['batch'])
            batches = db.execute("SELECT * FROM batches WHERE state='pending' ORDER BY last_scheduled,created,id").fetchall()
            for b in batches:
                active = db.execute("SELECT COUNT(*) FROM tasks WHERE batch=? AND state IN ('reserved','running','finishing')", (b['id'],)).fetchone()[0]
                if active >= b['max_parallel']:
                    continue
                for t in db.execute("SELECT * FROM tasks WHERE batch=? AND state='queued' ORDER BY created,id", (b['id'],)).fetchall():
                    p = json.loads(t['part'])
                    groups = p['cores'] // 4
                    choices = sorted(json.loads(t['eligible']), key=lambda d: (d == 'slurm', d))
                    for device in choices:
                        pool = db.execute('SELECT total FROM pools WHERE name=? AND enabled=1', (device,)).fetchone()
                        used = db.execute('SELECT COALESCE(SUM(groups),0) FROM allocations WHERE pool=? AND released=0', (device,)).fetchone()[0]
                        if not pool or pool[0] - used < groups:
                            continue
                        r = db.execute('''SELECT r.* FROM runners r JOIN hosts h ON r.host=h.name
                            WHERE r.pool=? AND r.online=1 AND r.busy=0 AND r.seen>? AND h.seen>? AND h.drained=0
                            AND NOT EXISTS(SELECT 1 FROM allocations a WHERE a.runner=r.tag AND a.runner_done=0)
                            ORDER BY r.tag LIMIT 1''', (device, now-heartbeat_ttl, now-heartbeat_ttl)).fetchone()
                        if not r:
                            continue
                        aid = uuid.uuid4().hex
                        db.execute('INSERT INTO allocations(id,task,pool,runner,host,groups,state,created,updated) VALUES(?,?,?,?,?,?,?,?,?)',
                                   (aid, t['id'], device, r['tag'], r['host'], groups, 'reserved', now, now))
                        db.execute("UPDATE tasks SET state='reserved',attempts=attempts+1 WHERE id=?", (t['id'],))
                        # Monotonic service order also works when several allocations share a clock tick.
                        sequence = db.execute('SELECT COALESCE(MAX(last_scheduled),0) FROM batches').fetchone()[0]
                        db.execute('UPDATE batches SET last_scheduled=? WHERE id=?', (max(now, sequence + .000001), b['id']))
                        self._enqueue(db, f'dispatch:{aid}', 'dispatch', {'allocation_id': aid})
                        return self._allocation(db, aid)
        return None

    def _allocation(self, db, aid):
        a = db.execute('SELECT * FROM allocations WHERE id=?', (aid,)).fetchone()
        if not a:
            raise central.Error('unknown allocation')
        t = db.execute('SELECT * FROM tasks WHERE id=?', (a['task'],)).fetchone()
        b = db.execute('SELECT * FROM batches WHERE id=?', (t['batch'],)).fetchone()
        p, source = json.loads(t['part']), json.loads(b['source'])
        grant = {'allocation_schema': 1, 'allocation_id': aid, 'task_id': t['id'], 'host': a['host'],
                 'requested_device': p['device'], 'device': a['pool'], 'backend': 'slurm' if a['pool'] == 'slurm' else 'local',
                 'runner_tag': a['runner'], 'cpu_groups': a['groups'], 'cores': a['groups'] * 4, 'RAM': f'{a["groups"] * 4}G',
                 'workers': p['workers'], 'minutes': p['minutes'], 'queue_minutes': p['queue_minutes']}
        grant['job_timeout_minutes'] = math.ceil(p['minutes'] + p['queue_minutes'] + 30)
        return {**dict(a), 'grant': grant, 'part': p, 'source': source, 'batch_id': b['id'],
                'deadline': t['deadline'], 'result': json.loads(a['result'])}

    def allocation(self, aid):
        with self.lock:
            return self._allocation(self.db, aid)

    def allocations(self, *, host: str | None = None, active=True) -> list[dict]:
        with self.lock:
            clauses, args = [], []
            if active:
                clauses.append('(released=0 OR runner_done=0)')
            if host is not None:
                clauses.append('host=?')
                args.append(host)
            ids = self.db.execute('SELECT id FROM allocations' + (' WHERE ' + ' AND '.join(clauses) if clauses else ''), args).fetchall()
            return [self._allocation(self.db, row[0]) for row in ids]

    def claim(self, aid: str, host: str, runner: str, repository: str, run_id: int, run_attempt: int) -> dict:
        if not central.positive(run_id) or not central.positive(run_attempt):
            raise central.Error('invalid execution run identity')
        with self.transaction() as db:
            a = self._allocation(db, aid)
            if (a['host'] != host or a['runner'] != runner or a['source']['repository'].lower() != repository.lower()
                    or a['cancelled'] or a['released'] or a['runner_done']):
                raise central.Error('allocation is closed or bound to another executor')
            if a['run_id'] is not None and (a['run_id'], a['run_attempt']) != (run_id, run_attempt):
                raise central.Error('allocation already belongs to another execution attempt')
            if a['state'] == 'reserved' and self.clock() >= a['deadline']:
                raise central.Error('allocation queue deadline exceeded')
            db.execute('UPDATE allocations SET run_id=?,run_attempt=?,updated=? WHERE id=?', (run_id, run_attempt, self.clock(), aid))
            return self._allocation(db, aid)

    def event(self, host: str, event_id: str, aid: str, kind: str, result: dict | None = None):
        if not isinstance(event_id, str) or not central.ID.fullmatch(event_id):
            raise central.Error('invalid event identity')
        result = {} if result is None else result
        digest = central.fingerprint([host, aid, kind, result])
        with self.transaction() as db:
            a = self._allocation(db, aid)
            if a['host'] != host:
                raise central.Error('event is from another host')
            previous = db.execute('SELECT digest FROM events WHERE id=?', (event_id,)).fetchone()
            if previous:
                if previous[0] != digest:
                    raise central.Error('event identity reused with different content')
                return
            if kind == 'allocated':
                if not a['released'] and not a['cancelled'] and a['allocated_at'] is None:
                    if a['run_id'] is None:
                        raise central.Error('allocation must be claimed before acknowledgement')
                    db.execute("UPDATE allocations SET state='allocated',updated=?,allocated_at=? WHERE id=?", (self.clock(), self.clock(), aid))
                    db.execute("UPDATE tasks SET state='running',wait_remaining=MAX(0,deadline-?) WHERE id=?", (self.clock(), a['task']))
            elif kind == 'released':
                if not isinstance(result, dict) or result.get('state') not in central.TERMINAL:
                    raise central.Error('release needs a terminal execution result')
                if not a['released']:
                    db.execute("UPDATE allocations SET released=1,state='released',result=?,updated=? WHERE id=?",
                               (central.dumps(result), self.clock(), aid))
                    db.execute("UPDATE tasks SET state='finishing' WHERE id=?", (a['task'],))
                elif a['result'] != result and not (a['runner_done'] and a['run_id'] is None):
                    raise central.Error('allocation already released with another result')
            else:
                raise central.Error('unknown allocation event')
            db.execute('INSERT INTO events VALUES(?,?,?)', (event_id, aid, digest))
            self._finish(db, aid)

    def runner_finished(self, aid: str, run_id: int, run_attempt: int, *, conclusion='success', artifacts=()):
        """Only the trusted GitHub reconciler calls this after observing a completed run."""
        with self.transaction() as db:
            a = self._allocation(db, aid)
            if (a['run_id'], a['run_attempt']) != (run_id, run_attempt):
                raise central.Error('completion is for another execution attempt')
            db.execute('UPDATE allocations SET runner_done=1,updated=?,conclusion=?,artifacts=? WHERE id=?',
                       (self.clock(), conclusion, central.dumps(list(artifacts)), aid))
            self._finish(db, aid)

    def bind_run(self, aid: str, run_id: int, run_attempt: int):
        with self.transaction() as db:
            a = self._allocation(db, aid)
            if a['run_id'] is not None and (a['run_id'], a['run_attempt']) != (run_id, run_attempt):
                raise central.Error('another execution run already owns this allocation')
            db.execute('UPDATE allocations SET run_id=?,run_attempt=? WHERE id=?', (run_id, run_attempt, aid))

    def batches(self, *, pending=False):
        with self.lock:
            return [{**dict(r), 'source': json.loads(r['source'])} for r in self.db.execute(
                'SELECT * FROM batches' + (" WHERE state='pending'" if pending else ''))]

    def is_latest_status(self, batch: dict) -> bool:
        s = batch['source']
        for b in self.batches() + self.notices():
            other = b['source']
            if ([other['repository'].lower(), other['sha'], other['context']]
                    == [s['repository'].lower(), s['sha'], s['context']]
                    and (other['run_id'], other['run_attempt']) > (s['run_id'], s['run_attempt'])):
                return False
        return True

    def notice(self, source: dict, state: str, reason: str):
        key = central.source_key(source)
        if state not in {'pending', 'success', 'failure', 'error'}:
            raise central.Error('invalid submission status')
        with self.transaction() as db:
            old = db.execute('SELECT state FROM notices WHERE id=?', (key,)).fetchone()
            if old and (old[0] == 'accepted' or state == 'pending' and old[0] != 'pending'):
                return
            db.execute('INSERT INTO notices VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,reason=excluded.reason',
                       (key, central.dumps(source), state, reason))
            self._enqueue(db, f'notice:{key}:{state}', 'source_status', {'source': source, 'state': state, 'reason': reason})

    def notices(self, *, pending=False):
        with self.lock:
            return [{**dict(r), 'source': json.loads(r['source'])} for r in self.db.execute(
                'SELECT * FROM notices' + (" WHERE state='pending'" if pending else ''))]

    def expire(self, aid):
        """Cancel only the allocation whose queue budget expired; never release on a timer."""
        with self.transaction() as db:
            a = self._allocation(db, aid)
            if a['allocated_at'] is None and self.clock() >= a['deadline']:
                db.execute('UPDATE allocations SET cancelled=1 WHERE id=?', (aid,))
                self._enqueue(db, f'cancel:{aid}', 'cancel', {'allocation_id': aid})

    def dispatch_aborted(self, aid: str):
        """Close work whose outbox has demonstrably never sent a dispatch request."""
        with self.transaction() as db:
            a = self._allocation(db, aid)
            msg = db.execute('SELECT attempts,state FROM outbox WHERE dedupe=?', (f'dispatch:{aid}',)).fetchone()
            if a['run_id'] is not None or not msg or msg['attempts'] > 1 or msg['state'] != 'sending':
                raise central.Error('cannot prove dispatch was never sent')
            result = {'state': 'cancelled' if a['cancelled'] else 'error', 'reason': 'not dispatched'}
            db.execute("UPDATE allocations SET released=1,runner_done=1,state='released',result=? WHERE id=?", (central.dumps(result), aid))
            self._finish(db, aid)

    def _finish(self, db, aid):
        a = self._allocation(db, aid)
        if not (a['released'] and a['runner_done']):
            return
        t = db.execute('SELECT * FROM tasks WHERE id=?', (a['task'],)).fetchone()
        if t['state'] in central.TERMINAL or t['state'] == 'queued':
            return
        result = dict(a['result'])
        if a['conclusion'] and a['conclusion'] != 'success' and result['state'] == 'success':
            result = {**result, 'state': 'error', 'reason': 'execution workflow or artifact upload failed', 'retryable': False}
        if a['cancelled']:
            result = {**result, 'state': 'cancelled'}
        b = db.execute('SELECT retries FROM batches WHERE id=?', (t['batch'],)).fetchone()
        remaining = t['wait_remaining'] if a['allocated_at'] is not None else max(0, t['deadline']-self.clock())
        retry = (result.get('retryable') is True and result['state'] == 'error' and not a['cancelled']
                 and t['attempts'] <= b[0] and remaining > 0)
        db.execute('UPDATE tasks SET state=?,result=?,deadline=?,wait_remaining=? WHERE id=?',
                   ('queued' if retry else result['state'], central.dumps(result), self.clock()+remaining, remaining, t['id']))
        self._batch_end(db, t['batch'])

    def _batch_end(self, db, batch):
        b = db.execute('SELECT state FROM batches WHERE id=?', (batch,)).fetchone()
        if not b or b[0] != 'pending':
            return
        states = {r[0] for r in db.execute('SELECT state FROM tasks WHERE batch=?', (batch,))}
        if not states <= central.TERMINAL:
            return
        state = next((s for s in ('failure', 'error', 'cancelled') if s in states), 'success')
        db.execute('UPDATE batches SET state=? WHERE id=?', (state, batch))
        self._enqueue(db, f'final:{batch}', 'status', {'batch_id': batch, 'state': state})

    def batch(self, key):
        with self.lock:
            b = self.db.execute('SELECT * FROM batches WHERE id=?', (key,)).fetchone()
            if not b:
                raise central.Error('unknown batch')
            return {**dict(b), 'source': json.loads(b['source']),
                    'tasks': [{**dict(t), 'part': json.loads(t['part']), 'result': json.loads(t['result'])}
                              for t in self.db.execute('SELECT * FROM tasks WHERE batch=? ORDER BY id', (key,))],
                    'allocations': [{**dict(a), 'result': json.loads(a['result']), 'artifacts': json.loads(a['artifacts'])}
                                    for a in self.db.execute('SELECT a.* FROM allocations a JOIN tasks t ON a.task=t.id WHERE t.batch=? ORDER BY a.created,a.id', (key,))]}

    def capacity(self) -> list[dict]:
        with self.lock:
            out = []
            for p in self.db.execute('SELECT * FROM pools ORDER BY name'):
                a = self.db.execute('SELECT state,SUM(groups) AS n FROM allocations WHERE pool=? AND released=0 GROUP BY state', (p['name'],)).fetchall()
                used = {r['state']: r['n'] for r in a}
                n = self.db.execute('SELECT COUNT(*) FROM runners WHERE pool=?', (p['name'],)).fetchone()[0]
                free = self.db.execute('''SELECT COUNT(*) FROM runners r JOIN hosts h ON h.name=r.host
                    WHERE r.pool=? AND online=1 AND busy=0 AND h.drained=0 AND h.seen>? AND r.seen>?
                    AND NOT EXISTS(SELECT 1 FROM allocations a WHERE a.runner=r.tag AND runner_done=0)''',
                    (p['name'], self.clock()-120, self.clock()-120)).fetchone()[0]
                out.append({'device': p['name'], 'groups_total': p['total'], 'groups_reserved': used.get('reserved', 0),
                            'groups_allocated': used.get('allocated', 0), 'groups_free': p['total'] - sum(used.values()),
                            'runners_total': n, 'runners_free': free,
                            'runners_schedulable': min(free, p['total'] - sum(used.values())) if p['enabled'] else 0,
                            'enabled': bool(p['enabled'])})
            return out

    def next_message(self) -> dict | None:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM outbox WHERE state!='done' AND available<=? ORDER BY id LIMIT 1", (self.clock(),)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE outbox SET state='sending',attempts=attempts+1,available=? WHERE id=?", (self.clock()+120, row['id']))
            return {**dict(row), 'payload': json.loads(row['payload']), 'attempts': row['attempts']+1}

    def message_done(self, ident: int):
        with self.transaction() as db:
            db.execute("UPDATE outbox SET state='done' WHERE id=?", (ident,))

    def message_retry(self, ident: int, error: str, delay: float):
        with self.transaction() as db:
            db.execute("UPDATE outbox SET state='pending',available=?,error=? WHERE id=?", (self.clock()+max(1, delay), error[:160], ident))
