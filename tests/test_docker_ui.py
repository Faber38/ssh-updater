import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PyQt6 import QtCore, QtWidgets, QtTest
from sshupdater import ui_main
from sshupdater.ui_docker import DockerDetailsDialog, DETAILS_ROLE
from sshupdater.core import db


class DockerTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        # Keep standalone runs away from the user's window settings.
        self.settings = QtCore.QSettings(str(Path(tmp) / 'settings.ini'), QtCore.QSettings.Format.IniFormat)
        self.enterContext(mock.patch.object(
            ui_main.QtCore, 'QSettings',
            side_effect=lambda *args: self.settings))
        self.hosts = [dict(id=i, name=f'host-{i}', primary_ip='test', user='root',
                           auth_method='key', pending_updates=3, last_check='old timestamp')
                      for i in (1, 2, 3)]

    def window(self):
        with mock.patch.object(db, 'list_hosts', return_value=self.hosts), \
                mock.patch.object(ui_main.SysInfoWidget, 'refresh'):
            window = ui_main.MainWindow()
        self.addCleanup(window.close)
        return window

    def result(self, window, discovery, *, host_id=1, status='ok', updates=0):
        result = dict(host_id=host_id, name=f'host-{host_id}', status=status,
                      distro='debian', updates=updates, note='package note')
        if discovery is not None:
            result['docker_compose'] = discovery
        with mock.patch.object(db, 'set_check_result') as save:
            window._on_check_result(result)
        return save

    def discovery(self, status='ok', **kwargs):
        return dict(status=status, docker_available=True, compose_available=True,
                    docker_version='Docker version 29.8.1, build abc123',
                    compose_version='Docker Compose version v5.5.1', projects=[], **kwargs)

    def test_registry_session_survives_new_workers_but_not_new_window(self):
        from sshupdater.core import ssh_client
        window = self.window()
        sessions = []
        async def check(host, *, registry_session):
            sessions.append(registry_session)
            return dict(host_id=host['id'], status='ok')
        with mock.patch.object(db, 'list_hosts', return_value=self.hosts), \
                mock.patch.object(ssh_client, 'check_updates_for_host', side_effect=check):
            for _ in range(2):
                worker = ui_main._CheckWorker(registry_session=window._registry_session)
                worker.run()
                self.assertIsNone(worker.fatal_error)
        self.assertEqual(len(sessions), 6)
        self.assertTrue(all(s is window._registry_session for s in sessions))
        self.assertIsNot(self.window()._registry_session, window._registry_session)

    def test_column_position_and_initial_state(self):
        model = self.window().table.model()
        self.assertEqual([model.horizontalHeaderItem(i).text() for i in range(model.columnCount())],
                         ['✓', 'Name', 'IP', 'User', 'Auth', 'Status', 'Docker', 'Letzte Prüfung'])
        for row in range(3):
            self.assertEqual(model.item(row, 6).text(), 'Nicht geprüft')
            self.assertFalse(model.item(row, 6).isEditable())
            self.assertEqual(model.item(row, 5).text(), 'Online – 3 Updates')
            self.assertEqual(model.item(row, 7).text(), 'old timestamp')

    def test_discovery_states_do_not_change_package_status(self):
        window = self.window()
        cases = [
            ({'status': 'docker_missing', 'docker_available': False}, '—'),
            (self.discovery(), '🐳 Docker 29.8.1'),
            (dict(self.discovery('compose_missing'), compose_available=False),
             '🐳 Docker 29.8.1 – Compose fehlt'),
            (self.discovery('permission_denied'), '⚠ Keine Berechtigung'),
        ]
        cases += [(self.discovery(status), '⚠ Docker-Fehler') for status in
                  ('daemon_unreachable', 'timeout', 'invalid_output', 'command_error')]
        for discovery, expected in cases:
            with self.subTest(status=discovery['status']):
                window.log.clear()
                original = copy.deepcopy(discovery)
                save = self.result(window, discovery)
                model = window.table.model()
                self.assertEqual(model.item(0, 6).text(), expected)
                self.assertFalse(model.item(0, 6).isEditable())
                self.assertEqual(model.item(0, 5).text(), 'Online – 0 Updates')
                self.assertEqual(window.log.toPlainText(), '✔ host-1 [debian]: 0 Updates\npackage note')
                self.assertEqual(discovery, original)
                save.assert_called_once_with(1, model.item(0, 7).text(), 0)

    def test_version_formats_and_unknown_version(self):
        window = self.window()
        for raw, expected in [
            ('Docker version 29.8.1, build abc123', '29.8.1'),
            ('  Docker version v29.8.1  ', '29.8.1'),
            ('29.8.1', '29.8.1'),
            ('Docker version 29.8.1-rc.1+vendor.2, build 123', '29.8.1-rc.1+vendor.2'),
            ('Docker version 20.10.24+dfsg1, build 297e128', '20.10.24+dfsg1'),
            ('unknown build 123.456', None), ('', None), (None, None),
        ]:
            with self.subTest(raw=raw):
                discovery = dict(self.discovery(), docker_version=raw)
                self.result(window, discovery)
                text = f'🐳 Docker {expected}' if expected else '🐳 Docker (Version unbekannt)'
                self.assertEqual(window.table.model().item(0, 6).text(), text)
                self.assertEqual(discovery['docker_version'], raw)

    def test_multiple_hosts_are_updated_by_id(self):
        window = self.window()
        self.result(window, self.discovery('permission_denied'), host_id=3, updates=8)
        self.result(window, self.discovery(), host_id=1)
        self.result(window, {'status': 'docker_missing'}, host_id=2, updates=4)
        model = window.table.model()
        self.assertEqual([model.item(r, 6).text() for r in range(3)],
                         ['🐳 Docker 29.8.1', '—', '⚠ Keine Berechtigung'])
        self.assertEqual([model.item(r, 5).text() for r in range(3)],
                         ['Online – 0 Updates', 'Online – 4 Updates', 'Online – 8 Updates'])

    def test_package_error_keeps_independent_docker_result(self):
        window = self.window()
        save = self.result(window, self.discovery(), status='error')
        model = window.table.model()
        self.assertEqual(model.item(0, 5).text(), 'Prüfung fehlgeschlagen')
        self.assertEqual(model.item(0, 6).text(), '🐳 Docker 29.8.1')
        save.assert_called_once_with(1, model.item(0, 7).text(), None)

    def test_missing_result_or_cancellation_does_not_keep_old_docker(self):
        window = self.window()
        for status in ('error', 'unknown', 'ok'):
            with self.subTest(status=status):
                self.result(window, self.discovery())
                self.result(window, None, status=status)
                self.assertEqual(window.table.model().item(0, 6).text(), 'Nicht geprüft')

    def test_new_check_clears_all_hosts_before_worker_starts(self):
        window = self.window()
        for host in self.hosts:
            self.result(window, self.discovery(), host_id=host['id'])
        model = window.table.model()
        model.item(0, 0).setCheckState(QtCore.Qt.CheckState.Checked)

        def started():
            self.assertEqual([model.item(r, 6).text() for r in range(3)], ['Nicht geprüft'] * 3)
            self.assertEqual(window.worker.host_ids, [1])

        with mock.patch.object(window, '_prepare_passwords', return_value=True), \
                mock.patch.object(ui_main._CheckWorker, 'start', side_effect=started) as start:
            window.act_check.trigger()
        start.assert_called_once()
        # Exercise the existing worker-to-GUI signal connection without SSH.
        with mock.patch.object(db, 'set_check_result'):
            window.worker.one_result.emit(dict(host_id=1, name='host-1', status='ok',
                                               updates=0, docker_compose=self.discovery()))
        self.assertEqual(model.item(0, 6).text(), '🐳 Docker 29.8.1')
        self.assertEqual(model.item(1, 6).text(), 'Nicht geprüft')

    def reload(self, window):
        with mock.patch.object(db, 'list_hosts', return_value=self.hosts):
            window._reload_hosts()

    def test_reload_preserves_session_docker_results(self):
        window = self.window()
        self.result(window, self.discovery())
        self.reload(window)
        self.assertEqual(window.table.model().item(0, 6).text(), '🐳 Docker 29.8.1')

    def test_config_dialog_close_preserves_session_results(self):
        from sshupdater.ui_config import ConfigDialog
        window = self.window()
        self.result(window, self.discovery())
        with mock.patch.object(db, 'list_hosts', return_value=self.hosts), \
                mock.patch.object(ConfigDialog, 'exec', return_value=0) as execute:
            window._open_config()
        execute.assert_called_once()
        self.assertEqual(window.table.model().item(0, 6).text(), '🐳 Docker 29.8.1')

    def test_new_window_has_no_previous_session_results(self):
        window = self.window()
        self.result(window, self.discovery())
        window.close()
        fresh = self.window()
        self.assertEqual(fresh.table.model().item(0, 6).text(), 'Nicht geprüft')
        self.assertEqual(fresh._docker_results, {})

    def test_new_host_starts_unchecked_and_deleted_host_is_evicted(self):
        window = self.window()
        self.result(window, self.discovery())
        deleted = self.hosts.pop(0)
        self.hosts.append(dict(deleted, id=4, name='new host'))
        self.reload(window)
        self.assertNotIn(deleted['id'], window._docker_results)
        self.assertNotIn(deleted['id'], window._docker_connections)
        self.assertEqual(window.table.model().item(2, 6).text(), 'Nicht geprüft')
        # Even reusing the removed ID must not resurrect the old result.
        self.hosts.append(deleted)
        self.reload(window)
        self.assertEqual(window.table.model().item(3, 6).text(), 'Nicht geprüft')

    def test_connection_changes_invalidate_only_affected_host(self):
        window = self.window()
        for field, value in [('primary_ip', 'other'), ('user', 'holger'), ('port', 2222),
                             ('auth_method', 'password'), ('key_path', '/synthetic/key'),
                             ('password_enc', b'changed encrypted credential')]:
            with self.subTest(field=field):
                self.result(window, self.discovery())
                self.result(window, self.discovery('permission_denied'), host_id=2)
                self.hosts[0][field] = value
                self.reload(window)
                self.assertEqual(window.table.model().item(0, 6).text(), 'Nicht geprüft')
                self.assertNotIn(1, window._docker_results)
                self.assertEqual(window.table.model().item(1, 6).text(), '⚠ Keine Berechtigung')

    def test_metadata_changes_and_reordering_preserve_each_hosts_status(self):
        window = self.window()
        self.result(window, self.discovery())
        self.result(window, self.discovery('permission_denied'), host_id=2)
        self.result(window, {'status': 'docker_missing'}, host_id=3)
        self.hosts[0].update(name='renamed', pending_updates=7, last_check='new timestamp',
                             tags_json='["test"]', distro='debian')
        self.hosts.reverse()
        self.reload(window)
        self.assertEqual([window.table.model().item(r, 6).text() for r in range(3)],
                         ['—', '⚠ Keine Berechtigung', '🐳 Docker 29.8.1'])

    def test_recheck_replaces_cached_result_and_missing_result_evicts_it(self):
        window = self.window()
        self.result(window, self.discovery())
        window.table.model().item(0, 0).setCheckState(QtCore.Qt.CheckState.Checked)
        with mock.patch.object(window, '_prepare_passwords', return_value=True), \
                mock.patch.object(ui_main._CheckWorker, 'start'):
            window._on_check()
        self.reload(window)
        self.assertEqual(window.table.model().item(0, 6).text(), 'Nicht geprüft')
        self.assertEqual(window._docker_results, {})
        self.result(window, self.discovery('permission_denied'))
        self.reload(window)
        self.assertEqual(window.table.model().item(0, 6).text(), '⚠ Keine Berechtigung')
        self.result(window, None, status='error')
        self.reload(window)
        self.assertEqual(window.table.model().item(0, 6).text(), 'Nicht geprüft')
        self.assertNotIn(1, window._docker_results)

    def test_session_result_is_a_snapshot(self):
        window = self.window()
        discovery = self.discovery()
        self.result(window, discovery)
        discovery['status'] = 'permission_denied'
        with mock.patch.object(db, 'list_hosts', return_value=self.hosts):
            window._reload_hosts()
        self.assertEqual(window.table.model().item(0, 6).text(), '🐳 Docker 29.8.1')

    def test_upgrade_timestamp_does_not_overwrite_docker_column(self):
        window = self.window()
        self.result(window, self.discovery('permission_denied'))
        with mock.patch.object(db, 'set_check_result') as save:
            window._on_upgrade_host_done(dict(host_id=1, name='host-1', status='ok', distro='debian'))
        model = window.table.model()
        self.assertEqual(model.item(0, 6).text(), '⚠ Keine Berechtigung')
        self.assertEqual(model.item(0, 5).text(), 'Online – 0 Updates')
        self.assertRegex(model.item(0, 7).text(), r'^\d{4}-\d{2}-\d{2} ')
        save.assert_called_once_with(1, model.item(0, 7).text(), 0)

    def click_cell(self, window, row, column, button=QtCore.Qt.MouseButton.LeftButton):
        window.show()
        index = window.table.model().index(row, column)
        window.table.scrollTo(index)
        self.app.processEvents()
        QtTest.QTest.mouseClick(window.table.viewport(), button,
                               pos=window.table.visualRect(index).center())

    def test_clickability_tooltips_and_hover(self):
        window = self.window()
        self.result(window, self.discovery())
        self.result(window, {'status': 'docker_missing'}, host_id=2)
        model = window.table.model()
        self.assertTrue(model.item(0, 6).data(DETAILS_ROLE))
        self.assertTrue(model.item(0, 6).font().underline())
        self.assertEqual(model.item(0, 6).toolTip(), 'Docker-Details anzeigen')
        window.show()
        for row, column, expected in [(0, 6, QtCore.Qt.CursorShape.PointingHandCursor),
                                      (1, 6, QtCore.Qt.CursorShape.ArrowCursor),
                                      (2, 6, QtCore.Qt.CursorShape.ArrowCursor),
                                      (0, 1, QtCore.Qt.CursorShape.ArrowCursor)]:
            index = model.index(row, column)
            window.table.scrollTo(index)
            self.app.processEvents()
            QtTest.QTest.mouseMove(window.table.viewport(), window.table.visualRect(index).center())
            self.assertEqual(window.table.viewport().cursor().shape(), expected)
        with mock.patch.object(DockerDetailsDialog, 'exec') as execute:
            self.click_cell(window, 1, 6)
            self.click_cell(window, 2, 6)
            self.click_cell(window, 0, 1)
            self.click_cell(window, 0, 6, QtCore.Qt.MouseButton.RightButton)
        execute.assert_not_called()
        self.assertFalse(model.item(1, 6).data(DETAILS_ROLE))
        self.assertEqual(model.item(1, 6).toolTip(), '')

    def test_click_opens_own_host_snapshot_without_remote_calls(self):
        from sshupdater.core import ssh_client, ssh_connection, docker_compose, remote_process
        window = self.window()
        self.result(window, self.discovery())
        self.result(window, dict(self.discovery(), docker_version='Docker version 28.0.0, build xyz'), host_id=2)
        seen = []
        def execute(dialog):
            seen.append((dialog.windowTitle(), dialog.engine.text()))
            return 0
        with mock.patch.object(DockerDetailsDialog, 'exec', execute), \
                mock.patch.object(ssh_client, 'connect_host', side_effect=AssertionError('remote')) as connect, \
                mock.patch.object(ssh_connection, 'connect_host', side_effect=AssertionError('remote')) as ssh, \
                mock.patch.object(docker_compose, 'discover', side_effect=AssertionError('remote')) as discover, \
                mock.patch.object(remote_process, 'capture', side_effect=AssertionError('remote')) as capture:
            self.click_cell(window, 1, 6)
            self.click_cell(window, 0, 6)
        self.assertEqual(seen, [('Docker – host-2', '28.0.0'), ('Docker – host-1', '29.8.1')])
        for operation in (connect, ssh, discover, capture):
            operation.assert_not_called()

    def test_details_versions_and_empty_projects(self):
        result = self.discovery()
        original = copy.deepcopy(result)
        dialog = DockerDetailsDialog(None, 'dockertest', result)
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.engine.text(), '29.8.1')
        self.assertEqual(dialog.compose.text(), 'v5.5.1')
        self.assertEqual(dialog.status.text(), 'Bereit')
        self.assertEqual(dialog.empty.text(), 'Keine Compose-Projekte gefunden')
        self.assertFalse(dialog.empty.isHidden())
        self.assertTrue(dialog.projects.isHidden())
        self.assertTrue(dialog.error.isHidden())
        self.assertEqual(result, original)

    def test_project_rows_counts_status_and_paths_are_read_only(self):
        projects = [dict(name='web-test', status='running(1)', container_count=1,
                         config_files=['/srv/web/compose.yml', '/srv/web/override.yml']),
                    dict(name='hello-test', status='exited(0)', container_count=0,
                         config_files=['ambiguous'], config_files_raw='/srv/a,b/compose.yml'),
                    dict(name='<b>literal</b>', status=None, container_count=None)]
        for rows in (projects[:1], projects):
            with self.subTest(count=len(rows)):
                dialog = DockerDetailsDialog(None, 'host', dict(self.discovery(), projects=rows))
                self.addCleanup(dialog.close)
                self.assertEqual(dialog.projects.rowCount(), len(rows))
                self.assertTrue(dialog.empty.isHidden())
                self.assertEqual(dialog.projects.editTriggers(), QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
                self.assertEqual([dialog.projects.item(0, c).text() for c in range(4)],
                                 ['web-test', 'running(1)', '1', '/srv/web/compose.yml\n/srv/web/override.yml'])
                if len(rows) > 1:
                    self.assertEqual(dialog.projects.item(1, 2).text(), '0')
                    self.assertEqual(dialog.projects.item(1, 3).text(), '/srv/a,b/compose.yml')
                    self.assertEqual(dialog.projects.item(2, 0).text(), '<b>literal</b>')
                    self.assertEqual(dialog.projects.item(2, 2).text(), '—')

    def test_error_clicks_show_only_safe_session_diagnostics(self):
        window = self.window()
        for status in ('compose_missing', 'permission_denied', 'daemon_unreachable',
                       'timeout', 'invalid_output', 'command_error'):
            with self.subTest(status=status):
                result = self.discovery(status, command='docker compose ls --format json',
                                        exit_code=1, note='<b>password=SIMULATED_SECRET token=SIMULATED_TOKEN</b>')
                self.result(window, result)
                self.assertEqual(window.table.model().item(0, 6).toolTip(), 'Docker-Fehlerdetails anzeigen')
                def execute(dialog):
                    self.assertFalse(dialog.error.isHidden())
                    self.assertTrue(dialog.error.isReadOnly())
                    self.assertIn('Status: ' + status, dialog.error.toPlainText())
                    self.assertIn('Befehl: ' + result['command'], dialog.error.toPlainText())
                    self.assertIn('Exitcode: 1', dialog.error.toPlainText())
                    from sshupdater.core.docker_compose import DISCOVERY_REASONS
                    self.assertTrue(dialog.error.toPlainText().endswith(DISCOVERY_REASONS[status]))
                    self.assertNotIn('SIMULATED_', dialog.error.toPlainText())
                    self.assertNotIn('<b>', dialog.error.toPlainText())
                    self.assertEqual(dialog.empty.text(), 'Projektliste nicht verfügbar')
                    return 0
                with mock.patch.object(DockerDetailsDialog, 'exec', execute):
                    self.click_cell(window, 0, 6)

    def test_error_without_confirmed_docker_requires_diagnostics(self):
        for discovery, clickable in [({'status': 'command_error'}, False),
                                     ({'status': 'timeout', 'command': 'docker --version'}, True)]:
            self.assertEqual(bool(ui_main._docker_status_item(discovery).data(DETAILS_ROLE)), clickable)

    def test_other_table_interactions_and_reload_keep_link_behavior(self):
        window = self.window()
        self.result(window, self.discovery())
        self.reload(window)
        with mock.patch.object(DockerDetailsDialog, 'exec') as execute:
            window.show()
            window.table.setCurrentIndex(window.table.model().index(0, 0))
            QtTest.QTest.keyClick(window.table, QtCore.Qt.Key.Key_Space)
            self.assertEqual(window._get_selected_host_ids(), [1])
            self.click_cell(window, 1, 1)
            self.assertEqual(window.table.currentIndex().row(), 1)
            execute.assert_not_called()
            self.click_cell(window, 0, 6)
            execute.assert_called_once()

    def test_next_open_uses_replaced_session_result(self):
        window = self.window()
        seen = []
        def execute(dialog):
            seen.append(dialog.status.text())
            return 0
        with mock.patch.object(DockerDetailsDialog, 'exec', execute):
            self.result(window, self.discovery())
            self.click_cell(window, 0, 6)
            self.result(window, self.discovery('permission_denied'))
            self.click_cell(window, 0, 6)
        self.assertEqual(seen, ['Bereit', 'Keine Berechtigung'])

    def image_project(self, name, status='current', note=''):
        from sshupdater.core.docker_image_updates import ImageCheck, project_result
        result = project_result([ImageCheck(service='web', container_name=name + '-web-1',
                                           image='nginx:1.28-alpine', platform='linux/amd64',
                                           status=status, note=note)])
        return dict(name=name, status='running(1)', container_count=1, image_updates=result)

    def test_image_project_column_and_selected_service_details(self):
        result = dict(self.discovery(), projects=[self.image_project('app-test'),
                      self.image_project('web-test', 'uncheckable', 'Registry-Rate-Limit erreicht.')])
        dialog = DockerDetailsDialog(None, 'dockertest', result)
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.projects.horizontalHeaderItem(4).text(), 'Image-Status')
        self.assertEqual(dialog.projects.item(0, 4).text(), '✓ aktuell')
        self.assertEqual(dialog.projects.item(1, 4).text(), '⚠ Prüfung unvollständig')
        self.assertEqual([dialog.image_details.item(0, c).text() for c in range(5)],
                         ['web\napp-test-web-1', 'nginx:1.28-alpine', 'linux/amd64', '✓ aktuell', '—'])
        dialog.projects.selectRow(1)
        self.assertEqual(dialog.image_details.item(0, 0).text(), 'web\nweb-test-web-1')
        self.assertEqual(dialog.image_details.item(0, 3).text(), 'Prüfung nicht möglich')
        self.assertEqual(dialog.image_details.item(0, 4).text(), 'Registry-Rate-Limit erreicht.')
        self.assertEqual(dialog.image_details.editTriggers(), QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)

    def test_image_status_labels_and_literal_error_text(self):
        for status, expected in [('current', '✓ aktuell'), ('update_available', '↑ Update verfügbar'),
                                 ('local_build', 'Lokaler Build'), ('digest_pinned', 'Digest-fixiert'),
                                 ('uncheckable', 'Prüfung nicht möglich')]:
            with self.subTest(status=status):
                project = self.image_project('test', status, '<b>literal</b> & text')
                dialog = DockerDetailsDialog(None, 'test', dict(self.discovery(), projects=[project]))
                self.addCleanup(dialog.close)
                self.assertEqual(dialog.image_details.item(0, 3).text(), expected)
                self.assertEqual(dialog.image_details.item(0, 4).text(), '<b>literal</b> & text')

    def test_image_session_replaced_invalidated_and_never_persisted(self):
        window = self.window()
        discovery = dict(self.discovery(), projects=[self.image_project('test')])
        save = self.result(window, discovery)
        # DB call remains exclusively host ID, timestamp and Linux update count.
        save.assert_called_once_with(1, window.table.model().item(0, 7).text(), 0)
        self.reload(window)
        self.assertEqual(window._docker_results[1]['projects'][0]['image_updates']['summary'], '✓ aktuell')
        updated = dict(self.discovery(), projects=[self.image_project('test', 'update_available')])
        self.result(window, updated)
        self.assertEqual(window._docker_results[1]['projects'][0]['image_updates']['summary'], '↑ 1 Image-Update')
        self.hosts[0]['user'] = 'other'
        self.reload(window)
        self.assertNotIn(1, window._docker_results)
        fresh = self.window()
        self.assertEqual(fresh._docker_results, {})

    def test_opening_image_details_performs_no_image_check(self):
        from sshupdater.core import docker_image_updates
        window = self.window()
        self.result(window, dict(self.discovery(), projects=[self.image_project('test')]))
        with mock.patch.object(docker_image_updates, 'check_projects', side_effect=AssertionError('remote')) as check, \
                mock.patch.object(DockerDetailsDialog, 'exec') as execute:
            self.click_cell(window, 0, 6)
        execute.assert_called_once()
        check.assert_not_called()

    def test_container_manifest_fallback_reaches_gui_as_platform_update(self):
        import asyncio
        import json
        from sshupdater.core import docker_image_updates as updates
        image_id = 'sha256:005c5d05cbdb47e3b08df59cf1086ce90aa5003f21b4b29fa1c7248bd8630b36'
        platform = {'os': 'linux', 'architecture': 'amd64'}
        local = {'mediaType': updates.MANIFEST,
                 'digest': 'sha256:f6cc744be370d4fe25b366b2bb025222f02def5781aba8f14c2ffdfc5c43c8cf',
                 'platform': platform}
        remote = {'mediaType': updates.INDEX, 'digest': 'sha256:' + 'b' * 64,
                  'manifests': [dict(local, digest='sha256:a9bc50a8cb1cad8c07dcbd427ca898bec224a1d6bda30000419d7373fe951d56')]}
        ref = 'localhost:5000/ssh-updater-test:latest'
        container = {'Id': 'c' * 64, 'Name': '/update-test-test-1', 'Image': image_id,
                     'Platform': 'linux', 'Config': {'Image': ref}, 'ImageManifestDescriptor': local}
        with mock.patch.object(updates, 'capture', side_effect=[
            (1, '[]', 'Error response from daemon: No such image: ' + image_id),
            (0, 'buildx v0.37.1', ''), (0, json.dumps(remote), '')]):
            row = asyncio.run(updates.Checker(object()).image(container, 'test', {'image': ref}))
        project = {'name': 'update-test', 'image_updates': updates.project_result([row])}
        dialog = DockerDetailsDialog(None, 'dockertest', dict(self.discovery(), projects=[project]))
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.projects.item(0, 4).text(), '↑ 1 Image-Update')
        self.assertEqual(dialog.image_details.item(0, 1).text(), ref)
        self.assertEqual(dialog.image_details.item(0, 2).text(), 'linux/amd64')
        self.assertEqual(dialog.image_details.item(0, 3).text(), '↑ Update verfügbar')
