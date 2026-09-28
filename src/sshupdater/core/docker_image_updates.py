"""Bounded, read-only image checks on an existing SSH connection.

No image layers are fetched. Only explicit, typed manifest descriptors are
compared; image IDs and RepoDigests are retained as evidence, never guessed.
"""
import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import hashlib
from pathlib import PurePosixPath
import re
import shlex

from .remote_process import capture, RemoteTimeoutError

TIMEOUT = 20
TOTAL_TIMEOUT = 180
MAX_PROJECTS = 32
MAX_CONTAINERS = 64
INDEX = 'application/vnd.oci.image.index.v1+json'
LIST = 'application/vnd.docker.distribution.manifest.list.v2+json'
MANIFEST = 'application/vnd.oci.image.manifest.v1+json'
V2 = 'application/vnd.docker.distribution.manifest.v2+json'
INDEX_TYPES = (INDEX, LIST)
MANIFEST_TYPES = (MANIFEST, V2)
DIGEST = re.compile(r'sha256:[0-9a-f]{64}')
LABEL = 'com.docker.compose.'

REASONS = {
    'context': 'Compose-Kontext nicht eindeutig rekonstruierbar (unterstützt wird eine statische lokale Datei ohne Overrides, Interpolation oder Profile).',
    'buildx_missing': 'Docker Buildx ist nicht verfügbar oder konnte nicht gestartet werden.',
    'auth': 'Registry-Authentifizierung oder Leseberechtigung fehlt; vorhandenen Registry-Zugang am Host prüfen.',
    'not_found': 'Image oder Tag in der Registry nicht gefunden.',
    'rate_limit_backoff': 'Registry-Rate-Limit – erneute Prüfung später möglich.',
    'rate_limit': 'Registry-Rate-Limit erreicht (HTTP 429).',
    'network': 'Registry/Netzwerk nicht erreichbar oder TLS-Verbindung fehlgeschlagen.',
    'timeout': 'Zeitlimit der Image-Prüfung überschritten.',
    'command_error': 'Read-only-Abfrage fehlgeschlagen.',
    'local_image_unavailable': 'Lokales Image über Container-Image-ID nicht inspizierbar; kein eindeutig gültiger Container-Manifest-Descriptor als Ersatz verfügbar.',
    'invalid_output': 'Ungültige oder unvollständige strukturierte Docker-Ausgabe.',
    'identity': 'Keine eindeutig vergleichbaren Manifest-Identitäten vorhanden; Image-ID/RepoDigests werden nicht als Index-Digest interpretiert.',
    'platform': 'Plattform nicht eindeutig bestimmt oder Compose-Plattform weicht vom laufenden Image ab.',
    'drift': 'Compose-Image und tatsächlich verwendete Container-Referenz stimmen nicht überein.',
    'not_running': 'Kein laufender Container für diesen Service vorhanden.',
    'build_image': 'build und image gemeinsam: Registry-Herkunft des laufenden Images ist nicht eindeutig; eine automatische Prüfung wird derzeit nicht unterstützt.',
    'limit': 'Umfang der Prüfung überschreitet die unterstützte Grenze.',
}


class CheckFailure(Exception):
    def __init__(self, kind, command=None, exit_code=None):
        super().__init__(REASONS[kind])
        self.kind, self.command, self.exit_code = kind, command, exit_code


@dataclass
class ImageCheck:
    service: str = ''
    container_id: str = ''
    container_name: str = ''
    image: str = ''
    image_id: str = ''
    platform: str = ''
    os: str = ''
    architecture: str = ''
    variant: str = ''
    local_descriptor: dict | None = None
    identity_source: str = ''
    repo_digests: list[str] = field(default_factory=list)
    remote_descriptor: dict | None = None
    comparison_level: str = ''
    status: str = 'uncheckable'
    reason: str = ''
    note: str = ''
    command: str | None = None
    exit_code: int | None = None

    def fail(self, error):
        self.status = 'uncheckable'
        self.reason, self.note = error.kind, str(error)
        self.command, self.exit_code = error.command, error.exit_code
        return self


def summary(images):
    """Count distinct updated image/platform pairs, not replicas."""
    updates = len({(r['image'], r['platform']) for r in images if r['status'] == 'update_available'})
    unknown = sum(r['status'] == 'uncheckable' for r in images)
    builds = sum(r['status'] == 'local_build' for r in images)
    pinned = sum(r['status'] == 'digest_pinned' for r in images)
    if updates:
        text = f'↑ {updates} Image-Update' + ('s' if updates != 1 else '')
        if unknown:
            text += f' · {unknown} nicht prüfbar'
        if builds:
            text += f' · {builds} lokale Builds'
        if pinned:
            text += f' · {pinned} Digest-fixiert'
        return text
    if unknown or not images:
        return '⚠ Prüfung unvollständig'
    if builds == len(images):
        return 'Lokaler Build'
    if pinned == len(images):
        return 'Digest-fixiert'
    if builds or pinned:
        parts = []
        if any(r['status'] == 'current' for r in images):
            parts.append('✓ prüfbare Images aktuell')
        if builds:
            parts.append(f'{builds} lokale Builds')
        if pinned:
            parts.append(f'{pinned} Digest-fixiert')
        return ' · '.join(parts)
    return '✓ aktuell'


def project_result(images):
    rows = [asdict(r) for r in images]
    return {'checked_at': datetime.now(timezone.utc).isoformat(),
            'summary': summary(rows), 'images': rows}


def failure_kind(code, message):
    text = message.lower()
    if code == 124:
        return 'timeout'
    if 'no such image:' in text:
        return 'local_image_unavailable'
    if any(s in text for s in ('429', 'toomanyrequests', 'too many requests', 'rate limit')):
        return 'rate_limit'
    if any(s in text for s in ('unauthorized', 'authentication required', 'access denied', 'denied:', 'forbidden', '401', '403', 'credentials')):
        return 'auth'
    if any(s in text for s in ('manifest unknown', 'name unknown', 'not found', '404')):
        return 'not_found'
    if any(s in text for s in ('timeout', 'timed out', 'deadline exceeded')):
        return 'timeout'
    if any(s in text for s in ('no such host', 'connection refused', 'network', 'tls', 'x509', 'connection reset')):
        return 'network'
    return 'command_error'


def reference(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9][a-zA-Z0-9._/:@+-]*', value) or '://' in value:
        raise CheckFailure('context')
    if '@' in value:
        base, digest = value.split('@', 1)
        if not DIGEST.fullmatch(digest):
            raise CheckFailure('context')
    else:
        base, digest = value, None
    first = base.split('/')[0]
    if '/' not in base or not ('.' in first or ':' in first or first == 'localhost'):
        base = 'docker.io/' + base
    if base.startswith('docker.io/') and base.count('/') == 1:
        base = base.replace('docker.io/', 'docker.io/library/', 1)
    if digest:
        return base + '@' + digest
    if ':' not in base.rsplit('/', 1)[-1]:
        base += ':latest'
    return base


def descriptor(value):
    if not isinstance(value, dict) or value.get('mediaType') not in INDEX_TYPES + MANIFEST_TYPES:
        raise CheckFailure('identity')
    if not isinstance(value.get('digest'), str) or not DIGEST.fullmatch(value['digest']):
        raise CheckFailure('identity')
    return {'mediaType': value['mediaType'], 'digest': value['digest']}


def container_manifest_metadata(container, explicit_platform=''):
    """Use only the engine's typed platform manifest, never a label or Image ID."""
    value = container.get('ImageManifestDescriptor')
    local = descriptor(value)
    if local['mediaType'] not in MANIFEST_TYPES:
        raise CheckFailure('identity')
    platform = value.get('platform')
    if not isinstance(platform, dict):
        raise CheckFailure('platform')
    parts = [platform.get('os'), platform.get('architecture')]
    if 'variant' in platform:
        parts.append(platform['variant'])
    if any(not isinstance(p, str) or p == 'unknown' or not re.fullmatch(r'[a-z0-9][a-z0-9_.-]*', p) for p in parts):
        raise CheckFailure('platform')
    if container.get('Platform') and container['Platform'] != parts[0]:
        raise CheckFailure('platform')
    if explicit_platform:
        if not isinstance(explicit_platform, str):
            raise CheckFailure('platform')
        wanted = explicit_platform.split('/')
        if (len(wanted) not in (2, 3) or wanted[:2] != parts[:2]
                or (len(wanted) == 3 and wanted != parts)):
            raise CheckFailure('platform')
    return {'Id': container['Image'], 'Descriptor': local,
            'Os': parts[0], 'Architecture': parts[1],
            'Variant': parts[2] if len(parts) == 3 else '', 'RepoDigests': []}


def compare(local, remote, platform, explicit_platform=''):
    """Compare equal descriptor types; no ID/config/index coercion."""
    parts = platform.split('/')
    if len(parts) not in (2, 3) or any(not p or p == 'unknown' for p in parts):
        raise CheckFailure('platform')
    if explicit_platform:
        wanted = explicit_platform.split('/')
        if (len(wanted) not in (2, 3) or any(not p or p == 'unknown' for p in wanted)
                or wanted[:2] != parts[:2] or (len(wanted) == 3 and wanted != parts)):
            raise CheckFailure('platform')
    old, new = descriptor(local), descriptor(remote)
    if old['mediaType'] in INDEX_TYPES:
        if old['mediaType'] != new['mediaType']:
            raise CheckFailure('identity')
        level = 'index'
    elif new['mediaType'] in INDEX_TYPES:
        candidates = []
        for entry in remote.get('manifests', []):
            p = entry.get('platform', {})
            if p.get('os') != parts[0] or p.get('architecture') != parts[1]:
                continue
            if (p.get('variant') or '') != (parts[2] if len(parts) == 3 else ''):
                continue
            if entry.get('annotations', {}).get('vnd.docker.reference.type') == 'attestation-manifest':
                continue
            candidates.append(entry)
        if len(candidates) != 1:
            raise CheckFailure('platform')
        new = descriptor(candidates[0])
        if new['mediaType'] != old['mediaType']:
            raise CheckFailure('identity')
        level = 'platform_manifest'
    else:
        # A standalone remote manifest has no provable platform descriptor here.
        # Index -> platform selection is supported; this case is deferred.
        raise CheckFailure('identity')
    return ('current' if old['digest'] == new['digest'] else 'update_available'), level, new


def compose_command(name, path):
    """Shared explicit Compose context; callers append only their own operation."""
    return ['env', 'COMPOSE_PROFILES=', 'COMPOSE_ENV_FILES=', 'COMPOSE_DISABLE_ENV_FILE=1',
            'docker', 'compose', '--project-name', name,
            '--project-directory', str(PurePosixPath(path).parent),
            '--env-file', '/dev/null', '-f', path]


def compose_identity(source, config):
    """Keep no Compose secrets: only content digests and image/platform bindings."""
    return (hashlib.sha256(source.encode('utf-8')).hexdigest(),
            hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest(),
            tuple(sorted((name, spec.get('image', ''), spec.get('platform', ''))
                         for name, spec in config['services'].items())))


class Checker:
    def __init__(self, conn, registry_session=None, host_scope=None):
        from .registry_session import RegistrySession
        self.registry_session = registry_session if registry_session is not None else RegistrySession()
        self.host_scope = host_scope if host_scope is not None else object()
        self.conn = conn
        self.registry = {}
        self.local = {}
        self.buildx = None

    async def run(self, args, *, parse=True):
        command = shlex.join(args)
        try:
            code, out, err = await capture(self.conn, command, TIMEOUT)
        except (RemoteTimeoutError, TimeoutError):
            raise CheckFailure('timeout', command) from None
        except Exception:
            raise CheckFailure('command_error', command) from None
        if code:
            # Do not retain stderr: registry URLs/helper errors can contain secrets.
            raise CheckFailure(failure_kind(code, err or out), command, code)
        if not parse:
            return out
        try:
            return json.loads(out)
        except (ValueError, TypeError, RecursionError):
            raise CheckFailure('invalid_output', command, code) from None

    async def remote(self, ref, platform):
        ref = reference(ref)
        cache_key, limit_key = self.registry_session.keys(ref, self.host_scope)
        cached = self.registry_session.get(cache_key)
        if cached is not None:
            return cached
        if self.registry_session.blocked(limit_key):
            raise CheckFailure('rate_limit_backoff')
        if self.buildx is None:
            try:
                await self.run(['docker', 'buildx', 'version'], parse=False)
                self.buildx = True
            except CheckFailure as exc:
                self.buildx = CheckFailure('buildx_missing', exc.command, exc.exit_code)
        if isinstance(self.buildx, CheckFailure):
            raise self.buildx
        if ref in self.registry:
            raise self.registry[ref]
        try:
            remote = await self.run(
                ['docker', 'buildx', 'imagetools', 'inspect', ref, '--format', '{{json .Manifest}}'])
            return self.registry_session.put(cache_key, remote)
        except CheckFailure as exc:
            if exc.kind == 'rate_limit':
                self.registry_session.rate_limit(limit_key)
            self.registry[ref] = exc
            raise

    async def image(self, container, service, spec, *, local_only=False):
        result = ImageCheck(service=service, container_id=container.get('Id', ''),
                            container_name=container.get('Name', '').lstrip('/'),
                            image=spec.get('image', ''), image_id=container.get('Image', ''))
        try:
            if 'build' in spec:
                if result.image:
                    raise CheckFailure('build_image')
                result.status = 'local_build'
                return result
            ref = reference(result.image)
            if ref != reference(container.get('Config', {}).get('Image')):
                raise CheckFailure('drift')
            if not DIGEST.fullmatch(result.image_id):
                raise CheckFailure('identity')
            try:
                if result.image_id not in self.local:
                    records = await self.run(['docker', 'image', 'inspect', result.image_id])
                    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
                        raise CheckFailure('invalid_output')
                    self.local[result.image_id] = records[0]
                metadata = self.local[result.image_id]
                result.identity_source = 'image_inspect'
            except CheckFailure as exc:
                # Narrow fallback: only the daemon's explicit missing-image error.
                # Do not mask timeouts, permissions, invalid JSON or SSH failures.
                if exc.kind != 'local_image_unavailable':
                    raise
                try:
                    metadata = container_manifest_metadata(container, spec.get('platform', ''))
                except CheckFailure:
                    raise exc from None
                result.identity_source = 'container_manifest_descriptor'
                # Keep fallback evidence container-scoped, out of the image cache.
            if metadata.get('Id') != result.image_id:
                raise CheckFailure('identity')
            result.os = metadata.get('Os') or ''
            result.architecture = metadata.get('Architecture') or ''
            result.variant = metadata.get('Variant') or ''
            result.platform = '/'.join(p for p in (result.os, result.architecture, result.variant) if p)
            result.repo_digests = metadata.get('RepoDigests') or []
            result.local_descriptor = metadata.get('Descriptor')
            if '@' in ref:
                result.status = 'digest_pinned'
                return result
            descriptor(result.local_descriptor)
            if local_only:
                result.status = 'local_identity'
                return result
            remote = await self.remote(ref, result.platform)
            result.status, result.comparison_level, result.remote_descriptor = compare(
                result.local_descriptor, remote, result.platform, spec.get('platform', ''))
        except CheckFailure as exc:
            result.fail(exc)
        except Exception:
            result.fail(CheckFailure('invalid_output'))
        return result

    async def project(self, project):
        try:
            return await self._project(project)
        except CheckFailure as exc:
            return project_result([ImageCheck().fail(exc)])
        except Exception:
            return project_result([ImageCheck().fail(CheckFailure('invalid_output'))])

    async def resolve_project(self, project):
        name = project['name']
        paths = project.get('config_files') or []
        if (len(paths) != 1 or not isinstance(paths[0], str) or not paths[0].startswith('/')
                or ',' in paths[0] or '\n' in paths[0] or '\r' in paths[0]
                or project.get('config_files_raw') not in (None, paths[0])):
            raise CheckFailure('context')
        path = paths[0]
        workdir = str(PurePosixPath(path).parent)
        # Conservative admission gate only, NOT a YAML interpreter. Compose does
        # all parsing. Reject indirection before config can fetch remote includes.
        source = await self.run(['cat', '--', path], parse=False)
        if (any(c in source for c in ('$','\\','!', '&', '*', '%'))
                or re.search(r'include|extends|profiles|env_file|provider|models|pre_start|post_start|pre_stop', source, re.I)):
            raise CheckFailure('context')
        ids_text = await self.run(['docker', 'container', 'ls', '--quiet', '--no-trunc',
                                  '--filter', f'label={LABEL}project={name}'], parse=False)
        ids = ids_text.split()
        if not ids:
            raise CheckFailure('not_running')
        if len(ids) > MAX_CONTAINERS:
            raise CheckFailure('limit')
        if any(not re.fullmatch(r'[0-9a-f]{64}', value) for value in ids):
            raise CheckFailure('invalid_output')
        containers = await self.run(['docker', 'container', 'inspect', *ids])
        if not isinstance(containers, list) or len(containers) != len(ids):
            raise CheckFailure('invalid_output')
        by_service = {}
        for container in containers:
            labels = container.get('Config', {}).get('Labels') or {}
            if (container.get('Id') not in ids or not container.get('State', {}).get('Running')
                    or labels.get(LABEL + 'project') != name
                    or labels.get(LABEL + 'project.config_files') != path
                    or labels.get(LABEL + 'project.working_dir') != workdir
                    # Our explicit --env-file /dev/null is a known empty context.
                    or labels.get(LABEL + 'project.environment_file') not in (None, '', '/dev/null')
                    or labels.get(LABEL + 'oneoff', 'False').lower() != 'false'):
                raise CheckFailure('context')
            service = labels.get(LABEL + 'service')
            if not service:
                raise CheckFailure('context')
            by_service.setdefault(service, []).append(container)
        config = await self.run(compose_command(name, path) +
                                ['config', '--format', 'json', '--no-interpolate', '--no-env-resolution'])
        services = config.get('services')
        if not isinstance(services, dict) or not services or not set(by_service) <= set(services):
            raise CheckFailure('context')
        if len(services) > MAX_CONTAINERS:
            raise CheckFailure('limit')
        return path, source, config, by_service

    async def _project(self, project):
        path, source, config, by_service = await self.resolve_project(project)
        services = config['services']
        results = []
        for service, spec in services.items():
            if not isinstance(spec, dict) or spec.get('profiles') or spec.get('env_file'):
                raise CheckFailure('context')
            if service not in by_service:
                results.append(ImageCheck(service=service, image=spec.get('image', '')).fail(CheckFailure('not_running')))
                continue
            for container in by_service[service]:
                results.append(await self.image(container, service, spec))
        # Detect a concurrent Compose file edit; do not publish a stale conclusion.
        if await self.run(['cat', '--', path], parse=False) != source:
            raise CheckFailure('context')
        result = project_result(results)
        result['compose_identity'] = compose_identity(source, config)
        return result


async def check_projects(conn, projects, registry_session=None, host_scope=None):
    """One sequential host run, with per-run caches and a total deadline."""
    checker = Checker(conn, registry_session, host_scope)
    results = {}
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT):
            for index, project in enumerate(projects):
                if index >= MAX_PROJECTS:
                    results[project['name']] = project_result([ImageCheck().fail(CheckFailure('limit'))])
                else:
                    results[project['name']] = await checker.project(project)
    except TimeoutError:
        for project in projects:
            results.setdefault(project['name'], project_result([ImageCheck().fail(CheckFailure('timeout'))]))
    return results
