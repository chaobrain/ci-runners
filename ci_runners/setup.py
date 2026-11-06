"""Host setup as the runner account (design §5.2, §5.3): ``plan`` prints every change,
``apply`` makes exactly an approved plan. Neither needs sudo.

``plan`` reads the inventory (``etc/inventory.toml``), the project registry
(``etc/projects/*.toml``: ``name`` and ``memory_max``) and the installed release
(``releases/hook``: hook scripts and unit templates), and lists:

- directories (§3.2), with ``etc/`` mode 700;
- the hook scripts, the rendered units and slices, each with its sha256 and a diff
  against the file in place;
- the CI proxy's node configs, made from the source configs (``proxy.config``) with
  their local ports replaced by the CI ports; their content is withheld (secrets). A
  host with ``tunnel`` instead (h100, decision 11.8) gets ``ci-tunnel`` and ``ci-bridge``,
  and its ``.env`` points at the bridge; the tunnel's key, ``ssh_config`` and
  ``known_hosts`` in ``etc/tunnel/`` are the lead's, checked but never written;
- the runner app, checked by sha256, extracted once per runner directory;
- each runner's ``.env``;
- the ``systemctl --user`` commands.

Units are rendered with the inventory's ``data`` (the root of §3.2, ``~/data`` on the 3090
hosts, our subtree on h100) and ``python``. A ``ci-*`` unit file that exists but that no
earlier ``apply`` wrote (``state/setup.json``) stops the plan: another user of a shared
account may own it. ``plan --adopt`` instead lists each such file as ``adopt`` (a regular
file of this account only) and plans it as usual, for a host whose units an older release
wrote without a record; the lead approves the adoption with the plan, and ``apply`` must
be given ``--adopt`` too.

Registration is not in the plan: the lead runs ``setup/register.sh`` with a fresh
token. ``apply --plan FILE`` refuses unless a fresh plan is identical (by sha256) and
records what it made in ``state/setup.json``; a second run is a no-op.
"""
from __future__ import annotations

import difflib
import glob
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import time
import tomllib
import urllib.request

from ci_runners import inventory

RUNNER_VERSION = '2.338.2'
RUNNER_SHA256 = 'baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40'
PROXY_WAIT = 60  # s: the CI proxy's start, before the runner app is fetched through it
RUNNER_URL = (f'https://github.com/actions/runner/releases/download/v{RUNNER_VERSION}/'
              f'actions-runner-linux-x64-{RUNNER_VERSION}.tar.gz')
UNITS = ('ci-runner@.service', 'ci-proxy.service', 'ci-proxy-check.service', 'ci-proxy-check.timer',
         'ci-tunnel.service', 'ci-bridge.service',
         'ci-reaper.service', 'ci-reaper.timer', 'ci-orgsync.service', 'ci-orgsync.timer', 'ci.slice')
RUNNER_FILES = ('config.sh', 'run.sh', os.path.join('bin', 'Runner.Listener'))
# what goes into a unit file: one word each, so nothing can add a line
PROJECT_NAME = re.compile(r'[a-z0-9][a-z0-9-]{0,39}')
CLIENT = re.compile(r'/[A-Za-z0-9._/-]+')
MEMORY = re.compile(r'[1-9][0-9]*[KMGT]?|[1-9][0-9]?%|infinity')
TIMERS = ('ci-proxy-check.timer', 'ci-reaper.timer', 'ci-orgsync.timer')
SYSTEM_PYTHON = '/usr/bin/python3'
#: the runner unit's order after the CI proxy, where the host has one
PROXY_ORDER = 'After=ci-proxy.service\nWants=ci-proxy.service'
#: or after the egress bridge (h100, decision 11.8)
BRIDGE_ORDER = 'After=ci-bridge.service\nWants=ci-bridge.service'
#: the lead's files in etc/tunnel/ (design §5.3): the key first, which no one else may read
TUNNEL_FILES = ('id_ed25519', 'ssh_config', 'known_hosts')
#: etc/tunnel/ssh_config: ``Host egress`` and only where it is (the unit fixes the rest)
SSH_CONFIG_LINE = re.compile(r'\s*(?:#.*)?|\s*(?:HostName|User|Port)[ \t=]+[A-Za-z0-9._-]+\s*|Host egress\s*')


def _runners(inv: inventory.Inventory) -> list[tuple[str, list[str], str, int | None]]:
    out = []
    for pool, v in inv.host['pools'].items():
        spec = v if isinstance(v, dict) else {'count': v}
        labels = inventory.pool_labels(inv, pool) or list(dict.fromkeys([pool, inv.this]))
        if spec.get('per_card'):
            for card in inv.host['gpus']:
                out.append((f'{inv.this}-gpu{card}', [*labels, f'{inv.this}-gpu{card}'], pool, card))
        else:
            for i in range(1, spec['count'] + 1):
                out.append((f'{inv.this}-{pool}-{i}', labels, pool, None))
    names = [n for n, _, _, _ in out]
    if len(set(names)) != len(names):
        raise ValueError('two runners of this host have the same name')
    return out


def timers(h: dict) -> list[str]:
    """The timers this host enables: the proxy check only with a CI proxy, the org
    snapshot only where the inventory sets ``orgsync`` (design §6.5)."""
    return [t for t in TIMERS if ('proxy' in h or 'proxy' not in t) and (h.get('orgsync') or 'orgsync' not in t)]


def runners(inv: inventory.Inventory) -> list[tuple[str, list[str], str]]:
    """``(name, labels, class)`` of this host's runners (design §5.1): ``<role>-<pool>-<i>``,
    labelled with the pool (the class) and the host role, or with the pool's own
    ``labels``; a ``per_card`` pool has one runner per card, ``<role>-gpu<N>``, labelled
    also ``<role>-gpu<N>``. A remote host's runners are not here: they live on the host
    that drives it (RUN-2)."""
    return [(n, labels, cls) for n, labels, cls, _ in _runners(inv)]


def python_of(inv: inventory.Inventory) -> str:
    """The host layer's interpreter: the inventory's ``python``, else the system's."""
    return str(inv.host.get('python') or SYSTEM_PYTHON)


HOOK_PYTHON = 'py=/usr/bin/python3  # @CI_PYTHON@\n'


def hook_text(src: str, python: str) -> str:
    """A hook as installed: its interpreter line names ``python`` (validated by the
    caller as one absolute path). Raises ValueError if the hook has no such line. Pure."""
    if src.count(HOOK_PYTHON) != 1:
        raise ValueError('a hook has no interpreter line')
    return src.replace(HOOK_PYTHON, f'py={python}\n')


def check_python(path: str, run=subprocess.run) -> None:
    """Refuse an interpreter that is not an executable Python 3.11 or later (``tomllib``)."""
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        raise ValueError(f'python {path} is not an executable file')
    try:
        r = run([path, '-I', '-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'],
                capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise ValueError(f'python {path} did not answer: {type(e).__name__}') from None
    if r.returncode != 0:
        raise ValueError(f'python {path} is not Python 3.11 or later')


def render(template: str, subs: dict[str, str]) -> str:
    for k, v in subs.items():
        if not v:  # an empty value takes its line with it
            template = template.replace(f'@{k}@\n', '')
        template = template.replace(f'@{k}@', v)
    if '@' in ''.join(line for line in template.splitlines() if not line.lstrip().startswith('#')):
        raise ValueError('a unit template has an unknown @KEY@')
    return template


def check_tunnel(etc: str) -> None:
    """Refuse unless ``etc/tunnel/`` holds the lead's three files, as regular files of
    this account, the key and the directory readable by no one else, and ``ssh_config``
    says only ``Host egress`` with its ``HostName`` and, if it likes, ``User`` and
    ``Port``: the unit fixes every other option, and an option that accumulates or adds a
    forward or a command could not be fixed there."""
    d = os.path.join(etc, 'tunnel')
    try:
        st = os.lstat(d)
    except OSError:
        st = None
    if st is None or not stat.S_ISDIR(st.st_mode) or st.st_mode & 0o077 or st.st_uid != os.getuid():
        raise ValueError(f'{d} must be a directory of this account, mode 700, holding {", ".join(TUNNEL_FILES)}')
    for name in TUNNEL_FILES:
        path = os.path.join(d, name)
        try:
            st = os.lstat(path)
        except OSError:
            st = None
        if st is None or not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            raise ValueError(f'{path} must be a regular file of this account (the lead places it)')
        if name == 'id_ed25519' and st.st_mode & 0o077:
            raise ValueError(f'{path} is readable by others: chmod 600 it')
    with open(os.path.join(d, 'ssh_config'), encoding='utf-8', errors='replace') as f:
        lines = f.read().splitlines()
    bad = [i for i, line in enumerate(lines, 1) if not SSH_CONFIG_LINE.fullmatch(line)]
    if bad:
        raise ValueError(f'{d}/ssh_config line {bad[0]}: only Host egress, HostName, User and Port may be set there')
    if [line.strip() for line in lines if line.startswith('Host ')] != ['Host egress'] or not any(
            line.strip().startswith('HostName') for line in lines):
        raise ValueError(f'{d}/ssh_config must have one "Host egress" with its HostName')


def node_config(src: dict, http_port: int, socks_port: int) -> dict:
    """A node config with its inbounds replaced by the CI instance's local ports. Pure."""
    out = dict(src)
    out['inbounds'] = [
        {'tag': 'socks', 'listen': '127.0.0.1', 'port': socks_port, 'protocol': 'socks',
         'settings': {'udp': True}},
        {'tag': 'http', 'listen': '127.0.0.1', 'port': http_port, 'protocol': 'http'},
    ]
    out.pop('log', None)
    return out


def env_file(inv: inventory.Inventory, cls: str, etc: str, card: int | None = None) -> str:
    """A runner's ``.env`` (design §5.3): the runner app reads it into its environment,
    which its hooks and its jobs' steps inherit. A per-card runner names its card in
    ``CI_RUNNER_CARD``."""
    h = inv.host
    hooks = os.path.join(h['data'], 'ci-runners', 'hooks')
    lines = []
    if 'proxy' in h:
        p = h['proxy']
        lines += [f'https_proxy=http://127.0.0.1:{p["http_port"]}', f'http_proxy=http://127.0.0.1:{p["http_port"]}',
                  f'no_proxy={p["no_proxy"]}']
    elif 'tunnel' in h:
        t = h['tunnel']
        lines += [f'https_proxy=http://127.0.0.1:{t["http_port"]}', f'http_proxy=http://127.0.0.1:{t["http_port"]}',
                  f'no_proxy={",".join(t["direct"])}']
    lines += [f'ACTIONS_RUNNER_HOOK_JOB_STARTED={hooks}/job-started.sh',
              f'ACTIONS_RUNNER_HOOK_JOB_COMPLETED={hooks}/job-completed.sh',
              f'CI_RUNNER_CLASS={cls}', f'CI_RUNNERS_ETC={etc}']
    own = inventory.pool_labels(inv, cls)
    if own:  # the launcher's retry-on when this host lacks the release
        lines.append(f'CI_RUNNER_POOL_LABELS={",".join(own)}')
    if card is not None:
        lines.append(f'CI_RUNNER_CARD={card}')
    if h.get('python'):
        lines.append(f'CI_RUNNERS_PYTHON={h["python"]}')
    lines.append('LANG=C.UTF-8')
    return '\n'.join(lines) + '\n'


class Plan:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.actions: list[dict] = []

    def add(self, line: str, **action) -> None:
        self.lines.append(line)
        if action:
            self.actions.append(action)

    def text(self) -> str:
        return '\n'.join(self.lines) + '\n'

    def digest(self) -> str:
        return hashlib.sha256(self.text().encode()).hexdigest()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file(plan: Plan, path: str, content: str, mode: int, secret: bool = False, note: str = '') -> None:
    data = content.encode()
    try:
        with open(path, 'rb') as f:
            old = f.read()
    except OSError:
        old = None
    try:
        old_mode = os.stat(path).st_mode & 0o777
    except OSError:
        old_mode = None
    if old == data and old_mode == mode:
        plan.add(f'  keep   {path}{note}')
        return
    verb = 'write' if old is None else 'update'
    plan.add(f'  {verb:6} {path} mode {mode:o} sha256 {_sha(data)[:16]}{note}', kind='file', path=path,
             content=content, mode=mode)
    if secret:
        plan.add('         (content withheld: it holds secrets)')
    elif old is not None:
        for d in difflib.unified_diff(old.decode(errors='replace').splitlines(), content.splitlines(),
                                      'in place', 'planned', lineterm='', n=1):
            plan.add(f'         {d}')
    else:
        for line in content.splitlines():
            plan.add(f'         | {line}')


def build(etc: str, adopt: bool = False) -> Plan:
    """The plan for this host, from ``etc`` and the installed release
    (``$CI_RUNNERS_RELEASE`` instead, to preview a plan before the account exists).
    Pure but for reading files."""
    with open(os.path.join(etc, 'inventory.toml'), encoding='utf-8') as f:
        inv = inventory.load(f.read())
    h = inv.host
    if h.get('remote'):
        raise ValueError(f'{inv.this} is driven remotely: nothing is set up on it here (RUN-2)')
    data, home = h['data'], h['home']
    base = os.path.join(data, 'ci-runners')
    release = os.path.realpath(os.environ.get('CI_RUNNERS_RELEASE') or os.path.join(base, 'releases', 'hook'))
    plan = Plan()
    plan.add(f'# ci-runners setup plan for {inv.this}, release {os.path.basename(release)}')
    plan.add('directories:')
    dirs = [(base, 0o755), (os.path.join(base, 'releases'), 0o755), (os.path.join(base, 'hooks'), 0o755),
            (etc, 0o700), (os.path.join(base, 'state'), 0o700), (os.path.join(base, 'logs'), 0o700),
            (os.path.join(data, 'actions-runner'), 0o700), (os.path.join(data, 'ci-work'), 0o700),
            (os.path.join(home, '.config', 'systemd', 'user'), 0o755)]
    if 'proxy' in h:
        dirs += [(os.path.join(etc, 'proxy'), 0o700), (os.path.join(etc, 'proxy', 'nodes'), 0o700)]
    unit_dir = os.path.join(home, '.config', 'systemd', 'user')
    for d, mode in dirs:
        if os.path.isdir(d) and (d == unit_dir or os.stat(d).st_mode & 0o777 == mode):
            plan.add(f'  keep   {d}/')  # the unit directory is the account's: its mode is never changed
        else:
            plan.add(f'  mkdir  {d}/ mode {mode:o}', kind='mkdir', path=d, mode=mode)
    if not isinstance(h['memory_max'], str) or not MEMORY.fullmatch(h['memory_max']):
        raise ValueError(f'memory_max must match {MEMORY.pattern}')
    client = h.get('proxy', {}).get('client', '/bin/false')
    python = python_of(inv)
    for key, v in (('proxy.client', client), ('data', data), ('python', python), ('etc', etc)):
        if not isinstance(v, str) or not CLIENT.fullmatch(v):
            raise ValueError(f'{key} must be an absolute path matching {CLIENT.pattern}')
    if etc != os.path.join(base, 'etc'):
        raise ValueError(f'the etc directory must be {os.path.join(base, "etc")}')
    if h.get('python'):
        check_python(python)
    if 'tunnel' in h:
        check_tunnel(etc)
    plan.add('hooks:')
    for name in ('job-started.sh', 'job-completed.sh'):
        with open(os.path.join(release, 'hooks', name)) as f:
            _file(plan, os.path.join(base, 'hooks', name), hook_text(f.read(), python), 0o755)
    plan.add('units:')
    subs = {'CI_MEMORY_MAX': h['memory_max'], 'PROXY_CLIENT': client, 'CI_DATA': data, 'CI_ETC': etc,
            'CI_PYTHON': python, 'PROXY_ORDER': PROXY_ORDER if 'proxy' in h else BRIDGE_ORDER if 'tunnel' in h else '',
            'TUNNEL_SOCKS': str(h['tunnel']['socks_port']) if 'tunnel' in h else '',
            # the bridge reads its table once: a changed table changes its unit, which restarts it
            'TUNNEL_DIGEST': _sha(json.dumps(h['tunnel'], sort_keys=True).encode())[:16] if 'tunnel' in h else ''}
    ours = _ours(os.path.join(base, 'state', 'setup.json'))

    def unit(name: str, content: str) -> None:
        path = os.path.join(unit_dir, name)
        if os.path.lexists(path) and path not in ours:
            if not adopt:
                raise ValueError(f'{path} exists and no apply of ci-runners wrote it: another user\'s unit? '
                                 'Rename or remove it first, or plan with --adopt if it is ours')
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                raise ValueError(f'{path} is not a regular file of this account: it cannot be adopted')
            # the uid check tells nothing apart on a shared account: there the lead's reading
            # of each listed file is the check (design §5.3)
            plan.add(f'  adopt  {path} (no apply recorded it; the lead names it ours)')
            n = len(plan.actions)
            _file(plan, path, content, 0o644)
            if len(plan.actions) == n:  # kept as it is: record it; a write or update records itself, once made
                plan.actions.append({'kind': 'adopt', 'path': path})
            return
        _file(plan, path, content, 0o644)

    for name in UNITS:
        if (name.startswith('ci-proxy') and 'proxy' not in h or name.startswith('ci-orgsync') and not h.get('orgsync')
                or name in ('ci-tunnel.service', 'ci-bridge.service') and 'tunnel' not in h):
            continue
        with open(os.path.join(release, 'setup', 'units', name)) as f:
            unit(name, render(f.read(), subs))
    with open(os.path.join(release, 'setup', 'units', 'ci-project.slice')) as f:
        tmpl = f.read()
    projects = []
    for path in sorted(glob.glob(os.path.join(etc, 'projects', '*.toml'))):
        with open(path, 'rb') as f:
            p = tomllib.load(f)
        name, mem = p.get('name'), p.get('hosts', {}).get(inv.this, {}).get('memory_max')
        if not isinstance(name, str) or not isinstance(mem, str):
            raise ValueError(f'{path}: name and hosts.{inv.this}.memory_max are required')
        if not PROJECT_NAME.fullmatch(name) or not MEMORY.fullmatch(mem):
            raise ValueError(f'{path}: name must match {PROJECT_NAME.pattern} and memory_max {MEMORY.pattern}')
        projects.append(name)
        unit(f'ci-{name}.slice', render(tmpl, {'PROJECT': name, 'PROJECT_MEMORY_MAX': mem}))
    if 'proxy' in h:
        p = h['proxy']
        plan.add('CI proxy:')
        with open(os.path.join(p['config'], 'routing.json'), encoding='utf-8') as f:
            _file(plan, os.path.join(etc, 'proxy', 'routing.json'), f.read(), 0o600, secret=True)
        for node in p['nodes']:
            with open(os.path.join(p['config'], f'{node}.json'), encoding='utf-8') as f:
                cfg = node_config(json.load(f), p['http_port'], p['socks_port'])
            _file(plan, os.path.join(etc, 'proxy', 'nodes', f'{node}.json'), json.dumps(cfg, indent=1) + '\n',
                  0o600, secret=True,
                  note=f' (inbounds 127.0.0.1:{p["socks_port"]} socks, 127.0.0.1:{p["http_port"]} http)')
        active = os.path.join(etc, 'proxy', 'active.json')
        if os.path.lexists(active):
            plan.add(f'  keep   {active} -> {os.readlink(active)}')
        else:
            plan.add(f'  link   {active} -> nodes/{p["nodes"][0]}.json', kind='symlink', path=active,
                     target=f'nodes/{p["nodes"][0]}.json')
    if 'tunnel' in h:
        t = h['tunnel']
        plan.add('egress tunnel (the lead\'s files, checked):')
        for name in TUNNEL_FILES:
            plan.add(f'  keep   {os.path.join(etc, "tunnel", name)}')
        plan.add(f'  socks5 127.0.0.1:{t["socks_port"]}, bridge 127.0.0.1:{t["http_port"]}, '
                 f'{len(t["allow"])} names on port 443: {", ".join(t["allow"])}')
    egress = (['ci-proxy.service'] if 'proxy' in h else ['ci-tunnel.service', 'ci-bridge.service'] if 'tunnel' in h
              else [])
    # egress units already running with an older file: enable --now leaves them as they are
    stale = 'tunnel' in h and any(a['kind'] == 'file' and os.path.dirname(a['path']) == unit_dir
                                  and os.path.basename(a['path']) in egress and os.path.exists(a['path'])
                                  for a in plan.actions)
    if plan.actions:
        plan.add({'proxy': 'systemd --user (units, CI proxy):', 'tunnel': 'systemd --user (units, egress):'}.get(
            next((k for k in ('proxy', 'tunnel') if k in h), ''), 'systemd --user (units):'))
        plan.add('  run    systemctl --user daemon-reload', kind='run', argv=['systemctl', '--user', 'daemon-reload'])
        if egress:
            argv = ['systemctl', '--user', 'enable', '--now', *egress]
            plan.add(f'  run    {" ".join(argv)}', kind='run', argv=argv)
        if stale:
            argv = ['systemctl', '--user', 'restart', *egress]
            plan.add(f'  run    {" ".join(argv)}', kind='run', argv=argv)
    port = h['proxy']['http_port'] if 'proxy' in h else h['tunnel']['http_port'] if 'tunnel' in h else 0
    via = ('fetched through the CI proxy' if 'proxy' in h else 'fetched through the egress bridge' if 'tunnel' in h
           else 'fetched directly')
    plan.add(f'runners (actions/runner {RUNNER_VERSION}, sha256 {RUNNER_SHA256[:16]}, {via}):')
    cache = os.path.join(base, 'state', 'cache', os.path.basename(RUNNER_URL))
    rs = _runners(inv)
    waited = 'tunnel' not in h
    for name, labels, cls, card in rs:
        rdir = os.path.join(data, 'actions-runner', name)
        if runner_complete(rdir):
            plan.add(f'  keep   {rdir}/ (runner app present)')
        else:
            if not waited:  # the bridge listens at once; the tunnel's port, only once ssh is in
                t = h['tunnel']
                plan.add(f'  wait   127.0.0.1:{t["socks_port"]} (tunnel), 127.0.0.1:{t["http_port"]} (bridge)',
                         kind='wait', ports=[t['socks_port'], t['http_port']])
                waited = True
            plan.add(f'  unpack {RUNNER_URL} -> {rdir}/', kind='runner', path=rdir, cache=cache, port=port)
        _file(plan, os.path.join(rdir, '.env'), env_file(inv, cls, etc, card), 0o600)
        plan.add(f'         labels {",".join(labels)}; work {os.path.join(data, "ci-work", name)} (at registration)')
    if plan.actions:  # nothing changed: a second apply is a no-op
        plan.add('systemd --user (timers, runners):')
        cmds = [['systemctl', '--user', 'daemon-reload'],
                ['systemctl', '--user', 'enable', '--now', *timers(h)],
                ['systemctl', '--user', 'enable', *[f'ci-runner@{n}.service' for n, _, _, _ in rs]]]
        for c in cmds:
            plan.add(f'  run    {" ".join(c)}', kind='run', argv=c)
    plan.add('  (a runner starts at its registration: setup/register.sh, run by the lead)')
    return plan


def _ours(state_path: str) -> frozenset[str]:
    """The files an earlier apply wrote, from ``state/setup.json`` (none if it is absent)."""
    try:
        with open(state_path, encoding='utf-8') as f:
            state = json.load(f)
        return frozenset(m['path'] for a in state['applied'] for m in a['made'] if m.get('kind') == 'file')
    except FileNotFoundError:
        return frozenset()
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ValueError(f'{state_path} is unreadable ({type(e).__name__}): fix it before planning') from None


def wait_port(port: int, timeout: float = PROXY_WAIT, poll: float = 0.5) -> None:
    """Wait until something listens on ``127.0.0.1:port``: ``systemctl enable --now``
    returns once the CI proxy has started, before it listens."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=poll):
                return
        except OSError:
            if time.monotonic() >= deadline:
                raise RuntimeError(f'nothing listens on the egress port {port} after {timeout:g} s') from None
            time.sleep(poll)


def _unpack(path: str, cache: str, port: int) -> None:
    if not os.path.exists(cache):
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        if port:
            wait_port(port)
        proxies = {'https': f'http://127.0.0.1:{port}', 'http': f'http://127.0.0.1:{port}'} if port else {}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
        with opener.open(RUNNER_URL, timeout=600) as r, open(cache + '.part', 'wb') as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        os.replace(cache + '.part', cache)
    h = hashlib.sha256()
    with open(cache, 'rb') as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    if h.hexdigest() != RUNNER_SHA256:
        os.unlink(cache)
        raise RuntimeError('the runner app does not match its sha256')
    # extract beside it, then move in: an interrupted unpack never looks complete
    tmp = path + '.ci-new'
    if os.path.lexists(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp, mode=0o700)
    with tarfile.open(cache) as t:
        t.extractall(tmp, filter='tar')
    if not runner_complete(tmp):
        raise RuntimeError('the runner app is incomplete after extraction')
    if not os.path.lexists(path):
        os.rename(tmp, path)
        return
    for n in os.listdir(tmp):
        dst = os.path.join(path, n)
        if os.path.isdir(dst) and not os.path.islink(dst):
            shutil.rmtree(dst)
        os.replace(os.path.join(tmp, n), dst)
    os.rmdir(tmp)


def runner_complete(path: str) -> bool:
    """The runner app is all there: its configure script and its listener."""
    return all(os.path.isfile(os.path.join(path, f)) for f in RUNNER_FILES)


def apply(etc: str, plan_file: str, run=subprocess.run, adopt: bool = False) -> int:
    with open(plan_file, encoding='utf-8') as f:
        approved = f.read()
    plan = build(etc, adopt)
    if not plan.actions:
        print(f'apply: nothing to change (plan {plan.digest()[:16]})')
        return 0
    if plan.text() != approved:
        print(f'apply: the plan changed (approved {hashlib.sha256(approved.encode()).hexdigest()[:16]}, '
              f'now {plan.digest()[:16]}): run plan.sh again and have it approved', file=sys.stderr)
        return 1
    state_path = os.path.join(os.path.dirname(etc), 'state', 'setup.json')
    made = []
    try:
        _apply_actions(plan, made, run)
    finally:  # a failed apply still records what it made, so the next plan knows its own files
        _record(state_path, plan.digest(), made)
    print(f'apply: {len(made)} change(s), plan {plan.digest()[:16]}')
    return 0


def _apply_actions(plan: Plan, made: list, run) -> None:
    for a in plan.actions:
        k = a['kind']
        if k == 'mkdir':
            os.makedirs(a['path'], exist_ok=True)
            os.chmod(a['path'], a['mode'])
        elif k == 'file':
            tmp = a['path'] + '.ci-new'
            if os.path.lexists(tmp):
                os.unlink(tmp)  # a leftover, or a planted link: removed, never followed
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(a['content'])
                os.fchmod(f.fileno(), a['mode'])
            os.replace(tmp, a['path'])
        elif k == 'symlink':
            os.symlink(a['target'], a['path'])
        elif k == 'adopt':  # recorded as a file of this apply's, so uninstall removes it
            made.append({'kind': 'file', 'path': a['path']})
            print(f'apply: adopt {a["path"]}')
            continue
        elif k == 'wait':
            for port in a['ports']:
                wait_port(port)
        elif k == 'runner':
            _unpack(a['path'], a['cache'], a['port'])
        elif k == 'run':
            run(a['argv'], check=True, timeout=300)
        made.append({x: a[x] for x in ('kind', 'path', 'argv') if x in a})
        print(f'apply: {k} {a.get("path") or " ".join(a.get("argv", []))}')


def _record(state_path: str, digest: str, made: list) -> None:
    if not made:
        return
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    try:
        with open(state_path) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {'applied': []}
    state['applied'].append({'plan': digest, 'made': made})
    with open(state_path + '.new', 'w') as f:
        json.dump(state, f, indent=1)
    os.replace(state_path + '.new', state_path)


def main(argv: list[str]) -> int:
    etc = os.environ.get('CI_RUNNERS_ETC') or os.path.expanduser('~/data/ci-runners/etc')
    if argv == ['runners']:
        with open(os.path.join(etc, 'inventory.toml'), encoding='utf-8') as f:
            inv = inventory.load(f.read())
        for name, labels, cls in runners(inv):
            print(name, ','.join(labels), cls)
        return 0
    if argv in (['plan'], ['plan', '--adopt']):
        p = build(etc, adopt=len(argv) == 2)
        sys.stdout.write(p.text())
        print(f'# plan sha256 {p.digest()}', file=sys.stderr)
        return 0
    if len(argv) in (3, 4) and argv[0] == 'apply' and argv[1] == '--plan' and argv[3:] in ([], ['--adopt']):
        return apply(etc, argv[2], adopt=len(argv) == 4)
    print('usage: ci-run plan [--adopt] | ci-run apply --plan FILE [--adopt]', file=sys.stderr)
    return 2
