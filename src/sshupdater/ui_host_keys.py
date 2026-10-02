"""Credential-free endpoint inspection and explicit, independently checked trust."""
import asyncio
from dataclasses import dataclass
from PyQt6 import QtCore, QtWidgets
from .core import credentials, db, host_keys, ssh_connection
from .ui_text import PlainMessageBox


@dataclass(frozen=True)
class Target:
    host_ids: tuple
    names: tuple
    address: str
    port: int
    error: str = ''

    @property
    def endpoint(self):
        if self.error:
            return f'{self.address}:{self.port}'
        return host_keys.endpoint(self.address, self.port)

    @property
    def label(self):
        return ', '.join(self.names)


def inspection_targets(hosts, selected_ids):
    """Select actual rows, or missing pins; group shared endpoint trust decisions."""
    pins = host_keys.load()
    selected = set(selected_ids)
    groups = {}
    targets = []
    for host in hosts:
        if selected and host['id'] not in selected:
            continue
        try:
            address, _, port = credentials.normalize_target(host)
            endpoint = host_keys.endpoint(address, port)
        except (ValueError, OSError) as exc:
            targets.append(Target((host['id'],), (host.get('name') or 'Host',),
                                  str(host.get('primary_ip') or ''), host.get('port'),
                                  error=str(exc)))
            continue
        groups.setdefault(endpoint, []).append((host, address, port))
    for endpoint, rows in groups.items():
        if not selected and endpoint in pins:
            continue
        targets.append(Target(tuple(row[0]['id'] for row in rows),
                              tuple(row[0].get('name') or row[1] for row in rows),
                              rows[0][1], rows[0][2]))
    return targets


def target_still_exists(target):
    for hid in target.host_ids:
        host = db.get_host(hid)
        if host is not None:
            try:
                address, _, port = credentials.normalize_target(host)
                if host_keys.endpoint(address, port) == target.endpoint:
                    return True
            except (ValueError, OSError):
                pass
    return False


def recover_truststore(parent, error):
    answer = PlainMessageBox.warning(
        parent, 'Serveridentitäten erneut bestätigen',
        f'{error}\n\nSSH bleibt bis zur Wiederherstellung blockiert. '
        'Den bisherigen Trust-Store inaktiv sichern und einen leeren '
        'authentifizierten Store anlegen? Alle Server-Fingerprints müssen '
        'anschließend unabhängig geprüft und ausdrücklich bestätigt werden. '
        'Vorhandene Pins werden nicht übernommen.',
        PlainMessageBox.StandardButton.Yes | PlainMessageBox.StandardButton.Cancel,
        PlainMessageBox.StandardButton.Cancel)
    if answer != PlainMessageBox.StandardButton.Yes:
        return False
    try:
        host_keys.reset_for_reconfirmation()
    except (OSError, ValueError) as exc:
        PlainMessageBox.critical(parent, 'Trust-Store nicht wiederhergestellt',
                                 f'{exc}\nSSH bleibt blockiert. Dateizugriff prüfen und erneut versuchen.')
        return False
    return True


@dataclass
class ProbeResult:
    target: Target
    observation: object = None
    error: str = ''


class _ProbeWorker(QtCore.QThread):
    progress = QtCore.pyqtSignal(int, int)

    def __init__(self, targets, parent=None):
        super().__init__(parent)
        self.targets = tuple(targets)
        self.results = []
        self.integrity_error = None
        self.cancelled = False

    def run(self):
        async def probe_all():
            for target in self.targets:
                if self.isInterruptionRequested():
                    self.cancelled = True
                    break
                if target.error:
                    self.results.append(ProbeResult(target, error=target.error))
                    self.progress.emit(len(self.results), len(self.targets))
                    continue
                try:
                    # No encrypted credential, key path or authentication settings
                    # are passed into the inspection transport.
                    observation = await asyncio.wait_for(ssh_connection.inspect_host(
                        dict(primary_ip=target.address, port=target.port, user='root')), 20)
                    self.results.append(ProbeResult(target, observation))
                except host_keys.TruststoreIntegrityError as exc:
                    self.integrity_error = exc
                    break
                except TimeoutError:
                    self.results.append(ProbeResult(target, error='Zeitüberschreitung.'))
                except Exception as exc:
                    self.results.append(ProbeResult(target, error=str(exc)))
                self.progress.emit(len(self.results), len(self.targets))
            if self.isInterruptionRequested():
                self.cancelled = True
        asyncio.run(probe_all())


class HostKeyDialog(QtWidgets.QDialog):
    def __init__(self, targets, parent=None):
        super().__init__(parent)
        self.targets = tuple(targets)
        self._cancel_requested = False
        self.worker = None
        self.results = []
        self.setWindowTitle('Serveridentitäten prüfen')
        self.resize(1050, 500)
        layout = QtWidgets.QVBoxLayout(self)
        hint = QtWidgets.QLabel(
            'Die Prüfung meldet sich nicht an und erzeugt kein Vertrauen. '
            'Nur Fingerprints auswählen, die Sie unabhängig verglichen haben. '
            'Hosteinträge mit derselben Adresse und demselben Port teilen einen Pin. '
            'Eine Zeile auswählen, um vollständige Fingerprints unten zu kopieren.')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.progress = QtWidgets.QLabel('Prüfung wird vorbereitet …')
        layout.addWidget(self.progress)
        self.table = QtWidgets.QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels([
            'Unabhängig verglichen', 'Host(s)', 'Adresse/Port', 'Status',
            'Neuer Fingerprint', 'Bisheriger Fingerprint'])
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(False)
        layout.addWidget(self.table)
        self.details = QtWidgets.QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(135)
        self.details.setPlaceholderText('Zeile für vollständige Fingerprints auswählen.')
        layout.addWidget(self.details)
        self.table.currentCellChanged.connect(self._show_details)
        buttons = QtWidgets.QHBoxLayout()
        self.trust = QtWidgets.QPushButton('Ausgewählte Fingerprints bestätigen')
        self.close_button = QtWidgets.QPushButton('Abbrechen')
        self.trust.setAutoDefault(False)
        self.close_button.setDefault(True)
        self.trust.setEnabled(False)
        buttons.addWidget(self.trust)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        self.trust.clicked.connect(self._trust)
        self.close_button.clicked.connect(self.reject)
        self.table.itemChanged.connect(self._update_trust)
        QtCore.QTimer.singleShot(0, self._probe)

    def _probe(self):
        if self._cancel_requested:
            return
        self.worker = _ProbeWorker(self.targets, self)
        self.worker.progress.connect(lambda done, total:
                                     self.progress.setText(f'Prüfung: {done}/{total}'))
        self.worker.finished.connect(self._received)
        self.worker.start()

    def _received(self):
        if self._cancel_requested or self.worker.cancelled:
            self.results = []
            super().reject()
            return
        if self.worker.integrity_error is not None:
            self.results = []
            recover_truststore(self, self.worker.integrity_error)
            super().reject()
            return
        self.results = self.worker.results
        self.table.setRowCount(len(self.results))
        for row, result in enumerate(self.results):
            item = result.observation
            selectable = item is not None and item.previous != item.key
            checkbox = QtWidgets.QTableWidgetItem('')
            checkbox.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
            if selectable:
                checkbox.setFlags(checkbox.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                checkbox.setCheckState(QtCore.Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, checkbox)
            status = ('Geändert' if item.changed else
                      'Bereits bestätigt' if item.previous else 'Neu') if item else 'Fehler: ' + result.error
            values = [result.target.label, result.target.endpoint, status,
                      item.fingerprint if item else '—',
                      item.previous.get_fingerprint('sha256') if item and item.previous else '—']
            for col, value in enumerate(values, 1):
                cell = QtWidgets.QTableWidgetItem(value)
                cell.setToolTip(value)
                self.table.setItem(row, col, cell)
        self.progress.setText(f'{len(self.results)}/{len(self.targets)} geprüft. Noch kein neues Vertrauen gespeichert.')
        self.close_button.setText('Schließen')
        self.table.resizeColumnsToContents()
        if self.results:
            self.table.setCurrentCell(0, 1)
        self._update_trust()

    def _show_details(self, row, *_):
        if not 0 <= row < len(self.results):
            self.details.clear()
            return
        result = self.results[row]
        item = result.observation
        lines = [result.target.label, result.target.endpoint]
        if item is None:
            lines.append('Fehler: ' + result.error)
        else:
            lines.append('Neuer Fingerprint: ' + item.fingerprint)
            old = item.previous.get_fingerprint('sha256') if item.previous else 'Keiner'
            lines.append('Bisheriger Fingerprint: ' + old)
        self.details.setPlainText('\n'.join(lines))

    def _update_trust(self, *_):
        self.trust.setEnabled(any(
            self.table.item(row, 0) is not None and
            self.table.item(row, 0).checkState() == QtCore.Qt.CheckState.Checked
            for row in range(self.table.rowCount())))

    def _trust(self):
        for row, result in enumerate(self.results):
            checkbox = self.table.item(row, 0)
            if checkbox.checkState() != QtCore.Qt.CheckState.Checked:
                continue
            item = result.observation
            if item is None or item.previous == item.key:
                continue
            if item.changed:
                answer = PlainMessageBox.warning(
                    self, 'Geänderten Server-Key ersetzen?',
                    f'{result.target.label}\n{result.target.endpoint}\n'
                    f'Bisher: {item.previous.get_fingerprint("sha256")}\nNeu: {item.fingerprint}\n\n'
                    'Ein Wechsel kann auf eine Neuinstallation oder einen Angriff hinweisen. '
                    'Den unabhängig geprüften neuen Schlüssel wirklich übernehmen?',
                    PlainMessageBox.StandardButton.Yes | PlainMessageBox.StandardButton.Cancel,
                    PlainMessageBox.StandardButton.Cancel)
                if answer != PlainMessageBox.StandardButton.Yes:
                    checkbox.setCheckState(QtCore.Qt.CheckState.Unchecked)
                    continue
            try:
                if not target_still_exists(result.target):
                    raise OSError('Ergebnis veraltet: Host/Ziel geändert oder gelöscht. Bitte erneut prüfen.')
                host_keys.confirm(item)
            except host_keys.TruststoreIntegrityError as exc:
                recover_truststore(self, exc)
                self.accept()
                return
            except (OSError, ValueError, db.sqlite3.Error) as exc:
                self.table.item(row, 3).setText(str(exc))
                # A failed MAC write can leave an invalid pair. Offer recovery now.
                try:
                    host_keys.load()
                except (OSError, ValueError) as integrity_error:
                    recover_truststore(self, integrity_error)
                    self.accept()
                    return
            else:
                self.table.item(row, 3).setText('Bestätigung gespeichert')
            checkbox.setCheckState(QtCore.Qt.CheckState.Unchecked)
            checkbox.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
        self.progress.setText('Auswahl verarbeitet. Nur ausdrücklich bestätigte Pins wurden gespeichert.')
        self._update_trust()

    def reject(self):
        self._cancel_requested = True
        if self.worker is not None and self.worker.isRunning():
            self.worker.requestInterruption()
            self.close_button.setEnabled(False)
            self.progress.setText('Abbruch angefordert; laufende Prüfung endet spätestens nach ihrem Timeout.')
            return
        super().reject()

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)
