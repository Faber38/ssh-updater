-- Unmodified table definitions shared by v1.2.2 and v1.2.3.

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


    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );


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
