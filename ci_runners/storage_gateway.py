"""Forced-SSH stdio entrypoint. No installation, CI cutover, deletion or issuer key."""
from __future__ import annotations

import argparse
import os
import sqlite3
import signal
import sys

from ci_runners import storage_auth as auth, storage_protocol as p
from ci_runners.storage_store import Store


def serve(input_fd, output_fd, *, store, client, allowed_signers, deadline=None):
    deadline = deadline or p.Deadline()
    try:
        header, manifest_size, payload_size = p.read_head(input_fd, deadline)
        grant = auth.authorize(header, client=client, allowed_signers=allowed_signers, deadline=deadline)
        if grant['operation'] == 'publish':
            p.require(manifest_size > 0 and payload_size == grant['size'], 'size')
        else:
            p.require(manifest_size == payload_size == 0, 'framing')
        with store.slot():
            store.admit(grant, deadline=deadline)
            manifest, payload = p.read_body(input_fd, deadline, manifest_size, payload_size)
            result, out_manifest, out_payload = store.handle(grant, manifest, payload, deadline=deadline)
            p.grant(p.canonical(grant), client=client)
            p.write_frame(output_fd, deadline, {'schema': 1, 'status': 'ok', 'result': result}, out_manifest, out_payload)
        return 0
    except p.Error as error:
        code = error.code
    except (OSError, ValueError, sqlite3.Error):
        code = 'storage'
    # A refusal never writes payload/paths. Use remaining total time, not a fresh deadline.
    try:
        p.write_frame(output_fd, deadline, {'schema': 1, 'status': 'error', 'code': code})
    except (OSError, p.Error):
        pass
    return 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    args = parser.parse_args(argv)
    try:
        p.require(os.environ.get('SSH_ORIGINAL_COMMAND') == p.AUDIENCE, 'command')
        cfg = auth.config(args.config, {'root', 'authorized_keys', 'allowed_signers'})
        client = auth.client_fingerprint(cfg['authorized_keys'])
        trust = auth.read_file(cfg['allowed_signers'], limit=16384)
        # Config contains no adjustable limits or production repository opt-in.
        store = Store(cfg['root'])
        os.set_blocking(0, False)
        os.set_blocking(1, False)
        def expired(_signum, _frame):
            raise p.Error('timeout')
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, 300)
        try:
            return serve(0, 1, store=store, client=client, allowed_signers=trust)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
    except (p.Error, OSError, TypeError) as error:
        print('ci-storage-gateway: ' + (error.code if isinstance(error, p.Error) else 'configuration'), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
