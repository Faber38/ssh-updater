import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import shlex
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sshupdater.core import docker_image_updates as images, docker_compose, ssh_client
from sshupdater.core.remote_process import RemoteTimeoutError

A, B, C = ['sha256:' + c * 64 for c in 'abc']
CID = 'd' * 64
PATH = '/srv/test/compose.yaml'
PROJECT = dict(name='test', config_files=[PATH], config_files_raw=PATH)


def desc(digest=A, media=images.INDEX, **kwargs):
    return dict(digest=digest, mediaType=media, **kwargs)


class ComparisonTests(unittest.TestCase):
    def test_modern_index_equal_and_different(self):
        for kind in images.INDEX_TYPES:
            for remote, expected in [(A, 'current'), (B, 'update_available')]:
                with self.subTest(kind=kind, digest=remote):
                    status, level, _ = images.compare(desc(A, kind), desc(remote, kind), 'linux/amd64')
                    self.assertEqual((status, level), (expected, 'index'))

    def test_digest_levels_and_unknown_formats_never_coerced(self):
        for local, remote in [(None, desc()), ({'digest': A}, desc()),
                              (desc(media=images.INDEX), desc(media=images.MANIFEST)),
                              (desc(media=images.LIST), desc(media=images.INDEX)),
                              (desc(media='application/unknown'), desc()),
                              (desc('sha256:bad'), desc()),
                              (desc(media=images.MANIFEST), desc(media=images.MANIFEST))]:
            with self.subTest(local=local), self.assertRaises(images.CheckFailure):
                images.compare(local, remote, 'linux/amd64')

    def test_platform_selection_amd64_arm64_and_attestations(self):
        remote = desc(manifests=[
            desc(C, images.MANIFEST, platform=dict(os='unknown', architecture='unknown')),
            desc(A, images.MANIFEST, platform=dict(os='linux', architecture='amd64')),
            desc(B, images.MANIFEST, platform=dict(os='linux', architecture='arm64'))])
        for architecture, digest in [('amd64', A), ('arm64', B)]:
            with self.subTest(architecture=architecture):
                status, level, selected = images.compare(desc(digest, images.MANIFEST), remote, 'linux/' + architecture)
                self.assertEqual((status, level, selected['digest']), ('current', 'platform_manifest', digest))
        remote['manifests'].append(desc(A, images.MANIFEST, platform=dict(os='linux', architecture='amd64')))
        with self.assertRaises(images.CheckFailure):
            images.compare(desc(A, images.MANIFEST), remote, 'linux/amd64')

    def test_platform_variant_and_compose_constraints(self):
        remote = desc(manifests=[desc(A, images.MANIFEST, platform=dict(os='linux', architecture='arm64', variant='v8'))])
        self.assertEqual(images.compare(desc(A, images.MANIFEST), remote, 'linux/arm64/v8', 'linux/arm64')[0], 'current')
        for platform, requested in [('linux/arm64', ''), ('linux/arm64/v8', 'linux/amd64'), ('unknown/unknown', '')]:
            with self.subTest(platform=platform), self.assertRaises(images.CheckFailure):
                images.compare(desc(A, images.MANIFEST), remote, platform, requested)

    def test_project_summaries_preserve_partial_failures_and_pinning(self):
        def row(status, image='nginx:1.28-alpine'):
            return asdict(images.ImageCheck(status=status, image=image, platform='linux/amd64'))
        cases = [([row('current')], '✓ aktuell'),
                 ([row('update_available'), row('current')], '↑ 1 Image-Update'),
                 ([row('update_available'), row('update_available')], '↑ 1 Image-Update'),
                 ([row('update_available'), row('update_available', 'httpd:2.4-alpine')], '↑ 2 Image-Updates'),
                 ([row('update_available'), row('uncheckable')], '↑ 1 Image-Update · 1 nicht prüfbar'),
                 ([row('current'), row('uncheckable')], '⚠ Prüfung unvollständig'),
                 ([row('uncheckable')], '⚠ Prüfung unvollständig'),
                 ([row('local_build')], 'Lokaler Build'),
                 ([row('digest_pinned')], 'Digest-fixiert'),
                 ([row('current'), row('digest_pinned')], '✓ prüfbare Images aktuell · 1 Digest-fixiert')]
        for rows, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(images.summary(rows), expected)


class ProjectTests(unittest.IsolatedAsyncioTestCase):
    async def test_environment_label_accepts_only_known_empty_contexts(self):
        key = images.LABEL + 'project.environment_file'
        for value in (None, '', '/dev/null'):
            with self.subTest(accepted=value):
                self.container['Config']['Labels'].pop(key, None)
                if value is not None:
                    self.container['Config']['Labels'][key] = value
                with mock.patch.object(images, 'capture', side_effect=self.capture):
                    await images.Checker(object()).resolve_project(PROJECT)
        for value in ('other.env', '.env', '/srv/test/.env', '/tmp/empty',
                      '/dev/null,.env', '/dev/null,/dev/null', '/dev/null .env',
                      '/dev/null\n.env', ' /dev/null', '/dev/null ', '/dev/./null', 'false'):
            with self.subTest(rejected=value):
                self.container['Config']['Labels'][key] = value
                with mock.patch.object(images, 'capture', side_effect=self.capture):
                    with self.assertRaises(images.CheckFailure) as caught:
                        await images.Checker(object()).resolve_project(PROJECT)
                self.assertEqual(caught.exception.kind, 'context')

    def setUp(self):
        self.commands = []
        self.source = 'services:\n  web:\n    image: nginx:1.28-alpine\n'
        self.config = {'services': {'web': {'image': 'nginx:1.28-alpine'}}}
        self.container = dict(Id=CID, Name='/test-web-1', Image=A, State={'Running': True},
                              Config={'Image': 'nginx:1.28-alpine', 'Labels': {
                                  images.LABEL + 'project': 'test', images.LABEL + 'service': 'web',
                                  images.LABEL + 'project.config_files': PATH,
                                  images.LABEL + 'project.working_dir': '/srv/test'}})
        self.containers = [self.container]
        self.local = {A: dict(Id=A, Descriptor=desc(), Os='linux', Architecture='amd64', RepoDigests=['nginx@' + A])}
        self.remote = desc()
        self.errors = {}

    async def capture(self, conn, command, timeout):
        args = shlex.split(command)
        self.commands.append(args)
        self.assertEqual(timeout, images.TIMEOUT)
        if args[:2] == ['cat', '--']:
            return 0, self.source, ''
        if args[:3] == ['docker', 'container', 'ls']:
            return 0, '\n'.join(c['Id'] for c in self.containers), ''
        if args[:3] == ['docker', 'container', 'inspect']:
            value = self.containers
        elif args[:3] == ['docker', 'image', 'inspect']:
            if self.errors.get('image'):
                return self.errors['image']
            value = [self.local[args[3]]]
        elif args[0] == 'env' and 'config' in args:
            value = self.config
        elif args[:3] == ['docker', 'buildx', 'version']:
            failure = self.errors.get('buildx')
            if failure:
                return failure
            return 0, 'github.com/docker/buildx v0.37.1', ''
        elif args[:4] == ['docker', 'buildx', 'imagetools', 'inspect']:
            failure = self.errors.get('registry')
            if isinstance(failure, BaseException):
                raise failure
            if failure:
                return failure
            value = self.remote
        else:
            self.fail('Unexpected command: ' + command)
        return 0, json.dumps(value), ''

    async def check(self, project=None):
        with mock.patch.object(images, 'capture', side_effect=self.capture):
            result = await images.check_projects(object(), [project or PROJECT])
        return result[(project or PROJECT)['name']]

    async def test_complete_modern_running_image_flow(self):
        result = await self.check()
        self.assertEqual(result['summary'], '✓ aktuell')
        row, = result['images']
        self.assertEqual((row['service'], row['container_id'], row['container_name']), ('web', CID, 'test-web-1'))
        self.assertEqual(row['local_descriptor'], desc())
        self.assertEqual(row['remote_descriptor'], desc())
        self.assertEqual(row['repo_digests'], ['nginx@' + A])
        self.assertEqual(row['platform'], 'linux/amd64')
        self.assertEqual(row['comparison_level'], 'index')
        # The exact RUNNING image ID is inspected, never the mutable local tag.
        self.assertIn(['docker', 'image', 'inspect', A], self.commands)
        self.assertFalse(any(c[:3] == ['docker', 'image', 'inspect'] and c[3] != A for c in self.commands))
        remote, = [c for c in self.commands if c[:4] == ['docker', 'buildx', 'imagetools', 'inspect']]
        self.assertEqual(remote[4], 'docker.io/library/nginx:1.28-alpine')

    async def test_changed_index(self):
        self.remote = desc(B)
        result = await self.check()
        self.assertEqual(result['summary'], '↑ 1 Image-Update')

    def missing_image_with_descriptor(self):
        self.errors['image'] = (1, '[]\n', 'Error response from daemon: No such image: ' + A)
        self.container['ImageManifestDescriptor'] = desc(A, images.MANIFEST,
                                                        platform={'os': 'linux', 'architecture': 'amd64'})
        self.container['Platform'] = 'linux'
        self.remote = desc(B, manifests=[desc(A, images.MANIFEST,
                                              platform={'os': 'linux', 'architecture': 'amd64'})])

    async def test_missing_image_fallback_current_and_updated(self):
        self.missing_image_with_descriptor()
        for digest, expected in [(A, 'current'), (C, 'update_available')]:
            with self.subTest(digest=digest):
                self.remote['manifests'][0]['digest'] = digest
                self.commands.clear()
                result = await self.check()
                row = result['images'][0]
                self.assertEqual(row['status'], expected)
                self.assertEqual(row['identity_source'], 'container_manifest_descriptor')
                self.assertEqual(row['platform'], 'linux/amd64')
                self.assertEqual(row['comparison_level'], 'platform_manifest')
                self.assertEqual(row['local_descriptor'], desc(A, images.MANIFEST))
                self.assertEqual(row['remote_descriptor'], desc(digest, images.MANIFEST))
                self.assertEqual(result['summary'], '✓ aktuell' if expected == 'current' else '↑ 1 Image-Update')
                self.assertIn(['docker', 'image', 'inspect', A], self.commands)
                self.assertIn(['docker', 'buildx', 'imagetools', 'inspect',
                               'docker.io/library/nginx:1.28-alpine', '--format', '{{json .Manifest}}'], self.commands)
                self.assertFalse({'pull', 'up', 'down', 'restart', 'build', 'push', 'prune'}.intersection(
                    arg for cmd in self.commands for arg in cmd))

    async def test_fallback_invalid_descriptors_keep_specific_inspect_diagnostic(self):
        self.missing_image_with_descriptor()
        good = deepcopy(self.container['ImageManifestDescriptor'])
        missing_digest = {k: v for k, v in good.items() if k != 'digest'}
        for invalid in [None, {}, missing_digest, dict(good, digest='sha256:bad'),
                        dict(good, digest=A.upper()), dict(good, mediaType=images.INDEX),
                        dict(good, mediaType=images.LIST), dict(good, mediaType='unknown'),
                        {k: v for k, v in good.items() if k != 'platform'},
                        dict(good, platform={'os': 'unknown', 'architecture': 'unknown'}),
                        dict(good, platform={'os': 'linux'}),
                        dict(good, platform={'os': 'linux', 'architecture': None}),
                        dict(good, platform={'os': 'linux', 'architecture': 'amd64', 'variant': ''})]:
            with self.subTest(descriptor=invalid):
                self.container['ImageManifestDescriptor'] = invalid
                self.commands.clear()
                row = (await self.check())['images'][0]
                self.assertEqual(row['status'], 'uncheckable')
                self.assertEqual(row['reason'], 'local_image_unavailable')
                self.assertIn('Lokales Image über Container-Image-ID nicht inspizierbar', row['note'])
                self.assertEqual(row['exit_code'], 1)
                self.assertEqual(row['command'], 'docker image inspect ' + A)
                self.assertFalse(any('buildx' in cmd for cmd in self.commands))

    async def test_fallback_platform_and_variant_consistency(self):
        self.missing_image_with_descriptor()
        self.config['services']['web']['platform'] = 'linux/arm64'
        self.assertEqual((await self.check())['images'][0]['reason'], 'local_image_unavailable')
        self.config['services']['web']['platform'] = 'linux/amd64'
        self.container['Platform'] = 'windows'
        self.assertEqual((await self.check())['images'][0]['reason'], 'local_image_unavailable')
        self.container['Platform'] = 'linux'
        p = {'os': 'linux', 'architecture': 'arm64', 'variant': 'v8'}
        self.container['ImageManifestDescriptor']['platform'] = p
        self.remote['manifests'][0]['platform'] = p
        self.config['services']['web']['platform'] = 'linux/arm64/v8'
        row = (await self.check())['images'][0]
        self.assertEqual((row['status'], row['platform']), ('current', 'linux/arm64/v8'))
        self.config['services']['web']['platform'] = 'linux/arm64/v9'
        self.assertEqual((await self.check())['images'][0]['reason'], 'local_image_unavailable')

    async def test_normal_inspect_takes_precedence_and_other_errors_do_not_fallback(self):
        self.container['ImageManifestDescriptor'] = desc(B, images.MANIFEST,
                                                        platform={'os': 'linux', 'architecture': 'amd64'})
        row = (await self.check())['images'][0]
        self.assertEqual((row['status'], row['identity_source'], row['comparison_level']),
                         ('current', 'image_inspect', 'index'))
        for error in [(124, '', 'timeout'), (1, '', 'permission denied'), (0, 'not JSON', '')]:
            with self.subTest(error=error):
                self.errors['image'] = error
                self.commands.clear()
                row = (await self.check())['images'][0]
                self.assertEqual(row['status'], 'uncheckable')
                self.assertNotEqual(row['identity_source'], 'container_manifest_descriptor')
                self.assertFalse(any('buildx' in cmd for cmd in self.commands))

    async def test_fallback_does_not_populate_image_cache_for_other_container(self):
        self.missing_image_with_descriptor()
        second = deepcopy(self.container)
        second.update(Id='e' * 64, Name='/test-web-2')
        second.pop('ImageManifestDescriptor')
        self.containers.append(second)
        result = await self.check()
        self.assertEqual([r['status'] for r in result['images']], ['current', 'uncheckable'])
        self.assertEqual(result['summary'], '⚠ Prüfung unvollständig')

    async def test_legacy_store_is_uncheckable_even_if_id_matches_registry(self):
        self.local[A].pop('Descriptor')
        result = await self.check()
        self.assertEqual(result['images'][0]['reason'], 'identity')
        self.assertEqual(result['summary'], '⚠ Prüfung unvollständig')
        self.assertFalse(any('imagetools' in c for c in self.commands))

    async def test_buildx_missing(self):
        self.errors['buildx'] = (1, '', 'unknown command buildx')
        result = await self.check()
        self.assertEqual(result['images'][0]['reason'], 'buildx_missing')

    async def test_registry_errors_are_structured_and_do_not_leak_secrets(self):
        for failure, reason in [(RemoteTimeoutError('secret'), 'timeout'),
                                ((1, '', '401 Unauthorized secret-token'), 'auth'),
                                ((1, '', '429 Too Many Requests secret-token'), 'rate_limit'),
                                ((1, '', 'manifest unknown secret-token'), 'not_found'),
                                ((1, '', 'no such host secret-token'), 'network'),
                                ((1, '', 'unrecognized secret-token'), 'command_error')]:
            with self.subTest(reason=reason):
                self.errors['registry'] = failure
                result = await self.check()
                self.assertEqual(result['images'][0]['reason'], reason)
                self.assertNotIn('secret', json.dumps(result))
                self.assertEqual(result['summary'], '⚠ Prüfung unvollständig')

    async def test_digest_pinned_and_local_build_do_not_query_registry(self):
        for pinned in (True, False):
            self.commands.clear()
            if pinned:
                self.config['services']['web'] = {'image': 'nginx@' + A}
                self.container['Config']['Image'] = 'nginx@' + A
            else:
                self.config['services']['web'] = {'build': {'context': '/srv/test'}}
            result = await self.check()
            self.assertEqual(result['images'][0]['status'], 'digest_pinned' if pinned else 'local_build')
            self.assertFalse(any('buildx' in c for c in self.commands))

    async def test_build_plus_image_is_explicitly_unsupported(self):
        self.config['services']['web']['build'] = {'context': '/srv/test'}
        result = await self.check()
        self.assertEqual(result['images'][0]['reason'], 'build_image')

    async def test_context_rejected_before_compose_or_registry(self):
        for source in ['services: {web: {image: "${IMAGE}"}}', 'include: oci://example/config',
                       'services: {web: {extends: {file: other.yml}}}', 'profiles: [test]',
                       'env_file: secret.env', 'services: {web: {image: "nginx\\u003a1"}}']:
            with self.subTest(source=source):
                self.source = source
                self.commands.clear()
                result = await self.check()
                self.assertEqual(result['images'][0]['reason'], 'context')
                self.assertEqual(self.commands, [['cat', '--', PATH]])
        for project in [dict(PROJECT, config_files=[PATH, '/override.yaml']),
                        dict(PROJECT, config_files_raw=PATH + ',/other.yaml'),
                        dict(PROJECT, config_files=['relative.yaml'])]:
            self.commands.clear()
            result = await self.check(project)
            self.assertEqual(result['images'][0]['reason'], 'context')
            self.assertEqual(self.commands, [])

    async def test_context_labels_and_image_drift(self):
        self.container['Config']['Labels'][images.LABEL + 'project.working_dir'] = '/other'
        self.assertEqual((await self.check())['images'][0]['reason'], 'context')
        self.container['Config']['Labels'][images.LABEL + 'project.working_dir'] = '/srv/test'
        self.container['Config']['Image'] = 'nginx:1.29-alpine'
        self.assertEqual((await self.check())['images'][0]['reason'], 'drift')

    async def test_multiple_services_share_registry_query_and_replicas_count_once(self):
        second = deepcopy(self.container)
        second.update(Id='e' * 64, Name='/test-other-1')
        second['Config']['Labels'][images.LABEL + 'service'] = 'other'
        self.containers.append(second)
        self.config['services']['other'] = deepcopy(self.config['services']['web'])
        self.remote = desc(B)
        result = await self.check()
        self.assertEqual(len(result['images']), 2)
        self.assertEqual(result['summary'], '↑ 1 Image-Update')
        self.assertEqual(sum('imagetools' in c for c in self.commands), 1)
        self.assertEqual(sum(c[:3] == ['docker', 'buildx', 'version'] for c in self.commands), 1)
        self.config['services']['inactive'] = {'image': 'httpd:2.4-alpine'}
        result = await self.check()
        self.assertEqual(result['summary'], '↑ 1 Image-Update · 1 nicht prüfbar')

    async def test_new_run_does_not_reuse_registry_cache(self):
        self.assertEqual((await self.check())['summary'], '✓ aktuell')
        self.remote = desc(B)
        self.assertEqual((await self.check())['summary'], '↑ 1 Image-Update')
        self.assertEqual(sum('imagetools' in c for c in self.commands), 2)

    async def test_multiple_images_and_mixed_current_update_unknown(self):
        second = deepcopy(self.container)
        second.update(Id='e' * 64, Name='/test-app-1', Image=B)
        second['Config']['Image'] = 'httpd:2.4-alpine'
        second['Config']['Labels'][images.LABEL + 'service'] = 'app'
        self.containers.append(second)
        self.config['services']['app'] = {'image': 'httpd:2.4-alpine'}
        self.local[B] = dict(self.local[A], Id=B, Descriptor=desc(B))
        result = await self.check()
        self.assertEqual([r['status'] for r in result['images']], ['current', 'update_available'])
        self.assertEqual(result['summary'], '↑ 1 Image-Update')
        self.assertEqual(sum('imagetools' in c for c in self.commands), 2)

    async def test_invalid_registry_output_is_unknown(self):
        self.remote = {'digest': A}
        self.assertEqual((await self.check())['images'][0]['reason'], 'identity')
        self.remote = None
        self.assertEqual((await self.check())['images'][0]['status'], 'uncheckable')

    async def test_no_running_containers_is_not_current(self):
        self.containers = []
        result = await self.check()
        self.assertEqual(result['summary'], '⚠ Prüfung unvollständig')
        self.assertEqual(result['images'][0]['reason'], 'not_running')

    async def test_only_explicit_read_only_commands_and_quoted_arguments(self):
        await self.check()
        forbidden = {'pull', 'up', 'down', 'restart', 'stop', 'start', 'rm', 'rmi', 'prune', 'login', 'build'}
        for command in self.commands:
            self.assertFalse(forbidden.intersection(command), command)
            self.assertTrue(command[:2] == ['cat', '--'] or command[:3] in
                            [['docker', 'container', 'ls'], ['docker', 'container', 'inspect'],
                             ['docker', 'image', 'inspect'], ['docker', 'buildx', 'version']]
                            or command[:4] == ['docker', 'buildx', 'imagetools', 'inspect']
                            or (command[0] == 'env' and command[-5:] ==
                                ['config', '--format', 'json', '--no-interpolate', '--no-env-resolution']))
        seen = []
        async def capture(conn, command, timeout):
            seen.append(shlex.split(command))
            return 0, '', ''
        with mock.patch.object(images, 'capture', side_effect=capture):
            await images.Checker(object()).run(['cat', '--', '/srv/$(id); x/compose.yaml'], parse=False)
        self.assertEqual(seen, [['cat', '--', '/srv/$(id); x/compose.yaml']])

    async def test_cancellation_propagates(self):
        with mock.patch.object(images, 'capture', side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await images.check_projects(object(), [PROJECT])

    async def test_total_deadline_returns_unknown(self):
        async def hang(*args):
            await asyncio.Event().wait()
        with mock.patch.object(images, 'capture', side_effect=hang), mock.patch.object(images, 'TOTAL_TIMEOUT', .01):
            result = await images.check_projects(object(), [PROJECT])
        self.assertEqual(result['test']['images'][0]['reason'], 'timeout')

    async def test_registry_error_does_not_change_linux_package_result(self):
        self.errors['registry'] = (1, '', '429 Too Many Requests')
        @asynccontextmanager
        async def connect(host):
            yield object()
        discovery = docker_compose.DockerComposeDiscovery(status='ok', docker_available=True, compose_available=True,
                        projects=[docker_compose.ComposeProject(name='test', config_files=[PATH], config_files_raw=PATH)])
        with mock.patch.object(ssh_client, 'connect_host', connect), \
                mock.patch.object(docker_compose, 'discover', return_value=discovery), \
                mock.patch.object(images, 'capture', side_effect=self.capture), \
                mock.patch.object(ssh_client, '_detect_distro', return_value='debian'), \
                mock.patch.object(ssh_client, '_check_debian', return_value=(0, 'package note')):
            result = await ssh_client.check_updates_for_host(dict(id=1, name='test', primary_ip='host', user='root'))
        self.assertEqual((result['status'], result['updates'], result['note']), ('ok', 0, 'package note'))
        image_result = result['docker_compose']['projects'][0]['image_updates']
        self.assertEqual(image_result['images'][0]['reason'], 'rate_limit')

    async def test_unexpected_image_error_does_not_change_package_result(self):
        @asynccontextmanager
        async def connect(host):
            yield object()
        discovery = docker_compose.DockerComposeDiscovery(status='ok',
                        projects=[docker_compose.ComposeProject(name='test', config_files=[PATH])])
        with mock.patch.object(ssh_client, 'connect_host', connect), \
                mock.patch.object(docker_compose, 'discover', return_value=discovery), \
                mock.patch.object(images, 'check_projects', side_effect=RuntimeError('secret')), \
                mock.patch.object(ssh_client, '_detect_distro', return_value='debian'), \
                mock.patch.object(ssh_client, '_check_debian', return_value=(0, '')):
            result = await ssh_client.check_updates_for_host(dict(id=1, primary_ip='host', user='root'))
        self.assertEqual(result['status'], 'ok')
        self.assertNotIn('secret', json.dumps(result))
        self.assertEqual(result['docker_compose']['projects'][0]['image_updates']['summary'], '⚠ Prüfung unvollständig')
