from __future__ import annotations
import asyncssh, logging, re
from collections import deque
from typing import Dict, Any, Tuple
from .ssh_connection import connect_host
from . import credentials
from .remote_process import capture, stream, CommandExit, RemoteTimeoutError, STREAM_TIMEOUT

logger = logging.getLogger(__name__)

UPGRADE_FAILURE_BUFFER_LINES = 20
UPGRADE_FAILURE_LINE_LENGTH = 1000

async def _run(conn: asyncssh.SSHClientConnection, cmd: str, timeout: int = 90) -> Tuple[int, str, str]:
    try:
        return await capture(conn, cmd, timeout)
    except RemoteTimeoutError as exc:
        return 124, "", str(exc)

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

class UpdateCheckError(RuntimeError):
    pass


def _raise_check_error(step: str, code: int, stderr: str) -> None:
    if code == 124:
        raise UpdateCheckError(f"Zeitüberschreitung bei {step}.")
    detail = (stderr or "").strip().splitlines()
    suffix = f": {detail[0][:300]}" if detail else ""
    raise UpdateCheckError(f"{step} fehlgeschlagen (Exitcode {code}){suffix}")


async def _check_debian(conn):
    code, _, err = await _run(conn, "bash -lc 'export LC_ALL=C LANG=C; sudo -n apt-get -qq update --error-on=any'")
    if code != 0:
        _raise_check_error("apt-get update", code, err)

    index_note = err.strip()
    code, out, err = await _run(conn, "bash -lc 'export LC_ALL=C LANG=C; apt-get -s dist-upgrade'")
    if code != 0:
        _raise_check_error("apt-get -s dist-upgrade", code, err)
    err = "\n".join(note for note in (index_note, err.strip()) if note)

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
    code, out, err = await _run(conn, "sudo -n dnf -q check-update")
    if code == 0:
        return 0, err.strip()
    if code != 100:
        _raise_check_error("dnf check-update", code, err)

    package_line = re.compile(r"^\S+\.\S+\s+\S+\s+\S+(?:\s+.*)?$")
    n = sum(1 for line in out.splitlines() if package_line.match(line.strip()))
    return n, err.strip()

async def _check_arch(conn):
    code, _, err = await _run(conn, "command -v checkupdates")
    if code != 0:
        if code == 124:
            _raise_check_error("Prüfung auf checkupdates", code, err)
        raise UpdateCheckError(
            "checkupdates ist nicht verfügbar; bitte pacman-contrib installieren."
        )

    code, out, err = await _run(conn, "checkupdates")
    if code == 2:
        return 0, err.strip()
    if code != 0:
        _raise_check_error("checkupdates", code, err)
    n = sum(1 for line in out.splitlines() if line.strip())
    return n, err.strip()

async def check_updates_for_host(host: Dict[str, Any]) -> Dict[str, Any]:
    name = host.get("name") or f"id:{host['id']}"
    ip, user, _ = credentials.normalize_target(host)
    if not ip or not user:
        return {"host_id": host["id"], "name": name, "status": "error", "note": "IP/User fehlt"}

    try:
        async with connect_host(host) as conn:
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
    except UpdateCheckError as e:
        return {"host_id": host["id"], "name": name, "status": "error", "note": str(e)}
    except (asyncssh.Error, OSError) as e:
        return {"host_id": host["id"], "name": name, "status": "error", "note": f"SSH: {e}"}

# ---------- Simulation (Dry-Run) ----------

async def _sim_debian(conn):
    code, _, err = await _run(conn, "bash -lc 'export LC_ALL=C LANG=C; sudo -n apt-get -qq update --error-on=any'")
    if code != 0:
        _raise_check_error("apt-get update", code, err)
    index_note = err.strip()
    code, out, err = await _run(conn, "bash -lc 'export LC_ALL=C LANG=C; apt-get -s dist-upgrade'")
    if code != 0:
        _raise_check_error("apt-get -s dist-upgrade", code, err)
    err = "\n".join(note for note in (index_note, err.strip()) if note)
    n = sum(1 for line in out.splitlines() if line.startswith("Inst "))
    return n, out, err


async def _sim_rpm(conn):
    # check-update has unambiguous documented 0/100/1 exit semantics.
    # This is an available-update preview, not a dependency transaction plan.
    code, out, err = await _run(conn, "sudo -n dnf -q --refresh check-update")
    if code not in (0, 100):
        _raise_check_error("dnf check-update", code, err)
    package_line = re.compile(r"^\S+\.\S+\s+\S+\s+\S+(?:\s+.*)?$")
    n = sum(1 for line in out.splitlines() if package_line.match(line.strip())) if code == 100 else 0
    return n, out, "Verfügbare Updates; kein vollständiger DNF-Transaktionsplan. " + err


async def _sim_arch(conn):
    code, out, err = await _run(conn, "checkupdates")
    if code not in (0, 2):
        _raise_check_error("checkupdates", code, err)
    return (0 if code == 2 else len(out.splitlines())), out, err

async def simulate_upgrade_for_host(host: Dict[str, Any]) -> Dict[str, Any]:
    """Gibt geplante Paketupdates zurück (ohne Änderungen)."""
    name = host.get("name") or f"id:{host['id']}"
    ip, user, _ = credentials.normalize_target(host)
    if not ip or not user:
        return {"host_id": host["id"], "name": name, "status": "error", "note": "IP/User fehlt"}

    try:
        async with connect_host(host) as conn:
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
    except (asyncssh.Error, OSError, UpdateCheckError) as e:
        return {"host_id": host["id"], "name": name, "status": "error", "note": f"SSH: {e}"}

# ---------- Upgrade (mit Live-Streaming) ----------

async def _stream(conn, cmd: str, timeout: int = STREAM_TIMEOUT):
    async for event in stream(conn, cmd, timeout):
        yield event

async def _upgrade_debian(conn, use_sudo: bool):
    prefix = "sudo -n " if use_sudo else ""

    # Paketlisten aktualisieren und Ausgabe/Fehler ebenfalls weiterreichen.
    update_rc = 1
    async for line in _stream(
        conn,
        f"{prefix}apt-get update --error-on=any -y -o=Dpkg::Use-Pty=0"
    ):
        yield line
        if isinstance(line, CommandExit):
            update_rc = line.status

    # Wenn schon apt-get update scheitert, kein dist-upgrade mehr starten.
    if update_rc != 0:
        return

    # Bei root können wir DEBIAN_FRONTEND direkt setzen.
    # Bei sudo darf DEBIAN_FRONTEND ohne SETENV-Regel nicht übergeben werden.
    # Darum verwenden wir hier apt/dpkg-Optionen, welche Konfigurationsdatei-
    # Rückfragen automatisch mit der bestehenden Konfiguration beantworten.
    if use_sudo:
        cmd = (
            f"{prefix}apt-get -y dist-upgrade "
            "-o=Dpkg::Use-Pty=0 "
            "-o=Dpkg::Options::=--force-confdef "
            "-o=Dpkg::Options::=--force-confold"
        )
    else:
        cmd = (
            "DEBIAN_FRONTEND=noninteractive "
            "apt-get -y dist-upgrade "
            "-o=Dpkg::Use-Pty=0 "
            "-o=Dpkg::Options::=--force-confdef "
            "-o=Dpkg::Options::=--force-confold"
        )

    async for line in _stream(conn, cmd):
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
    ip, user, _ = credentials.normalize_target(host)
    if not ip or not user:
        # Ergebnis "yielden", nicht returnen
        yield {"type": "result", "result": {"status": "error", "note": "IP/User fehlt"}}
        return

    dispatched = False
    try:
        async with connect_host(host) as conn:
            distro = await _detect_distro(conn)
            # nur sudo verwenden, wenn wir NICHT als root eingeloggt sind
            use_sudo = (user != "root")
            if distro == "debian":
                gen = _upgrade_debian(conn, use_sudo)
            elif distro == "rpm":
                gen = _upgrade_rpm(conn)
            elif distro == "arch":
                gen = _upgrade_arch(conn)
            else:
                yield {"type": "result", "result": {"status": "error", "note": "Unbekannte Distro"}}
                return

            dispatched = True
            rc = 1
            recent_output = deque(maxlen=UPGRADE_FAILURE_BUFFER_LINES)
            async for line in gen:
                # Zeilen streamen
                if isinstance(line, CommandExit):
                    rc = line.status
                else:
                    recent_output.append(line[:UPGRADE_FAILURE_LINE_LENGTH])
                    yield {"type": "line", "line": line}

            if rc != 0:
                step = {
                    "debian": "apt-get dist-upgrade",
                    "rpm": "dnf upgrade",
                    "arch": "pacman -Syu",
                }.get(distro, f"{distro} upgrade")
                output = "\n".join(recent_output) or "(keine Remote-Ausgabe)"
                logger.error(
                    "Upgrade fehlgeschlagen: host=%s, schritt=%s, exitcode=%d; "
                    "letzte %d Ausgabelinien:\n%s",
                    name,
                    step,
                    rc,
                    len(recent_output),
                    output,
                )

            # Finales Ergebnis liefern
            note = f"rc={rc}"
            if rc != 0 and recent_output:
                note += f"; letzte Ausgabe: {recent_output[-1][:200]}"
            yield {"type": "result", "result": {"status": "ok" if rc == 0 else "error", "note": note, "distro": distro}}
            return
    except (asyncssh.Error, OSError, UpdateCheckError) as e:
        yield {"type": "result", "result": {"status": "unknown" if dispatched else "error", "note": (f"Remote-Zustand unbekannt: {e}. Vor erneutem Start am Host prüfen." if dispatched else f"SSH: {e}")}}
        return
# ---------- Autoremove (Simulation + Live-Run) ----------

async def _sim_autoremove_debian(conn):
    # Simulation (sprachunabhängig)
    code, out, err = await _run(conn, "bash -lc 'export LC_ALL=C LANG=C; apt-get -s autoremove --purge'")
    if code != 0:
        _raise_check_error("apt-get -s autoremove --purge", code, err)
    # Zählen: Zeilen, die mit 'Remv ' beginnen, oder Summary '... to remove'
    n = sum(1 for ln in out.splitlines() if ln.startswith("Remv "))
    if n == 0:
        m = re.search(r'(\d+)\s+to remove', out)
        if m:
            n = int(m.group(1))
    return n, out, err

async def simulate_autoremove_for_host(host: Dict[str, Any]) -> Dict[str, Any]:
    name = host.get("name") or f"id:{host['id']}"
    ip, user, _ = credentials.normalize_target(host)
    if not ip or not user:
        return {"host_id": host["id"], "name": name, "status": "error", "note": "IP/User fehlt"}

    try:
        async with connect_host(host) as conn:
            distro = await _detect_distro(conn)
            if distro != "debian":
                return {"host_id": host["id"], "name": name, "status": "error", "note": "Autoremove nur Debian implementiert"}
            n, details, note = await _sim_autoremove_debian(conn)
            return {"host_id": host["id"], "name": name, "status": "ok", "distro": distro, "packages": n, "details": details, "note": note or ""}
    except (asyncssh.Error, OSError, UpdateCheckError) as e:
        return {"host_id": host["id"], "name": name, "status": "error", "note": f"SSH: {e}"}

async def _run_autoremove_debian(conn):
    async for line in _stream(conn, "sudo -n apt-get -y autoremove --purge -o=Dpkg::Use-Pty=0"):
        yield line

async def autoremove_host_stream(host: Dict[str, Any]):
    """Async-Generator: liefert {'type':'line','line':...} und am Ende {'type':'result',...}."""
    name = host.get("name") or f"id:{host['id']}"
    ip, user, _ = credentials.normalize_target(host)
    if not ip or not user:
        yield {"type": "result", "result": {"status": "error", "note": "IP/User fehlt"}}
        return
    dispatched = False
    try:
        async with connect_host(host) as conn:
            distro = await _detect_distro(conn)
            if distro != "debian":
                yield {"type": "result", "result": {"status": "error", "note": "Autoremove nur Debian implementiert"}}
                return
            dispatched = True
            rc = 1
            async for line in _run_autoremove_debian(conn):
                if isinstance(line, CommandExit):
                    rc = line.status
                else:
                    yield {"type": "line", "line": line}
            yield {"type": "result", "result": {"status": "ok" if rc == 0 else "error", "note": f"rc={rc}", "distro": distro}}
            return
    except (asyncssh.Error, OSError, UpdateCheckError) as e:
        yield {"type": "result", "result": {"status": "unknown" if dispatched else "error", "note": (f"Remote-Zustand unbekannt: {e}. Vor erneutem Start am Host prüfen." if dispatched else f"SSH: {e}")}}
        return

# ---------- Reboot ----------

async def reboot_host(host: Dict[str, Any]) -> Dict[str, Any]:
    """Plant einen leicht verzögerten Reboot und bestätigt dessen Exitcode."""
    name = host.get("name") or f"id:{host['id']}"
    ip, user, _ = credentials.normalize_target(host)
    if not ip or not user:
        return {"host_id": host["id"], "name": name, "status": "error", "note": "IP/User fehlt"}

    dispatched = False
    try:
        async with connect_host(host) as conn:
            code, _, err = await _run(conn, "command -v systemd-run", timeout=10)
            if code == 124:
                return {
                    "host_id": host["id"], "name": name, "status": "error",
                    "note": "Reboot konnte nicht bestätigt werden: Zeitüberschreitung bei der Prüfung auf systemd-run.",
                }
            if code != 0:
                return {
                    "host_id": host["id"], "name": name, "status": "error",
                    "note": "Reboot konnte nicht bestätigt werden: systemd-run ist nicht verfügbar.",
                }

            prefix = "" if user == "root" else "sudo -n "
            cmd = f"{prefix}systemd-run --quiet --on-active=2s systemctl reboot"
            dispatched = True
            code, _, err = await _run(conn, cmd, timeout=10)
            if code == 0:
                return {
                    "host_id": host["id"], "name": name, "status": "ok",
                    "note": "Reboot erfolgreich eingeplant",
                }

            if code == 124:
                note = "Reboot konnte nicht bestätigt werden: Remote-Zustand unbekannt; Zeitüberschreitung beim Einplanen."
            else:
                detail = (err or "").strip().splitlines()
                suffix = f": {detail[0][:300]}" if detail else ""
                note = f"Reboot konnte nicht eingeplant werden (Exitcode {code}){suffix}"
            return {"host_id": host["id"], "name": name,
                    "status": "unknown" if code == 124 else "error", "note": note}
    except (asyncssh.Error, OSError, UpdateCheckError) as e:
        return {
            "host_id": host["id"], "name": name, "status": "unknown" if dispatched else "error",
            "note": f"Reboot konnte nicht bestätigt werden: SSH: {e}" +
                    (". Remote-Zustand unbekannt; vor erneutem Start am Host prüfen." if dispatched else ""),
        }
