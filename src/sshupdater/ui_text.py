"""Plain text at every boundary where remote or local external text is shown."""
from PyQt6 import QtCore, QtWidgets


class PlainTextLog(QtWidgets.QPlainTextEdit):
    MAX_BLOCKS = 2000
    MAX_ENTRY_CHARS = 16384

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(self.MAX_BLOCKS)
        self.setUndoRedoEnabled(False)

    def append(self, text):
        text = str(text)
        if len(text) > self.MAX_ENTRY_CHARS:
            text = text[:self.MAX_ENTRY_CHARS] + '\n[Anzeige gekürzt]'
        self.appendPlainText(text)


class PlainMessageBox(QtWidgets.QMessageBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTextFormat(QtCore.Qt.TextFormat.PlainText)

    @classmethod
    def _show(cls, icon, parent, title, text, buttons=None, defaultButton=None):
        box = cls(parent)
        box.setIcon(icon)
        box.setWindowTitle(title)
        box.setText(str(text))
        box.setStandardButtons(buttons or cls.StandardButton.Ok)
        if defaultButton is not None:
            box.setDefaultButton(defaultButton)
        elif buttons and buttons & cls.StandardButton.No:
            box.setDefaultButton(cls.StandardButton.No)
        return cls.StandardButton(box.exec())

    @classmethod
    def information(cls, parent, title, text, buttons=None, defaultButton=None):
        return cls._show(cls.Icon.Information, parent, title, text, buttons, defaultButton)

    @classmethod
    def warning(cls, parent, title, text, buttons=None, defaultButton=None):
        return cls._show(cls.Icon.Warning, parent, title, text, buttons, defaultButton)

    @classmethod
    def critical(cls, parent, title, text, buttons=None, defaultButton=None):
        return cls._show(cls.Icon.Critical, parent, title, text, buttons, defaultButton)

    @classmethod
    def question(cls, parent, title, text, buttons=None, defaultButton=None):
        return cls._show(cls.Icon.Question, parent, title, text,
                         buttons or cls.StandardButton.Yes | cls.StandardButton.No,
                         defaultButton)
