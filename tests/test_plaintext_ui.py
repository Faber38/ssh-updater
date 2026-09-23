import os
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PyQt6 import QtCore, QtGui, QtWidgets
from sshupdater import ui_main
from sshupdater.core import db
from sshupdater.ui_text import PlainTextLog, PlainMessageBox

MARKUP = '<b>test</b> &amp; &#65; <img src="file:///nonexistent"> <img src="data:image/svg+xml,<svg/>">'


class ResourceSpy(QtGui.QTextDocument):
    def __init__(self, parent):
        super().__init__(parent)
        self.requests = []
        self.setDocumentLayout(QtWidgets.QPlainTextDocumentLayout(self))

    def loadResource(self, kind, url):
        self.requests.append(url)
        return None


class PlaintextUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def window(self):
        with mock.patch.object(db, 'list_hosts', return_value=[]), \
                mock.patch.object(ui_main.SysInfoWidget, 'refresh'):
            window = ui_main.MainWindow()
        self.addCleanup(window.close)
        window._clean_results = {}
        return window

    def test_actual_output_slots_preserve_markup_without_loading_resources(self):
        window = self.window()
        document = ResourceSpy(window.log)
        window.log.setDocument(document)
        paths = [
            (window._on_upgrade_progress, {'name': 'host', 'line': MARKUP}),
            (window._on_clean_progress, {'name': 'host', 'line': MARKUP}),
            (window._on_sim_result, {'status': 'ok', 'name': 'host', 'details': MARKUP}),
            (window._on_clean_sim_result, {'status': 'ok', 'name': 'host', 'details': MARKUP}),
        ]
        for slot in (window._on_check_result, window._on_sim_result,
                     window._on_clean_sim_result, window._on_upgrade_host_done,
                     window._on_clean_host_done, window._on_reboot_host_done):
            paths.append((slot, {'status': 'error', 'name': 'host', 'note': MARKUP}))
        for slot, payload in paths:
            with self.subTest(slot=slot.__name__, status=payload.get('status')):
                window.log.clear()
                slot(payload)
                self.app.processEvents()
                self.assertIn(MARKUP, window.log.toPlainText())
                block = document.begin()
                while block.isValid():
                    fragment = block.begin()
                    while not fragment.atEnd():
                        self.assertFalse(fragment.fragment().charFormat().isImageFormat())
                        fragment += 1
                    block = block.next()
        self.assertEqual(document.requests, [])

    def test_dialog_wrappers_are_plain_and_destructive_question_defaults_no(self):
        for method in ('information', 'warning', 'critical', 'question'):
            def execute(box):
                self.assertEqual(box.textFormat(), QtCore.Qt.TextFormat.PlainText)
                self.assertEqual(box.text(), MARKUP)
                if method == 'question':
                    self.assertEqual(box.standardButton(box.defaultButton()), PlainMessageBox.StandardButton.No)
                return int(PlainMessageBox.StandardButton.No)
            with self.subTest(method=method), mock.patch.object(PlainMessageBox, 'exec', execute):
                getattr(PlainMessageBox, method)(None, 'test', MARKUP)

    def test_log_has_bounded_document_and_entries(self):
        log = PlainTextLog()
        log.append('a' * (log.MAX_ENTRY_CHARS * 2))
        self.assertLess(len(log.toPlainText()), log.MAX_ENTRY_CHARS + 100)
        self.assertIn('[Anzeige gekürzt]', log.toPlainText())
        for _ in range(log.MAX_BLOCKS + 100):
            log.append(MARKUP)
        self.assertLessEqual(log.document().blockCount(), log.MAX_BLOCKS)
        self.assertFalse(log.isUndoRedoEnabled())

    def test_failed_or_missing_simulations_excluded_from_actual_clean_worker(self):
        window = self.window()
        window._clean_selected = [1, 2, 3]
        window.clean_sim_worker = SimpleNamespace(fatal_error=None)
        window._on_clean_sim_result({'host_id': 1, 'name': 'ok', 'status': 'ok'})
        window._on_clean_sim_result({'host_id': 2, 'name': 'failed', 'status': 'error'})
        with mock.patch.object(PlainMessageBox, 'question', return_value=PlainMessageBox.StandardButton.Yes), \
                mock.patch.object(ui_main._CleanRunWorker, 'start') as start, \
                mock.patch.object(db, 'get_host', return_value=dict(id=1, auth_method='key')):
            window._on_clean_sim_done()
        start.assert_called_once()
        self.assertEqual(window.clean_run_worker.host_ids, [1])

    def test_no_successful_simulation_never_offers_or_starts_cleanup(self):
        window = self.window()
        window._clean_selected = [1]
        window.clean_sim_worker = SimpleNamespace(fatal_error=None)
        window._on_clean_sim_result({'host_id': 1, 'name': 'failed', 'status': 'error'})
        with mock.patch.object(PlainMessageBox, 'question') as question, \
                mock.patch.object(ui_main._CleanRunWorker, 'start') as start:
            window._on_clean_sim_done()
        question.assert_not_called()
        start.assert_not_called()

    def test_finishing_local_wait_does_not_claim_remote_action_completed(self):
        window = self.window()
        for stopped in (False, True):
            for attr, result_slot, done_slot in (
                ('upg_worker', window._on_upgrade_host_done, window._on_upgrade_done),
                ('clean_run_worker', window._on_clean_host_done, window._on_clean_done),
            ):
                setattr(window, attr, SimpleNamespace(fatal_error=None, stop_requested=stopped))
                window.log.clear()
                result_slot({'name': 'host', 'status': 'unknown', 'note': 'Remote-Zustand unbekannt'})
                done_slot()
                self.assertIn('Remote-Zustand unbekannt', window.log.toPlainText())
                self.assertIn('Lokal', window.statusBar().currentMessage())
                self.assertNotIn('abgeschlossen', window.statusBar().currentMessage())

    def test_stopped_clean_simulation_never_prompts_or_starts_cleanup(self):
        window = self.window()
        window.clean_sim_worker = SimpleNamespace(fatal_error=None, stop_requested=True)
        window._clean_selected = [1, 2]
        window._clean_results = {1: 'ok', 2: 'unknown'}
        with mock.patch.object(PlainMessageBox, 'question') as question, \
                mock.patch.object(ui_main, '_CleanRunWorker') as worker:
            window._on_clean_sim_done()
        question.assert_not_called()
        worker.assert_not_called()
        self.assertEqual(window._clean_selected, [])
        self.assertFalse(window.act_stop.isEnabled())

    def test_close_stops_check_and_closes_after_thread_exits(self):
        import asyncio
        import threading
        from sshupdater.core import ssh_client
        window = self.window()
        entered = threading.Event()
        started = []
        hosts = [dict(id=i, name=f'host-{i}', primary_ip='test', user='root') for i in (1, 2)]
        async def check(host):
            started.append(host['id'])
            entered.set()
            await asyncio.Event().wait()
        worker = ui_main._CheckWorker([1, 2])
        window.worker = worker
        with mock.patch.object(db, 'list_hosts', return_value=hosts), \
                mock.patch.object(ssh_client, 'check_updates_for_host', check):
            window.show()
            worker.start()
            ready = entered.wait(2)
            window.close()
            completed = worker.wait(3000)
            loop = QtCore.QEventLoop()
            QtCore.QTimer.singleShot(250, loop.quit)
            loop.exec()
        self.assertTrue(ready)
        self.assertTrue(completed)
        self.assertEqual(started, [1])
        self.assertFalse(window.isVisible())
