"""Exact host/port pins in an application-owned OpenSSH-format file."""
from dataclasses import dataclass
from threading import RLock
from contextlib import contextmanager
import os
import asyncssh
from . import settings, storage, crypto

_LOCK = RLock()


def endpoint(host: str, port: int) -> str:
    if not host or any(c.isspace() or c in ',*?!|[]' for c in host):
        raise OSError("Ungültiger Hostname für die Serveridentitätsprüfung.")
    return f"[{host.lower()}]:{port}"



class TruststoreIntegrityError(OSError):
    """Der Vertrauensspeicher passt nicht zu seinem Integritätssiegel."""


class TruststoreInitializationRequired(TruststoreIntegrityError):
    """Es existiert noch kein vollständig authentisierter Truststore."""


class TruststoreMigrationRequired(TruststoreIntegrityError):
    """Ein bestehender Truststore besitzt noch kein verifizierbares Siegel."""


def _write_truststore_mac(raw: bytes):
    if not crypto.is_unlocked():
        raise TruststoreIntegrityError("Vault ist gesperrt; Vertrauensspeicher bleibt blockiert.")
    storage.write_private(settings.KNOWN_HOSTS_MAC, crypto.truststore_mac(raw))


def _verify_truststore_integrity(raw: bytes):
    """Das known_hosts-Siegel gegen den entsperrten Vault prüfen.

    Fehlende oder falsche Metadaten blockieren. Ein bestehender Truststore darf
    nie seine eigene Authentizität begründen.
    """
    try:
        tag = storage.read_private(settings.KNOWN_HOSTS_MAC)
    except FileNotFoundError:
        tag = None
    if not crypto.is_unlocked():
        raise TruststoreIntegrityError("Vault ist gesperrt; Vertrauensspeicher bleibt blockiert.")
    if tag is None:
        raise TruststoreMigrationRequired(
            "Integritätssiegel des Vertrauensspeichers fehlt. Vorhandene Server-Keys "
            "werden nicht automatisch übernommen und müssen erneut bestätigt werden.")
    if not crypto.verify_truststore(raw, tag):
        raise TruststoreIntegrityError(
            "Integrität des Vertrauensspeichers verletzt: known_hosts wurde außerhalb "
            "der Anwendung verändert. Alle SSH-Aktionen sind blockiert. "
            "Serveridentitäten in Konfiguration → Serveridentität prüfen erneut bestätigen.")


def load():
    storage.secure_directory(settings.DATA_DIR)
    try:
        raw = storage.read_private(settings.KNOWN_HOSTS)
    except FileNotFoundError:
        if settings.KNOWN_HOSTS_MAC.exists():
            raise TruststoreIntegrityError(
                "Vertrauensspeicher fehlt, aber ein Integritätssiegel ist vorhanden.")
        raise TruststoreInitializationRequired(
            "Vertrauensspeicher ist nicht initialisiert. Serverzugriffe bleiben blockiert.")
    _verify_truststore_integrity(raw)
    content = raw.decode("ascii")
    entries = {}
    try:
        for line in content.splitlines():
            if not line.strip() or line.startswith('#'):
                continue
            name, key_data = line.split(' ', 1)
            if name in entries or not name.startswith('[') or ']:' not in name:
                raise ValueError("Kein eindeutiger Host/Port-Eintrag")
            entries[name] = asyncssh.import_public_key(key_data)
    except (ValueError, asyncssh.KeyImportError) as exc:
        raise OSError(f"Ungültige Datei {settings.KNOWN_HOSTS}: {exc}") from exc
    return entries


@dataclass(frozen=True)
class Observation:
    host: str
    port: int
    key: object
    previous: object = None
    requested: str = ""
    address: str = ""

    @property
    def name(self):
        return endpoint(self.host, self.port)

    @property
    def changed(self):
        return self.previous is not None and self.previous != self.key

    @property
    def fingerprint(self):
        return self.key.get_fingerprint('sha256')


class ReviewRequired(OSError):
    def __init__(self, observation):
        self.observation = observation
        state = "GEÄNDERT – Verbindung blockiert" if observation.changed else "noch nicht bestätigt"
        super().__init__(f"Serveridentität {observation.name}: {state}. "
                         "Konfiguration → Serveridentität prüfen.")


@contextmanager
def _locked():
    with _LOCK:
        storage.secure_directory(settings.DATA_DIR)
        path = settings.DATA_DIR / 'known_hosts.lock'
        storage.secure_file(path, create=True)
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            if os.name == 'posix':
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


def initialize_fresh():
    """Create the first empty authenticated trust state exactly once."""
    with _locked():
        if settings.KNOWN_HOSTS.exists() or settings.KNOWN_HOSTS_MAC.exists():
            raise TruststoreIntegrityError(
                "Vertrauensspeicher ist bereits teilweise oder vollständig vorhanden.")
        raw = b''
        storage.write_private(settings.KNOWN_HOSTS, raw, exclusive=True)
        _write_truststore_mac(raw)


def reset_for_reconfirmation():
    """Quarantine unauthenticated legacy data and start with no trusted pins.

    This is only called after an explicit user confirmation in the startup UI.
    The quarantined bytes are inert and are never used as trust input.
    """
    with _locked():
        if not crypto.is_unlocked():
            raise TruststoreIntegrityError("Vault ist gesperrt.")
        if settings.KNOWN_HOSTS.exists():
            raw = storage.read_private(settings.KNOWN_HOSTS)
            storage.write_private(settings.KNOWN_HOSTS.with_name('known_hosts.untrusted'), raw)
        if settings.KNOWN_HOSTS_MAC.exists():
            tag = storage.read_private(settings.KNOWN_HOSTS_MAC)
            storage.write_private(settings.KNOWN_HOSTS_MAC.with_name('known_hosts.mac.untrusted'), tag)
        raw = b''
        storage.write_private(settings.KNOWN_HOSTS, raw)
        _write_truststore_mac(raw)


def confirm(observation: Observation):
    """Only called after explicit UI approval; detect stale confirmation dialogs."""
    with _locked():
        entries = load()
        if entries.get(observation.name) != observation.previous:
            raise OSError("Servereintrag wurde inzwischen geändert. Bitte erneut prüfen.")
        entries[observation.name] = observation.key
        content = ''.join(f"{name} {key.export_public_key('openssh').decode('ascii').strip()}\n"
                          for name, key in sorted(entries.items()))
        raw = content.encode('ascii')
        storage.write_private(settings.KNOWN_HOSTS, raw)
        _write_truststore_mac(raw)


class Validator(asyncssh.SSHClient):
    def __init__(self, host, port, requested, *, inspect=False, address="",
                 credential_host=None):
        self.host, self.port, self.requested = host, port, requested
        self.previous = load().get(endpoint(host, port))
        self.inspect = inspect
        self.address = address
        self.observation = None
        self.credential_host = credential_host
        self._trust_accepted = False
        self._password = None

    def validate_host_public_key(self, host, addr, port, key):
        self.observation = Observation(self.host, self.port, key, self.previous,
                                       self.requested, self.address)
        # Inspection deliberately rejects the handshake before user authentication.
        if self.inspect or self.previous is None or key != self.previous:
            self._trust_accepted = False
            return False
        # Re-read and authenticate the state at the trust decision. This catches
        # deletion/replacement after Validator construction.
        self._trust_accepted = load().get(endpoint(self.host, self.port)) == key
        return self._trust_accepted

    def _credential(self):
        if not self._trust_accepted or self.inspect or self.credential_host is None:
            return None
        if self._password is None:
            token = self.credential_host.get('password_enc')
            if token is None:
                return None
            self._password = crypto.decrypt_host_password(token, self.credential_host)
        return self._password

    def password_auth_requested(self):
        return self._credential()

    def kbdint_auth_requested(self):
        return '' if self.credential_host is not None else None

    def kbdint_challenge_received(self, name, instructions, lang, prompts):
        if not prompts:
            return []
        if len(prompts) == 1 and any(word in prompts[0][0].lower()
                                     for word in ('password', 'passcode')):
            password = self._credential()
            return [password] if password is not None else None
        return None

    def auth_completed(self):
        self._password = None
