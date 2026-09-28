import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
import json
import shlex
import unittest
from unittest import mock
import test_docker_preflight as fixtures
from sshupdater.core import docker_apply as apply, docker_image_updates as images, docker_preflight as preflight
from sshupdater.core.docker_pull import container_snapshot

A, B, C = fixtures.fixtures.A, fixtures.fixtures.B, fixtures.fixtures.C


class ApplyTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.PreflightTests.setUp
    plan = fixtures.PreflightTests.plan

    async def prepare(self):
        plan = await self.plan()
        self.local[B] = dict(self.local[A], Id=B, Descriptor=fixtures.fixtures.desc(B))
        self.tag = B
        self.up_calls = 0
        self.after = lambda: None
        self.code = 0
        self.pause = False
        self.entered = asyncio.Event()
        with mock.patch.object(images, 'capture', side_effect=self.capture):
            before = await container_snapshot(images.Checker(object()), plan.selection[0])
            loaded = await apply.loaded_image(images.Checker(object()), B)
        i = plan.candidates[0].images[0]
        row = dict(host_id=1, project='test', service=i[0], image=i[3], platform=i[4],
                   status='pulled', exit_code=0, containers_before=before, containers_after=before, loaded_image=loaded)
        self.commands.clear()
        return dict(plan=plan, result=dict(status='pulled', mutation_attempted=True, apply_pending=True, pulls=[row]))

    async def capture(self, conn, command, timeout):
        args = shlex.split(command)
        if 'up' in args:
            self.commands.append(args)
            self.up_calls += 1
            if self.pause:
                self.entered.set()
                await asyncio.Event().wait()
            for index, container in enumerate(self.containers):
                if container['Config']['Labels'][images.LABEL+'service'] in args[args.index('--')+1:]:
                    container.update(Id=('f' if index == 0 else 'e')*64, Image=B)
                    container['Config']['Labels'][images.LABEL+'project.environment_file'] = '/dev/null'
                    container['ImageManifestDescriptor'] = fixtures.fixtures.desc(
                        C, images.MANIFEST, platform={'os': 'linux', 'architecture': 'amd64'})
            self.after()
            return self.code, '', 'TOKEN=SECRET password=SECRET'
        if args[:3] == ['docker','image','inspect'] and not args[3].startswith('sha256:'):
            self.commands.append(args)
            if not self.tag:
                return 1, '', 'No such image SECRET'
            return 0, json.dumps([self.local[self.tag]]), ''
        if args[:3] == ['docker', 'container', 'inspect']:
            self.commands.append(args)
            return 0, json.dumps([c for c in self.containers if c['Id'] in args[3:]]), ''
        return await fixtures.PreflightTests.capture(self, conn, command, timeout)

    async def execute(self, state, hosts=None):
        @asynccontextmanager
        async def connect(host):
            yield object()
        with mock.patch.object(apply, 'connect_host', connect), \
             mock.patch.object(preflight.db, 'get_connection_context', side_effect=lambda h:h), \
             mock.patch.object(images, 'capture', side_effect=self.capture), \
             mock.patch.object(apply, 'capture', side_effect=self.capture), \
             mock.patch.object(images.Checker, 'remote', side_effect=AssertionError('registry')), \
             mock.patch('builtins.open', side_effect=AssertionError('persistence')):
            self.run_state = apply.ApplyRun(state, hosts or {1:fixtures.HOST})
            return await self.run_state.run()

    async def test_success_targeted_no_pull_new_container_and_verification_pending(self):
        state = await self.prepare()
        before = deepcopy(state)
        result = await self.execute(state)
        self.assertEqual(result['status'], 'apply_succeeded', result)
        self.assertTrue(result['verification_pending'])
        self.assertFalse(result['apply_pending'])
        row = result['applies'][0]['containers'][0]
        self.assertEqual(row['container_id'], 'f'*64)
        self.assertEqual(row['image_id'], B)
        self.assertEqual(row['platform'], 'linux/amd64')
        self.assertEqual(row['local_descriptor']['digest'], B)
        self.assertEqual(row['local_descriptor']['mediaType'], images.INDEX)
        self.assertEqual(row['manifest_descriptor']['digest'], C)
        self.assertEqual(row['manifest_descriptor']['mediaType'], images.MANIFEST)
        self.assertEqual(self.container['Config']['Labels'][images.LABEL+'project.environment_file'], '/dev/null')
        self.assertEqual(state, before)
        up, = [c for c in self.commands if 'up' in c]
        self.assertEqual(up[-8:], ['up','-d','--no-deps','--pull','never','--no-build','--','web'])
        self.assertIn('COMPOSE_PARALLEL_LIMIT=1', up)
        self.assertGreater(self.commands.index(up), 5)
        for c in self.commands:
            self.assertFalse(set(c) & {'pull','down','restart','stop','start','rm','rmi','prune','build','push','buildx'})
        self.assertIn('Registry-Verifikation steht noch aus', result['note'])

    async def test_missing_or_partial_pull_state_cannot_apply(self):
        state = await self.prepare()
        for bad in (None, {}, dict(state, result=dict(state['result'], status='failed')),
                    dict(state, result=dict(state['result'], pulls=[]))):
            self.assertEqual((await self.execute(bad))['status'], 'failed')
        self.assertEqual(self.up_calls, 0)

    async def test_precheck_rejects_all_drift_before_up(self):
        cases = [lambda: setattr(self, 'source', self.source+'\n# changed'),
                 lambda: self.config['services'].clear(),
                 lambda: self.config['services']['web'].update(image='other:tag'),
                 lambda: self.config['services']['web'].update(platform='linux/arm64'),
                 lambda: self.container.update(Id='e'*64),
                 lambda: self.container['Config']['Labels'].update({images.LABEL+'project':'other'}),
                 lambda: self.container['Config']['Labels'].update({images.LABEL+'service':'other'}),
                 lambda: setattr(self, 'tag', None),
                 lambda: setattr(self, 'tag', A),
                 lambda: self.local[B].update(Architecture='arm64'),
                 lambda: self.local[B].update(Descriptor=fixtures.fixtures.desc(C))]
        for n, change in enumerate(cases):
            with self.subTest(n=n):
                self.setUp()
                state = await self.prepare()
                change()
                result = await self.execute(state)
                self.assertEqual(result['status'], 'failed')
                self.assertFalse(result['mutation_attempted'])
                self.assertEqual(self.up_calls, 0)
                self.assertNotIn('SECRET', repr(result))

    async def test_postcheck_rejects_missing_stopped_wrong_image_labels_and_platform(self):
        cases = [lambda: self.containers.clear(),
                 lambda: self.container['State'].update(Running=False),
                 lambda: self.container.update(Image=A),
                 lambda: self.container['Config']['Labels'].update({images.LABEL+'project':'other'}),
                 lambda: self.container['Config']['Labels'].update({images.LABEL+'service':'other'}),
                 lambda: self.local[B].update(Architecture='arm64'),
                 lambda: self.config.update(volumes={'changed': {}})]
        for n, change in enumerate(cases):
            with self.subTest(n=n):
                self.setUp()
                state = await self.prepare()
                self.after = change
                result = await self.execute(state)
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(self.up_calls, 1)
                self.assertFalse(result['verification_pending'])
                self.assertTrue(result['mutation_attempted'])

    async def test_postcheck_context_failure_retains_safe_reason(self):
        state = await self.prepare()
        self.after = lambda: self.container['Config']['Labels'].update(
            {images.LABEL+'project.environment_file': '/secret/TOKEN=SECRET.env'})
        result = await self.execute(state)
        self.assertEqual(result['reason'], 'context')
        self.assertIn(images.REASONS['context'], result['note'])
        self.assertEqual(result['applies'][0]['status'], 'unknown')
        self.assertNotIn('SECRET', result['note'])

    async def test_check_failure_uses_allowlisted_message_not_exception_or_command(self):
        state = await self.prepare()
        error = images.CheckFailure('context', command='SECRET', exit_code=1)
        error.args = ('TOKEN=SECRET',)
        with mock.patch.object(apply, 'verify_applied', side_effect=error):
            result = await self.execute(state)
        self.assertEqual(result['reason'], 'context')
        self.assertIn(images.REASONS['context'], result['note'])
        self.assertNotIn('SECRET', repr(result))

    async def test_up_failure_and_cancel_report_possible_changes(self):
        state = await self.prepare()
        self.code = 1
        result = await self.execute(state)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('observed', result['applies'][0])
        self.assertNotIn('SECRET', repr(result))
        self.setUp()
        state = await self.prepare()
        self.pause = True
        task = asyncio.create_task(self.execute(state))
        await asyncio.wait_for(self.entered.wait(), 2)
        task.cancel()
        result = await task
        self.assertEqual(result['status'], 'cancelled')
        self.assertFalse(result['verification_pending'])
        self.assertIn('verändert', result['note'])
        self.assertNotIn('keine Änderungen', result['note'])

    async def test_host_drift_and_ssh_failure_no_up(self):
        state = await self.prepare()
        result = await self.execute(state, {1:dict(fixtures.HOST, user='other')})
        self.assertEqual(result['reason'], 'host')
        self.assertEqual(self.up_calls, 0)

    async def test_multiple_projects_stop_after_partial_success(self):
        state = await self.prepare()
        original = state['plan'].selection[0]
        others = [replace(original, name=name) for name in ('second','third')]
        state['plan'] = replace(state['plan'], selection=(original,*others), candidates=(original,*others))
        state['result']['pulls'] += [dict(state['result']['pulls'][0], project=p.name) for p in others]
        async def capture(conn, command, timeout):
            self.up_calls += 1
            return (0 if self.up_calls == 1 else 1), '', 'SECRET'
        @asynccontextmanager
        async def connect(host):
            yield object()
        with mock.patch.object(apply, 'connect_host', connect), \
             mock.patch.object(apply, 'verify_prepared', return_value=None), \
             mock.patch.object(apply, 'verify_applied', return_value=[]), \
             mock.patch.object(apply.ApplyRun, 'diagnose', return_value=None), \
             mock.patch.object(preflight.db, 'get_connection_context'), \
             mock.patch.object(apply, 'capture', side_effect=capture):
            result = await apply.ApplyRun(state, {1:fixtures.HOST}).run()
        self.assertEqual(self.up_calls, 2)
        self.assertEqual([r['status'] for r in result['applies']], ['apply_succeeded','unknown'])
        self.assertFalse(result['verification_pending'])

    async def test_multiple_target_services_and_untouched_current_service(self):
        second = deepcopy(self.container)
        second.update(Id='b'*64, Name='/test-second-1')
        second['Config']['Labels'][images.LABEL+'service'] = 'second'
        self.containers.append(second)
        self.config['services']['second'] = {'image': 'nginx:1.28-alpine'}
        current = deepcopy(self.container)
        current.update(Id='c'*64, Name='/test-current-1', Image=B)
        current['Config']['Labels'][images.LABEL+'service'] = 'current'
        self.containers.append(current)
        self.config['services']['current'] = {'image': 'nginx:1.28-alpine'}
        self.local[B] = dict(self.local[A], Id=B, Descriptor=fixtures.fixtures.desc(B))
        state = await self.prepare()
        original = state['result']['pulls'][0]
        state['result']['pulls'] = [dict(original, service=i[0], image=i[3], platform=i[4])
                                  for i in state['plan'].candidates[0].images]
        old_current = deepcopy(current)
        result = await self.execute(state)
        self.assertEqual(result['status'], 'apply_succeeded', result)
        up, = [c for c in self.commands if 'up' in c]
        self.assertEqual(up[up.index('--')+1:], ['second', 'web'])
        self.assertEqual(current, old_current)
        self.assertEqual(len(result['applies'][0]['containers']), 2)

    async def test_ssh_failure_and_missing_loaded_identity_are_safe(self):
        state = await self.prepare()
        @asynccontextmanager
        async def broken(host):
            raise OSError('PASSWORD=SECRET')
            yield
        with mock.patch.object(apply, 'connect_host', broken):
            result = await apply.ApplyRun(state, {1:fixtures.HOST}).run()
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('SECRET', repr(result))
        del state['result']['pulls'][0]['loaded_image']
        result = await self.execute(state)
        self.assertFalse(result['mutation_attempted'])
        self.assertEqual(self.up_calls, 0)
