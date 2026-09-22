import importlib
import inspect
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class RebootSchedulingTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls._home = tempfile.TemporaryDirectory()
        cls._home_patch = mock.patch.object(
            Path, "home", classmethod(lambda _cls: Path(cls._home.name))
        )
        cls._home_patch.start()
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        cls.ssh_client = importlib.import_module("sshupdater.core.ssh_client")

    @classmethod
    def tearDownClass(cls):
        for handler in logging.getLogger().handlers[:]:
            handler.close()
            logging.getLogger().removeHandler(handler)
        cls._home_patch.stop()
        cls._home.cleanup()

    def _host(self, user="root"):
        return {
            "id": 7,
            "name": "test-host",
            "primary_ip": "192.0.2.7",
            "port": 22,
            "user": user,
            "auth_method": "key",
        }

    async def _run_reboot(self, responses, user="root"):
        class ConnectionContext:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, exc_type, exc, traceback):
                return False

        run = mock.AsyncMock(side_effect=responses)
        with (
            mock.patch.object(
                self.ssh_client, "connect_host", return_value=ConnectionContext()
            ),
            mock.patch.object(self.ssh_client, "_run", new=run),
        ):
            result = await self.ssh_client.reboot_host(self._host(user))
        return result, run

    async def test_confirmed_exit_zero_is_success(self):
        result, _ = await self._run_reboot([(0, "/usr/bin/systemd-run\n", ""), (0, "", "")])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["note"], "Reboot erfolgreich eingeplant")

    async def test_exit_one_is_error(self):
        result, _ = await self._run_reboot([(0, "/usr/bin/systemd-run\n", ""), (1, "", "systemd-run fehlgeschlagen")])
        self.assertEqual(result["status"], "error")
        self.assertIn("Exitcode 1", result["note"])

    async def test_sudo_error_is_error(self):
        result, _ = await self._run_reboot(
            [(0, "/usr/bin/systemd-run\n", ""), (1, "", "sudo: a password is required")],
            user="admin",
        )
        self.assertEqual(result["status"], "error")
        self.assertIn("sudo", result["note"])

    async def test_scheduling_timeout_has_unknown_remote_state(self):
        result, _ = await self._run_reboot([(0, "/usr/bin/systemd-run\n", ""), (124, "", "Timeout")])
        self.assertEqual(result["status"], "unknown")
        self.assertIn("Zeitüberschreitung", result["note"])

    async def test_asyncssh_error_is_never_success(self):
        with (
            mock.patch.object(
                self.ssh_client,
                "connect_host",
                side_effect=self.ssh_client.asyncssh.DisconnectError(10, "Verbindung fehlgeschlagen"),
            ),
        ):
            result = await self.ssh_client.reboot_host(self._host())
        self.assertEqual(result["status"], "error")
        self.assertIn("nicht bestätigt", result["note"])

    async def test_connection_loss_during_scheduling_is_never_success(self):
        result, _ = await self._run_reboot(
            [
                (0, "/usr/bin/systemd-run\n", ""),
                self.ssh_client.asyncssh.ConnectionLost("Verbindung abgebrochen"),
            ]
        )
        self.assertEqual(result["status"], "unknown")
        self.assertIn("Remote-Zustand unbekannt", result["note"])
        self.assertIn("nicht bestätigt", result["note"])

    async def test_root_command_does_not_use_sudo(self):
        _, run = await self._run_reboot([(0, "/usr/bin/systemd-run\n", ""), (0, "", "")])
        command = run.await_args_list[1].args[1]
        self.assertNotIn("sudo", command)
        self.assertIn("--on-active=2s", command)

    async def test_non_root_command_uses_noninteractive_sudo(self):
        _, run = await self._run_reboot(
            [(0, "/usr/bin/systemd-run\n", ""), (0, "", "")], user="admin"
        )
        command = run.await_args_list[1].args[1]
        self.assertTrue(command.startswith("sudo -n systemd-run"))

    async def test_missing_systemd_run_is_controlled_error_without_fallback(self):
        result, run = await self._run_reboot([(127, "", "not found")])
        self.assertEqual(result["status"], "error")
        self.assertIn("systemd-run ist nicht verfügbar", result["note"])
        self.assertEqual(run.await_count, 1)

    def test_unsafe_trigger_marker_is_absent(self):
        source = inspect.getsource(self.ssh_client.reboot_host)
        self.assertNotIn("echo TRIGGERED", source)
        self.assertNotIn("nohup", source)


if __name__ == "__main__":
    unittest.main()
