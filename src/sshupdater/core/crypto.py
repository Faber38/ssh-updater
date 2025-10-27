from __future__ import annotations
import os, base64
from pathlib import Path
from typing import Optional
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.fernet import Fernet, InvalidToken

# Speicherort des Salts (~/.sshupdater/vault.salt)
from .settings import DATA_DIR

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
        return _SALT_PATH.read_bytes()
    salt = os.urandom(16)
    _SALT_PATH.write_bytes(salt)
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
    return _SALT_PATH.exists() and _VERIFIER_PATH.exists()

def set_master_password(password: str) -> None:
    """Leitet den Schlüssel ab, verifiziert (oder erzeugt) den Keystore und entsperrt das Vault."""
    global _FERNET
    salt = _get_or_create_salt()
    key = _derive_key(password, salt)
    f = Fernet(key)

    if _VERIFIER_PATH.exists():
        # Folgestart: Verifier entschlüsseln → Passwort prüfen
        token = _VERIFIER_PATH.read_bytes()
        try:
            plain = f.decrypt(token)
        except InvalidToken as e:
            raise WrongPassword("Master-Passwort ist falsch.") from e
        if plain != _CHALLENGE:
            # theoretisch „fremde“/veraltete Datei
            raise WrongPassword("Keystore-Verifikation fehlgeschlagen.")
        _FERNET = f
        return

    # Erstlauf: Verifier anlegen
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    token = f.encrypt(_CHALLENGE)
    _VERIFIER_PATH.write_bytes(token)
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
