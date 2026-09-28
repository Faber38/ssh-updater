"""Read-only completion of a successful apply. Never changes the apply outcome."""
import asyncio
from copy import deepcopy
import hashlib

from . import docker_image_updates as images, docker_preflight as preflight
from .ssh_connection import connect_host

COMPLETE = ('verified', 'update_available')
LOCAL_NOTE = ('Der Apply war zuvor erfolgreich. Der aktuelle Containerzustand entspricht jedoch '
              'nicht mehr dem unmittelbar nach dem Apply bestätigten Zustand oder ist nicht eindeutig prüfbar.')


class CompletionRegistry:
    """Delegate TTL/scopes/backoff; only reuse old metadata proven to match applied images."""
    def __init__(self, session, applied, live=None):
        self.session, self.applied = session, applied
        self.live = live if live is not None else set()

    def keys(self, ref, scope):
        return self.session.keys(ref, scope)

    def blocked(self, key):
        return self.session.blocked(key)

    def rate_limit(self, key):
        self.session.rate_limit(key)

    def put(self, key, value):
        result = self.session.put(key, value)
        self.live.add(key)
        return result

    def get(self, key):
        value = self.session.get(key)
        if value is None or key in self.live:
            return value
        relevant = [c for c in self.applied if images.reference(c['image']) == key[1]]
        if not relevant:
            return value  # Other services use the normal TTL policy.
        try:
            if all(images.compare(c['local_descriptor'], value, c['platform'])[0] == 'current'
                   for c in relevant):
                return value
        except (images.CheckFailure, KeyError, TypeError):
            pass
        return None  # No eviction, and especially no bypass of registry backoff.


def scope_for(host):
    # Same opaque host/auth scope as the normal SSH host check; no credentials retained.
    return hashlib.sha256(repr(tuple(host.get(k) for k in (
        'id', 'primary_ip', 'port', 'user', 'auth_method', 'key_path', 'password_enc'
    ))).encode()).hexdigest()


async def local_state(conn, applied):
    checker = images.Checker(conn)
    project = dict(name=applied['project'], config_files=list(applied['paths']))
    path, source, config, by_service = await checker.resolve_project(project)
    if images.compose_identity(source, config) != applied['compose_identity']:
        raise preflight.PreflightFailure('config')
    expected = applied.get('containers', [])
    if not expected or {c['service'] for c in expected} != set(applied['services']):
        raise preflight.PreflightFailure('plan')
    results = []
    for service in applied['services']:
        old = [c for c in expected if c['service'] == service]
        live = by_service.get(service, [])
        if (len(live) != len(old) or {c['Id'] for c in live} != {c['container_id'] for c in old}):
            raise preflight.PreflightFailure('container')
        for c in live:
            previous = next(p for p in old if p['container_id'] == c['Id'])
            if (c.get('State', {}).get('StartedAt') != previous.get('started_at')
                    or c.get('RestartCount') != previous.get('restart_count')):
                raise preflight.PreflightFailure('container')
            result = await checker.image(c, service, config['services'][service], local_only=True)
            if (result.status != 'local_identity' or result.image_id != previous['image_id']
                    or result.image != previous['image'] or result.platform != previous['platform']
                    or images.descriptor(result.local_descriptor) != previous['local_descriptor']):
                raise preflight.PreflightFailure('identity')
            results.append(result)
    if await checker.run(['cat', '--', path], parse=False) != source:
        raise preflight.PreflightFailure('file')
    return project


class VerificationRun:
    def __init__(self, applied, hosts, session, previous=None, progress=lambda text: None):
        self.applied, self.hosts, self.session = deepcopy(applied), hosts, session
        self.progress = progress
        self.live_registry_keys = set()
        self.rows = deepcopy((previous or {}).get('projects', []))

    def outcome(self, *, cancelled=False):
        expected = {(r['host_id'], r['project']) for r in self.applied.get('applies', [])}
        done = {(r['host_id'], r['project']) for r in self.rows if r['status'] in COMPLETE}
        completed = bool(expected) and expected == done and not cancelled
        newer = any(r['status'] == 'update_available' for r in self.rows)
        return dict(apply_status='apply_succeeded', completed=completed, verification_pending=not completed,
                    status='cancelled' if cancelled else 'update_available' if completed and newer else
                    'verified' if completed else 'pending', projects=deepcopy(self.rows))

    def interrupted(self):
        return self.outcome(cancelled=True)

    async def run(self):
        if (self.applied.get('status') != 'apply_succeeded' or not self.applied.get('verification_pending')
                or not self.applied.get('applies') or any(r.get('status') != 'apply_succeeded' for r in self.applied['applies'])):
            return self.outcome(cancelled=True)
        try:
            for applied in self.applied['applies']:
                key = (applied['host_id'], applied['project'])
                old = next((r for r in self.rows if (r['host_id'], r['project']) == key), None)
                if old and old['status'] in COMPLETE:
                    continue  # Preserve an already verified receipt across a partial retry.
                row = dict(host_id=key[0], project=key[1], paths=applied['paths'], connection=applied['connection'],
                           status='local_changed', reason='local', note=LOCAL_NOTE)
                if old:
                    self.rows.remove(old)
                self.rows.append(row)
                host = self.hosts.get(key[0])
                row['host'] = (host or {}).get('name') or str(key[0])
                if not host or preflight.connection_identity(host) != applied['connection'][:5]:
                    row.update(reason='host', note=LOCAL_NOTE + ' ' + preflight.MESSAGES['host'])
                    continue
                self.progress(f"Docker prüfen: {key[1]} …")
                try:
                    async with asyncio.timeout(images.TOTAL_TIMEOUT):
                        async with connect_host(host) as conn:
                            project = await local_state(conn, applied)
                            row.update(status='registry_pending', reason='', note='Abschließende Registry-Verifikation derzeit nicht möglich.')
                            registry = CompletionRegistry(self.session, applied['containers'], self.live_registry_keys)
                            checker = images.Checker(conn, registry, scope_for(host))
                            checked = await checker.project(project)
                            # A registry request can take time; check local identities again before publishing.
                            row.update(status='local_changed', note=LOCAL_NOTE)
                            await local_state(conn, applied)
                            preflight.db.get_connection_context(host)
                            targets = [i for i in checked['images'] if i['service'] in applied['services']]
                            ids = {c['container_id'] for c in applied['containers']}
                            if (len(targets) != len(ids) or {i['container_id'] for i in targets} != ids):
                                raise preflight.PreflightFailure('container')
                            for result in targets:
                                expected = next(c for c in applied['containers'] if c['container_id'] == result['container_id'])
                                if (result['image_id'] != expected['image_id'] or result['image'] != expected['image']
                                        or result['platform'] != expected['platform']
                                        or images.descriptor(result['local_descriptor']) != expected['local_descriptor']):
                                    raise preflight.PreflightFailure('identity')
                            row['image_updates'] = checked
                            unknown = [i for i in targets if i['status'] not in ('current', 'update_available')]
                            if unknown:
                                reason = unknown[0].get('reason') or 'identity'
                                row.update(status='registry_pending', reason=reason,
                                           note=images.REASONS.get(reason, images.REASONS['identity']))
                            elif any(i['status'] == 'update_available' for i in targets):
                                row.update(status='update_available', reason='', note='Erneutes Image-Update für denselben Tag verfügbar. Der Apply bleibt erfolgreich.')
                            else:
                                row.update(status='verified', reason='', note='Docker-Update vollständig verifiziert. Der Image-Stand entspricht dem aktuellen zulässigen Registry-Stand.')
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    reason = exc.reason if isinstance(exc, preflight.PreflightFailure) else exc.kind if isinstance(exc, images.CheckFailure) else 'error'
                    detail = (preflight.MESSAGES.get(reason) if isinstance(exc, preflight.PreflightFailure)
                              else images.REASONS.get(reason, preflight.MESSAGES['error']))
                    row.update(reason=reason, note=(LOCAL_NOTE if row['status'] == 'local_changed' else
                               'Apply erfolgreich; abschließende Registry-Verifikation ausstehend.') + ' ' + detail)
                    row.pop('image_updates', None)
            return self.outcome()
        except asyncio.CancelledError:
            return self.interrupted()
