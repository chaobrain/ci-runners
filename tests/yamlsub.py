"""A reader of the YAML subset this repository's workflows and actions are written in, so
that their structure tests need no third-party package (AGENTS.md: stdlib tests).

Block mappings and sequences, plain and quoted scalars, one-line flow sequences and
mappings of scalars, ``|`` and ``>`` block scalars (with ``-``), and comments. Plain
scalars stay strings but for integers, ``true``/``false`` and ``null``; ``on`` stays a
string (YAML 1.1 would read it as true). Anything else raises ``ValueError``, so a file
outside the subset fails its test instead of being misread.
"""
from __future__ import annotations

import re

_INT = re.compile(r'-?[0-9]+\Z')


def _strip_comment(s: str) -> str:
    out, q = [], ''
    for i, ch in enumerate(s):
        if q:
            if ch == q:
                q = ''
        elif ch in '\'"' and (i == 0 or s[i - 1] in ' [{,:'):
            q = ch
        elif ch == '#' and (i == 0 or s[i - 1] == ' '):
            break
        out.append(ch)
    return ''.join(out).rstrip()


def scalar(s: str) -> object:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] == "'":
        return s[1:-1].replace("''", "'")
    if len(s) >= 2 and s[0] == s[-1] == '"':
        return s[1:-1].replace('\\"', '"').replace('\\\\', '\\')
    if s.startswith('[') and s.endswith(']'):
        inner = s[1:-1].strip()
        return [scalar(x) for x in _split(inner)] if inner else []
    if s.startswith('{') and s.endswith('}'):
        inner = s[1:-1].strip()
        out = {}
        for x in _split(inner) if inner else []:
            k, _, v = x.partition(':')
            out[str(scalar(k))] = scalar(v)
        return out
    if s.startswith(('[', '{', '&', '*', '!', '|', '>')):
        raise ValueError(f'outside the subset: {s!r}')
    if _INT.match(s):
        return int(s)
    return {'true': True, 'false': False, 'null': None, '~': None, '': None}.get(s, s)


def _split(s: str) -> list[str]:
    out, cur, q, depth = [], '', '', 0
    for ch in s:
        if q:
            q = '' if ch == q else q
        elif ch in '\'"':
            q = ch
        elif ch in '[{':
            depth += 1
        elif ch in ']}':
            depth -= 1
        elif ch == ',' and depth == 0:
            out.append(cur)
            cur = ''
            continue
        cur += ch
    out.append(cur)
    return [x.strip() for x in out]


def _key(s: str) -> tuple[str, str] | None:
    m = re.match(r'([A-Za-z0-9_.${}\'" -]+?):(?: (.*)|$)', s)
    if not m or m.group(1).startswith(('-', '"', "'")):
        return None
    return m.group(1), (m.group(2) or '')


def load(text: str) -> object:
    lines = []
    for raw in text.splitlines():
        if '\t' in raw[:len(raw) - len(raw.lstrip())]:
            raise ValueError('a tab in the indentation')
        lines.append(raw)
    pos = [0]

    def indent(line: str) -> int:
        return len(line) - len(line.lstrip(' '))

    def skip() -> None:
        while pos[0] < len(lines) and not _strip_comment(lines[pos[0]]).strip():
            pos[0] += 1

    def block_scalar(style: str, parent: int) -> str:
        body = []
        while pos[0] < len(lines) and (not lines[pos[0]].strip() or indent(lines[pos[0]]) > parent):
            body.append(lines[pos[0]])
            pos[0] += 1
        while body and not body[-1].strip():
            body.pop()
        ind = min((indent(x) for x in body if x.strip()), default=0)
        body = [x[ind:] for x in body]
        if style[0] == '|':
            out = '\n'.join(body)
        else:
            out = ' '.join(x.strip() for x in body if x.strip())
        return out if style.endswith('-') else out + '\n'

    def value(rest: str, parent: int) -> object:
        rest = _strip_comment(rest).strip()
        if rest in ('|', '|-', '>', '>-'):
            return block_scalar(rest, parent)
        if rest:
            return scalar(rest)
        skip()
        if pos[0] >= len(lines) or indent(lines[pos[0]]) <= parent and not lines[pos[0]].lstrip().startswith('- '):
            return None
        return node(indent(lines[pos[0]]))

    def node(ind: int) -> object:
        skip()
        first = lines[pos[0]][ind:]
        if first.startswith('- ') or first == '-':
            seq = []
            while True:
                skip()
                if pos[0] >= len(lines) or indent(lines[pos[0]]) != ind or not lines[pos[0]][ind:].startswith('-'):
                    return seq
                item = _strip_comment(lines[pos[0]][ind + 1:])
                if not item.strip():
                    pos[0] += 1
                    seq.append(value('', ind))
                    continue
                inner = ind + 1 + indent(item)
                kv = _key(item.strip())
                if kv:  # a mapping item: its keys line up at ``inner``
                    lines[pos[0]] = ' ' * inner + item.strip()
                    seq.append(node(inner))
                else:
                    pos[0] += 1
                    seq.append(value(item, ind))
        out: dict = {}
        while True:
            skip()
            if pos[0] >= len(lines) or indent(lines[pos[0]]) != ind or lines[pos[0]][ind:].startswith('- '):
                return out
            kv = _key(_strip_comment(lines[pos[0]]).strip())
            if not kv:
                raise ValueError(f'line {pos[0] + 1}: not a key: {lines[pos[0]]!r}')
            k, rest = kv
            k = str(scalar(k)) if k[0] in '\'"' else k
            if k in out:
                raise ValueError(f'line {pos[0] + 1}: key {k!r} twice')
            pos[0] += 1
            out[k] = value(rest, ind)

    skip()
    doc = node(0) if pos[0] < len(lines) else None
    skip()
    if pos[0] < len(lines):
        raise ValueError(f'line {pos[0] + 1}: not read: {lines[pos[0]]!r}')
    return doc
