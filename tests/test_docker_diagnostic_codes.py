"""Codes add evidence, never change admission, commands, or approval."""
from copy import deepcopy
import json
import shlex
import unittest
from unittest import mock

import test_docker_image_updates as fixtures
import test_docker_dotenv as dotenv
from sshupdater.core import docker_image_updates as images, docker_context as contexts
from sshupdater.core.docker_diagnostic_codes import DIAGNOSTICS, MULTI_REASONS, allowed_code
from sshupdater.core.registry_session import RegistrySession
from sshupdater.docker_diagnostics import diagnostic_details, report
from sshupdater.docker_plan import build_plan

SECRET = 'SECRET_SENTINEL_PRIVATE_ä'
CONFIG = images.compose_command('test', fixtures.PATH) + [
    'config', '--format', 'json', '--no-interpolate', '--no-env-resolution']
COMMANDS = [
    ['cat', '--', fixtures.PATH],
    ['docker', 'container', 'ls', '--quiet', '--no-trunc', '--filter', 'label=com.docker.compose.project=test'],
    ['docker', 'container', 'inspect', fixtures.CID],
    contexts.probe_command(fixtures.PATH), CONFIG,
    ['docker', 'image', 'inspect', fixtures.A],
    ['docker', 'buildx', 'version'],
    ['docker', 'buildx', 'imagetools', 'inspect', 'docker.io/library/nginx:1.28-alpine', '--format', '{{json .Manifest}}'],
    ['cat', '--', fixtures.PATH],
]


def plan(checked):
    return build_plan([dict(host_id=1, connection_identity=('host', 'root', 22, 'key', None),
                           result_revision=1, projects=[dict(fixtures.PROJECT, image_updates=checked)])])


class ImageCodeTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ProjectTests.setUp
    capture = fixtures.ProjectTests.capture
    check = fixtures.ProjectTests.check

    def rejected(self, result, reason, code, commands):
        row, = result['images']
        self.assertEqual((row['status'], row['reason'], row['diagnostic_code']), ('uncheckable', reason, code))
        self.assertEqual(row['note'], images.REASONS[reason])
        self.assertEqual(self.commands, commands)
        self.assertFalse(plan(result).candidates)
        self.assertNotIn(SECRET, json.dumps(result))
        return row

    async def test_source_filter_and_interpolation_keep_early_exit(self):
        for source, code in [('services: {}\n# include ' + SECRET, 'COMPOSE_SOURCE_FILTER'),
                             ('services: {}\n# $UNKNOWN', 'COMPOSE_INTERPOLATION_REJECTED')]:
            with self.subTest(code=code):
                self.setUp()
                self.source = source
                self.rejected(await self.check(), 'context', code, COMMANDS[:1])

    async def test_file_binding_has_no_remote_commands(self):
        result = await self.check(dict(fixtures.PROJECT, config_files=['relative.yaml']))
        self.rejected(result, 'context', 'COMPOSE_FILE_BINDING_INVALID', [])

    async def test_container_binding_and_missing_service_label(self):
        for field, value, code in [('project.working_dir', '/other', 'COMPOSE_CONTAINER_BINDING_INVALID'),
                                   ('service', '', 'COMPOSE_SERVICE_LABEL_MISSING')]:
            self.setUp()
            self.container['Config']['Labels'][images.LABEL + field] = value
            self.rejected(await self.check(), 'context', code, COMMANDS[:3])
        self.setUp()
        self.container['State']['Running'] = False
        self.rejected(await self.check(), 'context', 'COMPOSE_CONTAINER_BINDING_INVALID', COMMANDS[:3])

    async def test_environment_label_conflict(self):
        other = deepcopy(self.container)
        other['Id'] = 'e' * 64
        self.container['Config']['Labels'][images.LABEL + 'project.environment_file'] = '/dev/null'
        other['Config']['Labels'][images.LABEL + 'project.environment_file'] = '/srv/test/.env'
        self.containers.append(other)
        commands = deepcopy(COMMANDS[:3])
        commands[2].append(other['Id'])
        self.rejected(await self.check(), 'context', 'COMPOSE_ENV_LABEL_CONFLICT', commands)

    async def test_expected_dotenv_absent_and_empty_dollar(self):
        self.container['Config']['Labels'][images.LABEL + 'project.environment_file'] = '/srv/test/.env'
        self.rejected(await self.check(), 'context', 'COMPOSE_ENV_EXPECTED_BUT_ABSENT', COMMANDS[:4])
        self.setUp()
        self.source += '# ${SIMPLE}'
        self.rejected(await self.check(), 'context', 'COMPOSE_EMPTY_CONTEXT_DOLLAR_REJECTED', COMMANDS[:4])

    async def test_config_failure_json_and_no_registry_misclassification(self):
        for response, reason, code in [((1, '', SECRET), 'command_error', 'COMPOSE_CONFIG_FAILED'),
                                       ((0, SECRET, ''), 'invalid_output', 'COMPOSE_CONFIG_INVALID_JSON'),
                                       ((1, '', '401 ' + SECRET), 'auth', 'COMPOSE_CONFIG_FAILED')]:
            self.setUp()
            base = self.capture
            async def capture(conn, command, timeout):
                if shlex.split(command) == CONFIG:
                    self.commands.append(CONFIG)
                    return response
                return await base(conn, command, timeout)
            with mock.patch.object(images, 'capture', side_effect=capture):
                result = await images.Checker(object()).project(fixtures.PROJECT)
            row = self.rejected(result, reason, code, COMMANDS[:5])
            self.assertEqual(row['exit_code'], response[0])
            self.assertEqual(row['command'], shlex.join(CONFIG))
            self.assertNotIn('REGISTRY_AUTH_FAILED', report({}, dict(image_updates=result)))

    async def test_service_mapping_and_policy(self):
        self.config['services'] = {}
        self.rejected(await self.check(), 'context', 'COMPOSE_SERVICE_MAPPING_INVALID', COMMANDS[:5])
        self.setUp()
        self.config['services']['web']['profiles'] = ['test']
        self.rejected(await self.check(), 'context', 'COMPOSE_SERVICE_POLICY_REJECTED', COMMANDS[:5])

    async def test_missing_and_invalid_descriptor(self):
        for value, code in [(None, 'IMAGE_DESCRIPTOR_MISSING'), ({}, 'IMAGE_DESCRIPTOR_INVALID'),
                            (fixtures.desc(SECRET), 'IMAGE_DESCRIPTOR_INVALID')]:
            self.setUp()
            self.local[fixtures.A]['Descriptor'] = value
            self.rejected(await self.check(), 'identity', code, COMMANDS[:6] + COMMANDS[-1:])

    async def test_reference_and_image_id_failures(self):
        cases = [('reference', 'context', 'IMAGE_REFERENCE_INVALID', 5),
                 ('drift', 'drift', 'IMAGE_REFERENCE_DRIFT', 5),
                 ('id', 'identity', 'IMAGE_ID_INVALID', 5),
                 ('metadata_id', 'identity', 'IMAGE_ID_MISMATCH', 6)]
        for change, reason, code, count in cases:
            self.setUp()
            if change == 'reference':
                self.config['services']['web']['image'] = 'https://invalid'
            elif change == 'drift':
                self.container['Config']['Image'] = 'nginx:other'
            elif change == 'id':
                self.container['Image'] = 'invalid'
            else:
                self.local[fixtures.A]['Id'] = fixtures.B
            self.rejected(await self.check(), reason, code, COMMANDS[:count] + COMMANDS[-1:])

    async def test_platform_conflict_keeps_registry_query_sequence(self):
        self.config['services']['web']['platform'] = 'linux/arm64'
        self.rejected(await self.check(), 'platform', 'IMAGE_PLATFORM_MISMATCH', COMMANDS)

    async def test_registry_codes_keep_primary_reason_exit_and_commands(self):
        for reason, message, code in [('auth', '401', 'REGISTRY_AUTH_FAILED'),
                                      ('rate_limit', '429', 'REGISTRY_RATE_LIMIT'),
                                      ('network', 'TLS', 'REGISTRY_NETWORK_FAILED'),
                                      ('timeout', 'timed out', 'REGISTRY_TIMEOUT')]:
            self.setUp()
            self.errors['registry'] = (1, '', message + ' ' + SECRET)
            row = self.rejected(await self.check(), reason, code, COMMANDS)
            self.assertEqual(row['exit_code'], 1)
            self.assertEqual(row['command'], shlex.join(COMMANDS[7]))

    async def test_registry_metadata_validation(self):
        for value, reason in [(None, 'identity'), (fixtures.desc(manifests=SECRET), 'invalid_output')]:
            self.setUp()
            self.remote = value
            self.rejected(await self.check(), reason, 'REGISTRY_METADATA_INVALID', COMMANDS)

    async def test_registry_backoff_preserves_no_query(self):
        session = RegistrySession()
        _, key = session.keys('nginx:1.28-alpine', 'host')
        session.rate_limit(key)
        with mock.patch.object(images, 'capture', side_effect=self.capture):
            result = await images.Checker(object(), session, 'host').project(fixtures.PROJECT)
        self.rejected(result, 'rate_limit_backoff', 'REGISTRY_RATE_LIMIT_BACKOFF', COMMANDS[:6] + COMMANDS[-1:])

    async def test_source_drift_project_abort_preserves_code(self):
        base = self.capture
        reads = 0
        async def capture(conn, command, timeout):
            nonlocal reads
            if shlex.split(command) == COMMANDS[0]:
                reads += 1
                if reads == 2:
                    self.source += '\n# ' + SECRET
            return await base(conn, command, timeout)
        with mock.patch.object(images, 'capture', side_effect=capture):
            result = await images.Checker(object()).project(fixtures.PROJECT)
        self.rejected(result, 'context', 'COMPOSE_SOURCE_CHANGED', COMMANDS)
        self.assertNotIn('compose_identity', result)

    async def test_valid_empty_context_current_and_update_approval_unchanged(self):
        for updated in (False, True):
            self.setUp()
            if updated:
                self.remote = fixtures.desc(fixtures.B)
            result = await self.check()
            self.assertEqual(self.commands, COMMANDS)
            row, = result['images']
            self.assertIsNone(row['diagnostic_code'])
            self.assertEqual(row['reason'], '')
            self.assertEqual(row['status'], 'update_available' if updated else 'current')
            self.assertEqual(bool(plan(result).candidates), updated)
            old = deepcopy(result)
            old['images'][0].pop('diagnostic_code')
            self.assertEqual(plan(old), plan(result))
            self.assertIn('Kein Fehler festgestellt.', report({}, dict(image_updates=result)))

    async def test_missing_image_fallback_keeps_original_error(self):
        self.errors['image'] = (1, '', 'No such image: ' + fixtures.A)
        result = await self.check()
        self.rejected(result, 'local_image_unavailable', None, COMMANDS[:6] + COMMANDS[-1:])


class DotenvCodeTests(unittest.IsolatedAsyncioTestCase):
    setUp = dotenv.DotenvTests.setUp
    capture = dotenv.DotenvTests.capture
    check = dotenv.DotenvTests.check

    def expected(self):
        commands = deepcopy(COMMANDS)
        commands[4] = contexts.config_command('test', fixtures.PATH, dotenv.DOTENV)
        return commands + [commands[3], commands[4]]

    async def test_probe_failure_and_config_stderr(self):
        for probe, response, code in [(True, (0, ''), 'COMPOSE_ENV_PROBE_FAILED'),
                                       (False, (0, SECRET), 'COMPOSE_CONFIG_STDERR')]:
            self.setUp()
            self.probe_error = probe
            self.config_error = response
            result = await self.check()
            row, = result['images']
            self.assertEqual((row['status'], row['reason'], row['diagnostic_code']), ('uncheckable', 'context', code))
            self.assertIsNone(row['command'])
            self.assertIsNone(row['exit_code'])
            self.assertEqual(self.commands, self.expected()[:4 if probe else 5])
            self.assertNotIn(SECRET, json.dumps(result))

    async def test_environment_rejection(self):
        self.config['services']['web']['environment'] = {'APP': None}
        result = await self.check()
        row, = result['images']
        self.assertEqual(row['diagnostic_code'], 'COMPOSE_ENVIRONMENT_REJECTED')
        self.assertEqual(row['reason'], 'context')
        self.assertEqual(self.commands, self.expected()[:5])

    async def test_config_and_context_drift_preserve_short_circuit(self):
        for drift, code in [('config', 'COMPOSE_CONFIG_CHANGED'), ('context', 'COMPOSE_CONTEXT_CHANGED')]:
            self.setUp()
            base = self.capture
            probes = 0
            async def capture(conn, command, timeout):
                nonlocal probes
                if shlex.split(command) == contexts.probe_command(fixtures.PATH):
                    probes += 1
                    if probes == 2:
                        if drift == 'context':
                            self.kind = contexts.EMPTY_CONTEXT
                        else:
                            self.config['services']['web']['environment']['TEST_MESSAGE'] = SECRET
                return await base(conn, command, timeout)
            with mock.patch.object(images, 'capture', side_effect=capture):
                result = await images.Checker(object()).project(fixtures.PROJECT)
            row, = result['images']
            self.assertEqual((row['status'], row['reason'], row['diagnostic_code']), ('uncheckable', 'config', code))
            self.assertEqual(self.commands, self.expected() if drift == 'config' else self.expected()[:-1])
            self.assertFalse(plan(result).candidates)
            self.assertNotIn(SECRET, json.dumps(result))


class ComparisonCodeTests(unittest.TestCase):
    def test_comparison_subcauses_preserve_primary_classes(self):
        manifest = fixtures.desc(media=images.MANIFEST)
        entry = dict(manifest, platform={'os': 'linux', 'architecture': 'amd64'})
        cases = [
            (fixtures.desc(), fixtures.desc(), 'unknown/amd64', 'platform', 'IMAGE_PLATFORM_INVALID'),
            (manifest, fixtures.desc(manifests=[]), 'linux/amd64', 'platform', 'IMAGE_PLATFORM_NOT_FOUND'),
            (manifest, fixtures.desc(manifests=[entry, entry]), 'linux/amd64', 'platform', 'IMAGE_PLATFORM_AMBIGUOUS'),
            (fixtures.desc(), fixtures.desc(media=images.LIST), 'linux/amd64', 'identity', 'IMAGE_DESCRIPTOR_TYPE_MISMATCH'),
            (manifest, manifest, 'linux/amd64', 'identity', 'IMAGE_COMPARISON_UNSUPPORTED'),
        ]
        for local, remote, platform, reason, code in cases:
            with self.subTest(code=code), self.assertRaises(images.CheckFailure) as caught:
                images.compare(local, remote, platform)
            self.assertEqual((caught.exception.kind, caught.exception.diagnostic_code), (reason, code))


class ExportCodeTests(unittest.TestCase):
    def test_all_codes_have_fixed_bilingual_descriptions_and_roundtrip(self):
        for code, (reason, german, english) in DIAGNOSTICS.items():
            reasons = (reason,) if reason else MULTI_REASONS[code]
            for primary in reasons:
                error = images.CheckFailure(primary, diagnostic_code=code)
                row = images.project_result([images.ImageCheck().fail(error)])['images'][0]
                self.assertEqual(row['diagnostic_code'], code)
                self.assertEqual(diagnostic_details(row, 'de'), (code, german))
                self.assertEqual(diagnostic_details(row, 'en'), (code, english))
                self.assertTrue(german and english)
                self.assertNotEqual(german, english)

    def test_old_unknown_manipulated_and_mismatched_codes(self):
        for code in (None, '', SECRET, 'COMPOSE_SOURCE_FILTER\n' + SECRET, {}, [], 42, 'REGISTRY_AUTH_FAILED'):
            row = dict(status='uncheckable', reason='context', diagnostic_code=code,
                       note=SECRET, command=SECRET, environment={'PASSWORD': SECRET})
            text = report({'name': SECRET}, {'name': SECRET, 'image_updates': {'images': [row]}})
            self.assertIn('Genauere Ursache im Prüfergebnis nicht erfasst.', text)
            self.assertNotIn(SECRET, text)
            self.assertIsNone(allowed_code(code, 'context'))
        self.assertEqual(diagnostic_details({'status': 'uncheckable', 'reason': 'context'})[1],
                         'Genauere Ursache im Prüfergebnis nicht erfasst.')

    def test_success_special_status_and_conflicts(self):
        for state in ('current', 'update_available'):
            row = dict(status=state, reason='')
            self.assertEqual(diagnostic_details(row)[1], 'Kein Fehler festgestellt.')
            self.assertEqual(diagnostic_details(row, 'en')[1], 'No error detected.')
            row['diagnostic_code'] = SECRET
            self.assertIn('nicht erfasst', diagnostic_details(row)[1])
        for state in ('local_build', 'digest_pinned'):
            text = diagnostic_details(dict(status=state))[1]
            self.assertIn('keine Registry-', text)
            self.assertNotIn('Kein Fehler festgestellt.', text)

    def test_known_code_never_exports_raw_values(self):
        row = dict(status='uncheckable', reason='context', diagnostic_code='COMPOSE_SOURCE_FILTER',
                   service=SECRET, image=SECRET, note=SECRET, command=SECRET, exception=SECRET,
                   environment={'SECRET': SECRET}, config={'SECRET': SECRET}, stdout=SECRET, stderr=SECRET)
        text = report({'name': SECRET}, dict(name=SECRET, config_files=[SECRET], image_updates={'images': [row]}))
        self.assertIn('Diagnosecode: COMPOSE_SOURCE_FILTER', text)
        self.assertIn(DIAGNOSTICS['COMPOSE_SOURCE_FILTER'][1], text)
        self.assertNotIn(SECRET, text)
