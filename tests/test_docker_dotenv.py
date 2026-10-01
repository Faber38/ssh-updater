"""ENV: mocked Compose responses plus real local shell-policy execution.

No Docker daemon, registry, SSH connection or custom dotenv parser is used.
Real Compose 5.5.1 parsing is reserved for the separately authorized host test.
"""
from dataclasses import FrozenInstanceError, replace
import io
import json
import logging
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import test_docker_image_updates as fixtures
from sshupdater.core import docker_context as contexts, docker_image_updates as images
from sshupdater.core import docker_preflight as preflight, docker_pull as pull, docker_apply as apply
from sshupdater.docker_plan import build_plan

SECRET = 'ENV1_PRIVATE_SENTINEL_734938'
DOTENV = (contexts.PROJECT_DOTENV_CONTEXT, '/srv/test/.env')


class InterpolationTests(unittest.TestCase):
    def test_simple_compose_variables(self):
        for source in ('${NAME}', '${IMAGE_TAG}', '${TEST_MESSAGE}'):
            with self.subTest(source=source):
                self.assertTrue(contexts.interpolation_allowed(source))

    def test_escaped_runtime_variables_are_not_compose_inputs(self):
        for source in ('$$NAME', 'echo "$$TEST_MESSAGE"', 'echo "$$HOME"',
                       'command: ["sh", "-c", "echo $$VALUE"]',
                       '$${HOME}', '$$COMPOSE_FILE', '$${DOCKER_HOST}'):
            with self.subTest(source=source):
                self.assertTrue(contexts.interpolation_allowed(source))

    def test_mixed_interpolation_and_multiple_escaped_pairs(self):
        for source in ('echo "${PREFIX}-$$RUNTIME_VALUE"',
                       '${NAME} $$HOME $$RUNTIME $${DOCKER_HOST}', '$$$$',
                       '$$$$HOME', '$$${NAME}'):
            with self.subTest(source=source):
                self.assertTrue(contexts.interpolation_allowed(source))

    def test_remaining_or_complex_dollar_syntax_is_rejected(self):
        for source in ('$NAME', '$', '${NAME:-default}', '${NAME-default}',
                       '${NAME:?error}', '${NAME?error}', '${NAME:+replacement}',
                       '${NAME+replacement}', '${${NAME}}', '${NAME:-${OTHER}}',
                       '${NAME', '${', '${}', '${NA$$ME}'):
            with self.subTest(source=source):
                self.assertFalse(contexts.interpolation_allowed(source))

    def test_allowed_tokens_do_not_hide_disallowed_remainders(self):
        for source in ('${NAME} $$RUNTIME $OTHER', '$$HOME $', '$$$',
                       '${NAME} $$$$$', '$$$NAME', '$$VALUE ${NAME?error}',
                       '$$VALUE ${', '$$VALUE ${HOME}', '$$HOME ${DOCKER_HOST}',
                       '$${HOME} ${COMPOSE_FILE}'):
            with self.subTest(source=source):
                self.assertFalse(contexts.interpolation_allowed(source))

    def test_reserved_compose_inputs_remain_rejected(self):
        for name in (*contexts.INFRASTRUCTURE, 'COMPOSE_FILE', 'COMPOSE_PROFILES',
                     'COMPOSE_ENV_FILES', 'DOCKER_UNRECOGNIZED'):
            with self.subTest(name=name):
                self.assertFalse(contexts.interpolation_allowed('${' + name + '}'))


class DotenvTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.ProjectTests.setUp(self)
        self.source = 'services:\n  web:\n    image: nginx:${IMAGE_TAG}\n    environment:\n      TEST_MESSAGE: ${TEST_MESSAGE}\n'
        self.config['services']['web']['environment'] = {'TEST_MESSAGE': 'A'}
        self.kind = contexts.PROJECT_DOTENV_CONTEXT
        self.config_error = (0, '')
        self.probe_error = False

    async def capture(self, conn, command, timeout):
        args = shlex.split(command)
        if args == contexts.probe_command(fixtures.PATH):
            self.commands.append(args)
            return (1, '', SECRET) if self.probe_error else (0, self.kind, '')
        if args == contexts.config_command('test', fixtures.PATH, DOTENV):
            self.commands.append(args)
            return self.config_error[0], json.dumps(self.config), self.config_error[1]
        return await fixtures.ProjectTests.capture(self, conn, command, timeout)

    check = fixtures.ProjectTests.check

    async def plan(self):
        self.remote = fixtures.desc(fixtures.B)
        checked = await self.check()
        return build_plan([dict(host_id=1, connection_identity=('host', 'root', 22, 'key', None),
                                result_revision=1, projects=[dict(fixtures.PROJECT, image_updates=checked)])])

    async def verify(self, snapshot):
        with mock.patch.object(images, 'capture', side_effect=self.capture), \
                mock.patch.object(images.Checker, 'remote', side_effect=AssertionError('registry in preflight')):
            await preflight.verify_project(object(), snapshot)

    async def test_standard_missing_label_is_readonly_resolved_and_identity_bound(self):
        checked = await self.check()
        self.assertEqual(checked['summary'], '✓ aktuell')
        self.assertEqual(checked['compose_context'], DOTENV)
        self.assertEqual(checked['compose_identity'][2], (('web', 'nginx:1.28-alpine', ''),))
        self.assertEqual(self.commands.count(contexts.config_command('test', fixtures.PATH, DOTENV)), 2)
        self.assertFalse(any('pull' in c or 'up' in c for c in self.commands))

    async def test_real_env_test_command_escape_passes_with_bound_candidate(self):
        self.source = '''services:
  web:
    image: nginx:${IMAGE_TAG}
    environment:
      TEST_MESSAGE: ${TEST_MESSAGE}
    command:
      - sh
      - -c
      - |
        echo "TEST_MESSAGE=$$TEST_MESSAGE"
'''
        self.config['services']['web']['command'] = ['sh', '-c', 'echo "TEST_MESSAGE=$$TEST_MESSAGE"']
        self.assertTrue(contexts.interpolation_allowed(self.source))
        plan = await self.plan()
        self.assertEqual(plan.selection[0].compose_context, DOTENV)
        self.assertEqual(plan.selection[0].images[0][5], 'update_available')
        self.assertEqual(plan.candidates, plan.selection)
        await self.verify(plan.selection[0])
        self.assertNotIn('$$TEST_MESSAGE', repr(plan))

    async def test_escaped_dollars_remain_rejected_in_empty_context(self):
        self.source = 'services:\n  web:\n    image: nginx:1.28-alpine\n    command: echo $$HOME\n'
        for explicit_devnull in (False, True):
            with self.subTest(explicit_devnull=explicit_devnull):
                self.kind = contexts.EMPTY_CONTEXT
                if explicit_devnull:
                    self.container['Config']['Labels'][images.LABEL + 'project.environment_file'] = '/dev/null'
                self.commands.clear()
                checked = await self.check()
                self.assertEqual(checked['images'][0]['reason'], 'context')
                self.assertNotIn('compose_identity', checked)
                self.assertFalse(any(c[0] == 'env' for c in self.commands))
                self.assertNotIn(contexts.config_command('test', fixtures.PATH, DOTENV), self.commands)

    async def test_effective_environment_drift_rejects_old_snapshot(self):
        plan = await self.plan()
        original = plan.selection[0]
        await self.verify(original)
        self.config['services']['web']['environment']['TEST_MESSAGE'] = 'B'
        with self.assertRaises(preflight.PreflightFailure) as caught:
            await self.verify(original)
        self.assertEqual(caught.exception.reason, 'config')
        checked = await self.check()
        self.assertEqual(checked['compose_identity'][0], original.compose_identity[0])
        self.assertNotEqual(checked['compose_identity'][1], original.compose_identity[1])
        # New B is a valid current config; no claim about historic container env.
        self.assertEqual(checked['images'][0]['status'], 'update_available')
        self.assertEqual(self.container['Config']['Image'], 'nginx:1.28-alpine')

    async def test_image_tag_drift_is_specific(self):
        snapshot = (await self.plan()).selection[0]
        self.config['services']['web']['image'] = 'nginx:1.29-alpine'
        with self.assertRaises(preflight.PreflightFailure) as caught:
            await self.verify(snapshot)
        self.assertEqual(caught.exception.reason, 'image')
        self.assertEqual((await self.check())['images'][0]['reason'], 'drift')

    async def test_comment_unused_variable_and_json_order_do_not_change_identity(self):
        snapshot = (await self.plan()).selection[0]
        # Compose emits the same effective model after comment/unused-key edits.
        self.config = json.loads(json.dumps(self.config, sort_keys=True))
        await self.verify(snapshot)
        self.assertEqual((await self.check())['compose_identity'], snapshot.compose_identity)
        self.assertFalse(any(c[:2] == ['cat', '--'] and c[-1].endswith('/.env') for c in self.commands))

    async def test_config_changes_during_check_are_not_published(self):
        base = self.capture
        count = 0
        async def changing(conn, command, timeout):
            nonlocal count
            if shlex.split(command) == contexts.config_command('test', fixtures.PATH, DOTENV):
                count += 1
                if count > 1:
                    self.config['services']['web']['environment']['TEST_MESSAGE'] = 'B'
            return await base(conn, command, timeout)
        with mock.patch.object(images, 'capture', side_effect=changing):
            checked = await images.Checker(object()).project(fixtures.PROJECT)
        self.assertEqual(checked['images'][0]['reason'], 'config')
        self.assertNotIn('compose_identity', checked)

    async def test_missing_unreadable_dotenv_and_unset_variable_fail_safely(self):
        self.kind = contexts.EMPTY_CONTEXT
        self.assertEqual((await self.check())['images'][0]['reason'], 'context')
        self.kind = contexts.PROJECT_DOTENV_CONTEXT
        self.probe_error = True
        self.assertEqual((await self.check())['images'][0]['reason'], 'context')
        self.probe_error = False
        for code in (0, 1):
            self.config_error = (code, 'Variable not set. ' + SECRET)
            checked = await self.check()
            self.assertEqual(checked['images'][0]['status'], 'uncheckable')
            self.assertNotIn(SECRET, repr(checked))
            self.assertNotIn('compose_identity', checked)

    async def test_unresolved_and_infrastructure_service_environment_is_rejected(self):
        for environment in ({'MISSING': None}, {'HOME': '/shell/home'}, {'DOCKER_HOST': 'tcp://host'},
                            {'COMPOSE_PROFILES': 'hidden'}):
            self.config['services']['web']['environment'] = environment
            self.assertEqual((await self.check())['images'][0]['reason'], 'context')

    async def test_preflight_rechecks_effective_config_at_end(self):
        snapshot = (await self.plan()).selection[0]
        base = self.capture
        count = 0
        async def changing(conn, command, timeout):
            nonlocal count
            if shlex.split(command) == contexts.config_command('test', fixtures.PATH, DOTENV):
                count += 1
                if count == 2:
                    self.config['services']['web']['environment']['TEST_MESSAGE'] = 'B'
            return await base(conn, command, timeout)
        with mock.patch.object(images, 'capture', side_effect=changing):
            with self.assertRaises(preflight.PreflightFailure) as caught:
                await preflight.verify_project(object(), snapshot)
        self.assertEqual(caught.exception.reason, 'config')

    async def test_labels_only_allow_exact_project_file(self):
        key = images.LABEL + 'project.environment_file'
        self.container['Config']['Labels'][key] = DOTENV[1]
        self.assertEqual((await self.check())['compose_context'], DOTENV)
        for label in ('.env', '/elsewhere/.env', DOTENV[1]+',other.env', '/dev/null,'+DOTENV[1]):
            self.container['Config']['Labels'][key] = label
            self.assertEqual((await self.check())['images'][0]['reason'], 'context')
        self.container['Config']['Labels'][key] = '/dev/null'
        self.assertEqual((await self.check())['images'][0]['reason'], 'context')

    async def test_indirect_contexts_and_expanded_interpolation_still_rejected(self):
        source = self.source
        for extra in ('env_file: .env', 'include: other.yml', 'extends: other.yml', 'profiles: [x]',
                      'provider: x', 'models: x', 'pre_start: x', 'post_start: x', 'pre_stop: x',
                      'x: &anchor x', 'x: !tag x', 'x: $VAR', 'x: ${VAR:-default}', 'x: ${HOME}',
                      'x: ${DOCKER_HOST}', 'x: ${COMPOSE_FILE}'):
            self.source = source + '\n' + extra
            self.commands.clear()
            self.assertEqual((await self.check())['images'][0]['reason'], 'context', extra)
            self.assertEqual(self.commands, [['cat', '--', fixtures.PATH]])
        self.source = source
        for paths in ([fixtures.PATH, '/override.yaml'], ['relative.yaml']):
            checked = await self.check(dict(fixtures.PROJECT, config_files=paths))
            self.assertEqual(checked['images'][0]['reason'], 'context')

    async def test_dotenv_without_interpolation_is_also_mutation_eligible(self):
        self.source = 'services:\n  web:\n    image: nginx:1.28-alpine\n'
        plan = await self.plan()
        self.assertEqual(plan.selection[0].compose_context, DOTENV)
        self.assertEqual(plan.candidates, plan.selection)

    async def test_bound_candidates_and_forged_mutation_plans_blocked_before_ssh(self):
        plan = await self.plan()
        self.assertEqual(plan.selection[0].images[0][5], 'update_available')
        self.assertEqual(plan.candidates, plan.selection)
        with self.assertRaises(FrozenInstanceError):
            plan.selection[0].compose_context = contexts.EMPTY
        forged = replace(plan, candidates=(replace(plan.candidates[0],
                          compose_context=(contexts.PROJECT_DOTENV_CONTEXT, '/other/.env')),))
        with mock.patch.object(pull, 'connect_host', side_effect=AssertionError('SSH')), \
                mock.patch.object(preflight, 'preflight', side_effect=AssertionError('preflight')):
            result = await pull.PullRun(forged, {}).run()
        self.assertFalse(result['mutation_attempted'])
        state = dict(plan=forged, result=dict(status='pulled', apply_pending=True, mutation_attempted=True))
        self.assertFalse(apply.prepared(state))
        with mock.patch.object(apply, 'connect_host', side_effect=AssertionError('SSH')):
            result = await apply.ApplyRun(state, {}).run()
        self.assertFalse(result['mutation_attempted'])

    async def test_context_change_invalidates_even_identical_config(self):
        self.source = 'services:\n  web:\n    image: nginx:1.28-alpine\n'
        snapshot = (await self.plan()).selection[0]
        self.kind = contexts.EMPTY_CONTEXT
        with self.assertRaises(preflight.PreflightFailure) as caught:
            await self.verify(snapshot)
        self.assertEqual(caught.exception.reason, 'config')

    async def test_explicit_devnull_remains_empty_even_with_dotenv_present(self):
        self.source = 'services:\n  web:\n    image: nginx:1.28-alpine\n'
        self.container['Config']['Labels'][images.LABEL + 'project.environment_file'] = '/dev/null'
        plan = await self.plan()
        self.assertEqual(plan.selection[0].compose_context, contexts.EMPTY)
        self.assertEqual(len(plan.candidates), 1)
        self.assertNotIn(contexts.probe_command(fixtures.PATH), self.commands)

    async def test_workdir_and_mixed_context_labels_fail(self):
        self.container['Config']['Labels'][images.LABEL + 'project.working_dir'] = '/other'
        self.assertEqual((await self.check())['images'][0]['reason'], 'context')
        self.container['Config']['Labels'][images.LABEL + 'project.working_dir'] = '/srv/test'
        from copy import deepcopy
        second = deepcopy(self.container)
        second['Id'] = 'e' * 64
        second['Config']['Labels'][images.LABEL + 'project.environment_file'] = '/dev/null'
        self.container['Config']['Labels'][images.LABEL + 'project.environment_file'] = DOTENV[1]
        self.containers.append(second)
        self.assertEqual((await self.check())['images'][0]['reason'], 'context')

    async def test_secrets_absent_from_results_plan_gui_and_logs(self):
        from PyQt6 import QtWidgets
        from sshupdater.ui_docker_preview import DockerUpdatePreviewDialog, project_state
        self.config['services']['web']['environment']['PASSWORD'] = SECRET
        self.remote = fixtures.desc(fixtures.B)
        log = io.StringIO()
        handler = logging.StreamHandler(log)
        logging.getLogger().addHandler(handler)
        try:
            with mock.patch('builtins.open', side_effect=AssertionError('persistence')):
                checked = await self.check()
                plan = await self.plan()
                await self.verify(plan.selection[0])
            project = dict(fixtures.PROJECT, image_updates=checked)
            app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
            dialog = DockerUpdatePreviewDialog(None, [dict(name='test', projects=[project])])
            dialog.show()
            app.processEvents()
            self.assertEqual(dialog.counts['candidate'], 1)
            self.assertEqual(dialog.counts['updates'], 1)
            self.assertIn('Standard-.env', dialog.details.toPlainText())
            self.assertIn('vor Pull und Apply erneut geprüft', dialog.details.toPlainText())
            self.assertTrue(project_state(project)['candidate'])
            self.assertNotIn(SECRET, dialog.details.toPlainText())
            dialog.close()
            self.assertNotIn(SECRET, repr((checked, plan, self.commands)))
            self.assertNotIn(SECRET, log.getvalue())
        finally:
            logging.getLogger().removeHandler(handler)


class LocalPolicyTests(unittest.TestCase):
    # Independent expectations: changes to the builder must not silently change
    # the security contract asserted by these tests.
    infrastructure_keys = (
        'HOME', 'PATH', 'DOCKER_CONFIG', 'DOCKER_CONTEXT', 'DOCKER_HOST',
        'DOCKER_TLS', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH', 'DOCKER_API_VERSION',
        'DOCKER_CUSTOM_HEADERS', 'DOCKER_DEFAULT_PLATFORM',
        'XDG_RUNTIME_DIR', 'XDG_CONFIG_HOME', 'SSH_AUTH_SOCK',
        'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'ALL_PROXY',
        'http_proxy', 'https_proxy', 'no_proxy', 'all_proxy',
        'SSL_CERT_FILE', 'SSL_CERT_DIR',
    )
    controls = (
        'COMPOSE_FILE=', 'COMPOSE_PROJECT_NAME=', 'COMPOSE_PROFILES=',
        'COMPOSE_ENV_FILES=', 'COMPOSE_DISABLE_ENV_FILE=1', 'COMPOSE_PATH_SEPARATOR=:',
        'COMPOSE_CONVERT_WINDOWS_PATHS=0', 'COMPOSE_IGNORE_ORPHANS=0',
        'COMPOSE_REMOVE_ORPHANS=0', 'COMPOSE_PARALLEL_LIMIT=1',
        'COMPOSE_ANSI=never', 'COMPOSE_STATUS_STDOUT=0', 'COMPOSE_PROGRESS=quiet',
        'COMPOSE_MENU=0', 'COMPOSE_EXPERIMENTAL=0', 'COMPOSE_BAKE=false',
        'COMPOSE_COMPATIBILITY=0',
    )
    operations = (
        ['config', '--format', 'json', '--no-env-resolution'],
        ['pull', '--policy', 'always', '--quiet', '--', 'web'],
        ['up', '-d', '--no-deps', '--pull', 'never', '--no-build', '--', 'web'],
    )

    def assert_remote_policy(self, command, operation, remote):
        """Check the complete generated grammar before modelling POSIX expansion.

        This is deliberately not a general shell interpreter. Only the exact
        quoted positional-argument construction and fixed env -i form are valid.
        A plain dict models Linux's case-sensitive environment on every OS.
        """
        self.assertEqual(command[:2], ['sh', '-c'])
        self.assertEqual(command[3:], ['ssh-updater-dotenv-' + operation[0]])
        lines = command[2].splitlines()
        expected_args = ['--project-name', 'test', '--project-directory', '/srv/test',
                         '--env-file', '/srv/test/.env', '-f', '/srv/test/compose.yaml', *operation]
        self.assertEqual(lines[0], 'set -- ' + shlex.join(expected_args))
        self.assertEqual(lines[1], 'set -- docker compose "$@"')
        assignments = [f'set -- "{key}=${{{key}-}}" "$@"' for key in self.infrastructure_keys]
        self.assertEqual(lines[2:-1], assignments)
        self.assertEqual(lines[-1], 'exec env -i ' + shlex.join(self.controls) + ' "$@"')
        # Each validated assignment prepends one argument; env -i starts empty.
        effective = {}
        for key in reversed(self.infrastructure_keys):
            effective[key] = remote.get(key, '')
        effective.update(value.split('=', 1) for value in self.controls)
        self.assertNotIn(SECRET, repr(command) + repr(effective))
        return effective

    def assert_all_remote_operations(self):
        infrastructure = {key: 'synthetic-' + key for key in self.infrastructure_keys}
        infrastructure['PATH'] = '/synthetic/bin:/usr/bin:/bin'
        controls = dict(value.split('=', 1) for value in self.controls)
        for operation in self.operations:
            for missing in (False, True):
                with self.subTest(operation=operation[0], missing=missing):
                    expected = dict(infrastructure)
                    if missing:
                        for key in ('HOME', 'DOCKER_HOST', 'XDG_CONFIG_HOME', 'http_proxy'):
                            expected.pop(key)
                    remote = dict(expected, IMAGE_TAG=SECRET, TEST_MESSAGE=SECRET,
                                  UNRELATED_SECRET=SECRET, COMPOSE_FAKE_FUTURE_CONTROL=SECRET)
                    remote.update({key: SECRET for key in controls})
                    command = contexts.operation_command('test', fixtures.PATH, DOTENV, operation)
                    effective = self.assert_remote_policy(command, operation, remote)
                    self.assertEqual(effective, dict(
                        {key: expected.get(key, '') for key in self.infrastructure_keys}, **controls))
                    for key in ('IMAGE_TAG', 'TEST_MESSAGE', 'UNRELATED_SECRET', 'COMPOSE_FAKE_FUTURE_CONTROL'):
                        self.assertNotIn(key, effective)
                    if not missing:
                        for upper in ('HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'ALL_PROXY'):
                            self.assertEqual(effective[upper], 'synthetic-' + upper)
                            self.assertEqual(effective[upper.lower()], 'synthetic-' + upper.lower())
                            self.assertNotEqual(effective[upper], effective[upper.lower()])

    def test_structural_policy_rejects_broken_builders(self):
        # Negative controls exercise the same oracle used on Windows and POSIX.
        for operation in self.operations:
            good = contexts.operation_command('test', fixtures.PATH, DOTENV, operation)
            wrong_operation = dict(config='config --environment', pull='pull --quiet -- other',
                                   up='up -d --pull always --build -- other')[operation[0]]
            original_tail = shlex.join(operation)
            mutations = (
                good[2].replace('--env-file /srv/test/.env', '--env-file /foreign/.env'),
                good[2].replace('exec env -i ', 'exec env '),
                good[2].replace('exec env -i ', 'set -- "IMAGE_TAG=${IMAGE_TAG-}" "$@"\nexec env -i '),
                good[2].replace('COMPOSE_PROFILES=', 'COMPOSE_PROFILES=hidden'),
                good[2].replace('COMPOSE_ENV_FILES= ', ''),
                good[2].replace(original_tail, wrong_operation, 1),
            )
            for index, script in enumerate(mutations):
                with self.subTest(operation=operation[0], mutation=index):
                    self.assertNotEqual(script, good[2])
                    broken = [*good[:2], script, *good[3:]]
                    with self.assertRaises(AssertionError):
                        self.assert_remote_policy(broken, operation, {'IMAGE_TAG': SECRET})

    def test_real_shell_removes_application_values_preserves_infrastructure_and_pins_controls(self):
        self.assert_all_remote_operations()
        # Windows has completed the full structural security check above.
        # Native Windows Python is not an oracle for a Linux exec environment.
        if os.name != 'posix':
            return
        with tempfile.TemporaryDirectory(prefix='dotenv-policy-') as tmp:
            docker = Path(tmp) / 'docker'
            infrastructure = {key: 'synthetic-'+key for key in self.infrastructure_keys}
            infrastructure['PATH'] = tmp + ':/usr/bin:/bin'
            # A local executable verifies the actual exec environment and argv.
            # It does not parse .env and never contacts Docker or prints an env dump.
            script = ('#!' + sys.executable + '\nimport os, sys, json\n'
                      'assert "IMAGE_TAG" not in os.environ\n'
                      'assert "TEST_MESSAGE" not in os.environ\n'
                      'assert "UNRELATED_SECRET" not in os.environ\n'
                      'assert "COMPOSE_FAKE_FUTURE_CONTROL" not in os.environ\n'
                      'expected = ' + repr(infrastructure) + '\n'
                      'assert all(os.environ[k] == v for k,v in expected.items())\n'
                      'controls = ' + repr(dict(v.split('=', 1) for v in self.controls)) + '\n'
                      'assert all(os.environ[k] == v for k,v in controls.items())\n'
                      'assert sys.argv[1] == "compose"\n'
                      'assert sys.argv[sys.argv.index("--env-file")+1] == "/srv/test/.env"\n'
                      'assert sys.argv.count("--env-file") == 1\n'
                      'assert "--no-interpolate" not in sys.argv\n'
                      'operation = sys.argv[sys.argv.index("-f")+2:]\n'
                      'assert operation in [["config", "--format", "json", "--no-env-resolution"], '
                      '["pull", "--policy", "always", "--quiet", "--", "web"], '
                      '["up", "-d", "--no-deps", "--pull", "never", "--no-build", "--", "web"]]\n'
                      'print(json.dumps({"services": {"web": {"image": "nginx:1.28-alpine"}}}))\n')
            docker.write_text(script, encoding="utf-8")
            docker.chmod(0o700)
            environment = dict(infrastructure, IMAGE_TAG='B', TEST_MESSAGE=SECRET, UNRELATED_SECRET=SECRET,
                               COMPOSE_FILE='/override.yaml', COMPOSE_PROFILES='hidden',
                               COMPOSE_ENV_FILES='/external.env', COMPOSE_FAKE_FUTURE_CONTROL='1')
            operations = [['config', '--format', 'json', '--no-env-resolution'],
                          ['pull', '--policy', 'always', '--quiet', '--', 'web'],
                          ['up', '-d', '--no-deps', '--pull', 'never', '--no-build', '--', 'web']]
            for operation in operations:
                with self.subTest(operation=operation[0]):
                    command = contexts.operation_command('test', fixtures.PATH, DOTENV, operation)
                    result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout)['services']['web']['image'], 'nginx:1.28-alpine')
                    self.assertNotIn(SECRET, repr(command) + result.stdout + result.stderr)

    def test_probe_requires_local_regular_canonical_file(self):
        # Remote paths are POSIX strings even when the application runs on Windows.
        expected_script = '''
if [ ! -e "$1/.env" ] && [ ! -L "$1/.env" ]; then
    printf EMPTY_CONTEXT
    exit 0
fi
[ -f "$1/.env" ] && [ -r "$1/.env" ] && [ ! -L "$1/.env" ] || exit 1
[ "$(realpath -e -- "$1")" = "$1" ] || exit 1
[ "$(realpath -e -- "$2")" = "$2" ] || exit 1
[ "$(realpath -e -- "$1/.env")" = "$1/.env" ] || exit 1
printf PROJECT_DOTENV_CONTEXT
'''
        for root in ('/srv/example', '/srv/project with spaces'):
            path = root + '/compose.yaml'
            context = (contexts.PROJECT_DOTENV_CONTEXT, root + '/.env')
            self.assertEqual(contexts.probe_command(path),
                             ['sh', '-c', expected_script, 'ssh-updater-context', root, path])
            self.assertTrue(contexts.valid_context((path,), context))
            self.assertFalse(contexts.valid_context((path,),
                             (contexts.PROJECT_DOTENV_CONTEXT, '/external/.env')))
        windows_path = r'C:\synthetic\example\compose.yaml'
        self.assertFalse(contexts.valid_context((windows_path,),
                         (contexts.PROJECT_DOTENV_CONTEXT, '/srv/example/.env')))
        if os.name != 'posix':
            return  # Structural probe contract checked; no Windows filesystem surrogate.
        with tempfile.TemporaryDirectory(prefix='dotenv-path-') as tmp:
            root = Path(tmp)
            compose = root / 'compose.yaml'
            compose.write_text('services: {}')
            def probe(path=compose):
                return subprocess.run(contexts.probe_command(str(path)), capture_output=True, text=True, timeout=10)
            self.assertEqual(probe().stdout, contexts.EMPTY_CONTEXT)
            dotenv = root / '.env'
            dotenv.write_text('IMAGE_TAG=A\n' + SECRET)
            self.assertEqual(probe().stdout, contexts.PROJECT_DOTENV_CONTEXT)
            dotenv.chmod(0)
            if os.geteuid() != 0:
                self.assertNotEqual(probe().returncode, 0)
            dotenv.chmod(0o600)
            dotenv.unlink()
            external = root / 'external.env'
            external.write_text('IMAGE_TAG=B')
            dotenv.symlink_to(external)
            self.assertNotEqual(probe().returncode, 0)
            dotenv.unlink()
            dotenv.mkdir()
            self.assertNotEqual(probe().returncode, 0)

    def test_empty_command_is_unchanged(self):
        self.assertEqual(images.compose_command('test', fixtures.PATH),
            ['env', 'COMPOSE_PROFILES=', 'COMPOSE_ENV_FILES=', 'COMPOSE_DISABLE_ENV_FILE=1',
             'docker', 'compose', '--project-name', 'test', '--project-directory', '/srv/test',
             '--env-file', '/dev/null', '-f', fixtures.PATH])

    def test_config_builder_cannot_select_external_or_multiple_env_files(self):
        for context in (contexts.EMPTY, (contexts.PROJECT_DOTENV_CONTEXT, '/external.env'),
                        (contexts.PROJECT_DOTENV_CONTEXT, '/srv/test/.env,/extra.env')):
            with self.assertRaises(ValueError):
                contexts.config_command('test', fixtures.PATH, context)

    def test_only_restricted_targeted_mutation_commands_are_available(self):
        for operation in (['up', '-d'], ['pull'], ['build'], ['down'],
                          ['config', '--environment'],
                          ['pull', '--policy', 'always', '--quiet', '--'],
                          ['pull', '--policy', 'always', '--quiet', '--', '--all'],
                          ['pull', '--policy', 'always', '--quiet', '--', 'web', 'other'],
                          ['up', '-d', '--no-deps', '--pull', 'always', '--no-build', '--', 'web'],
                          ['up', '-d', '--no-deps', '--pull', 'never', '--no-build', '--', 'web', 'web']):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                contexts.operation_command('test', fixtures.PATH, DOTENV, operation)

    def test_noncanonical_or_unbound_context_never_becomes_eligible(self):
        for path, context in (('/srv/../test/compose.yaml', DOTENV),
                              ('/srv/test//compose.yaml', DOTENV),
                              ('relative.yaml', DOTENV),
                              (fixtures.PATH, ('UNKNOWN', DOTENV[1])),
                              (fixtures.PATH, (contexts.PROJECT_DOTENV_CONTEXT, '/other/.env'))):
            with self.subTest(path=path, context=context):
                self.assertFalse(contexts.valid_context((path,), context))
                with self.assertRaises(ValueError):
                    contexts.operation_command('test', path, context,
                                               ['pull', '--policy', 'always', '--quiet', '--', 'web'])
