"""Exact host/port pins in an application-owned OpenSSH-format file."""
from dataclasses import dataclass
from threading import RLock
from contextlib import contextmanager
import os
import asyncssh
from . import settings, storage

_LOCK = RLock()


def endpoint(host: str, port: int) -> str:
    if not host or any(c.isspace() or c in ',*?!|[]' for c in host):
        raise OSError("Ungültiger Hostname für die Serveridentitätsprüfung.")
    return f"[{host.lower()}]:{port}"


def load():
    storage.secure_directory(settings.DATA_DIR)
    try:
        content = storage.read_private(settings.KNOWN_HOSTS).decode("ascii")
    except FileNotFoundError:
        return {}
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


def confirm(observation: Observation):
    """Only called after explicit UI approval; detect stale confirmation dialogs."""
    with _locked():
        entries = load()
        if entries.get(observation.name) != observation.previous:
            raise OSError("Servereintrag wurde inzwischen geändert. Bitte erneut prüfen.")
        entries[observation.name] = observation.key
        content = ''.join(f"{name} {key.export_public_key('openssh').decode('ascii').strip()}\n"
                          for name, key in sorted(entries.items()))
        storage.write_private(settings.KNOWN_HOSTS, content.encode('ascii'))


class Validator(asyncssh.SSHClient):
    def __init__(self, host, port, requested, *, inspect=False, address=""):
        self.host, self.port, self.requested = host, port, requested
        self.previous = load().get(endpoint(host, port))
        self.inspect = inspect
        self.address = address
        self.observation = None

    def validate_host_public_key(self, host, addr, port, key):
        self.observation = Observation(self.host, self.port, key, self.previous,
                                       self.requested, self.address)
        # Inspection deliberately rejects the handshake before user authentication.
        return not self.inspect and self.previous is not None and key == self.previous
