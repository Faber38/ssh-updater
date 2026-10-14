"""Local diagnostic viewer and explicit UTF-8 export."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from PyQt6 import QtCore, QtWidgets
from .docker_diagnostics import report


PRIVACY_DE = (
    'Der Diagnosebericht wird ausschließlich lokal erstellt und gespeichert.\n'
    'SSH Updater überträgt keine Diagnosedaten automatisch an den Entwickler oder Dritte.\n\n'
    'Ob du den Bericht zur Fehleranalyse weitergeben möchtest, entscheidest du selbst. '
    'Bitte prüfe die Datei vor einer Weitergabe auf persönliche oder sensible Angaben.'
)
PRIVACY_EN = (
    'The diagnostic report is generated and saved locally only.\n'
    'SSH Updater does not automatically transmit diagnostic data to the developer or third parties.\n\n'
    'You decide whether to share the report for troubleshooting. '
    'Please review the file for personal or sensitive information before sharing it.'
)


def privacy_notice():
    # No app-wide language selector exists. Honour Qt's configured UI locale;
    # tr() additionally supports a translator installed by the application.
    source = PRIVACY_EN if QtCore.QLocale().language() == QtCore.QLocale.Language.English else PRIVACY_DE
    return QtCore.QCoreApplication.translate('DockerDiagnosticsDialog', source)


class DockerDiagnosticsDialog(QtWidgets.QDialog):
    def __init__(self, parent, host, project):
        super().__init__(parent)
        self.host, self.project = deepcopy(host), deepcopy(project)
        self.report_language = 'en' if QtCore.QLocale().language() == QtCore.QLocale.Language.English else 'de'
        self.setWindowTitle('Docker – Fehleranalyse')
        self.resize(850, 600)
        layout = QtWidgets.QVBoxLayout(self)
        self.project_label = QtWidgets.QLabel('Ausgewähltes Projekt: ' + str(project.get('name') or 'Unbekannt'))
        self.project_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.project_label.setWordWrap(True)
        layout.addWidget(self.project_label)
        layout.addWidget(QtWidgets.QLabel('Der Bericht verwendet vorhandene Prüfdaten und anonymisierte Kennungen.'))
        self.text = QtWidgets.QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setPlainText(report(self.host, self.project, language=self.report_language))
        layout.addWidget(self.text, 1)
        self.privacy_label = QtWidgets.QLabel(privacy_notice())
        self.privacy_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.privacy_label.setWordWrap(True)
        layout.addWidget(self.privacy_label)
        buttons = QtWidgets.QDialogButtonBox()
        self.save_button = buttons.addButton('Bericht speichern...', QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)
        self.save_button.clicked.connect(self.save_report)
        self.close_button = buttons.addButton('Schließen', QtWidgets.QDialogButtonBox.ButtonRole.RejectRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def save_report(self):
        filename = datetime.now().strftime('ssh-updater-docker-diagnose-%Y%m%d-%H%M%S.txt')
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, 'Diagnosebericht speichern', filename, 'Textdateien (*.txt)')
        if not path:
            return
        text = report(self.host, self.project, exported_at=datetime.now(timezone.utc), language=self.report_language)
        try:
            Path(path).write_text(text, encoding='utf-8')
        except (OSError, ValueError):
            QtWidgets.QMessageBox.warning(self, 'Speichern fehlgeschlagen',
                                          'Der Diagnosebericht konnte nicht gespeichert werden. Bitte Speicherort und Schreibrechte prüfen.')
            return
        self.text.setPlainText(text)
