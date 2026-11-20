"""Install scoped credentials and issue per-host control access."""
from __future__ import annotations

import fcntl
import ipaddress
import os
import secrets
import shutil
import ssl
import subprocess
import tempfile
import urllib.parse
from pathlib import Path

from parallel import deployment_files as files, transport


PAT_FILE = Path('credentials/pat/github-pat')
REGISTRATION_FILE = Path('credentials/runner/runner_token')
HOST_TOKENS = Path('credentials/hosts')
TLS_DIR = Path('credentials/tls')
TLS_FILES = ('ca.crt', 'server.crt', 'server.key')


def input_file(input_dir, relative, legacy):
    """Prefer the new path, including empty or invalid files explicitly placed there."""
    current = input_dir/relative
    previous = input_dir/legacy
    if os.path.lexists(current) or not os.path.lexists(previous):
        return current
    return previous


def pat_path(input_dir):
    return input_file(input_dir, PAT_FILE, 'credentials/github-pat')


def registration_path(input_dir):
    return input_file(input_dir, REGISTRATION_FILE, 'credentials/runner-registration-token')


def initialize_layout(input_dir):
    for name in ('credentials', PAT_FILE.parent, REGISTRATION_FILE.parent, TLS_DIR, HOST_TOKENS,
                 'credentials/slurm', 'credentials/adm_token'):
        files.private_directory(input_dir/name)
    for path in (pat_path(input_dir), registration_path(input_dir)):
        try:
            path.touch(mode=0o600, exist_ok=False)
        except FileExistsError:
            pass  # Never overwrite hand-filled files or shadow a legacy token with a blank.


def credential_sources(config):
    sources = {}
    if config.is_controller:
        sources['github-pat'] = pat_path(config.input_dir)
        for directory in (config.input_dir/TLS_DIR, config.input_dir/'tls'):
            if any(os.path.lexists(directory/name) for name in TLS_FILES):
                if not all((directory/name).is_file() for name in TLS_FILES):
                    raise ValueError(f'import TLS requires ca.crt, server.crt and server.key together in {directory}')
                sources.update({name: directory/name for name in TLS_FILES})
                break
    else:
        sources['host-token'] = input_file(config.input_dir, HOST_TOKENS/config.host_name, 'credentials/host-token')
        sources['ca.crt'] = input_file(config.input_dir, TLS_DIR/'ca.crt', 'tls/ca.crt')
    if config.host.get('slurm_runners', 0):
        for site in config.sites.values():
            for alias in site['accounts']:
                sources[f'slurm/keys/{alias}'] = config.input_dir/'credentials/slurm'/alias
        sources['slurm/ssh_config'] = config.input_dir/'slurm/ssh_config'
        sources['slurm/known_hosts'] = config.input_dir/'slurm/known_hosts'
    return sources


def copy_private(source, destination, *, immutable=False):
    data = files.read_private(source)
    if os.path.lexists(destination):
        existing = files.read_private(destination)
        if existing == data:
            return
        if immutable:
            raise ValueError(f'credential differs from installed value; explicit rotation required: {destination}')
    files.atomic_write(destination, data)


def validate_tls(config, directory, run=subprocess.run):
    directory = Path(directory)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(directory/'server.crt', directory/'server.key')
    hostname = urllib.parse.urlsplit(config.document['controller']['url']).hostname
    try:
        ipaddress.ip_address(hostname)
        option = '-verify_ip'
    except ValueError:
        option = '-verify_hostname'
    result = run(['openssl', 'verify', '-CAfile', str(directory/'ca.crt'), option, hostname,
                  str(directory/'server.crt')], capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError('server certificate must be valid for controller.url and signed by ca.crt')


def issue_tls(config, directory, run):
    hostname = urllib.parse.urlsplit(config.document['controller']['url']).hostname
    try:
        ipaddress.ip_address(hostname)
        alternative = 'IP:'+hostname
    except ValueError:
        alternative = 'DNS:'+hostname
    files.atomic_write(directory/'server.ext', 'subjectAltName='+alternative+'\nextendedKeyUsage=serverAuth\n')
    commands = [
        ['openssl', 'req', '-x509', '-newkey', 'rsa:3072', '-nodes', '-days', '3650', '-subj', '/CN=CI control CA',
         '-keyout', str(directory/'ca.key'), '-out', str(directory/'ca.crt'),
         '-addext', 'basicConstraints=critical,CA:TRUE'],
        ['openssl', 'req', '-new', '-newkey', 'rsa:3072', '-nodes', '-subj', '/CN=CI controller',
         '-keyout', str(directory/'server.key'), '-out', str(directory/'server.csr')],
        ['openssl', 'x509', '-req', '-in', str(directory/'server.csr'), '-CA', str(directory/'ca.crt'),
         '-CAkey', str(directory/'ca.key'), '-CAcreateserial', '-days', '365', '-out', str(directory/'server.crt'),
         '-extfile', str(directory/'server.ext')],
    ]
    for command in commands:
        if run(command, capture_output=True, timeout=60).returncode:
            raise ValueError('TLS certificate generation failed')
    for name in ('server.ext', 'server.csr', 'ca.srl'):
        (directory/name).unlink()
    for name in (*TLS_FILES, 'ca.key'):
        (directory/name).chmod(0o600)


def install_issued_tls(config, run):
    material = config.input_dir/TLS_DIR/'issued'
    files.private_directory(material.parent)
    if not os.path.lexists(material):
        temporary = Path(tempfile.mkdtemp(prefix='.tls-', dir=material.parent))
        try:
            legacy = config.private/'tls-material'
            if os.path.lexists(legacy):
                for name in (*TLS_FILES, 'ca.key'):
                    copy_private(legacy/name, temporary/name)
            elif any(os.path.lexists(config.private/name) for name in TLS_FILES):
                raise ValueError('restore the controller TLS inputs for the installed identity before applying')
            else:
                issue_tls(config, temporary, run)
            validate_tls(config, temporary, run)
            os.rename(temporary, material)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    files.private_directory(material)
    for name in TLS_FILES:
        copy_private(material/name, config.private/name, immutable=True)


def install_controller_identities(config, sources, run):
    # Two runtime roots may be initialized from the same controller input.
    root = config.input_dir/'credentials'
    files.private_directory(root)
    descriptor = os.open(root, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if 'server.crt' not in sources:
            install_issued_tls(config, run)
        validate_tls(config, config.private, run)
        files.private_directory(config.input_dir/HOST_TOKENS)
        for host_name, host in config.document['hosts'].items():
            if 'executor' not in host['roles']:
                continue
            token = config.input_dir/HOST_TOKENS/host_name
            installed = config.private/'hosts'/host_name
            if not os.path.lexists(token):
                if os.path.lexists(installed):
                    copy_private(installed, token)
                else:
                    files.atomic_write(token, secrets.token_urlsafe(32)+'\n')
            transport.secret(str(token))
            copy_private(token, installed, immutable=True)
        if 'executor' in config.host['roles']:
            copy_private(config.private/'hosts'/config.host_name, config.private/'host-token', immutable=True)
    finally:
        os.close(descriptor)


def install(config, run=subprocess.run):
    files.private_directory(config.private)
    sources = credential_sources(config)
    for name, source in sources.items():
        destination = config.etc/name if name.startswith('slurm/') else config.private/name
        if name == 'slurm/ssh_config':
            text = files.read_private(source).decode()
            # Explicit config avoids inheriting unrelated aliases or keys from ~/.ssh.
            text = f'Host *\n  UserKnownHostsFile {config.etc}/slurm/known_hosts\n  StrictHostKeyChecking yes\n' + text
            files.atomic_write(destination, text)
        else:
            copy_private(source, destination, immutable=name in ('host-token', 'ca.crt', 'server.crt', 'server.key'))
    if config.is_controller:
        install_controller_identities(config, sources, run)
