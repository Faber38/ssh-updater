import asyncio
from contextlib import asynccontextmanager
import importlib
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sshupdater.core import docker_compose as dc
from sshupdater.core.remote_process import RemoteTimeoutError, RemoteWaitError


DOCKER = (0, "Docker version 27.5.1, build example\n", "")
COMPOSE = (0, "Docker Compose version v2.32.4\n", "")


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def probe(self, responses):
        conn = object()
        with mock.patch.object(dc, "capture", new=mock.AsyncMock(side_effect=responses)) as run:
            result = await dc.discover(conn)
        self.assertEqual(run.await_args_list, [
            mock.call(conn, command, dc.PROBE_TIMEOUT)
            for command in ("docker --version", "docker compose version",
                            "docker compose ls --format json")[:len(responses)]
        ])
        return result

    async def test_docker_missing(self):
        result = await self.probe([(127, "", "bash: docker: command not found")])
        self.assertEqual(result.status, "docker_missing")
        self.assertIs(result.docker_available, False)
        self.assertIsNone(result.compose_available)

    async def test_compose_missing(self):
        result = await self.probe([DOCKER, (1, "", "docker: 'compose' is not a docker command.")])
        self.assertEqual(result.status, "compose_missing")
        self.assertTrue(result.docker_available)
        self.assertIs(result.compose_available, False)

    async def test_available_without_projects(self):
        for output in ("[]", "null", " \n[]\n"):
            with self.subTest(output=output):
                result = await self.probe([DOCKER, COMPOSE, (0, output, "")])
                self.assertEqual(result.status, "ok")
                self.assertTrue(result.docker_available)
                self.assertTrue(result.compose_available)
                self.assertEqual(result.docker_version, DOCKER[1].strip())
                self.assertEqual(result.compose_version, COMPOSE[1].strip())
                self.assertEqual(result.projects, [])

    async def test_one_project(self):
        row = {"Name": "web", "Status": "running(2), exited(1)",
               "ConfigFiles": "/srv/web/compose.yml,/srv/web/override.yml"}
        result = await self.probe([DOCKER, COMPOSE, (0, json.dumps([row]), "")])
        project, = result.projects
        self.assertEqual(project.name, "web")
        self.assertEqual(project.status, row["Status"])
        self.assertEqual(project.container_count, 3)
        self.assertEqual(project.container_states, {"running": 2, "exited": 1})
        self.assertEqual(project.config_files, ["/srv/web/compose.yml", "/srv/web/override.yml"])
        self.assertEqual(project.config_files_raw, row["ConfigFiles"])

    async def test_multiple_projects_and_optional_fields(self):
        rows = [{"Name": "a", "Status": "running(1)", "ConfigFiles": ["/a.yml"]},
                {"Name": "b", "Extra": "ignored"},
                {"Name": "c", "Status": "future status(3)"}]
        result = await self.probe([DOCKER, COMPOSE, (0, json.dumps(rows), "")])
        self.assertEqual([p.name for p in result.projects], ["a", "b", "c"])
        self.assertEqual(result.projects[0].config_files, ["/a.yml"])
        self.assertIsNone(result.projects[1].status)
        self.assertEqual(result.projects[1].config_files, [])
        self.assertIsNone(result.projects[2].container_count)
        self.assertEqual(result.projects[2].status, "future status(3)")

    async def test_single_object_and_unusual_version_strings(self):
        result = await self.probe([(0, "vendor Docker", ""), (0, "vendor Compose", ""),
                                   (0, '{"Name":"legacy"}', "warning")])
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.projects[0].name, "legacy")
        self.assertEqual(result.note, "")

    async def test_invalid_json_and_shapes(self):
        for output in ("", "not json", "[", "42", '"text"', '{}', '[null]',
                       '[{"Name":"ok"},{}]', '{"Name":1}',
                       '{"Name":"x","Status":3}',
                       '{"Name":"x","ConfigFiles":[1]}'):
            with self.subTest(output=output):
                result = await self.probe([DOCKER, COMPOSE, (0, output, "")])
                self.assertEqual(result.status, "invalid_output")
                self.assertEqual(result.projects, [])
                self.assertTrue(result.compose_available)

    async def test_permissions_and_daemon_failures(self):
        for detail, expected in (
            ("permission denied while trying to connect to the Docker daemon socket", "permission_denied"),
            ("Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?", "daemon_unreachable"),
            ("error during connect: connection refused", "daemon_unreachable"),
            ("unknown flag: --format", "command_error"),
        ):
            with self.subTest(detail=detail):
                result = await self.probe([DOCKER, COMPOSE, (1, "", detail)])
                self.assertEqual(result.status, expected)
                self.assertTrue(result.compose_available)
                self.assertEqual(result.command, dc.COMMANDS[2])
                self.assertEqual(result.exit_code, 1)

    async def test_ssh_errors_and_timeouts_at_each_step(self):
        for step in range(3):
            for error, status in ((OSError("channel closed"), "command_error"),
                                  (RemoteWaitError("output limit"), "command_error"),
                                  (RemoteTimeoutError("deadline"), "timeout"),
                                  (RuntimeError("unexpected"), "command_error")):
                with self.subTest(step=step, error=error):
                    result = await self.probe([DOCKER, COMPOSE][:step] + [error])
                    self.assertEqual(result.status, status)
                    self.assertEqual(result.command, dc.COMMANDS[step])

    async def test_only_fixed_diagnostics_cross_discovery_boundary(self):
        secret = '<b>password=SIMULATED_SECRET token=SIMULATED_TOKEN</b>'
        cases = [
            ([(127, '', 'docker: not found ' + secret)], 'docker_missing'),
            ([DOCKER, (1, '', 'unknown command ' + secret)], 'compose_missing'),
            ([DOCKER, COMPOSE, (1, '', 'permission denied ' + secret)], 'permission_denied'),
            ([DOCKER, COMPOSE, (17, secret, secret)], 'command_error'),
            ([DOCKER, COMPOSE, (0, secret, secret)], 'invalid_output'),
            ([RuntimeError(secret)], 'command_error'),
            ([RemoteTimeoutError(secret)], 'timeout'),
            ([DOCKER, COMPOSE, (0, '[]', secret)], 'ok'),
        ]
        for responses, status in cases:
            with self.subTest(status=status, responses=responses):
                result = await self.probe(responses)
                self.assertEqual(result.status, status)
                self.assertEqual(result.note, dc.DISCOVERY_REASONS.get(status, ''))
                self.assertNotIn('SIMULATED_', repr(result))
                self.assertNotIn('<b>', result.note)
                self.assertEqual(result.command, dc.COMMANDS[len(responses) - 1])
                final = responses[-1]
                self.assertEqual(result.exit_code, final[0] if isinstance(final, tuple) else None)

    async def test_cancellation_propagates(self):
        with mock.patch.object(dc, "capture", side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await dc.discover(object())

    async def test_unknown_probe_error_does_not_claim_missing(self):
        result = await self.probe([(1, "", "unexpected failure")])
        self.assertEqual(result.status, "command_error")
        self.assertIsNone(result.docker_available)

    async def test_exit_code_timeout(self):
        result = await self.probe([(124, "", "timeout")])
        self.assertEqual(result.status, "timeout")

    async def test_host_check_continues_after_discovery_failures(self):
        client = importlib.import_module("sshupdater.core.ssh_client")
        host = dict(id=1, name="test", primary_ip="192.0.2.1", user="root")
        connected = False

        @asynccontextmanager
        async def connect(_host):
            nonlocal connected
            connected = True
            yield object()
            connected = False

        async def distro(_conn):
            self.assertTrue(connected)
            self.assertTrue(run.await_count)
            return "debian"

        for response in ((127, "", "docker: not found"), OSError("SSH channel error")):
            with (
                mock.patch.object(client, "connect_host", connect),
                mock.patch.object(dc, "capture", new=mock.AsyncMock(side_effect=[response])) as run,
                mock.patch.object(client, "_detect_distro", side_effect=distro),
                mock.patch.object(client, "_check_debian", return_value=(3, "existing note")),
            ):
                result = await client.check_updates_for_host(host)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["updates"], 3)
            self.assertEqual(result["note"], "existing note")
            self.assertIn(result["docker_compose"]["status"], ("docker_missing", "command_error"))
            self.assertFalse(connected)

    async def test_discovery_survives_package_check_failure(self):
        client = importlib.import_module("sshupdater.core.ssh_client")

        @asynccontextmanager
        async def connect(_host):
            yield object()

        with (
            mock.patch.object(client, "connect_host", connect),
            mock.patch.object(dc, "capture", side_effect=[
                DOCKER, COMPOSE, (0, '[{"Name":"web","Status":"running(1)"}]', "")]),
            mock.patch.object(client, "_detect_distro", return_value="debian"),
            mock.patch.object(client, "_check_debian", side_effect=client.UpdateCheckError("existing error")),
        ):
            result = await client.check_updates_for_host(
                dict(id=1, primary_ip="192.0.2.1", user="root"))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["note"], "existing error")
        self.assertEqual(result["docker_compose"]["status"], "ok")
        self.assertEqual(result["docker_compose"]["projects"][0]["name"], "web")

    async def test_no_discovery_before_successful_connection(self):
        client = importlib.import_module("sshupdater.core.ssh_client")

        @asynccontextmanager
        async def connect(_host):
            raise OSError("connection failed")
            yield  # pragma: no cover

        with (mock.patch.object(client, "connect_host", connect),
              mock.patch.object(dc, "capture") as run):
            result = await client.check_updates_for_host(
                dict(id=1, primary_ip="192.0.2.1", user="root"))
        run.assert_not_called()
        self.assertEqual(result["status"], "error")
        self.assertNotIn("docker_compose", result)


if __name__ == "__main__":
    unittest.main()
