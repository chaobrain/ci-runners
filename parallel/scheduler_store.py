"""Durable scheduling ledger. All resource decisions are short SQLite transactions.

Task requests and reservations commit atomically. Missing completion evidence never
makes an allocation free. Resource cleanup and GitHub job completion are independent.
"""
from __future__ import annotations

import contextlib
from datetime import UTC, datetime
import json
import math
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Mapping

from ci_runners import registry
from parallel import protocol

HEARTBEAT_TTL = 120
ORGANIZATION_DEVICE_ORDER = ('h100', 'pro6000', '3090b', '3090a', 'slurm')

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(version INTEGER NOT NULL);
INSERT INTO meta SELECT 2 WHERE NOT EXISTS(SELECT 1 FROM meta);
CREATE TABLE IF NOT EXISTS pools(name TEXT PRIMARY KEY, total INTEGER NOT NULL, enabled INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS hosts(name TEXT PRIMARY KEY, seen REAL NOT NULL, drained INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS device_registrations(
 device TEXT PRIMARY KEY, profile TEXT NOT NULL, projects TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS organization_readiness(
 device TEXT PRIMARY KEY REFERENCES device_registrations(device), ready INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS organization_cache_devices(
 device TEXT PRIMARY KEY REFERENCES device_registrations(device));
CREATE TABLE IF NOT EXISTS organization_environments(
 device TEXT NOT NULL REFERENCES device_registrations(device), repository TEXT NOT NULL,
 PRIMARY KEY(repository,device));
CREATE TABLE IF NOT EXISTS runners(
 tag TEXT PRIMARY KEY, pool TEXT NOT NULL REFERENCES pools(name), host TEXT NOT NULL,
 online INTEGER NOT NULL DEFAULT 0, busy INTEGER NOT NULL DEFAULT 1, seen REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS batches(
 id TEXT PRIMARY KEY, source TEXT NOT NULL, digest TEXT NOT NULL, created REAL NOT NULL,
 last_scheduled REAL NOT NULL DEFAULT 0, state TEXT NOT NULL, max_parallel INTEGER NOT NULL,
 retries INTEGER NOT NULL, cancel_key TEXT NOT NULL, source_done INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS tasks(
 id TEXT PRIMARY KEY, batch TEXT NOT NULL REFERENCES batches(id), part TEXT NOT NULL,
 eligible TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL, deadline REAL NOT NULL,
 wait_remaining REAL NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, requested_attempt INTEGER NOT NULL DEFAULT 0,
 result TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS allocations(
 id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id), pool TEXT NOT NULL,
 runner TEXT NOT NULL, host TEXT NOT NULL, groups INTEGER NOT NULL CHECK(groups > 0),
 state TEXT NOT NULL, run_id INTEGER, run_attempt INTEGER, job_id INTEGER,
 task_attempt INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL, updated REAL NOT NULL,
 released INTEGER NOT NULL DEFAULT 0, runner_done INTEGER NOT NULL DEFAULT 0,
 allocated_at REAL, conclusion TEXT, artifacts TEXT NOT NULL DEFAULT '[]',
 cancelled INTEGER NOT NULL DEFAULT 0, result TEXT NOT NULL DEFAULT '{}');
CREATE UNIQUE INDEX IF NOT EXISTS one_runner_allocation ON allocations(runner) WHERE runner_done = 0;
CREATE UNIQUE INDEX IF NOT EXISTS one_task_allocation ON allocations(task) WHERE runner_done = 0 OR released = 0;
CREATE TABLE IF NOT EXISTS repository_environment_allocations(
 allocation TEXT PRIMARY KEY REFERENCES allocations(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, allocation TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS prepared_environments(
 id TEXT PRIMARY KEY, repository_id INTEGER NOT NULL, sha TEXT NOT NULL, recipe TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_storage(
 id TEXT PRIMARY KEY, repository_id INTEGER NOT NULL, sha TEXT NOT NULL, binding TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS allocation_storage(
 allocation TEXT PRIMARY KEY REFERENCES allocations(id), reference TEXT NOT NULL);
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
            try:
                self._upgrade_database()
            except Exception:
                self.db.close()
                raise
        if path != ':memory:':
            os.chmod(path, 0o600)

    def _upgrade_database(self):
        version = self.db.execute('SELECT version FROM meta').fetchone()[0]
        if version == 4:
            return
        if version not in (1, 2, 3):
            raise protocol.Error('unsupported scheduler database version')
        with self.transaction() as database:
            version = database.execute('SELECT version FROM meta').fetchone()[0]
            if version == 4:
                return
            pending = database.execute("SELECT 1 FROM batches WHERE state='pending'").fetchone()
            active = database.execute('SELECT 1 FROM allocations WHERE released=0 OR runner_done=0').fetchone()
            if pending or active:
                raise protocol.Error('finish old batches and drain allocations before upgrading the scheduler')
            if version == 1:
                database.execute('ALTER TABLE batches ADD COLUMN source_done INTEGER NOT NULL DEFAULT 0')
                database.execute('ALTER TABLE tasks ADD COLUMN requested_attempt INTEGER NOT NULL DEFAULT 0')
                database.execute('ALTER TABLE allocations ADD COLUMN job_id INTEGER')
                database.execute('ALTER TABLE allocations ADD COLUMN task_attempt INTEGER NOT NULL DEFAULT 1')
                database.execute('UPDATE batches SET source_done=1')
                database.execute("DELETE FROM outbox WHERE kind IN ('dispatch','cancel')")
            if version < 3:
                database.execute('''CREATE TABLE gpu_cards(uuid TEXT PRIMARY KEY, pool TEXT NOT NULL REFERENCES pools(name),
                                 host TEXT NOT NULL, position INTEGER NOT NULL UNIQUE, free_mib INTEGER)''')
                database.execute('ALTER TABLE tasks ADD COLUMN gpu_mem_mib INTEGER NOT NULL DEFAULT 0')
                database.execute('ALTER TABLE allocations ADD COLUMN gpu_uuid TEXT')
                database.execute('''CREATE UNIQUE INDEX one_gpu_allocation ON allocations(gpu_uuid)
                                 WHERE gpu_uuid IS NOT NULL AND released=0''')
            database.execute('''CREATE TABLE allocation_cards(
                allocation TEXT NOT NULL REFERENCES allocations(id), uuid TEXT NOT NULL,
                position INTEGER NOT NULL, PRIMARY KEY(allocation,uuid), UNIQUE(allocation,position))''')
            database.execute('''INSERT INTO allocation_cards SELECT id,gpu_uuid,0 FROM allocations
                                WHERE gpu_uuid IS NOT NULL''')
            database.execute('''CREATE TABLE allocation_capabilities(
                                host TEXT PRIMARY KEY REFERENCES hosts(name), schema INTEGER NOT NULL)''')
            database.execute('UPDATE meta SET version=4')

    def prepared_environment(self, source, candidate=None):
        """The first trusted resolution for this source attempt wins across restarts."""
        ident = protocol.source_key(source)
        with self.transaction() as db:
            if candidate is not None:
                db.execute('INSERT OR IGNORE INTO prepared_environments VALUES(?,?,?,?)',
                           (ident, source['repository_id'], source['sha'], protocol.dumps(candidate)))
            row = db.execute('SELECT * FROM prepared_environments WHERE id=?', (ident,)).fetchone()
            if row is None:
                return None
            if row['repository_id'] != source['repository_id'] or row['sha'] != source['sha']:
                raise protocol.Error('prepared environment belongs to another repository or test commit')
            return json.loads(row['recipe'])

    def prepared_storage(self, source, candidate=None):
        """One backend per verified repository/run attempt, shared by all contexts."""
        from ci_runners import storage
        from parallel import bundles
        rid = bundles.repository_id(source)
        ident = protocol.fingerprint([rid, source['run_id'], source['run_attempt']])
        with self.transaction() as db:
            if candidate is not None:
                candidate = storage.binding(candidate)
                db.execute('INSERT OR IGNORE INTO source_storage VALUES(?,?,?,?)',
                           (ident, rid, source['sha'], protocol.dumps(candidate)))
            row = db.execute('SELECT * FROM source_storage WHERE id=?', (ident,)).fetchone()
            if row is None:
                return None
            if row['repository_id'] != rid or row['sha'] != source['sha']:
                raise storage.Error('identity')
            return storage.binding(json.loads(row['binding']))

    def storage_reference(self, allocation):
        with self.lock:
            row = self.db.execute('SELECT reference FROM allocation_storage WHERE allocation=?', (allocation,)).fetchone()
            return json.loads(row['reference']) if row else None

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
                   (key, kind, protocol.dumps(payload), self.clock()))

    def configure(self, pools: Mapping[str, int], runners: list[dict], gpu_inventory=None):
        """Install explicit pool/driver mappings; refuse destructive changes while busy."""
        for name, total in pools.items():
            if name not in protocol.DEVICES or not protocol.positive(total) or total > protocol.DEVICE_GROUPS[name]:
                raise protocol.Error('invalid pool capacity')
        names = set()
        for r in runners:
            if (r.get('pool') not in pools or r.get('tag') not in protocol.runner_names(r['pool'])
                    or not isinstance(r.get('host'), str) or not protocol.ID.fullmatch(r['host'])
                    or r['tag'] in names):
                raise protocol.Error('invalid or duplicate runner mapping')
            names.add(r['tag'])
        if any(sum(r['pool'] == p for r in runners) != total for p, total in pools.items()):
            raise protocol.Error('register one runner per group')
        if any(len({r['host'] for r in runners if r['pool'] == p}) != 1 for p in pools if p != 'slurm'):
            raise protocol.Error('a local device must belong to one physical host')
        cards = {}
        for device, uuids in (gpu_inventory or {}).items():
            if device not in pools or device not in protocol.GPU_QUOTAS:
                raise protocol.Error('GPU device needs a local executor pool')
            protocol.validate_gpu_list(device, uuids)
            host = next(r['host'] for r in runners if r['pool'] == device)
            for card in uuids:
                if card in cards:
                    raise protocol.Error('duplicate physical GPU UUID')
                cards[card] = (device, host)
        with self.transaction() as db:
            for a in db.execute('SELECT * FROM allocations WHERE released=0 OR runner_done=0'):
                match = next((r for r in runners if r['tag'] == a['runner']), None)
                if not match or match['host'] != a['host'] or match['pool'] != a['pool']:
                    raise protocol.Error('drain active allocations before changing runner mappings')
                held_cards = [r[0] for r in db.execute('SELECT uuid FROM allocation_cards WHERE allocation=?', (a['id'],))]
                if not a['released'] and any(cards.get(card) != (a['pool'], a['host']) for card in held_cards):
                    raise protocol.Error('drain active GPU leases before changing the card inventory')
            for p, total in pools.items():
                used = db.execute('SELECT COALESCE(SUM(groups),0) FROM allocations WHERE pool=? AND released=0', (p,)).fetchone()[0]
                if used > total:
                    raise protocol.Error('cannot shrink a pool below its reservations')
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
            for old in db.execute('SELECT uuid,pool,host FROM gpu_cards').fetchall():
                if cards.get(old['uuid']) != (old['pool'], old['host']):
                    db.execute('DELETE FROM gpu_cards WHERE uuid=?', (old['uuid'],))
            db.execute('UPDATE gpu_cards SET free_mib=NULL')
            for card, (device, host) in cards.items():
                db.execute('''INSERT OR IGNORE INTO gpu_cards(uuid,pool,host,position)
                           VALUES(?,?,?,(SELECT COALESCE(MAX(position),0)+1 FROM gpu_cards))''', (card, device, host))

    def registrations(self):
        with self.lock:
            return [json.loads(row['profile']) for row in
                    self.db.execute('SELECT profile FROM device_registrations ORDER BY device')]

    def register_device(self, identity: str, profile: dict):
        profile = protocol.load_device_registration(profile)
        device = profile['device']
        if identity != device:
            raise protocol.Error('execution device does not match its credential')
        if registry.memory_bytes(profile['memory_max']) < profile['cpu_count'] * (1 << 30):
            raise protocol.Error('device memory cannot cover its CPU groups')
        runners = protocol.runner_names(device, profile['runner_count'])
        cards = profile['gpu_uuids']
        encoded = protocol.dumps(profile)
        with self.transaction() as db:
            existing = db.execute('SELECT profile FROM device_registrations WHERE device=?', (device,)).fetchone()
            if existing and existing['profile'] == encoded:
                return
            if existing and {**json.loads(existing['profile']), 'priority': profile['priority']} == profile:
                db.execute('UPDATE device_registrations SET profile=? WHERE device=?', (encoded, device))
                return
            if db.execute('SELECT 1 FROM allocations WHERE host=? AND (released=0 OR runner_done=0)', (device,)).fetchone():
                raise protocol.Error('drain active allocations before changing device resources')
            if db.execute('SELECT 1 FROM runners WHERE pool=? AND host<>?', (device, device)).fetchone():
                raise protocol.Error('device pool belongs to another host')
            for card in cards:
                if db.execute('SELECT 1 FROM gpu_cards WHERE uuid=? AND pool<>?', (card, device)).fetchone():
                    raise protocol.Error('duplicate physical GPU UUID')
            db.execute('INSERT INTO pools VALUES(?,?,1) ON CONFLICT(name) DO UPDATE SET total=excluded.total,enabled=1',
                       (device, profile['runner_count']))
            db.execute('DELETE FROM runners WHERE pool=?', (device,))
            db.executemany('INSERT INTO runners(tag,pool,host) VALUES(?,?,?)',
                           [(runner, device, device) for runner in runners])
            db.execute('DELETE FROM gpu_cards WHERE pool=?', (device,))
            for card in cards:
                db.execute('''INSERT INTO gpu_cards(uuid,pool,host,position)
                              VALUES(?,?,?,(SELECT COALESCE(MAX(position),0)+1 FROM gpu_cards))''',
                           (card, device, device))
            db.execute("INSERT INTO device_registrations VALUES(?,?,'{}') "
                       "ON CONFLICT(device) DO UPDATE SET profile=excluded.profile,projects='{}'", (device, encoded))
            db.execute('DELETE FROM organization_cache_devices WHERE device=?', (device,))
            db.execute('DELETE FROM organization_readiness WHERE device=?', (device,))
            db.execute('DELETE FROM organization_environments WHERE device=?', (device,))
            db.execute('UPDATE hosts SET seen=0,drained=1 WHERE name=?', (device,))

    def devices(self, allowed, *, heartbeat_ttl=HEARTBEAT_TTL):
        cutoff = self.clock() - heartbeat_ttl
        with self.lock:
            rows = self.db.execute('''SELECT d.*,h.seen,h.drained,
                EXISTS(SELECT 1 FROM runners r WHERE r.host=d.device AND r.pool=d.device AND r.online=1 AND r.seen>?) AS alive
                FROM device_registrations d LEFT JOIN hosts h ON h.name=d.device ORDER BY d.device''', (cutoff,)).fetchall()
        devices = {}
        for row in rows:
            profile = json.loads(row['profile'])
            name = profile.pop('device')
            seen = row['seen']
            devices[name] = {
                **profile,
                'runner_tags': protocol.runner_names(name, profile['runner_count']),
                'online': name in allowed and bool(row['alive']) and seen > cutoff and not row['drained'],
                'last_heartbeat_at': datetime.fromtimestamp(seen, UTC).isoformat() if seen else None,
            }
        return {'schema': 1, 'devices': devices}

    def heartbeat(self, host: str, runners: list[dict], *, drained: bool = False, gpu_free_mib=None,
                  ready_projects=None, organization_ready=False, organization_repositories=None, environment_cache=False,
                  allocation_schema=None):
        """Called only for an authenticated host; it cannot report another host's runners."""
        if (type(drained) is not bool or type(organization_ready) is not bool or not isinstance(runners, list)
                or type(environment_cache) is not bool
                or (environment_cache and (organization_ready or organization_repositories is not None))):
            raise protocol.Error('invalid heartbeat')
        if allocation_schema is None:
            allocation_schema = 2 if gpu_free_mib is not None else 1
        if type(allocation_schema) is not int or allocation_schema not in (1, 2, 3):
            raise protocol.Error('invalid allocation capability')
        with self.transaction() as db:
            known = {r[0] for r in db.execute('SELECT tag FROM runners WHERE host=?', (host,))}
            if not known:
                raise protocol.Error('unknown execution host')
            registration = db.execute('SELECT profile FROM device_registrations WHERE device=?', (host,)).fetchone()
            if registration is None and (ready_projects is not None or environment_cache or organization_ready
                                         or organization_repositories is not None):
                raise protocol.Error('execution device must register before sending heartbeats')
            if registration is not None:
                db.execute('DELETE FROM organization_cache_devices WHERE device=?', (host,))
                if environment_cache:
                    db.execute('INSERT INTO organization_cache_devices VALUES(?)', (host,))
                db.execute('DELETE FROM organization_environments WHERE device=?', (host,))
                if organization_repositories:
                    db.executemany('INSERT INTO organization_environments(device,repository) VALUES(?,?)',
                                   [(host, name) for name in organization_repositories])
                db.execute('INSERT INTO organization_readiness(device,ready) VALUES(?,?) '
                           'ON CONFLICT(device) DO UPDATE SET ready=excluded.ready', (host, organization_ready))
            if ready_projects is not None:
                limit = registry.memory_bytes(json.loads(registration['profile'])['memory_max'])
                if (not isinstance(ready_projects, dict) or any(
                        not isinstance(name, str) or not registry.NAME.fullmatch(name) or not isinstance(spec, dict)
                        or set(spec) != {'repository', 'memory_bytes', 'max_minutes', 'devices'}
                        or not isinstance(spec['repository'], str) or not protocol.REPO.fullmatch(spec['repository'])
                        or not protocol.positive(spec['memory_bytes']) or spec['memory_bytes'] > limit
                        or not protocol.positive(spec['max_minutes']) or spec['max_minutes'] > protocol.MAX_MINUTES
                        or not isinstance(spec['devices'], list) or any(
                            not isinstance(value, str) or value not in protocol.DEVICES for value in spec['devices'])
                        for name, spec in ready_projects.items())):
                    raise protocol.Error('invalid ready project capabilities')
                db.execute('UPDATE device_registrations SET projects=? WHERE device=?',
                           (protocol.dumps(ready_projects), host))
            cards = {r[0] for r in db.execute('SELECT uuid FROM gpu_cards WHERE host=?', (host,))}
            observations = {} if gpu_free_mib is None else gpu_free_mib
            if (not isinstance(observations, dict) or set(observations) - cards or
                    any(type(mib) is not int or mib < 0 for mib in observations.values())):
                raise protocol.Error('heartbeat contains an invalid GPU observation')
            db.execute('UPDATE gpu_cards SET free_mib=NULL WHERE host=?', (host,))
            db.executemany('UPDATE gpu_cards SET free_mib=? WHERE uuid=? AND host=?',
                           [(mib, uuid, host) for uuid, mib in observations.items()])
            seen = set()
            for r in runners:
                if (not isinstance(r, dict) or r.get('tag') not in known or r['tag'] in seen
                        or type(r.get('online')) is not bool or type(r.get('busy')) is not bool):
                    raise protocol.Error('heartbeat names an invalid runner')
                seen.add(r['tag'])
                db.execute('UPDATE runners SET online=?,busy=?,seen=? WHERE tag=?',
                           (r['online'], r['busy'], self.clock(), r['tag']))
            for missing in known - seen:
                db.execute('UPDATE runners SET online=0 WHERE tag=?', (missing,))
            db.execute('INSERT INTO hosts VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET seen=excluded.seen,drained=excluded.drained',
                       (host, self.clock(), drained))
            db.execute('''INSERT INTO allocation_capabilities VALUES(?,?) ON CONFLICT(host)
                          DO UPDATE SET schema=excluded.schema''', (host, allocation_schema))

    def submit(self, source: dict, plan: dict, eligible: Mapping[str, list[str]], *, max_parallel=16, retries=1,
               gpu_mem_mib=0, organization=False) -> str:
        plan = protocol.load_plan(plan)
        key = protocol.source_key(source)
        if not protocol.positive(max_parallel) or retries not in (0, 1) or type(gpu_mem_mib) is not int or gpu_mem_mib < 0:
            raise protocol.Error('invalid project limits')
        cancel_key = protocol.dumps([source['repository'].lower(), source['context'], source['concurrency_key']])
        now = self.clock()
        with self.transaction() as db:
            previous = db.execute('SELECT digest,source FROM batches WHERE id=?', (key,)).fetchone()
            if previous and organization:
                # Check a replay against its original scheduling decision. Current Slurm admission
                # is checked independently when reserving and claiming, including for retries.
                saved = json.loads(previous['source'])
                source = dict(source)
                for field in ('slurm_project', 'slurm_target'):
                    source.pop(field, None)
                    if field in saved:
                        source[field] = saved[field]
                tasks = db.execute('SELECT part,eligible FROM tasks WHERE batch=?', (key,))
                eligible = {json.loads(task['part'])['id']: json.loads(task['eligible']) for task in tasks}
            else:
                for p in plan['parts']:
                    choices = eligible.get(p['id'], [])
                    if not choices and organization:
                        raise protocol.Error(f'{p["id"]}: no configured, admitted device satisfies the resource request')
                    if (not choices or any(d not in protocol.DEVICES or p.get('gpu') and d not in protocol.GPU_QUOTAS
                                           or p['device'] and p['device'] != d or p['cores'] > protocol.DEVICE_GROUPS[d] * 4
                                           for d in choices)):
                        raise protocol.Error(f'{p["id"]}: no eligible device satisfies the request')
            identity = [source, plan, eligible, max_parallel, retries]
            if any(p.get('gpu') for p in plan['parts']):
                identity.append(gpu_mem_mib)
            digest = protocol.fingerprint(identity)
            if previous:
                if previous[0] != digest:
                    raise protocol.Error('source batch already submitted with different content')
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
                       (key, protocol.dumps(source), digest, now, 'pending', min(max_parallel, plan['max_parallel']), retries, cancel_key))
            for p in plan['parts']:
                task = protocol.fingerprint([key, p['id']])[:32]
                db.execute('INSERT INTO tasks(id,batch,part,eligible,state,created,deadline,wait_remaining,gpu_mem_mib) VALUES(?,?,?,?,?,?,?,?,?)',
                           (task, key, protocol.dumps(p), protocol.dumps(eligible[p['id']]), 'queued', now,
                            now + p['queue_minutes'] * 60, p['queue_minutes'] * 60, gpu_mem_mib if p.get('gpu') else 0))
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
                   (protocol.dumps({'state': 'cancelled', 'reason': 'source cancelled'}), batch))
        for a in db.execute('SELECT a.* FROM allocations a JOIN tasks t ON a.task=t.id WHERE t.batch=? AND (a.released=0 OR a.runner_done=0)', (batch,)).fetchall():
            db.execute('UPDATE allocations SET cancelled=1 WHERE id=?', (a['id'],))
        self._batch_end(db, batch)

    def cancel(self, batch: str):
        with self.transaction() as db:
            if not db.execute('SELECT 1 FROM batches WHERE id=?', (batch,)).fetchone():
                raise protocol.Error('unknown batch')
            self._cancel(db, batch)

    def request_task_allocation(self, batch_id: str, task_id: str, task_attempt: int) -> dict:
        if type(task_attempt) is not int or task_attempt not in (1, 2):
            raise protocol.Error('task attempt must be 1 or 2')
        with self.transaction() as database:
            task = database.execute('SELECT * FROM tasks WHERE id=? AND batch=?', (task_id, batch_id)).fetchone()
            if task is None:
                raise protocol.Error('unknown task in this batch')
            if task_attempt > task['attempts'] + 1:
                raise protocol.Error('cannot skip a task attempt')
            database.execute('UPDATE tasks SET requested_attempt=MAX(requested_attempt,?) WHERE id=?',
                             (task_attempt, task_id))
        return self.task_status(batch_id, task_id, task_attempt)

    def task_status(self, batch_id: str, task_id: str, task_attempt: int = 1) -> dict:
        with self.lock:
            task = self.db.execute('SELECT * FROM tasks WHERE id=? AND batch=?', (task_id, batch_id)).fetchone()
            if task is None:
                raise protocol.Error('unknown task in this batch')
            allocation = self.db.execute('SELECT id FROM allocations WHERE task=? AND task_attempt=?',
                                         (task_id, task_attempt)).fetchone()
            allocation_record = self._allocation(self.db, allocation['id']) if allocation else None
            return {**dict(task), 'part': json.loads(task['part']), 'result': json.loads(task['result']),
                    'allocation': allocation_record}

    @staticmethod
    def _organization_environment_hosts(db, repository, require_repository_environment):
        rows = db.execute("""SELECT device FROM organization_environments WHERE repository=?
                             UNION ALL SELECT device FROM organization_readiness WHERE ready=1 AND ?=0""",
                          (repository.lower(), require_repository_environment))
        return {row[0] for row in rows}

    def _available_runner(self, database, task_record, source, heartbeat_ttl, allowed_devices, organization,
                          slurm_permitted, require_repository_environment, require_environment_cache):
        cached_recipe = 'environment_recipe' in source
        if (cached_recipe != require_environment_cache or
                (organization and 'organization_profile' not in source)):
            return None
        part = json.loads(task_record['part'])
        groups_required = part['cores'] // protocol.GROUP_CPUS
        devices = sorted(json.loads(task_record['eligible']), key=lambda device: (device == 'slurm', device))
        fresh_after = self.clock() - heartbeat_ttl
        priorities = {}
        if allowed_devices is not None:
            compatible = {}
            required_memory = protocol.part_memory_gib(part) * (1 << 30)
            if organization:
                cache_devices = {r[0] for r in database.execute('SELECT device FROM organization_cache_devices')}
                env_hosts = cache_devices if cached_recipe else self._organization_environment_hosts(
                    database, source['repository'], require_repository_environment)
            registrations = database.execute('SELECT * FROM device_registrations')
            for row in registrations:
                device = row['device']
                if device not in allowed_devices:
                    continue
                profile = json.loads(row['profile'])
                if organization:
                    # A registered driver can run its remote CPU pool independently of its local environment.
                    if cached_recipe != (device in cache_devices):
                        continue
                    compatible[device] = {'slurm'}
                    if device in env_hosts and registry.memory_bytes(profile['memory_max']) >= required_memory:
                        compatible[device].add(device)
                else:
                    ready = json.loads(row['projects']).get(source['project'])
                    if (not ready or ready['repository'] != source['repository'].lower()
                            or ready['memory_bytes'] < required_memory or part['minutes'] > ready['max_minutes']):
                        continue
                    compatible[device] = ready['devices']
                priorities[device] = profile['priority']
            devices = [device for device in devices
                       if device == 'slurm' or device in compatible.get(device, ())]
            if organization:
                devices.sort(key=ORGANIZATION_DEVICE_ORDER.index)
            else:
                devices.sort(key=lambda device: -priorities.get(device, 0))
        if part.get('gpu'):
            free_cards = database.execute('''SELECT pool,uuid FROM gpu_cards c WHERE free_mib>=?
                AND NOT EXISTS(SELECT 1 FROM allocation_cards ac JOIN allocations a ON a.id=ac.allocation
                               WHERE ac.uuid=c.uuid AND a.released=0)
                ORDER BY position''', (task_record['gpu_mem_mib'],)).fetchall()
            need = protocol.gpu_count(part)
            candidates = [(c['pool'], [c['uuid']]) for c in free_cards if c['pool'] in devices] if need == 1 else []
            if need == 2:
                for device in dict.fromkeys(c['pool'] for c in free_cards if c['pool'] in devices):
                    cards = [c['uuid'] for c in free_cards if c['pool'] == device]
                    if len(cards) >= need:
                        candidates.append((device, cards[:need]))
            if organization:
                candidates.sort(key=lambda candidate: ORGANIZATION_DEVICE_ORDER.index(candidate[0]))
            elif allowed_devices is not None:
                candidates.sort(key=lambda candidate: -priorities[candidate[0]])
        else:
            candidates = [(device, []) for device in devices]
        for device, gpu_uuids in candidates:
            pool = database.execute('SELECT total FROM pools WHERE name=? AND enabled=1', (device,)).fetchone()
            occupied = database.execute('SELECT COALESCE(SUM(groups),0) FROM allocations WHERE pool=? AND released=0',
                                        (device,)).fetchone()[0]
            if pool is None or pool['total'] - occupied < groups_required:
                continue
            runner = database.execute('''SELECT r.* FROM runners r JOIN hosts h ON r.host=h.name
                WHERE r.pool=? AND r.online=1 AND r.busy=0 AND r.seen>? AND h.seen>? AND h.drained=0
                AND NOT EXISTS(SELECT 1 FROM allocations a WHERE a.runner=r.tag AND a.runner_done=0)
                ORDER BY r.tag LIMIT 1''', (device, fresh_after, fresh_after)).fetchone()
            if runner is None or (allowed_devices is not None and device not in compatible.get(runner['host'], ())):
                continue
            if len(gpu_uuids) == 2:
                capability = database.execute('SELECT schema FROM allocation_capabilities WHERE host=?',
                                               (runner['host'],)).fetchone()
                if capability is None or capability[0] < 3:
                    continue
            if device != 'slurm':
                registration = database.execute('SELECT profile FROM device_registrations WHERE device=?',
                                                (runner['host'],)).fetchone()
                if registration:
                    memory_limit = registry.memory_bytes(json.loads(registration['profile'])['memory_max'])
                else:
                    # A static pool has only its original core-derived capacity as evidence.
                    # Do not infer extra host RAM for an explicit request.
                    memory_limit = pool['total'] * protocol.GROUP_GIB * (1 << 30)
                occupied_parts = database.execute('''SELECT t.part FROM allocations a JOIN tasks t ON t.id=a.task
                    WHERE a.host=? AND a.pool!='slurm' AND a.released=0''', (runner['host'],))
                occupied_memory = sum(protocol.part_memory_gib(json.loads(row['part'])) * (1 << 30)
                                      for row in occupied_parts)
                requested_memory = protocol.part_memory_gib(part) * (1 << 30)
                if occupied_memory + requested_memory > memory_limit:
                    continue
            if organization and device == 'slurm' and (slurm_permitted is None or not slurm_permitted(source, part)):
                continue
            return runner, groups_required, gpu_uuids
        return None

    def reserve(self, *, heartbeat_ttl=HEARTBEAT_TTL, allowed_devices=None, organization=False,
                slurm_permitted=None, require_repository_environment=False, require_environment_cache=False) -> dict | None:
        if organization and allowed_devices is None:
            raise protocol.Error('organization dispatch needs an authoritative device list')
        now = self.clock()
        with self.transaction() as database:
            expired = database.execute("SELECT id,batch FROM tasks WHERE state='queued' AND deadline<=?", (now,)).fetchall()
            for task_record in expired:
                database.execute("UPDATE tasks SET state='error',result=? WHERE id=?",
                                 (protocol.dumps({'state': 'error', 'reason': 'queue deadline exceeded'}), task_record['id']))
                self._batch_end(database, task_record['batch'])
            batches = database.execute("SELECT * FROM batches WHERE state='pending' AND source_done=0 ORDER BY last_scheduled,created,id").fetchall()
            for batch_record in batches:
                active = database.execute("SELECT COUNT(*) FROM tasks WHERE batch=? AND state IN ('reserved','running','finishing')",
                                          (batch_record['id'],)).fetchone()[0]
                if active >= batch_record['max_parallel']:
                    continue
                tasks = database.execute("SELECT * FROM tasks WHERE batch=? AND state='queued' AND requested_attempt=attempts+1 ORDER BY created,id",
                                         (batch_record['id'],)).fetchall()
                source = json.loads(batch_record['source'])
                for task_record in tasks:
                    selected = self._available_runner(database, task_record, source, heartbeat_ttl,
                                                      allowed_devices, organization, slurm_permitted,
                                                      require_repository_environment, require_environment_cache)
                    if selected is None:
                        continue
                    runner_record, groups_required, gpu_uuids = selected
                    allocation_id = uuid.uuid4().hex
                    database.execute('''INSERT INTO allocations
                        (id,task,pool,runner,host,groups,state,run_id,run_attempt,task_attempt,created,updated,gpu_uuid)
                        VALUES(?,?,?,?,?,?,'reserved',?,?,?,?,?,?)''',
                        (allocation_id, task_record['id'], runner_record['pool'], runner_record['tag'], runner_record['host'],
                         groups_required, source['run_id'], source['run_attempt'], task_record['attempts'] + 1, now, now,
                         gpu_uuids[0] if len(gpu_uuids) == 1 else None))
                    database.executemany('INSERT INTO allocation_cards VALUES(?,?,?)',
                                         [(allocation_id, card, n) for n, card in enumerate(gpu_uuids)])
                    if organization and runner_record['pool'] != 'slurm':
                        database.execute('''INSERT INTO repository_environment_allocations(allocation)
                            SELECT ? FROM organization_environments WHERE device=? AND repository=?''',
                            (allocation_id, runner_record['host'], source['repository'].lower()))
                    database.execute("UPDATE tasks SET state='reserved',attempts=attempts+1 WHERE id=?", (task_record['id'],))
                    sequence = database.execute('SELECT COALESCE(MAX(last_scheduled),0) FROM batches').fetchone()[0]
                    database.execute('UPDATE batches SET last_scheduled=? WHERE id=?',
                                     (max(now, sequence + .000001), batch_record['id']))
                    return self._allocation(database, allocation_id)
        return None

    def _allocation(self, db, aid):
        a = db.execute('SELECT * FROM allocations WHERE id=?', (aid,)).fetchone()
        if not a:
            raise protocol.Error('unknown allocation')
        t = db.execute('SELECT * FROM tasks WHERE id=?', (a['task'],)).fetchone()
        b = db.execute('SELECT * FROM batches WHERE id=?', (t['batch'],)).fetchone()
        p, source = json.loads(t['part']), json.loads(b['source'])
        grant = {'allocation_schema': 1, 'allocation_id': aid, 'task_id': t['id'], 'task_attempt': a['task_attempt'], 'host': a['host'],
                 'requested_device': p['device'], 'device': a['pool'], 'backend': 'slurm' if a['pool'] == 'slurm' else 'local',
                 'runner_tag': a['runner'], 'cpu_groups': a['groups'], 'cores': a['groups'] * 4, 'RAM': f'{a["groups"] * 4}G',
                 'workers': p['workers'], 'minutes': p['minutes'], 'queue_minutes': p['queue_minutes']}
        grant['job_timeout_minutes'] = math.ceil(p['minutes'] + p['queue_minutes'] + 30)
        if 'memory_gib' in p:
            grant.update(memory_gib=p['memory_gib'], RAM=f'{p["memory_gib"]}G')
        if a['gpu_uuid'] is not None:
            grant.update(allocation_schema=2, gpu_uuid=a['gpu_uuid'])
        else:
            cards = [row[0] for row in db.execute('''SELECT uuid FROM allocation_cards
                          WHERE allocation=? ORDER BY position''', (aid,))]
            if cards:
                grant.update(allocation_schema=3, gpu_count=2, gpu_uuids=cards)
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

    def claim(self, aid: str, host: str, runner: str, repository: str, run_id: int, run_attempt: int, *, job_id: int,
              require_repository_environment=False, repository_environment_confirmed=False,
              require_environment_cache=False, environment_cache_confirmed=False) -> dict:
        if not protocol.positive(job_id):
            raise protocol.Error('invalid execution job identity')
        if not protocol.positive(run_id) or not protocol.positive(run_attempt):
            raise protocol.Error('invalid execution run identity')
        with self.transaction() as db:
            a = self._allocation(db, aid)
            if (a['host'] != host or a['runner'] != runner or a['source']['repository'].lower() != repository.lower()
                    or a['cancelled'] or a['released'] or a['runner_done']):
                raise protocol.Error('allocation is closed or bound to another executor')
            cached_recipe = 'environment_recipe' in a['source']
            if cached_recipe or require_environment_cache or environment_cache_confirmed:
                if not (cached_recipe and require_environment_cache and environment_cache_confirmed):
                    raise protocol.Error('executor and controller must confirm the pinned environment cache mode')
                cutoff = self.clock() - HEARTBEAT_TTL
                live = db.execute("""SELECT 1 FROM organization_cache_devices c
                                     JOIN hosts h ON h.name=c.device JOIN runners r ON r.host=c.device
                                     WHERE r.host=? AND r.tag=? AND r.pool=? AND r.online=1
                                     AND r.seen>? AND h.drained=0 AND h.seen>?""",
                                  (host, runner, a['pool'], cutoff, cutoff)).fetchone()
                if not live:
                    raise protocol.Error('environment cache capability is no longer live on this executor')
            pinned = db.execute('SELECT 1 FROM repository_environment_allocations WHERE allocation=?', (aid,)).fetchone()
            if a['pool'] != 'slurm' and (pinned or require_repository_environment or repository_environment_confirmed):
                if not repository_environment_confirmed:
                    raise protocol.Error('executor must confirm repository environment mode')
                cutoff = self.clock() - HEARTBEAT_TTL
                live = db.execute("""SELECT 1 FROM runners r JOIN hosts h ON h.name=r.host
                                     JOIN organization_environments e ON e.device=r.host AND e.repository=?
                                     WHERE r.tag=? AND r.host=? AND r.pool=? AND r.online=1
                                     AND r.seen>? AND h.drained=0 AND h.seen>?""",
                                  (a['source']['repository'].lower(), runner, host, a['pool'], cutoff, cutoff)).fetchone()
                if not live:
                    raise protocol.Error('organization environment is no longer ready on this executor')
                db.execute('INSERT OR IGNORE INTO repository_environment_allocations VALUES(?)', (aid,))
            if a['run_id'] is not None and (a['run_id'], a['run_attempt']) != (run_id, run_attempt):
                raise protocol.Error('allocation already belongs to another execution attempt')
            if a['state'] == 'reserved' and self.clock() >= a['deadline']:
                raise protocol.Error('allocation queue deadline exceeded')
            if a['job_id'] is not None and a['job_id'] != job_id:
                raise protocol.Error('allocation already belongs to another execution job')
            db.execute('UPDATE allocations SET job_id=?,updated=? WHERE id=?', (job_id, self.clock(), aid))
            return self._allocation(db, aid)

    def event(self, host: str, event_id: str, aid: str, kind: str, result: dict | None = None):
        if not isinstance(event_id, str) or not protocol.ID.fullmatch(event_id):
            raise protocol.Error('invalid event identity')
        result = {} if result is None else result
        digest = protocol.fingerprint([host, aid, kind, result])
        with self.transaction() as db:
            a = self._allocation(db, aid)
            if a['host'] != host:
                raise protocol.Error('event is from another host')
            previous = db.execute('SELECT digest FROM events WHERE id=?', (event_id,)).fetchone()
            if previous:
                if previous[0] != digest:
                    raise protocol.Error('event identity reused with different content')
                return
            if kind == 'allocated':
                if not a['released'] and not a['cancelled'] and a['allocated_at'] is None:
                    if a['job_id'] is None:
                        raise protocol.Error('allocation must be claimed before acknowledgement')
                    db.execute("UPDATE allocations SET state='allocated',updated=?,allocated_at=? WHERE id=?", (self.clock(), self.clock(), aid))
                    db.execute("UPDATE tasks SET state='running',wait_remaining=MAX(0,deadline-?) WHERE id=?", (self.clock(), a['task']))
            elif kind == 'released':
                if not isinstance(result, dict) or result.get('state') not in protocol.TERMINAL:
                    raise protocol.Error('release needs a terminal execution result')
                if not a['released']:
                    for card in protocol.grant_cards(a['grant']):
                        db.execute('''UPDATE gpu_cards SET position=(SELECT MAX(position)+1 FROM gpu_cards)
                                   WHERE uuid=?''', (card,))
                    db.execute("UPDATE allocations SET released=1,state='released',result=?,updated=? WHERE id=?",
                               (protocol.dumps(result), self.clock(), aid))
                    db.execute("UPDATE tasks SET state='finishing' WHERE id=?", (a['task'],))
                elif a['result'] != result:
                    raise protocol.Error('allocation already released with another result')
            else:
                raise protocol.Error('unknown allocation event')
            db.execute('INSERT INTO events VALUES(?,?,?)', (event_id, aid, digest))
            self._finish(db, aid)

    def runner_finished(self, aid: str, run_id: int, run_attempt: int, *, job_id: int | None,
                        conclusion='success', artifacts=(), storage_reference=None):
        """Trusted reconciliation supplies a completed job or proof the source ended."""
        with self.transaction() as database:
            allocation = self._allocation(database, aid)
            if (allocation['run_id'], allocation['run_attempt']) != (run_id, run_attempt):
                raise protocol.Error('completion is for another execution attempt')
            if allocation['job_id'] is not None and allocation['job_id'] != job_id:
                raise protocol.Error('completion is for another execution job')
            if allocation['runner_done']:
                return
            if storage_reference is not None:
                from ci_runners import storage
                storage.reference(storage_reference)
                database.execute('INSERT INTO allocation_storage VALUES(?,?)',
                                 (aid, protocol.dumps(storage_reference)))
            database.execute('UPDATE allocations SET job_id=?,runner_done=1,updated=?,conclusion=?,artifacts=? WHERE id=?',
                             (job_id, self.clock(), conclusion, protocol.dumps(list(artifacts)), aid))
            self._finish(database, aid)

    def allocation_for_runner(self, host: str, runner: str, repository: str, run_id: int, run_attempt: int) -> dict:
        matches = [allocation for allocation in self.allocations(host=host)
                   if allocation['runner'] == runner and allocation['source']['repository'].lower() == repository.lower()
                   and (allocation['run_id'], allocation['run_attempt']) == (run_id, run_attempt)
                   and not allocation['cancelled'] and not allocation['runner_done'] and not allocation['released']]
        if len(matches) != 1:
            raise protocol.Error('runner has no unique active allocation for this source attempt')
        return matches[0]

    def source_finished(self, batch_id: str, *, cancelled: bool):
        with self.transaction() as database:
            database.execute('UPDATE batches SET source_done=1 WHERE id=?', (batch_id,))
            if cancelled:
                self._cancel(database, batch_id)
                return
            database.execute("UPDATE tasks SET state='error',result=? WHERE batch=? AND state='queued'",
                             (protocol.dumps({'state': 'error', 'reason': 'source ended before task completion'}), batch_id))
            self._batch_end(database, batch_id)

    def task_request_failed(self, batch_id: str, task_id: str, task_attempt: int):
        with self.transaction() as database:
            database.execute("UPDATE tasks SET state='error',result=? WHERE id=? AND batch=? AND state='queued' AND attempts<?",
                             (protocol.dumps({'state': 'error', 'reason': 'allocation request job failed'}),
                              task_id, batch_id, task_attempt))
            self._batch_end(database, batch_id)

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
        key = protocol.source_key(source)
        if state not in {'pending', 'success', 'failure', 'error'}:
            raise protocol.Error('invalid submission status')
        with self.transaction() as db:
            old = db.execute('SELECT state FROM notices WHERE id=?', (key,)).fetchone()
            if old and (old[0] == 'accepted' or state == 'pending' and old[0] != 'pending'):
                return
            db.execute('INSERT INTO notices VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,reason=excluded.reason',
                       (key, protocol.dumps(source), state, reason))
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

    def _finish(self, db, aid):
        a = self._allocation(db, aid)
        if not (a['released'] and a['runner_done']):
            return
        t = db.execute('SELECT * FROM tasks WHERE id=?', (a['task'],)).fetchone()
        if t['state'] in protocol.TERMINAL or t['state'] == 'queued' or t['attempts'] != a['task_attempt']:
            return
        result = dict(a['result'])
        source = json.loads(db.execute('SELECT source FROM batches WHERE id=?', (t['batch'],)).fetchone()[0])
        if (source.get('storage', {}).get('backend') == 'ssh' and result.get('state') == 'success'
                and db.execute('SELECT 1 FROM allocation_storage WHERE allocation=?', (aid,)).fetchone() is None):
            result = {**result, 'state': 'error', 'reason': 'required internal result not verified', 'retryable': False}
        if a['conclusion'] and a['conclusion'] != 'success' and result['state'] == 'success':
            result = {**result, 'state': 'error', 'reason': 'execution job or artifact upload failed', 'retryable': False}
        if a['cancelled']:
            result = {**result, 'state': 'cancelled'}
        b = db.execute('SELECT retries,source_done FROM batches WHERE id=?', (t['batch'],)).fetchone()
        remaining = t['wait_remaining'] if a['allocated_at'] is not None else max(0, t['deadline']-self.clock())
        retry = (result.get('retryable') is True and result['state'] == 'error' and not a['cancelled']
                 and t['attempts'] <= b['retries'] and not b['source_done'] and remaining > 0)
        db.execute('UPDATE tasks SET state=?,result=?,deadline=?,wait_remaining=? WHERE id=?',
                   ('queued' if retry else result['state'], protocol.dumps(result), self.clock()+remaining, remaining, t['id']))
        self._batch_end(db, t['batch'])

    def _batch_end(self, db, batch):
        b = db.execute('SELECT state FROM batches WHERE id=?', (batch,)).fetchone()
        if not b or b[0] != 'pending':
            return
        states = {r[0] for r in db.execute('SELECT state FROM tasks WHERE batch=?', (batch,))}
        if not states <= protocol.TERMINAL:
            return
        state = next((s for s in ('failure', 'error', 'cancelled') if s in states), 'success')
        db.execute('UPDATE batches SET state=? WHERE id=?', (state, batch))
        self._enqueue(db, f'final:{batch}', 'status', {'batch_id': batch, 'state': state})

    def batch(self, key):
        with self.lock:
            b = self.db.execute('SELECT * FROM batches WHERE id=?', (key,)).fetchone()
            if not b:
                raise protocol.Error('unknown batch')
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
                gpu_states = self.db.execute('''SELECT
                    CASE WHEN a.id IS NOT NULL THEN a.state
                         WHEN c.free_mib IS NULL OR h.seen IS NULL OR h.seen<=? OR h.drained THEN 'unavailable'
                         ELSE 'free' END AS status, COUNT(*) AS n
                    FROM gpu_cards c LEFT JOIN hosts h ON c.host=h.name
                    LEFT JOIN (SELECT ac.uuid,a.id,a.state FROM allocation_cards ac
                               JOIN allocations a ON a.id=ac.allocation WHERE a.released=0) a ON a.uuid=c.uuid
                    WHERE c.pool=? GROUP BY status''', (self.clock()-120, p['name'])).fetchall()
                gpu = {row['status']: row['n'] for row in gpu_states}
                out.append({'device': p['name'], 'groups_total': p['total'], 'groups_reserved': used.get('reserved', 0),
                            'groups_allocated': used.get('allocated', 0), 'groups_free': p['total'] - sum(used.values()),
                            'runners_total': n, 'runners_free': free,
                            'runners_schedulable': min(free, p['total'] - sum(used.values())) if p['enabled'] else 0,
                            'gpus_total': sum(gpu.values()), 'gpus_reserved': gpu.get('reserved', 0),
                            'gpus_allocated': gpu.get('allocated', 0), 'gpus_free': gpu.get('free', 0),
                            'gpus_unavailable': gpu.get('unavailable', 0), 'enabled': bool(p['enabled'])})
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
