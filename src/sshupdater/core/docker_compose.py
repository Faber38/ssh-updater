"""Read-only Compose discovery on an already authenticated SSH connection.

No sudo, legacy docker-compose fallback, or container-changing commands.
Availability is None when a failed probe cannot establish presence/absence.
"""
from dataclasses import dataclass, field
import json
import re

from .remote_process import capture, RemoteTimeoutError


PROBE_TIMEOUT = 10
COMMANDS = ("docker --version", "docker compose version", "docker compose ls --format json")


# Only fixed diagnostics cross the SSH/discovery boundary into session data.
DISCOVERY_REASONS = {
    "docker_missing": "Docker ist nicht verfügbar.",
    "compose_missing": "Docker Compose ist nicht verfügbar.",
    "permission_denied": "Berechtigung für die Docker-Abfrage verweigert.",
    "daemon_unreachable": "Docker-Daemon ist nicht erreichbar.",
    "timeout": "Zeitüberschreitung bei der Docker-Erkennung.",
    "invalid_output": "Die Ausgabe der Docker-Erkennung ist nicht interpretierbar.",
    "command_error": "Docker-Erkennung fehlgeschlagen.",
}


@dataclass
class ComposeProject:
    name: str
    status: str | None = None
    config_files: list[str] = field(default_factory=list)
    # Keep Docker's original string: comma-separated filenames are ambiguous.
    config_files_raw: str | None = None
    container_count: int | None = None
    container_states: dict[str, int] | None = None


@dataclass
class DockerComposeDiscovery:
    status: str = "command_error"
    docker_available: bool | None = None
    compose_available: bool | None = None
    docker_version: str | None = None
    compose_version: str | None = None
    projects: list[ComposeProject] = field(default_factory=list)
    command: str | None = None
    exit_code: int | None = None
    note: str = ""


def parse_projects(output: str) -> list[ComposeProject]:
    """Validate JSON without treating malformed output as an empty inventory.

    Accept an array, a single project object, and null (older empty outputs).
    Unknown fields are ignored; absent optional fields stay unknown.
    Counts are derived only from a fully recognized status summary.
    """
    rows = json.loads(output)
    if rows is None:
        return []
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        raise ValueError("Compose-Projektliste ist kein JSON-Array.")
    projects = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Compose-Projekt ist kein JSON-Objekt.")
        name = row.get("Name")
        status = row.get("Status")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Compose-Projektname fehlt oder ist ungültig.")
        if status is not None and not isinstance(status, str):
            raise ValueError("Compose-Projektstatus ist ungültig.")
        files = row.get("ConfigFiles")
        raw = files if isinstance(files, str) else None
        if files is None:
            files = []
        elif isinstance(files, str):
            files = [part.strip() for part in files.split(",") if part.strip()]
        elif not isinstance(files, list) or any(not isinstance(f, str) for f in files):
            raise ValueError("Compose-Konfigurationspfade sind ungültig.")
        states = None
        if status and re.fullmatch(r"\s*[a-zA-Z_-]+\(\d+\)(?:\s*,\s*[a-zA-Z_-]+\(\d+\))*\s*", status):
            states = {}
            for state, count in re.findall(r"([a-zA-Z_-]+)\((\d+)\)", status):
                states[state] = states.get(state, 0) + int(count)
        projects.append(ComposeProject(
            name=name, status=status, config_files=files, config_files_raw=raw,
            container_count=sum(states.values()) if states is not None else None,
            container_states=states,
        ))
    return projects


def _failure_status(step: int, code: int, detail: str) -> str:
    message = detail.lower()
    if code == 124:
        return "timeout"
    if "permission denied" in message or "access denied" in message or "operation not permitted" in message:
        return "permission_denied"
    if any(marker in message for marker in (
        "cannot connect to the docker daemon", "is the docker daemon running",
        "error during connect", "connection refused",
    )):
        return "daemon_unreachable"
    if step == 0 and (code == 127 or re.search(r"docker: (?:command )?not found", message)):
        return "docker_missing"
    if step == 1 and any(marker in message for marker in (
        "is not a docker command", "unknown command", "no such plugin",
    )):
        return "compose_missing"
    return "command_error"


async def discover(conn) -> DockerComposeDiscovery:
    """Best-effort discovery; failures never escape into the package check.

    Task cancellation deliberately propagates. Each command is bounded by the
    existing capture helper's output limit and a short individual deadline.
    Version output is retained verbatim rather than assuming a version format.
    """
    result = DockerComposeDiscovery()
    try:
        for step, command in enumerate(COMMANDS):
            result.command = command
            result.exit_code = None
            code, out, err = await capture(conn, command, PROBE_TIMEOUT)
            result.exit_code = code
            if code != 0:
                result.status = _failure_status(step, code, err.strip() or out.strip())
                result.note = DISCOVERY_REASONS[result.status]
                if result.status == "docker_missing":
                    result.docker_available = False
                elif result.status == "compose_missing":
                    result.compose_available = False
                return result
            if step == 0:
                result.docker_available = True
                result.docker_version = out.strip()
            elif step == 1:
                result.compose_available = True
                result.compose_version = out.strip()
            else:
                try:
                    result.projects = parse_projects(out)
                except (ValueError, TypeError, RecursionError):
                    result.status = "invalid_output"
                    result.note = DISCOVERY_REASONS[result.status]
                    return result
                result.note = ""
        result.status = "ok"
    except (RemoteTimeoutError, TimeoutError):
        result.status = "timeout"
        result.note = DISCOVERY_REASONS[result.status]
    except Exception:
        # Optional discovery must not break updates on SSH/channel errors or
        # unexpected client responses. BaseException/cancellation isn't caught.
        result.status = "command_error"
        result.note = DISCOVERY_REASONS[result.status]
    return result
