"""Phase 4b: fresh preflight, targeted image pulls, then STOP. RAM results only."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import shlex

from . import docker_preflight as preflight, docker_image_updates as images
from .remote_process import capture, RemoteTimeoutError
from .ssh_connection import connect_host
from ..docker_plan import proven

PULL_TIMEOUT = 1800


async def container_snapshot(checker, expected):
    ids = sorted({i[1] for i in expected.images})
    records = await checker.run(['docker', 'container', 'inspect', *ids])
    if not isinstance(records, list) or len(records) != len(ids):
        raise preflight.PreflightFailure('container')
    result = []
    for record in records:
        state = record.get('State', {})
        if record.get('Id') not in ids or state.get('Running') is not True:
            raise preflight.PreflightFailure('container')
        result.append((record['Id'], record.get('Image'), record.get('Name'),
                       state.get('StartedAt'), record.get('RestartCount'), state.get('Running')))
    if len({r[0] for r in result}) != len(ids):
        raise preflight.PreflightFailure('container')
    return tuple(sorted(result))


class PullRun:
    """Keeps partial outcomes even when task cancellation interrupts a command."""
    def __init__(self, plan, hosts, progress=lambda text: None):
        self.plan, self.hosts, self.progress = plan, hosts, progress
        self.pulls = []
        self.mutation_attempted = False
        self.context = {}

    def outcome(self, status, note, reason=''):
        return dict(status=status, stage='pull' if self.mutation_attempted else 'preflight',
                    reason=reason, note=note, mutation_attempted=self.mutation_attempted,
                    apply_pending=any(r['status'] == 'pulled' for r in self.pulls),
                    pulls=deepcopy(self.pulls), context=deepcopy(self.context))

    def interrupted(self, reason='cancelled'):
        for row in self.pulls:
            if row['status'] == 'pulling':
                row.update(status='unknown', note='Pull-Abschluss nicht bestätigt.')
        note = ('Image-Pull abgebrochen. Der lokale Imagebestand kann teilweise verändert worden sein. '
                'Der Remote-Pull kann nach dem Abbruch des lokalen Wartens weiterlaufen.'
                if self.mutation_attempted else 'Docker-Preflight abgebrochen. Kein Pull gestartet.')
        return self.outcome('cancelled', note, reason)

    async def run(self):
        try:
            # Reject inconsistent candidate lists before invoking any remote command.
            if self.plan is None or not self.plan.candidates:
                return self.outcome('failed', preflight.MESSAGES['plan'], 'plan')
            for candidate in self.plan.candidates:
                expected = next(p for p in self.plan.selection if (p.host_id, p.name) == (candidate.host_id, candidate.name))
                if (not candidate.images or candidate != replace(expected, images=tuple(i for i in expected.images if proven(i)))
                        or any(i[5] != 'current' and not proven(i) for i in expected.images)):
                    return self.outcome('failed', preflight.MESSAGES['plan'], 'plan')
            self.progress('Docker-Preflight …')
            checked = await preflight.preflight(self.plan, self.hosts)
            if checked['status'] != 'passed':
                self.context = {k: checked[k] for k in ('host_id', 'project') if checked.get(k) is not None}
                return self.outcome('failed', checked['note'], checked.get('reason', 'preflight'))
            for candidate in self.plan.candidates:
                host = self.hosts[candidate.host_id]
                expected = next(p for p in self.plan.selection if (p.host_id, p.name) == (candidate.host_id, candidate.name))
                if preflight.connection_identity(host) != candidate.connection[:5]:
                    raise preflight.PreflightFailure('host')
                async with connect_host(host) as conn:
                    checker = images.Checker(conn)
                    services = sorted({i[0] for i in candidate.images})
                    for service in services:
                        self.context = dict(host_id=candidate.host_id, host=host.get('name') or str(candidate.host_id),
                                            project=candidate.name, service=service)
                        self.progress('Docker-Preflight …')
                        # No old success is reused, even after another service's pull.
                        async with asyncio.timeout(preflight.TOTAL_TIMEOUT):
                            await preflight.verify_project(conn, expected)
                        preflight.db.get_connection_context(host)
                        before = await container_snapshot(checker, expected)
                        rows = [i for i in candidate.images if i[0] == service]
                        if len({(i[3], i[4]) for i in rows}) != 1:
                            raise preflight.PreflightFailure('plan')
                        ref, platform = rows[0][3:5]
                        command = images.compose_command(candidate.name, candidate.paths[0])
                        command.insert(1, 'COMPOSE_PARALLEL_LIMIT=1')
                        command += ['pull', '--policy', 'always', '--quiet', '--', service]
                        row = dict(host_id=candidate.host_id, host=host.get('name') or str(candidate.host_id),
                                   project=candidate.name, service=service, image=ref, platform=platform,
                                   status='pulling', containers_before=before)
                        self.pulls.append(row)
                        self.progress('Docker-Image wird geladen …')
                        self.mutation_attempted = True
                        code, out, err = await capture(conn, shlex.join(command), PULL_TIMEOUT)
                        row['exit_code'] = code
                        if code:
                            reason = images.failure_kind(code, err or out)
                            row.update(status='failed', note=images.REASONS[reason])
                            return self.outcome('failed', 'Image-Pull fehlgeschlagen. Weitere Pulls wurden gestoppt.', reason)
                        # Exit 0 confirms pull only, never an applied container update.
                        row['status'] = 'pulled'
                        row['containers_after'] = await container_snapshot(checker, expected)
                        if row['containers_after'] != before:
                            return self.outcome('failed', 'Image geladen, aber Containerzustand hat sich zwischenzeitlich geändert. Keine weiteren Pulls.', 'container')
                        loaded = await checker.run(['docker', 'image', 'inspect', ref])
                        if not isinstance(loaded, list) or len(loaded) != 1 or not images.DIGEST.fullmatch(loaded[0].get('Id', '')):
                            raise images.CheckFailure('invalid_output')
                        metadata = loaded[0]
                        observed = '/'.join(p for p in (metadata.get('Os'), metadata.get('Architecture'), metadata.get('Variant')) if p)
                        if observed != platform:
                            raise preflight.PreflightFailure('platform')
                        descriptor = metadata.get('Descriptor')
                        row['loaded_image'] = dict(id=metadata['Id'], platform=observed,
                            descriptor=images.descriptor(descriptor) if descriptor else None)
            return self.outcome('pulled', 'Image-Pull erfolgreich. Das angeforderte Image ist lokal verfügbar. Apply/Container-Aktualisierung steht noch aus.')
        except asyncio.CancelledError:
            return self.interrupted()
        except (RemoteTimeoutError, TimeoutError):
            result = self.interrupted('timeout')
            result.update(status='failed', note='Zeitlimit überschritten. Pull-Abschluss gegebenenfalls nicht bestätigt.')
            return result
        except Exception as exc:
            reason = exc.reason if isinstance(exc, preflight.PreflightFailure) else (exc.kind if isinstance(exc, images.CheckFailure) else 'error')
            note = str(exc) if isinstance(exc, (preflight.PreflightFailure, images.CheckFailure)) else 'SSH-/Pull-Prüfung fehlgeschlagen; keine weiteren Pulls.'
            for row in self.pulls:
                if row['status'] == 'pulling':
                    row.update(status='unknown', note='Pull-Abschluss nicht bestätigt.')
            return self.outcome('failed', note, reason)
