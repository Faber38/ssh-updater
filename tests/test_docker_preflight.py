import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import test_docker_image_updates as fixtures
from sshupdater.core import docker_image_updates as images, docker_preflight as preflight
from sshupdater.docker_plan import build_plan

HOST = dict(id=1, name='test-host', primary_ip='host', user='root', auth_method='key')


class PreflightTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ProjectTests.setUp
    capture = fixtures.ProjectTests.capture

    async def plan(self):
        self.remote = fixtures.desc(fixtures.B)
        with mock.patch.object(images, 'capture', side_effect=self.capture):
            checked = await images.check_projects(object(), [fixtures.PROJECT])
        project = dict(fixtures.PROJECT, image_updates=checked['test'])
        plan = build_plan([dict(host_id=1, connection_identity=preflight.connection_identity(HOST)+(1,),
                               result_revision=1, projects=[project])])
        self.commands.clear()
        self.assertEqual(len(plan.candidates), 1)
        return plan

    async def run_preflight(self, plan, *, capture=None, hosts=None):
        @asynccontextmanager
        async def connect(host):
            yield object()
        with mock.patch.object(preflight, 'connect_host', connect), \
             mock.patch.object(preflight.db, 'get_connection_context', side_effect=lambda h: h), \
             mock.patch.object(images, 'capture', side_effect=capture or self.capture), \
             mock.patch.object(images.Checker, 'remote', side_effect=AssertionError('registry forbidden')):
            return await preflight.preflight(plan, hosts or {1: HOST})

    async def test_unchanged_plan_passes_without_registry_or_mutation_commands(self):
        plan = await self.plan()
        before = deepcopy(plan)
        result = await self.run_preflight(plan)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(plan, before)
        self.assertEqual(result['projects'][0]['platform'], 'linux/amd64')
        for args in self.commands:
            self.assertTrue(args[:2] == ['cat', '--'] or
                            args[:3] in (['docker', 'container', 'ls'], ['docker', 'container', 'inspect'], ['docker', 'image', 'inspect']) or
                            (args[0] == 'env' and args[-5:] == ['config', '--format', 'json', '--no-interpolate', '--no-env-resolution']))
        self.assertFalse(any('buildx' in c for c in self.commands))

    async def test_missing_plan_or_legacy_evidence_fails(self):
        self.assertEqual((await self.run_preflight(None))['status'], 'failed')
        plan = await self.plan()
        old = replace(plan, selection=(replace(plan.selection[0], compose_identity=()),))
        self.assertEqual((await self.run_preflight(old))['reason'], 'plan')

    async def test_file_missing_changed_and_change_during_check(self):
        plan = await self.plan()
        original = self.source
        self.source += '\n# changed\n'
        self.assertEqual((await self.run_preflight(plan))['reason'], 'file')
        self.source = original
        async def missing(conn, command, timeout):
            if command.startswith('cat '):
                return 1, '', 'SECRET missing file'
            return await self.capture(conn, command, timeout)
        result = await self.run_preflight(plan, capture=missing)
        self.assertEqual(result['reason'], 'file')
        self.assertNotIn('SECRET', repr(result))
        count = 0
        async def changed(conn, command, timeout):
            nonlocal count
            if command.startswith('cat '):
                count += 1
                return 0, self.source + ('\n# changed' if count > 1 else ''), ''
            return await self.capture(conn, command, timeout)
        self.assertEqual((await self.run_preflight(plan, capture=changed))['reason'], 'file')

    async def test_compose_service_image_platform_and_configuration_changes(self):
        plan = await self.plan()
        original = deepcopy(self.config)
        cases = [({'services': {}}, 'project'),
                 ({'services': {'web': {'image': 'other:latest'}}}, 'image'),
                 ({'services': {'web': {'image': 'nginx:1.28-alpine', 'platform': 'linux/amd64'}}}, 'platform'),
                 ({'services': original['services'], 'name': 'different'}, 'project'),
                 ({'services': original['services'], 'volumes': {'new': {}}}, 'config')]
        for config, reason in cases:
            self.config = config
            with self.subTest(reason=reason):
                self.assertEqual((await self.run_preflight(plan))['reason'], reason)
        self.config = original
        self.config['services']['extra'] = {'image': 'nginx:latest'}
        self.assertEqual((await self.run_preflight(plan))['reason'], 'service')

    async def test_container_membership_running_state_service_and_identity(self):
        plan = await self.plan()
        original = deepcopy(self.container)
        mutations = [lambda c: c['State'].update(Running=False),
                     lambda c: c['Config']['Labels'].update({images.LABEL+'project': 'other'}),
                     lambda c: c['Config']['Labels'].update({images.LABEL+'service': 'other'}),
                     lambda c: c.update(Id='f'*64), lambda c: c.update(Name='/replaced')]
        for mutation in mutations:
            self.container.clear()
            self.container.update(deepcopy(original))
            mutation(self.container)
            self.assertEqual((await self.run_preflight(plan))['status'], 'failed')
        self.containers = []
        self.assertEqual((await self.run_preflight(plan))['reason'], 'container')

    async def test_local_image_descriptor_and_platform_changes(self):
        plan = await self.plan()
        original = deepcopy(self.local)
        for field, value, reason in [('Descriptor', fixtures.desc(fixtures.C), 'identity'),
                                      ('Architecture', 'arm64', 'platform')]:
            self.local = deepcopy(original)
            self.local[fixtures.A][field] = value
            self.assertEqual((await self.run_preflight(plan))['reason'], reason)
        self.local = deepcopy(original)
        self.container['Image'] = fixtures.C
        self.local[fixtures.C] = dict(self.local[fixtures.A], Id=fixtures.C)
        self.assertEqual((await self.run_preflight(plan))['reason'], 'identity')

    async def test_existing_container_descriptor_fallback_remains_read_only(self):
        self.container['ImageManifestDescriptor'] = fixtures.desc(fixtures.A, images.MANIFEST,
            platform={'os': 'linux', 'architecture': 'amd64'})
        self.errors['image'] = (1, '', 'No such image: missing')
        self.remote = fixtures.desc(manifests=[fixtures.desc(fixtures.B, images.MANIFEST,
            platform={'os': 'linux', 'architecture': 'amd64'})])
        with mock.patch.object(images, 'capture', side_effect=self.capture):
            checked = await images.check_projects(object(), [fixtures.PROJECT])
        plan = build_plan([dict(host_id=1, connection_identity=preflight.connection_identity(HOST)+(1,),
            result_revision=1, projects=[dict(fixtures.PROJECT, image_updates=checked['test'])])])
        self.assertTrue(plan.candidates)
        self.commands.clear()
        self.assertEqual((await self.run_preflight(plan))['status'], 'passed')
        self.assertFalse(any('buildx' in c for c in self.commands))

    async def test_host_and_ssh_failures_are_sanitized(self):
        plan = await self.plan()
        self.assertEqual((await self.run_preflight(plan, hosts={1: dict(HOST, user='changed')}))['reason'], 'host')
        @asynccontextmanager
        async def broken(host):
            raise OSError('PASSWORD TOKEN SECRET')
            yield
        with mock.patch.object(preflight, 'connect_host', broken):
            result = await preflight.preflight(plan, {1: HOST})
        self.assertEqual(result['reason'], 'ssh')
        self.assertNotIn('SECRET', repr(result))

    async def test_multiple_hosts_atomic_failure_and_success(self):
        plan = await self.plan()
        second = replace(plan.selection[0], host_id=2)
        plan = replace(plan, selection=plan.selection+(second,), candidates=plan.candidates+(second,))
        hosts = {1: HOST, 2: dict(HOST, id=2)}
        self.assertEqual(len((await self.run_preflight(plan, hosts=hosts))['projects']), 2)
        @asynccontextmanager
        async def connect(host):
            if host['id'] == 2:
                self.source += '\n# changed'
            yield object()
        with mock.patch.object(preflight, 'connect_host', connect), \
             mock.patch.object(preflight.db, 'get_connection_context', side_effect=lambda h:h), \
             mock.patch.object(images, 'capture', side_effect=self.capture):
            result = await preflight.preflight(plan, hosts)
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('projects', result)

    async def test_timeout_and_cancellation(self):
        plan = await self.plan()
        async def wait(*args):
            await asyncio.Event().wait()
        with mock.patch.object(preflight, 'TOTAL_TIMEOUT', .01):
            self.assertEqual((await self.run_preflight(plan, capture=wait))['reason'], 'timeout')
        task = asyncio.create_task(self.run_preflight(plan, capture=wait))
        await asyncio.sleep(.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
