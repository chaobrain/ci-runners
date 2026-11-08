"""``ci-run slurm env <site> <account> <project> --via ALIAS``: build a project's env on a
cluster's login host (design decision 14.9, operations §10), not on the driver, whose
newer C library could pick wheels the nodes cannot load.

The driver reads the project's env spec as ``ci-run env build`` does (``envbuild.fetch``:
the default branch's ``.github/ci-runners.toml`` and its requirement files, with the
maintainer's ``GH_TOKEN`` for this run only, which never leaves the driver), and computes
the steps of each build (``plan_builds``) for ``<root>/ci-runners/envs``: one build,
``envs/<project>-<stamp>``, for the variants the account's targets run that the spec does
not name, and one of its own, ``envs/<project>-<stamp>-<variant>``, for each it names
(``[variant.<name>]``: its ``install`` replaces the base's). The login host
(``slurmlogin.build``, through the account owner's login) runs them at nice 10, then,
once every build has passed, points ``envs/<project>-<variant>`` at its build for each
variant (what the batch job uses, ``slurmgate.env_dir``).

Egress, by the site's ``egress`` (D4):

- ``tunnel``: the build's ssh connection carries a reverse forward from a Unix socket in
  ``<root>/ci-runners/run`` (mode 700: no other user of the login host reaches it) to the
  driver's egress forward (the inventory's ``tunnel.http_port``), and the build runs a
  relay on the login host's 127.0.0.1 that takes only its random ``Proxy-Authorization``
  (``slurmlogin.Relay``), given to pip, conda and git through their environment. The
  egress host's allowlist applies (11.10). Both end with the build: nothing listens after.
- ``proxy``: the site's proxy; ``direct``: none.
- ``push`` (a wheelhouse installed offline, D4 b) is not built: it refuses.

Without conda on the login host, the build first installs Miniforge (``MINIFORGE``, its
sha256 pinned) into ``<root>/ci-runners/conda``, offline, from an installer the driver
downloads once into its cache. Upstreams are cloned anonymously (a private one fails).
``--refresh`` rebuilds from the spec the driver kept of the last build
(``state/slurm/<site>/env-<account>-<project>.json``) only when an upstream's branch moved
since that variant's build, without a token: the kept file holds each variant's spec
commit and upstream commits, so a ``--variant`` subset run leaves the others' as they were,
and a refresh rebuilds every variant asked for (all of the account's, by default) whose
upstreams moved or whose build is of an older spec; "up to date" holds for each of them.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import shlex
import subprocess
import time
import urllib.request
from collections.abc import Callable

from ci_runners import envbuild, github, registry, slurmpart, slurmsetup
from ci_runners.slurmsetup import Ctx, SetupError

#: (installer, its URL, its sha256): the conda the login host gets where it has none
MINIFORGE = ('Miniforge3-26.9.4-2-Linux-x86_64.sh',
             'https://github.com/conda-forge/miniforge/releases/download/26.9.4-2/Miniforge3-26.9.4-2-Linux-x86_64.sh',
             'bbaf16b3897da24c4be50315934ba74848f617b4495982841d6a0a60e0edbbdf')
SOCKET_MAX = 100  # bytes of a Unix socket path (108 with its NUL, less a margin)


def installer(cache: str, opener: Callable = urllib.request.urlopen) -> str:
    """The Miniforge installer in the driver's ``cache``, downloaded once (through the
    driver's egress) and checked against its pinned sha256."""
    name, url, sha = MINIFORGE
    path = os.path.join(cache, name)
    if not os.path.exists(path):
        os.makedirs(cache, mode=0o700, exist_ok=True)
        tmp = f'{path}.{os.getpid()}.part'
        with opener(url, timeout=600) as r, open(tmp, 'wb') as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        os.replace(tmp, path)
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    if h.hexdigest() != sha:
        os.unlink(path)
        raise SetupError(f'{name}: sha256 {h.hexdigest()} is not the pinned {sha}')
    return path


def grace(site, alias: str) -> int:
    """Seconds a build no link holds is kept: the account's longest part, its margin and an
    hour (a job resolves its env's link when it starts)."""
    return 60 * (max(site.targets[t].max_minutes for t in site.accounts[alias].a['targets']) + 60)


def plan_builds(spec: envbuild.Spec, variants: list[str], *, project: str, envs: str, stamp: str, reqdir: str,
                conda: str) -> list[dict]:
    """The builds an env run makes: one for the variants the spec does not name (the base),
    and one of its own for each it names (``[variant.<name>]``, whose ``install`` replaces
    the base's). Pure."""
    out = []
    base = [v for v in variants if v not in spec.variants]
    if base:
        out.append({'variant': '', 'links': base, 'install': list(spec.install),
                    'steps': envbuild.steps(spec, project=project, envs=envs, stamp=stamp, reqdir=reqdir, conda=conda)})
    for v in variants:
        if v in spec.variants:
            vs = spec.variant(v)
            out.append({'variant': v, 'links': [v], 'install': list(vs.install),
                        'steps': envbuild.steps(vs, project=project, envs=envs, stamp=stamp, reqdir=reqdir, conda=conda,
                                                variant=v)})
    return out


def env(ctx: Ctx, name: str, alias: str, project: str, via: str, *, variants: list[str] = (), refresh: bool = False,
        tunnel_port: int = 0, call: Callable | None = None, opener: Callable = urllib.request.urlopen) -> int:
    site = ctx.account(name, alias)
    mode = str(site.site['egress'])
    if mode == 'push':
        raise SetupError('egress "push" (a wheelhouse installed offline, D4 b) is not built: use tunnel, proxy or direct')
    if mode == 'tunnel' and not 1 <= tunnel_port <= 65535:
        raise SetupError('egress "tunnel" needs this host\'s egress forward (the inventory\'s tunnel.http_port)')
    own = sorted({str(site.targets[t].t['variant']) for t in site.accounts[alias].a['targets']})
    if set(variants) - set(own):
        raise SetupError(f'--variant {sorted(set(variants) - set(own))}: not a variant of {alias}\'s targets {own}')
    proj = registry.load_file(ctx.etc, project, ctx.driver)
    kept_path = os.path.join(slurmpart.state_dir(ctx.data, site.name), f'env-{alias}-{project}.json')
    kept = slurmpart._read_json(kept_path)
    if refresh:
        if kept is None:
            raise SetupError(f'no build of {project} kept for {alias}: run env without --refresh')
        spec, files, sha = envbuild.parse_spec(kept['spec']), kept['files'], kept['sha']
        genv = envbuild._git_env()
        heads = {u.name: envbuild.head_of(ctx.run, u, genv) for u in spec.upstream}
        held = kept.get('variants', {})
        stale = [v for v in (list(variants) or own) if held.get(v, {}).get('sha') != sha
                 or held[v].get('commits') != heads]  # one built from an older spec, or never, is stale too
        if not stale:
            ctx.say(f'{site.name} {alias} {project}: up to date')
            return 0
        moved = sorted({n for v in stale for n in heads if heads[n] != held.get(v, {}).get('commits', {}).get(n)})
        ctx.say(f'{site.name} {alias} {project}: moved: {", ".join(moved) or "none"}; rebuilding {", ".join(stale)}')
        variants = stale
    else:
        sha, spec, files = envbuild.fetch(call or github.api, envbuild._token(ctx.etc), proj.repository)
    base = slurmsetup.base_of(site, alias)
    stamp = envbuild._stamp()
    conda = f'{base}/conda/bin/conda'
    builds = plan_builds(spec, list(variants) or own, project=project, envs=f'{base}/envs', stamp=stamp,
                         reqdir=f'{base}/run/req-{stamp}', conda=conda)
    lc = dict(ssh=ctx.ssh, run=ctx.run)
    inst = ''
    if not slurmsetup.login_call(site, via, 'conda_state', {'base': base}, **lc)['conda']:
        local = installer(os.path.join(ctx.data, 'ci-runners', 'cache'), opener)
        inst = f'{base}/run/{MINIFORGE[0]}'
        r = ctx.run([ctx.rsync, '-t', '--chmod=go=', '-e', shlex.join(slurmsetup.ssh_via(ctx.ssh, via)[:-1]), local,
                     f'{via}:{inst}'], capture_output=True, timeout=1800, env=slurmsetup.ssh_env())
        if r.returncode != 0:
            raise SetupError(f'the installer copy: rsync exit {r.returncode}')
    args = {'base': base, 'project': project, 'stamp': stamp, 'builds': builds, 'files': files,
            'record': {'repository': proj.repository, 'sha': sha, 'upstream_names': [u.name for u in spec.upstream]},
            'egress': mode, 'proxy': str(site.site.get('proxy', '')),
            'installer': inst, 'grace': grace(site, alias)}
    opts: tuple[str, ...] = ()
    if mode == 'tunnel':
        sock = f'{base}/run/egress-{stamp}.sock'
        if len(sock.encode()) > SOCKET_MAX:
            raise SetupError(f'{sock}: longer than a Unix socket path may be')
        args.update(secret=secrets.token_urlsafe(24), socket_path=sock)
        opts = ('-o', 'ExitOnForwardFailure=yes', '-o', 'StreamLocalBindMask=0177', '-R', f'{sock}:127.0.0.1:{tunnel_port}')
    ctx.say(f'{site.name} {alias}: building {project} ({proj.repository} at {sha[:12]}), egress {mode}: '
            + '; '.join(f'{project}-{stamp}' + (f'-{b["variant"]}' if b['variant'] else '') + f' for {", ".join(b["links"])}'
                        for b in builds))
    t0 = time.monotonic()
    r = slurmsetup.login_call(site, via, 'build', args, timeout=4 * 3600, opts=opts, stream=True, **lc)
    os.makedirs(os.path.dirname(kept_path), mode=0o700, exist_ok=True)
    built = dict((kept or {}).get('variants', {}))  # each variant's own build, kept across subset runs
    built.update(r.get('variants') or {})  # what the login host links: also a run from another data dir
    built.update({v: {'build': b, 'sha': sha, 'commits': r['commits']} for b, links in r['builds'].items() for v in links})
    slurmpart._write_json(kept_path, {'sha': sha, 'spec': spec.text, 'files': files, 'stamp': stamp,
                                      'builds': r['builds'],
                                      'variants': {v: e for v, e in sorted(built.items()) if v in own}})
    ctx.say(f'{site.name} {alias}: ' + '; '.join(f'{", ".join(links)} -> {b}' for b, links in r['builds'].items())
            + f' in {time.monotonic() - t0:.0f} s; removed {r["removed"] or "none"}')
    return 0
