"""A deliberately separate, explicit server-trust workflow."""
import asyncio
from PyQt6 import QtCore, QtWidgets
from .core import host_keys, ssh_connection
from .ui_text import PlainMessageBox


class _ProbeWorker(QtCore.QThread):
    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        self.observation = None
        self.error = None

    def run(self):
        async def probe():
            return await asyncio.wait_for(ssh_connection.inspect_host(self.host), 20)
        try:
            self.observation = asyncio.run(probe())
        except TimeoutError:
            self.error = "Zeitüberschreitung. Bitte Adresse und Erreichbarkeit prüfen."
        except Exception as exc:
            self.error = str(exc)


class HostKeyDialog(QtWidgets.QDialog):
    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        self.worker = None
        self.observation = None
        self.setWindowTitle("Serveridentität prüfen")
        self.resize(650, 390)
        layout = QtWidgets.QVBoxLayout(self)
        self.details = QtWidgets.QPlainTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(self.details)
        hint = QtWidgets.QLabel(
            "Vergleichen Sie den SHA256-Fingerprint mit der lokalen Konsole des Servers "
            "oder einer unabhängig geprüften Quelle. Nur dann bestätigen.\n"
            "Bei ProxyJump wird gegebenenfalls zuerst der Sprungserver angezeigt. "
            "Nach dessen Bestätigung erneut prüfen, bis das Ziel bestätigt ist.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.checked = QtWidgets.QCheckBox("Ich habe den Fingerprint unabhängig verglichen; er stimmt überein.")
        layout.addWidget(self.checked)
        buttons = QtWidgets.QHBoxLayout()
        self.probe = QtWidgets.QPushButton("Erneut prüfen")
        self.trust = QtWidgets.QPushButton("Diesem Server vertrauen")
        self.close_button = QtWidgets.QPushButton("Schließen")
        for button in (self.probe, self.trust, self.close_button):
            button.setAutoDefault(False)
            buttons.addWidget(button)
        self.close_button.setDefault(True)
        layout.addLayout(buttons)
        self.probe.clicked.connect(self._probe)
        self.trust.clicked.connect(self._trust)
        self.close_button.clicked.connect(self.reject)
        self.checked.toggled.connect(self._update_trust)
        self.trust.setEnabled(False)
        QtCore.QTimer.singleShot(0, self._probe)

    def _update_trust(self):
        self.trust.setEnabled(bool(self.observation and self.checked.isChecked()
                                  and self.observation.previous != self.observation.key))

    def _probe(self):
        self.observation = None
        self.checked.setChecked(False)
        self.trust.setEnabled(False)
        self.probe.setEnabled(False)
        self.close_button.setEnabled(False)
        self.details.setPlainText("Server-Key wird geprüft (höchstens 20 Sekunden) …")
        self.worker = _ProbeWorker(self.host, self)
        self.worker.finished.connect(self._received)
        self.worker.start()

    def _received(self):
        self.probe.setEnabled(True)
        self.close_button.setEnabled(True)
        if self.worker.error:
            self.details.setPlainText(self.worker.error)
            return
        self.observation = item = self.worker.observation
        status = "GEÄNDERT – Verbindung blockiert" if item.changed else (
            "Bereits bestätigt – Fingerprint stimmt überein" if item.previous else "Noch nicht bestätigt")
        old = item.previous.get_fingerprint('sha256') if item.previous else "Keiner"
        self.details.setPlainText(
            f"{status}\n\nAngefragter Host: {item.requested}\n"
            f"Verbindungsadresse: {item.address or item.host}:{item.port}\n"
            f"Vertrauensname / Port: {item.name}\n"
            f"Schlüsseltyp: {item.key.get_algorithm()}\n"
            f"Bisheriger Fingerprint: {old}\nNeuer Fingerprint: {item.fingerprint}")
        self.trust.setText("Geänderten Server-Key ersetzen" if item.changed else "Diesem Server vertrauen")
        self._update_trust()

    def _trust(self):
        if not self.observation or not self.checked.isChecked():
            return
        if self.observation.changed:
            answer = PlainMessageBox.warning(
                self, "Geänderten Server-Key ersetzen?",
                "Ein geänderter Schlüssel kann auf eine Neuinstallation oder einen Angriff hinweisen.\n"
                "Den unabhängig geprüften neuen Schlüssel wirklich übernehmen?",
                PlainMessageBox.StandardButton.Yes | PlainMessageBox.StandardButton.Cancel,
                PlainMessageBox.StandardButton.Cancel)
            if answer != PlainMessageBox.StandardButton.Yes:
                return
        try:
            host_keys.confirm(self.observation)
        except (OSError, ValueError) as exc:
            PlainMessageBox.critical(self, "Server-Key nicht gespeichert", str(exc))
            return
        self.observation = None
        self.trust.setEnabled(False)
        self.details.appendPlainText(
            "\nBestätigung gespeichert. Es wurde keine Update-Aktion gestartet.\n"
            "Bei ProxyJump: erneut prüfen, um auch das Ziel zu bestätigen.")

    def reject(self):
        if self.worker is not None and self.worker.isRunning():
            return
        super().reject()

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            event.ignore()
        else:
            super().closeEvent(event)
