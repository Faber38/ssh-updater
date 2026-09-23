"""Versioned SSH credentials. No I/O and no implicit legacy migration."""
from dataclasses import dataclass
import json
import re

from cryptography.fernet import InvalidToken
from asyncssh.saslprep import saslprep


PREFIX = b'sshupdater-credential:'
V2_PREFIX = PREFIX + b'v2:'
MAGIC = b'\xffSSHUPD-CRED\x00'


class CredentialError(OSError):
    pass


class LegacyCredentialRequired(CredentialError):
    def __init__(self):
        super().__init__('Passwort im älteren Speicherformat: bitte für diesen Host erneut eingeben.')


@dataclass(frozen=True)
class Identity:
    host_id: int
    primary_ip: str
    user: str
    port: int


def _validate_username(user):
    # Use the very same preparation as the pinned AsyncSSH implementation, but
    # reject transformations instead of changing an authenticated identity.
    try:
        user.encode('utf-8')
        if saslprep(user) != user:
            raise ValueError('changed identity')
    except (ValueError, UnicodeError):
        raise CredentialError('SSH-Benutzername ist nicht unverändert übertragbar.') from None


def _validate_secret(secret):
    if type(secret) is not str or not secret:
        raise CredentialError('Ein nicht leeres SSH-Passwort ist erforderlich.')
    try:
        secret.encode('utf-8')
    except UnicodeError:
        raise CredentialError('SSH-Passwort ist nicht UTF-8-kodierbar.') from None


def normalize_target(host):
    """Keep stored host/user spelling, including aliases; never resolve DNS.

    Only absent/empty user and absent port receive the historical defaults.
    Zero, booleans, floats and malformed ports must never silently become 22.
    """
    address = host.get('primary_ip')
    if address is None:
        address = ''
    user = host.get('user')
    if user is None or user == '':
        user = 'root'
    port = host.get('port')
    if port is None:
        port = 22
    if type(port) is str and re.fullmatch(r'[0-9]+', port):
        try:
            port = int(port)
        except ValueError:
            raise CredentialError('Ungültiger SSH-Port.') from None
    if type(address) is not str or type(user) is not str:
        raise CredentialError('Ungültige SSH-Hostidentität.')
    if type(port) is not int or not 1 <= port <= 65535:
        raise CredentialError('Ungültiger SSH-Port.')
    _validate_username(user)
    return address, user, port


def identity(host):
    host_id = host.get('id')
    if type(host_id) is not int or host_id <= 0:
        raise CredentialError('Ungültige Host-ID.')
    return Identity(host_id, *normalize_target(host))


def version(token):
    if type(token) is not bytes:
        raise CredentialError('Ungültiges Credential-Format.')
    if token.startswith(V2_PREFIX):
        return 2
    if token.startswith(PREFIX):
        raise CredentialError('Unbekannte Credential-Version.')
    # Legacy is an actual Fernet token, not an arbitrary unrecognized envelope.
    if re.fullmatch(rb'[A-Za-z0-9_-]+={0,2}', token):
        return 1
    raise CredentialError('Ungültiges Credential-Format.')


def encrypt(fernet, secret, host):
    bound = identity(host)
    _validate_secret(secret)
    payload = dict(version=2, purpose='ssh-password', host_id=bound.host_id,
                   primary_ip=bound.primary_ip, user=bound.user, port=bound.port,
                   secret=secret)
    try:
        plain = MAGIC + json.dumps(payload, ensure_ascii=True, separators=(',', ':')).encode('utf-8')
        return V2_PREFIX + fernet.encrypt(plain)
    except (ValueError, UnicodeError):
        raise CredentialError('SSH-Passwort konnte nicht gespeichert werden.') from None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate field')
        result[key] = value
    return result


def _decrypt_legacy(fernet, token, validate_only):
    # V1 is reachable only through the format dispatcher, never on a V2 error.
    # A V2 payload stripped of its prefix fails this UTF-8 check as well.
    fernet.decrypt(token).decode('utf-8')
    if validate_only:
        return None
    raise LegacyCredentialRequired()


def decrypt(fernet, token, host, *, validate_legacy=False):
    """Legacy is readable only for vault validation, never returned for login."""
    fmt = version(token)
    try:
        if fmt == 1:
            return _decrypt_legacy(fernet, token, validate_legacy)
        plain = fernet.decrypt(token[len(V2_PREFIX):])
        if not plain.startswith(MAGIC):
            raise ValueError('magic')
        payload = json.loads(plain[len(MAGIC):].decode('utf-8'), object_pairs_hook=_unique_object)
        fields = {'version': int, 'purpose': str, 'host_id': int,
                  'primary_ip': str, 'user': str, 'port': int, 'secret': str}
        if type(payload) is not dict or set(payload) != set(fields):
            raise ValueError('fields')
        if any(type(payload[k]) is not kind for k, kind in fields.items()):
            raise ValueError('types')
        if payload['version'] != 2 or payload['purpose'] != 'ssh-password' or not payload['secret']:
            raise ValueError('purpose/version/secret')
        _validate_username(payload['user'])
        _validate_secret(payload['secret'])
        actual = Identity(payload['host_id'], payload['primary_ip'], payload['user'], payload['port'])
        if actual != identity(host):
            raise CredentialError('SSH-Credential passt nicht zur Hostidentität.')
        return payload['secret']
    except (InvalidToken, ValueError, TypeError, UnicodeError, RecursionError):
        # Never include parser errors, plaintext, or chained payload exceptions.
        raise CredentialError('SSH-Credential ist beschädigt oder hat ein ungültiges Format.') from None
