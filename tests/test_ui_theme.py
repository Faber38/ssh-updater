from pathlib import Path
import tempfile
from unittest import mock
import unittest
from PyQt6 import QtCore, QtGui, QtWidgets, QtTest
import test_docker_project_rows as fixtures
from sshupdater import app as entry, ui_main, ui_config, ui_theme, ui_resources
from sshupdater.core import settings, db, ssh_client
from sshupdater.ui_docker import DockerDetailsDialog
from sshupdater.ui_docker_preview import DockerUpdatePreviewDialog


class ThemeTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.ProjectRowTests.setUpClass.__func__)
    window = fixtures.ProjectRowTests.window
    result = fixtures.ProjectRowTests.result
    discovery = fixtures.ProjectRowTests.discovery
    project = fixtures.ProjectRowTests.project
    show_projects = fixtures.ProjectRowTests.show_projects

    def setUp(self):
        fixtures.ProjectRowTests.setUp(self)
        self.addCleanup(self.app.setStyleSheet, self.app.styleSheet())
        self.enterContext(mock.patch.object(settings, 'THEME', 'light'))
        self.enterContext(mock.patch.object(ssh_client, 'connect_host', side_effect=AssertionError('remote')))

    def start_saved(self, theme):
        self.settings.setValue('ui/theme', theme)
        self.app.setStyleSheet('')
        windows = []
        def create():
            self.assertEqual(self.app.styleSheet(), ui_resources.resource_path('assets','qss',ui_theme.normalize_theme(theme)+'.qss').read_text(encoding="utf-8"))
            windows.append(self.window())
            return windows[-1]
        with mock.patch.object(entry.QtWidgets,'QApplication',return_value=self.app), \
             mock.patch.object(entry.settings,'initialize'), \
             mock.patch.object(entry.crypto,'keystore_exists',return_value=True), \
             mock.patch.object(entry.crypto,'set_master_password'), \
             mock.patch.object(entry.QInputDialog,'getText',return_value=('test',True)), \
             mock.patch.object(entry.db,'init_db'), \
             mock.patch.object(entry,'MainWindow',side_effect=create), \
             mock.patch.object(entry,'apply_theme',wraps=ui_theme.apply_theme) as apply, \
             mock.patch.object(ui_config.ConfigDialog,'__init__',side_effect=AssertionError('configuration opened')), \
             mock.patch.object(self.app,'exec',return_value=0):
            self.assertEqual(entry.main(),0)
            apply.assert_called_once_with(self.app)
        self.app.processEvents()
        return windows[-1]

    def test_saved_themes_applied_before_window_without_configuration(self):
        for theme, grid in (('colour','#d6c1a6'),('dark','#333333'),('light','#dddddd')):
            with self.subTest(theme=theme):
                w=self.start_saved(theme)
                self.assertEqual(w.styleSheet(),'')  # No conflicting per-window stylesheet.
                self.assertEqual(w.table.gridColor.name(),grid)
                self.assertIn('QTreeView#dockerHostTable',self.app.styleSheet())
                self.assertIn('QToolBar',self.app.styleSheet())
                detail=DockerDetailsDialog(w,'test',self.discovery())
                preview=DockerUpdatePreviewDialog(w,[])
                for dialog in (detail,preview):
                    dialog.show(); dialog.ensurePolished()
                    self.assertEqual(dialog.styleSheet(),'')
                    self.assertEqual(dialog.style().metaObject().className(),w.style().metaObject().className())
                    dialog.close()
                w.close()

    def test_color_alias_and_legacy_fallback(self):
        w=self.start_saved('Color')
        self.assertEqual(w.table.gridColor.name(),'#d6c1a6')
        self.settings.remove('ui/theme')
        settings.THEME='dark'
        self.assertEqual(ui_theme.saved_theme(),'dark')

    def test_configuration_uses_same_application_theme_function(self):
        w=self.start_saved('colour')
        before=self.app.styleSheet()
        with mock.patch.object(db,'list_hosts',return_value=self.hosts), \
             mock.patch.object(ui_config.storage,'write_private'), \
             mock.patch.object(ui_theme,'apply_theme',wraps=ui_theme.apply_theme) as apply:
            dialog=ui_config.ConfigDialog(w)
            self.assertEqual(self.app.styleSheet(),before)
            for index,name in ((0,'light'),(1,'dark'),(3,'colour')):
                dialog.cmb_theme.setCurrentIndex(index)
                self.app.processEvents()
                self.assertEqual(ui_theme.saved_theme(),name)
                self.assertEqual(self.app.styleSheet(),ui_resources.resource_path('assets','qss',name+'.qss').read_text(encoding="utf-8"))
            self.assertGreaterEqual(apply.call_count,4)
            dialog.close()

    def test_grid_and_group_backgrounds_are_rendered_on_start(self):
        w=self.start_saved('colour')
        self.show_projects(w,[self.project('child-a')],1)
        self.show_projects(w,[self.project('child-b')],2)
        w.show(); self.app.processEvents()
        table=w.table; model=table.model()
        picture=table.viewport().grab().toImage(); scale=picture.devicePixelRatio()
        def pixel(x,y): return picture.pixelColor(round(x*scale),round(y*scale))
        backgrounds=[]
        for row in range(3):
            parent=model.item(row,0)
            rect=table.visualRect(model.index(row,2))
            background=pixel(rect.right()-4,rect.bottom()-4)
            backgrounds.append(background)
            for index in [model.index(row,2)]+[parent.child(i,2).index() for i in range(parent.rowCount())]:
                r=table.visualRect(index)
                self.assertEqual(pixel(r.right(),r.center().y()),table.gridColor)
                self.assertEqual(pixel(r.center().x(),r.bottom()),table.gridColor)
                self.assertEqual(pixel(r.right()-4,r.bottom()-4),background)
        self.assertEqual(backgrounds[0],backgrounds[2])
        self.assertNotEqual(backgrounds[0],backgrounds[1])

    def test_passive_toolbar_slot_and_font_independent_labels_all_themes(self):
        for theme in ('colour','light','dark'):
            w=self.start_saved(theme)
            toolbar=w.findChild(QtWidgets.QToolBar)
            actions=toolbar.actions()
            self.assertEqual(actions.index(w.act_docker_mark),actions.index(w.act_toggle_checks)+1)
            self.assertEqual(actions.index(w.act_docker_preview),actions.index(w.act_docker_mark)+1)
            self.assertLess(actions.index(w.act_docker_update),actions.index(w.act_stop))
            self.assertIsInstance(w.docker_toolbar_mark,QtWidgets.QLabel)
            self.assertEqual(w.docker_toolbar_mark.text(),'')
            self.assertEqual(w.docker_toolbar_mark.size(),QtCore.QSize(26,22))
            self.assertNotIn('🐳',' '.join(a.text() for a in actions))
            with mock.patch.object(w,'_start_docker_preflight',side_effect=AssertionError('action')):
                QtTest.QTest.mouseClick(w.docker_toolbar_mark,QtCore.Qt.MouseButton.LeftButton)
            self.assertFalse(w.act_docker_preview.isEnabled())
            self.assertFalse(w.act_docker_update.isEnabled())
            w.close()

    def test_missing_and_valid_svg_resource(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'test.svg'
            with mock.patch.object(ui_resources,'resource_path',return_value=path):
                missing=ui_resources.DockerToolbarMark()
                self.assertTrue(missing.pixmap().isNull())
                # Original geometric test fixture, not Docker artwork.
                path.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><rect width="20" height="20" fill="black"/></svg>')
                present=ui_resources.DockerToolbarMark()
                self.assertFalse(present.pixmap().isNull())
                self.assertEqual(present.pixmap().deviceIndependentSize(),QtCore.QSizeF(20,20))
                missing.close(); present.close()

    def test_original_container_svg_has_three_shapes_and_no_font_or_external_resource(self):
        import xml.etree.ElementTree as ET
        path=ui_resources.resource_path('assets','icons','container.svg')
        root=ET.fromstring(path.read_text(encoding="utf-8"))
        ns={'svg':'http://www.w3.org/2000/svg'}
        self.assertEqual(len(root.findall('.//svg:rect',ns)),3)
        self.assertTrue(root.findall('.//svg:path',ns))
        for tag in ('text','image','foreignObject','use','script'):
            self.assertFalse(root.findall('.//svg:'+tag,ns))
        self.assertNotIn('🐳',path.read_text(encoding="utf-8"))
        self.assertIn('Eigenes SSH-Updater-Container-Symbol; keine Docker-Marke',path.with_name('README.md').read_text(encoding="utf-8"))

    def test_container_group_painting_and_dynamic_labels_follow_theme(self):
        w=self.start_saved('colour')
        toolbar=w.findChild(ui_resources.ContainerToolBar)
        mark=w.docker_toolbar_mark
        self.assertFalse(mark.pixmap().isNull())
        self.assertEqual(mark.toolTip(),'Container / Docker')
        self.assertEqual(toolbar.container_actions,(w.act_docker_mark,w.act_docker_preview,w.act_docker_update))
        seen=[]
        for theme in ('colour','light','dark'):
            # Same central entry point used by the configuration dialog.
            ui_theme.apply_theme(self.app,theme)
            self.app.processEvents()
            for label in ('Docker-Update','Docker anwenden','Docker prüfen'):
                w.act_docker_update.setText(label)
                self.app.processEvents()
                group=toolbar.container_group_rect()
                for action in (w.act_docker_mark,w.act_docker_preview,w.act_docker_update):
                    self.assertTrue(group.contains(toolbar.widgetForAction(action).geometry()))
                for action in (w.act_toggle_checks,w.act_stop):
                    self.assertFalse(group.intersects(toolbar.widgetForAction(action).geometry()))
                self.assertLess(mark.x(),toolbar.widgetForAction(w.act_docker_preview).x())
                painted=toolbar.grab().toImage()
                actions=toolbar.container_actions
                toolbar.container_actions=()
                plain=toolbar.grab().toImage()
                toolbar.container_actions=actions
                toolbar.update()
                scale=painted.devicePixelRatio()
                x,y=mark.x()+1,mark.geometry().center().y()
                tinted=painted.pixelColor(round(x*scale),round(y*scale))
                base=plain.pixelColor(round(x*scale),round(y*scale))
                delta=max(abs(a-b) for a,b in zip(tinted.getRgb()[:3],base.getRgb()[:3]))
                self.assertGreater(delta,0,(theme,label,tinted.name(),base.name()))
                self.assertLessEqual(delta,20)
                for action in (w.act_toggle_checks,w.act_stop):
                    point=toolbar.widgetForAction(action).geometry().center()
                    self.assertEqual(painted.pixelColor(round(point.x()*scale),round(point.y()*scale)),
                                     plain.pixelColor(round(point.x()*scale),round(point.y()*scale)))
            seen.append(tinted.name())
        self.assertEqual(len(set(seen)),3)
