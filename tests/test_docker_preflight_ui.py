import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import unittest
from PyQt6 import QtCore, QtWidgets
import test_docker_plan as fixtures
from sshupdater import ui_main
from sshupdater.core import db, docker_preflight, docker_pull


class PreflightUiTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.PlanTests.setUpClass.__func__)
    setUp = fixtures.PlanTests.setUp
    window = fixtures.PlanTests.window
    result = fixtures.PlanTests.result
    discovery = fixtures.PlanTests.discovery
    show_projects = fixtures.PlanTests.show_projects
    project = fixtures.PlanTests.project
    approve = fixtures.PlanTests.approve

    def test_no_plan_cannot_start(self):
        w = self.window()
        with mock.patch.object(ui_main, '_DockerPreflightWorker') as worker:
            w._start_docker_preflight()
        worker.assert_not_called()

    def finish(self, w, status, *, cancelled=False):
        plan = w._docker_plan
        run = docker_pull.PullRun(plan, {})
        if status == 'passed':
            run.mutation_attempted = True
            run.pulls = [dict(host_id=p.host_id, host='test', project=p.name, service=i[0], image=i[3],
                              platform=i[4], status='pulled', exit_code=0)
                         for p in plan.candidates for i in p.images]
            result = run.outcome('pulled', 'Image erfolgreich geladen.')
        else:
            result = run.outcome('failed', 'Preflight fehlgeschlagen.')
        w.preflight_worker = SimpleNamespace(plan=plan, result=result, stop_requested=cancelled, pull_run=run)
        w._preflight_busy = True
        w._preflight_actions = {w.act_check: True}
        w._on_preflight_done()
        return plan

    def test_failure_invalidates_and_requires_new_normal_check(self):
        w = self.window()
        self.approve(w)
        self.finish(w, 'failed')
        self.assertIsNone(w._docker_plan)
        self.assertFalse(w.act_docker_update.isEnabled())
        w.act_docker_preview.trigger()
        self.assertIsNone(w._docker_plan)
        self.show_projects(w, [self.project()])
        w.act_docker_preview.trigger()
        self.assertTrue(w.act_docker_update.isEnabled())

    def test_success_consumes_plan_displays_apply_pending_and_blocks_repeat(self):
        w = self.window()
        self.approve(w)
        plan = self.finish(w, 'passed')
        self.assertIsNone(w._docker_plan)
        self.assertIs(w._docker_pull_state['plan'], plan)
        self.assertTrue(w._docker_pull_state['result']['apply_pending'])
        dialogs = [d for d in w.findChildren(QtWidgets.QDialog) if d.windowTitle() == 'Docker Image-Pull erfolgreich']
        text = dialogs[0].findChild(QtWidgets.QPlainTextEdit)
        self.assertIn('Der laufende Container wurde noch nicht aktualisiert.', text.toPlainText())
        self.assertNotRegex(text.toPlainText(),r'Phase\s+[1-4]')
        self.assertIn('Die Container-Aktualisierung steht noch aus.',text.toPlainText())
        self.assertIn('kein Container neu erstellt', text.toPlainText())
        self.assertNotIn('keine Änderungen', text.toPlainText())
        self.assertTrue(text.isReadOnly())
        with mock.patch.object(ui_main._DockerPreflightWorker, 'start') as start:
            w.act_docker_preview.trigger()
            w._open_docker_preview()
        start.assert_not_called()
        self.assertTrue(w.act_docker_update.isEnabled())
        self.assertEqual(w.act_docker_update.text(), 'Docker anwenden')
        self.assertFalse(w.act_docker_preview.isEnabled())
        self.assertIn('„Docker anwenden“', text.toPlainText())
        self.assertIsNone(self.window()._docker_pull_state)

    def test_cancelled_completion_is_never_success(self):
        w = self.window()
        self.approve(w)
        self.finish(w, 'passed', cancelled=True)
        self.assertIsNone(w._preflight_passed)
        self.assertIsNone(w._docker_plan)
        self.assertIsNone(w._prepared_docker_apply())
        self.assertEqual(w.act_docker_update.text(), 'Docker-Update')
        self.assertFalse(w.act_docker_update.isEnabled())

    def test_apply_requires_complete_success_and_exact_candidate_coverage(self):
        w = self.window()
        self.approve(w, [self.project('a'), self.project('b')])
        self.finish(w, 'passed')
        original = deepcopy(w._docker_pull_state)
        mutations = [lambda r, s=s: r.update(status=s) for s in ('failed', 'cancelled', 'unknown')]
        mutations += [lambda r: r.update(apply_pending=False),
                      lambda r: r.update(mutation_attempted=False),
                      lambda r: r['pulls'].pop(),
                      lambda r: r['pulls'].append(dict(r['pulls'][0])),
                      lambda r: r['pulls'][0].update(status='unknown'),
                      lambda r: r['pulls'][0].update(exit_code=1),
                      lambda r: r['pulls'][0].update(service='other')]
        for change in mutations:
            with self.subTest(change=change):
                w._docker_pull_state = deepcopy(original)
                change(w._docker_pull_state['result'])
                w._sync_docker_actions()
                self.assertIsNone(w._prepared_docker_apply())
                self.assertEqual(w.act_docker_update.text(), 'Docker-Update')
                self.assertFalse(w.act_docker_update.isEnabled())

    def test_pending_targets_independent_of_selection_preview_and_display(self):
        w = self.window()
        parent = self.approve(w, [self.project('a'), self.project('b', 'current')])
        self.finish(w, 'passed')
        state = w._docker_pull_state
        saved = deepcopy(state)
        self.assertEqual([p.name for p in state['plan'].candidates], ['a'])
        for row in range(parent.rowCount()):
            parent.child(row, 0).setCheckState(QtCore.Qt.CheckState.Unchecked)
        parent.child(1, 0).setCheckState(QtCore.Qt.CheckState.Checked)
        w._open_docker_preview()  # Guard direct calls as well as disabled QAction.
        w.table.collapseAll()
        w.table.expandAll()
        w._on_stop_requested()
        with mock.patch.object(ui_main.DockerDetailsDialog, 'exec', return_value=0):
            w._open_docker_details(w.table.model().index(0, 6))
        for theme in ('light', 'dark', 'colour'):
            qss = Path(ui_main.__file__).parent / 'assets' / 'qss' / (theme + '.qss')
            w.setStyleSheet(qss.read_text(encoding='utf-8'))
            w._sync_docker_actions()
            self.assertTrue(w.act_docker_update.isEnabled())
            self.assertFalse(w.act_docker_preview.isEnabled())
        self.assertIs(w._docker_pull_state, state)
        self.assertEqual(state, saved)
        self.assertIsNone(w._docker_plan)

    def test_apply_click_consumes_state_and_restart_has_no_prepared_state(self):
        w = self.window()
        self.approve(w)
        self.finish(w, 'passed')
        state = w._docker_pull_state
        with mock.patch.object(db, 'list_hosts', return_value=self.hosts), \
             mock.patch.object(ui_main._DockerApplyWorker, 'start') as start:
            w.act_docker_update.trigger()
        start.assert_called_once()
        self.assertEqual(w.preflight_worker.apply_run.state, state)
        self.assertIsNone(w._docker_pull_state)
        self.assertFalse(w.act_docker_preview.isEnabled())
        w.preflight_worker.result = w.preflight_worker.apply_run.outcome('failed', 'Test')
        w._on_apply_done()
        other = self.window()
        self.assertIsNone(other._docker_pull_state)
        self.assertEqual(other.act_docker_update.text(), 'Docker-Update')
        self.assertFalse(other.act_docker_update.isEnabled())

    def test_multiple_hosts_prepare_only_confirmed_candidates_without_secrets(self):
        w = self.window()
        project = self.project('a')
        project.update(password='SECRET', token='SECRET')
        self.approve(w, [project], host=1)
        self.approve(w, [self.project('b')], host=2)
        self.finish(w, 'passed')
        state = w._prepared_docker_apply()
        self.assertIsNotNone(state)
        self.assertEqual({p.host_id for p in state['plan'].candidates}, {1, 2})
        self.assertEqual(len(state['result']['pulls']), 2)
        self.assertNotIn('SECRET', repr(state))

    def test_real_worker_keeps_gui_responsive_and_stop_cancels(self):
        w = self.window()
        self.approve(w)
        entered = []
        async def slow(plan, hosts):
            entered.append(True)
            await asyncio.Event().wait()
        pulses = []
        timer = QtCore.QTimer()
        timer.timeout.connect(lambda: pulses.append(True))
        timer.timeout.connect(lambda: w._on_stop_requested() if entered and len(pulses) >= 3 else None)
        loop = QtCore.QEventLoop()
        with mock.patch.object(db, 'list_hosts', return_value=self.hosts), \
             mock.patch.object(docker_preflight, 'preflight', side_effect=slow):
            w._start_docker_preflight()
            timer.start(5)
            w.preflight_worker.finished.connect(loop.quit)
            QtCore.QTimer.singleShot(2000, loop.quit)
            loop.exec()
            timer.stop()
            w.preflight_worker.wait(1000)
            self.app.processEvents()
        self.assertTrue(entered)
        self.assertGreater(len(pulses), 1)
        self.assertEqual(w.preflight_worker.result['reason'], 'cancelled')
        self.assertFalse(w._preflight_busy)
        self.assertIsNone(w._docker_plan)
        self.assertTrue(w.act_check.isEnabled())

    def test_gui_responsive_during_pull_and_abort_reports_possible_changes(self):
        w = self.window()
        self.approve(w)
        async def slow(run):
            run.mutation_attempted = True
            run.progress('Docker-Image wird geladen …')
            await asyncio.Event().wait()
        pulses = []
        timer = QtCore.QTimer()
        timer.timeout.connect(lambda: pulses.append(True))
        timer.timeout.connect(lambda: w._on_stop_requested()
                              if w.preflight_worker.pull_run.mutation_attempted and len(pulses) >= 3 else None)
        loop = QtCore.QEventLoop()
        with mock.patch.object(db, 'list_hosts', return_value=self.hosts), \
             mock.patch.object(docker_pull.PullRun, 'run', slow):
            w._start_docker_preflight()
            timer.start(5)
            w.preflight_worker.finished.connect(loop.quit)
            QtCore.QTimer.singleShot(2000, loop.quit)
            loop.exec()
            timer.stop()
            w.preflight_worker.wait(1000)
            self.app.processEvents()
        self.assertGreater(len(pulses), 1)
        self.assertTrue(w._docker_pull_state['result']['mutation_attempted'])
        dialog = [d for d in w.findChildren(QtWidgets.QDialog) if d.windowTitle() == 'Docker Image-Pull abgebrochen'][0]
        text = dialog.findChild(QtWidgets.QPlainTextEdit).toPlainText()
        self.assertIn('teilweise verändert', text)
        self.assertNotIn('keine Änderungen', text)
        self.assertTrue(w.act_check.isEnabled())

    def test_changed_connection_before_start_requires_new_normal_check(self):
        w = self.window()
        self.approve(w)
        hosts = [dict(h, primary_ip='changed') for h in self.hosts]
        with mock.patch.object(db, 'list_hosts', return_value=hosts), \
             mock.patch.object(ui_main, '_DockerPreflightWorker') as worker:
            w.act_docker_update.trigger()
        worker.assert_not_called()
        w.act_docker_preview.trigger()
        self.assertIsNone(w._docker_plan)

    def test_stop_after_thread_completion_but_before_result_delivery(self):
        w = self.window()
        self.approve(w)
        worker = ui_main._DockerPreflightWorker(w._docker_plan, {})
        worker.result = {'status': 'passed'}
        w.preflight_worker = worker
        w._preflight_actions = {}
        w._preflight_busy = True
        w._on_stop_requested()
        w._on_preflight_done()
        self.assertIsNone(w._preflight_passed)
        self.assertIsNone(w._docker_plan)

    def test_plan_change_during_worker_suppresses_success(self):
        w = self.window()
        self.approve(w)
        plan = w._docker_plan
        w._invalidate_docker_plan()
        w.preflight_worker = SimpleNamespace(plan=plan, result={'status': 'passed'}, stop_requested=False)
        w._preflight_actions = {}
        w._on_preflight_done()
        self.assertIsNone(w._preflight_passed)
        self.assertFalse(w.act_docker_update.isEnabled())
