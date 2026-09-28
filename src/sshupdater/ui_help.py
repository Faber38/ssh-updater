"""Offline help shell. Navigation and Markdown content live in packaged JSON."""
import json
from PyQt6 import QtCore, QtWidgets
from .ui_resources import resource_path


class LocalHelpBrowser(QtWidgets.QTextBrowser):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)

    def loadResource(self, resource_type, url):
        # Phase 1 is text-only; never resolve remote or external file resources.
        return None


class HelpDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('helpDialog')
        self.setWindowTitle('SSH Updater – Hilfe')
        self.resize(900, 620)
        self.setMinimumSize(600, 380)
        layout = QtWidgets.QVBoxLayout(self)
        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.navigation = QtWidgets.QTreeWidget()
        self.navigation.setObjectName('helpNavigation')
        self.navigation.setHeaderHidden(True)
        self.navigation.setAccessibleName('Hilfethemen')
        self.navigation.setMinimumWidth(180)
        self.content = LocalHelpBrowser()
        self.content.setObjectName('helpContent')
        self.content.setAccessibleName('Hilfetext')
        self.splitter.addWidget(self.navigation)
        self.splitter.addWidget(self.content)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([235, 645])
        layout.addWidget(self.splitter)
        self.buttons = QtWidgets.QDialogButtonBox()
        self.buttons.addButton('Schließen', QtWidgets.QDialogButtonBox.ButtonRole.RejectRole)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.topics = {}
        try:
            topics = json.loads(resource_path('assets', 'help', 'topics.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            topics = [dict(id='getting-started', title='Erste Schritte',
                           body='Die lokale Hilferessource konnte nicht geladen werden.')]
        self._add_topics(self.navigation.invisibleRootItem(), topics)
        self.navigation.currentItemChanged.connect(self._show_topic)
        self.navigation.setCurrentItem(self.navigation.topLevelItem(0))

    def _add_topics(self, parent, topics):
        for topic in topics:
            self.topics[topic['id']] = topic
            item = QtWidgets.QTreeWidgetItem(parent, [topic['title']])
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, topic['id'])
            self._add_topics(item, topic.get('children', []))

    def _show_topic(self, current, previous=None):
        if current is None:
            return
        topic = self.topics[current.data(0, QtCore.Qt.ItemDataRole.UserRole)]
        self.content.setMarkdown('# ' + topic['title'] + '\n\n' + topic['body'])
        self.content.verticalScrollBar().setValue(0)
