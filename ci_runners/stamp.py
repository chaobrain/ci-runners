"""The release stamp (design §9, decision 16.6): ``python -m ci_runners.stamp RELEASE``.

``RELEASE`` is ``vX.Y.Z`` (the stamp PR) or ``hook`` (the restore PR). It writes
``actions/RELEASE`` and rewrites, in the reusable workflows ``ci.yml`` and ``part.yml``
alone, every reference to this repository's actions and ``ci.yml``'s reference to
``part.yml`` to ``@<RELEASE>``, or to ``@v1`` for ``hook``. Nothing else changes, so a
caller pinned to ``ci.yml@vX.Y.Z`` runs that release's ``part.yml`` and actions, and they
that release's host layer.

``--ref REF`` names the references ``@REF`` instead and leaves ``actions/RELEASE`` alone:
a branch that proves the workflows on the canary before a release (its actions then run
the host's ``hook`` release). Stdlib only.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

WORKFLOWS = ('.github/workflows/ci.yml', '.github/workflows/part.yml')
#: a reference this repository's reusable workflows make to itself
REF = re.compile(r'(chaobrain/ci-runners/(?:actions/[a-z][a-z0-9-]*|\.github/workflows/part\.yml))@([A-Za-z0-9._/-]+)')
RELEASE = re.compile(r'(hook|v\d+\.\d+\.\d+)\Z')
GIT_REF = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,99}\Z')
MAJOR = 'v1'


def refs(text: str) -> list[tuple[str, str]]:
    """``(target, ref)`` of every self-reference in ``text``."""
    return [(m.group(1), m.group(2)) for m in REF.finditer(text)]


def rewrite(text: str, ref: str) -> str:
    return REF.sub(lambda m: f'{m.group(1)}@{ref}', text)


def stamp(root: pathlib.Path, release: str, ref: str = '') -> list[str]:
    """Stamp the tree at ``root``; the files it changed. Raises ValueError for a bad
    release or ref."""
    if not ref and not RELEASE.match(release):
        raise ValueError(f'release {release!r} is neither hook nor vX.Y.Z')
    if ref and not GIT_REF.match(ref) or '..' in ref:
        raise ValueError(f'ref {ref!r} is not a branch or tag name')
    target = ref or (MAJOR if release == 'hook' else release)
    changed = []
    if not ref:
        f = root / 'actions' / 'RELEASE'
        if f.read_text().strip() != release:
            f.write_text(release + '\n')
            changed.append('actions/RELEASE')
    for rel in WORKFLOWS:
        f = root / rel
        old = f.read_text()
        if not refs(old):
            raise ValueError(f'{rel}: no reference to stamp')
        new = rewrite(old, target)
        if new != old:
            f.write_text(new)
            changed.append(rel)
    return changed


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog='python -m ci_runners.stamp')
    ap.add_argument('release', help='vX.Y.Z, or hook to restore')
    ap.add_argument('--ref', default='', help='a proof branch: name the references @REF, leave actions/RELEASE')
    ap.add_argument('--root', default=str(pathlib.Path(__file__).resolve().parents[1]))
    a = ap.parse_args(argv)
    try:
        for rel in stamp(pathlib.Path(a.root), a.release, a.ref):
            print(f'stamped {rel}')
    except (OSError, ValueError) as e:
        print(f'stamp: {e}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
