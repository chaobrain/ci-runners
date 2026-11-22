"""Bounded JSON RPC over local Unix sockets or authenticated HTTPS."""
from __future__ import annotations

import hmac
import http.client
import http.server
import json
import os
import socket
import socketserver
import ssl
import stat
import struct
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from parallel import layout, organization, protocol

LIMIT = 2 << 20


def settings(etc: str) -> dict:
    with open(layout.settings_path(etc), 'rb') as f:
        cfg = tomllib.load(f)
    if (cfg.get('schema') != 1 or set(cfg) - {'schema', 'plan', 'executor', 'hosts', 'repository_members', 'organization'}
            or type(cfg.get('repository_members', False)) is not bool):
        raise protocol.Error('invalid central service configuration')
    if 'organization' in cfg:
        organization.validate_settings(cfg['organization'], controller='plan' in cfg)
    return cfg


def secret(path: str) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd) as f:
        s = os.fstat(f.fileno())
        if not stat.S_ISREG(s.st_mode) or s.st_mode & 0o077:
            raise protocol.Error('service credential must be a private regular file')
        token = f.read(8193).strip()
    if not token or len(token) > 8192 or any(c.isspace() for c in token):
        raise protocol.Error('invalid service credential')
    return token


def device_tokens(path: str | Path) -> dict[str, Path]:
    """Read the scheduler's device admission file. Only token paths are returned."""
    path = Path(path)
    try:
        root = path.parent.resolve()
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(descriptor, 'rb') as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise protocol.Error('device admission file must be owned by the scheduler account')
            data = file.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise protocol.Error('device admission file is too large')
        doc = tomllib.loads(data.decode())
    except (OSError, ValueError) as error:
        raise protocol.Error('cannot read device admission file') from error
    devices = doc.get('devices')
    if (set(doc) - {'schema', 'devices'} or type(doc.get('schema', 1)) is not int
            or doc.get('schema', 1) != 1 or not isinstance(devices, dict)):
        raise protocol.Error('invalid device admission file')
    tokens = {}
    for name, entry in devices.items():
        if name not in protocol.DEVICES - {'slurm'} or not isinstance(entry, dict) or set(entry) != {'token_file'}:
            raise protocol.Error('invalid device admission entry')
        relative = entry['token_file']
        if not protocol.relative(relative):
            raise protocol.Error('device token path must be relative to its admission file')
        candidate = root / relative
        if not candidate.parent.resolve().is_relative_to(root):
            raise protocol.Error('device token path escapes its admission directory')
        tokens[name] = candidate
    if len(set(tokens.values())) != len(tokens):
        raise protocol.Error('each device needs a separate credential file')
    return tokens


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=30):
        super().__init__('localhost', timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise protocol.Error('control endpoint must not redirect credentials')


class Client:
    def __init__(self, *, path='', url='', token_file='', ca_file=None, device=''):
        if bool(path) == bool(url):
            raise protocol.Error('choose one local socket or HTTPS endpoint')
        if url:
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise protocol.Error('executor control endpoint must use HTTPS')
        if device and (not url or device not in protocol.DEVICES - {'slurm'}):
            raise protocol.Error('invalid HTTPS execution device')
        self.path, self.url, self.token_file = path, url.rstrip('/'), token_file
        self.device = device
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca_file)))

    def call(self, method: str, **params):
        data = protocol.dumps({'method': method, 'params': params}).encode()
        if len(data) > LIMIT:
            raise protocol.Error('control request too large')
        if self.path:
            # First preparation may read a manifest, recursive files and upstream refs from GitHub.
            conn = UnixConnection(self.path, timeout=300) if method == 'prepare' else UnixConnection(self.path)
            try:
                conn.request('POST', '/rpc', data, {'Content-Type': 'application/json'})
                reply = conn.getresponse()
                code, body = reply.status, reply.read(LIMIT+1)
            finally:
                conn.close()
        else:
            headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer '+secret(self.token_file)}
            if self.device:
                headers['X-CI-Device'] = self.device
            req = urllib.request.Request(self.url+'/rpc', data=data, method='POST', headers=headers)
            try:
                with self.opener.open(req, timeout=30) as reply:
                    code, body = reply.status, reply.read(LIMIT+1)
            except urllib.error.HTTPError as e:
                code, body = e.code, e.read(LIMIT+1)
        if len(body) > LIMIT:
            raise protocol.Error('control response too large')
        obj = json.loads(body)
        if code != 200 or not isinstance(obj, dict) or 'result' not in obj:
            raise protocol.Error(obj.get('error', 'control request refused') if isinstance(obj, dict) else 'invalid response')
        return obj['result']


def handler(application, authenticate):
    class Handler(http.server.BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def log_message(self, *_args):
            pass

        def do_POST(self):
            code = 200
            try:
                identity = authenticate(self)
                length = int(self.headers.get('Content-Length', '-1'))
                if self.path != '/rpc' or not 0 <= length <= LIMIT:
                    raise protocol.Error('invalid control request')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict) or set(data) != {'method', 'params'} or not isinstance(data['params'], dict):
                    raise protocol.Error('invalid control envelope')
                body = {'result': application(data['method'], data['params'], identity)}
            except (ValueError, KeyError, TypeError) as e:
                code, body = 400, {'error': str(e) if isinstance(e, protocol.Error) else 'malformed control request'}
            except Exception:
                code, body = 503, {'error': 'control service unavailable'}
            raw = protocol.dumps(body).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    return Handler


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def unix_server(path: str, application, *, uids: list[int] | None = None):
    allowed = set(uids if uids is not None else [os.getuid()])
    if any(type(u) is not int or u < 0 for u in allowed):
        raise protocol.Error('invalid local client uid')
    def authenticate(request):
        _, uid, _ = struct.unpack('3i', request.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid not in allowed:
            raise protocol.Error('local client is not authorized')
        return 'local'
    Path(path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.path.lexists(path):
        if not stat.S_ISSOCK(os.lstat(path).st_mode):
            raise protocol.Error('control socket path is not a socket')
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(path)
        except ConnectionRefusedError:
            os.unlink(path)
        else:
            raise protocol.Error('control service is already running')
        finally:
            probe.close()
    server = UnixServer(path, handler(application, authenticate))
    os.chmod(path, 0o660)
    return server


def https_server(cfg: dict, hosts: dict, application):
    def authenticate(request):
        supplied = request.headers.get('Authorization', '')
        if not supplied.isascii():
            raise protocol.Error('execution host is not authorized')
        if 'device_admission_file' in cfg:
            name = request.headers.get('X-CI-Device')
            paths = device_tokens(cfg['device_admission_file'])
            path = paths.get(name)
            try:
                credential = secret(path) if path is not None else ''
                authenticated = bool(credential) and hmac.compare_digest(supplied, 'Bearer '+credential)
                if authenticated:
                    # The same secret under two IDs would let either host claim the other ID.
                    for other in paths.values():
                        if other == path:
                            continue
                        try:
                            if hmac.compare_digest(credential, secret(other)):
                                authenticated = False
                                break
                        except (OSError, ValueError):
                            continue
            except (OSError, ValueError):
                authenticated = False
            if authenticated:
                return name
            raise protocol.Error('execution host is not authorized')
        for name, spec in hosts.items():
            if hmac.compare_digest(supplied, 'Bearer '+secret(spec['token_file'])):
                return name
        raise protocol.Error('execution host is not authorized')
    server = http.server.ThreadingHTTPServer((cfg['bind'], int(cfg['port'])), handler(application, authenticate))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cfg['tls_certificate'], cfg['tls_key'])
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    return server
