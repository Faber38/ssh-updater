from __future__ import annotations
import base64
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.exceptions import InvalidSignature
from cryptography.fernet import Fernet, InvalidToken

# Speicherort des Salts (~/.sshupdater/vault.salt)
from .settings import DATA_DIR
from . import storage, credentials

_SALT_PATH = DATA_DIR / "vault.salt"
_FERNET: Optional[Fernet] = None
_MAC_KEY: Optional[bytes] = None
# Getrennter Zweck-String, damit der Truststore-Schlüssel nie mit dem Vault-Schlüssel kollidiert.
_TRUSTSTORE_MAC_INFO = b"ssh-updater-truststore-mac-v1"

# NEU:
class WrongPassword(Exception):
    pass

# NEU: Prüf-Token-Datei (liegt neben vault.salt)
_VERIFIER_PATH = DATA_DIR / "vault.verify"
_CHALLENGE = b"ssh-updater-keystore-v1"

logger = logging.getLogger(__name__)

# F3: umschlossener Vault-Schlüssel mit speicherharter Ableitung (scrypt).
def _kdf_path():
    return DATA_DIR / "vault.kdf"

# Mindestlänge nur beim Setzen/Ändern, nicht beim Entsperren bestehender Vaults.
MIN_MASTER_PASSWORD_LENGTH = 12

# scrypt-Parameter (~64 MiB je Rateversuch, GPU-hart). Erhöhbar; Migration ist automatisch.
_SCRYPT_PARAMS = {"algo": "scrypt", "n": 1 << 16, "r": 8, "p": 1}

# Online-Drossel: wachsende Wartezeit nach Fehlversuchen (siehe throttle-Datei).
def _throttle_path():
    return DATA_DIR / "vault.throttle"

_THROTTLE_MAX_DELAY = 30.0



def _get_or_create_salt() -> bytes:
    if _SALT_PATH.exists():
        return storage.read_private(_SALT_PATH)
    salt = os.urandom(16)
    storage.write_private(_SALT_PATH, salt, exclusive=True)
    return salt

# Hilfsfunktion: Existiert bereits ein Keystore?
def keystore_exists() -> bool:
    """True, sobald ein Keystore existiert. Legacy (vault.salt) und scrypt (vault.kdf).

    Ein Keystore braucht genau eine KDF-Quelle plus den Prüftoken. Liegen nach einer
    unterbrochenen Migration beide KDF-Quellen vor, gewinnt vault.kdf; der Legacy-Salt
    wird beim Entsperren entfernt.
    """
    storage.initialize(DATA_DIR)
    verify_exists = _VERIFIER_PATH.exists()
    kdf_exists = _kdf_path().exists()
    salt_exists = _SALT_PATH.exists()
    key_source = kdf_exists or salt_exists

    if verify_exists != key_source:
        raise OSError('Vault unvollständig: Schlüsselmaterial oder Prüftoken fehlt. '
                      'Bitte Sicherung wiederherstellen.')
    if not verify_exists:
        if _encrypted_tokens():
            raise OSError('Vault unvollständig: Verschlüsselte Zugangsdaten vorhanden, aber '
                          'Schlüsselmaterial und Prüftoken fehlen. Bitte zusammengehörige '
                          'Sicherung wiederherstellen.')
        return False
    if salt_exists and not kdf_exists and len(storage.read_private(_SALT_PATH)) != 16:
        raise OSError('Vault inkonsistent: Ungültiger Salt. Bitte Sicherung wiederherstellen.')
    return True


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

class WeakPassword(Exception):
    pass


def require_strong_password(password: str) -> None:
    """Nur beim Setzen/Ändern des Master-Passworts, nicht beim Entsperren."""
    if not isinstance(password, str) or len(password) < MIN_MASTER_PASSWORD_LENGTH:
        raise WeakPassword(
            f"Master-Passwort muss mindestens {MIN_MASTER_PASSWORD_LENGTH} Zeichen haben.")
    if len(set(password)) < 4:
        raise WeakPassword("Master-Passwort ist zu einförmig (zu wenige verschiedene Zeichen).")


def _scrypt_raw(password: str, salt: bytes, params: dict) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=params["n"], r=params["r"], p=params["p"])
    return kdf.derive(password.encode("utf-8"))


def _wrapping_fernet(password: str, salt: bytes, params: dict) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(_scrypt_raw(password, salt, params)))


def _read_kdf() -> Optional[dict]:
    if not _kdf_path().exists():
        return None
    try:
        data = json.loads(storage.read_private(_kdf_path()).decode("utf-8"))
        fields = {"version", "algo", "n", "r", "p", "salt", "wrapped_vmk"}
        if type(data) is not dict or set(data) != fields:
            raise ValueError("fields")
        if (type(data["version"]) is not int or data["version"] != 1
                or type(data["algo"]) is not str or data["algo"] != "scrypt"
                or type(data["n"]) is not int or type(data["r"]) is not int
                or type(data["p"]) is not int or type(data["salt"]) is not str
                or type(data["wrapped_vmk"]) is not str
                or (data["n"], data["r"], data["p"]) !=
                   (_SCRYPT_PARAMS["n"], _SCRYPT_PARAMS["r"], _SCRYPT_PARAMS["p"])):
            raise ValueError("policy")
        salt = base64.b64decode(data["salt"], altchars=b'-_', validate=True)
        wrapped = base64.b64decode(data["wrapped_vmk"], altchars=b'-_', validate=True)
        if len(salt) != 16 or not 80 <= len(wrapped) <= 256:
            raise ValueError("size")
    except (KeyError, ValueError, TypeError, UnicodeError) as exc:
        raise OSError(
            "Vault-KDF-Block unbekannt oder beschädigt. Bitte Sicherung wiederherstellen.") from exc
    return data


def _write_kdf(password: str, vmk_raw: bytes, params: dict) -> None:
    salt = os.urandom(16)
    wrapped = _wrapping_fernet(password, salt, params).encrypt(vmk_raw)
    blob = json.dumps({"version": 1, "algo": params["algo"], "n": params["n"],
                       "r": params["r"], "p": params["p"],
                       "salt": base64.urlsafe_b64encode(salt).decode("ascii"),
                       "wrapped_vmk": base64.urlsafe_b64encode(wrapped).decode("ascii")}).encode("utf-8")
    storage.write_private(_kdf_path(), blob)


def _unwrap_vmk(password: str, kdf: dict) -> bytes:
    salt = base64.b64decode(kdf["salt"], altchars=b'-_', validate=True)
    wrapped = base64.b64decode(kdf["wrapped_vmk"], altchars=b'-_', validate=True)
    params = {"n": kdf["n"], "r": kdf["r"], "p": kdf["p"]}
    try:
        return _wrapping_fernet(password, salt, params).decrypt(wrapped)
    except InvalidToken as exc:
        raise WrongPassword("Master-Passwort ist falsch oder der Vault-Block ist beschädigt.") from exc


def _throttle_state():
    try:
        raw = storage.read_private(_throttle_path())
        count, last = json.loads(raw.decode("utf-8"))
        return int(count), float(last)
    except (FileNotFoundError, ValueError, TypeError, UnicodeError):
        return 0, 0.0


def _throttle_wait_if_needed():
    count, _ = _throttle_state()
    if count <= 0:
        return
    delay = min(2.0 ** min(count, 6), _THROTTLE_MAX_DELAY)
    time.sleep(delay)


def _throttle_record_failure():
    count, _ = _throttle_state()
    try:
        storage.write_private(_throttle_path(),
                              json.dumps([count + 1, time.time()]).encode("utf-8"))
    except OSError:
        pass


def _throttle_reset():
    try:
        os.remove(_throttle_path())
    except (FileNotFoundError, OSError):
        pass


def _derive_mac_key(fernet_key: bytes) -> bytes:
    """Truststore-MAC-Schlüssel, getrennt vom Vault-Schlüssel abgeleitet."""
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=_TRUSTSTORE_MAC_INFO).derive(fernet_key)


def truststore_mac(data: bytes) -> bytes:
    """HMAC-SHA256 über den Truststore-Inhalt; nur bei entsperrtem Vault."""
    if _MAC_KEY is None:
        raise RuntimeError("Vault ist gesperrt.")
    h = hmac.HMAC(_MAC_KEY, hashes.SHA256())
    h.update(data)
    return h.finalize()


def verify_truststore(data: bytes, tag: bytes) -> bool:
    if _MAC_KEY is None:
        raise RuntimeError("Vault ist gesperrt.")
    h = hmac.HMAC(_MAC_KEY, hashes.SHA256())
    h.update(data)
    try:
        h.verify(tag)
        return True
    except InvalidSignature:
        return False


def _consistency_check(f: Fernet) -> None:
    """Prüft Verifier und alle gespeicherten Zugangsdaten gegen den Schlüssel."""
    token = storage.read_private(_VERIFIER_PATH)
    try:
        plain = f.decrypt(token)
    except InvalidToken as e:
        raise WrongPassword("Master-Passwort ist falsch oder der Vault-Prüftoken ist beschädigt.") from e
    if plain != _CHALLENGE:
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


def _unlock(f: Fernet, fernet_key: bytes) -> None:
    global _FERNET, _MAC_KEY
    _FERNET = f
    _MAC_KEY = _derive_mac_key(fernet_key)
    _throttle_reset()


def set_master_password(password: str) -> None:
    """Entsperrt das Vault oder legt es an; migriert Legacy-Vaults transparent auf scrypt.

    Der Fernet-Schlüssel ist in beiden Formaten base64(32 rohe Bytes). Beim Legacy-Format
    sind das die PBKDF2-Bytes, beim scrypt-Format ein zufälliger Vault-Schlüssel, der mit
    dem scrypt-abgeleiteten Schlüssel umschlossen in vault.kdf liegt. Die Migration bindet
    den bestehenden PBKDF2-Schlüssel als Vault-Schlüssel ein, sodass Zugangsdaten und das
    Truststore-Siegel ohne Neuverschlüsselung gültig bleiben.
    """
    global _FERNET, _MAC_KEY
    _FERNET = None
    _MAC_KEY = None
    existing = keystore_exists()
    kdf = _read_kdf()

    if existing:
        _throttle_wait_if_needed()
        if kdf is not None:
            # scrypt-Vault: Vault-Schlüssel aus vault.kdf auswickeln.
            try:
                vmk_raw = _unwrap_vmk(password, kdf)
            except WrongPassword:
                _throttle_record_failure()
                raise
            fernet_key = base64.urlsafe_b64encode(vmk_raw)
            f = Fernet(fernet_key)
            try:
                _consistency_check(f)
            except WrongPassword:
                _throttle_record_failure()
                raise
            if _SALT_PATH.exists():
                try:
                    os.remove(_SALT_PATH)
                except OSError as exc:
                    raise OSError(
                        "Legacy-Salt konnte nach der KDF-Migration nicht sicher entfernt werden; "
                        "Vault bleibt gesperrt.") from exc
            _unlock(f, fernet_key)
            return

        # Legacy-Vault (PBKDF2): entsperren, prüfen, dann auf scrypt migrieren.
        salt = _get_or_create_salt()
        raw = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                         iterations=200_000).derive(password.encode("utf-8"))
        fernet_key = base64.urlsafe_b64encode(raw)
        f = Fernet(fernet_key)
        try:
            _consistency_check(f)
        except WrongPassword:
            _throttle_record_failure()
            raise
        # Migration: vault.kdf atomar schreiben, danach Legacy-Salt entfernen.
        _write_kdf(password, raw, _SCRYPT_PARAMS)
        try:
            os.remove(_SALT_PATH)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise OSError(
                "Legacy-Salt konnte nach der KDF-Migration nicht sicher entfernt werden; "
                "Vault bleibt gesperrt.") from exc
        logger.warning("Vault von PBKDF2 auf scrypt migriert (vault.kdf angelegt).")
        _unlock(f, fernet_key)
        return

    # Erstlauf: neuen scrypt-Vault mit zufälligem Vault-Schlüssel anlegen.
    require_strong_password(password)
    vmk_raw = os.urandom(32)
    fernet_key = base64.urlsafe_b64encode(vmk_raw)
    f = Fernet(fernet_key)
    token = f.encrypt(_CHALLENGE)
    storage.write_private(_VERIFIER_PATH, token, exclusive=True)
    try:
        _write_kdf(password, vmk_raw, _SCRYPT_PARAMS)
    except Exception:
        # Avoid turning a failed first run into an incomplete vault. If removal
        # itself fails, the remaining verifier still makes the next start fail
        # closed instead of silently generating a different key.
        try:
            os.remove(_VERIFIER_PATH)
        except OSError:
            pass
        raise
    _unlock(f, fernet_key)

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
