"""Bounded SSH synthetic probe client. Never installs a key or changes a CI backend."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

from ci_runners import storage_auth as auth, storage_protocol as p


class Client:
    def __init__(self, cfg):
        p.require(isinstance(cfg['host'], str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}', cfg['host']), 'configuration')
        p.require(isinstance(cfg['user'], str) and re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', cfg['user']), 'configuration')
        auth.protected(cfg['key'], owner=os.getuid(), private=True)
        auth.read_file(cfg['known_hosts'], limit=16384)
        self.client = cfg['client']
        self.command = ['/usr/bin/ssh', '-F', '/dev/null', '-T', '-i', str(cfg['key']),
                        '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                        '-o', 'UpdateHostKeys=no', '-o', 'ForwardAgent=no', '-o', 'IdentityAgent=none',
                        '-o', 'ControlMaster=no', '-o', 'ControlPath=none', '-o', 'ProxyCommand=none',
                        '-o', 'PermitLocalCommand=no', '-o', 'PasswordAuthentication=no',
                        '-o', 'KbdInteractiveAuthentication=no', '-o', 'PreferredAuthentications=publickey',
                        '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'UserKnownHostsFile=' + str(cfg['known_hosts']),
                        '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=2',
                        f"{cfg['user']}@{cfg['host']}", p.AUDIENCE]

    def _exchange(self, header, manifest, payload, deadline):
        process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env={'PATH': '/usr/bin:/bin'})
        os.set_blocking(process.stdin.fileno(), False)
        os.set_blocking(process.stdout.fileno(), False)
        failures = []
        def writer():
            try:
                p.write_frame(process.stdin.fileno(), deadline, header, manifest, payload)
            except (OSError, p.Error, ValueError) as error:
                failures.append(error)
            finally:
                process.stdin.close()
        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        try:
            response, m, n = p.read_head(process.stdout.fileno(), deadline)
            found_manifest, found_payload = p.read_body(process.stdout.fileno(), deadline, m, n)
            p.require(type(response.get('schema')) is int and response['schema'] == 1, 'framing')
            if response.get('status') == 'error':
                p.require(set(response) == {'schema', 'status', 'code'} and not m and not n
                          and isinstance(response['code'], str)
                          and response['code'] in {'format', 'identity', 'authorization', 'signature', 'expired',
                          'size', 'digest', 'framing', 'truncated', 'trailing', 'incomplete', 'busy', 'timeout',
                          'permissions', 'corrupt', 'quota', 'space', 'conflict', 'replay', 'missing', 'storage'}, 'framing')
                raise p.Error(response['code'])
            p.require(set(response) == {'schema', 'status', 'result'} and response['status'] == 'ok', 'framing')
            status = process.wait(timeout=deadline.remaining())
            thread.join(timeout=deadline.remaining())
            p.require(status == 0 and not thread.is_alive() and not failures, 'transport')
            return response['result'], found_manifest, found_payload
        except (OSError, subprocess.TimeoutExpired):
            raise p.Error('transport') from None
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            thread.join(timeout=1)
            process.stdout.close()

    def call(self, header, manifest=b'', payload=b'', *, deadline=None):
        deadline = deadline or p.Deadline()
        raw, _signature = p.request(header)
        authorization = p.grant(raw, client=self.client)
        p.require(header['operation'] == authorization['operation'], 'authorization')
        if authorization['operation'] == 'publish':
            p.validate_manifest(manifest, payload, authorization)
        else:
            p.require(not manifest and not payload, 'framing')
        for attempt in range(3):
            # Expiry/deadline is rechecked on every transport attempt.
            p.grant(raw, client=self.client)
            deadline.remaining()
            try:
                result, found_manifest, found_payload = self._exchange(header, manifest, payload, deadline)
                self._validate_response(result, found_manifest, found_payload, authorization)
                return result, found_manifest, found_payload
            except p.Error as error:
                if error.code not in ('transport', 'truncated', 'busy', 'timeout') or attempt == 2:
                    raise
                time.sleep(min(.1 * (attempt + 1), deadline.remaining()))

    @staticmethod
    def _validate_response(result, manifest, payload, authorization):
        p.require(type(result) is dict, 'framing')
        if authorization['operation'] == 'list':
            p.require(set(result) == {'objects'} and type(result['objects']) is list
                      and len(result['objects']) <= 16 and not manifest and not payload, 'framing')
            seen = set()
            for item in result['objects']:
                p.require(type(item) is dict and set(item) == {'identity', 'manifest_sha256', 'payload_sha256', 'size'}, 'framing')
                p.scope(item['identity'])
                p.require(all(item['identity'][k] == v for k, v in authorization['identity'].items()), 'identity')
                key = p.digest(p.canonical(item['identity']))
                p.require(key not in seen and p.is_digest(item['manifest_sha256'])
                          and p.is_digest(item['payload_sha256']) and p.integer(item['size'], 1, p.PAYLOAD_MAX), 'framing')
                seen.add(key)
        else:
            p.require(set(result) == {'identity', 'manifest_sha256'}, 'identity')
            p.scope(result['identity'])
            p.require(result['identity'] == authorization['identity'], 'identity')
            p.require(result['manifest_sha256'] == authorization['manifest_sha256'], 'digest')
            if authorization['operation'] == 'read':
                p.validate_manifest(manifest, payload, authorization)
            else:
                p.require(not manifest and not payload, 'framing')


def save_result(target, result, manifest, payload):
    """Create a fresh private result; remove only this attempt's files on refusal."""
    target = Path(target)
    auth.protected(target.parent, owner=os.getuid(), private=True, directory=True)
    target.mkdir(mode=0o700)
    try:
        fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except BaseException:
        os.rmdir(target)
        raise
    created = []
    try:
        # result.json is the local completion record and is written last.
        for name, raw in [('manifest.json', manifest), ('payload', payload), ('result.json', p.canonical(result))]:
            output_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            created.append(name)
            with os.fdopen(output_fd, 'wb') as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
        os.fsync(fd)
    except BaseException:
        for name in created:
            os.unlink(name, dir_fd=fd)
        os.rmdir(target)
        raise
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--request', required=True)
    parser.add_argument('--manifest')
    parser.add_argument('--payload')
    parser.add_argument('--output', required=True, help='new private directory for response files')
    args = parser.parse_args(argv)
    try:
        cfg = auth.config(args.config, {'host', 'user', 'key', 'known_hosts', 'client'})
        header = p.parse(auth.read_file(args.request, owner=os.getuid(), private=True, limit=p.HEADER_MAX))
        manifest = auth.read_file(args.manifest, owner=os.getuid(), private=True) if args.manifest else b''
        payload = auth.read_file(args.payload, owner=os.getuid(), private=True, limit=p.PAYLOAD_MAX) if args.payload else b''
        target = Path(args.output)
        auth.protected(target.parent, owner=os.getuid(), private=True, directory=True)
        p.require(not os.path.lexists(target), 'configuration')
        result, found_manifest, found_payload = Client(cfg).call(header, manifest, payload)
        save_result(target, result, found_manifest, found_payload)
        return 0
    except (p.Error, OSError, TypeError) as error:
        print('ci-storage-probe: ' + (error.code if isinstance(error, p.Error) else 'configuration'), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
