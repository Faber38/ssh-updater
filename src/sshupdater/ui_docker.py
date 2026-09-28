"""Read-only views of Docker discovery snapshots; no remote operations."""
from copy import deepcopy
from .core.docker_compose import COMMANDS, DISCOVERY_REASONS
import re

from PyQt6 import QtCore, QtWidgets, QtGui

PROJECT_ROLE = QtCore.Qt.ItemDataRole.UserRole + 2

DETAILS_ROLE = QtCore.Qt.ItemDataRole.UserRole + 1


def engine_version(raw):
    match = re.fullmatch(
        r"\s*(?:Docker\s+version\s+)?v?(\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.-]+)*)(?:\s*,.*)?\s*",
        raw or "", re.IGNORECASE,
    )
    return match.group(1) if match else None


def details_available(result):
    return bool(result and result.get('status') != 'docker_missing' and (
        result.get('docker_available') is True
        or result.get('status') in ('ok', 'compose_missing')
        or result.get('note') or result.get('command')
        or result.get('exit_code') is not None
    ))


class DockerHostTable(QtWidgets.QTreeView):
    docker_clicked = QtCore.pyqtSignal(QtCore.QModelIndex)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("dockerHostTable")
        self._grid_color = QtGui.QColor()
        self.setMouseTracking(True)
        self.setTreePosition(1)
        self.setIndentation(18)
        self.setUniformRowHeights(True)
        self.setExpandsOnDoubleClick(False)
        self._project_selection = {}
        self._expanded_hosts = {}
        self.expanded.connect(lambda index: self._remember_expansion(index, True))
        self.collapsed.connect(lambda index: self._remember_expansion(index, False))
        self._docker_press = QtCore.QPersistentModelIndex()

    def _set_grid_color(self, color):
        self._grid_color = color
        self.viewport().update()

    gridColor = QtCore.pyqtProperty(QtGui.QColor, lambda self: self._grid_color,
                                   _set_grid_color)

    def drawRow(self, painter, option, index):
        root = index
        while root.parent().isValid():
            root = root.parent()
        option = QtWidgets.QStyleOptionViewItem(option)
        base = self.palette().color(QtGui.QPalette.ColorRole.Base)
        ink = self.palette().color(QtGui.QPalette.ColorRole.Text)
        if root.row() % 2:
            base = QtGui.QColor(*(round(a * .97 + b * .03) for a, b in zip(
                (base.red(), base.green(), base.blue()),
                (ink.red(), ink.green(), ink.blue()))))
        # Force a row background from the HOST ordinal, not visible tree depth.
        option.features |= QtWidgets.QStyleOptionViewItem.ViewItemFeature.Alternate
        option.palette.setColor(QtGui.QPalette.ColorRole.AlternateBase, base)
        painter.fillRect(option.rect, base)
        super().drawRow(painter, option, index)
        # QTreeView has no QTableView grid. Draw across the branch indentation
        # as well, using the existing theme colour or the native table style.
        color = self._grid_color
        if not color.isValid():
            hint = self.style().styleHint(QtWidgets.QStyle.StyleHint.SH_Table_GridLineColor,
                                          option, self)
            color = (option.palette.color(QtGui.QPalette.ColorRole.Mid) if hint == -1
                     else QtGui.QColor.fromRgba(hint & 0xffffffff))
        painter.save()
        painter.setPen(QtGui.QPen(color, 1))
        header = self.header()
        right = min(self.viewport().width() - 1, header.length() - header.offset() - 1)
        painter.drawLine(0, option.rect.bottom(), right, option.rect.bottom())
        for column in range(header.count()):
            if not header.isSectionHidden(column):
                x = header.sectionViewportPosition(column) + header.sectionSize(column) - 1
                painter.drawLine(x, option.rect.top(), x, option.rect.bottom())
        painter.restore()

    def _remember_expansion(self, index, expanded):
        if not index.parent().isValid():
            host_id = index.siblingAtColumn(1).data(QtCore.Qt.ItemDataRole.UserRole)
            self._expanded_hosts[host_id] = expanded

    def remember_projects(self):
        model = self.model()
        if model is None:
            return
        for row in range(model.rowCount()):
            parent = model.item(row, 0)
            host_id = model.item(row, 1).data(QtCore.Qt.ItemDataRole.UserRole)
            if parent.rowCount():
                self._project_selection[host_id] = {
                    parent.child(i, 0).data(PROJECT_ROLE): parent.child(i, 0).checkState()
                    for i in range(parent.rowCount())}

    def retain_hosts(self, host_ids):
        self._project_selection = {h: v for h, v in self._project_selection.items() if h in host_ids}
        self._expanded_hosts = {h: v for h, v in self._expanded_hosts.items() if h in host_ids}

    def populate_projects(self, host_id, parent, discovery):
        parent.removeRows(0, parent.rowCount())
        projects = discovery.get('projects', []) if discovery and discovery.get('status') == 'ok' else []
        selected = self._project_selection.get(host_id, {})
        retained = {}
        names = [p.get('name') for p in projects]
        for project in sorted(projects, key=lambda p: p.get('name') or ''):
            name = project.get('name')
            if not name or names.count(name) != 1:
                continue
            # A changed Compose origin is not assumed to be the same project.
            identity = (name, tuple(project.get('config_files') or []), project.get('config_files_raw'))
            state = selected.get(identity, QtCore.Qt.CheckState.Unchecked)
            items = [QtGui.QStandardItem(value) for value in (
                '', name, '', '', '', project.get('status') or '—',
                project.get('image_updates', {}).get('summary', 'Nicht geprüft'), '')]
            for item in items:
                item.setEditable(False)
            items[0].setCheckable(True)
            items[0].setCheckState(state)
            items[0].setData(identity, PROJECT_ROLE)
            parent.appendRow(items)
            retained[identity] = state
        self._project_selection[host_id] = retained
        if parent.rowCount():
            self.setExpanded(parent.index(), self._expanded_hosts.get(host_id, True))

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        index = self.indexAt(event.position().toPoint())
        if index.data(DETAILS_ROLE):
            self.viewport().setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        else:
            self.viewport().unsetCursor()

    def leaveEvent(self, event):
        self.viewport().unsetCursor()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        self._docker_press = QtCore.QPersistentModelIndex(
            index if event.button() == QtCore.Qt.MouseButton.LeftButton and index.data(DETAILS_ROLE)
            else QtCore.QModelIndex())
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        activate = (event.button() == QtCore.Qt.MouseButton.LeftButton
                    and index.isValid() and index == self._docker_press
                    and index.data(DETAILS_ROLE))
        self._docker_press = QtCore.QPersistentModelIndex()
        super().mouseReleaseEvent(event)
        if activate:
            self.viewport().unsetCursor()
            self.docker_clicked.emit(index)


class DockerDetailsDialog(QtWidgets.QDialog):
    def __init__(self, parent, host_name, discovery):
        super().__init__(parent)
        result = deepcopy(discovery)
        self.setWindowTitle(f'Docker – {host_name}')
        self.resize(880, 600)
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()

        def label(text):
            value = QtWidgets.QLabel(str(text))
            value.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            value.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            value.setWordWrap(True)
            return value

        raw = result.get('docker_version')
        self.engine = label(engine_version(raw) or raw or 'Unbekannt')
        compose = result.get('compose_version') or ''
        compose = re.sub(r'^\s*Docker\s+Compose\s+version\s+', '', compose, flags=re.IGNORECASE).strip()
        self.compose = label(compose or ('Nicht installiert' if result.get('compose_available') is False else 'Unbekannt'))
        status = result.get('status')
        states = {
            'ok': 'Bereit', 'compose_missing': 'Docker Compose nicht installiert',
            'permission_denied': 'Keine Berechtigung', 'daemon_unreachable': 'Docker-Daemon nicht erreichbar',
            'timeout': 'Zeitüberschreitung', 'invalid_output': 'Ungültige Docker-Ausgabe',
            'command_error': 'Docker-Befehl fehlgeschlagen',
        }
        self.status = label(states.get(status, 'Docker-Fehler'))
        form.addRow('Docker Engine:', self.engine)
        form.addRow('Docker Compose:', self.compose)
        form.addRow('Status:', self.status)
        layout.addLayout(form)
        layout.addWidget(label('Stand der letzten Hostprüfung · keine Live-Abfrage'))
        layout.addWidget(label('Compose-Projekte'))
        projects = result.get('projects') or []
        self.projects = QtWidgets.QTableWidget(len(projects), 5)
        self.projects.setHorizontalHeaderLabels(['Name', 'Status', 'Container', 'Compose-Pfade', 'Image-Status'])
        self.projects.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.projects.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.projects.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.projects.verticalHeader().hide()
        for row, project in enumerate(projects):
            paths = project.get('config_files_raw')
            if paths is None:
                paths = '\n'.join(project.get('config_files') or [])
            count = project.get('container_count')
            values = [project.get('name') or '—', project.get('status') or '—',
                      str(count) if count is not None else '—', paths or '—',
                      project.get('image_updates', {}).get('summary', 'Nicht geprüft')]
            for column, value in enumerate(values):
                self.projects.setItem(row, column, QtWidgets.QTableWidgetItem(value))
        self.projects.resizeColumnsToContents()
        self.projects.horizontalHeader().setStretchLastSection(True)
        self.projects.resizeRowsToContents()
        layout.addWidget(self.projects)
        self.projects.setVisible(bool(projects))
        self.empty = label('Keine Compose-Projekte gefunden' if status == 'ok' else 'Projektliste nicht verfügbar')
        self.empty.setVisible(not projects)
        layout.addWidget(self.empty)
        self.image_details = QtWidgets.QTableWidget(0, 5)
        self.image_details.setHorizontalHeaderLabels(['Service / Container', 'Image', 'Plattform', 'Status', 'Hinweis'])
        self.image_details.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.image_details.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.image_details.verticalHeader().hide()
        self.image_details.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(label('Images des ausgewählten Projekts · gleicher Tag, keine neueren Tags'))
        layout.addWidget(self.image_details)

        def show_images():
            row = self.projects.currentRow()
            images = projects[row].get('image_updates', {}).get('images', []) if 0 <= row < len(projects) else []
            self.image_details.setRowCount(len(images))
            states = {'current': '✓ aktuell', 'update_available': '↑ Update verfügbar',
                      'uncheckable': 'Prüfung nicht möglich', 'digest_pinned': 'Digest-fixiert',
                      'local_build': 'Lokaler Build'}
            for i, entry in enumerate(images):
                service = entry.get('service') or '—'
                if entry.get('container_name'):
                    service += '\n' + entry['container_name']
                values = [service, entry.get('image') or '—', entry.get('platform') or '—',
                          states.get(entry.get('status'), 'Prüfung nicht möglich'), entry.get('note') or '—']
                for j, value in enumerate(values):
                    self.image_details.setItem(i, j, QtWidgets.QTableWidgetItem(value))
            self.image_details.resizeColumnsToContents()
            self.image_details.resizeRowsToContents()

        self.projects.itemSelectionChanged.connect(show_images)
        if projects:
            self.projects.selectRow(0)
        self.error = QtWidgets.QPlainTextEdit()
        self.error.setReadOnly(True)
        code = result.get('exit_code')
        safe_status = status if status in DISCOVERY_REASONS or status == 'ok' else 'command_error'
        command = result.get('command')
        self.error.setPlainText(
            f"Status: {safe_status}\nBefehl: {command if command in COMMANDS else '—'}\n"
            f"Exitcode: {code if type(code) is int else '—'}\n\n"
            f"{DISCOVERY_REASONS.get(safe_status, '')}")
        self.error.setVisible(status != 'ok')
        layout.addWidget(self.error)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
