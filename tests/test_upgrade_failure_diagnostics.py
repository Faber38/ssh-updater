import importlib
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class UpgradeFailureDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
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

    async def _run_upgrade(self, lines, rc):
        class ConnectionContext:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, exc_type, exc, traceback):
                return False

        async def upgrade(_conn, _use_sudo):
            for line in lines:
                yield line
            yield self.ssh_client.CommandExit(rc)

        host = {
            "id": 42,
            "name": "test-host",
            "primary_ip": "192.0.2.42",
            "port": 22,
            "user": "root",
            "auth_method": "key",
        }
        with (
            mock.patch.object(
                self.ssh_client,
                "connect_host",
                return_value=ConnectionContext(),
            ),
            mock.patch.object(
                self.ssh_client,
                "_detect_distro",
                new=mock.AsyncMock(return_value="debian"),
            ),
            mock.patch.object(self.ssh_client, "_upgrade_debian", new=upgrade),
        ):
            return [message async for message in self.ssh_client.upgrade_host_stream(host)]

    async def test_success_does_not_log_failure_buffer(self):
        with mock.patch.object(self.ssh_client.logger, "error") as log_error:
            await self._run_upgrade(["upgrade complete"], 0)

        log_error.assert_not_called()

    async def test_failure_logs_recent_output(self):
        with self.assertLogs(self.ssh_client.logger, level="ERROR") as captured:
            await self._run_upgrade(["first detail", "E: dpkg lock held"], 100)

        log = "\n".join(captured.output)
        self.assertIn("first detail", log)
        self.assertIn("E: dpkg lock held", log)

    async def test_failure_logs_only_last_twenty_lines(self):
        lines = [f"remote line {number:02d}" for number in range(25)]
        with self.assertLogs(self.ssh_client.logger, level="ERROR") as captured:
            await self._run_upgrade(lines, 100)

        log = "\n".join(captured.output)
        self.assertNotIn("remote line 04", log)
        self.assertIn("remote line 05", log)
        self.assertIn("remote line 24", log)

    async def test_failure_logs_step_and_exit_code(self):
        with self.assertLogs(self.ssh_client.logger, level="ERROR") as captured:
            await self._run_upgrade(["failure"], 100)

        log = "\n".join(captured.output)
        self.assertIn("schritt=apt-get dist-upgrade", log)
        self.assertIn("exitcode=100", log)

    async def test_live_output_is_unchanged(self):
        lines = ["preparing packages", "E: package conflict"]
        messages = await self._run_upgrade(lines, 100)

        live_lines = [
            message["line"] for message in messages if message.get("type") == "line"
        ]
        self.assertEqual(live_lines, lines)


if __name__ == "__main__":
    unittest.main()
