"""Pinned environment recipes on a Slurm account's shared filesystem."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import shlex
import stat
import subprocess
import sys
import tempfile
import time

from ci_runners import environment_cache, part, slurmgate as gate, slurmlogin, slurmpart


def check(account, request):
    recipe = request['environment_recipe']
    if 'allocation' not in request:
        raise gate.GateError('environment preparation requires a central CPU allocation')
    project = request['project']
    allowed = account.get('cache_projects', {}).get(request['target'])
    if allowed is not True and not (isinstance(allowed, list) and project in allowed):
        raise gate.GateError('cached environments are not enabled for this project on the target')
    spec = environment_cache.validate(recipe)
    run_repository = '/'.join(request['run_url'].split('/')[3:5]).lower()
    if recipe['repository'].lower() != run_repository:
        raise gate.GateError('the pinned environment does not belong to the source repository')
    if not gate.REQUEST['variables'][0](dict(spec.variables)):
        raise gate.GateError('invalid environment variables for a Slurm job')
    return spec


def check_key(key, request):
    match = gate.KEY.fullmatch(key) if isinstance(key, str) else None
    if not match or f"{match['owner']}/{match['repo']}" != request['environment_recipe']['repository'].lower():
        raise gate.GateError('the environment repository does not match the job key')


def _read(data):
    if len(data) > gate.MAX_REQUEST:
        raise gate.GateError('the cache request is too long')
    try:
        value = json.loads(data)
    except (ValueError, RecursionError):
        raise gate.GateError('the cache request is not bounded JSON') from None
    if not isinstance(value, dict):
        raise gate.GateError('invalid cache request')
    return value


def _prefix(base, account, request):
    return environment_cache.lookup(Path(base) / 'envs/cache', request['environment_recipe'],
                                    variant=account['targets'][request['target']]['variant'])


def probe(base, key, data):
    request = _read(data)
    if 'environment_recipe' not in request:
        raise gate.GateError('missing environment recipe')
    account = gate.load_account(base)
    gate.check_request(request, account, base)
    check_key(key, request)
    return {'ready': _prefix(base, account, request) is not None}


def _ensure(base, account, request, proxy):
    # Build commands inherit host-owned inputs only; the test's PATH, temp,
    # GITHUB_TOKEN, proxy and package index settings are never build inputs.
    variant = account['targets'][request['target']]['variant']
    with tempfile.TemporaryDirectory(dir=Path(base) / 'run', prefix='cache-home-') as home:
        env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': home, 'TMPDIR': home}
        if proxy:
            env.update({name: proxy for name in ('https_proxy', 'http_proxy', 'HTTPS_PROXY', 'HTTP_PROXY')})
            env.update(no_proxy='localhost,127.0.0.1', NO_PROXY='localhost,127.0.0.1')
        return environment_cache.ensure(Path(base) / 'envs/cache', request['environment_recipe'],
                                        variant=variant, conda=str(Path(base) / 'conda/bin/conda'),
                                        host_lock=str(Path(base) / 'envs' / slurmlogin.BUILD_LOCK),
                                        build_env=env, timeout=max(1, int(request['minutes'] * 60)))


def execute(directory):
    """Child of the allocation supervisor. Resource checks have already passed."""
    d = Path(directory)
    base = d.parents[4]
    try:
        request = _read((d / 'request.json').read_bytes())
        account = gate.load_account(str(base))
        check_key(d.relative_to(base / 'work').as_posix(), request)
        spec = check(account, request)
        prefix = _prefix(base, account, request)
        if prefix is None:
            mode = account.get('cache_egress')
            if mode not in ('direct', 'proxy'):
                raise gate.GateError('the pinned cache is unavailable on this node; check cluster build egress and platform')
            prefix = _ensure(base, account, request, account.get('cache_proxy', ''))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, gate.GateError) as exc:
        # No requested command has been launched. Preserve a reason in the job
        # log, without printing credential-bearing proxy URLs or build arguments.
        detail = str(exc) if isinstance(exc, (environment_cache.ERROR, gate.GateError)) else type(exc).__name__
        message = f'environment preparation failed: {detail}'[:350]
        part._write_json(str(d/'record'/'environment-failure.json'), {'reason': message})
        print(message, file=sys.stderr, flush=True)
        return 2
    env = dict(os.environ, **spec.variables)
    env.update(CONDA_PREFIX=str(prefix), PATH=f'{prefix}/bin:/usr/local/bin:/usr/bin:/bin')
    argv = part.command_argv(request['command'])
    os.execvpe(argv[0], argv, env)


def _endpoint(ssh, config, alias, run):
    query = run([ssh, *config, '-G', alias], capture_output=True, timeout=20)
    facts = {}
    for line in query.stdout.decode(errors='replace').splitlines():
        name, _, value = line.partition(' ')
        if name in ('hostname', 'user', 'port'):
            facts[name] = value.strip().lower() if name == 'hostname' else value.strip()
    if query.returncode or not all(facts.get(k) for k in ('hostname', 'user', 'port')):
        raise slurmpart.Refused('cannot verify the cache builder SSH endpoint')
    return facts


def _wait_for_builder(argv, payload, seconds, stopping):
    with tempfile.TemporaryFile() as body:
        body.write(json.dumps(payload).encode())
        body.seek(0)
        deadline = time.monotonic() + seconds
        with tempfile.TemporaryFile() as errors, subprocess.Popen(argv, stdin=body, stdout=subprocess.DEVNULL,
                              stderr=errors,
                              env={'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': os.path.expanduser('~')}) as child:
            while not stopping() and time.monotonic() < deadline:
                try:
                    rc = child.wait(timeout=1)
                    if rc:
                        errors.seek(max(0, errors.seek(0, os.SEEK_END) - 1024))
                        # The helper emits only a bounded stage description. SSH or an arbitrary
                        # login banner can contain private connection data, so discard other text.
                        for line in errors.read().decode(errors='replace').splitlines():
                            if line.startswith('ci-cache: ') and len(line) <= 350:
                                raise slurmpart.Refused(line)
                    return rc
                except subprocess.TimeoutExpired:
                    pass
            child.kill()
            child.wait()
            raise slurmpart.Refused('cluster cache build cancelled or timed out')


def prepare_tunnel(g, key, request, default_port, stopping):
    """Prepare through the operator-configured builder; never grant forwarding to the CI key."""
    data = json.dumps(request).encode()
    if g.json('cache-ready', key, data=data).get('ready') is True:
        return
    acct = g.site.accounts[g.alias].a
    via = acct.get('cache_build_via')
    port = g.site.site.get('cache_tunnel_port', default_port)
    if not via or not isinstance(port, int) or not 1 <= port <= 65535:
        raise slurmpart.Refused('cold environment needs cache_build_via and the builder egress port')
    config = ['-F', g.ssh_config] if os.path.isfile(g.ssh_config) else []
    base = Path(acct['root']) / 'ci-runners'
    nonce = secrets.token_hex(8)
    socket_path = base / 'run' / f'ec-{nonce}.sock'
    if len(os.fsencode(socket_path)) > 107:
        raise slurmpart.Refused('the cache tunnel socket path is too long')
    payload = dict(request=request, key=key, site=g.site.name, account=g.alias, nonce=nonce)
    argv = [g.ssh, *config, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ForwardAgent=no', '-o', 'IdentitiesOnly=yes', '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
            '-o', 'ExitOnForwardFailure=yes', '-o', 'StreamLocalBindMask=0177',
            '-R', f'{socket_path}:127.0.0.1:{port}', via, shlex.quote(str(base/'releases/hook/bin/ci-slurm-cache-build'))]
    try:
        if _endpoint(g.ssh, config, g.host, g.run) != _endpoint(g.ssh, config, via, g.run):
            raise slurmpart.Refused('cache builder SSH identity differs from the configured cluster account')
        status = _wait_for_builder(argv, payload, max(1, int(request['minutes'] * 60)) + 40, stopping)
    except (OSError, subprocess.SubprocessError) as exc:
        raise slurmpart.Refused(f'cluster environment preparation failed: {type(exc).__name__}') from None
    if status or g.json('cache-ready', key, data=data).get('ready') is not True:
        raise slurmpart.Refused('cluster cache build failed or produced no compatible environment')


def _tunnel_build(base, file):
    payload = _read(Path(file).read_bytes())
    account = gate.load_account(base)
    request = payload['request']
    socket_path = Path(base) / 'run' / f"ec-{payload['nonce']}.sock"
    sock_stat = socket_path.lstat()
    if not stat.S_ISSOCK(sock_stat.st_mode) or sock_stat.st_uid != os.getuid():
        raise gate.GateError('the builder tunnel socket does not belong to this account')
    secret = secrets.token_urlsafe(24)
    relay = slurmlogin.Relay(secret, str(socket_path))
    try:
        _ensure(base, account, request, f'http://ci:{secret}@127.0.0.1:{relay.port}')
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        detail = str(exc) if isinstance(exc, environment_cache.ERROR) else type(exc).__name__
        Path(file).with_suffix('.failure').write_text(detail[:300], encoding='utf-8')
        return 2
    finally:
        relay.close()
    return 0


def builder(base, data):
    """Dedicated login helper. Only process a verified recipe, never the test command."""
    payload = _read(data)
    account = gate.load_account(base)
    if (set(payload) != {'request', 'key', 'site', 'account', 'nonce'}
            or payload['site'] != account['site'] or payload['account'] != account['account']
            or account.get('cache_egress') != 'tunnel' or not isinstance(payload['nonce'], str)
            or not re.fullmatch('[a-f0-9]{16}', payload['nonce'])
            or not isinstance(payload['request'], dict) or 'environment_recipe' not in payload['request']):
        raise gate.GateError('invalid tunnel builder request or cluster identity')
    request = payload['request']
    gate.check_request(request, account, base)
    check_key(payload['key'], request)
    socket_path = Path(base) / 'run' / f"ec-{payload['nonce']}.sock"
    try:
        if _prefix(base, account, request):
            return 0
        with tempfile.TemporaryDirectory(dir=Path(base)/'run', prefix='cache-build-') as temp:
            path = Path(temp)/'inputs.json'
            path.write_bytes(data)
            release = str(Path(__file__).resolve().parents[1])
            call = ('import sys; sys.path.insert(0,sys.argv.pop(1)); '
                    'from ci_runners.slurmcache import _tunnel_build; sys.exit(_tunnel_build(*sys.argv[1:]))')
            rc = part.supervise(temp, request['minutes'] * 60,
                                [sys.executable, '-I', '-c', call, release, base, str(path)],
                                cap=gate._size_bytes(request['memory']))
            if rc:
                reason = path.with_suffix('.failure')
                detail = reason.read_text(encoding='utf-8') if reason.is_file() else 'cluster builder did not finish'
                print(f'ci-cache: {detail}', file=sys.stderr, flush=True)
            return rc
    finally:
        if socket_path.is_socket():
            socket_path.unlink(missing_ok=True)
