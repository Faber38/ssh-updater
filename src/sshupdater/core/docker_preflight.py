"""Live LOCAL identity checks against an existing plan. Never consult a registry."""
import asyncio
from . import docker_image_updates as images, db
from .ssh_connection import connect_host

TOTAL_TIMEOUT = 180
MESSAGES = {
    'plan': 'Plan enthält keine ausreichenden Vergleichsdaten. Normale Prüfung und neue Vorschau erforderlich.',
    'host': 'Host-Verbindungskontext hat sich seit der Vorschau geändert.',
    'ssh': 'SSH-Verbindung oder Host-Key-Prüfung fehlgeschlagen.',
    'file': 'Compose-Datei nicht lesbar oder seit der Vorschau geändert.',
    'config': 'Compose-Konfiguration hat sich seit der Vorschau geändert.',
    'project': 'Projekt konnte nicht mehr eindeutig zugeordnet werden.',
    'service': 'Service ist nicht mehr vorhanden oder seine Zuordnung hat sich geändert.',
    'image': 'Image-Referenz hat sich seit der Vorschau geändert.',
    'platform': 'Plattform hat sich seit der Vorschau geändert.',
    'container': 'Containerzustand hat sich seit der Vorschau geändert.',
    'identity': 'Lokale Image-/Descriptoridentität hat sich seit der Vorschau geändert.',
    'timeout': 'Zeitlimit des Docker-Preflights überschritten.',
    'cancelled': 'Docker-Preflight abgebrochen. Keine Freigabe.',
    'error': 'Read-only-Preflight fehlgeschlagen; normale Prüfung und neue Vorschau erforderlich.',
}


class PreflightFailure(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(MESSAGES[reason])


def failed(reason, host_id=None, project=None):
    return dict(status='failed', reason=reason, note=MESSAGES[reason], host_id=host_id, project=project)


def connection_identity(host):
    return tuple(host.get(key) for key in ('primary_ip', 'user', 'port', 'auth_method', 'key_path'))


async def verify_project(conn, expected):
    if (len(expected.compose_identity) != 3 or not expected.images
            or any(not i[1] or not i[6] for i in expected.images)):
        raise PreflightFailure('plan')
    checker = images.Checker(conn)
    project = dict(name=expected.name, config_files=list(expected.paths), config_files_raw=expected.raw_paths)
    try:
        path, source, config, by_service = await checker.resolve_project(project)
    except images.CheckFailure as exc:
        if exc.kind == 'timeout':
            raise PreflightFailure('timeout') from None
        if exc.command and exc.command.startswith('cat '):
            raise PreflightFailure('file') from None
        if exc.kind == 'not_running':
            raise PreflightFailure('container') from None
        raise PreflightFailure('project') from None
    services = config['services']
    if config.get('name', expected.name) != expected.name:
        raise PreflightFailure('project')
    # Explicit bindings are compared before hashes to give a specific safe reason.
    old_bindings = {name: (ref, platform) for name, ref, platform in expected.compose_identity[2]}
    if set(old_bindings) != set(services):
        raise PreflightFailure('service')
    for name, (ref, platform) in old_bindings.items():
        spec = services[name]
        if not isinstance(spec, dict):
            raise PreflightFailure('config')
        if spec.get('image', '') != ref:
            raise PreflightFailure('image')
        if spec.get('platform', '') != platform:
            raise PreflightFailure('platform')
    identity = images.compose_identity(source, config)
    if identity[0] != expected.compose_identity[0]:
        raise PreflightFailure('file')
    if identity != expected.compose_identity:
        raise PreflightFailure('config')
    expected_containers = {(i[0], i[1]) for i in expected.images}
    actual_containers = [(service, c.get('Id')) for service, containers in by_service.items() for c in containers]
    if len(set(actual_containers)) != len(actual_containers) or set(actual_containers) != expected_containers:
        raise PreflightFailure('container')
    for old in expected.images:
        service, cid, name, ref, platform, _, image_id, _, local, _ = old
        container = next(c for c in by_service[service] if c['Id'] == cid)
        live = await checker.image(container, service, services[service], local_only=True)
        if live.status != 'local_identity':
            raise PreflightFailure('identity')
        if live.container_name != name or live.container_id != cid:
            raise PreflightFailure('container')
        if live.image != ref:
            raise PreflightFailure('image')
        if live.platform != platform:
            raise PreflightFailure('platform')
        desc = images.descriptor(live.local_descriptor)
        if live.image_id != image_id or (desc['mediaType'], desc['digest']) != local:
            raise PreflightFailure('identity')
    # Detect edits during the preflight itself, using the same read as normal checks.
    if await checker.run(['cat', '--', path], parse=False) != source:
        raise PreflightFailure('file')


async def preflight(plan, hosts):
    host_id = project_name = None
    if plan is None or not plan.candidates:
        return failed('plan')
    rows = []
    try:
        for candidate in plan.candidates:
            expected = next(p for p in plan.selection if p.host_id == candidate.host_id and p.name == candidate.name)
            if (len(expected.compose_identity) != 3 or not expected.images
                    or any(not i[1] or not i[6] for i in expected.images)):
                raise PreflightFailure('plan')
        async with asyncio.timeout(TOTAL_TIMEOUT):
            for host_id in sorted({p.host_id for p in plan.candidates}):
                project_name = None
                host = hosts.get(host_id)
                candidates = [p for p in plan.candidates if p.host_id == host_id]
                if not host or any(connection_identity(host) != p.connection[:5] for p in candidates):
                    raise PreflightFailure('host')
                try:
                    async with connect_host(host) as conn:
                        for candidate in candidates:
                            project_name = candidate.name
                            # Verify every previously checked container in that project,
                            # including unchanged services; never silently update a plan.
                            expected = next(p for p in plan.selection if p.host_id == host_id and p.name == candidate.name)
                            try:
                                await verify_project(conn, expected)
                            except (PreflightFailure, TimeoutError, images.CheckFailure):
                                raise
                            except Exception:
                                raise PreflightFailure('error') from None
                            rows.extend(dict(host_id=host_id, host=host.get('name') or str(host_id),
                                             project=candidate.name, service=i[0], image=i[3], platform=i[4])
                                        for i in candidate.images)
                        db.get_connection_context(host)
                except db.HostConfigurationChanged:
                    raise PreflightFailure('host') from None
                except (PreflightFailure, TimeoutError, images.CheckFailure):
                    raise
                except Exception:
                    raise PreflightFailure('ssh') from None
        return dict(status='passed', note='Preflight erfolgreich – Update kann durchgeführt werden.', projects=rows)
    except PreflightFailure as exc:
        return failed(exc.reason, host_id, project_name)
    except TimeoutError:
        return failed('timeout', host_id, project_name)
    except Exception:
        return failed('error', host_id, project_name)
