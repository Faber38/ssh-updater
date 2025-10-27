from __future__ import annotations
import asyncio, asyncssh, re
from typing import Dict, Any, Tuple
from . import db

def _auth_params(host: Dict[str, Any]) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    if host.get("auth_method") == "password":
        pw = db.get_host_password(host["id"])
        if pw:
            params["password"] = pw
    else:
        key_path = host.get("key_path")
        if key_path:
            params["client_keys"] = [key_path]
    return params

async def _run(conn: asyncssh.SSHClientConnection, cmd: str, timeout: int = 90) -> Tuple[int, str, str]:
    try:
        res = await asyncio.wait_for(conn.run(cmd, check=False), timeout=timeout)
        return res.exit_status, res.stdout, res.stderr
    except asyncio.TimeoutError:
        return 124, "", f"Timeout after {timeout}s: {cmd}"

async def _detect_distro(conn) -> str:
    code, out, _ = await _run(conn, "bash -lc 'cat /etc/os-release 2>/dev/null'")
    if code != 0 or not out:
        return "unknown"
    m = re.search(r'^ID=(.+)$', out, re.MULTILINE)
    if not m:
        return "unknown"
    val = m.group(1).strip().strip('"').lower()
    if val in ("debian","ubuntu","linuxmint","pop"):
        return "debian"
    if val in ("fedora","rhel","centos","rocky","almalinux","ol"):
        return "rpm"
    if val in ("arch","manjaro","endeavouros","arco"):
        return "arch"
    return "unknown"

# ---------- Update Check  ----------
async def _check_debian(conn):
    await _run(conn, "bash -lc 'export LC_ALL=C LANG=C; sudo -n apt-get -qq update'")
    code, out, err = await _run(conn, "bash -lc 'export LC_ALL=C LANG=C; apt-get -s dist-upgrade'")
    if code == 124:
        return -1, "Timeout"

    n = 0
    for line in out.splitlines():
        if line.startswith("Inst "):  # APT schreibt 'Inst <pkg> [...]'
            n += 1

    # Fallback: Summary-Zeile 'X upgraded, Y newly installed, ...'
    if n == 0:
        m = re.search(r'(\d+)\s+upgraded', out)
        if m:
            n = int(m.group(1))

    return n, err.strip()

async def _check_rpm(conn):
    code, out, err = await _run(conn, "sudo -n dnf -q check-update; echo $?")
    last = out.strip().splitlines()[-1] if out else "0"
    try:
        rc = int(last)
    except ValueError:
        rc = 0
    n = 0 if rc in (0, 1) else 1
    return n, err.strip()

async def _check_arch(conn):
    code, out, err = await _run(conn, "bash -lc 'command -v checkupdates >/dev/null 2>&1 && checkupdates | wc -l || echo 0'")
    try:
        n = int(out.strip()) if out.strip() else 0
    except ValueError:
        n = 0
    return n, err.strip()

async def check_updates_for_host(host: Dict[str, Any]) -> Dict[str, Any]:
    name = host.get("name") or f"id:{host['id']}"
    ip = host.get("primary_ip")
    port = int(host.get("port") or 22)
    user = host.get("user") or "root"
    if not ip or not user:
        return {"host_id": host["id"], "name": name, "status": "error", "note": "IP/User fehlt"}

    params = _auth_params(host)
    try:
        async with asyncssh.connect(ip, port=port, username=user, known_hosts=None, **params) as conn:
            distro = await _detect_distro(conn)
            if distro == "debian":
                n, note = await _check_debian(conn)
            elif distro == "rpm":
                n, note = await _check_rpm(conn)
            elif distro == "arch":
                n, note = await _check_arch(conn)
            else:
                return {"host_id": host["id"], "name": name, "status": "error", "note": "Unbekannte Distro"}
            return {"host_id": host["id"], "name": name, "status": "ok", "distro": distro, "updates": max(n,0), "note": note or ""}
    except (asyncssh.Error, OSError) as e:
        return {"host_id": host["id"], "name": name, "status": "error", "note": f"SSH: {e}"}

# ---------- Simulation (Dry-Run) ----------

async def _sim_debian(conn):
    # 1) Index aktualisieren (sprachneutral)
    await _run(conn, "bash -lc 'export LC_ALL=C LANG=C; sudo -n apt-get -qq update'")

    # 2) Simulation fahren
    code, out, err = await _run(conn, "bash -lc 'export LC_ALL=C LANG=C; apt-get -s dist-upgrade'")

    # 3) Pakete zählen
    n = 0
    for line in out.splitlines():
        if line.startswith("Inst "):   # apt-get -s schreibt 'Inst <pkg> ...'
            n += 1
    if n == 0:
        code2, out2, _ = await _run(conn, "bash -lc 'export LC_ALL=C LANG=C; apt list --upgradable 2>/dev/null | tail -n +2 | wc -l'")
        try: n = max(n, int(out2.strip()))
        except: pass

    return n, out, err

async def _sim_rpm(conn):
    # 'assumeno' zeigt, was passieren würde
    code, out, err = await _run(conn, "sudo -n dnf -q upgrade --refresh --assumeno")
    # Heuristik: Zeilen mit 'Upgrading ' zählen
    _, n_out, _ = await _run(conn, r"bash -lc \"sudo -n dnf -q upgrade --refresh --assumeno | grep -c -E 'Upgrading|Downgrading|Installing' || true\"")
    try:
        count = int(n_out.strip())
    except ValueError:
        count = 0
    return count, out, err

async def _sim_arch(conn):
    # Paketliste (ähnlich Dry-Run)
    code, out, err = await _run(conn, "bash -lc 'command -v checkupdates >/dev/null 2>&1 && checkupdates || true'")
    count = len([l for l in out.splitlines() if l.strip()])
    return count, out, err

async def simulate_upgrade_for_host(host: Dict[str, Any]) -> Dict[str, Any]:
    """Gibt geplante Paketupdates zurück (ohne Änderungen)."""
    name = host.get("name") or f"id:{host['id']}"
    ip = host.get("primary_ip")
    port = int(host.get("port") or 22)
    user = host.get("user") or "root"
    if not ip or not user:
        return {"host_id": host["id"], "name": name, "status": "error", "note": "IP/User fehlt"}

    params = _auth_params(host)
    try:
        async with asyncssh.connect(ip, port=port, username=user, known_hosts=None, **params) as conn:
            distro = await _detect_distro(conn)
            if distro == "debian":
                n, details, note = await _sim_debian(conn)
            elif distro == "rpm":
                n, details, note = await _sim_rpm(conn)
            elif distro == "arch":
                n, details, note = await _sim_arch(conn)
            else:
                return {"host_id": host["id"], "name": name, "status": "error", "note": "Unbekannte Distro"}
            return {
                "host_id": host["id"], "name": name, "status": "ok",
                "distro": distro, "packages": n, "details": details, "note": note or ""
            }
    except (asyncssh.Error, OSError) as e:
        return {"host_id": host["id"], "name": name, "status": "error", "note": f"SSH: {e}"}

# ---------- Upgrade (mit Live-Streaming) ----------

async def _stream(conn, cmd: str, timeout: int = 0):
    """
    Führt einen Befehl aus und liefert stdout zeilenweise (yield).
    timeout=0 -> kein globales Timeout (Paket-Upgrades können dauern).
    Gibt am Ende eine Zeile [RC=<code>] aus.
    """
    proc = await conn.create_process(cmd)
    rc = 0
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            yield line.rstrip("\n")
        rc = await proc.wait()
    except Exception as e:
        yield f"[client] stream error: {e}"
        rc = 1
    # kein return in async generatoren
    yield f"[RC={rc}]"

async def _upgrade_debian(conn):
    # Paketlisten
    async for _ in _stream(conn, "sudo -n apt-get update -y -o=Dpkg::Use-Pty=0"):
        pass
    # Upgrade streamen
    async for line in _stream(conn, "sudo -n DEBIAN_FRONTEND=noninteractive apt-get -y dist-upgrade -o=Dpkg::Use-Pty=0"):
        yield line

async def _upgrade_rpm(conn):
    async for line in _stream(conn, "sudo -n dnf -y upgrade --refresh"):
        yield line

async def _upgrade_arch(conn):
    async for line in _stream(conn, "sudo -n pacman -Syu --noconfirm"):
        yield line

async def upgrade_host_stream(host: Dict[str, Any]):
    """
    Async-Generator:
      - liefert während des Upgrades dicts: {"type":"line","line": "..."}
      - am Ende ein dict: {"type":"result","result": {"status": "...", "note": "...", "distro": "..."}}
    """
    name = host.get("name") or f"id:{host['id']}"
    ip = host.get("primary_ip")
    port = int(host.get("port") or 22)
    user = host.get("user") or "root"
    if not ip or not user:
        # Ergebnis "yielden", nicht returnen
        yield {"type": "result", "result": {"status": "error", "note": "IP/User fehlt"}}
        return

    params = _auth_params(host)
    try:
        async with asyncssh.connect(ip, port=port, username=user, known_hosts=None, **params) as conn:
            distro = await _detect_distro(conn)
            if distro == "debian":
                gen = _upgrade_debian(conn)
            elif distro == "rpm":
                gen = _upgrade_rpm(conn)
            elif distro == "arch":
                gen = _upgrade_arch(conn)
            else:
                yield {"type": "result", "result": {"status": "error", "note": "Unbekannte Distro"}}
                return

            rc = 0
            async for line in gen:
                # Zeilen streamen
                if line.startswith("[RC="):
                    # Exitcode extrahieren
                    try:
                        rc = int(line[4:-1])
                    except Exception:
                        rc = 0
                else:
                    yield {"type": "line", "line": line}

            # Finales Ergebnis liefern
            yield {"type": "result", "result": {"status": "ok" if rc == 0 else "error", "note": f"rc={rc}", "distro": distro}}
            return
    except (asyncssh.Error, OSError) as e:
        yield {"type": "result", "result": {"status": "error", "note": f"SSH: {e}"}}
        return
# ---------- Autoremove (Simulation + Live-Run) ----------

async def _sim_autoremove_debian(conn):
    # Simulation (sprachunabhängig)
    code, out, err = await _run(conn, "bash -lc 'export LC_ALL=C LANG=C; apt-get -s autoremove --purge'")
    # Zählen: Zeilen, die mit 'Remv ' beginnen, oder Summary '... to remove'
    n = sum(1 for ln in out.splitlines() if ln.startswith("Remv "))
    if n == 0:
        m = re.search(r'(\d+)\s+to remove', out)
        if m:
            n = int(m.group(1))
    return n, out, err

async def simulate_autoremove_for_host(host: Dict[str, Any]) -> Dict[str, Any]:
    name = host.get("name") or f"id:{host['id']}"
    ip, port, user = host.get("primary_ip"), int(host.get("port") or 22), host.get("user") or "root"
    if not ip or not user:
        return {"host_id": host["id"], "name": name, "status": "error", "note": "IP/User fehlt"}

    params = _auth_params(host)
    try:
        async with asyncssh.connect(ip, port=port, username=user, known_hosts=None, **params) as conn:
            distro = await _detect_distro(conn)
            if distro != "debian":
                return {"host_id": host["id"], "name": name, "status": "error", "note": "Autoremove nur Debian implementiert"}
            n, details, note = await _sim_autoremove_debian(conn)
            return {"host_id": host["id"], "name": name, "status": "ok", "distro": distro, "packages": n, "details": details, "note": note or ""}
    except (asyncssh.Error, OSError) as e:
        return {"host_id": host["id"], "name": name, "status": "error", "note": f"SSH: {e}"}

async def _run_autoremove_debian(conn):
    async for line in _stream(conn, "sudo -n apt-get -y autoremove --purge -o=Dpkg::Use-Pty=0"):
        yield line

async def autoremove_host_stream(host: Dict[str, Any]):
    """Async-Generator: liefert {'type':'line','line':...} und am Ende {'type':'result',...}."""
    name = host.get("name") or f"id:{host['id']}"
    ip, port, user = host.get("primary_ip"), int(host.get("port") or 22), host.get("user") or "root"
    if not ip or not user:
        yield {"type": "result", "result": {"status": "error", "note": "IP/User fehlt"}}
        return
    params = _auth_params(host)
    try:
        async with asyncssh.connect(ip, port=port, username=user, known_hosts=None, **params) as conn:
            distro = await _detect_distro(conn)
            if distro != "debian":
                yield {"type": "result", "result": {"status": "error", "note": "Autoremove nur Debian implementiert"}}
                return
            rc = 0
            async for line in _run_autoremove_debian(conn):
                if line.startswith("[RC="):
                    try: rc = int(line[4:-1])
                    except: rc = 0
                else:
                    yield {"type": "line", "line": line}
            yield {"type": "result", "result": {"status": "ok" if rc == 0 else "error", "note": f"rc={rc}", "distro": distro}}
            return
    except (asyncssh.Error, OSError) as e:
        yield {"type": "result", "result": {"status": "error", "note": f"SSH: {e}"}}
        return

# ---------- Reboot ----------

async def reboot_host(host: Dict[str, Any]) -> Dict[str, Any]:
    """Löst einen Reboot auf dem Zielhost aus (fire-and-forget)."""
    name = host.get("name") or f"id:{host['id']}"
    ip = host.get("primary_ip")
    port = int(host.get("port") or 22)
    user = host.get("user") or "root"
    if not ip or not user:
        return {"host_id": host["id"], "name": name, "status": "error", "note": "IP/User fehlt"}

    params = _auth_params(host)
    try:
        async with asyncssh.connect(ip, port=port, username=user, known_hosts=None, **params) as conn:
            # Fire-and-forget: Command im Hintergrund starten und gleich zurückkehren
            cmd = "bash -lc 'nohup sudo -n systemctl reboot >/dev/null 2>&1 & disown; echo TRIGGERED'"
            code, out, err = await _run(conn, cmd, timeout=10)
            if code == 0 and "TRIGGERED" in (out or ""):
                return {"host_id": host["id"], "name": name, "status": "ok", "note": "Reboot ausgelöst"}
            else:
                # Fallback versuchen
                cmd2 = "bash -lc 'nohup sudo -n reboot >/dev/null 2>&1 & disown; echo TRIGGERED'"
                code2, out2, err2 = await _run(conn, cmd2, timeout=10)
                if code2 == 0 and "TRIGGERED" in (out2 or ""):
                    return {"host_id": host["id"], "name": name, "status": "ok", "note": "Reboot ausgelöst"}
                return {"host_id": host["id"], "name": name, "status": "error", "note": (err or err2 or 'Unbekannter Fehler')}
    except (asyncssh.Error, OSError) as e:
        # Wenn die Verbindung sofort gekappt wird, war der Reboot sehr wahrscheinlich erfolgreich
        return {"host_id": host["id"], "name": name, "status": "ok", "note": f"Reboot (verbindung beendet): {e}"}
