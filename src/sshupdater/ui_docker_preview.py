"""Local, read-only action plan from an immutable-by-copy selection snapshot."""
from copy import deepcopy
import re
from PyQt6 import QtCore, QtWidgets
from .ui_docker import engine_version
from .docker_plan import image_identity, project_eligible
from .core.docker_image_updates import REASONS


STATUS_TEXT = {'current': '✓ aktuell', 'update_available': '↑ Image-Update',
               'uncheckable': 'Prüfung nicht möglich', 'digest_pinned': 'Digest-fixiert',
               'local_build': 'Lokaler Build'}


def project_state(project):
    images = project.get('image_updates', {}).get('images') or []
    states = [image.get('status') for image in images]
    return {
        'candidate': project_eligible(project.get('config_files'), tuple(image_identity(i) for i in images)),
        'updates': states.count('update_available'),
        'current': bool(states) and all(s == 'current' for s in states),
        'incomplete': not states or any(s not in STATUS_TEXT or s == 'uncheckable' for s in states),
        'pinned': 'digest_pinned' in states,
        'build': 'local_build' in states,
    }


def description(host, project):
    state = project_state(project)
    check = project.get('image_updates', {})
    lines = [f"Host: {host['name']}", f"Projekt: {project['name']}",
             f"Compose-Pfade: {project.get('config_files_raw') or ', '.join(project.get('config_files') or []) or '—'}",
             f"Prüfzeit: {check.get('checked_at') or 'Nicht verfügbar'}",
             f"Status: {check.get('summary') or 'Nicht geprüft'}", '']
    if state['current']:
        lines.append('Keine Aktualisierung erforderlich.')
    if state['incomplete']:
        lines += ['Updatezustand konnte nicht zuverlässig bestimmt werden.',
                  'Keine automatische Aktualisierung vorgesehen.' if not state['candidate'] else
                  'Für nicht prüfbare Images ist keine automatische Aktualisierung vorgesehen.']
    if state['pinned']:
        lines.append('Digest-fixiert – keine automatische Tag-Aktualisierung vorgesehen.')
    if state['build']:
        lines.append('Lokaler Build – keine Registry-Aktualisierung vorgesehen.')
    if state['updates'] and not state['candidate']:
        lines += [f"Erkannte Image-Updates: {state['updates']}",
                  'Projekt nicht zur Aktualisierung freigegeben: Nicht alle Services sind eindeutig prüfbar '
                  'oder die erforderlichen Freigabenachweise fehlen.']
    if state['candidate']:
        lines += ['', 'Geplanter späterer Ablauf – nur für nachgewiesene Image-Updates:',
                  '1. Neues Image für denselben konfigurierten Tag herunterladen.',
                  '2. Betroffene Compose-Services mit dem neuen Image aktualisieren und deren Container neu erstellen.',
                  '3. Laufzustand anschließend kontrollieren.',
                  '4. Image-Stand anschließend erneut prüfen.',
                  'Compose-Dateien werden dabei nicht verändert.']
    for image in check.get('images') or []:
        lines += ['', f"Service: {image.get('service') or '—'}",
                  f"Container: {image.get('container_name') or image.get('container_id') or '—'}",
                  f"Image: {image.get('image') or '—'}",
                  f"Plattform: {image.get('platform') or '—'}",
                  f"Image-Status: {STATUS_TEXT.get(image.get('status'), 'Prüfung nicht möglich')}"]
        note = image.get('note') or REASONS.get(image.get('reason'), '')
        if note:
            lines.append(f"Hinweis: {note}")
    return '\n'.join(lines)


class DockerUpdatePreviewDialog(QtWidgets.QDialog):
    def __init__(self, parent, hosts):
        super().__init__(parent)
        self.snapshot = deepcopy(hosts)
        self.setWindowTitle('Docker Update-Vorschau')
        available = self.screen().availableGeometry()
        self.resize(min(950, available.width() - 40), min(820, available.height() - 60))
        layout = QtWidgets.QVBoxLayout(self)
        notice = QtWidgets.QLabel('Es wurden keine Änderungen durchgeführt.\n'
                                'Die Vorschau basiert auf dem Stand der letzten Hostprüfung.\n'
                                'Aktionsplan – keine Simulation. Die Vorschau zeigt den geplanten Updateablauf.')
        notice.setWordWrap(True)
        layout.addWidget(notice)
        states = [project_state(p) for h in self.snapshot for p in h['projects']]
        self.counts = {key: sum(s[key] for s in states) for key in ('candidate', 'updates', 'current', 'incomplete', 'pinned', 'build')}
        self.summary = QtWidgets.QWidget()
        summary_layout = QtWidgets.QGridLayout(self.summary)
        summary_layout.setContentsMargins(0, 0, 0, 0)
        summary_layout.setHorizontalSpacing(24)
        summary_layout.setVerticalSpacing(6)
        self.summary_labels = []
        counters = [('Ausgewählte Projekte', len(states)), ('Freigabefähige Update-Projekte', self.counts['candidate']),
                    ('Erkannte Image-Updates', self.counts['updates']),
                    ('Bereits aktuell', self.counts['current']), ('Nicht zuverlässig prüfbar', self.counts['incomplete']),
                    ('Digest-fixiert', self.counts['pinned']), ('Lokaler Build', self.counts['build'])]
        for index, (title, count) in enumerate(counters):
            label = QtWidgets.QLabel(f'{title}: {count}')
            label.setWordWrap(True)
            summary_layout.addWidget(label, index // 2, index % 2)
            self.summary_labels.append(label)
        summary_layout.setColumnStretch(0, 1)
        summary_layout.setColumnStretch(1, 1)
        overlap = QtWidgets.QLabel('Bei gemischten Projekten können sich Kategorien überschneiden.')
        overlap.setWordWrap(True)
        summary_layout.addWidget(overlap, 4, 0, 1, 2)
        layout.addWidget(self.summary)
        self.projects = QtWidgets.QTreeWidget()
        self.projects.setHeaderLabels(['Host / Projekt', 'Image-Status'])
        self.projects.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.projects, 1)
        self.details = QtWidgets.QPlainTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(self.details, 5)
        first = None
        for host in self.snapshot:
            compose = re.sub(r'^\s*Docker\s+Compose\s+version\s+', '', host.get('compose_version') or '', flags=re.I)
            root = QtWidgets.QTreeWidgetItem(self.projects, [f"Host: {host['name']}",
                f"Docker Engine: {engine_version(host.get('docker_version')) or 'Unbekannt'} · Docker Compose: {compose or 'Unbekannt'}"])
            for project in host['projects']:
                item = QtWidgets.QTreeWidgetItem(root, [project['name'], project.get('image_updates', {}).get('summary') or 'Nicht geprüft'])
                item.setData(0, QtCore.Qt.ItemDataRole.UserRole, description(host, project))
                first = first or item
        self.projects.expandAll()
        self.projects.resizeColumnToContents(0)
        self.projects.currentItemChanged.connect(self._show_project)
        if first is not None:
            self.projects.setCurrentItem(first)
        self.buttons = QtWidgets.QDialogButtonBox()
        self.close_button = self.buttons.addButton('Schließen', QtWidgets.QDialogButtonBox.ButtonRole.RejectRole)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _show_project(self, item, previous):
        text = item.data(0, QtCore.Qt.ItemDataRole.UserRole) if item else ''
        self.details.setPlainText(text or '')
