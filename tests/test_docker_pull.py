import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
import json
import shlex
import unittest
from unittest import mock
import test_docker_preflight as fixtures
from sshupdater.core import docker_pull as pull, docker_preflight as preflight, docker_image_updates as images


class PullTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.PreflightTests.setUp
    plan = fixtures.PreflightTests.plan

    async def capture(self, conn, command, timeout):
        args = shlex.split(command)
        if 'pull' in args:
            self.commands.append(args)
            self.pull_calls += 1
            if self.pull_error and self.pull_calls == self.fail_at:
                return 1, '', self.pull_error
            if self.pause_pull:
                self.entered.set()
                await asyncio.Event().wait()
            self.tag_pulled = True
            return 0, '', ''
        if args[:3] == ['docker', 'image', 'inspect'] and not args[3].startswith('sha256:'):
            self.commands.append(args)
            return 0, json.dumps([dict(Id=fixtures.fixtures.B, Descriptor=fixtures.fixtures.desc(fixtures.fixtures.B),
                                      Os='linux', Architecture='amd64')]), ''
        return await fixtures.PreflightTests.capture(self, conn, command, timeout)

    async def execute(self, plan, *, verify=True):
        self.pull_calls = 0
        self.pull_error = getattr(self, 'pull_error', '')
        self.fail_at = getattr(self, 'fail_at', 1)
        self.pause_pull = getattr(self, 'pause_pull', False)
        self.tag_pulled = False
        self.entered = asyncio.Event()
        self.events = []
        @asynccontextmanager
        async def connect(host):
            yield object()
        with mock.patch.object(preflight, 'connect_host', connect), \
             mock.patch.object(pull, 'connect_host', connect), \
             mock.patch.object(preflight.db, 'get_connection_context', side_effect=lambda h:h), \
             mock.patch.object(images, 'capture', side_effect=self.capture), \
             mock.patch.object(pull, 'capture', side_effect=self.capture), \
             mock.patch.object(images.Checker, 'remote', side_effect=AssertionError('Registry cache touched')):
            self.run_state = pull.PullRun(plan, {1: fixtures.HOST}, self.events.append)
            if verify:
                return await self.run_state.run()
            with mock.patch.object(preflight, 'preflight', return_value={'status':'passed'}), \
                 mock.patch.object(preflight, 'verify_project', return_value=None):
                return await self.run_state.run()

    async def test_fresh_preflight_targeted_pull_and_container_unchanged(self):
        plan = await self.plan()
        before = deepcopy(self.container)
        result = await self.execute(plan)
        self.assertEqual(result['status'], 'pulled')
        self.assertTrue(result['apply_pending'])
        row, = result['pulls']
        self.assertEqual(row['containers_before'], row['containers_after'])
        self.assertEqual(self.container, before)
        self.assertEqual(row['loaded_image']['id'], fixtures.fixtures.B)
        commands = [c for c in self.commands if 'pull' in c]
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][-6:], ['pull', '--policy', 'always', '--quiet', '--', 'web'])
        self.assertIn('COMPOSE_DISABLE_ENV_FILE=1', commands[0])
        first = self.commands.index(commands[0])
        self.assertGreater(sum(c[:3] == ['docker', 'container', 'inspect'] for c in self.commands[:first]), 1)
        self.assertNotIn('buildx', repr(self.commands))
        self.assertEqual(self.events[-1], 'Docker-Image wird geladen …')
        self.assertTrue(all(not set(c).intersection({'up','down','restart','stop','start','rm','rmi','build','push','prune'}) for c in self.commands))

    async def test_preflight_failure_has_no_pull(self):
        plan = await self.plan()
        self.source += '\n# drift'
        result = await self.execute(plan)
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['mutation_attempted'])
        self.assertEqual(self.pull_calls, 0)

    async def test_current_service_not_pulled(self):
        plan = await self.plan()
        expected = plan.selection[0]
        current = list(expected.images[0]); current[0] = 'current'; current[5] = 'current'
        expected = replace(expected, images=expected.images+(tuple(current),))
        plan = replace(plan, selection=(expected,))
        result = await self.execute(plan, verify=False)
        self.assertEqual(result['status'], 'pulled')
        self.assertEqual([c[-1] for c in self.commands if 'pull' in c], ['web'])

    async def test_uncheckable_build_pinned_and_injected_candidate_never_pulled(self):
        plan = await self.plan()
        for status in ('uncheckable', 'local_build', 'digest_pinned', 'current'):
            bad = list(plan.candidates[0].images[0]); bad[5] = status
            altered = replace(plan, candidates=(replace(plan.candidates[0], images=(tuple(bad),)),))
            result = await self.execute(altered)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(self.pull_calls, 0)

    async def test_multiple_services_and_projects_sequential_partial_failure(self):
        plan = await self.plan()
        expected = plan.selection[0]
        second = list(expected.images[0]); second[0] = 'second'
        expected = replace(expected, images=expected.images+(tuple(second),))
        other = replace(expected, name='other-project')
        plan = replace(plan, selection=(expected,other), candidates=(expected,other))
        result = await self.execute(plan, verify=False)
        self.assertEqual(result['status'], 'pulled')
        self.assertEqual([(r['project'], r['service']) for r in result['pulls']],
                         [('test','second'),('test','web'),('other-project','second'),('other-project','web')])
        self.pull_error = 'unauthorized TOKEN=secret password=secret'
        self.fail_at = 2
        result = await self.execute(plan, verify=False)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.pull_calls, 2)
        self.assertEqual([r['status'] for r in result['pulls']], ['pulled','failed'])
        self.assertTrue(result['apply_pending'])
        self.assertNotIn('secret', repr(result))
        self.assertEqual(result['reason'], 'auth')

    async def test_multiple_hosts_and_no_persistence(self):
        plan = await self.plan()
        other = replace(plan.selection[0], host_id=2)
        plan = replace(plan, selection=plan.selection+(other,), candidates=plan.candidates+(other,))
        self.pull_calls = 0
        self.pull_error = ''
        self.pause_pull = False
        @asynccontextmanager
        async def connect(host):
            yield object()
        with mock.patch.object(preflight, 'preflight', return_value={'status': 'passed'}), \
             mock.patch.object(preflight, 'verify_project', return_value=None), \
             mock.patch.object(preflight.db, 'get_connection_context', side_effect=lambda h:h), \
             mock.patch.object(pull, 'connect_host', connect), \
             mock.patch.object(images, 'capture', side_effect=self.capture), \
             mock.patch.object(pull, 'capture', side_effect=self.capture), \
             mock.patch('builtins.open', side_effect=AssertionError('file persistence')), \
             mock.patch('sqlite3.connect', side_effect=AssertionError('DB persistence')):
            result = await pull.PullRun(plan, {1:fixtures.HOST, 2:dict(fixtures.HOST,id=2)}).run()
        self.assertEqual(result['status'], 'pulled')
        self.assertEqual([r['host_id'] for r in result['pulls']], [1,2])

    async def test_unconfirmed_exit_is_not_success(self):
        from sshupdater.core.remote_process import RemoteTimeoutError
        plan = await self.plan()
        base = self.capture
        async def timed(conn, command, timeout):
            if ' pull ' in command:
                raise RemoteTimeoutError('SECRET')
            return await base(conn, command, timeout)
        with mock.patch.object(self, 'capture', side_effect=timed):
            result = await self.execute(plan)
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['mutation_attempted'])
        self.assertFalse(result['apply_pending'])
        self.assertEqual(result['pulls'][0]['status'], 'unknown')
        self.assertNotIn('SECRET', repr(result))

    async def test_cancellation_keeps_unknown_partial_change(self):
        plan = await self.plan()
        self.pause_pull = True
        task = asyncio.create_task(self.execute(plan))
        for _ in range(100):
            if getattr(self, 'entered', None) and self.entered.is_set():
                break
            await asyncio.sleep(.001)
        self.assertTrue(self.entered.is_set())
        task.cancel()
        result = await task
        self.assertEqual(result['status'], 'cancelled')
        self.assertTrue(result['mutation_attempted'])
        self.assertEqual(result['pulls'][0]['status'], 'unknown')
        self.assertIn('teilweise verändert', result['note'])
        self.assertNotIn('Keine Änderungen', result['note'])
