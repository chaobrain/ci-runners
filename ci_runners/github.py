"""The ``claim`` step (design §4.1): read a head's statuses, decide, post pending.

``ci-run claim --context C [--sha S] [--pending-description D]`` uses ``GITHUB_TOKEN``
(the job's token, with ``statuses: write``) against ``GITHUB_API_URL`` through the host's
``https_proxy``. Each API call gets 30 s and 3 tries (design §8). Only the statuses of the
runners' own identity count: the policy's ``bot`` (decision 15.9); no allowlisted person's.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping

from ci_runners import claim, policy

TRIES, CALL_BOUND = 3, 30
PAGE, PAGES = 100, 10
SHA = re.compile(r'[0-9a-f]{40}')


class ApiError(RuntimeError):
    pass


def api(method: str, url: str, token: str, body: dict | None = None, opener: Callable = urllib.request.urlopen,
        tries: int = TRIES, pause: float = 5) -> object:
    data = json.dumps(body).encode() if body is not None else None
    last = ''
    for i in range(tries):
        req = urllib.request.Request(url, data=data, method=method, headers={
            'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'ci-runners'})
        try:
            with opener(req, timeout=CALL_BOUND) as r:
                return json.loads(r.read() or b'null')
        except urllib.error.HTTPError as e:
            last = f'HTTP {e.code}'
            if e.code < 500 and e.code != 429:
                break  # a refusal does not improve with retries
        except (OSError, ValueError) as e:
            last = type(e).__name__
        if i + 1 < tries:
            time.sleep(pause)
    raise ApiError(f'{method} {url.split("?")[0]}: {last}')


def statuses(call: Callable, url: str, token: str) -> list:
    """Every status of a head, newest first: up to ``PAGES`` pages of ``PAGE``."""
    out: list = []
    for page in range(1, PAGES + 1):
        got = call('GET', f'{url}&page={page}', token)
        if not isinstance(got, list):
            raise ApiError('GET statuses: not a list')
        out += got
        if len(got) < PAGE:
            break
    return out


def run_claim(env: Mapping[str, str], *, context: str, sha: str, description: str, bot: str,
              call: Callable = api) -> claim.Claim:
    repo, token = env.get('GITHUB_REPOSITORY', ''), env.get('GITHUB_TOKEN', '')
    base = env.get('GITHUB_API_URL', 'https://api.github.com').rstrip('/')
    if not SHA.fullmatch(sha) or not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9._-]+', repo) or not token:
        raise ValueError('claim needs a full sha, GITHUB_REPOSITORY and GITHUB_TOKEN')
    url = f'{base}/repos/{repo}/commits/{sha}/statuses?per_page={PAGE}'
    c = claim.decide(claim.parse_statuses(statuses(call, url, token), context), bot=bot)
    if c.run:
        target = f'{env.get("GITHUB_SERVER_URL", "https://github.com")}/{repo}/actions/runs/{env.get("GITHUB_RUN_ID", "")}'
        call('POST', f'{base}/repos/{repo}/statuses/{sha}', token,
             {'state': 'pending', 'context': context, 'description': description[:140], 'target_url': target})
    return c


def main(argv: list[str], etc: str, call: Callable | None = None) -> int:
    p = argparse.ArgumentParser(prog='ci-run claim')
    p.add_argument('--context', required=True)
    p.add_argument('--sha', default='')
    p.add_argument('--pending-description', default='queued on the runners')
    a = p.parse_args(argv)
    env = os.environ
    sha = a.sha or env.get('GITHUB_SHA', '')
    try:
        with open(os.path.join(etc, 'policy.toml'), 'rb') as f:
            bot = policy.load(f.read(1 << 20)).bot  # the runners' identity; the actors are the hook's alone
        c = run_claim(env, context=a.context, sha=sha, description=a.pending_description, bot=bot, call=call or api)
    except (OSError, ValueError, ApiError) as e:
        print(f'ci-run claim: {e}', file=sys.stderr)
        return 2
    if env.get('GITHUB_OUTPUT'):
        with open(env['GITHUB_OUTPUT'], 'a', encoding='utf-8') as f:
            f.write(f'run={"true" if c.run else "false"}\nreason={c.reason}\n')
    print(f'ci-run claim: {"run" if c.run else "skip"} ({c.reason})', file=sys.stderr)
    return 0
