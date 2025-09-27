from PyQt6 import QtWidgets, QtGui, QtCore
from pathlib import Path


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SSH Updater")
        self.resize(1100, 680)

        # Toolbar
        tb = QtWidgets.QToolBar("Main")
        tb.setIconSize(QtCore.QSize(18, 18))
        self.addToolBar(tb)

        # Aktionen explizit anlegen (statt in einer Schleife), damit wir Referenzen haben
        self.act_check = QtGui.QAction("Prüfen", self)
        self.act_sim    = QtGui.QAction("Simulieren", self)
        self.act_upg    = QtGui.QAction("Upgrade", self)
        self.act_config = QtGui.QAction("Konfiguration", self)
        self.act_import = QtGui.QAction("Import von Proxmox", self)

        for a in (self.act_check, self.act_sim, self.act_upg, self.act_config, self.act_import):
            tb.addAction(a)

        # Klick-Handler verbinden
        self.act_config.triggered.connect(self._open_config)

        # Zentraler Bereich
        central = QtWidgets.QWidget()
        main = QtWidgets.QHBoxLayout(central)

        left = QtWidgets.QFrame()
        left.setFixedWidth(240)
        left.setLayout(QtWidgets.QVBoxLayout())
        left.layout().addWidget(QtWidgets.QLabel("Filter (Stub)"))
        left.layout().addStretch(1)

        self.table = QtWidgets.QTableView()
        self.table.setModel(self._dummy())
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)

        bottom = QtWidgets.QTextEdit()
        bottom.setReadOnly(True)
        bottom.setPlaceholderText("Logs …")

        right = QtWidgets.QWidget()
        rlay = QtWidgets.QVBoxLayout(right)
        rlay.addWidget(self.table, 3)
        rlay.addWidget(bottom, 2)

        main.addWidget(left)
        main.addWidget(right)
        self.setCentralWidget(central)

        self._apply_theme()
        self.statusBar().showMessage("Bereit")

    def _apply_theme(self):
        qss = Path(__file__).resolve().parents[1] / "assets" / "qss" / "dark.qss"
        if qss.exists():
            self.setStyleSheet(qss.read_text(encoding="utf-8"))

    def _dummy(self):
        m = QtGui.QStandardItemModel()
        m.setHorizontalHeaderLabels(["Name", "OS", "Status", "Letzte Prüfung"])
        for row in [("debian-vm", "Debian", "OK", "–"),
                    ("web-lxc", "Debian", "Updates verfügbar", "–")]:
            m.appendRow([QtGui.QStandardItem(str(c)) for c in row])
        return m

    def _open_config(self):
        from .ui_config import ConfigDialog
        dlg = ConfigDialog(self)
        dlg.exec()
