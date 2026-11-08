"""The Slurm site config (design decision 14.6): schema, loader, prices and deny tokens.

A site config is ``etc/slurm/<site>.toml`` on each driver host, mode 600, and never in
the repository (``setup/slurm/site.example.toml`` holds placeholders). It names the
site's GPU request syntax and caps, its prices, its targets (a partition, its nodes and
cards) and the cluster accounts the parts run under. Every name in it is inventory
(design §3.3) but the keys of ``PUBLIC_KEYS``: ``deny`` prints the hashes of the rest
for ``tests/deny.sha256``, as ``ci_runners.inventory deny`` does for a host.

``account_entry`` is the cluster-side copy of one account's entry, which the gate
validates every request against (14.5): JSON, so that the gate reads it without
``tomllib``.

CLI (stdlib only)::

    python -m ci_runners.slurmsite check FILE           # validate the schema and the mode
    python -m ci_runners.slurmsite deny FILE...         # hashes of their tokens
    python -m ci_runners.slurmsite entry FILE ACCOUNT   # the account's cluster-side copy
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import stat
import sys
import tomllib
from collections.abc import Mapping

from ci_runners import inventory

SCHEMA = 1
#: the margin of a job's ``--time`` over the part's timeout, in minutes (14.3): what a
#: part's worst case is priced at
TIME_MARGIN = 5
#: the oldest Python the gate and the cluster supervisor run on: the release's own floor,
#: so that they run its code unchanged (``check`` verifies the login host's)
MIN_PYTHON = (3, 11)
EGRESS = ('direct', 'proxy', 'tunnel', 'push')

#: a site, target or account alias: a role name of ours, never the site's own name
NAME = re.compile(r'[a-z][a-z0-9-]{0,30}\Z')
#: a partition or a Slurm account
SLURM_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')
#: a node name or a Slurm host list (``node[1-4]``)
NODE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}(?:\[[0-9,-]{1,64}\])?\Z')
#: an ssh alias of the CI account's ssh config
SSH_ALIAS = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')
#: one ``sbatch`` argument as one token: ``--name`` or ``--name=value``, no white space
SBATCH_ARG = re.compile(r'--[a-z][a-z-]{0,30}(?:=[A-Za-z0-9_.,:=/+%-]{1,128})?\Z')
#: the site's GPU request: one ``sbatch`` argument with ``{n}`` once, the card count
GPU_REQUEST = re.compile(r'--[a-z][a-z-]{0,30}=[A-Za-z0-9_.,:=/+-]{0,64}\{n\}[A-Za-z0-9_.,:=/+-]{0,64}\Z')
SIZE = re.compile(r'[1-9][0-9]{0,6}[MGT]\Z')
VARIANT = re.compile(r'[a-z0-9][a-z0-9_.-]{0,30}\Z')
PROJECT = re.compile(r'[a-z0-9][a-z0-9-]{0,39}\Z')
_PATH = re.compile(r'/[A-Za-z0-9_.+/-]{1,200}\Z')
#: what the batch template sets itself: a site's ``submit_args`` may not set them again
OWN_ARGS = frozenset({'--account', '--partition', '--nodelist', '--exclude', '--time', '--signal', '--job-name',
                      '--comment', '--output', '--error', '--export', '--cpus-per-task', '--mem', '--ntasks',
                      '--nodes', '--chdir', '--gpus', '--gres', '--wrap', '--array', '--dependency', '--uid',
                      '--gid', '--get-user-env'})


class SiteError(ValueError):
    """The site config is malformed, of an unknown schema, or readable by others; the
    executor then ends every ``slurm`` part ``unavailable``."""


def _num(v: object, lo: float = 0.0) -> bool:
    return type(v) in (int, float) and v > lo


def _int(v: object, lo: int = 1) -> bool:
    return type(v) is int and v >= lo


def _path(v: object) -> bool:
    return isinstance(v, str) and bool(_PATH.fullmatch(v)) and '..' not in v.split('/') and '//' not in v


def _list(v: object, rx: re.Pattern, *, empty: bool = False) -> bool:
    return (isinstance(v, list) and (empty or bool(v)) and len(set(v)) == len(v)
            and all(isinstance(x, str) and rx.fullmatch(x) for x in v))


def _submit_args(v: object) -> bool:
    return _list(v, SBATCH_ARG, empty=True) and not any(x.split('=', 1)[0] in OWN_ARGS for x in v)


def _enabled(v: object) -> bool:
    return type(v) is bool or _list(v, PROJECT)


#: key -> (check, required)
SITE_KEYS = {
    'name': (lambda v: isinstance(v, str) and bool(NAME.fullmatch(v)), True),
    'gpu_request': (lambda v: isinstance(v, str) and bool(GPU_REQUEST.fullmatch(v)), True),
    'submit_args': (_submit_args, False),
    'login_python': (_path, True),
    'egress': (lambda v: v in EGRESS, True),
    'proxy': (lambda v: isinstance(v, str) and bool(re.fullmatch(r'https?://[A-Za-z0-9_.:@/-]{1,200}', v)), False),
    'max_parts': (_int, True),          # running cluster parts per driver host
    'pending_minutes': (_num, True),    # a target's pending bound, unless it sets its own
    'daily_cap': (_num, True),          # every account's spend, in the prices' currency
    'monthly_cap': (_num, True),
    'prices': (lambda v: isinstance(v, dict), True),
}
PRICE_KEYS = {
    'cpu_core_hour': (lambda v: type(v) in (int, float) and v >= 0, True),
    'cpu_on_gpu_jobs': (lambda v: type(v) is bool, True),  # a GPU job pays its CPUs too
    'card_hour': (lambda v: isinstance(v, dict) and all(VARIANT.fullmatch(k) and _num(p) for k, p in v.items()),
                  True),
}
TARGET_KEYS = {
    'partition': (lambda v: isinstance(v, str) and bool(SLURM_NAME.fullmatch(v)), True),
    # its -w: one node (Slurm's --nodelist asks for every node it names); or exclude
    'nodes': (lambda v: _list(v, NODE) and len(v) == 1 and '[' not in v[0], False),
    'exclude': (lambda v: _list(v, NODE), False),
    'gpu_model': (lambda v: isinstance(v, str) and bool(VARIANT.fullmatch(v)), False),  # a key of card_hour
    'cards_per_node': (_int, False),   # GPU targets
    'max_cards': (_int, False),        # GPU targets; 1 where the P2P canary fails (14.4)
    'cpus_per_card': (_int, False),    # GPU targets
    'cpus': (_int, False),             # CPU targets: a part's CPUs
    'variant': (lambda v: isinstance(v, str) and bool(VARIANT.fullmatch(v)), True),
    'mem_per_node': (lambda v: isinstance(v, str) and bool(SIZE.fullmatch(v)), True),
    'max_minutes': (_int, True),
    'pending_minutes': (_num, False),
    'enabled': (_enabled, False),      # true, or the projects it is enabled for (14.10)
}
GPU_ONLY = ('cards_per_node', 'max_cards', 'cpus_per_card')
ACCOUNT_KEYS = {
    'ssh': (lambda v: isinstance(v, str) and bool(SSH_ALIAS.fullmatch(v)), True),
    'root': (_path, True),             # on the shared file system; ci-runners lives below it
    'slurm_account': (lambda v: isinstance(v, str) and bool(SLURM_NAME.fullmatch(v)), True),
    'targets': (lambda v: _list(v, NAME), True),
    'weight': (_num, False),           # its share of the spend (14.8)
    'monthly_cap': (_num, True),
    'retired': (lambda v: type(v) is bool, False),  # uninstalled: no part uses it (14.9)
}


@dataclasses.dataclass(frozen=True)
class Target:
    name: str
    t: Mapping[str, object]

    @property
    def gpu(self) -> bool:
        return 'gpu_model' in self.t

    @property
    def max_cards(self) -> int:
        return int(self.t.get('max_cards', 0))

    @property
    def max_minutes(self) -> int:
        return int(self.t['max_minutes'])

    def cpus(self, cards: int) -> int:
        """A part's CPUs: its cards' share on a GPU target, the target's on a CPU one."""
        return cards * int(self.t['cpus_per_card']) if self.gpu else int(self.t['cpus'])

    def enabled_for(self, project: str) -> bool:
        """Whether the target counts for ``project`` (14.10): ``enabled`` true, or a list
        that names it. Absent: not enabled."""
        e = self.t.get('enabled', False)
        return e is True or (isinstance(e, list) and project in e)


@dataclasses.dataclass(frozen=True)
class Account:
    alias: str
    a: Mapping[str, object]

    @property
    def weight(self) -> float:
        return float(self.a.get('weight', 1.0))

    @property
    def retired(self) -> bool:
        return bool(self.a.get('retired', False))


@dataclasses.dataclass(frozen=True)
class Site:
    name: str
    site: Mapping[str, object]
    targets: Mapping[str, Target]
    accounts: Mapping[str, Account]

    @property
    def prices(self) -> Mapping[str, object]:
        return self.site['prices']  # type: ignore[return-value]

    def pending_minutes(self, target: str) -> float:
        return float(self.targets[target].t.get('pending_minutes', self.site['pending_minutes']))

    def hourly(self, target: str, cards: int, cpus: int) -> float:
        """What a job of ``cards`` and ``cpus`` on ``target`` costs an hour: its cards at
        the target's card price, and its CPUs on a CPU job or where a GPU job pays them too. Pure."""
        p = self.prices
        t = self.targets[target]
        cost = cards * float(p['card_hour'][t.t['gpu_model']]) if t.gpu and cards else 0.0
        if not (t.gpu and cards) or p['cpu_on_gpu_jobs']:
            cost += cpus * float(p['cpu_core_hour'])
        return cost

    def worst_cost(self, target: str, cards: int, minutes: float) -> float:
        """A part's worst case (14.7): its job at its full ``--time``, the part's timeout
        and ``TIME_MARGIN``. Pure."""
        cpus = self.targets[target].cpus(cards)
        return self.hourly(target, cards, cpus) * (minutes + TIME_MARGIN) / 60


def _check_table(where: str, t: object, keys: Mapping[str, tuple]) -> None:
    if not isinstance(t, dict):
        raise SiteError(f'{where}: not a table')
    unknown = set(t) - set(keys)
    if unknown:
        raise SiteError(f'{where}: unknown keys {sorted(unknown)}')
    for k, (ok, required) in keys.items():
        if k not in t:
            if required:
                raise SiteError(f'{where}: {k} is missing')
        elif not ok(t[k]):
            raise SiteError(f'{where}: {k} is malformed')


def _check_target(name: str, t: Mapping[str, object], card_hour: Mapping[str, object]) -> None:
    where = f'targets.{name}'
    if 'nodes' in t and 'exclude' in t:
        raise SiteError(f'{where}: nodes or exclude, not both')
    if 'gpu_model' in t:
        if t['gpu_model'] not in card_hour:
            raise SiteError(f'{where}: gpu_model {t["gpu_model"]} has no price in site.prices.card_hour')
        for k in GPU_ONLY:
            if k not in t:
                raise SiteError(f'{where}: a GPU target needs {k}')
        if t['max_cards'] > t['cards_per_node']:
            raise SiteError(f'{where}: max_cards is above cards_per_node')
        if 'cpus' in t:
            raise SiteError(f'{where}: a GPU target takes cpus_per_card, not cpus')
    else:
        for k in GPU_ONLY:
            if k in t:
                raise SiteError(f'{where}: {k} on a CPU target (no gpu_model)')
        if 'cpus' not in t:
            raise SiteError(f'{where}: a CPU target needs cpus')


def load(text: str) -> Site:
    """Parse and validate a site config. An unknown ``schema`` raises SiteError, as does
    any unknown, missing or malformed key, a GPU target without a price, ``max_cards``
    above ``cards_per_node`` and an account naming an unknown target. Pure."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise SiteError(f'site config: not TOML: {e}') from None
    if doc.get('schema') != SCHEMA:
        raise SiteError(f'site config: schema {doc.get("schema")!r} is not {SCHEMA}')
    top = {'schema', 'site', 'targets', 'accounts'}
    if set(doc) - top:
        raise SiteError(f'site config: unknown keys {sorted(set(doc) - top)}')
    site = doc.get('site')
    _check_table('site', site, SITE_KEYS)
    _check_table('site.prices', site['prices'], PRICE_KEYS)
    if (site['egress'] == 'proxy') != ('proxy' in site):
        raise SiteError('site: proxy is given with egress = "proxy", and only then')
    targets, accounts = doc.get('targets'), doc.get('accounts')
    for key, v in (('targets', targets), ('accounts', accounts)):
        if not isinstance(v, dict) or not v:
            raise SiteError(f'site config: no {key}')
        for name in v:
            if not NAME.fullmatch(name):
                raise SiteError(f'site config: {key} name {name!r} is malformed')
    for name, t in targets.items():
        _check_table(f'targets.{name}', t, TARGET_KEYS)
        _check_target(name, t, site['prices']['card_hour'])
    for alias, a in accounts.items():
        _check_table(f'accounts.{alias}', a, ACCOUNT_KEYS)
        unknown = [t for t in a['targets'] if t not in targets]
        if unknown:
            raise SiteError(f'accounts.{alias}: targets {unknown} are not targets of the site')
    return Site(site['name'], site, {n: Target(n, t) for n, t in targets.items()},
                {n: Account(n, a) for n, a in accounts.items()})


def load_file(path: str) -> Site:
    """Load a site config from ``path``, which only its owner may read (mode 600)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as e:
        raise SiteError(f'site config: {type(e).__name__}') from None
    with os.fdopen(fd, 'rb') as f:
        if stat.S_IMODE(os.fstat(f.fileno()).st_mode) & 0o077:
            raise SiteError('site config: readable by others: chmod 600')
        text = f.read().decode('utf-8')
    site = load(text)
    if os.path.basename(path) != f'{site.name}.toml':
        raise SiteError(f'site config: site.name {site.name} is not its file name')
    return site


def site_dir(etc: str) -> str:
    return os.path.join(etc, 'slurm')


def site_files(etc: str) -> dict[str, str]:
    """The site configs of ``etc/slurm``, by site name (their file name)."""
    d = site_dir(etc)
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return {}
    return {n[:-5]: os.path.join(d, n) for n in names if n.endswith('.toml') and NAME.fullmatch(n[:-5])}


def account_entry(site: Site, alias: str) -> dict[str, object]:
    """The cluster-side copy of account ``alias`` (14.5): what the gate validates a
    request against and renders the batch script from, and nothing of the other accounts.
    JSON-serialisable. Pure."""
    a = site.accounts[alias]
    keep = ('partition', 'nodes', 'exclude', 'gpu_model', 'cards_per_node', 'max_cards', 'cpus_per_card', 'cpus',
            'variant', 'mem_per_node', 'max_minutes')
    return {
        'schema': SCHEMA, 'site': site.name, 'account': alias, 'slurm_account': a.a['slurm_account'],
        'root': a.a['root'], 'gpu_request': site.site['gpu_request'],
        'submit_args': list(site.site.get('submit_args', [])), 'login_python': site.site['login_python'],
        'targets': {n: {k: v for k, v in site.targets[n].t.items() if k in keep} for n in a.a['targets']},
    }


# ------------------------------------------------------------------ deny list

#: values that are not inventory: our role names, the request syntax, the egress mode,
#: GPU models, env variants, sizes and project names. Everything else is denied.
PUBLIC_KEYS = frozenset({'name', 'gpu_request', 'egress', 'gpu_model', 'variant', 'mem_per_node', 'enabled'})


def _private(doc: object, path: tuple = ()) -> object:
    """``doc`` without the values of PUBLIC_KEYS, an account's ``targets`` (target
    names) and a system interpreter (``/usr/...``): what is denied."""
    if isinstance(doc, dict):
        return {k: _private(v, (*path, k)) for k, v in doc.items() if k not in PUBLIC_KEYS
                and not (k == 'targets' and path[:1] == ('accounts',))
                and not (k == 'login_python' and isinstance(v, str) and v.startswith('/usr/'))}
    if isinstance(doc, list):
        return [_private(v, path) for v in doc]
    return doc


def deny_tokens(*docs: Mapping[str, object]) -> set[str]:
    """The tokens of parsed site configs that the repository must not hold, split as
    ``inventory.deny_tokens`` splits them. Table keys (target names, account aliases) are
    role names of ours and not tokens. Pure."""
    return inventory.deny_tokens(*(_private(d) for d in docs))  # type: ignore[arg-type]


# ------------------------------------------------------------------ redaction

#: a value shorter than this, or a common word, is not replaced: as the deny list
#: (``inventory.MIN_TOKEN``, ``inventory._COMMON``), so it would not be denied either
REDACT_MIN = inventory.MIN_TOKEN
#: a node name is replaced from this length (``c07``): one is a whole word only
NODE_MIN = 2
#: the node names of one host list a redactor takes at most
HOSTLIST_MAX = 4096
_HOSTLIST = re.compile(r'[A-Za-z0-9_.-]+(?:\[[0-9,-]{1,64}\][A-Za-z0-9_.-]*)?(?:,[A-Za-z0-9_.-]+'
                       r'(?:\[[0-9,-]{1,64}\][A-Za-z0-9_.-]*)?)*\Z')


def hostlist(text: str) -> list[str]:
    """The node names of a Slurm host list (``zq[01-03,7],cpu1``: a range keeps its
    start's width), at most ``HOSTLIST_MAX``; ``[]`` for anything else. Pure."""
    if not _HOSTLIST.fullmatch(text):
        return []
    out: list[str] = []
    for m in re.finditer(r'([A-Za-z0-9_.-]+)(?:\[([0-9,-]+)\]([A-Za-z0-9_.-]*))?', text):
        pre, ranges, post = m.groups()
        if ranges is None:
            out.append(pre)
            continue
        for r in ranges.split(','):
            a, dash, b = r.partition('-')
            if not a.isdigit() or dash and not b.isdigit():
                return []
            for i in range(int(a), min(int(b or a), int(a) + HOSTLIST_MAX) + 1):
                out.append(f'{pre}{i:0{len(a)}d}{post}')
        if len(out) >= HOSTLIST_MAX:
            return out[:HOSTLIST_MAX]
    return out


#: a value this long or longer is replaced wherever it occurs, also inside a word
#: (``%2F<value>``, a repr's ``\\n<value>``); a shorter one as a whole word only
SUBSTRING_MIN = 8
#: an ANSI escape sequence (a colour, a cursor move): removed before redacting, so that
#: none hides a value's start
ANSI = re.compile(r'\x1b(?:\[[0-?]{0,32}[ -/]{0,8}[@-~]|[@-Z\\-_])')
#: a compressed host list in text (``UnavailableNodes:n[19-20]``): redacted as a whole
#: when it names a known node
_HOSTLIST_TOKEN = re.compile(r'[A-Za-z0-9_.-]{0,64}\[[0-9,-]{1,64}\][A-Za-z0-9_.-]{0,64}'
                             r'(?:,[A-Za-z0-9_.-]{1,64}(?:\[[0-9,-]{1,64}\][A-Za-z0-9_.-]{0,64})?){0,64}')
#: before a short value: no letter or digit, or a URL-encoded ``/`` or a repr's escape
_BEFORE = r'(?:(?<![A-Za-z0-9])|(?<=%2[Ff])|(?<=\\[ntr]))'
#: a host's or node's domain, which goes with it
_DOMAIN = r'(?:\.[A-Za-z0-9-]{1,63}){0,8}'


class Redactor:
    """Replaces a site's inventory in text with placeholders, for what a part shows on
    GitHub (its job log, reason and summary): each account's root (``<root>``) and its
    parent (``<home>``), also URL-encoded, Slurm account (``<account>``) and ssh alias
    (``<host>``), the login host, its ProxyJump hosts and the users its ssh config names
    (``add_ssh``: ``<host>``, ``<user>``), each target's partition (``<partition>``), the
    nodes (``<node>``: the config's, the cluster's from ``sinfo`` and those a job ran on,
    ``add_nodes``; host lists expanded, and a compressed list that names one replaced
    whole), the login Python, proxy and submit arguments. A host or node takes its
    domain with it. ANSI escape sequences are removed first. A value of
    ``SUBSTRING_MIN`` characters or more is replaced wherever it occurs; a shorter one
    as a whole word only (no letter or digit next to it, but after ``%2F`` or a repr's
    ``\\n``). The longest first, in any case. A value that is one of our role names
    (site, target, account, variant, GPU model) says nothing and is kept, as is one the
    deny list would not hold (``REDACT_MIN``), but a node name (``NODE_MIN``)."""

    def __init__(self, site: Site | None = None) -> None:
        self.public: set[str] = set()
        self.pairs: dict[str, str] = {}
        self._rx: re.Pattern | None = None
        if site is None:
            return
        self.public = {site.name.lower(), *(t.lower() for t in site.targets), *(a.lower() for a in site.accounts)}
        for t in site.targets.values():
            self.public.update(str(t.t[k]).lower() for k in ('variant', 'gpu_model') if k in t.t)
        for a in site.accounts.values():
            root = str(a.a['root']).rstrip('/')
            self.add_path(root, '<root>')
            if root.count('/') >= 2:
                self.add_path(os.path.dirname(root), '<home>')
            self.add(str(a.a['slurm_account']), '<account>')
            self.add(str(a.a['ssh']), '<host>')
        for t in site.targets.values():
            self.add(str(t.t['partition']), '<partition>')
            for n in [*t.t.get('nodes', []), *t.t.get('exclude', [])]:
                self.add_nodes(str(n))
        py = str(site.site['login_python'])
        if not py.startswith('/usr/'):
            self.add_path(py, '<python>')
        if site.site.get('proxy'):
            self.add(str(site.site['proxy']), '<proxy>')
        for x in site.site.get('submit_args', []):
            self.add(str(x), '<submit-arg>')
            if '=' in str(x):
                self.add(str(x).split('=', 1)[1], '<submit-arg>')

    def add(self, value: str, placeholder: str, least: int = REDACT_MIN) -> None:
        v = value.strip()
        if len(v) < least or v.lower() in inventory._COMMON or v.lower() in self.public:
            return
        if v.lower() not in self.pairs:
            self.pairs[v.lower()] = placeholder
            self._rx = None

    def add_path(self, path: str, placeholder: str) -> None:
        """A path, and its URL-encoded form (``%2Fshared%2F...``)."""
        self.add(path, placeholder)
        self.add(path.replace('/', '%2F'), placeholder)

    def add_nodes(self, text: str) -> None:
        """A node name or host list, as sinfo, sacct or the site config writes it."""
        text = text.strip()
        names = hostlist(text)
        for n in names:  # the list itself, in text, goes whole (_HOSTLIST_TOKEN)
            self.add(n, '<node>', NODE_MIN)

    def add_ssh(self, facts: Mapping[str, str]) -> None:
        """``ssh -G``'s ``hostname``, ``user`` and ``proxyjump`` hosts (``[user@]host[:port]``,
        comma-separated) of an account's alias."""
        if facts.get('hostname'):
            self.add(facts['hostname'], '<host>')
        if facts.get('user'):
            self.add(facts['user'], '<user>')
        jump = facts.get('proxyjump', '')
        for hop in jump.split(','):  # ssh's 'none' is a common word: kept
            hop = hop.strip().removeprefix('ssh://')
            user, _, host = hop.rpartition('@')
            if host.startswith('['):  # [v6]:port
                host = host[1:].split(']', 1)[0]
            elif host.count(':') == 1:
                host = host.split(':', 1)[0]
            self.add(host, '<host>')
            if user:
                self.add(user, '<user>')

    def _pattern(self) -> re.Pattern:
        if self._rx is None:
            alts = []
            for k in sorted(self.pairs, key=len, reverse=True):
                dom = _DOMAIN if self.pairs[k] in ('<node>', '<host>') else ''
                if len(k) >= SUBSTRING_MIN:
                    alts.append(re.escape(k) + dom)
                else:
                    alts.append(_BEFORE + re.escape(k) + dom + r'(?![A-Za-z0-9])')
            self._rx = re.compile('|'.join(alts) or r'(?!)', re.IGNORECASE)
        return self._rx

    def _value(self, m: re.Match) -> str:
        k = m.group().lower()
        while k not in self.pairs and '.' in k:  # a host's or node's domain
            k = k.rsplit('.', 1)[0]
        return self.pairs.get(k, '<redacted>')

    def _list(self, m: re.Match) -> str:
        names = hostlist(m.group())
        return '<node>' if any(self.pairs.get(n.lower()) == '<node>' for n in names) else m.group()

    def __call__(self, text: str) -> str:
        text = ANSI.sub('', text)
        if not self.pairs:
            return text
        return self._pattern().sub(self._value, _HOSTLIST_TOKEN.sub(self._list, text))

    def safe_cut(self, text: str, cut: int) -> int:
        """``cut``, or the start of a value, host list or escape sequence it would split
        in ``text`` (raw): where ``text`` may be cut before redacting its head. Pure."""
        rxs = (ANSI, _HOSTLIST_TOKEN, self._pattern())
        moved = True
        while moved:
            moved = False
            for rx in rxs:
                for m in rx.finditer(text, max(0, cut - 4096), min(len(text), cut + 4096)):
                    if m.start() < cut < m.end():
                        cut, moved = m.start(), True
        return cut


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.split('CLI (stdlib only)::', 1)[1], file=sys.stderr)
        return 2
    cmd, args = argv[0], argv[1:]
    try:
        if cmd == 'check' and len(args) == 1:
            s = load_file(args[0])
            print(f'site {s.name}: {len(s.targets)} targets, {len(s.accounts)} accounts')
            return 0
        if cmd == 'deny' and args:
            docs = []
            for p in args:
                with open(p, 'rb') as f:
                    docs.append(tomllib.load(f))
            print('\n'.join(sorted(inventory.hash_token(t) for t in deny_tokens(*docs))))
            return 0
        if cmd == 'entry' and len(args) == 2:
            s = load_file(args[0])
            if args[1] not in s.accounts:
                print(f'no account {args[1]!r} in site {s.name}', file=sys.stderr)
                return 2
            print(json.dumps(account_entry(s, args[1]), indent=1, sort_keys=True))
            return 0
    except SiteError as e:
        print(e, file=sys.stderr)
        return 1
    print(f'unknown command {cmd!r}', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
