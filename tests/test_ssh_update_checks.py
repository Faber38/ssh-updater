import importlib
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class PackageCheckTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_debian_detects_two_updates(self):
        responses = [
            (0, "", ""),
            (0, "Inst paket-a [1] (2 repo)\nInst paket-b [1] (2 repo)\n", ""),
        ]
        with mock.patch.object(
            self.ssh_client, "_run", new=mock.AsyncMock(side_effect=responses)
        ):
            count, note = await self.ssh_client._check_debian(object())
        self.assertEqual(count, 2)
        self.assertEqual(note, "")

    async def test_debian_update_failure_stops_before_simulation(self):
        run = mock.AsyncMock(return_value=(1, "", "Paketquellen nicht erreichbar"))
        with mock.patch.object(self.ssh_client, "_run", new=run):
            with self.assertRaisesRegex(
                self.ssh_client.UpdateCheckError, "apt-get update.*Exitcode 1"
            ):
                await self.ssh_client._check_debian(object())
        run.assert_awaited_once()

    async def test_debian_update_timeout_is_error(self):
        with mock.patch.object(
            self.ssh_client,
            "_run",
            new=mock.AsyncMock(return_value=(124, "", "Timeout")),
        ):
            with self.assertRaisesRegex(
                self.ssh_client.UpdateCheckError, "Zeitüberschreitung"
            ):
                await self.ssh_client._check_debian(object())

    async def test_debian_simulation_failure_is_error(self):
        responses = [(0, "", ""), (2, "", "Paketstatus fehlerhaft")]
        with mock.patch.object(
            self.ssh_client, "_run", new=mock.AsyncMock(side_effect=responses)
        ):
            with self.assertRaisesRegex(
                self.ssh_client.UpdateCheckError,
                "apt-get -s dist-upgrade.*Exitcode 2",
            ):
                await self.ssh_client._check_debian(object())

    async def test_debian_success_without_updates_returns_zero(self):
        responses = [(0, "", ""), (0, "0 upgraded, 0 newly installed", "")]
        with mock.patch.object(
            self.ssh_client, "_run", new=mock.AsyncMock(side_effect=responses)
        ):
            count, _ = await self.ssh_client._check_debian(object())
        self.assertEqual(count, 0)

    async def test_rpm_exit_zero_returns_zero(self):
        with mock.patch.object(
            self.ssh_client,
            "_run",
            new=mock.AsyncMock(return_value=(0, "", "")),
        ):
            count, _ = await self.ssh_client._check_rpm(object())
        self.assertEqual(count, 0)

    async def test_rpm_counts_only_three_package_lines(self):
        output = """Last metadata expiration check: 0:01:00 ago.

Available Upgrades
paket-a.x86_64 2.0 updates
paket-b.noarch 3.0 updates
paket-c.aarch64 4.0 updates
Security: kernel update available
"""
        with mock.patch.object(
            self.ssh_client,
            "_run",
            new=mock.AsyncMock(return_value=(100, output, "")),
        ):
            count, _ = await self.ssh_client._check_rpm(object())
        self.assertEqual(count, 3)

    async def test_rpm_unexpected_exit_is_error(self):
        with mock.patch.object(
            self.ssh_client,
            "_run",
            new=mock.AsyncMock(return_value=(1, "", "DNF-Fehler")),
        ):
            with self.assertRaisesRegex(
                self.ssh_client.UpdateCheckError, "dnf check-update.*Exitcode 1"
            ):
                await self.ssh_client._check_rpm(object())

    async def test_arch_missing_checkupdates_is_error(self):
        with mock.patch.object(
            self.ssh_client,
            "_run",
            new=mock.AsyncMock(return_value=(1, "", "")),
        ):
            with self.assertRaisesRegex(
                self.ssh_client.UpdateCheckError, "pacman-contrib"
            ):
                await self.ssh_client._check_arch(object())

    async def test_arch_counts_three_package_lines(self):
        responses = [
            (0, "/usr/bin/checkupdates\n", ""),
            (0, "paket-a 1 -> 2\npaket-b 2 -> 3\npaket-c 3 -> 4\n", ""),
        ]
        with mock.patch.object(
            self.ssh_client, "_run", new=mock.AsyncMock(side_effect=responses)
        ):
            count, _ = await self.ssh_client._check_arch(object())
        self.assertEqual(count, 3)

    async def test_arch_empty_success_returns_zero(self):
        responses = [(0, "/usr/bin/checkupdates\n", ""), (0, "", "")]
        with mock.patch.object(
            self.ssh_client, "_run", new=mock.AsyncMock(side_effect=responses)
        ):
            count, _ = await self.ssh_client._check_arch(object())
        self.assertEqual(count, 0)

    async def test_arch_unexpected_exit_is_error(self):
        responses = [(0, "/usr/bin/checkupdates\n", ""), (3, "", "Fehler")]
        with mock.patch.object(
            self.ssh_client, "_run", new=mock.AsyncMock(side_effect=responses)
        ):
            with self.assertRaisesRegex(
                self.ssh_client.UpdateCheckError, "checkupdates.*Exitcode 3"
            ):
                await self.ssh_client._check_arch(object())

    async def test_check_error_uses_gui_compatible_result(self):
        class ConnectionContext:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, exc_type, exc, traceback):
                return False

        host = {
            "id": 42,
            "name": "test-host",
            "primary_ip": "192.0.2.42",
            "port": 22,
            "user": "root",
            "auth_method": "key",
        }
        with (
            mock.patch.object(self.ssh_client, "_auth_params", return_value={}),
            mock.patch.object(
                self.ssh_client.asyncssh,
                "connect",
                return_value=ConnectionContext(),
            ),
            mock.patch.object(
                self.ssh_client,
                "_detect_distro",
                new=mock.AsyncMock(return_value="debian"),
            ),
            mock.patch.object(
                self.ssh_client,
                "_check_debian",
                new=mock.AsyncMock(
                    side_effect=self.ssh_client.UpdateCheckError("Paketprüfung fehlgeschlagen")
                ),
            ),
        ):
            result = await self.ssh_client.check_updates_for_host(host)

        self.assertEqual(result["host_id"], 42)
        self.assertEqual(result["name"], "test-host")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["note"], "Paketprüfung fehlgeschlagen")


if __name__ == "__main__":
    unittest.main()
