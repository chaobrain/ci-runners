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
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterable, Iterator, Mapping

SCHEMA = 1

# ------------------------------------------------------------------ schema 1

_ROLE = re.compile(r'[a-z0-9][a-z0-9-]{0,30}\Z')
_POOL = re.compile(r'[a-z][a-z0-9-]{0,30}\Z')
_SIZE = re.compile(r'[1-9][0-9]{0,5}[KMGT]\Z')
_CPUS = re.compile(r'[0-9]+(?:-[0-9]+)?(?:,[0-9]+(?:-[0-9]+)?)*\Z')
#: a runner label the inventory gives (design §5.1): GitHub's own are added at registration
LABEL = re.compile(r'[a-z0-9][a-z0-9-]{0,39}\Z')
#: labels a pool may not give itself: GitHub's own, and the 3090 pools' classes, which a
#: job of another host's policy asks for (design §5.1: h100's labels share nothing with them)
RESERVED_LABELS = frozenset({'self-hosted', 'linux', 'x64', 'light', 'part', 'compile', 'nightly', 'gpu'})
#: what a pool's runners run with, besides the count (RUN-5)
POOL_KEYS = frozenset({'count', 'labels', 'per_card'})


class InventoryError(ValueError):
    """The inventory is malformed or of an unknown schema; the executor then ends every
    part ``unavailable`` (design §9)."""


def _str(v: object) -> bool:
    return isinstance(v, str) and bool(v) and '\0' not in v


def _abs(v: object) -> bool:
    return _str(v) and v.startswith('/')


def _int(v: object, lo: int = 0) -> bool:
    return type(v) is int and v >= lo


def _num(v: object) -> bool:
    return type(v) in (int, float) and v > 0


def _pool(v: object) -> bool:
    """A pool: a runner count, or a table (RUN-5) of ``count``, ``labels`` (the runners'
    labels instead of ``<pool>, <role>``) and ``per_card`` (one runner per card of the
    host's ``gpus``, with no ``count``)."""
    if _int(v, 1):
        return True
    if not isinstance(v, dict) or not v or set(v) - POOL_KEYS:
        return False
    per_card = v.get('per_card', False)
    if type(per_card) is not bool or per_card == ('count' in v):
        return False
    if 'count' in v and not _int(v['count'], 1):
        return False
    labels = v.get('labels', [])
    return (isinstance(labels, list) and len(labels) <= 8 and len(set(labels)) == len(labels)
            and all(isinstance(x, str) and LABEL.match(x) and x not in RESERVED_LABELS for x in labels) and ('labels' not in v or bool(labels)))


#: per host: key -> (check, required). Paths are absolute.
HOST_KEYS = {
    'ssh': (_str, True),             # how a trusted host reaches this one (place, salvage)
    'account': (_str, True),         # the Unix account the runners and parts run as
    'home': (_abs, True),
    'data': (_abs, True),            # runners, work directories and ci-runners live below it
    'trusted': (lambda v: type(v) is bool, True),  # runs checks, CPU and compile parts
    'remote': (lambda v: type(v) is bool, False),  # driven over ssh from another host (h100)
    'cpu_slowdown': (_num, True),    # per-core time against the reference host
    'slots': (lambda v: _int(v, 1), True),         # host CI slots (design §7)
    'memory_max': (lambda v: isinstance(v, str) and bool(_SIZE.match(v)), True),  # ci.slice
    'disk_min_gib': (_int, True),
    'gpus': (lambda v: isinstance(v, list) and all(_int(i) for i in v), True),
    'max_temp_c': (lambda v: _int(v, 1), True),
    'cpus': (lambda v: isinstance(v, str) and bool(_CPUS.match(v)), False),  # taskset list
    'pools': (lambda v: isinstance(v, dict) and bool(v) and all(isinstance(k, str) and _POOL.match(k) and _pool(n)
                                                                for k, n in v.items()), True),
    'files': (lambda v: isinstance(v, dict) and set(v) == {'host_lock', 'slot_lock', 'slot_queue'}
              and all(_abs(p) for p in v.values()), True),  # brainspike's lock and slot files (D5)
    'proxy': (lambda v: isinstance(v, dict), False),  # the CI proxy (3090 hosts, decision 5)
    'tunnel': (lambda v: isinstance(v, dict), False),  # the ssh egress and its bridge (h100, decision 11.8)
    'python': (_abs, False),         # the host layer's interpreter, 3.11 or later; else /usr/bin/python3
    'orgsync': (lambda v: type(v) is bool, False),  # runs ci-orgsync.timer: the org snapshot (§6.5)
    'watchdog_log': (_abs, False),   # the GPU watchdog's log (h100; design §4.4 thermal)
    'watchdog_notice': (_abs, False),  # the watchdog's notice file (h100 health gate, §5.4 phase 1)
}
PROXY_KEYS = {
    'client': (_abs, True),
    'config': (_abs, True),
    'nodes': (lambda v: isinstance(v, list) and bool(v) and all(_str(n) for n in v), True),
    'http_port': (lambda v: _int(v, 1) and v < 65536, True),
    'socks_port': (lambda v: _int(v, 1) and v < 65536, True),
    'no_proxy': (_str, True),
}
_PORT = (lambda v: _int(v, 1) and v < 65536, True)
#: a DNS name the bridge passes (the bridge's own check, lower case, two labels or more)
NAME = re.compile(r'[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+\Z')
_DIRECT = re.compile(r'\.?[a-z0-9][a-z0-9.-]{0,252}\Z|[0-9a-f:.]{2,39}\Z')
TUNNEL_KEYS = {
    'socks_port': _PORT,  # ci-tunnel's SOCKS5 on 127.0.0.1
    'http_port': _PORT,   # ci-bridge's HTTP CONNECT on 127.0.0.1
    # the names the bridge passes on port 443; not all numeric (no address literal)
    'allow': (lambda v: isinstance(v, list) and bool(v) and all(
        isinstance(n, str) and NAME.match(n) and not n.replace('.', '').isdigit() for n in v), True),
    # what the runners reach without the bridge: their no_proxy, one name, suffix or address each
    'direct': (lambda v: isinstance(v, list) and all(isinstance(n, str) and _DIRECT.match(n) for n in v), True),
}


@dataclasses.dataclass(frozen=True)
class Inventory:
    this: str  # the role of the host that reads it
    hosts: Mapping[str, Mapping[str, object]]

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
        if 'proxy' in h:
            _check_table(f'hosts.{role}.proxy', h['proxy'], PROXY_KEYS)
        if 'tunnel' in h:
            _check_table(f'hosts.{role}.tunnel', h['tunnel'], TUNNEL_KEYS)
            if 'proxy' in h:
                raise InventoryError(f'hosts.{role}: a CI proxy and a tunnel; give one')
            if h['tunnel']['socks_port'] == h['tunnel']['http_port']:
                raise InventoryError(f'hosts.{role}.tunnel: socks_port and http_port are the same')
        for pool, v in h['pools'].items():
            if isinstance(v, dict) and v.get('per_card') and not h['gpus']:
                raise InventoryError(f'hosts.{role}.pools.{pool}: per_card on a host without gpus')
    this = doc.get('this')
    if this not in hosts:
        raise InventoryError('inventory: this names no host')
    return Inventory(this, hosts)


def pool_labels(inv: 'Inventory', pool: str) -> list[str] | None:
    """The labels a pool of this host gives its runners (RUN-5, design §5.1), or None
    where it gives none and the runners carry ``<pool>, <role>``. A per-card pool's runners
    carry also their card's ``<role>-gpu<N>``, which is not the pool's."""
    v = inv.host['pools'].get(pool)
    if isinstance(v, dict) and v.get('labels'):
        return list(v['labels'])
    return None


# ------------------------------------------------------------------ deny list

#: string values that are not inventory: they appear in the design or in brainspike's
#: scripts already. Every other string value of the inventory and the policy is denied.
PUBLIC_KEYS = frozenset({'this', 'memory_max', 'cpus', 'host_lock', 'slot_lock', 'slot_queue', 'bot', 'visibility',
                         'extra_events', 'labels', 'allow', 'direct'})
#: tokens too common to deny; a value that is one of them is left out, with a short one
_COMMON = frozenset({'localhost', 'home', 'data', 'true', 'false', 'none'})
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


def _strings(doc: object, key: str = '') -> Iterator[str]:
    if isinstance(doc, dict):
        for k, v in doc.items():
            if k not in PUBLIC_KEYS:
                yield from _strings(v, k)
    elif isinstance(doc, list):
        for v in doc:
            yield from _strings(v, key)
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
}
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
    kind: str  # denied, ipv4, home-path, github-token, private-key

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
