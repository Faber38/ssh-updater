import asyncio
from copy import deepcopy
from pathlib import Path
from unittest import mock
import unittest
from PyQt6 import QtCore, QtWidgets
import test_docker_apply_ui as fixtures
from sshupdater import ui_main
from sshupdater.core import db, docker_verification, docker_image_updates as images


class VerificationUiTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.ApplyUiTests.setUpClass.__func__)
    setUp = fixtures.ApplyUiTests.setUp
    window = fixtures.ApplyUiTests.window
    result = fixtures.ApplyUiTests.result
    discovery = fixtures.ApplyUiTests.discovery
    show_projects = fixtures.ApplyUiTests.show_projects
    project = fixtures.ApplyUiTests.project
    approve = fixtures.ApplyUiTests.approve
    finish = fixtures.ApplyUiTests.finish
    start = fixtures.ApplyUiTests.start

    def pending(self, w):
        self.approve(w)
        self.finish(w, 'passed')
        self.start(w)
        p = w.preflight_worker.apply_run.state['plan'].candidates[0]
        row = dict(host_id=p.host_id, project=p.name, paths=p.paths, connection=p.connection,
                   compose_identity=p.compose_identity, services=['service-0'], status='apply_succeeded', containers=[])
        w.preflight_worker.apply_run.applies = [row]
        w.preflight_worker.result = w.preflight_worker.apply_run.outcome('apply_succeeded', 'Apply erfolgreich')
        w._on_apply_done()
        return row

    def verification_result(self, row, status='verified'):
        project = self.project(row['project'], 'current' if status == 'verified' else 'update_available')
        checked = deepcopy(project['image_updates'])
        if status not in ('verified','update_available'):
            checked = images.project_result([images.ImageCheck(service='service-0').fail(images.CheckFailure('rate_limit_backoff'))])
        else:
            checked['summary'] = '✓ aktuell' if status == 'verified' else '↑ 1 Image-Update'
        record = dict(row, status=status, reason='', note='Apply erfolgreich', image_updates=checked)
        completed = status in ('verified','update_available')
        return dict(status=status if completed else 'pending', completed=completed,
                    verification_pending=not completed, apply_status='apply_succeeded', projects=[record])

    def start_verification(self, w):
        with mock.patch.object(db,'list_hosts',return_value=self.hosts), \
             mock.patch.object(ui_main._DockerVerificationWorker,'start') as start:
            w.act_docker_update.trigger()
        start.assert_called_once()

    def test_only_pending_can_start(self):
        w = self.window()
        with mock.patch.object(ui_main,'_DockerVerificationWorker') as worker:
            w._start_docker_verification()
            self.approve(w)
            w._start_docker_verification()
        worker.assert_not_called()

    def test_completion_updates_project_clears_intermediate_states_and_preserves_system_status(self):
        w = self.window()
        row = self.pending(w)
        before = w.table.model().item(0,5).text()
        self.start_verification(w)
        self.assertFalse(w.act_docker_update.isEnabled())
        self.assertFalse(w.act_docker_preview.isEnabled())
        w.preflight_worker.result = self.verification_result(row)
        w._on_verification_done()
        self.assertFalse(w._verification_pending())
        self.assertIsNone(w._docker_apply_result)
        self.assertIsNone(w._docker_pull_state)
        self.assertIsNone(w._docker_plan)
        self.assertEqual(w.act_docker_update.text(),'Docker-Update')
        self.assertFalse(w.act_docker_update.isEnabled())
        self.assertTrue(w.act_docker_preview.isEnabled())
        self.assertEqual(w._docker_results[1]['projects'][0]['image_updates']['summary'],'✓ aktuell')
        self.assertEqual(w.table.model().item(0,0).child(0,6).text(),'✓ aktuell')
        self.assertEqual(w.table.model().item(0,5).text(),before)
        w.act_docker_preview.trigger()
        self.assertFalse(w.act_docker_update.isEnabled())

    def test_new_registry_state_requires_new_preview(self):
        w = self.window()
        row = self.pending(w)
        self.start_verification(w)
        w.preflight_worker.result = self.verification_result(row,'update_available')
        w._on_verification_done()
        self.assertFalse(w._verification_pending())
        self.assertFalse(w.act_docker_update.isEnabled())
        self.assertEqual(w._docker_results[1]['projects'][0]['image_updates']['summary'],'↑ 1 Image-Update')
        w.act_docker_preview.trigger()
        self.assertTrue(w.act_docker_update.isEnabled())

    def test_pending_registry_failure_preserves_apply_and_locks_preview_all_themes(self):
        w = self.window()
        row = self.pending(w)
        applied = deepcopy(w._docker_apply_result)
        self.start_verification(w)
        w.preflight_worker.result = self.verification_result(row,'registry_pending')
        w._on_verification_done()
        self.assertEqual(w._docker_apply_result,applied)
        w._open_docker_preview()
        self.assertIsNone(w._docker_plan)
        for theme in ('light','dark','colour'):
            w.setStyleSheet((Path(ui_main.__file__).parent/'assets'/'qss'/(theme+'.qss')).read_text())
            w._sync_docker_actions()
            self.assertEqual(w.act_docker_update.text(),'Docker prüfen')
            self.assertTrue(w.act_docker_update.isEnabled())
            self.assertFalse(w.act_docker_preview.isEnabled())
        self.assertFalse(self.window()._verification_pending())

    def test_responsive_worker_cancellation_keeps_pending_and_apply_success(self):
        w = self.window()
        self.pending(w)
        applied = deepcopy(w._docker_apply_result)
        entered = []
        async def slow(run):
            entered.append(True)
            await asyncio.Event().wait()
        pulses = []
        timer = QtCore.QTimer()
        timer.timeout.connect(lambda:pulses.append(True))
        timer.timeout.connect(lambda:w._on_stop_requested() if entered and len(pulses)>=3 else None)
        loop = QtCore.QEventLoop()
        with mock.patch.object(db,'list_hosts',return_value=self.hosts), \
             mock.patch.object(docker_verification.VerificationRun,'run',slow):
            w.act_docker_update.trigger()
            timer.start(5)
            w.preflight_worker.finished.connect(loop.quit)
            QtCore.QTimer.singleShot(3000,loop.quit)
            loop.exec()
            timer.stop()
            w.preflight_worker.wait(1000)
            self.app.processEvents()
        self.assertGreaterEqual(len(pulses),3)
        self.assertEqual(w._docker_apply_result,applied)
        self.assertTrue(w._verification_pending())
        self.assertTrue(w.act_docker_update.isEnabled())
        self.assertEqual(w._docker_verification_result['status'],'cancelled')
        dialog=next(d for d in w.findChildren(QtWidgets.QDialog) if d.windowTitle()=='Docker-Apply erfolgreich – Abschlussprüfung')
        text=dialog.findChild(QtWidgets.QPlainTextEdit).toPlainText()
        self.assertIn('abgebrochen',text)
        self.assertNotIn('Apply fehlgeschlagen',text)
