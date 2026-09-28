"""The action plan consumes existing snapshots, never live Docker state."""
from pathlib import Path
from unittest import mock
import unittest
from PyQt6 import QtCore, QtWidgets, QtTest
import test_docker_project_rows as fixtures
from sshupdater import ui_main
from sshupdater.ui_docker_preview import DockerUpdatePreviewDialog
from sshupdater.core import db, ssh_client, docker_image_updates, docker_compose


class PreviewTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.ProjectRowTests.setUpClass.__func__)
    setUp = fixtures.ProjectRowTests.setUp
    window = fixtures.ProjectRowTests.window
    result = fixtures.ProjectRowTests.result
    discovery = fixtures.ProjectRowTests.discovery
    show_projects = fixtures.ProjectRowTests.show_projects

    def project(self, name, *states):
        summaries = {'current': '✓ aktuell', 'update_available': '↑ 1 Image-Update',
                     'uncheckable': '⚠ Prüfung unvollständig', 'digest_pinned': 'Digest-fixiert',
                     'local_build': 'Lokaler Build'}
        return dict(name=name, config_files=[f'/srv/{name}/compose.yaml'], status='running(1)',
                    image_updates=dict(checked_at='2026-09-28T10:00:00+00:00',
                    summary=summaries.get(states[0], 'Nicht geprüft') if states else 'Nicht geprüft',
                    images=[dict(service=f'service-{i}', container_name=f'{name}-container',
                                 image=f'localhost:5000/{name}:latest', platform='linux/amd64',
                                 status=state, note='Sicherer Hinweis' if state == 'uncheckable' else '')
                            for i, state in enumerate(states)]))

    def open(self, w):
        before = w.findChildren(DockerUpdatePreviewDialog)
        w.act_docker_preview.trigger()
        dialogs = [d for d in w.findChildren(DockerUpdatePreviewDialog) if d not in before]
        self.assertEqual(len(dialogs), 1)
        self.assertFalse(w.act_docker_update.isEnabled())
        return dialogs[0]

    def test_toolbar_click_only_selected_grouped_hosts_and_identity(self):
        w = self.window()
        one = self.show_projects(w, [self.project('selected', 'update_available'), self.project('unused', 'current')], 1)
        two = self.show_projects(w, [self.project('second', 'current')], 2)
        one.child(0, 0).setCheckState(fixtures.CHECKED)
        two.child(0, 0).setCheckState(fixtures.CHECKED)
        w.table.collapse(one.index())
        toolbar = w.findChild(QtWidgets.QToolBar)
        w.show()
        self.app.processEvents()
        QtTest.QTest.mouseClick(toolbar.widgetForAction(w.act_docker_preview), QtCore.Qt.MouseButton.LeftButton)
        dialog, = w.findChildren(DockerUpdatePreviewDialog)
        self.assertEqual(dialog.windowTitle(), 'Docker Update-Vorschau')
        notice='\n'.join(label.text() for label in dialog.findChildren(QtWidgets.QLabel))
        self.assertIn('Es wurden keine Änderungen durchgeführt.',notice)
        self.assertIn('Die Vorschau zeigt den geplanten Updateablauf.',notice)
        self.assertNotIn('nur Preflight und Pull',notice)
        self.assertNotRegex(notice,r'Phase\s+[1-4]')
        self.assertEqual(dialog.projects.topLevelItemCount(), 2)
        self.assertEqual([h['host_id'] for h in dialog.snapshot], [1, 2])
        self.assertEqual([p['name'] for h in dialog.snapshot for p in h['projects']], ['selected', 'second'])
        self.assertIn('29.8.1', dialog.projects.topLevelItem(0).text(1))
        self.assertIn('v5.5.1', dialog.projects.topLevelItem(0).text(1))
        self.assertIn('connection_identity', dialog.snapshot[0])
        self.assertEqual(dialog.snapshot[0]['projects'][0]['config_files'], ['/srv/selected/compose.yaml'])
        self.assertNotIn('unused', dialog.details.toPlainText())
        self.assertFalse(w.act_docker_update.isEnabled())

    def test_status_semantics_service_details_and_counts(self):
        w = self.window()
        projects = [self.project(name, state) for name, state in [
            ('a-update', 'update_available'), ('b-current', 'current'), ('c-error', 'uncheckable'),
            ('d-pinned', 'digest_pinned'), ('e-build', 'local_build')]]
        parent = self.show_projects(w, projects)
        for i in range(5):
            parent.child(i, 0).setCheckState(fixtures.CHECKED)
        dialog = self.open(w)
        self.assertEqual(dialog.counts, dict(candidate=0, updates=1, current=1, incomplete=1, pinned=1, build=1))
        self.assertEqual([label.text() for label in dialog.summary_labels], [
            'Ausgewählte Projekte: 5', 'Freigabefähige Update-Projekte: 0', 'Erkannte Image-Updates: 1', 'Bereits aktuell: 1',
            'Nicht zuverlässig prüfbar: 1', 'Digest-fixiert: 1', 'Lokaler Build: 1'])
        root = dialog.projects.topLevelItem(0)
        texts = []
        for i in range(5):
            dialog.projects.setCurrentItem(root.child(i))
            texts.append(dialog.details.toPlainText())
        for value in ('service-0', 'a-update-container', 'localhost:5000/a-update:latest',
                      'linux/amd64', 'Projekt nicht zur Aktualisierung freigegeben', '2026-09-28T10:00:00+00:00'):
            self.assertIn(value, texts[0])
        self.assertIn('Keine Aktualisierung erforderlich.', texts[1])
        self.assertIn('Keine automatische Aktualisierung vorgesehen.', texts[2])
        self.assertIn('Sicherer Hinweis', texts[2])
        self.assertIn('Digest-fixiert – keine automatische Tag-Aktualisierung vorgesehen.', texts[3])
        self.assertIn('Lokaler Build – keine Registry-Aktualisierung vorgesehen.', texts[4])
        self.assertNotIn('compose down', '\n'.join(texts))
        self.assertNotIn('prune', '\n'.join(texts))
        labels = '\n'.join(label.text() for label in dialog.findChildren(QtWidgets.QLabel))
        self.assertIn('Es wurden keine Änderungen durchgeführt.', labels)
        self.assertIn('Stand der letzten Hostprüfung', labels)
        self.assertTrue(dialog.details.isReadOnly())
        self.assertEqual(dialog.close_button.text(), 'Schließen')
        QtTest.QTest.mouseClick(dialog.close_button, QtCore.Qt.MouseButton.LeftButton)
        self.assertFalse(w.act_docker_update.isEnabled())

    def test_snapshot_survives_new_selection_and_new_session_results(self):
        w = self.window()
        parent = self.show_projects(w, [self.project('a', 'current'), self.project('b', 'update_available')])
        parent.child(0, 0).setCheckState(fixtures.CHECKED)
        first = self.open(w)
        parent.child(0, 0).setCheckState(fixtures.UNCHECKED)
        parent.child(1, 0).setCheckState(fixtures.CHECKED)
        second = self.open(w)
        self.assertEqual(first.snapshot[0]['projects'][0]['name'], 'a')
        self.assertEqual(second.snapshot[0]['projects'][0]['name'], 'b')
        self.show_projects(w, [self.project('b', 'uncheckable')])
        self.assertEqual(second.snapshot[0]['projects'][0]['image_updates']['images'][0]['status'], 'update_available')
        self.assertEqual(first.counts['current'], 1)
        self.assertFalse(w.act_docker_update.isEnabled())

    def test_mixed_incomplete_and_missing_data_never_claim_current(self):
        w = self.window()
        parent = self.show_projects(w, [self.project('a-mixed', 'update_available', 'uncheckable'),
                                       self.project('b-empty')])
        for i in range(2):
            parent.child(i, 0).setCheckState(fixtures.CHECKED)
        dialog = self.open(w)
        self.assertEqual(dialog.counts['candidate'], 0)
        self.assertEqual(dialog.counts['updates'], 1)
        self.assertEqual(dialog.counts['incomplete'], 2)
        self.assertEqual(dialog.counts['current'], 0)
        self.assertIn('Projekt nicht zur Aktualisierung freigegeben', dialog.details.toPlainText())

    def test_layout_prioritizes_plan_and_long_content_scrolls_in_all_themes(self):
        w = self.window()
        project = self.project('update-test', 'update_available')
        project['config_files'] = ['/opt/docker-tests/update-test/compose.yaml']
        parent = self.show_projects(w, [project])
        parent.child(0, 0).setCheckState(fixtures.CHECKED)
        themes = Path(ui_main.__file__).parent / 'assets' / 'qss'
        for theme in ('light', 'dark', 'colour'):
            with self.subTest(theme=theme):
                w.setStyleSheet((themes / (theme + '.qss')).read_text(encoding='utf-8'))
                dialog = self.open(w)
                self.app.processEvents()
                self.assertGreater(dialog.details.height(), dialog.projects.height() * 2)
                self.assertLessEqual(dialog.height(), dialog.screen().availableGeometry().height())
                grid = dialog.summary.layout()
                for i, label in enumerate(dialog.summary_labels):
                    self.assertIs(grid.itemAtPosition(i // 2, i % 2).widget(), label)
                self.assertEqual(dialog.details.verticalScrollBar().maximum(), 0)
                self.assertEqual(dialog.close_button.text(), 'Schließen')
                self.assertFalse(w.act_docker_update.isEnabled())
                dialog.close()
                long_project = self.project('long-project', *(['update_available'] * 30))
                row = self.show_projects(w, [long_project])
                row.child(0, 0).setCheckState(fixtures.CHECKED)
                longer = self.open(w)
                self.app.processEvents()
                bar = longer.details.verticalScrollBar()
                self.assertGreater(bar.maximum(), 0)
                bar.setValue(bar.maximum())
                self.assertEqual(bar.value(), bar.maximum())
                self.assertTrue(longer.details.isReadOnly())
                longer.close()
                row = self.show_projects(w, [project])
                row.child(0, 0).setCheckState(fixtures.CHECKED)

    def test_no_remote_or_persistence_in_any_theme(self):
        w = self.window()
        p = self.project('test', 'uncheckable')
        p['image_updates']['images'][0].update(note='', reason='rate_limit')
        parent = self.show_projects(w, [p])
        parent.child(0, 0).setCheckState(fixtures.CHECKED)
        self._buttons = [a.isEnabled() for a in (w.act_check, w.act_sim, w.act_upg, w.act_clean, w.act_reboot, w.act_stop)]
        themes = Path(ui_main.__file__).parent / 'assets' / 'qss'
        with mock.patch.object(ssh_client, 'connect_host', side_effect=AssertionError('SSH')), \
             mock.patch.object(docker_compose, 'discover', side_effect=AssertionError('Docker')), \
             mock.patch.object(docker_image_updates.Checker, 'remote', side_effect=AssertionError('Registry')), \
             mock.patch.object(docker_image_updates, 'capture', side_effect=AssertionError('Command')), \
             mock.patch.object(db, 'set_check_result', side_effect=AssertionError('DB')):
            for theme in ('light', 'dark', 'colour'):
                w.setStyleSheet((themes / (theme + '.qss')).read_text(encoding='utf-8'))
                dialog = self.open(w)
                self.app.processEvents()
                self.assertTrue(dialog.isVisible())
                self.assertIn('HTTP 429', dialog.details.toPlainText())
                self.assertFalse(w.act_docker_update.isEnabled())
                dialog.close()
        self.assertEqual(self._buttons, [a.isEnabled() for a in (w.act_check, w.act_sim, w.act_upg, w.act_clean, w.act_reboot, w.act_stop)])
