"""GUI resources shared by development and PyInstaller builds."""
import sys
from pathlib import Path
from PyQt6 import QtCore, QtGui, QtWidgets


def resource_path(*parts):
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)).joinpath(*parts)


class DockerToolbarMark(QtWidgets.QLabel):
    """Original, passive container resource, tinted from the toolbar palette."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('dockerToolbarMark')
        self.setFixedSize(26, 22)
        self.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.setToolTip('Container / Docker')
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.setStyleSheet('background: transparent; border: none;')
        path = resource_path('assets', 'icons', 'container.svg')
        icon = QtGui.QIcon(str(path)) if path.is_file() else QtGui.QIcon()
        if not icon.isNull():
            self.setPixmap(icon.pixmap(QtCore.QSize(20, 20), self.devicePixelRatioF()))
        # Missing/corrupt resources remain an empty, font-independent slot.

    def paintEvent(self, event):
        source = self.pixmap()
        if source.isNull():
            return
        pixmap = source.copy()
        painter = QtGui.QPainter(pixmap)
        painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_SourceIn)
        palette = self.parentWidget().palette() if self.parentWidget() else self.palette()
        painter.fillRect(pixmap.rect(), toolbar_ink(palette))
        painter.end()
        painter = QtGui.QPainter(self)
        size = pixmap.deviceIndependentSize()
        painter.drawPixmap(QtCore.QPointF((self.width()-size.width())/2, (self.height()-size.height())/2), pixmap)


def toolbar_ink(palette):
    """Contrast against the actual theme brush, including Colour's gradient."""
    brush = palette.brush(QtGui.QPalette.ColorRole.Window)
    gradient = brush.gradient()
    background = gradient.stops()[0][1] if gradient and gradient.stops() else brush.color()
    return palette.color(QtGui.QPalette.ColorRole.BrightText if background.lightness() < 128
                         else QtGui.QPalette.ColorRole.WindowText)


class ContainerToolBar(QtWidgets.QToolBar):
    """Paint one subtle group behind existing actions; preserve toolbar mechanics."""
    def __init__(self, title, parent=None):
        super().__init__(title, parent)
        self.container_actions = ()

    def container_group_rect(self):
        rect = QtCore.QRect()
        for action in self.container_actions:
            widget = self.widgetForAction(action)
            if widget is not None and widget.isVisible():
                rect = rect.united(widget.geometry())
        return rect

    def paintEvent(self, event):
        super().paintEvent(event)
        rect = self.container_group_rect()
        if not rect.isNull():
            tint = toolbar_ink(self.palette())
            tint.setAlpha(18)
            painter = QtGui.QPainter(self)
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(tint)
            painter.drawRoundedRect(QtCore.QRectF(rect), 4, 4)
