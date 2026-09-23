"""One policy for all application SSH connections, including ProxyJump hops."""
from contextlib import asynccontextmanager, AsyncExitStack
from urllib.parse import urlsplit
import asyncio
import asyncssh
from asyncssh.connection import _select_host_key_algs
from asyncssh.public_key import get_default_public_key_algs
from . import db, host_keys, crypto, credentials


CONNECT_TIMEOUT = 20
CLOSE_TIMEOUT = 2


def auth_params(host):
    method = host.get('auth_method', 'key')
    params = dict(agent_forwarding=False, host_based_auth=False,
                  gss_auth=False, gss_kex=False, gss_delegate_creds=False)
    if method == 'password':
        token = host.get('password_enc')
        password = crypto.decrypt_host_password(token, host) if token is not None else None
        if not password:
            raise OSError("Kein SSH-Passwort gespeichert. Bitte in der Konfiguration eingeben.")
        params.update(password=password, client_keys=None, client_certs=[],
                      agent_path=None, pkcs11_provider=None, public_key_auth=False,
                      password_auth=True, kbdint_auth=True,
                      preferred_auth=['password', 'keyboard-interactive'])
    elif method == 'key':
        params.update(password=None, password_auth=False, kbdint_auth=False,
                      public_key_auth=True, preferred_auth=['publickey'])
        if host.get('key_path'):
            # CertificateFile only attaches certificates matching this identity.
            params.update(client_keys=[host['key_path']],
                          agent_path=None, pkcs11_provider=None)
    else:
        raise OSError("Unbekannte SSH-Authentifizierungsmethode.")
    return params


def options_for(host, *, inspect=False, jump=False, config=()):
    params = auth_params(host) if not inspect else dict(
        client_keys=None, client_certs=[], agent_path=None, pkcs11_provider=None,
        password=None, public_key_auth=False, password_auth=False, kbdint_auth=False)
    params.update(agent_forwarding=False, x11_forwarding=False, request_pty=False,
                  connect_timeout=CONNECT_TIMEOUT, login_timeout=CONNECT_TIMEOUT,
                  host_based_auth=False, gss_auth=False,
                  gss_kex=False, gss_delegate_creds=False,
                  # Empty trust set invokes our mandatory exact-pin validator.
                  known_hosts=asyncssh.import_known_hosts(''),
                  x509_trusted_certs=None, x509_trusted_cert_paths=[],
                  client_host_keysign=False, client_host_keys=None)
    if jump:
        # ProxyJump identities belong to SSH config, not to managed DB rows.
        address, user, port = host['primary_ip'], host.get('user', ()), host.get('port', ())
    else:
        address, user, port = credentials.normalize_target(host)
    options = asyncssh.SSHClientConnectionOptions(
        config=config, host=address,
        port=port, username=user,
        **params)
    # Resolve OpenSSH algorithm modifiers before removing unsupported host
    # certificates. This does not alter client identities/CertificateFile.
    algorithms = _select_host_key_algs(
        (), options.config.get('HostKeyAlgorithms', ()), get_default_public_key_algs())
    raw_algorithms = [alg.decode('ascii') for alg in algorithms
                      if b'-cert-' not in alg and not alg.startswith(b'x509-')]
    if not raw_algorithms:
        raise ValueError('HostKeyAlgorithms erlaubt keinen unterstützten öffentlichen Host-Key.')
    # Reconstruct rather than assigning attributes: connect() copies options
    # using their original keyword arguments.
    return asyncssh.SSHClientConnectionOptions(options, server_host_key_algs=raw_algorithms)


def _jump_host(spec):
    parsed = urlsplit('ssh://' + spec)
    if not parsed.hostname or parsed.path or parsed.query or parsed.fragment:
        raise OSError(f"Ungültiger ProxyJump: {spec}")
    return dict(primary_ip=parsed.hostname, port=parsed.port or (),
                user=parsed.username or (), auth_method='key')


@asynccontextmanager
async def _open(options, requested, *, inspect=False, chain=(), tunnel=None):
    identity = (options.host, options.port)
    if identity in chain or len(chain) >= 8:
        raise OSError("Zyklische oder zu lange ProxyJump-Konfiguration.")
    async with AsyncExitStack() as stack:
        if tunnel is None and isinstance(options.tunnel, str):
            for spec in options.tunnel.split(','):
                jump_host = _jump_host(spec)
                jump_options = options_for(jump_host, jump=True)
                tunnel = await stack.enter_async_context(_open(
                    jump_options, spec, chain=chain + (identity,), tunnel=tunnel))
        validator = host_keys.Validator(options.host_key_alias or options.host,
                                        options.port, requested, inspect=inspect,
                                        address=options.host)
        conn = None
        try:
            conn = await asyncssh.connect(options.host, port=options.port,
                                          username=options.username, options=options, tunnel=tunnel,
                                          client_factory=lambda: validator)
            yield conn
        except asyncssh.HostKeyNotVerifiable as exc:
            if validator.observation is not None:
                raise host_keys.ReviewRequired(validator.observation) from exc
            raise OSError("Serveridentität konnte nicht geprüft werden; Verbindung blockiert.") from exc
        finally:
            if conn is not None:
                conn.close()
                try:
                    await asyncio.wait_for(conn.wait_closed(), CLOSE_TIMEOUT)
                except TimeoutError:
                    conn.abort()
                except asyncio.CancelledError:
                    conn.abort()
                    raise



@asynccontextmanager
async def connect_host(host):
    host = db.get_connection_context(host)
    try:
        options = options_for(host)
    except ValueError as exc:
        raise OSError(f"SSH-Konfiguration ungültig: {exc}") from exc
    async with AsyncExitStack() as stack:
        async with asyncio.timeout(CONNECT_TIMEOUT):
            conn = await stack.enter_async_context(_open(options, host['primary_ip']))
        yield conn


async def inspect_host(host):
    """Return the first untrusted hop, or the target key, without target login."""
    options = options_for(host, inspect=True)
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT):
            async with _open(options, host['primary_ip'], inspect=True):
                raise OSError("Server hat keinen prüfbaren öffentlichen Host-Key angeboten.")
    except host_keys.ReviewRequired as exc:
        return exc.observation
