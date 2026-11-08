"""``ci-run slurm``: set up and keep a Slurm site's accounts from a driver host (design
decision 14.9, operations §10), as the host's CI account::

    ci-run slurm check     <site> <account> [--via ALIAS]   read-only, free
    ci-run slurm plan      <site> <account>                 every change; changes nothing
    ci-run slurm apply     <site> <account> --plan FILE --via ALIAS
    ci-run slurm status    <site>
    ci-run slurm env       <site> <account> <project> --via ALIAS [--variant V] [--refresh]   (slurmenv)
    ci-run slurm uninstall <site> <account> --via ALIAS [--cancel]

The site config is ``etc/slurm/<site>.toml`` (``ci_runners.slurmsite``); the account's CI
key is ``etc/slurm/keys/<account>`` with its public half ``<account>.pub``
(``ssh-ed25519``). The CI key's ``authorized_keys`` line forces the gate, whose verbs
cannot write a release or ``etc/``: the steps that do (``apply``, ``uninstall``, and
``check`` before an install) go through ``--via``, an ssh alias of the account owner's
own login, and run ``ci_runners.slurmlogin`` there, sent on stdin (``BOOT``); nothing of
ours is installed for them or stays running (14.11).

- ``check``: the gate's ``check`` (the CI key, after an install; through ``--via`` before
  one, in a temporary tree under the root) judged by ``findings``: the login Python
  (``slurmsite.MIN_PYTHON``), the Slurm commands and ``rsync``, Slurm >= 20.11, the root
  writable with ``MIN_FREE_GIB`` free, and per target its partition up and
  ``sbatch --test-only`` taking the rendered request (the site's GPU syntax, the
  account's partition access). Exit 1 on a finding.
- ``plan`` (``plan_text``): the directories, the release this host runs copied to the
  cluster (its files and their sha256), the account's cluster-side entry
  (``etc/account.json``) and ``etc/python``, the ``authorized_keys`` line the account's
  owner installs (``key_line``), the env builds and the canary's jobs with their cost.
  It reads no remote state, so that a plan is the same text until its inputs change.
- ``apply`` refuses unless a fresh plan is the same text, then makes it and records it
  in ``state/slurm/<site>/install-<account>.json``; a second run changes nothing. It
  never touches ``authorized_keys``: that is the owner's step.
- ``status``: each account's spend this month and today by the site's prices, its live
  ``ci-`` jobs, its install, and the targets a P2P failure holds at one card.
- ``uninstall`` refuses while a ``ci-`` job of the account is there (``--cancel`` cancels
  them), removes the ``authorized_keys`` lines that force its gate and the root's
  ``ci-runners`` tree, and marks the account ``retired`` in the site config; its ledger
  rows stay in ``sacct`` for the month.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time
from collections.abc import Callable

from ci_runners import inventory, slurmlogin, slurmpart, slurmsite

RELEASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TAG = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')
MIN_FREE_GIB = 50
MIN_SLURM = (20, 11)
CANARY_MINUTES = 3
COMMANDS = ('sbatch', 'squeue', 'sacct', 'scancel', 'sinfo', 'rsync')
GATE = 'releases/hook/bin/ci-slurm-gate'
#: the boot of a login step, read as stdin's first line by a one-line command (any login
#: shell runs it): the modules' sources and the call follow on stdin; its JSON is printed
#: after ``SENTINEL``, so that what a login prints itself is passed over
SENTINEL = '@@ci-runners-result@@'
BOOT = ('import json, sys, types\n'
        'd = json.loads(sys.stdin.read())\n'
        'for n, src in d["modules"].items():\n'
        '    m = types.ModuleType("ci_runners." + n)\n'
        '    sys.modules[m.__name__] = m\n'
        '    exec(compile(src, n + ".py", "exec"), m.__dict__)\n'
        'login = sys.modules["ci_runners.slurmlogin"]\n'
        'try:\n'
        '    r = getattr(login, d["call"])(**d["args"])\n'
        'except (login.LoginError, OSError) as e:\n'
        '    sys.stderr.write(f"refused: {e}\\n")\n'
        '    sys.exit(2)\n'
        f'sys.stdout.write("\\n" + {SENTINEL!r} + json.dumps(r))\n')


class SetupError(Exception):
    """A step refused, or the login host did."""


# ------------------------------------------------------------------ pure parts


def base_of(site: slurmsite.Site, alias: str) -> str:
    return os.path.join(str(site.accounts[alias].a['root']), 'ci-runners')


def release_files(release: str) -> list[str]:
    """The files of a release the cluster runs: the package and the gate's wrapper."""
    pkg = os.path.join(release, 'ci_runners')
    return [*(f'ci_runners/{n}' for n in sorted(os.listdir(pkg)) if n.endswith('.py')), 'bin/ci-slurm-gate']


def release_digest(release: str, files: list[str]) -> str:
    h = hashlib.sha256()
    for rel in files:
        with open(os.path.join(release, rel), 'rb') as f:
            data = f.read()
        h.update(f'{rel}\0{len(data)}\0'.encode() + data)
    return h.hexdigest()


def public_key(etc: str, alias: str) -> str:
    """The account's CI key, public half; its private half must be the owner's only."""
    key = os.path.join(etc, 'slurm', 'keys', alias)
    try:
        st = os.lstat(key)
    except FileNotFoundError:
        raise SetupError(f'no CI key: {key} (ssh-keygen -t ed25519 -N "" -f {key})') from None
    if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) & 0o077:
        raise SetupError(f'{key}: not a file of mode 600')
    try:
        with open(key + '.pub', encoding='utf-8') as f:
            pub = ' '.join(f.read().split()[:2])
    except OSError:
        raise SetupError(f'no {key}.pub') from None
    if not inventory.PUSH_KEY.fullmatch(pub):
        raise SetupError(f'{key}.pub: not an ssh-ed25519 public key')
    return pub


def key_line(base: str, key: str, driver: str) -> str:
    """The CI key's ``authorized_keys`` line (14.5): ``restrict``, forced to the gate,
    marked ``ci-runners-slurm-<driver>``."""
    return f'restrict,command="{base}/{GATE}" {key} ci-runners-slurm-{driver}'


def canary_jobs(site: slurmsite.Site, alias: str) -> list[tuple[str, int, int, float]]:
    """The canary's job per target of the account: ``(target, cards, cpus, cost)``; a GPU
    target's at its ``max_cards`` (the P2P self-test where 2 or more), for
    ``CANARY_MINUTES`` at most."""
    out = []
    for t in site.accounts[alias].a['targets']:
        tg = site.targets[t]
        cards = tg.max_cards if tg.gpu else 0
        cpus = tg.cpus(cards)
        out.append((t, cards, cpus, site.hourly(t, cards, cpus) * CANARY_MINUTES / 60))
    return out


def plan_text(site: slurmsite.Site, alias: str, *, release: str, key: str, driver: str) -> str:
    """Every change of an install of account ``alias``, as the lead approves it. Pure
    but for reading the release's files."""
    a = site.accounts[alias]
    base = base_of(site, alias)
    tag = os.path.basename(os.path.realpath(release))
    if not TAG.fullmatch(tag) or tag == 'hook':
        raise SetupError(f'release {tag!r}: not a release name')
    files = release_files(release)
    entry = json.dumps(slurmsite.account_entry(site, alias), indent=1, sort_keys=True)
    variants = sorted({str(site.targets[t].t['variant']) for t in a.a['targets']})
    jobs = canary_jobs(site, alias)
    lines = [
        f'# ci-run slurm plan {site.name} {alias}: release {tag}, driver {driver}',
        f'login: ssh {a.a["ssh"]}; Slurm account {a.a["slurm_account"]}; root {a.a["root"]}',
        '',
        '1. directories, mode 700:',
        *(f'   {d}' for d in (base, *(f'{base}/{x}' for x in slurmlogin.DIRS))),
        f'2. release {tag}, copied from this host to {base}/releases/{tag} (group and others: no access), '
        f'{len(files)} files, sha256 {release_digest(release, files)}; {base}/releases/hook -> {tag}',
        f'3. {base}/etc/account.json, mode 600 (sha256 {hashlib.sha256(entry.encode()).hexdigest()}):',
        *(f'   {x}' for x in entry.splitlines()),
        f'   {base}/etc/python, mode 600: {site.site["login_python"]}',
        '4. the account\'s owner adds this line to ~/.ssh/authorized_keys on the login host, replacing a line '
        f'marked ci-runners-slurm-{driver} (apply does not):',
        f'   {key_line(base, key, driver)}',
        f'5. env builds (ci-run slurm env {site.name} {alias} <project>; egress {site.site["egress"]}), '
        f'variants: {", ".join(variants)}',
        f'6. canary (ci-run slurm canary {site.name} {alias}): one job per target, at most {CANARY_MINUTES} min:',
        *(f'   {t}: {c} cards, {n} CPUs, up to {cost:.2f}' for t, c, n, cost in jobs),
        f'   in all up to {sum(j[3] for j in jobs):.2f}',
    ]
    return '\n'.join(lines) + '\n'


def findings(site: slurmsite.Site, alias: str, facts: dict) -> list[str]:
    """What keeps the account from running parts, from the gate's ``check``. Pure."""
    out = []
    if isinstance(facts.get('root'), str):
        return [facts['root']]
    py = facts.get('python')
    if not isinstance(py, list) or tuple(py[:2]) < slurmsite.MIN_PYTHON:
        out.append(f'login Python {py}: {".".join(map(str, slurmsite.MIN_PYTHON))} or later needed')
    cmds = facts.get('commands') if isinstance(facts.get('commands'), dict) else {}
    missing = [c for c in COMMANDS if not cmds.get(c)]
    if missing:
        out.append(f'not on the login host: {" ".join(missing)}')
    v = facts.get('slurm')
    if not isinstance(v, list) or tuple(v[:2]) < MIN_SLURM:
        out.append(f'Slurm {v}: {".".join(map(str, MIN_SLURM))} or later needed')
    if not facts.get('writable'):
        out.append('the root is not writable')
    elif float(facts.get('free_gib') or 0) < MIN_FREE_GIB:
        out.append(f'the root has {float(facts.get("free_gib") or 0):.0f} GiB free, {MIN_FREE_GIB} needed')
    if isinstance(facts.get('account'), str):
        out.append(f'the account entry: {facts["account"]}')
        return out
    targets = facts.get('targets') if isinstance(facts.get('targets'), dict) else {}
    for t in site.accounts[alias].a['targets']:
        f = targets.get(t)
        if not isinstance(f, dict):
            out.append(f'{t}: not checked (the cluster-side entry lacks it: apply the plan)')
            continue
        if not f.get('partition_up'):
            out.append(f'{t}: partition {site.targets[t].t["partition"]} is not up')
        if not f.get('test_only'):
            out.append(f'{t}: sbatch --test-only refused the request: {f.get("test_only_text", "")}')
    return out


def retire_text(text: str, alias: str) -> str:
    """The site config with ``retired = true`` in ``[accounts.<alias>]``. Pure."""
    lines = text.splitlines(keepends=True)
    head = next((i for i, x in enumerate(lines) if re.fullmatch(rf'\[accounts\.{re.escape(alias)}\]\s*(#.*)?', x.strip())),
                None)
    if head is None:
        raise SetupError(f'no [accounts.{alias}] table')
    end = next((i for i in range(head + 1, len(lines)) if lines[i].lstrip().startswith('[')), len(lines))
    for i in range(head + 1, end):
        if re.match(r'\s*retired\s*=', lines[i]):
            lines[i] = 'retired = true\n'
            break
    else:
        lines.insert(head + 1, 'retired = true\n')
    out = ''.join(lines)
    if not slurmsite.load(out).accounts[alias].retired:
        raise SetupError(f'accounts.{alias}: not retired after the edit')
    return out


# ------------------------------------------------------------------ the login host


def ssh_via(ssh: str, via: str) -> list[str]:
    if not slurmsite.SSH_ALIAS.fullmatch(via):
        raise SetupError(f'--via {via!r}: not an ssh alias')
    return [ssh, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20', via]


def login_call(site: slurmsite.Site, via: str, call: str, args: dict, *, ssh: str, run: Callable,
               timeout: float = 600, opts: tuple[str, ...] = (), stream: bool = False) -> dict:
    """``slurmlogin.<call>(**args)`` on the login host through ``via``, with more ssh
    ``opts`` before the alias; ``stream``: its stderr (a build's steps) to ours."""
    mods = {'slurmlogin': slurmlogin, 'slurmgate': sys.modules.get('ci_runners.slurmgate')}
    if mods['slurmgate'] is None:
        from ci_runners import slurmgate
        mods['slurmgate'] = slurmgate
    src = {}
    for n, m in mods.items():
        with open(m.__file__, encoding='utf-8') as f:  # type: ignore[union-attr]
            src[n] = f.read()
    cmd = f'{shlex.quote(str(site.site["login_python"]))} -I -c {shlex.quote("import sys; exec(sys.stdin.readline())")}'
    data = f'exec({BOOT!r})\n' + json.dumps({'modules': src, 'call': call, 'args': args})
    try:
        argv = ssh_via(ssh, via)
        r = run([*argv[:-1], *opts, argv[-1], cmd], input=data.encode(), stdout=subprocess.PIPE,
                stderr=None if stream else subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SetupError(f'{call} on {via}: no answer in {timeout:g} s') from None
    if r.returncode != 0:
        why = (r.stderr or b'').decode(errors='replace').strip()[-300:] or f'exit {r.returncode} (its output is above)'
        raise SetupError(f'{call} on {via}: {why}')
    _, mark, out = r.stdout.decode(errors='replace').rpartition(SENTINEL)
    try:
        return json.loads(out if mark else '')
    except ValueError:
        raise SetupError(f'{call} on {via}: not JSON') from None


def copy_release(release: str, files: list[str], base: str, tag: str, via: str, *, ssh: str, rsync: str,
                 run: Callable) -> None:
    rules = [f'--include=/{d}/' for d in sorted({f.split('/')[0] for f in files})]
    rules += [f'--include=/{f}' for f in files] + ['--exclude=*']
    argv = [rsync, '-rlpt', '--delete-excluded', '--chmod=go=', '-e', shlex.join(ssh_via(ssh, via)[:-1]),
            *rules, release.rstrip('/') + '/', f'{via}:{base}/releases/{tag}/']
    r = run(argv, capture_output=True, timeout=1800)
    if r.returncode != 0:
        raise SetupError(f'the release copy: rsync exit {r.returncode}: '
                         f'{(r.stderr or b"").decode(errors="replace").strip()[-300:]}')


# ------------------------------------------------------------------ the commands


class Ctx:
    """A command's inputs: the host's ``etc`` and ``data``, its role, the tools."""

    def __init__(self, etc: str, data: str, driver: str, *, ssh: str = '/usr/bin/ssh', rsync: str = '/usr/bin/rsync',
                 run: Callable = subprocess.run, release: str = RELEASE, out=None) -> None:
        self.etc, self.data, self.driver, self.ssh, self.rsync, self.run = etc, data, driver, ssh, rsync, run
        self.release, self.out = release, out or sys.stdout

    def site(self, name: str) -> tuple[slurmsite.Site, str]:
        path = slurmsite.site_files(self.etc).get(name)
        if path is None:
            raise SetupError(f'no site config etc/slurm/{name}.toml')
        return slurmsite.load_file(path), path

    def account(self, name: str, alias: str) -> slurmsite.Site:
        site, _ = self.site(name)
        if alias not in site.accounts:
            raise SetupError(f'no account {alias} in site {name}')
        return site

    def gate(self, site: slurmsite.Site, alias: str) -> slurmpart.Gate:
        return slurmpart.Gate(self.etc, self.data, site, alias, ssh=self.ssh, rsync=self.rsync, run=self.run)

    def say(self, text: str) -> None:
        self.out.write(text + '\n')

    def plan(self, site: slurmsite.Site, alias: str) -> str:
        return plan_text(site, alias, release=self.release, key=public_key(self.etc, alias), driver=self.driver)

    def record(self, site: slurmsite.Site, alias: str) -> str:
        return os.path.join(slurmpart.state_dir(self.data, site.name), f'install-{alias}.json')


def check(ctx: Ctx, name: str, alias: str, via: str = '') -> int:
    site = ctx.account(name, alias)
    public_key(ctx.etc, alias)
    if via:
        facts = login_call(site, via, 'facts', {'root': str(site.accounts[alias].a['root']),
                                                'entry': slurmsite.account_entry(site, alias)}, ssh=ctx.ssh, run=ctx.run)
    else:
        try:
            facts = ctx.gate(site, alias).json('check', timeout=600)
        except (slurmpart.Unreachable, slurmpart.Refused) as e:
            raise SetupError(f'the gate: {e} (before an install, check through --via)') from None
    ctx.say(f'{site.name} {alias}: Python {facts.get("python")}, Slurm {facts.get("slurm")}, '
            f'{float(facts.get("free_gib") or 0):.0f} GiB free, envs {facts.get("envs", [])}')
    for t, f in sorted((facts.get('targets') or {}).items()):
        ctx.say(f'  {t}: partition {"up" if f.get("partition_up") else "NOT up"}, '
                f'test-only {"ok" if f.get("test_only") else "REFUSED"}')
    bad = findings(site, alias, facts)
    for x in bad:
        ctx.say(f'finding: {x}')
    ctx.say(f'{len(bad)} finding(s)')
    return 1 if bad else 0


def plan(ctx: Ctx, name: str, alias: str) -> int:
    ctx.out.write(ctx.plan(ctx.account(name, alias), alias))
    return 0


def apply(ctx: Ctx, name: str, alias: str, plan_file: str, via: str) -> int:
    site = ctx.account(name, alias)
    if site.accounts[alias].retired:
        raise SetupError(f'account {alias} is retired')
    text = ctx.plan(site, alias)
    try:
        with open(plan_file, encoding='utf-8') as f:
            approved = f.read()
    except OSError as e:
        raise SetupError(f'--plan {plan_file}: {e.strerror}') from None
    if approved != text:
        raise SetupError('the plan is stale: its inputs changed since it was approved; print and approve a new one')
    base, tag = base_of(site, alias), os.path.basename(os.path.realpath(ctx.release))
    made = login_call(site, via, 'prepare', {'base': base}, ssh=ctx.ssh, run=ctx.run)['made']
    copy_release(ctx.release, release_files(ctx.release), base, tag, via, ssh=ctx.ssh, rsync=ctx.rsync, run=ctx.run)
    entry = json.dumps(slurmsite.account_entry(site, alias), indent=1, sort_keys=True)
    login_call(site, via, 'install', {'base': base, 'tag': tag,
                                      'files': {'account.json': entry, 'python': f'{site.site["login_python"]}\n'}},
               ssh=ctx.ssh, run=ctx.run)
    os.makedirs(slurmpart.state_dir(ctx.data, site.name), mode=0o700, exist_ok=True)
    slurmpart._write_json(ctx.record(site, alias), {'plan_sha256': hashlib.sha256(text.encode()).hexdigest(),
                                                    'release': tag, 'at': time.time(), 'via': via})
    ctx.say(f'{site.name} {alias}: applied ({len(made)} directories made, release {tag}); the owner installs the key '
            f'line of step 4, then: ci-run slurm check {site.name} {alias}')
    return 0


def status(ctx: Ctx, name: str) -> int:
    site, _ = ctx.site(name)
    now = time.time()
    day = datetime.date.fromtimestamp(now)
    gates = {a: ctx.gate(site, a) for a, x in site.accounts.items() if not x.retired}
    cache = slurmpart.state_dir(ctx.data, site.name)
    rows, silent = slurmpart.ledgers(gates, cache, now, day.strftime('%Y-%m'))
    rows.update(slurmpart.retired_rows(site, cache, day.strftime('%Y-%m')))
    total_m = total_d = 0.0
    for alias, a in sorted(site.accounts.items()):
        rec = slurmpart._read_json(ctx.record(site, alias)) or {}
        inst = f'release {rec["release"]}' if rec.get('release') else 'not installed from this host'
        if alias not in rows:
            ctx.say(f'{alias}: {"retired" if a.retired else inst + "; the login host does not answer"}, no ledger cached')
            continue
        month, today = slurmpart.spend(site, rows[alias], day.isoformat())
        total_m, total_d = total_m + month, total_d + today
        live = [j for j in rows[alias] if j.get('JobName', '').startswith('ci-')
                and slurmpart.row_cost(site, j)[1]]
        ctx.say(f'{alias}: {"retired" if a.retired else inst}; month {month:.2f} of {float(a.a["monthly_cap"]):g}, '
                f'today {today:.2f}'
                f'{" (cached: the login host does not answer)" if alias in silent else ""}; '
                f'{len(live)} ci- job(s) live' + ''.join(f'\n  job {j["JobID"]} {j["JobName"]} {j["State"]}' for j in live))
    ctx.say(f'site {site.name}: month {total_m:.2f} of {float(site.site["monthly_cap"]):g}, '
            f'today {total_d:.2f} of {float(site.site["daily_cap"]):g}')
    held = slurmpart._read_json(os.path.join(slurmpart.state_dir(ctx.data, site.name), 'targets.json')) or {}
    for t, h in sorted(held.items()):
        if isinstance(h, dict):
            ctx.say(f'target {t}: held at {h.get("max_cards")} card(s): {h.get("why", "")}')
    return 0


def uninstall(ctx: Ctx, name: str, alias: str, via: str, cancel: bool) -> int:
    """Refuse while a ``ci-`` job of the account is there (unless ``cancel``); retire the
    account in the site config first, so that no part takes it; then the login host's
    steps (``slurmlogin.uninstall``)."""
    site = ctx.account(name, alias)
    _, path = ctx.site(name)
    base = base_of(site, alias)
    acct = str(site.accounts[alias].a['slurm_account'])
    with open(path, encoding='utf-8') as f:
        text = retire_text(f.read(), alias)  # before any step: a config it cannot edit stops here
    jobs = login_call(site, via, 'ci_jobs', {'slurm_account': acct}, ssh=ctx.ssh, run=ctx.run)['jobs']
    if jobs and not cancel:
        raise SetupError(f'ci- jobs of the account are there: {" ".join(jobs)} (--cancel cancels them)')
    tmp = f'{path}.{os.getpid()}.new'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)
    r = login_call(site, via, 'uninstall', {'base': base, 'slurm_account': acct, 'gate': f'{base}/{GATE}'},
                   ssh=ctx.ssh, run=ctx.run)
    try:
        os.unlink(ctx.record(site, alias))
    except FileNotFoundError:
        pass
    ctx.say(f'{site.name} {alias}: {len(r["cancelled"])} ci- job(s) cancelled, {r["key_lines"]} key line(s) removed, '
            f'{"the tree removed" if r["removed"] else "no tree there"}; retired in {os.path.basename(path)}')
    return 0


def main(argv: list[str], inv: inventory.Inventory, etc: str, **kw) -> int:
    p = argparse.ArgumentParser(prog='ci-run slurm')
    sub = p.add_subparsers(dest='verb', required=True)
    for verb in ('check', 'plan', 'apply', 'uninstall'):
        s = sub.add_parser(verb)
        s.add_argument('site')
        s.add_argument('account')
        if verb in ('check', 'apply', 'uninstall'):
            s.add_argument('--via', default='', required=verb != 'check',
                           help="an ssh alias of the account owner's own login")
        if verb == 'apply':
            s.add_argument('--plan', required=True)
        if verb == 'uninstall':
            s.add_argument('--cancel', action='store_true')
    sub.add_parser('status').add_argument('site')
    s = sub.add_parser('env')
    s.add_argument('site')
    s.add_argument('account')
    s.add_argument('project')
    s.add_argument('--via', required=True, help="an ssh alias of the account owner's own login")
    s.add_argument('--variant', action='append', default=[], help='only these of the account\'s variants')
    s.add_argument('--refresh', action='store_true', help='rebuild from the kept spec only if an upstream moved')
    a = p.parse_args(argv)
    ctx = Ctx(etc, str(inv.host['data']), inv.this, **kw)
    try:
        if a.verb == 'check':
            return check(ctx, a.site, a.account, a.via)
        if a.verb == 'plan':
            return plan(ctx, a.site, a.account)
        if a.verb == 'apply':
            return apply(ctx, a.site, a.account, a.plan, a.via)
        if a.verb == 'uninstall':
            return uninstall(ctx, a.site, a.account, a.via, a.cancel)
        if a.verb == 'env':
            from ci_runners import slurmenv
            return slurmenv.env(ctx, a.site, a.account, a.project, a.via, variants=a.variant, refresh=a.refresh,
                                tunnel_port=int((inv.host.get('tunnel') or {}).get('http_port') or 0))
        return status(ctx, a.site)
    except (SetupError, slurmsite.SiteError, ValueError) as e:
        print(f'ci-run slurm {a.verb}: {e}', file=sys.stderr)
        return 2
