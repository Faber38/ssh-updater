"""ENV-2 lifecycle with real Core orchestration and fake SSH/Docker responses."""
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
import io
import json
import logging
import shlex
import unittest
from unittest import mock

import test_docker_dotenv as dotenv
from sshupdater.core import docker_image_updates as images, docker_context as contexts
from sshupdater.core import docker_preflight as preflight, docker_pull as pull
from sshupdater.core import docker_apply as apply, docker_verification as verification
from sshupdater.core.registry_session import RegistrySession
from sshupdater.docker_plan import build_plan, project_eligible

fixtures = dotenv.fixtures
HOST = dict(id=1, primary_ip='host', user='root', port=22, auth_method='key', key_path=None)
PULL = ['pull', '--policy', 'always', '--quiet', '--', 'web']
UP = ['up', '-d', '--no-deps', '--pull', 'never', '--no-build', '--', 'web']


class DotenvCycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        dotenv.DotenvTests.setUp(self)
        self.remote = fixtures.desc(fixtures.B)
        self.container['State']['StartedAt'] = 'before'
        self.container['RestartCount'] = 0
        self.config['services']['web']['environment']['PASSWORD'] = dotenv.SECRET
        self.tag = None
        self.mutations = []
        self.wrappers = []
        self.after_pull = lambda: None
        self.after_up = lambda: None
        self.drift_host = None
        self.fail_pull_host = None

    async def capture(self, conn, command, timeout):
        args = shlex.split(command)
        if args[:2] == ['sh', '-c'] and args[-1] in ('ssh-updater-dotenv-pull', 'ssh-updater-dotenv-up'):
            # Decode the shell's literal positional arguments; don't execute Docker.
            argv = shlex.split(args[2].splitlines()[0])[2:]
            operation = argv[argv.index('-f')+2:]
            self.assertEqual(argv[:8], ['--project-name', 'test', '--project-directory', '/srv/test',
                                       '--env-file', '/srv/test/.env', '-f', fixtures.PATH])
            self.assertNotIn(dotenv.SECRET, command)
            self.wrappers.append(args)
            self.mutations.append((conn, operation))
            if operation[0] == 'pull':
                self.assertEqual(operation[:5], PULL[:5])
                self.assertEqual(len(operation), 6)
                if conn == self.fail_pull_host:
                    return 1, '', 'denied: ' + dotenv.SECRET
                self.local[fixtures.B] = dict(self.local[fixtures.A], Id=fixtures.B, Descriptor=fixtures.desc(fixtures.B))
                self.tag = fixtures.B
                self.after_pull()
            else:
                self.assertEqual(operation[:7], UP[:7])
                services = operation[7:]
                for index, c in enumerate(self.containers):
                    if c['Config']['Labels'][images.LABEL+'service'] in services:
                        c.update(Id=('f' if index == 0 else 'e')*64, Image=self.tag, RestartCount=0)
                        c['State'].update(Running=True, StartedAt='after')
                        c['Config']['Labels'][images.LABEL+'project.environment_file'] = '/srv/test/.env'
                self.after_up()
            return 0, '', dotenv.SECRET
        if args[:3] == ['docker', 'container', 'inspect']:
            self.commands.append(args)
            return 0, json.dumps([c for c in self.containers if c['Id'] in args[3:]]), ''
        if args[:3] == ['docker', 'image', 'inspect'] and not args[3].startswith('sha256:'):
            self.commands.append(args)
            return (0, json.dumps([self.local[self.tag]]), '') if self.tag else (1, '', 'No such image')
        if args == contexts.config_command('test', fixtures.PATH, dotenv.DOTENV) and conn == self.drift_host:
            config = deepcopy(self.config)
            config['services']['web']['environment']['TEST_MESSAGE'] = 'changed'
            return 0, json.dumps(config), ''
        return await dotenv.DotenvTests.capture(self, conn, command, timeout)

    @asynccontextmanager
    async def connect(self, host):
        yield host['id']

    async def plan(self):
        with mock.patch.object(images, 'capture', side_effect=self.capture):
            checked = await images.Checker(1).project(fixtures.PROJECT)
        plan = build_plan([dict(host_id=1, connection_identity=preflight.connection_identity(HOST),
                               result_revision=1, projects=[dict(fixtures.PROJECT, image_updates=checked)])])
        self.assertEqual(len(plan.candidates), 1, checked)
        self.assertEqual(plan.candidates[0].compose_context, dotenv.DOTENV)
        self.assertEqual(plan.candidates[0].compose_identity, plan.selection[0].compose_identity)
        return plan

    async def pull(self, plan, hosts=None):
        with mock.patch.object(preflight, 'connect_host', self.connect), \
                mock.patch.object(pull, 'connect_host', self.connect), \
                mock.patch.object(preflight.db, 'get_connection_context'), \
                mock.patch.object(images, 'capture', side_effect=self.capture), \
                mock.patch.object(pull, 'capture', side_effect=self.capture), \
                mock.patch.object(images.Checker, 'remote', side_effect=AssertionError('registry in pull')):
            return await pull.PullRun(plan, hosts or {1: HOST}).run()

    async def prepared(self):
        plan = await self.plan()
        result = await self.pull(plan)
        self.assertEqual(result['status'], 'pulled', result)
        return dict(plan=plan, result=result)

    async def apply(self, state):
        with mock.patch.object(apply, 'connect_host', self.connect), \
                mock.patch.object(preflight.db, 'get_connection_context'), \
                mock.patch.object(images, 'capture', side_effect=self.capture), \
                mock.patch.object(apply, 'capture', side_effect=self.capture), \
                mock.patch.object(images.Checker, 'remote', side_effect=AssertionError('registry in apply')):
            return await apply.ApplyRun(state, {1: HOST}).run()

    async def verify(self, applied, session=None):
        with mock.patch.object(verification, 'connect_host', self.connect), \
                mock.patch.object(preflight.db, 'get_connection_context'), \
                mock.patch.object(images, 'capture', side_effect=self.capture):
            return await verification.VerificationRun(applied, {1: HOST}, session or RegistrySession()).run()

    async def test_bound_context_full_cycle_and_secrets(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logging.getLogger().addHandler(handler)
        try:
            with mock.patch('builtins.open', side_effect=AssertionError('file persistence')), \
                    mock.patch('sqlite3.connect', side_effect=AssertionError('DB persistence')):
                before = deepcopy(self.container)
                state = await self.prepared()
                self.assertEqual(self.container, before)
                self.assertEqual(self.mutations, [(1, PULL)])
                self.assertEqual(state['result']['pulls'][0]['containers_before'], state['result']['pulls'][0]['containers_after'])
                applied = await self.apply(state)
                self.assertEqual(applied['status'], 'apply_succeeded', applied)
                row = applied['applies'][0]
                self.assertEqual(row['compose_context'], dotenv.DOTENV)
                self.assertEqual(row['compose_identity'], state['plan'].selection[0].compose_identity)
                self.assertNotEqual(self.container['Id'], before['Id'])
                self.assertTrue(self.container['State']['Running'])
                self.assertEqual(row['containers'][0]['image_id'], fixtures.B)
                self.assertEqual(self.container['Config']['Labels'][images.LABEL+'project.environment_file'], dotenv.DOTENV[1])
                verified = await self.verify(applied)
                self.assertEqual(verified['status'], 'verified', verified)
                self.assertTrue(verified['completed'])
                self.assertEqual(verified['projects'][0]['image_updates']['compose_context'], dotenv.DOTENV)
                self.assertEqual(self.mutations, [(1, PULL), (1, UP)])
                self.assertNotIn(dotenv.SECRET, repr((state, applied, verified, self.commands, self.wrappers)))
                self.assertNotIn(dotenv.SECRET, output.getvalue())
        finally:
            logging.getLogger().removeHandler(handler)

    async def test_dotenv_drift_before_pull_stops_entire_plan(self):
        plan = await self.plan()
        self.config['services']['web']['environment']['TEST_MESSAGE'] = 'B'
        result = await self.pull(plan)
        self.assertEqual(result['reason'], 'config')
        self.assertFalse(result['mutation_attempted'])
        self.assertEqual(self.mutations, [])

    async def test_drift_between_overall_and_immediate_preflight_stops_pull(self):
        plan = await self.plan()
        actual = preflight.preflight
        async def drift(*args):
            result = await actual(*args)
            self.config['services']['web']['environment']['TEST_MESSAGE'] = 'B'
            return result
        with mock.patch.object(preflight, 'preflight', side_effect=drift):
            result = await self.pull(plan)
        self.assertEqual(result['reason'], 'config')
        self.assertFalse(result['mutation_attempted'])
        self.assertEqual(self.mutations, [])

    async def test_dotenv_drift_after_pull_prevents_apply(self):
        state = await self.prepared()
        self.config['services']['web']['environment']['TEST_MESSAGE'] = 'B'
        result = await self.apply(state)
        self.assertEqual(result['reason'], 'config')
        self.assertFalse(result['mutation_attempted'])
        self.assertEqual(self.mutations, [(1, PULL)])

    async def test_foreign_environment_label_blocks_before_pull(self):
        plan = await self.plan()
        self.container['Config']['Labels'][images.LABEL+'project.environment_file'] = '/foreign/.env'
        result = await self.pull(plan)
        self.assertFalse(result['mutation_attempted'])
        self.assertEqual(self.mutations, [])

    async def test_foreign_label_after_apply_is_not_accepted(self):
        state = await self.prepared()
        self.after_up = lambda: self.container['Config']['Labels'].update(
            {images.LABEL+'project.environment_file': '/foreign/'+dotenv.SECRET})
        result = await self.apply(state)
        self.assertEqual(result['reason'], 'context')
        self.assertEqual(result['applies'][0]['status'], 'unknown')
        self.assertFalse(result['verification_pending'])
        self.assertNotIn(dotenv.SECRET, repr(result))

    async def test_post_apply_config_drift_prevents_success(self):
        state = await self.prepared()
        self.after_up = lambda: self.config['services']['web']['environment'].update(TEST_MESSAGE='B')
        result = await self.apply(state)
        self.assertEqual(result['reason'], 'config')
        self.assertTrue(result['mutation_attempted'])
        self.assertFalse(result['verification_pending'])

    async def test_context_change_with_same_hash_is_rejected_after_apply(self):
        self.source = 'services:\n  web:\n    image: nginx:1.28-alpine\n'
        state = await self.prepared()
        self.after_up = lambda: self.container['Config']['Labels'].update(
            {images.LABEL+'project.environment_file': '/dev/null'})
        result = await self.apply(state)
        self.assertEqual(result['reason'], 'config')
        self.assertFalse(result['verification_pending'])

    async def test_verification_rejects_context_change_even_with_same_hash(self):
        self.source = 'services:\n  web:\n    image: nginx:1.28-alpine\n'
        applied = await self.apply(await self.prepared())
        self.assertEqual(applied['status'], 'apply_succeeded')
        self.container['Config']['Labels'][images.LABEL+'project.environment_file'] = '/dev/null'
        result = await self.verify(applied)
        self.assertEqual(result['projects'][0]['reason'], 'config')
        self.assertFalse(result['completed'])

    async def test_verification_config_drift_is_not_verified(self):
        applied = await self.apply(await self.prepared())
        self.config['services']['web']['environment']['TEST_MESSAGE'] = 'B'
        result = await self.verify(applied)
        self.assertEqual(result['projects'][0]['status'], 'local_changed')
        self.assertEqual(result['projects'][0]['reason'], 'config')
        self.assertFalse(result['completed'])

    async def test_verification_does_not_publish_transiently_different_check_identity(self):
        applied = await self.apply(await self.prepared())
        original = images.Checker.project
        async def changed(checker, project):
            result = await original(checker, project)
            identity = result['compose_identity']
            result['compose_identity'] = (identity[0], '0'*64, identity[2])
            return result
        with mock.patch.object(images.Checker, 'project', changed):
            result = await self.verify(applied)
        self.assertEqual(result['projects'][0]['reason'], 'config')
        self.assertFalse(result['completed'])
        self.assertNotIn('image_updates', result['projects'][0])

    async def test_verification_backoff_unchanged(self):
        applied = await self.apply(await self.prepared())
        session = RegistrySession()
        _, limit = session.keys('nginx:1.28-alpine', verification.scope_for(HOST))
        session.rate_limit(limit)
        result = await self.verify(applied, session)
        self.assertEqual(result['projects'][0]['reason'], 'rate_limit_backoff')
        self.assertFalse(result['completed'])

    async def test_tampered_candidates_and_missing_identity_never_start_pull(self):
        plan = await self.plan()
        candidate = plan.candidates[0]
        changed_image = list(candidate.images[0]); changed_image[0] = 'unapproved'
        for changed in (replace(candidate, images=(tuple(changed_image),)),
                        replace(candidate, compose_context=contexts.EMPTY),
                        replace(candidate, compose_context=(contexts.PROJECT_DOTENV_CONTEXT, '/foreign/.env'))):
            result = await self.pull(replace(plan, candidates=(changed,)))
            self.assertEqual(result['reason'], 'plan')
            self.assertFalse(result['mutation_attempted'])
        for context, identity in ((dotenv.DOTENV, ()), (('UNKNOWN', ''), candidate.compose_identity),
                                 ((contexts.PROJECT_DOTENV_CONTEXT, '/foreign/.env'), candidate.compose_identity)):
            changed = replace(candidate, compose_context=context, compose_identity=identity)
            bad = replace(plan, selection=(changed,), candidates=(changed,))
            result = await self.pull(bad)
            self.assertEqual(result['reason'], 'plan')
            self.assertFalse(result['mutation_attempted'])
            self.assertFalse(project_eligible(changed.paths, changed.images, context, identity))
        self.assertEqual(self.mutations, [])

    async def test_tampered_apply_candidates_are_rejected(self):
        state = await self.prepared()
        candidate = state['plan'].candidates[0]
        changed = replace(candidate, compose_context=(contexts.PROJECT_DOTENV_CONTEXT, '/foreign/.env'))
        state['plan'] = replace(state['plan'], candidates=(changed,))
        result = await self.apply(state)
        self.assertFalse(result['mutation_attempted'])
        self.assertEqual(self.mutations, [(1, PULL)])

    async def test_multi_host_preflight_failure_prevents_all_pulls(self):
        plan = await self.plan()
        second = replace(plan.selection[0], host_id=2)
        plan = replace(plan, selection=plan.selection+(second,), candidates=plan.candidates+(second,))
        self.drift_host = 2
        result = await self.pull(plan, {1: HOST, 2: dict(HOST, id=2)})
        self.assertEqual(result['reason'], 'config')
        self.assertFalse(result['mutation_attempted'])
        self.assertEqual(self.mutations, [])

    async def test_multi_host_partial_pull_failure_stops_later_hosts(self):
        plan = await self.plan()
        others = tuple(replace(plan.selection[0], host_id=i) for i in (2, 3))
        plan = replace(plan, selection=plan.selection+others, candidates=plan.candidates+others)
        self.fail_pull_host = 2
        result = await self.pull(plan, {i: dict(HOST, id=i) for i in (1, 2, 3)})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual([r['status'] for r in result['pulls']], ['pulled', 'failed'])
        self.assertEqual(self.mutations, [(1, PULL), (2, PULL)])
        self.assertNotIn(dotenv.SECRET, repr(result))

    async def test_only_update_services_are_pulled_and_applied(self):
        current = deepcopy(self.container)
        current.update(Id='c'*64, Name='/test-current-1', Image=fixtures.B)
        current['Config']['Labels'][images.LABEL+'service'] = 'current'
        self.containers.append(current)
        self.config['services']['current'] = {'image': 'nginx:1.28-alpine'}
        self.local[fixtures.B] = dict(self.local[fixtures.A], Id=fixtures.B, Descriptor=fixtures.desc(fixtures.B))
        before = deepcopy(current)
        state = await self.prepared()
        self.assertEqual(len(state['plan'].selection[0].images), 2)
        self.assertEqual(len(state['plan'].candidates[0].images), 1)
        result = await self.apply(state)
        self.assertEqual(result['status'], 'apply_succeeded', result)
        self.assertEqual(current, before)
        self.assertEqual(self.mutations, [(1, PULL), (1, UP)])
