import importlib
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class WorkerCompletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._home = tempfile.TemporaryDirectory()
        cls._home_patch = mock.patch.object(
            Path, "home", classmethod(lambda _cls: Path(cls._home.name))
        )
        cls._home_patch.start()
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

        from PyQt6 import QtWidgets
        from PyQt6.QtTest import QSignalSpy

        cls.QtWidgets = QtWidgets
        cls.QSignalSpy = QSignalSpy
        cls.ui_main = importlib.import_module("sshupdater.ui_main")
        cls.db = importlib.import_module("sshupdater.core.db")
        cls.ssh_client = importlib.import_module("sshupdater.core.ssh_client")
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.app.processEvents()
        for handler in logging.getLogger().handlers[:]:
            handler.close()
            logging.getLogger().removeHandler(handler)
        cls._home_patch.stop()
        cls._home.cleanup()

    def _workers(self):
        return (
            self.ui_main._CheckWorker([]),
            self.ui_main._SimWorker([]),
            self.ui_main._UpgradeWorker([]),
            self.ui_main._CleanSimWorker([]),
            self.ui_main._CleanRunWorker([]),
            self.ui_main._RebootWorker([]),
        )

    def _actions(self, window):
        return (
            window.act_check,
            window.act_sim,
            window.act_upg,
            window.act_clean,
            window.act_reboot,
            window.act_config,
        )

    def _window(self):
        with mock.patch.object(self.db, "list_hosts", return_value=[]):
            return self.ui_main.MainWindow()

    def test_successful_workers_emit_finished_once(self):
        with mock.patch.object(self.db, "list_hosts", return_value=[]):
            for worker in self._workers():
                with self.subTest(worker=type(worker).__name__):
                    spy = self.QSignalSpy(worker.finished_all)
                    worker.run()
                    self.assertEqual(len(spy), 1)
                    self.assertIsNone(worker.fatal_error)

    def test_exception_before_processing_sets_fatal_error_and_finishes_once(self):
        worker = self.ui_main._RebootWorker([])
        spy = self.QSignalSpy(worker.finished_all)
        with mock.patch.object(self.db, "list_hosts", side_effect=RuntimeError("DB kaputt")):
            worker.run()

        self.assertEqual(len(spy), 1)
        self.assertEqual(worker.fatal_error, "RuntimeError: DB kaputt")

    def test_exception_inside_processing_finishes_once(self):
        host = {
            "id": 1,
            "name": "host-1",
            "primary_ip": "192.0.2.1",
            "user": "root",
        }
        worker = self.ui_main._CheckWorker([1])
        spy = self.QSignalSpy(worker.finished_all)
        with (
            mock.patch.object(self.db, "list_hosts", return_value=[host]),
            mock.patch.object(
                self.ssh_client,
                "check_updates_for_host",
                new=mock.AsyncMock(side_effect=RuntimeError("unerwartet")),
            ),
        ):
            worker.run()

        self.assertEqual(len(spy), 1)
        self.assertEqual(worker.fatal_error, "RuntimeError: unerwartet")

    def test_single_host_error_continues_without_fatal_worker_error(self):
        hosts = [
            {"id": 1, "name": "host-1", "primary_ip": "192.0.2.1", "user": "root"},
            {"id": 2, "name": "host-2", "primary_ip": "192.0.2.2", "user": "root"},
        ]
        worker = self.ui_main._SimWorker([1, 2])
        results = self.QSignalSpy(worker.one_result)
        finished = self.QSignalSpy(worker.finished_all)
        simulate = mock.AsyncMock(
            side_effect=[
                RuntimeError("Hostfehler"),
                {"name": "host-2", "status": "ok", "packages": 0},
            ]
        )
        with (
            mock.patch.object(self.db, "list_hosts", return_value=hosts),
            mock.patch.object(
                self.ssh_client, "simulate_upgrade_for_host", new=simulate
            ),
        ):
            worker.run()

        self.assertEqual(simulate.await_count, 2)
        self.assertEqual(len(results), 2)
        self.assertEqual(len(finished), 1)
        self.assertIsNone(worker.fatal_error)

    def test_fatal_clean_simulation_does_not_open_confirmation(self):
        window = self._window()
        self.addCleanup(window.close)
        window.clean_sim_worker = SimpleNamespace(fatal_error="interner Fehler")
        window._clean_selected = [1]
        for action in self._actions(window):
            action.setEnabled(False)

        with mock.patch.object(self.ui_main.PlainMessageBox, "question") as question:
            window._on_clean_sim_done()

        question.assert_not_called()
        self.assertTrue(all(action.isEnabled() for action in self._actions(window)))
        self.assertFalse(window.act_stop.isEnabled())
        self.assertEqual(window._clean_selected, [])

    def test_fatal_upgrade_and_clean_release_gui_actions(self):
        window = self._window()
        self.addCleanup(window.close)

        for action in self._actions(window):
            action.setEnabled(False)
        window.act_stop.setEnabled(True)
        window.upg_worker = SimpleNamespace(
            fatal_error="Upgrade intern fehlgeschlagen", stop_requested=False
        )
        window._on_upgrade_done()
        self.assertTrue(all(action.isEnabled() for action in self._actions(window)))
        self.assertFalse(window.act_stop.isEnabled())

        for action in self._actions(window):
            action.setEnabled(False)
        window.act_stop.setEnabled(True)
        window.clean_run_worker = SimpleNamespace(
            fatal_error="Bereinigung intern fehlgeschlagen", stop_requested=False
        )
        window._on_clean_done()
        self.assertTrue(all(action.isEnabled() for action in self._actions(window)))
        self.assertFalse(window.act_stop.isEnabled())

    def test_fatal_reboot_releases_gui_actions(self):
        window = self._window()
        self.addCleanup(window.close)
        for action in self._actions(window):
            action.setEnabled(False)
        window.act_stop.setEnabled(True)
        window.reboot_worker = SimpleNamespace(fatal_error="Reboot intern fehlgeschlagen")

        window._on_reboot_done()

        self.assertTrue(all(action.isEnabled() for action in self._actions(window)))
        self.assertFalse(window.act_stop.isEnabled())


if __name__ == "__main__":
    unittest.main()
