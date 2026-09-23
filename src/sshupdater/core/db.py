from __future__ import annotations
import json, sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from .settings import DB_PATH
from . import crypto, storage, credentials
from types import MappingProxyType

def _connect() -> sqlite3.Connection:
    storage.secure_directory(DB_PATH.parent)
    storage.secure_file(DB_PATH, create=True)
    for suffix in ('-journal', '-wal', '-shm'):
        storage.secure_file(Path(str(DB_PATH) + suffix))
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db() -> None:
    con = _connect()
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS hosts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        proxmox_uid TEXT UNIQUE,
        name TEXT NOT NULL,
        primary_ip TEXT,
        ips_json TEXT,
        port INTEGER DEFAULT 22,
        user TEXT,
        auth_method TEXT CHECK(auth_method IN ('key','password')) DEFAULT 'key',
        key_path TEXT,
        password_enc BLOB,
        distro TEXT,
        tags_json TEXT,
        last_check TEXT,
        pending_updates INTEGER
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        host_id INTEGER,
        ts TEXT,
        action TEXT,
        rc INTEGER,
        summary TEXT,
        stdout TEXT,
        stderr TEXT,
        FOREIGN KEY(host_id) REFERENCES hosts(id)
    );
    """)
    con.commit()
    con.close()

# -------- Settings DAO --------

def set_setting(key: str, value: Any) -> None:
    con = _connect()
    con.execute("REPLACE INTO settings(key,value) VALUES(?,?)", (key, json.dumps(value)))
    con.commit(); con.close()

def get_setting(key: str, default: Any=None) -> Any:
    con = _connect()
    cur = con.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone(); con.close()
    if not row: return default
    return json.loads(row["value"])

# -------- Hosts DAO --------

class HostNotFoundError(LookupError):
    pass

class CredentialChangeRequired(ValueError):
    pass


def target_changed(old, new):
    return credentials.normalize_target(old) != credentials.normalize_target(new)


def _password_value(old, new, password_plain, delete_password, keep_password):
    if delete_password and password_plain:
        raise CredentialChangeRequired("Passwort entweder ersetzen oder löschen.")
    if password_plain:
        return crypto.encrypt_host_password(password_plain, new)
    if delete_password:
        return None
    if old and old.get("password_enc") is not None:
        if target_changed(old, new):
            raise CredentialChangeRequired(
                "Host, Benutzer oder Port geändert: Passwort erneut eingeben oder löschen.")
        if (old.get("auth_method") == "password" and new["auth_method"] == "key"
                and keep_password is not True):
            raise CredentialChangeRequired("Bitte entscheiden, ob das alte Passwort behalten wird.")
        return old["password_enc"]
    return None


def add_or_update_host(
    *, proxmox_uid: Optional[str], name: str, primary_ip: Optional[str],
    ips: Optional[List[str]] = None, port: int = 22, user: Optional[str] = None,
    auth_method: str = "key", key_path: Optional[str] = None,
    password_plain: Optional[str] = None, distro: Optional[str] = None,
    tags: Optional[List[str]] = None, delete_password: bool = False,
    keep_password: Optional[bool] = None,
) -> int:
    """Upsert with the same credential policy as the explicit edit path."""
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        if proxmox_uid:
            rows = con.execute("SELECT * FROM hosts WHERE proxmox_uid=?", (proxmox_uid,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM hosts WHERE name=?", (name,)).fetchall()
        if len(rows) > 1:
            raise CredentialChangeRequired("Hostname ist nicht eindeutig. Bitte vorhandenen Host bearbeiten.")
        old = dict(rows[0]) if rows else None
        new = dict(primary_ip=primary_ip, port=port, user=user, auth_method=auth_method)
        credentials.normalize_target(new)
        if old:
            host_id = old["id"]
        else:
            # Allocate the final identity in this transaction before encryption.
            cur = con.execute("INSERT INTO hosts(name, proxmox_uid) VALUES(?,?)", (name, proxmox_uid))
            host_id = cur.lastrowid
        new['id'] = host_id
        token = _password_value(old, new, password_plain, delete_password, keep_password)
        values = (name, primary_ip, json.dumps(ips or []), port, user,
                  auth_method, key_path, token, distro, json.dumps(tags or []))
        con.execute("""UPDATE hosts SET name=?, primary_ip=?, ips_json=?, port=?, user=?,
            auth_method=?, key_path=?, password_enc=?, distro=?, tags_json=? WHERE id=?""",
            values + (host_id,))
        con.commit()
        return host_id
    finally:
        con.close()


def update_host(
    host_id: int, *, name: str, primary_ip: Optional[str], port: int = 22,
    user: Optional[str] = None, auth_method: str = "key",
    key_path: Optional[str] = None, password_plain: Optional[str] = None,
    delete_password: bool = False, keep_password: Optional[bool] = None,
) -> None:
    """Change target and credentials atomically, preserving unrelated metadata."""
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM hosts WHERE id=?", (host_id,)).fetchone()
        if row is None:
            raise HostNotFoundError(f"Host mit ID {host_id} wurde nicht gefunden.")
        new = dict(id=host_id, primary_ip=primary_ip, port=port, user=user, auth_method=auth_method)
        credentials.normalize_target(new)
        token = _password_value(dict(row), new, password_plain, delete_password, keep_password)
        con.execute("""UPDATE hosts SET name=?, primary_ip=?, port=?, user=?,
            auth_method=?, key_path=?, password_enc=? WHERE id=?""",
            (name, primary_ip, port, user, auth_method, key_path, token, host_id))
        con.commit()
    finally:
        con.close()

def list_hosts() -> List[Dict[str, Any]]:
    con = _connect()
    cur = con.execute("SELECT * FROM hosts ORDER BY name")
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    # password_enc bleibt verschlüsselt; UI zeigt Platzhalter
    return rows

def get_host(host_id: int) -> Optional[Dict[str, Any]]:
    con = _connect()
    try:
        row = con.execute("SELECT * FROM hosts WHERE id=?", (host_id,)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()

class HostConfigurationChanged(OSError):
    def __init__(self):
        super().__init__('Die Hostkonfiguration wurde zwischenzeitlich geändert. Bitte Aktion neu starten.')


def connection_context(host, *, include_unused_auth=False):
    """Immutable scalar snapshot; no plaintext secrets, no mutable backing data."""
    bound = credentials.identity(host)
    method = host.get('auth_method', 'key')
    key_path = (host.get('key_path') or None) if method == 'key' or include_unused_auth else None
    if method not in ('key', 'password') or (key_path is not None and type(key_path) is not str):
        raise credentials.CredentialError('Ungültige SSH-Authentifizierungskonfiguration.')
    token = host.get('password_enc') if method == 'password' or include_unused_auth else None
    if token is not None and type(token) is not bytes:
        raise credentials.CredentialError('Ungültiges Credential-Format.')
    return MappingProxyType(dict(id=bound.host_id, primary_ip=bound.primary_ip,
                                 user=bound.user, port=bound.port, auth_method=method,
                                 key_path=key_path, password_enc=token))


def _check_expected(current, expected, *, credential_edit=False):
    # Credential edits must still detect replacement of a retained password.
    if (connection_context(current, include_unused_auth=credential_edit) !=
            connection_context(expected, include_unused_auth=credential_edit)):
        raise HostConfigurationChanged()


def get_connection_context(expected):
    # One SELECT supplies every connection field, including the encrypted token.
    current = get_host(expected['id'])
    if current is None:
        raise HostConfigurationChanged()
    _check_expected(current, expected)
    return connection_context(current)


def set_host_password(host_id: int, password_plain: str, *, expected=None) -> None:
    con = _connect()
    try:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT * FROM hosts WHERE id=?', (host_id,)).fetchone()
        if row is None:
            raise HostNotFoundError('Host wurde nicht gefunden.')
        host = dict(row)
        if expected is not None:
            _check_expected(host, expected, credential_edit=True)
        token = crypto.encrypt_host_password(password_plain, host)
        con.execute('UPDATE hosts SET password_enc=? WHERE id=?', (token, host_id))
        con.commit()
    finally:
        con.close()


def get_host_password(host_id: int) -> Optional[str]:
    host = get_host(host_id)
    if not host or host['password_enc'] is None:
        return None
    return crypto.decrypt_host_password(host['password_enc'], host)

def set_check_result(host_id: int, last_check: str, pending_updates: int | None) -> None:
    con = _connect()
    con.execute(
        "UPDATE hosts SET last_check=?, pending_updates=? WHERE id=?",
        (last_check, pending_updates, host_id),
    )
    con.commit()
    con.close()
