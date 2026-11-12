"""The host inventory: schema, loader and the lint that keeps it out of the repository
(design §3.3, D1, T9).

The inventory lives only on the hosts (``etc/inventory.toml``). The repository holds a
deny list of salted SHA-256 hashes of its tokens (``tests/deny.sha256``), so the list
does not publish what it protects, and ``lint`` refuses any tracked file that holds a
denied token, an IPv4 address, a home-directory path, a GitHub token or a private key.
The lint prints a path, a line number and a kind, never the text it matched.

CLI (stdlib only)::

    python -m ci_runners.inventory check INVENTORY            # validate the schema
    python -m ci_runners.inventory deny INVENTORY [POLICY]... # hashes of their tokens
    python -m ci_runners.inventory hash < tokens              # hashes of tokens, one a line
    python -m ci_runners.inventory lint [ROOT] [--deny FILE]  # exit 1 on a finding
"""
from __future__ import annotations

import dataclasses
import hashlib
import math
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterable, Iterator, Mapping

from ci_runners.policy import CLASSES

SCHEMA = 1

# ------------------------------------------------------------------ schema 1

_ROLE = re.compile(r'[a-z0-9][a-z0-9-]{0,30}\Z')
_POOL = re.compile(r'[a-z][a-z0-9-]{0,30}\Z')
_SIZE = re.compile(r'[1-9][0-9]{0,5}[KMGT]\Z')
_CPUS = re.compile(r'[0-9]+(?:-[0-9]+)?(?:,[0-9]+(?:-[0-9]+)?)*\Z')
#: a runner label the inventory gives (design §5.1): GitHub's own are added at registration
LABEL = re.compile(r'[a-z0-9][a-z0-9-]{0,39}\Z')
#: labels a pool may not give itself: GitHub's own and the bare class names, which any
#: runner's job could ask for (design §5.1: h100's labels are its own)
RESERVED_LABELS = frozenset({'self-hosted', 'linux', 'x64', 'light', 'part', 'compile', 'nightly', 'gpu', 'multi',
                             'slurm'})
#: what a pool's runners run with, besides the count (RUN-5), and their class where it is
#: not the pool's key (decision 12.5); from RUN-10's R1 (decision 16.3) also a pool's ``slot``
#: (16.2) and ``audience`` (16.7), checked for shape and read by nothing but setup's inert
#: ``CI_RUNNER_SLOT`` until R2 and P1, so that a host's inventory may gain them before then
POOL_KEYS = frozenset({'count', 'labels', 'per_card', 'class', 'slot', 'audience'})
#: a pool's ``slot`` table (decision 16.2): key -> required
SLOT_KEYS = {'cpus': True, 'workers': True, 'memory': True, 'speed': True, 'isa': False}
#: the runner audiences (decision 16.7): every pool is ``private`` but the public pool's
AUDIENCES = frozenset({'private', 'public'})
#: the public pool (decision 18.7): per card, of class ``gpu``, beside the host's own ``gpu``
#: pool, whose cards it shares through the leases; its runners come from the issuer (18.6),
#: never from setup, and it holds no ``count`` or ``slot`` of its own
PUBLIC_CLASSES = frozenset({'gpu'})
PUBLIC_POOL_KEYS = frozenset({'per_card', 'class', 'labels', 'audience'})
#: the public pool's capability label (16.1, 18.7): no private pool carries a ``ci-pub-`` label
PUBLIC_LABEL = 'ci-pub-gpu'
PUBLIC_PREFIX = 'ci-pub-'
PUBLIC_HOST_SUFFIX = '-pub-gpu'  # its host label, <role>-pub-gpu
#: a host's ``isolation`` (decision 18.2): the backend the host is set up for, which the probe
#: and the check prove; accepted and inert until a later phase reads it (16.3)
ISOLATION = frozenset({'account', 'ns-subuid'})
#: a slot's ``isa``: the name of the highest instruction-set level a part may use
ISA = re.compile(r'[a-z0-9][a-z0-9_.-]{0,31}\Z')
#: the highest CPU number a cpuset of a slot may name
MAX_CPU = 65535
#: the classes whose runners lease one card each, the runner's own: a per-card pool's
PER_CARD_CLASSES = frozenset({'gpu'})
#: the class of a host's home pool (design §5.1, decision 16.4; issue #232): the one class
#: of which a host may have two pools, the light pool (with a ``ci-`` label) and the home pool
#: (without one, never stopped by presence, from which a job may hold its host, 19.8)
HOME_CLASS = 'light'


class InventoryError(ValueError):
    """The inventory is malformed or of an unknown schema; the executor then ends every
    part ``unavailable`` (design §9)."""


def _str(v: object) -> bool:
    return isinstance(v, str) and bool(v) and '\0' not in v


def _abs(v: object) -> bool:
    return _str(v) and v.startswith('/')


def shared_path(v: object) -> bool:
    """A file the CI account shares with local accounts (decision 12.9): directly in
    ``/tmp``, a plain name (no ``/`` after ``/tmp/``, no leading ``.``, so no ``..``). Root
    makes it root-owned with ACL entries (§5.3), so it may name nothing else."""
    return _str(v) and re.fullmatch(r'/tmp/[A-Za-z0-9_][A-Za-z0-9._-]*', v) is not None


def _int(v: object, lo: int = 0) -> bool:
    return type(v) is int and v >= lo


def _num(v: object) -> bool:
    """A finite positive number (TOML also has ``inf`` and ``nan``; decision 19.7)."""
    return type(v) in (int, float) and math.isfinite(v) and v > 0


def cpu_ranges(v: object) -> list[tuple[int, int]] | None:
    """A cpuset list such as ``0-3,8-9`` as its ``(first, last)`` ranges, or None where it
    is not one (a range downwards, a CPU above MAX_CPU). Pure."""
    if not isinstance(v, str) or not _CPUS.match(v):
        return None
    out = []
    for part in v.split(','):
        lo, _, hi = part.partition('-')
        r = (int(lo), int(hi or lo))
        if r[0] > r[1] or r[1] > MAX_CPU:
            return None
        out.append(r)
    return out


def _slot(v: object) -> bool:
    """A pool's ``slot`` table (decision 16.2): ``cpus``, one cpuset per runner, no CPU in
    two of them; ``workers``, a count; ``memory``, a size; ``speed``, a finite positive number; and
    an optional ``isa``. Accepted and inert until R2 (16.3); its count is checked in load."""
    if not isinstance(v, dict) or set(v) - set(SLOT_KEYS) or any(r and k not in v for k, r in SLOT_KEYS.items()):
        return False
    sets = [cpu_ranges(c) for c in v['cpus']] if isinstance(v['cpus'], list) else []
    if not sets or None in sets:
        return False
    ranges = sorted(r for s in sets for r in s)
    if any(a[1] >= b[0] for a, b in zip(ranges, ranges[1:])):
        return False  # two cpusets share a CPU (or one names a CPU twice)
    return (_int(v['workers'], 1) and isinstance(v['memory'], str) and bool(_SIZE.match(v['memory']))
            and _num(v['speed']) and ('isa' not in v or isinstance(v['isa'], str) and bool(ISA.match(v['isa']))))


def _pool(v: object) -> bool:
    """A pool: a runner count, or a table (RUN-5) of ``count``, ``labels`` (the runners'
    labels instead of ``<pool>, <role>``) and ``per_card`` (one runner per card of the
    host's ``gpus``, with no ``count``); from R1 also ``slot`` and ``audience`` (16.3)."""
    if _int(v, 1):
        return True
    if not isinstance(v, dict) or not v or set(v) - POOL_KEYS:
        return False
    if 'slot' in v and not _slot(v['slot']) or 'audience' in v and v['audience'] not in AUDIENCES:
        return False
    per_card = v.get('per_card', False)
    if type(per_card) is not bool or per_card == ('count' in v):
        return False
    if 'count' in v and not _int(v['count'], 1):
        return False
    if 'class' in v and not isinstance(v['class'], str):
        return False
    labels = v.get('labels', [])
    return (isinstance(labels, list) and len(labels) <= 8 and len(set(labels)) == len(labels)
            and all(isinstance(x, str) and LABEL.match(x) and x not in RESERVED_LABELS for x in labels) and ('labels' not in v or bool(labels)))


def _peer_labels(v: object) -> bool:
    """``peer_labels``: a table from a runner class to the labels of the other host's pool
    of that class (decision 13.3), each a label a pool may give itself."""
    return isinstance(v, dict) and all(
        k in CLASSES and isinstance(ls, list) and 0 < len(ls) <= 8 and len(set(ls)) == len(ls)
        and all(isinstance(x, str) and LABEL.match(x) and x not in RESERVED_LABELS for x in ls)
        for k, ls in v.items())


#: the ``busy`` table's keys (decision 13.2): the windows in whole minutes, the rest positive
BUSY_KEYS = {'window_minutes': int, 'lock_minutes': int, 'cpu_cores': float, 'memory_gib': float, 'gpu_mib': float}


def _busy(v: object) -> bool:
    return isinstance(v, dict) and set(v) <= set(BUSY_KEYS) and all(
        (_int(x, 1) if BUSY_KEYS[k] is int else _num(x)) for k, x in v.items())


#: a host's ``presence`` table (decision 16.4): ``presence = { enabled = false }`` is its
#: rollback; accepted and inert until R3 (16.3)
PRESENCE_KEYS = {'enabled': bool, 'tick_seconds': int, 'infra_failures': int}


def _presence(v: object) -> bool:
    return isinstance(v, dict) and set(v) <= set(PRESENCE_KEYS) and all(
        (type(x) is bool if PRESENCE_KEYS[k] is bool else _int(x, 1)) for k, x in v.items())


def peer_labels(inv: 'Inventory', cls: str) -> list[str] | None:
    """The labels of the other host's pool of class ``cls`` (decision 13.3), or None where
    the inventory names none. Pure."""
    v = inv.host.get('peer_labels')
    return list(v[cls]) if isinstance(v, dict) and cls in v else None


#: a Unix account name (what useradd accepts, and what a tmpfiles.d ACL entry may name)
ACCOUNT = re.compile(r'[a-z_][a-z0-9_-]{0,31}')
#: an ed25519 public key, with or without its comment (the comment is replaced)
PUSH_KEY = re.compile(r'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI[A-Za-z0-9+/]{43}(?: [!-~]{1,64})?')
#: the slot files and the host lock (brainspike's on the 3090 hosts, decision 12.9), and the
#: GPU leases' directory where the pool runs of other accounts share them (decision 15.1)
FILES = frozenset({'slot_lock', 'slot_queue', 'host_lock', 'lease_dir'})
#: per host: key -> (check, required). Paths are absolute.
HOST_KEYS = {
    'account': (_str, True),         # the Unix account the runners and parts run as
    'home': (_abs, True),
    'data': (_abs, True),            # runners, work directories and ci-runners live below it
    'slots': (lambda v: _int(v, 1), True),         # host CI slots (design §7)
    'memory_max': (lambda v: isinstance(v, str) and bool(_SIZE.match(v)), True),  # ci.slice
    'disk_min_gib': (_int, True),
    'gpus': (lambda v: isinstance(v, list) and all(_int(i) for i in v), True),
    'max_temp_c': (lambda v: _int(v, 1), True),
    'cpus': (lambda v: isinstance(v, str) and bool(_CPUS.match(v)), False),  # taskset list
    'compile_cpus': (lambda v: isinstance(v, str) and bool(_CPUS.match(v)), False),  # compile parts' (12.7)
    'pools': (lambda v: isinstance(v, dict) and bool(v) and all(isinstance(k, str) and _POOL.match(k) and _pool(n)
                                                                for k, n in v.items()), True),
    'files': (lambda v: isinstance(v, dict) and {'slot_lock', 'slot_queue'} <= set(v) <= FILES
              and all(_abs(p) for p in v.values()), True),  # the slot files (design §7)
    'tunnel': (lambda v: isinstance(v, dict), False),  # the ssh forward to the egress proxy (h100, decision 11.10)
    'python': (_abs, False),         # the host layer's interpreter, 3.11 or later; else /usr/bin/python3
    'orgsync': (lambda v: type(v) is bool, False),  # runs ci-orgsync.timer: the org snapshot (§6.5)
    # the project of a slot ticket without one: ci_slot.sh's (design §7 rule 4, decision 12.9)
    'default_project': (lambda v: isinstance(v, str) and bool(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,39}', v)), False),
    # the local accounts that share the host lock and slot files with the CI account: the
    # root step's ACL entries (decision 12.9, §5.3); with it, setup plans that root step
    'local_accounts': (lambda v: isinstance(v, list) and bool(v) and len(set(v)) == len(v)
                       and all(isinstance(x, str) and ACCOUNT.fullmatch(x) for x in v), False),
    # the org snapshot's push key (decision 12.4): its line in the account's authorized_keys
    'push_key': (lambda v: isinstance(v, str) and bool(PUSH_KEY.fullmatch(v)), False),
    # the other hosts' CI accounts' pool probe keys (decision 15.1, item 8): one key, or from 15.8 a
    # list of them, one per calling host; each its line, forced to pool probe
    'probe_key': (lambda v: bool(probe_keys(v)) and all(PUSH_KEY.fullmatch(k) for k in probe_keys(v)), False),
    # the other host's pool of each runner class: where a drained or busy part goes (decision 13.3)
    'peer_labels': (lambda v: _peer_labels(v), False),
    # the busy rule's thresholds (decision 13.2); each optional, with the rule's defaults
    'busy': (lambda v: _busy(v), False),
    # the presence controller's switch and bounds (decision 16.4): accepted, inert until R3 (16.3)
    'presence': (lambda v: _presence(v), False),
    # the public pool's isolation backend (decision 18.2, 18.7): accepted, inert until a later phase
    'isolation': (lambda v: isinstance(v, str) and v in ISOLATION, False),
}
#: the first path's keys (decision 11.12): accepted and ignored in v1.3, so that a host's
#: inventory needs no change in the release's window (design §9); nothing reads them
RETIRED_KEYS = frozenset({'ssh', 'trusted', 'remote', 'cpu_slowdown', 'proxy'})
HOST_KEYS.update({k: (lambda v: True, False) for k in RETIRED_KEYS})
_PORT = (lambda v: _int(v, 1) and v < 65536, True)
#: a DNS name the egress proxy passes (its own check, lower case, two labels or more)
NAME = re.compile(r'(?=.{1,253}\Z)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+\Z')
_DIRECT = re.compile(r'\.?[a-z0-9][a-z0-9.-]{0,252}\Z|[0-9a-f:.]{2,39}\Z')
TUNNEL_KEYS = {
    'http_port': _PORT,    # ci-tunnel's forward on 127.0.0.1: the runners' https_proxy
    'egress_port': _PORT,  # the egress proxy's port on the egress host's 127.0.0.1 (ci-egress.service)
    # what the runners reach without the tunnel: their no_proxy, one name, suffix or address each.
    # The allowlist is the egress host's (setup/egress/allow), not the inventory's (decision 11.10)
    'direct': (lambda v: isinstance(v, list) and all(isinstance(n, str) and _DIRECT.match(n) for n in v), True),
}


@dataclasses.dataclass(frozen=True)
class Inventory:
    this: str  # the role of the host that reads it
    hosts: Mapping[str, Mapping[str, object]]  # each host's ``pools`` without its public pool
    #: per host role, its public pool (decision 18.7): ``{pool: spec}``, taken out of the
    #: host's ``pools`` at load, so that setup, presence, the route and the executor, which
    #: read ``pools``, see the inventory they saw before the key existed (16.3)
    public: Mapping[str, Mapping[str, Mapping[str, object]]] = dataclasses.field(default_factory=dict)

    @property
    def host(self) -> Mapping[str, object]:
        return self.hosts[self.this]


def _check_table(where: str, t: object, keys: Mapping[str, tuple]) -> None:
    if not isinstance(t, dict):
        raise InventoryError(f'{where}: not a table')
    unknown = set(t) - set(keys)
    if unknown:
        raise InventoryError(f'{where}: unknown keys {sorted(unknown)}')
    for k, (ok, required) in keys.items():
        if k not in t:
            if required:
                raise InventoryError(f'{where}: {k} is missing')
        elif not ok(t[k]):
            raise InventoryError(f'{where}: {k} is malformed')


def load(text: str) -> Inventory:
    """Parse and validate ``inventory.toml``. An unknown ``schema`` raises
    InventoryError, as does any unknown, missing or malformed key. Pure."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise InventoryError(f'inventory: not TOML: {e}') from None
    if doc.get('schema') != SCHEMA:
        raise InventoryError(f'inventory: schema {doc.get("schema")!r} is not {SCHEMA}')
    if set(doc) - {'schema', 'this', 'hosts'}:
        raise InventoryError(f'inventory: unknown keys {sorted(set(doc) - {"schema", "this", "hosts"})}')
    hosts = doc.get('hosts')
    if not isinstance(hosts, dict) or not hosts:
        raise InventoryError('inventory: no hosts')
    for role, h in hosts.items():
        if not _ROLE.match(role):
            raise InventoryError(f'inventory: host role {role!r} is malformed')
        _check_table(f'hosts.{role}', h, HOST_KEYS)
        if 'tunnel' in h:
            _check_table(f'hosts.{role}.tunnel', h['tunnel'], TUNNEL_KEYS)
        seen: dict[tuple[str, bool], str] = {}
        for pool, v in h['pools'].items():
            if isinstance(v, dict) and v.get('per_card') and not h['gpus']:
                raise InventoryError(f'hosts.{role}.pools.{pool}: per_card on a host without gpus')
            cls = _class(pool, v)
            if cls not in CLASSES:
                raise InventoryError(f'hosts.{role}.pools.{pool}: no runner class {cls!r}: name one in its class')
            if isinstance(v, dict) and v.get('per_card') and cls not in PER_CARD_CLASSES:
                raise InventoryError(f'hosts.{role}.pools.{pool}: per_card for a class of {sorted(PER_CARD_CLASSES)} only')
            pub = _public(v)
            if pub:
                _check_public(f'hosts.{role}.pools.{pool}', role, cls, v)
            else:
                if pool.startswith('pub-gpu'):  # its runners' names would read as the public pool's (C2c)
                    raise InventoryError(f'hosts.{role}.pools.{pool}: a private pool\'s key starts with pub-gpu')
                _check_private_labels(f'hosts.{role}.pools.{pool}', role, pool, v)
            if (cls, pub) in seen and cls != HOME_CLASS:  # the home pool: _check_home
                raise InventoryError(f'hosts.{role}.pools.{pool}: class {cls} is also the class of {seen[cls, pub]}')
            if isinstance(v, dict) and 'slot' in v:  # one cpuset per runner (16.2)
                n = len(h['gpus']) if v.get('per_card') else v['count']
                if len(v['slot']['cpus']) != n:
                    raise InventoryError(f'hosts.{role}.pools.{pool}: slot.cpus has {len(v["slot"]["cpus"])} '
                                         f'cpusets for {n} runners')
            seen[cls, pub] = pool
        _check_home(role, h)
        if any(pub for _, pub in seen) and ('gpu', False) not in seen:
            raise InventoryError(f'hosts.{role}: a public pool shares the cards of the host\'s own gpu pool: name one')
        if any(pub for _, pub in seen) and 'isolation' not in h:
            raise InventoryError(f'hosts.{role}: a public pool needs the host\'s isolation')
        if 'local_accounts' in h:  # shared with local accounts: root's step names /tmp only
            for k, p in h['files'].items():
                if not shared_path(p):
                    raise InventoryError(f'hosts.{role}.files.{k}: with local_accounts, a plain name in /tmp')
    this = doc.get('this')
    if this not in hosts:
        raise InventoryError('inventory: this names no host')
    public = {}
    for role, h in hosts.items():  # the public pools out of ``pools``: every reader of it ignores them
        pub = {k: v for k, v in h['pools'].items() if _public(v)}
        if pub:
            public[role] = pub
            h['pools'] = {k: v for k, v in h['pools'].items() if k not in pub}
    return Inventory(this, hosts, public)


def _public(v: object) -> bool:
    """Whether a pool is of audience ``public`` (decision 16.7). Pure."""
    return isinstance(v, dict) and v.get('audience') == 'public'


def _check_public(where: str, role: str, cls: str, v: Mapping[str, object]) -> None:
    """A public pool (decision 18.7): per card, of class ``gpu``, labelled ``ci-pub-gpu`` and
    at most its host label ``<role>-pub-gpu``, with no count or slot of its own."""
    if cls not in PUBLIC_CLASSES:
        raise InventoryError(f'{where}: a public pool is of class {"/".join(sorted(PUBLIC_CLASSES))} only')
    if set(v) - PUBLIC_POOL_KEYS:  # no count, so per card (``_pool``)
        raise InventoryError(f'{where}: a public pool is per card and takes {sorted(PUBLIC_POOL_KEYS)} only')
    labels = v.get('labels', [])
    if PUBLIC_LABEL not in labels or not set(labels) <= {PUBLIC_LABEL, f'{role}{PUBLIC_HOST_SUFFIX}'}:
        raise InventoryError(f'{where}: a public pool\'s labels are {PUBLIC_LABEL} and {role}-pub-gpu only')


def _check_private_labels(where: str, role: str, pool: str, v: object) -> None:
    """No private pool carries a label of the public pool's (decision 18.7), its own or the
    ``<pool>, <role>`` its runners carry without them (setup's rule, §5.1). Pure."""
    labels = (v.get('labels') if isinstance(v, dict) else None) or [pool, role]
    if any(x.startswith(PUBLIC_PREFIX) or x.endswith(PUBLIC_HOST_SUFFIX) for x in labels):
        raise InventoryError(f'{where}: a label of the public pool on a private pool')


def public_pool(inv: Inventory, role: str | None = None) -> tuple[str, Mapping[str, object]] | None:
    """The public pool of the host ``role`` (this host by default) as ``(key, spec)``, or None
    (decision 18.7). Pure."""
    pools = inv.public.get(inv.this if role is None else role) or {}
    return next(iter(pools.items()), None)


def _class(pool: str, v: object) -> str:
    """A pool's runner class: its ``class``, else its key (decision 12.5). Pure."""
    return str(v['class']) if isinstance(v, dict) and 'class' in v else pool


def _capable(v: object) -> bool:
    """Whether a pool gives its runners a capability label (a ``ci-`` one, 16.1). Pure."""
    return isinstance(v, dict) and any(str(x).startswith('ci-') for x in v.get('labels') or ())


def _check_home(role: str, h: Mapping[str, object]) -> None:
    """A host's two pools of class light (issue #232): one, the light pool, carries a
    ``ci-`` label and the other, the home pool, none; the home pool names its labels, one of
    them its own (no other pool of the host gives it) and the rest the light pool's, and has
    no ``slot`` or ``audience``
    (per_card is a gpu pool's only). One light pool or none: nothing to check."""
    pools = h['pools']
    light = [p for p, v in pools.items() if _class(p, v) == HOME_CLASS]
    if len(light) < 2:
        return
    where = f'hosts.{role}.pools'
    if len(light) > 2:
        raise InventoryError(f'{where}: {len(light)} pools of class light ({", ".join(light)}): '
                             'a light pool and a home pool at most')
    capable = [p for p in light if _capable(pools[p])]
    if len(capable) != 1:
        raise InventoryError(f'{where}: two pools of class light ({", ".join(light)}), and '
                             f'{"both carry" if capable else "neither carries"} a ci- label: the light pool carries '
                             'one, the home pool none')
    home = next(p for p in light if p not in capable)
    v = pools[home]
    if not isinstance(v, dict) or 'labels' not in v:
        raise InventoryError(f'{where}.{home}: the home pool names its labels (<role>-home, <role>-light)')
    if 'slot' in v or 'audience' in v:
        raise InventoryError(f'{where}.{home}: the home pool takes no slot and no audience')
    others = {x for p, w in pools.items() if p != home
              for x in (w.get('labels') if isinstance(w, dict) and w.get('labels') else [p, role])}
    others |= {f'{role}-gpu{c}' for p, w in pools.items() if isinstance(w, dict) and w.get('per_card') for c in h['gpus']}
    if set(v['labels']) <= others:
        raise InventoryError(f'{where}.{home}: no label of the home pool is its own: give it one that no other '
                             'pool of the host carries (<role>-home)')
    lights = labels_of(h, capable[0]) or [capable[0], role]
    if set(v['labels']) & others - set(lights):  # another pool's jobs would land on it
        raise InventoryError(f'{where}.{home}: the home pool shares a label with a pool other than the light pool: '
                             'its labels are its own and the light pool\'s (<role>-home, <role>-light)')


def home_pool(host: Mapping[str, object]) -> str | None:
    """The host's home pool (design §5.1, decision 16.4; issue #232): of its two pools of
    class light, the one without a ``ci-`` label; None on a host with one light pool or
    none. Pure."""
    light = [p for p, v in host['pools'].items() if _class(p, v) == HOME_CLASS]
    home = [p for p in light if not _capable(host['pools'][p])]
    return home[0] if len(light) == 2 and len(home) == 1 else None


def pool_class(host: Mapping[str, object], pool: str) -> str:
    """The runner class of a pool of ``host`` (design §5.1). Pure."""
    return _class(pool, host['pools'][pool])


def pool_of(host: Mapping[str, object], cls: str) -> str | None:
    """The pool of ``host`` whose runners have class ``cls``, or None: one at most, but for
    class light beside the home pool (``home_pool``), where it is the light pool, the one
    with a ``ci-`` label (issue #232). Pure."""
    mine = [p for p, v in host['pools'].items() if _class(p, v) == cls]
    if len(mine) > 1:
        mine = [p for p in mine if p != home_pool(host)]
    return mine[0] if mine else None


def probe_keys(v: object) -> list[str]:
    """The inventory's ``probe_key`` as a list (decision 15.8): one key is a list of one; a
    value of neither form, or a list holding a non-string, is an empty list. Pure."""
    if isinstance(v, str):
        return [v] if v else []
    if isinstance(v, list) and all(isinstance(k, str) and k for k in v):
        return list(v)
    return []


def pool_labels(inv: 'Inventory', cls: str) -> list[str] | None:
    """The labels the pool of class ``cls`` of this host gives its runners (RUN-5, design
    §5.1), or None where it gives none and the runners carry ``<pool>, <role>``. A per-card
    pool's runners carry also their card's ``<role>-gpu<N>``, which is not the pool's."""
    pool = pool_of(inv.host, cls)
    return None if pool is None else labels_of(inv.host, pool)


def labels_of(host: Mapping[str, object], pool: str) -> list[str] | None:
    """The labels the pool ``pool`` of ``host`` gives its runners, by its key (the home pool
    and the light pool share a class, issue #232), or None where it gives none. Pure."""
    v = host['pools'].get(pool)
    if isinstance(v, dict) and v.get('labels'):
        return list(v['labels'])
    return None


# ------------------------------------------------------------------ deny list

#: string values that are not inventory: they appear in the design already. Every other string value of the inventory and the policy is denied.
PUBLIC_KEYS = frozenset({'this', 'memory_max', 'cpus', 'compile_cpus', 'host_lock', 'slot_lock', 'slot_queue', 'bot',
                         'visibility', 'extra_events', 'labels', 'default_project', 'class', 'classes',
                         'peer_labels', 'memory', 'isa', 'audience', 'isolation'})
#: ``tunnel.direct`` values the repository holds (the example, the tests): public, so not
#: denied. Every other ``direct`` value is denied, as any inventory value is.
#: the policy's ``[org] name`` is the org's, public as the repository is.
PUBLIC_DIRECT = frozenset({'localhost', '127.0.0.1', '::1', 'api.github.com', '.actions.githubusercontent.com',
                           '.blob.core.windows.net'})


def _tunnel_key(path: tuple) -> str | None:
    """The key name for the path of a host's tunnel key (``direct``), else None."""
    return path[3] if len(path) == 4 and path[0] == 'hosts' and path[2] == 'tunnel' else None
#: tokens too common to deny; a value that is one of them is left out, with a short one
#: ``ssh-ed25519``: a push key's type word, which the code names; the key itself is denied
_COMMON = frozenset({'localhost', 'home', 'data', 'true', 'false', 'none', 'ssh-ed25519'})
MIN_TOKEN = 4

_SALT = b'ci-runners/deny/1\0'
_SEP = r'./:@~\[\]_-'
#: a word the lint reads: letters and digits of any script, joined by separators; a part
#: is a run of letters and digits
_WORD = re.compile(r'(?:[^\W_]|[' + re.escape(_SEP) + r'])+')
_PART = re.compile(r'[^\W_]+')
#: the longest run of parts a candidate substring may span; deny tokens are shorter
MAX_PARTS = 16


def normalise(token: str) -> str:
    """A token as it is hashed: lower case, without leading or trailing separators."""
    return token.strip(_SEP).lower()


def hash_token(token: str) -> str:
    """The salted SHA-256 of a normalised token, hex. Pure."""
    return hashlib.sha256(_SALT + normalise(token).encode()).hexdigest()


def _strings(doc: object, path: tuple = ()) -> Iterator[str]:
    if isinstance(doc, dict):
        for k, v in doc.items():
            if k not in PUBLIC_KEYS and (*path, k) != ('org', 'name'):
                yield from _strings(v, (*path, k))
    elif isinstance(doc, list):
        for v in doc:
            if not (_tunnel_key(path) == 'direct' and v in PUBLIC_DIRECT):
                yield from _strings(v, path)
    elif isinstance(doc, str):
        yield doc


def deny_tokens(*docs: Mapping[str, object]) -> set[str]:
    """The tokens of parsed inventory and policy documents that the repository must not
    hold: every string value outside PUBLIC_KEYS, split on white space and commas, of at
    least MIN_TOKEN characters and not a common word. Table keys (host roles, repository
    names) are not tokens. Pure."""
    out = set()
    for doc in docs:
        for s in _strings(doc):
            for m in _WORD.finditer(s):  # split where the lint splits words
                t = normalise(m.group())
                spans = [p.span() for p in _PART.finditer(t)]
                if len(spans) > MAX_PARTS:  # the lint spans at most MAX_PARTS parts: deny the head
                    t = t[:spans[MAX_PARTS - 1][1]]
                if len(t) >= MIN_TOKEN and t not in _COMMON:
                    out.add(t)
    return out


def read_deny(text: str) -> frozenset[str]:
    """Hashes from a deny file: one hex digest a line; ``#`` starts a comment."""
    out = set()
    for raw in text.splitlines():
        s = raw.split('#', 1)[0].strip()
        if s:
            if not re.fullmatch(r'[0-9a-f]{64}', s):
                raise ValueError('deny list: a line is not a sha256 hex digest')
            out.add(s)
    return frozenset(out)


# ------------------------------------------------------------------ the lint

_PATTERNS = {
    'ipv4': re.compile(r'(?<![\w.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?!\w|\.\d)'),
    'home-path': re.compile(r'/(?:home|Users)/(?!runner(?![\w.-]))(?=\w)[\w.-]+'),
    'github-token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})'),
    'private-key': re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'),
    # a host's account uid (user@<uid>.service, user-<uid>.slice, /run/user/<uid>, uid=<uid>,
    # uid <uid>): the inventory names accounts, never uids, so a real one can only come
    # from a host (RUN-8 review)
    'uid': re.compile(r'(?:\buser[@-]|/run/user/|\buid\s*[=:]?\s*)(\d{4,5})(?!\d)', re.I),
    # a block device's numbers, as mountinfo (decimal) and /proc/locks (hex) print them
    'device': re.compile(r'(?<![\w:.])\d+ \d+ (\d+):\d+ /'
                         r'|\b(?:FLOCK|POSIX|OFDLCK)\s+\S+\s+(?:READ|WRITE)\s+\d+\s+([0-9a-f]+):[0-9a-f]+:\d+'),
}
#: the uids and device majors that are not a host's: a fixture uses these
FAKE_UIDS = frozenset({4242})
#: majors that name no real disk: 0 (anonymous: tmpfs, proc) and 240-254 (local or
#: experimental use, Linux's devices.txt)
FAKE_MAJORS = frozenset({0, *range(240, 255)})
#: IPv4 addresses that are not inventory: loopback, the unspecified address, the
#: documentation ranges (RFC 5737)
_PUBLIC_IP = re.compile(r'127\.|0\.0\.0\.0$|192\.0\.2\.|198\.51\.100\.|203\.0\.113\.')


def _candidates(text: str) -> Iterator[str]:
    """Every substring of a word that starts and ends on part boundaries, lower case."""
    for m in _WORD.finditer(text):
        word = m.group()
        spans = [p.span() for p in _PART.finditer(word)]
        for i, (a, _) in enumerate(spans):
            for _, b in spans[i:i + MAX_PARTS]:
                yield word[a:b].lower()


@dataclasses.dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str  # denied, ipv4, home-path, github-token, private-key, uid, device

    def __str__(self) -> str:
        return f'{self.path}:{self.line}: {self.kind}'


def scan(text: str, deny: frozenset[str] | set[str], path: str = '') -> list[Finding]:
    """The findings in ``text``: a token whose hash is in ``deny``, or a pattern of
    _PATTERNS. One finding per line and kind. Pure."""
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        kinds = []
        if deny and any(hashlib.sha256(_SALT + c.encode()).hexdigest() in deny for c in _candidates(line)):
            kinds.append('denied')
        for kind, rx in _PATTERNS.items():
            for m in rx.finditer(line):
                if kind == 'ipv4' and (not all(int(g) <= 255 for g in m.groups()) or _PUBLIC_IP.match(m.group())):
                    continue
                if kind == 'uid' and not (1000 <= int(m.group(1)) <= 65533 and int(m.group(1)) not in FAKE_UIDS):
                    continue  # a system account's, or a fixture's
                if kind == 'device' and (int(m.group(1)) if m.group(1) else int(m.group(2), 16)) in FAKE_MAJORS:
                    continue
                kinds.append(kind)
                break
        out += [Finding(path, n, k) for k in kinds]
    return out


def tracked_files(root: str) -> list[str]:
    """The files git tracks under ``root`` (every file if it is not a checkout)."""
    try:
        out = subprocess.run(['git', '-C', root, 'ls-files', '-z'], capture_output=True, check=True, timeout=60)
        return sorted(p for p in out.stdout.decode().split('\0') if p)
    except (OSError, subprocess.SubprocessError):
        files = []
        for d, dirs, names in os.walk(root):
            dirs[:] = [x for x in dirs if x != '.git']
            files += [os.path.relpath(os.path.join(d, x), root) for x in names]
        return sorted(files)


def lint(root: str, deny: frozenset[str] | set[str], files: Iterable[str] | None = None) -> list[Finding]:
    """Scan every tracked file under ``root`` (binary files, with a NUL in their first
    8 KiB, are skipped). File names are scanned as well as contents."""
    out = []
    for n, rel in enumerate(tracked_files(root) if files is None else files, 1):
        # a finding in a name must not print the name: it says which file, by number
        out += [Finding(f'<the name of tracked file {n}>', 0, f.kind) for f in scan(rel, deny)]
        try:
            with open(os.path.join(root, rel), 'rb') as f:
                data = f.read()
        except OSError:
            continue  # deleted in the work tree
        if b'\0' in data[:8192]:
            continue
        out += scan(data.decode('utf-8', 'replace'), deny, rel)
    return out


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.split('CLI (stdlib only)::', 1)[1], file=sys.stderr)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == 'check' and len(args) == 1:
        with open(args[0], encoding='utf-8') as f:
            load(f.read())
        return 0
    if cmd == 'deny' and args:
        docs = []
        for p in args:
            with open(p, 'rb') as f:
                docs.append(tomllib.load(f))
        print('\n'.join(sorted(hash_token(t) for t in deny_tokens(*docs))))
        return 0
    if cmd == 'hash' and not args:
        toks = {normalise(t) for t in sys.stdin.read().split()}
        print('\n'.join(sorted(hash_token(t) for t in toks if t)))
        return 0
    if cmd == 'lint':
        deny_file = None
        if '--deny' in args:
            i = args.index('--deny')
            deny_file = args[i + 1]
            args = args[:i] + args[i + 2:]
        root = args[0] if args else '.'
        deny: frozenset[str] = frozenset()
        if deny_file:
            with open(deny_file, encoding='utf-8') as f:
                deny = read_deny(f.read())
        findings = lint(root, deny)
        for f in findings:
            print(f)
        print(f'inventory lint: {len(findings)} finding(s), {len(deny)} denied hashes', file=sys.stderr)
        return 1 if findings else 0
    print(f'unknown command {cmd!r}', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
