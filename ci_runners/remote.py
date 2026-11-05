"""The environment a remote part sees (design §5.4, "Env allowlist"; T8).

RUN-1 holds only the allowlist; the h100 driver that uses it comes with RUN-2.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

#: names whose values come from the inventory, never from the job's environment
FIXED = ('PATH', 'HOME', 'LANG')
#: refused even if a project lists them: design §5.4's list, plus more secret-like
#: parts and the variables that make a shell or the loader run other code
REFUSED_PREFIXES = ('GITHUB_', 'ACTIONS_', 'RUNNER_')
REFUSED_NAMES = frozenset({'SSH_AUTH_SOCK', 'BASH_ENV', 'ENV', 'LD_PRELOAD', 'LD_AUDIT'})
REFUSED_PARTS = ('TOKEN', 'SECRET', 'PASSWORD', 'PASSWD', 'CREDENTIAL', 'API_KEY', 'APIKEY', 'PRIVATE_KEY')
#: a value that looks like a GitHub token never leaves, whatever its name
_TOKEN_VALUE = re.compile(r'gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}')

_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,127}\Z')


def refused(name: str) -> bool:
    """Whether ``name`` may never reach a remote: not a plain variable name, a GitHub,
    Actions or runner variable, the ssh agent socket, or a name containing TOKEN, SECRET
    or PASSWORD in any case. Pure."""
    if not isinstance(name, str) or not _NAME.match(name):
        return True
    up = name.upper()
    return up.startswith(REFUSED_PREFIXES) or up in REFUSED_NAMES or any(p in up for p in REFUSED_PARTS)


def check_names(names: Iterable[str]) -> list[str]:
    """The names of a project's ``remote_env`` that are refused or fixed, sorted (the
    registry lint reports them). Pure."""
    return sorted({n for n in names if refused(n) or n in FIXED or n == 'CUDA_VISIBLE_DEVICES'}, key=str)


def remote_env(environ: Mapping[str, str], *, fixed: Mapping[str, str], names: Iterable[str],
               cuda: str) -> dict[str, str]:
    """The variables a remote part runs with:

    - ``PATH``, ``HOME`` and ``LANG`` from ``fixed`` (the inventory), all three required;
    - ``CUDA_VISIBLE_DEVICES`` set to ``cuda``, the driver's card, never the job's value;
    - every ``CI_*`` variable of ``environ``;
    - the project's ``names`` that ``environ`` holds;

    minus every refused name (``refused``). A value that is not a str, holds a NUL or
    looks like a GitHub token is dropped. Pure.
    """
    missing = [k for k in FIXED if not isinstance(fixed.get(k), str) or not fixed[k]]
    if missing:
        raise ValueError(f'remote_env: the inventory gives no {", ".join(missing)}')
    if not isinstance(cuda, str) or not re.fullmatch(r'[0-9]+(,[0-9]+)*', cuda):
        raise ValueError('remote_env: cuda must be a list of card indexes')
    fixed_names = set(FIXED) | {'CUDA_VISIBLE_DEVICES'}
    wanted = set(names) | {k for k in environ if isinstance(k, str) and k.startswith('CI_')}
    out = {**{k: fixed[k] for k in FIXED}, 'CUDA_VISIBLE_DEVICES': cuda}
    for k in sorted(wanted - fixed_names, key=str):
        v = environ.get(k)
        if not refused(k) and isinstance(v, str) and '\0' not in v and not _TOKEN_VALUE.search(v):
            out[k] = v
    return out
