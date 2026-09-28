import asyncio
from pathlib import Path
from unittest import mock
import unittest
from PyQt6 import QtCore, QtWidgets
import test_docker_preflight_ui as fixtures
from sshupdater import ui_main
from sshupdater.core import db, docker_apply


class ApplyUiTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.PreflightUiTests.setUpClass.__func__)
    setUp = fixtures.PreflightUiTests.setUp
    window = fixtures.PreflightUiTests.window
    result = fixtures.PreflightUiTests.result
    discovery = fixtures.PreflightUiTests.discovery
    show_projects = fixtures.PreflightUiTests.show_projects
    project = fixtures.PreflightUiTests.project
    approve = fixtures.PreflightUiTests.approve
    finish = fixtures.PreflightUiTests.finish

    def start(self, w):
        with mock.patch.object(db, 'list_hosts', return_value=self.hosts), \
             mock.patch.object(ui_main._DockerApplyWorker, 'start') as start:
            w.act_docker_update.trigger()
        start.assert_called_once()

    def test_missing_pending_never_starts_apply(self):
        w = self.window()
        with mock.patch.object(ui_main, '_DockerApplyWorker') as worker:
            w._start_docker_apply()
            self.approve(w)
            w._start_docker_apply()
        worker.assert_not_called()

    def test_success_consumes_pending_locks_preview_and_changes_button_all_themes(self):
        w = self.window()
        parent = self.approve(w)
        self.finish(w, 'passed')
        pending = w._docker_pull_state
        parent.child(0, 0).setCheckState(QtCore.Qt.CheckState.Unchecked)
        before = [a.isEnabled() for a in (w.act_check,w.act_sim,w.act_upg,w.act_clean,w.act_reboot)]
        self.start(w)
        self.assertEqual(w.preflight_worker.apply_run.state, pending)
        self.assertIsNone(w._docker_pull_state)
        self.assertFalse(w.act_docker_preview.isEnabled())
        w.preflight_worker.result = w.preflight_worker.apply_run.outcome('apply_succeeded', 'Die abschließende Registry-Verifikation steht noch aus.')
        w._on_apply_done()
        self.assertTrue(w._verification_pending())
        self.assertTrue(w.act_docker_update.isEnabled())
        self.assertEqual(w.act_docker_update.text(), 'Docker prüfen')
        parent.child(0, 0).setCheckState(QtCore.Qt.CheckState.Checked)
        w._open_docker_preview()
        self.assertIsNone(w._docker_plan)
        for theme in ('light', 'dark', 'colour'):
            w.setStyleSheet((Path(ui_main.__file__).parent/'assets'/'qss'/(theme+'.qss')).read_text(encoding="utf-8"))
            w._sync_docker_actions()
            self.assertFalse(w.act_docker_preview.isEnabled())
            self.assertTrue(w.act_docker_update.isEnabled())
        self.assertEqual(before, [a.isEnabled() for a in (w.act_check,w.act_sim,w.act_upg,w.act_clean,w.act_reboot)])
        dialog = next(d for d in w.findChildren(QtWidgets.QDialog) if d.windowTitle() == 'Docker-Apply erfolgreich')
        self.assertIn('Verifikation steht noch aus', dialog.findChild(QtWidgets.QPlainTextEdit).toPlainText())
        self.assertFalse(self.window()._verification_pending())

    def test_failure_invalidates_prepared_state_no_verification(self):
        w = self.window()
        self.approve(w)
        self.finish(w, 'passed')
        self.start(w)
        w.preflight_worker.result = w.preflight_worker.apply_run.outcome('failed', 'Zustand verändert.')
        w._on_apply_done()
        self.assertIsNone(w._docker_pull_state)
        self.assertFalse(w._verification_pending())
        self.assertFalse(w.act_docker_update.isEnabled())
        self.assertEqual(w.act_docker_update.text(), 'Docker-Update')

    def test_context_error_from_postcheck_is_visible_and_sanitized(self):
        import test_docker_apply as core_fixtures
        from sshupdater.core import docker_image_updates as images
        fixture = core_fixtures.ApplyTests()
        fixture.setUp()
        async def failed_postcheck():
            state = await fixture.prepare()
            error = images.CheckFailure('context', command='TOKEN=SECRET')
            error.args = ('SECRET',)
            with mock.patch.object(docker_apply, 'verify_applied', side_effect=error):
                return await fixture.execute(state)
        result = asyncio.run(failed_postcheck())
        self.assertEqual(result['reason'], 'context')
        w = self.window()
        w._show_apply_result(result)
        dialog = next(d for d in w.findChildren(QtWidgets.QDialog)
                      if d.windowTitle() == 'Docker-Apply nicht erfolgreich')
        text = dialog.findChild(QtWidgets.QPlainTextEdit).toPlainText()
        self.assertIn('Compose-Kontext nicht eindeutig rekonstruierbar', text)
        self.assertNotIn('SECRET', text)

    def test_connection_change_never_starts_worker(self):
        w = self.window()
        self.approve(w)
        self.finish(w, 'passed')
        with mock.patch.object(db, 'list_hosts', return_value=[dict(h, user='other') for h in self.hosts]), \
             mock.patch.object(ui_main, '_DockerApplyWorker') as worker:
            w._start_docker_apply()
        worker.assert_not_called()
        self.assertIsNone(w._docker_pull_state)
        self.assertFalse(w.act_docker_update.isEnabled())

    def test_responsive_worker_stop_is_conservative(self):
        w = self.window()
        self.approve(w)
        self.finish(w, 'passed')
        entered = []
        async def slow(run):
            entered.append(True)
            run.mutation_attempted = True
            await asyncio.Event().wait()
        pulses = []
        timer = QtCore.QTimer()
        timer.timeout.connect(lambda: pulses.append(True))
        timer.timeout.connect(lambda: w._on_stop_requested() if entered and len(pulses) >= 3 else None)
        loop = QtCore.QEventLoop()
        with mock.patch.object(db, 'list_hosts', return_value=self.hosts), \
             mock.patch.object(docker_apply.ApplyRun, 'run', slow):
            w.act_docker_update.trigger()
            timer.start(5)
            w.preflight_worker.finished.connect(loop.quit)
            QtCore.QTimer.singleShot(3000, loop.quit)
            loop.exec()
            timer.stop()
            w.preflight_worker.wait(1000)
            self.app.processEvents()
        self.assertGreaterEqual(len(pulses), 3)
        self.assertFalse(w._verification_pending())
        self.assertEqual(w._docker_apply_result['status'], 'cancelled')
        self.assertIn('verändert', w._docker_apply_result['note'])
        self.assertNotIn('keine Änderungen', w._docker_apply_result['note'])
