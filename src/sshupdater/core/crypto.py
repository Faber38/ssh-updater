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

def set_master_password(password: str) -> None:
    """Leitet den Schlüssel ab und entsperrt das Vault (prozessweit)."""
    global _FERNET
    salt = _get_or_create_salt()
    key = _derive_key(password, salt)
    _FERNET = Fernet(key)

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
