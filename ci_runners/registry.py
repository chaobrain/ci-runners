"""The project registry (design §4, consumers §1): ``etc/projects/<name>.toml`` on each
host, added by the lead. It holds what a consumer's inputs may leave out.

.. code-block:: toml

    name = "myproj"                       # the file's stem, [a-z0-9-]
    repository = "chaobrain/myproj"       # the part action refuses any other repository
    context = "myproj/ci"                 # the status context of claim and the verdict
    retries = 1                           # 0 or 1 (design §4.4)
    gpu_mem_mib = 6000                    # free MiB a leased card needs
    memory = "40G"                        # a part's MemoryMax unless the input sets one
    max_waiting = 1                       # waiting parts per pool (design §7 rule 2)
    [fallback]                            # labels per runner class, for next=fallback
    part = ["self-hosted", "part"]
    [hosts.<role>]
    memory_max = "80G"                    # the project's slice, and a part's ceiling
    env = "/abs/prefix/of/the/conda/env"  # PATH gets <env>/bin
    max_slots = 2                         # CI slots this project may hold here
    variables = { NAME = "value" }        # set in every part's environment

Pure but for ``load_file``.
"""
from __future__ import annotations

import dataclasses
import os
import re
import tomllib
from collections.abc import Mapping

NAME = re.compile(r'[a-z0-9][a-z0-9-]{0,39}')
REPO = re.compile(r'[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}')
MEMORY = re.compile(r'[1-9][0-9]*[KMGT]?')
VAR = re.compile(r'[A-Z_][A-Z0-9_]{0,63}')
#: names a project may not set: the executor's own, and what changes how code loads
RESERVED = re.compile(r'(CI_|GITHUB_|ACTIONS_|RUNNER_).*|CUDA_VISIBLE_DEVICES|PATH|LD_PRELOAD|LD_LIBRARY_PATH|'
                      r'LD_AUDIT|PYTHONPATH|PYTHONHOME|PYTHONSTARTUP|BASH_ENV|ENV|HOME|USER|SHELL')
TOP = {'name', 'repository', 'context', 'retries', 'gpu_mem_mib', 'memory', 'max_waiting', 'fallback', 'hosts'}
HOST = {'memory_max', 'env', 'max_slots', 'variables'}


class RegistryError(ValueError):
    """A malformed registry entry: the part ends ``unavailable``."""


@dataclasses.dataclass(frozen=True)
class Project:
    name: str
    repository: str  # lower case
    context: str
    retries: int = 1
    gpu_mem_mib: int = 0
    memory: str = ''
    max_waiting: int = 1
    fallback: Mapping[str, tuple[str, ...]] = dataclasses.field(default_factory=dict)
    memory_max: str = ''  # this host's
    env: str = ''  # this host's
    max_slots: int = 1  # this host's
    variables: Mapping[str, str] = dataclasses.field(default_factory=dict)  # this host's


def _bytes(m: str) -> int:
    n, unit = (int(m[:-1]), m[-1]) if m[-1] in 'KMGT' else (int(m), '')
    return n * {'': 1, 'K': 1 << 10, 'M': 1 << 20, 'G': 1 << 30, 'T': 1 << 40}[unit]


def memory_bytes(m: str) -> int:
    if not isinstance(m, str) or not MEMORY.fullmatch(m):
        raise RegistryError(f'memory {m!r} must match {MEMORY.pattern}')
    return _bytes(m)


def _int(where: str, v: object, lo: int, hi: int) -> int:
    if type(v) is not int or not lo <= v <= hi:
        raise RegistryError(f'{where} must be an integer in [{lo}, {hi}]')
    return v


def parse(doc: Mapping[str, object], role: str, stem: str = '') -> Project:
    """The entry ``doc`` as seen from host ``role``. Raises RegistryError."""
    unknown = set(doc) - TOP
    if unknown:
        raise RegistryError(f'unknown keys {sorted(unknown)}')
    name = doc.get('name')
    if not isinstance(name, str) or not NAME.fullmatch(name) or (stem and name != stem):
        raise RegistryError(f'name must match {NAME.pattern} and the file name')
    repo, context = doc.get('repository'), doc.get('context')
    if not isinstance(repo, str) or not REPO.fullmatch(repo):
        raise RegistryError('repository must be owner/name')
    if not isinstance(context, str) or not re.fullmatch(r'[A-Za-z0-9._/-]{1,100}', context):
        raise RegistryError('context must be a status context such as myproj/ci')
    retries = _int('retries', doc.get('retries', 1), 0, 1)
    gpu_mem = _int('gpu_mem_mib', doc.get('gpu_mem_mib', 0), 0, 1 << 20)
    max_waiting = _int('max_waiting', doc.get('max_waiting', 1), 1, 64)
    memory = doc.get('memory', '')
    if memory:
        memory_bytes(memory)
    fallback = doc.get('fallback', {})
    if not isinstance(fallback, dict) or not all(
            isinstance(k, str) and isinstance(v, list) and v and all(isinstance(x, str) and re.fullmatch(
                r'[A-Za-z0-9._-]{1,64}', x) for x in v) for k, v in fallback.items()):
        raise RegistryError('fallback must map a runner class to a list of labels')
    hosts = doc.get('hosts', {})
    h = hosts.get(role) if isinstance(hosts, dict) else None
    if not isinstance(h, dict):
        raise RegistryError(f'no [hosts.{role}] entry: the project does not run here')
    unknown = set(h) - HOST
    if unknown:
        raise RegistryError(f'hosts.{role}: unknown keys {sorted(unknown)}')
    memory_max = h.get('memory_max')
    memory_bytes(memory_max)
    if memory and _bytes(memory) > _bytes(memory_max):
        raise RegistryError('memory is above the host\'s memory_max')
    env = h.get('env', '')
    if env and (not isinstance(env, str) or not re.fullmatch(r'/[A-Za-z0-9._/-]+', env)):
        raise RegistryError(f'hosts.{role}.env must be an absolute path')
    max_slots = _int(f'hosts.{role}.max_slots', h.get('max_slots', 1), 0, 64)
    variables = h.get('variables', {})
    if not isinstance(variables, dict):
        raise RegistryError(f'hosts.{role}.variables must be a table')
    for k, v in variables.items():
        if not VAR.fullmatch(k) or RESERVED.fullmatch(k):
            raise RegistryError(f'hosts.{role}.variables: {k!r} is not a name a project may set')
        if not isinstance(v, str) or '\0' in v or '\n' in v:
            raise RegistryError(f'hosts.{role}.variables.{k} must be a one-line string')
    return Project(name, repo.lower(), context, retries, gpu_mem, memory, max_waiting,
                   {k: tuple(v) for k, v in fallback.items()}, memory_max, env, max_slots, dict(variables))


def load_file(etc: str, name: str, role: str) -> Project:
    if not NAME.fullmatch(name):
        raise RegistryError(f'project {name!r} is not a registry name')
    path = os.path.join(etc, 'projects', f'{name}.toml')
    try:
        with open(path, 'rb') as f:
            doc = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise RegistryError(f'project {name}: {type(e).__name__}') from None
    return parse(doc, role, name)
