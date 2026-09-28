"""Project rows use session snapshots only; host actions never traverse children."""
from pathlib import Path
from unittest import mock
import unittest

import test_docker_ui as fixtures
from PyQt6 import QtCore, QtTest, QtWidgets, QtGui
from sshupdater import ui_main
from sshupdater.core import db, docker_image_updates, ssh_client

CHECKED = QtCore.Qt.CheckState.Checked
UNCHECKED = QtCore.Qt.CheckState.Unchecked


class ProjectRowTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.DockerTableTests.setUpClass.__func__)
    setUp = fixtures.DockerTableTests.setUp
    window = fixtures.DockerTableTests.window
    result = fixtures.DockerTableTests.result
    discovery = fixtures.DockerTableTests.discovery

    def project(self, name, summary='✓ aktuell'):
        return dict(name=name, status='running(1)', config_files=[f'/srv/{name}/compose.yaml'],
                    image_updates={'summary': summary})

    def show_projects(self, window, projects, host=1):
        data = self.discovery()
        data['projects'] = projects
        self.result(window, data, host_id=host)
        return window.table.model().item(window._find_row_by_host_id(host), 0)

    def assert_docker_buttons(self, window, preview):
        self.assertEqual(window.act_docker_preview.isEnabled(), preview)
        self.assertFalse(window.act_docker_update.isEnabled())

    def test_docker_toolbar_position_initial_state_and_no_handlers(self):
        w = self.window()
        toolbar = w.findChild(QtWidgets.QToolBar)
        actions = toolbar.actions()
        self.assertEqual(w.act_docker_preview.text(), 'Update-Vorschau')
        self.assertEqual(w.act_docker_update.text(), 'Docker-Update')
        self.assertLess(actions.index(w.act_toggle_checks), actions.index(w.act_docker_preview))
        self.assertLess(actions.index(w.act_docker_preview), actions.index(w.act_docker_update))
        self.assertLess(actions.index(w.act_docker_update), actions.index(w.act_stop))
        self.assertFalse(toolbar.widgetForAction(w.act_docker_preview).isEnabled())
        self.assertFalse(toolbar.widgetForAction(w.act_docker_update).isEnabled())
        self.assert_docker_buttons(w, False)
        for action in (w.act_check, w.act_sim, w.act_upg, w.act_clean, w.act_reboot, w.act_config):
            self.assertTrue(action.isEnabled())
        self.assertFalse(w.act_stop.isEnabled())

    def test_docker_buttons_ignore_hosts_and_follow_all_project_statuses(self):
        w = self.window()
        parent = self.show_projects(w, [self.project('a'), self.project('b', '↑ 1 Image-Update'),
                                       self.project('c', '⚠ Prüfung unvollständig')])
        w._set_all_checks(True)
        self.assert_docker_buttons(w, False)
        w._set_all_checks(False)
        for i in range(3):
            parent.child(i, 0).setCheckState(CHECKED)
            self.assert_docker_buttons(w, True)
            parent.child(i, 0).setCheckState(UNCHECKED)
            self.assert_docker_buttons(w, False)
        parent.child(0, 0).setCheckState(CHECKED)
        parent.child(1, 0).setCheckState(CHECKED)
        parent.child(0, 0).setCheckState(UNCHECKED)
        self.assert_docker_buttons(w, True)
        w.table.collapse(parent.index())
        self.assert_docker_buttons(w, True)
        parent.child(1, 0).setCheckState(UNCHECKED)
        self.assert_docker_buttons(w, False)

    def test_buttons_follow_removal_reload_and_host_invalidation(self):
        w = self.window()
        a = self.show_projects(w, [self.project('a')], 1)
        b = self.show_projects(w, [self.project('b')], 2)
        a.child(0, 0).setCheckState(CHECKED)
        b.child(0, 0).setCheckState(CHECKED)
        self.show_projects(w, [], 1)
        self.assert_docker_buttons(w, True)
        with mock.patch.object(db, 'list_hosts', return_value=self.hosts):
            w._reload_hosts()
        self.assert_docker_buttons(w, True)
        self.hosts[1]['primary_ip'] = 'changed'
        with mock.patch.object(db, 'list_hosts', return_value=self.hosts):
            w._reload_hosts()
        self.assert_docker_buttons(w, False)
        parent = self.show_projects(w, [self.project('a')], 1)
        parent.child(0, 0).setCheckState(CHECKED)
        self.result(w, None)
        self.assert_docker_buttons(w, False)

    def test_button_clicks_and_selection_never_call_remote_in_any_theme(self):
        w = self.window()
        parent = self.show_projects(w, [self.project('a', '↑ 1 Image-Update')])
        toolbar = w.findChild(QtWidgets.QToolBar)
        w.show()
        themes = Path(ui_main.__file__).parent / 'assets' / 'qss'
        with mock.patch.object(ssh_client, 'check_updates_for_host') as remote, \
             mock.patch.object(docker_image_updates, 'capture') as capture, \
             mock.patch.object(db, 'set_check_result') as save:
            for theme in ('light', 'dark', 'colour'):
                w.setStyleSheet((themes / (theme + '.qss')).read_text(encoding='utf-8'))
                self.app.processEvents()
                parent.child(0, 0).setCheckState(CHECKED)
                self.assert_docker_buttons(w, True)
                QtTest.QTest.mouseClick(toolbar.widgetForAction(w.act_docker_preview), QtCore.Qt.MouseButton.LeftButton)
                QtTest.QTest.mouseClick(toolbar.widgetForAction(w.act_docker_update), QtCore.Qt.MouseButton.LeftButton)
                w.act_docker_update.trigger()
                self.assert_docker_buttons(w, True)
                parent.child(0, 0).setCheckState(UNCHECKED)
                self.assert_docker_buttons(w, False)
            remote.assert_not_called()
            capture.assert_not_called()
            save.assert_not_called()

    def test_children_columns_indentation_and_all_image_states(self):
        w = self.window()
        statuses = ['✓ aktuell', '↑ 1 Image-Update', '⚠ Prüfung unvollständig']
        parent = self.show_projects(w, [self.project(str(i), s) for i, s in enumerate(statuses)])
        self.assertEqual(parent.rowCount(), 3)
        self.assertIsInstance(w.table, QtWidgets.QTreeView)
        self.assertEqual(w.table.treePosition(), 1)
        self.assertGreater(w.table.indentation(), 0)
        self.assertTrue(w.table.isExpanded(parent.index()))
        for i, status in enumerate(statuses):
            self.assertEqual(parent.child(i, 0).checkState(), UNCHECKED)
            self.assertEqual(parent.child(i, 1).text(), str(i))
            self.assertEqual(parent.child(i, 5).text(), 'running(1)')
            self.assertEqual(parent.child(i, 6).text(), status)
            self.assertEqual(parent.child(i, 0).index().parent(), parent.index())
            for column in (2, 3, 4, 7):
                self.assertEqual(parent.child(i, column).text(), '')

    def test_unchecked_missing_and_error_have_no_children(self):
        w = self.window()
        self.assertEqual(w.table.model().item(0, 0).rowCount(), 0)
        for status in ('docker_missing', 'permission_denied', 'invalid_output'):
            data = self.discovery(status)
            data['projects'] = [self.project('should-not-appear')]
            self.result(w, data)
            parent = w.table.model().item(0, 0)
            self.assertFalse(w.table.model().hasChildren(parent.index()))

    def test_native_expander_click_and_selection_are_local_only(self):
        w = self.window()
        parent = self.show_projects(w, [self.project('app')])
        w.show()
        self.app.processEvents()
        with mock.patch.object(ssh_client, 'check_updates_for_host', side_effect=AssertionError('SSH')), \
             mock.patch.object(docker_image_updates, 'check_projects', side_effect=AssertionError('Docker')), \
             mock.patch.object(db, 'set_check_result', side_effect=AssertionError('persist')):
            name_index = w.table.model().index(0, 1)
            rect = w.table.visualRect(name_index)
            point = QtCore.QPoint(rect.left() - w.table.indentation() // 2, rect.center().y())
            QtTest.QTest.mouseClick(w.table.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=point)
            self.assertFalse(w.table.isExpanded(parent.index()))
            self.assertFalse(w.table.visualRect(parent.child(0, 1).index()).isValid())
            QtTest.QTest.mouseClick(w.table.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=point)
            self.assertTrue(w.table.isExpanded(parent.index()))
            parent.child(0, 0).setCheckState(CHECKED)
            self.assertTrue(w.table.visualRect(parent.child(0, 1).index()).isValid())

    def test_host_and_project_checks_and_select_all_are_independent(self):
        w = self.window()
        parent = self.show_projects(w, [self.project('a'), self.project('b')])
        parent.child(0, 0).setCheckState(CHECKED)
        self.assertEqual(w._get_selected_host_ids(), [])
        self.assertEqual(parent.checkState(), UNCHECKED)
        self.assertEqual(parent.child(1, 0).checkState(), UNCHECKED)
        w._set_all_checks(True)
        self.assertEqual(w._get_selected_host_ids(), [1, 2, 3])
        self.assertEqual(parent.child(1, 0).checkState(), UNCHECKED)
        w._set_all_checks(False)
        self.assertEqual(parent.child(0, 0).checkState(), CHECKED)

    def test_refresh_retains_same_project_removes_old_and_adds_unchecked(self):
        w = self.window()
        parent = self.show_projects(w, [self.project('a'), self.project('b')])
        for i in range(2):
            parent.child(i, 0).setCheckState(CHECKED)
        parent = self.show_projects(w, [self.project('b', '↑ 1 Image-Update'), self.project('c')])
        self.assertEqual(parent.rowCount(), 2)
        self.assertEqual(parent.child(0, 1).text(), 'b')
        self.assertEqual(parent.child(0, 0).checkState(), CHECKED)
        self.assertEqual(parent.child(0, 6).text(), '↑ 1 Image-Update')
        self.assertEqual(parent.child(1, 0).checkState(), UNCHECKED)
        parent = self.show_projects(w, [self.project('a')])
        self.assertEqual(parent.child(0, 0).checkState(), UNCHECKED)

    def test_new_check_clears_rows_then_restores_only_same_project_choice(self):
        w = self.window()
        parent = self.show_projects(w, [self.project('a')])
        parent.child(0, 0).setCheckState(CHECKED)
        parent.setCheckState(CHECKED)
        with mock.patch.object(w, '_prepare_passwords', return_value=True), \
             mock.patch.object(ui_main._CheckWorker, 'start'):
            w._on_check()
        self.assertEqual(parent.rowCount(), 0)
        self.assert_docker_buttons(w, False)
        parent = self.show_projects(w, [self.project('a')])
        self.assert_docker_buttons(w, True)
        self.assertEqual(parent.child(0, 0).checkState(), CHECKED)

    def test_reload_preserves_selection_expansion_and_invalidates_connection(self):
        w = self.window()
        parent = self.show_projects(w, [self.project('a')])
        parent.child(0, 0).setCheckState(CHECKED)
        w.table.collapse(parent.index())
        with mock.patch.object(db, 'list_hosts', return_value=self.hosts):
            w._reload_hosts()
        parent = w.table.model().item(0, 0)
        self.assertEqual(parent.child(0, 0).checkState(), CHECKED)
        self.assertFalse(w.table.isExpanded(parent.index()))
        self.hosts[0]['user'] = 'other'
        with mock.patch.object(db, 'list_hosts', return_value=self.hosts):
            w._reload_hosts()
        self.assertEqual(w.table.model().item(0, 0).rowCount(), 0)
        parent = self.show_projects(w, [self.project('a')])
        self.assertEqual(parent.child(0, 0).checkState(), UNCHECKED)

    def test_multiple_hosts_sort_and_details_remain_attached(self):
        w = self.window()
        a = self.show_projects(w, [self.project('a')], 1)
        b = self.show_projects(w, [self.project('b')], 2)
        a.child(0, 0).setCheckState(CHECKED)
        w.table.collapse(a.index())
        w.table.setSortingEnabled(True)
        w.table.sortByColumn(1, QtCore.Qt.SortOrder.DescendingOrder)
        for host, name in ((1, 'a'), (2, 'b')):
            row = w._find_row_by_host_id(host)
            parent = w.table.model().item(row, 0)
            self.assertEqual(parent.child(0, 1).text(), name)
            self.assertEqual(parent.child(0, 0).checkState(), CHECKED if host == 1 else UNCHECKED)
            self.assertEqual(w.table.isExpanded(parent.index()), host == 2)
        with mock.patch.object(ui_main, 'DockerDetailsDialog') as dialog:
            w._open_docker_details(w.table.model().index(w._find_row_by_host_id(2), 6))
            self.assertEqual(dialog.call_args.args[1], 'host-2')
            dialog.return_value.exec.assert_called_once()
            w._open_docker_details(b.child(0, 6).index())
            self.assertEqual(dialog.call_count, 1)
        self.show_projects(w, [self.project('a', '↑ 1 Image-Update')], 1)
        self.assertEqual(w.table.model().item(w._find_row_by_host_id(1), 0).child(0, 6).text(), '↑ 1 Image-Update')

    def test_rendered_grid_and_host_group_backgrounds_in_all_themes(self):
        w = self.window()
        self.show_projects(w, [self.project('a'), self.project('b')], 1)
        self.show_projects(w, [self.project('c')], 2)
        w.show()
        themes = Path(ui_main.__file__).parent / 'assets' / 'qss'
        for theme in ('light', 'dark', 'colour'):
            with self.subTest(theme=theme):
                w.setStyleSheet((themes / (theme + '.qss')).read_text(encoding='utf-8'))
                self.app.processEvents()
                table = w.table
                model = table.model()
                picture = table.viewport().grab().toImage()
                scale = picture.devicePixelRatio()
                def pixel(x, y):
                    return picture.pixelColor(round(x * scale), round(y * scale))
                def background(index):
                    rect = table.visualRect(index)
                    return pixel(rect.right() - 4, rect.bottom() - 4)
                colors = []
                for row in range(3):
                    parent = model.item(row, 0)
                    colors.append(background(model.index(row, 2)))
                    for i in range(parent.rowCount()):
                        child = parent.child(i, 2).index()
                        self.assertEqual(background(child), colors[-1])
                    for index in [model.index(row, 2)] + [parent.child(i, 2).index() for i in range(parent.rowCount())]:
                        rect = table.visualRect(index)
                        self.assertEqual(pixel(rect.right(), rect.center().y()), table.gridColor)
                        self.assertEqual(pixel(rect.center().x(), rect.bottom()), table.gridColor)
                    # Horizontal lines also cross the expander/indentation area.
                    rect = table.visualRect(model.index(row, 1))
                    self.assertEqual(pixel(rect.left() - 4, rect.bottom()), table.gridColor)
                self.assertEqual(colors[0], colors[2])
                self.assertNotEqual(colors[0], colors[1])
                self.assertLessEqual(max(abs(a-b) for a,b in zip(colors[0].getRgb()[:3], colors[1].getRgb()[:3])), 8)
                table.collapse(model.index(0, 0))
                self.app.processEvents()
                picture = table.viewport().grab().toImage()
                self.assertEqual(background(model.index(1, 2)), colors[1])
                table.expand(model.index(0, 0))
                with mock.patch.object(db, 'list_hosts', return_value=self.hosts):
                    w._reload_hosts()
                model = table.model()
                self.app.processEvents()
                picture = table.viewport().grab().toImage()
                self.assertEqual(background(model.item(1, 0).child(0, 2).index()), colors[1])
                self.show_projects(w, [self.project('new', '↑ 1 Image-Update')], 2)
                table.sortByColumn(1, QtCore.Qt.SortOrder.DescendingOrder)
                self.app.processEvents()
                picture = table.viewport().grab().toImage()
                for row in range(3):
                    parent = model.item(row, 0)
                    expected = background(model.index(row, 2))
                    self.assertEqual(expected, colors[row])
                    for child in range(parent.rowCount()):
                        self.assertEqual(background(parent.child(child, 2).index()), expected)
                table.sortByColumn(1, QtCore.Qt.SortOrder.AscendingOrder)

    def test_config_dialog_and_deleted_hosts(self):
        from sshupdater import ui_config
        w = self.window()
        parent = self.show_projects(w, [self.project('a')])
        parent.child(0, 0).setCheckState(CHECKED)
        with mock.patch.object(ui_config, 'ConfigDialog'), \
             mock.patch.object(db, 'list_hosts', return_value=self.hosts):
            w._open_config()
        self.assertEqual(w.table.model().item(w._find_row_by_host_id(1), 0).child(0, 0).checkState(), CHECKED)
        with mock.patch.object(db, 'list_hosts', return_value=self.hosts[1:]):
            w._reload_hosts()
        self.assertNotIn(1, w.table._project_selection)
        self.assertNotIn(1, w.table._expanded_hosts)

    def test_new_window_and_changed_project_origin_start_unselected(self):
        w = self.window()
        parent = self.show_projects(w, [self.project('a')])
        parent.child(0, 0).setCheckState(CHECKED)
        changed = self.project('a')
        changed['config_files'] = ['/different/compose.yaml']
        self.assertEqual(self.show_projects(w, [changed]).child(0, 0).checkState(), UNCHECKED)
        other = self.window()
        self.assertEqual(self.show_projects(other, [self.project('a')]).child(0, 0).checkState(), UNCHECKED)
