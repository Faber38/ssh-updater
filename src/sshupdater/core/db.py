from __future__ import annotations
import json, sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from .settings import DB_PATH
from . import crypto

def _connect() -> sqlite3.Connection:
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

def add_or_update_host(
    *,
    proxmox_uid: Optional[str],
    name: str,
    primary_ip: Optional[str],
    ips: Optional[List[str]] = None,
    port: int = 22,
    user: Optional[str] = None,
    auth_method: str = "key",
    key_path: Optional[str] = None,
    password_plain: Optional[str] = None,
    distro: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> int:
    """Upsert per proxmox_uid (falls vorhanden), sonst per name."""
    con = _connect()
    ips_json = json.dumps(ips or [])
    tags_json = json.dumps(tags or [])
    password_enc = crypto.encrypt_str(password_plain) if password_plain else None

    # Prüfen, ob vorhanden
    if proxmox_uid:
        cur = con.execute("SELECT id FROM hosts WHERE proxmox_uid=?", (proxmox_uid,))
        row = cur.fetchone()
    else:
        cur = con.execute("SELECT id FROM hosts WHERE name=?", (name,))
        row = cur.fetchone()

    if row:
        host_id = row["id"]
        con.execute("""
            UPDATE hosts SET
                name=?, primary_ip=?, ips_json=?, port=?,
                user=?, auth_method=?, key_path=?,
                password_enc=COALESCE(?, password_enc),
                distro=?, tags_json=?
            WHERE id=?
        """, (name, primary_ip, ips_json, port, user, auth_method, key_path,
              password_enc, distro, tags_json, host_id))
    else:
        cur = con.execute("""
            INSERT INTO hosts(proxmox_uid,name,primary_ip,ips_json,port,user,auth_method,key_path,password_enc,distro,tags_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """, (proxmox_uid, name, primary_ip, ips_json, port, user, auth_method, key_path, password_enc, distro, tags_json))
        host_id = cur.lastrowid

    con.commit(); con.close()
    return host_id

def list_hosts() -> List[Dict[str, Any]]:
    con = _connect()
    cur = con.execute("SELECT * FROM hosts ORDER BY name")
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    # password_enc bleibt verschlüsselt; UI zeigt Platzhalter
    return rows

def get_host(host_id: int) -> Optional[Dict[str, Any]]:
    con = _connect()
    cur = con.execute("SELECT * FROM hosts WHERE id=?", (host_id,))
    row = cur.fetchone(); con.close()
    return dict(row) if row else None

def set_host_password(host_id: int, password_plain: str) -> None:
    con = _connect()
    token = crypto.encrypt_str(password_plain)
    con.execute("UPDATE hosts SET password_enc=? WHERE id=?", (token, host_id))
    con.commit(); con.close()

def get_host_password(host_id: int) -> Optional[str]:
    con = _connect()
    cur = con.execute("SELECT password_enc FROM hosts WHERE id=?", (host_id,))
    row = cur.fetchone(); con.close()
    if not row or row["password_enc"] is None:
        return None
    return crypto.decrypt_str(row["password_enc"])
