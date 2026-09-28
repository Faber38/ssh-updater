import json
from pathlib import Path
from unittest import mock
import unittest
from PyQt6 import QtCore, QtGui, QtWidgets, QtTest
import test_ui_theme as fixtures
from sshupdater import ui_help, ui_main, ui_theme, ui_resources
from sshupdater.core import ssh_client, docker_image_updates

TOPICS = ['Erste Schritte','Hosts einrichten','SSH / SSH-Key','Prüfen','Simulieren','Upgrade',
          'Bereinigen','Reboot','Docker / Compose','Sicherheit','Fehlerbehebung','Über SSH Updater']
DOCKER = ['Übersicht','Statusanzeigen','Projekte auswählen','Update-Vorschau','Docker-Update',
          'Docker anwenden','Docker prüfen','Voraussetzungen & Grenzen']


class HelpTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.ThemeTests.setUpClass.__func__)
    setUp = fixtures.ThemeTests.setUp
    window = fixtures.ThemeTests.window
    start_saved = fixtures.ThemeTests.start_saved

    def help(self, window):
        window.act_help.trigger()
        self.app.processEvents()
        return window.findChildren(ui_help.HelpDialog)[-1]

    def test_toolbar_position_and_existing_actions_unchanged(self):
        w=self.window()
        tb=w.findChild(ui_resources.ContainerToolBar)
        actions=tb.actions()
        self.assertEqual(w.act_help.text(),'Hilfe')
        self.assertEqual(actions.index(w.act_help),actions.index(w.act_stop)+1)
        self.assertEqual(tb.container_actions,(w.act_docker_mark,w.act_docker_preview,w.act_docker_update))
        self.assertNotIn(w.act_help,tb.container_actions)
        before=[(a,a.isEnabled(),a.text()) for a in actions]
        dialog=self.help(w)
        self.assertEqual(dialog.windowTitle(),'SSH Updater – Hilfe')
        self.assertEqual([(a,a.isEnabled(),a.text()) for a in actions],before)
        self.assertFalse(w.act_stop.isEnabled())
        self.assertIsNone(w._docker_plan)

    def test_complete_navigation_initial_content_and_clicks(self):
        dialog=self.help(self.window())
        tree=dialog.navigation
        topics=json.loads(ui_resources.resource_path('assets','help','topics.json').read_text(encoding="utf-8"))
        expected_topics={topic['id']:topic for topic in topics}
        for topic in topics:
            expected_topics.update((child['id'],child) for child in topic.get('children',[]))

        def assert_topic_content(item):
            topic_id=item.data(0,QtCore.Qt.ItemDataRole.UserRole)
            topic=expected_topics[topic_id]
            self.assertEqual(item.text(0),topic['title'])
            expected=QtGui.QTextDocument()
            expected.setMarkdown('# '+topic['title']+'\n\n'+topic['body'])
            self.assertEqual(dialog.content.toPlainText(),expected.toPlainText())
            self.assertTrue(topic['body'].strip())
            if topic_id in ('getting-started','hosts','ssh'):
                self.assertNotIn('Der vollständige Hilfetext wird in der nächsten Dokumentationsphase ergänzt.',
                                 dialog.content.toPlainText())

        self.assertEqual([tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())],TOPICS)
        self.assertEqual(tree.currentItem().text(0),'Erste Schritte')
        self.assertEqual(tree.currentItem().data(0,QtCore.Qt.ItemDataRole.UserRole),'getting-started')
        assert_topic_content(tree.currentItem())
        self.assertTrue(dialog.content.isReadOnly())
        docker=tree.topLevelItem(8)
        self.assertEqual([docker.child(i).text(0) for i in range(docker.childCount())],DOCKER)
        tree.setCurrentItem(docker); tree.setFocus()
        QtTest.QTest.keyClick(tree,QtCore.Qt.Key.Key_Right)
        self.assertTrue(docker.isExpanded())
        QtTest.QTest.keyClick(tree,QtCore.Qt.Key.Key_Left)
        self.assertFalse(docker.isExpanded())
        docker.setExpanded(True)
        items=[tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]+[docker.child(i) for i in range(docker.childCount())]
        for item in items:
            tree.scrollToItem(item); self.app.processEvents()
            QtTest.QTest.mouseClick(tree.viewport(),QtCore.Qt.MouseButton.LeftButton,
                                    pos=tree.visualItemRect(item).center())
            self.assertEqual(tree.currentItem(),item)
            assert_topic_content(item)

    def test_resizable_splitter_and_german_close_and_escape(self):
        w=self.window(); dialog=self.help(w)
        before=dialog.size()
        dialog.resize(before.width()+80,before.height()+40)
        self.app.processEvents()
        self.assertNotEqual(dialog.size(),before)
        sizes=dialog.splitter.sizes()
        self.assertLess(sizes[0],sizes[1])
        dialog.splitter.setSizes([300,500]); self.app.processEvents()
        self.assertNotEqual(dialog.splitter.sizes(),sizes)
        button,=dialog.buttons.buttons()
        self.assertEqual(button.text(),'Schließen')
        QtTest.QTest.mouseClick(button,QtCore.Qt.MouseButton.LeftButton)
        self.assertFalse(dialog.isVisible())
        dialog=self.help(w)
        QtTest.QTest.keyClick(dialog,QtCore.Qt.Key.Key_Escape)
        self.assertFalse(dialog.isVisible())

    def test_local_content_and_all_navigation_require_no_network(self):
        import socket
        import asyncssh
        w=self.window()
        with mock.patch.object(socket,'create_connection',side_effect=AssertionError('network')), \
             mock.patch.object(asyncssh,'connect',side_effect=AssertionError('SSH')), \
             mock.patch.object(docker_image_updates,'capture',side_effect=AssertionError('docker')), \
             mock.patch.object(docker_image_updates.Checker,'remote',side_effect=AssertionError('registry')), \
             mock.patch.object(QtGui.QDesktopServices,'openUrl',side_effect=AssertionError('browser')):
            dialog=self.help(w)
            for index in range(dialog.navigation.topLevelItemCount()):
                item=dialog.navigation.topLevelItem(index)
                dialog.navigation.setCurrentItem(item)
                for child in range(item.childCount()):
                    dialog.navigation.setCurrentItem(item.child(child))
            self.assertFalse(dialog.content.openLinks())
            self.assertFalse(dialog.content.openExternalLinks())
            for url in ('https://example.invalid/test.png','file:///not-a-help-resource'):
                self.assertIsNone(dialog.content.loadResource(2,QtCore.QUrl(url)))
        data=json.loads(ui_resources.resource_path('assets','help','topics.json').read_text(encoding="utf-8"))
        self.assertEqual(len(data),12)
        self.assertNotIn('Der vollständige Hilfetext',Path(ui_main.__file__).read_text(encoding="utf-8"))

    def test_saved_color_and_live_theme_changes(self):
        w=self.start_saved('Color')
        dialog=self.help(w)
        self.assertEqual(dialog.styleSheet(),'')
        colours=[]
        for theme in ('colour','light','dark','colour'):
            ui_theme.apply_theme(self.app,theme)
            self.app.processEvents()
            dialog.ensurePolished()
            colours.append(dialog.content.palette().color(QtGui.QPalette.ColorRole.Text).name())
            self.assertTrue(dialog.isVisible())
            self.assertEqual(dialog.navigation.currentItem().text(0),'Erste Schritte')
        self.assertEqual(colours,['#1c1c1c','#222222','#dddddd','#1c1c1c'])
        self.assertFalse(w.act_docker_update.isEnabled())

    def test_missing_content_stays_local_and_readable(self):
        with mock.patch.object(ui_help,'resource_path',return_value=Path('/not-existing-help-topics.json')):
            dialog=self.help(self.window())
        self.assertIn('lokale Hilferessource',dialog.content.toPlainText())

    def test_finished_help_has_no_development_placeholders_in_all_themes(self):
        dialog=self.help(self.window())
        items=[dialog.navigation.topLevelItem(i) for i in range(12)]
        items += [items[8].child(i) for i in range(8)]
        self.assertEqual(len(dialog.topics),20)
        for theme in ('colour','light','dark'):
            ui_theme.apply_theme(self.app,theme)
            for item in items:
                with self.subTest(theme=theme,topic=item.text(0)):
                    dialog.navigation.setCurrentItem(item)
                    self.app.processEvents()
                    text=dialog.content.toPlainText()
                    self.assertTrue(text.startswith(item.text(0)))
                    self.assertGreater(len(text),len(item.text(0)))
                    self.assertNotRegex(text,r'(?i)Platzhalter|wird später|wird in der nächsten|Phase\s+[1-4]|TODO|FIXME')
                    self.assertFalse(dialog.grab().isNull())

    def test_startup_password_and_docker_orientation_content(self):
        dialog=self.help(self.window())
        body=dialog.topics['getting-started']['body']
        for fragment in ('Erststart','Master-Passwort','bestätigen','späteren Starts','entsperren',
                         'auch bei ausschließlicher SSH-Key-Nutzung'):
            self.assertIn(fragment,body)
        ssh=dialog.topics['ssh']['body']
        self.assertIn('bei jedem Programmstart erforderliche Entsperrung',ssh)
        self.assertNotIn('gegebenenfalls erforderliche',ssh)
        docker=dialog.topics['docker']['body']
        for child in dialog.topics['docker']['children']:
            self.assertIn(child['title'],docker)
        self.assertIn('Prüfen → Projekt auswählen',docker)
