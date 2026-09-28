import asyncio
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sshupdater.core import docker_image_updates as images
from sshupdater.core.registry_session import RegistrySession
import test_docker_image_updates as fixtures
from test_docker_image_updates import PROJECT, A, B, desc


class SessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 0
        self.cache = RegistrySession(clock=lambda: self.now)
        self.run = mock.AsyncMock(side_effect=self.response)
        self.response_value = desc()
        self.failure = None

    async def response(self, args, **kwargs):
        if args[-1] == 'version':
            return 'Buildx'
        if self.failure:
            raise images.CheckFailure(self.failure)
        return deepcopy(self.response_value)

    async def remote(self, ref='nginx:1.28-alpine', host='A', platform='linux/amd64'):
        checker = images.Checker(object(), self.cache, host)
        checker.run = self.run
        return await checker.remote(ref, platform)

    def queries(self):
        return sum(c.args[0][:4] == ['docker', 'buildx', 'imagetools', 'inspect'] for c in self.run.call_args_list)

    async def test_live_shared_normalized_hit_and_fixed_expiry(self):
        await self.remote()
        self.assertEqual(self.queries(), 1)
        self.now = 1799
        await self.remote('docker.io/library/nginx:1.28-alpine', 'B', 'linux/arm64')
        self.assertEqual(self.queries(), 1)
        self.now = 1800
        await self.remote()
        self.assertEqual(self.queries(), 2)

    async def test_private_localhost_and_unknown_hub_are_host_scoped(self):
        for ref in ('localhost:5000/test:latest', 'registry.example.com/team/app:latest', 'docker.io/team/private:latest'):
            before = self.queries()
            await self.remote(ref, 'A')
            await self.remote(ref, 'A')
            await self.remote(ref, 'B')
            self.assertEqual(self.queries() - before, 2)

    async def test_backoff_steps_cap_and_cross_host_suppression(self):
        self.failure = 'rate_limit'
        for delay in (900, 1800, 3600, 3600):
            with self.assertRaises(images.CheckFailure) as error:
                await self.remote()
            self.assertEqual(error.exception.kind, 'rate_limit')
            count = self.queries()
            self.now += delay - 1
            with self.assertRaises(images.CheckFailure) as error:
                await self.remote('httpd:2.4-alpine', 'B')
            self.assertEqual(error.exception.kind, 'rate_limit_backoff')
            self.assertEqual(self.queries(), count)
            self.now += 1

    async def test_fresh_during_backoff_but_expired_never_used(self):
        await self.remote()
        self.now = 1500
        self.failure = 'rate_limit'
        with self.assertRaises(images.CheckFailure):
            await self.remote('httpd:2.4-alpine')
        await self.remote(host='B')
        self.now = 1800
        with self.assertRaises(images.CheckFailure) as error:
            await self.remote(host='B')
        self.assertEqual(error.exception.kind, 'rate_limit_backoff')
        row = images.ImageCheck().fail(error.exception)
        self.assertEqual(images.project_result([row])['summary'], '⚠ Prüfung unvollständig')
        self.assertEqual(self.queries(), 2)

    async def test_errors_do_not_restore_expired_success_or_cross_scope_auth(self):
        for kind in ('auth', 'not_found', 'timeout', 'network'):
            self.failure = None
            await self.remote('private.example/app:tag', 'A')
            self.failure = kind
            with self.assertRaises(images.CheckFailure):
                await self.remote('private.example/app:tag', 'B')
            self.now += 1800
            with self.assertRaises(images.CheckFailure):
                await self.remote('private.example/app:tag', 'A')

    async def test_new_context_empty_no_files_or_database_calls(self):
        with mock.patch('builtins.open', side_effect=AssertionError('file')), \
             mock.patch('sqlite3.connect', side_effect=AssertionError('database')), \
             mock.patch('pathlib.Path.open', side_effect=AssertionError('file')):
            await self.remote()
            self.cache = RegistrySession(clock=lambda: self.now)
            await self.remote()
            self.assertEqual(self.queries(), 2)

    async def test_only_whitelisted_metadata_and_defensive_copies(self):
        self.response_value = desc(manifests=[desc(media=images.MANIFEST,
            platform={'os': 'linux', 'architecture': 'amd64'},
            annotations={'secret': 'TOKEN'}, urls=['SECRET'])], secret='TOKEN')
        first = await self.remote()
        self.assertNotIn('TOKEN', repr(self.cache._entries))
        self.assertNotIn('SECRET', repr(self.cache._entries))
        first['digest'] = B
        second = await self.remote()
        self.assertEqual(second['digest'], A)

    async def test_same_checker_cannot_reuse_expired_snapshot(self):
        checker = images.Checker(object(), self.cache, 'A')
        checker.run = self.run
        await checker.remote('nginx:1.28-alpine', 'linux/amd64')
        self.now = 1800
        self.response_value = desc(B)
        result = await checker.remote('nginx:1.28-alpine', 'linux/amd64')
        self.assertEqual(result['digest'], B)
        self.assertEqual(self.queries(), 2)

    async def test_local_registry_backoff_is_host_scoped(self):
        self.failure = 'rate_limit'
        with self.assertRaises(images.CheckFailure):
            await self.remote('localhost:5000/app:tag', 'A')
        self.failure = None
        await self.remote('localhost:5000/app:tag', 'B')
        with self.assertRaises(images.CheckFailure):
            await self.remote('localhost:5000/other:tag', 'A')
        self.assertEqual(self.queries(), 2)

    async def test_invalid_metadata_not_cached(self):
        self.response_value = {'digest': 'bad', 'mediaType': images.INDEX}
        for _ in range(2):
            with self.assertRaises(images.CheckFailure):
                await self.remote()
        self.assertEqual(self.queries(), 2)

    async def test_new_session_has_no_backoff(self):
        self.failure = 'rate_limit'
        with self.assertRaises(images.CheckFailure):
            await self.remote()
        self.cache = RegistrySession()
        self.failure = None
        await self.remote()
        self.assertEqual(self.queries(), 2)


class FreshLocalTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ProjectTests.setUp
    capture = fixtures.ProjectTests.capture

    async def test_local_container_and_comparison_refreshed_across_runs(self):
        cache = RegistrySession()
        with mock.patch.object(images, 'capture', side_effect=self.capture), \
             mock.patch.object(images, 'compare', wraps=images.compare) as compare:
            first = await images.check_projects(object(), [PROJECT], cache, 'host')
            self.container['Image'] = B
            self.local[B] = dict(Id=B, Descriptor=desc(B), Os='linux', Architecture='amd64')
            second = await images.check_projects(object(), [PROJECT], cache, 'host')
        self.assertEqual(first['test']['summary'], '✓ aktuell')
        self.assertEqual(second['test']['summary'], '↑ 1 Image-Update')
        self.assertEqual(compare.call_count, 2)
        self.assertEqual(sum(c[:3] == ['docker', 'container', 'inspect'] for c in self.commands), 2)
        self.assertIn(['docker', 'image', 'inspect', B], self.commands)
        self.assertEqual(sum(c[:4] == ['docker', 'buildx', 'imagetools', 'inspect'] for c in self.commands), 1)
        forbidden = {'pull', 'push', 'up', 'down', 'restart', 'build', 'prune', 'rm', 'rmi', 'start', 'stop'}
        self.assertFalse(any(forbidden.intersection(c[:4]) for c in self.commands))
