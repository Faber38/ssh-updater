"""One application-wide theme path for startup and configuration changes."""
from PyQt6 import QtCore
from .core import settings
from .ui_resources import resource_path

STANDARD = '''
QWidget { background-color: #f0f0f0; color: #000; font-family: DejaVu Sans, Arial; font-size: 10pt; }
QPushButton { background-color: #e0e0e0; border: 1px solid #a0a0a0; padding: 4px 8px; }
QPushButton:hover { background-color: #f8f8f8; }
'''


def normalize_theme(value):
    theme = str(value or 'standard').strip().lower()
    if theme == 'color':
        theme = 'colour'
    return theme if theme in ('light', 'dark', 'colour', 'standard') else 'standard'


def saved_theme():
    value = QtCore.QSettings('Faber38', 'SSH Updater').value('ui/theme', None)
    return normalize_theme(value or settings.THEME)


def apply_theme(app, theme=None):
    theme = saved_theme() if theme is None else normalize_theme(theme)
    path = resource_path('assets', 'qss', theme + '.qss')
    stylesheet = path.read_text(encoding='utf-8') if theme != 'standard' and path.is_file() else STANDARD
    app.setStyleSheet(stylesheet)
    return theme
