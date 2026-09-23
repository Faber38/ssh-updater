from __future__ import annotations
import os, base64
import sqlite3
from pathlib import Path
from typing import Optional
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.fernet import Fernet, InvalidToken

# Speicherort des Salts (~/.sshupdater/vault.salt)
from .settings import DATA_DIR
from . import storage, credentials

_SALT_PATH = DATA_DIR / "vault.salt"
_FERNET: Optional[Fernet] = None

# NEU:
class WrongPassword(Exception):
    pass

# NEU: Prüf-Token-Datei (liegt neben vault.salt)
_VERIFIER_PATH = DATA_DIR / "vault.verify"
_CHALLENGE = b"ssh-updater-keystore-v1"



def _get_or_create_salt() -> bytes:
    if _SALT_PATH.exists():
        return storage.read_private(_SALT_PATH)
    salt = os.urandom(16)
    storage.write_private(_SALT_PATH, salt, exclusive=True)
    return salt

def _derive_key(password: str, salt: bytes, iterations: int = 200_000) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))

# Hilfsfunktion: Existiert bereits ein Keystore?
def keystore_exists() -> bool:
    storage.initialize(DATA_DIR)
    salt_exists = _SALT_PATH.exists()
    verify_exists = _VERIFIER_PATH.exists()
    if salt_exists != verify_exists:
        raise OSError('Vault unvollständig: Salt oder Prüftoken fehlt. Bitte Sicherung wiederherstellen.')
    if not salt_exists and _encrypted_tokens():
        raise OSError('Vault unvollständig: Verschlüsselte Zugangsdaten vorhanden, aber Salt und '
                      'Prüftoken fehlen. Bitte zusammengehörige Sicherung wiederherstellen.')
    if salt_exists and len(storage.read_private(_SALT_PATH)) != 16:
        raise OSError('Vault inkonsistent: Ungültiger Salt. Bitte Sicherung wiederherstellen.')
    return salt_exists


def _encrypted_records():
    # Also protect encrypted Proxmox configuration. Never create/migrate the DB
    # while deciding whether this is a fresh installation; include live WAL data.
    config = DATA_DIR / 'config.enc'
    tokens = [(None, storage.read_private(config))] if config.exists() else []
    path = DATA_DIR / 'app.db'
    if not path.exists():
        return tokens
    try:
        con = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
        con.row_factory = sqlite3.Row
        try:
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables:
                return tokens
            tokens.extend((dict(row), row['password_enc']) for row in con.execute(
                'SELECT * FROM hosts WHERE password_enc IS NOT NULL'))
            return tokens
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise OSError('Vault-Zustand nicht prüfbar: Datenbank beschädigt oder nicht lesbar. '
                      'Es wird kein neuer Schlüssel erzeugt.') from exc

def _encrypted_tokens():
    return [token for _, token in _encrypted_records()]


def set_master_password(password: str) -> None:
    """Leitet den Schlüssel ab, verifiziert (oder erzeugt) den Keystore und entsperrt das Vault."""
    global _FERNET
    _FERNET = None
    keystore_exists()
    salt = _get_or_create_salt()
    key = _derive_key(password, salt)
    f = Fernet(key)

    if _VERIFIER_PATH.exists():
        # Folgestart: Verifier entschlüsseln → Passwort prüfen
        token = storage.read_private(_VERIFIER_PATH)
        try:
            plain = f.decrypt(token)
        except InvalidToken as e:
            raise WrongPassword("Master-Passwort ist falsch oder der Vault-Prüftoken ist beschädigt.") from e
        if plain != _CHALLENGE:
            # theoretisch „fremde“/veraltete Datei
            raise WrongPassword("Keystore-Verifikation fehlgeschlagen.")
        try:
            for host, encrypted in _encrypted_records():
                if host is None:
                    f.decrypt(encrypted)
                else:
                    credentials.decrypt(f, encrypted, host, validate_legacy=True)
        except (InvalidToken, TypeError, credentials.CredentialError):
            raise OSError('Vault inkonsistent: Gespeicherte Zugangsdaten passen nicht zum Schlüssel '
                          'oder sind beschädigt. Bitte zusammengehörige Sicherung wiederherstellen.') from None
        _FERNET = f
        return

    # Erstlauf: Verifier anlegen
    token = f.encrypt(_CHALLENGE)
    storage.write_private(_VERIFIER_PATH, token, exclusive=True)
    _FERNET = f

def is_unlocked() -> bool:
    return _FERNET is not None

def encrypt_str(value: str) -> bytes:
    """Gibt einen Fernet-Token (bytes) zurück."""
    if not is_unlocked():
        raise RuntimeError("Vault ist gesperrt – setze zuerst das Masterpasswort.")
    return _FERNET.encrypt(value.encode("utf-8"))

def decrypt_str(token: bytes) -> str:
    if not is_unlocked():
        raise RuntimeError("Vault ist gesperrt – setze zuerst das Masterpasswort.")
    try:
        return _FERNET.decrypt(token).decode("utf-8")
    except InvalidToken as e:
        raise ValueError("Entschlüsselung fehlgeschlagen (falsches Masterpasswort?).") from e


def encrypt_host_password(secret, host):
    if not is_unlocked():
        raise credentials.CredentialError('Vault ist gesperrt.')
    return credentials.encrypt(_FERNET, secret, host)


def decrypt_host_password(token, host):
    if not is_unlocked():
        raise credentials.CredentialError('Vault ist gesperrt.')
    return credentials.decrypt(_FERNET, token, host)
