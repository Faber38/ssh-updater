import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import unittest
from unittest import mock
import shlex
import test_docker_apply as fixtures
from sshupdater.core import docker_verification as verification, docker_image_updates as images, docker_preflight
from sshupdater.core.registry_session import RegistrySession

HOST = fixtures.fixtures.HOST
A, B, C = fixtures.A, fixtures.B, fixtures.C


class VerificationTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ApplyTests.setUp
    plan = fixtures.ApplyTests.plan
    prepare = fixtures.ApplyTests.prepare
    capture = fixtures.ApplyTests.capture

    async def applied(self):
        state = await self.prepare()
        result = await fixtures.ApplyTests.execute(self, state)
        self.assertEqual(result['status'], 'apply_succeeded')
        self.commands.clear()
        self.now = [0]
        self.session = RegistrySession(clock=lambda:self.now[0])
        return result

    async def verify(self, applied, previous=None, hosts=None):
        @asynccontextmanager
        async def connect(host):
            yield object()
        async def readonly(conn, command, timeout):
            args = shlex.split(command)
            self.assertFalse(set(args) & {'pull','up','down','restart','stop','start','build','push','rm','rmi','prune'})
            return await self.capture(conn, command, timeout)
        with mock.patch.object(verification, 'connect_host', connect), \
             mock.patch.object(docker_preflight.db, 'get_connection_context'), \
             mock.patch.object(images, 'capture', side_effect=readonly), \
             mock.patch('builtins.open', side_effect=AssertionError('persistence')):
            self.verifier = verification.VerificationRun(applied, hosts or {1:HOST}, self.session, previous)
            return await self.verifier.run()

    def registry_calls(self):
        return sum('imagetools' in c for c in self.commands)

    async def test_fresh_local_current_completes_without_mutation(self):
        applied = await self.applied()
        before = deepcopy(applied)
        result = await self.verify(applied)
        self.assertEqual(result['status'], 'verified', result)
        self.assertTrue(result['completed'])
        self.assertEqual(result['projects'][0]['image_updates']['summary'], '✓ aktuell')
        self.assertEqual(self.registry_calls(), 1)
        self.assertGreaterEqual(sum(c[:3] == ['docker','container','inspect'] for c in self.commands), 3)
        self.assertEqual(applied, before)

    async def test_matching_fresh_cache_reused_even_during_backoff(self):
        applied = await self.applied()
        key, limit = self.session.keys('nginx:1.28-alpine', verification.scope_for(HOST))
        self.session.put(key, self.remote)
        self.session.rate_limit(limit)
        self.assertEqual((await self.verify(applied))['status'], 'verified')
        self.assertEqual(self.registry_calls(), 0)

    async def test_old_mismatching_cache_is_not_completion_evidence(self):
        applied = await self.applied()
        key, limit = self.session.keys('nginx:1.28-alpine', verification.scope_for(HOST))
        self.session.put(key, fixtures.fixtures.fixtures.desc(A))
        self.assertEqual((await self.verify(applied))['status'], 'verified')
        self.assertEqual(self.registry_calls(), 1)
        self.assertEqual(self.session.get(key)['digest'], B)

    async def test_expired_cache_and_mismatched_cache_respect_backoff(self):
        applied = await self.applied()
        key, limit = self.session.keys('nginx:1.28-alpine', verification.scope_for(HOST))
        for digest, expired in ((B, True), (A, False)):
            self.session.put(key, fixtures.fixtures.fixtures.desc(digest))
            if expired:
                self.now[0] += RegistrySession.TTL
            self.session.rate_limit(limit)
            result = await self.verify(applied)
            self.assertEqual(result['status'], 'pending')
            self.assertEqual(result['projects'][0]['reason'], 'rate_limit_backoff')
        self.assertEqual(self.registry_calls(), 0)

    async def test_new_registry_digest_closes_cycle_but_keeps_new_candidate(self):
        applied = await self.applied()
        self.remote = fixtures.fixtures.fixtures.desc(C)
        result = await self.verify(applied)
        self.assertEqual(result['status'], 'update_available')
        self.assertTrue(result['completed'])
        self.assertEqual(result['apply_status'], 'apply_succeeded')
        self.assertEqual(result['projects'][0]['image_updates']['summary'], '↑ 1 Image-Update')
        self.assertEqual(applied['status'], 'apply_succeeded')

    async def test_registry_errors_keep_apply_success_and_pending(self):
        from sshupdater.core.remote_process import RemoteTimeoutError
        cases = [('rate_limit', (1,'','429 Too Many Requests SECRET')),
                 ('timeout', RemoteTimeoutError('SECRET')),
                 ('network', (1,'','no such host SECRET')),
                 ('auth', (1,'','401 Unauthorized SECRET')),
                 ('not_found', (1,'','manifest unknown SECRET'))]
        for reason, error in cases:
            with self.subTest(reason=reason):
                self.setUp()
                applied = await self.applied()
                self.errors['registry'] = error
                result = await self.verify(applied)
                self.assertEqual(result['projects'][0]['status'], 'registry_pending', result)
                self.assertEqual(result['projects'][0]['reason'], reason)
                self.assertTrue(result['verification_pending'])
                self.assertEqual(applied['status'], 'apply_succeeded')
                self.assertNotIn('SECRET', repr(result))
                if reason == 'rate_limit':
                    count = self.registry_calls()
                    result = await self.verify(applied, result)
                    self.assertEqual(self.registry_calls(), count)
                    self.assertEqual(result['projects'][0]['reason'], 'rate_limit_backoff')

    async def test_local_drift_never_claims_verified_or_queries_registry(self):
        changes = [lambda:self.container['State'].update(Running=False),
                   lambda:self.container.update(Id='1'*64),
                   lambda:self.container.update(Image=A),
                   lambda:self.local[B].update(Architecture='arm64'),
                   lambda:self.container['State'].update(StartedAt='changed'),
                   lambda:self.container.update(RestartCount=2),
                   lambda:self.config['services']['web'].update(image='other:tag'),
                   lambda:self.container['Config']['Labels'].update({images.LABEL+'service':'other'})]
        for change in changes:
            self.setUp()
            applied = await self.applied()
            change()
            result = await self.verify(applied)
            self.assertEqual(result['projects'][0]['status'], 'local_changed')
            self.assertFalse(result['completed'])
            self.assertEqual(self.registry_calls(), 0)
            self.assertEqual(applied['status'], 'apply_succeeded')

    async def test_partial_multi_host_receipt_survives_later_failure_and_retry(self):
        applied = await self.applied()
        other = deepcopy(applied['applies'][0]); other['host_id'] = 2
        applied['applies'].append(other)
        result = await self.verify(applied)  # Host 2 absent: project 1 still verified.
        self.assertEqual([r['status'] for r in result['projects']], ['verified','local_changed'])
        self.assertTrue(result['verification_pending'])
        count = len(self.commands)
        self.remote = fixtures.fixtures.fixtures.desc(C)
        result2 = await self.verify(applied, result, {1:HOST, 2:dict(HOST,id=2)})
        self.assertTrue(result2['completed'])
        self.assertEqual(result2['projects'][0], result['projects'][0])
        self.assertGreater(len(self.commands), count)

    async def test_cancel_retains_apply_success_and_completed_receipts(self):
        applied = await self.applied()
        async def wait(*args):
            await asyncio.Event().wait()
        with mock.patch.object(images.Checker, 'remote', side_effect=wait):
            task = asyncio.create_task(self.verify(applied))
            await asyncio.sleep(.01)
            task.cancel()
            result = await task
        self.assertEqual(result['status'], 'cancelled')
        self.assertTrue(result['verification_pending'])
        self.assertEqual(applied['status'], 'apply_succeeded')

    async def test_normal_run_without_pending_does_nothing(self):
        applied = await self.applied()
        applied['verification_pending'] = False
        result = await self.verify(applied)
        self.assertFalse(result['completed'])
        self.assertFalse(self.commands)

    async def test_registry_429_on_second_host_preserves_first_verified_receipt(self):
        ref = 'localhost:5000/test:latest'
        self.config['services']['web']['image'] = ref
        self.container['Config']['Image'] = ref
        applied = await self.applied()
        other = deepcopy(applied['applies'][0]); other['host_id'] = 2
        applied['applies'].append(other)
        @asynccontextmanager
        async def connect(host):
            if host['id'] == 2:
                self.errors['registry'] = (1,'','HTTP 429 SECRET')
            yield object()
        with mock.patch.object(verification,'connect_host',connect), \
             mock.patch.object(docker_preflight.db,'get_connection_context'), \
             mock.patch.object(images,'capture',side_effect=self.capture):
            result = await verification.VerificationRun(applied,{1:HOST,2:dict(HOST,id=2)},self.session).run()
        self.assertEqual([r['status'] for r in result['projects']],['verified','registry_pending'])
        self.assertEqual(result['projects'][1]['reason'],'rate_limit')
        count = self.registry_calls()
        again = await self.verify(applied,result,{1:HOST,2:dict(HOST,id=2)})
        self.assertEqual(self.registry_calls(),count)
        self.assertEqual(again['projects'][0],result['projects'][0])
        self.assertEqual(again['projects'][1]['reason'],'rate_limit_backoff')
        self.errors.clear()
        self.now[0] += 900
        final = await self.verify(applied,again,{1:HOST,2:dict(HOST,id=2)})
        self.assertTrue(final['completed'])
        self.assertEqual(final['projects'][0],result['projects'][0])

    async def test_container_change_during_registry_request_prevents_completion(self):
        applied = await self.applied()
        original = self.capture
        async def changed(conn, command, timeout):
            value = await original(conn,command,timeout)
            if 'imagetools' in command:
                self.container['Id'] = '2'*64
            return value
        with mock.patch.object(self,'capture',side_effect=changed):
            result = await self.verify(applied)
        self.assertEqual(result['projects'][0]['status'],'local_changed')
        self.assertNotIn('image_updates',result['projects'][0])
        self.assertFalse(result['completed'])
