"""Phase 4c-2: consume confirmed RAM pull evidence; apply locally loaded images only."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import shlex
import re

from . import docker_preflight as preflight, docker_image_updates as images
from .docker_pull import container_snapshot
from .remote_process import capture
from .ssh_connection import connect_host
from ..docker_plan import proven

APPLY_TIMEOUT = 600


def prepared(state):
    """Validate complete pull coverage before accepting any apply work."""
    if not state or not state.get('plan'):
        return False
    plan, result = state['plan'], state.get('result', {})
    if (result.get('status') != 'pulled' or result.get('apply_pending') is not True
            or result.get('mutation_attempted') is not True or not plan.candidates):
        return False
    expected = set()
    for candidate in plan.candidates:
        original = next((p for p in plan.selection if (p.host_id, p.name) == (candidate.host_id, candidate.name)), None)
        if (original is None or not candidate.images
                or candidate != replace(original, images=tuple(i for i in original.images if proven(i)))):
            return False
        expected.update((candidate.host_id, candidate.name, i[0], i[3], i[4]) for i in candidate.images)
    rows = result.get('pulls', [])
    actual = {(r.get('host_id'), r.get('project'), r.get('service'), r.get('image'), r.get('platform')) for r in rows}
    return bool(expected == actual and len(rows) == len(expected)
                and all(r.get('status') == 'pulled' and r.get('exit_code') == 0 for r in rows))


async def loaded_image(checker, ref):
    records = await checker.run(['docker', 'image', 'inspect', ref])
    if not isinstance(records, list) or len(records) != 1:
        raise preflight.PreflightFailure('identity')
    record = records[0]
    if not images.DIGEST.fullmatch(record.get('Id', '')):
        raise preflight.PreflightFailure('identity')
    platform = '/'.join(p for p in (record.get('Os'), record.get('Architecture'), record.get('Variant')) if p)
    if not record.get('Os') or not record.get('Architecture') or 'unknown' in platform.split('/'):
        raise preflight.PreflightFailure('platform')
    desc = record.get('Descriptor')
    return dict(id=record['Id'], platform=platform, descriptor=images.descriptor(desc) if desc else None)


async def verify_prepared(conn, expected, rows):
    # Reuses exact Compose parsing, content identity, labels and old running identities.
    await preflight.verify_project(conn, expected)
    checker = images.Checker(conn)
    before = await container_snapshot(checker, expected)
    for row in rows:
        if (not row.get('containers_before') or row.get('containers_after') != before
                or row['containers_before'] != before or not row.get('loaded_image')):
            raise preflight.PreflightFailure('container')
        loaded = row['loaded_image']
        if (await loaded_image(checker, row['image']) != loaded
                or await loaded_image(checker, loaded['id']) != loaded
                or loaded['platform'] != row['platform']):
            raise preflight.PreflightFailure('identity')


async def observe(checker, candidate):
    """Small sanitized diagnostic snapshot, also usable after failed up."""
    ids = (await checker.run(['docker', 'container', 'ls', '--all', '--quiet', '--no-trunc',
                             '--filter', f'label={images.LABEL}project={candidate.name}'], parse=False)).split()
    if len(ids) > images.MAX_CONTAINERS or any(not re.fullmatch(r'[0-9a-f]{64}', cid) for cid in ids):
        raise preflight.PreflightFailure('container')
    records = await checker.run(['docker', 'container', 'inspect', *ids]) if ids else []
    if (not isinstance(records, list) or len(records) != len(ids)
            or {c.get('Id') for c in records} != set(ids)):
        raise preflight.PreflightFailure('container')
    result = []
    for c in records:
        labels = c.get('Config', {}).get('Labels') or {}
        desc = c.get('ImageManifestDescriptor')
        try:
            desc = images.descriptor(desc) if desc else None
        except images.CheckFailure:
            desc = None
        result.append(dict(container_id=c.get('Id'), image_id=c.get('Image'),
                           project=labels.get(images.LABEL+'project'), service=labels.get(images.LABEL+'service'),
                           running=c.get('State', {}).get('Running'), started_at=c.get('State', {}).get('StartedAt'),
                           restart_count=c.get('RestartCount'), manifest_descriptor=desc))
    return result


async def verify_applied(conn, expected, candidate, rows):
    checker = images.Checker(conn)
    project = dict(name=candidate.name, config_files=list(candidate.paths), config_files_raw=candidate.raw_paths)
    path, source, config, services = await checker.resolve_project(project)
    if images.compose_identity(source, config) != expected.compose_identity:
        raise preflight.PreflightFailure('config')
    observed = await observe(checker, candidate)
    applied = []
    for row in rows:
        service = row['service']
        containers = services.get(service, [])
        count = sum(i[0] == service for i in candidate.images)
        all_for_service = [c for c in observed if c['service'] == service]
        if (len(containers) != count or len(all_for_service) != count
                or {c['Id'] for c in containers} != {c['container_id'] for c in all_for_service}
                or len({c['Id'] for c in containers}) != count):
            raise preflight.PreflightFailure('container')
        for c in containers:
            if images.reference(c.get('Config', {}).get('Image')) != images.reference(row['image']):
                raise preflight.PreflightFailure('image')
            local = await loaded_image(checker, c.get('Image', ''))
            if local != row['loaded_image'] or c.get('Image') != local['id']:
                raise preflight.PreflightFailure('identity')
            if local['platform'] != row['platform']:
                raise preflight.PreflightFailure('platform')
            record = next(r for r in all_for_service if r['container_id'] == c['Id'])
            if record['running'] is not True or record['project'] != candidate.name:
                raise preflight.PreflightFailure('container')
            applied.append(dict(record, platform=local['platform'], local_descriptor=local['descriptor'], image=row['image']))
    # Non-target services must retain their exact previous container state.
    unaffected = replace(expected, images=tuple(i for i in expected.images if i[0] not in {r['service'] for r in rows}))
    if unaffected.images:
        old = tuple(r for r in rows[0]['containers_before'] if r[0] in {i[1] for i in unaffected.images})
        if await container_snapshot(checker, unaffected) != old:
            raise preflight.PreflightFailure('container')
    if await checker.run(['cat', '--', path], parse=False) != source:
        raise preflight.PreflightFailure('file')
    return applied


class ApplyRun:
    def __init__(self, state, hosts, progress=lambda text: None):
        self.state, self.hosts, self.progress = deepcopy(state), hosts, progress
        self.applies = []
        self.mutation_attempted = False

    def outcome(self, status, note, reason=''):
        return dict(status=status, note=note, reason=reason, apply_pending=False,
                    verification_pending=status == 'apply_succeeded',
                    mutation_attempted=self.mutation_attempted, applies=deepcopy(self.applies))

    def interrupted(self):
        for row in self.applies:
            if row['status'] == 'applying':
                row['status'] = 'unknown'
        return self.outcome('cancelled', 'Docker-Apply abgebrochen. Der Containerzustand kann bereits verändert worden sein. '
                            'Ein Remote-Compose-Up kann weiterlaufen. Keine automatische Wiederholung.', 'cancelled')

    async def diagnose(self, conn, candidate, row):
        try:
            async with asyncio.timeout(10):
                row['observed'] = await observe(images.Checker(conn), candidate)
        except (Exception, asyncio.CancelledError):
            row['observation_unavailable'] = True

    async def run(self):
        try:
            if not prepared(self.state):
                raise preflight.PreflightFailure('plan')
            plan = self.state['plan']
            for candidate in plan.candidates:
                host = self.hosts.get(candidate.host_id)
                if not host or preflight.connection_identity(host) != candidate.connection[:5]:
                    raise preflight.PreflightFailure('host')
                expected = next(p for p in plan.selection if (p.host_id, p.name) == (candidate.host_id, candidate.name))
                rows = [r for r in self.state['result']['pulls'] if (r['host_id'], r['project']) == (candidate.host_id, candidate.name)]
                row = dict(host_id=candidate.host_id, project=candidate.name,
                           connection=candidate.connection, paths=candidate.paths,
                           compose_identity=candidate.compose_identity,
                           services=sorted({r['service'] for r in rows}), status='precheck')
                self.applies.append(row)
                async with connect_host(host) as conn:
                    self.progress('Docker-Apply-Precheck …')
                    async with asyncio.timeout(preflight.TOTAL_TIMEOUT):
                        await verify_prepared(conn, expected, rows)
                    preflight.db.get_connection_context(host)
                    cmd = images.compose_command(candidate.name, candidate.paths[0])
                    cmd.insert(1, 'COMPOSE_PARALLEL_LIMIT=1')
                    cmd += ['up', '-d', '--no-deps', '--pull', 'never', '--no-build', '--', *row['services']]
                    row['status'] = 'applying'
                    self.progress('Docker-Service wird angewendet …')
                    self.mutation_attempted = True
                    try:
                        code, out, err = await capture(conn, shlex.join(cmd), APPLY_TIMEOUT)
                        row['exit_code'] = code
                        if code:
                            raise preflight.PreflightFailure('error')
                        async with asyncio.timeout(preflight.TOTAL_TIMEOUT):
                            row['containers'] = await verify_applied(conn, expected, candidate, rows)
                        row['status'] = 'apply_succeeded'
                    except (Exception, asyncio.CancelledError):
                        row['status'] = 'unknown'
                        await self.diagnose(conn, candidate, row)
                        raise
            return self.outcome('apply_succeeded', 'Der Container wurde mit dem vorbereiteten Image neu erstellt und läuft. '
                                'Die abschließende Registry-Verifikation steht noch aus.')
        except asyncio.CancelledError:
            return self.interrupted()
        except Exception as exc:
            if isinstance(exc, preflight.PreflightFailure):
                reason = exc.reason
                detail = preflight.MESSAGES.get(reason, preflight.MESSAGES['error'])
            elif isinstance(exc, images.CheckFailure) and exc.kind in images.REASONS:
                reason = exc.kind
                detail = images.REASONS[reason]
            else:
                reason, detail = 'error', preflight.MESSAGES['error']
            for row in self.applies:
                if row['status'] == 'precheck':
                    row['status'] = 'failed'
            note = ('Docker-Apply fehlgeschlagen. Der Containerzustand kann bereits verändert worden sein. '
                    'Weitere Applies wurden gestoppt.' if self.mutation_attempted else
                    'Docker-Apply abgebrochen. Der vorbereitete Zustand ist nicht mehr eindeutig gültig. '
                    'Bitte Docker-Host erneut prüfen und eine neue Update-Vorschau erstellen.')
            return self.outcome('failed', note + ' ' + detail, reason)
