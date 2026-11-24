"""Issue a reviewed synthetic-only grant using an isolated signing identity."""
from __future__ import annotations

import argparse
import os
import sys

from ci_runners import storage_auth as auth, storage_protocol as p


def issue(raw, cfg):
    doc = p.grant(raw, client=cfg['client'])
    directory = auth.protected(cfg['audit_root'], owner=os.getuid(), private=True, directory=True)
    target = directory / (doc['jti'] + '.json')
    signature = auth.sign(raw, cfg['key'], client=cfg['client'])
    header = {'schema': 1, 'operation': doc['operation'], 'grant': p.b64(raw), 'signature': p.b64(signature)}
    # Exclusive audit record: no silently reused/re-scoped grant id at the issuer.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as record:
        record.write(p.canonical(header))
        record.flush()
        os.fsync(record.fileno())
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return header


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--grant', required=True, help='reviewed canonical grant JSON, not a workflow environment')
    args = parser.parse_args(argv)
    try:
        cfg = auth.config(args.config, {'key', 'client', 'audit_root'})
        raw = auth.read_file(args.grant, owner=os.getuid(), private=True, limit=p.GRANT_MAX)
        header = issue(raw, cfg)
        sys.stdout.buffer.write(p.canonical(header) + b'\n')
        return 0
    except (p.Error, OSError, TypeError) as error:
        print('ci-storage-issuer: ' + (error.code if isinstance(error, p.Error) else 'configuration'), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
