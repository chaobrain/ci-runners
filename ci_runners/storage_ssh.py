"""Deadline-bounded OpenSSH/SFTP transport. Credentials never enter references."""
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from ci_runners import storage


class SSHStore:
    def __init__(self, config, store, limits):
        self.config, self.store, self.limits = config, store, limits

    def options(self):
        return ['-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=yes', '-o', 'NumberOfPasswordPrompts=0',
                '-o', f'ConnectTimeout={self.limits["connect_seconds"]}',
                '-o', f'UserKnownHostsFile={self.config["known_hosts_file"]}', '-i', self.config['identity_file']]

    def run(self, argv, data, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise storage.Error('timeout')
        try:
            # File-backed output bounds parent memory even if a remote peer is noisy.
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                                           start_new_session=os.name == 'posix',
                                           env={k: v for k, v in os.environ.items() if k not in ('SSH_AUTH_SOCK',)})
                try:
                    process.communicate(data, timeout=remaining)
                except BaseException:
                    if os.name == 'posix':
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    elif process.poll() is None:
                        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                    process.wait()
                    raise
                stdout.seek(0)
                result = stdout.read(self.limits['manifest_bytes'] * 2 + 1)
                if len(result) > self.limits['manifest_bytes'] * 2:
                    raise storage.Error('limit')
                if process.returncode:
                    try:
                        code = json.loads(result)['error']
                    except (ValueError, KeyError, TypeError):
                        code = 'transport'
                        stderr.seek(0)
                        private_error = stderr.read(8192).lower()
                        if b'host key verification failed' in private_error or b'remote host identification has changed' in private_error:
                            code = 'host-key'
                        elif b'permission denied (publickey' in private_error:
                            code = 'authentication'
                    if code not in ('configuration', 'identity', 'corrupt', 'missing', 'limit', 'conflict', 'permission',
                                    'host-key', 'authentication', 'capacity'):
                        code = 'transport'
                    raise storage.Error(code)
                return result
        except subprocess.TimeoutExpired as exc:
            raise storage.Error('timeout') from exc
        except OSError as exc:
            raise storage.Error('transport') from exc

    def rpc(self, operation, deadline, **params):
        command = shlex.join([self.config['helper'], '--root', self.config['root'], '--store', self.store])
        raw = self.run(['ssh', *self.options(), self.config['ssh_alias'], command],
                       storage.dumps({'operation': operation, 'limits': self.limits, **params}), deadline)
        try:
            return json.loads(raw)['result']
        except (ValueError, KeyError, TypeError) as exc:
            raise storage.Error('corrupt') from exc

    def transfer(self, commands, deadline):
        return self.run(['sftp', *self.options(), '-q', '-b', '-', self.config['ssh_alias']],
                        ('\n'.join(commands) + '\n').encode(), deadline)

    def attempt(self, operation, deadline=None):
        deadline = deadline if deadline is not None else time.monotonic() + self.limits['operation_seconds']
        for n in range(self.limits['attempts']):
            try:
                return operation(deadline)
            except storage.Error as exc:
                if exc.code not in ('transport', 'timeout') or n + 1 == self.limits['attempts']:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise storage.Error('timeout') from exc
                time.sleep(min(2 ** n, remaining))

    def publish_bundle(self, ident, source, retention='diagnostic'):
        deadline = time.monotonic() + self.limits['operation_seconds']
        ident = storage.identity(ident)
        with tempfile.TemporaryDirectory() as directory:
            payload = Path(directory)/'bundle.zip'
            manifest = storage.pack(source, payload, ident, self.limits, retention, deadline)
            path = Path(directory)/'manifest.json'
            path.write_bytes(storage.dumps(manifest))

            def publish(deadline):
                stage = self.rpc('begin', deadline)['stage']
                if not isinstance(stage, str) or not re.fullmatch('[0-9a-f]{32}', stage):
                    raise storage.Error('identity')
                remote = self.config['root'] + '/.staging/' + stage
                self.transfer([f'put "{payload.as_posix()}" "{remote}/bundle.zip"',
                               f'put "{path.as_posix()}" "{remote}/manifest.json"'], deadline)
                got = self.rpc('commit', deadline, stage=stage, identity=ident)
                ref = storage.reference(got['reference'], ident, self.store)
                if got['manifest']['sha256'] != manifest['sha256']:
                    raise storage.Error('corrupt')
                return ref
            return self.attempt(publish, deadline)

    def read_bundle(self, ref, expected, destination):
        storage.reference(ref, expected, self.store)

        def read(deadline):
            got = self.rpc('describe', deadline, identity=expected, manifest_sha256=ref['manifest_sha256'])
            storage.reference(got['reference'], expected, self.store)
            with tempfile.TemporaryDirectory() as directory:
                payload = Path(directory)/'bundle.zip'
                remote = str(storage.object_path(expected))
                self.transfer([f'get "{self.config["root"]}/{remote}/bundle.zip" "{payload.as_posix()}"'], deadline)
                return storage.unpack(storage.dumps(got['manifest']), payload, expected, destination,
                                      self.limits, ref['manifest_sha256'], deadline)
        return self.attempt(read)

    def describe(self, ident):
        return self.attempt(lambda deadline: self.rpc('describe', deadline, identity=ident))

    def list_completed(self, run, after=''):
        return self.attempt(lambda deadline: self.rpc('list', deadline, run=run, after=after))

    def cleanup(self, run, now, dry_run=True):
        return self.attempt(lambda deadline: self.rpc('cleanup', deadline, run=run, now=now, dry_run=dry_run))
