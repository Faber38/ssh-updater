from __future__ import annotations
from pathlib import Path
from PyQt6 import QtWidgets, QtCore
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QTableWidget, QTableWidgetItem,
    QMessageBox, QFormLayout, QLineEdit, QComboBox, QSpinBox, QFileDialog, QWidget, QLabel
)
from .core import db, settings


class HostEditDialog(QDialog):
    def __init__(self, parent: QWidget | None, host: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("Host bearbeiten" if host else "Host hinzufügen")
        self._host = host or {}
        lay = QVBoxLayout(self)

        form = QFormLayout()
        self.in_name = QLineEdit(self._host.get("name", ""))
        self.in_ip = QLineEdit(self._host.get("primary_ip", ""))
        self.in_port = QSpinBox(); self.in_port.setRange(1, 65535); self.in_port.setValue(self._host.get("port", 22) or 22)
        self.in_user = QLineEdit(self._host.get("user", ""))
        self.in_auth = QComboBox(); self.in_auth.addItems(["key","password"])
        self.in_auth.setCurrentText(self._host.get("auth_method", "key"))
        self.in_key = QLineEdit(self._host.get("key_path", "") or "")
        btn_key = QPushButton("…"); btn_key.clicked.connect(self._choose_key)
        key_row = QHBoxLayout(); key_row.addWidget(self.in_key, 1); key_row.addWidget(btn_key)

        self.in_pwd = QLineEdit(); self.in_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        if host:
            # Passwort-Feld leer lassen; wird nur gesetzt wenn geändert
            self.in_pwd.setPlaceholderText("Unverändert lassen, wenn leer")
        else:
            self.in_pwd.setPlaceholderText("Optional (SSH-Keys bevorzugt)")

        form.addRow("Name*", self.in_name)
        form.addRow("Primär-IP/Host*", self.in_ip)
        form.addRow("Port", self.in_port)
        form.addRow("User*", self.in_user)
        form.addRow("Auth-Methode", self.in_auth)
        form.addRow("Key-Pfad", QWidget())
        # Hack: vorherige Zeile für Label, jetzt den Key-Editor platzieren
        form.itemAt(form.rowCount()-1, QFormLayout.ItemRole.FieldRole).widget().setLayout(key_row)
        form.addRow("Passwort", self.in_pwd)
        lay.addLayout(form)

        hint = QLabel("Tipp: SSH-Keys sind sicherer als Passwörter.")
        hint.setStyleSheet("color:#aaa;")
        lay.addWidget(hint)

        btns = QHBoxLayout()
        btns.addStretch(1)
        b_cancel = QPushButton("Abbrechen"); b_ok = QPushButton("Speichern")
        b_cancel.clicked.connect(self.reject); b_ok.clicked.connect(self._save)
        btns.addWidget(b_cancel); btns.addWidget(b_ok)
        lay.addLayout(btns)

    def _choose_key(self):
        path, _ = QFileDialog.getOpenFileName(self, "SSH Private Key wählen", "", "Keys (*)")
        if path:
            self.in_key.setText(path)

    def _save(self):
        name = self.in_name.text().strip()
        ip = self.in_ip.text().strip()
        user = self.in_user.text().strip()
        if not name or not ip or not user:
            QMessageBox.warning(self, "Fehlende Angaben", "Name, Primär-IP/Host und User sind Pflichtfelder.")
            return
        self.result_host = {
            "id": self._host.get("id"),
            "proxmox_uid": self._host.get("proxmox_uid"),
            "name": name,
            "primary_ip": ip,
            "port": int(self.in_port.value()),
            "user": user,
            "auth_method": self.in_auth.currentText(),
            "key_path": self.in_key.text().strip() or None,
            "password_plain": (self.in_pwd.text() or None),
            "distro": self._host.get("distro"),
            "tags": None,  # später
        }
        self.accept()

class ConfigDialog(QDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Konfiguration – Hosts")
        self.resize(900, 520)
        lay = QVBoxLayout(self)

        # Table
        self.table = QTableWidget(0, 6, self)
        self.table.setHorizontalHeaderLabels(["Name","Primär-IP","Port","User","Auth","Key-Pfad"])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.table, 1)

        # --- Darstellung: Theme-Auswahl (nur UI, noch ohne Funktion) ---
        row_theme = QHBoxLayout()
        row_theme.addWidget(QLabel("Darstellung:"))

        self.cmb_theme = QComboBox()
        self.cmb_theme.addItems(["Hell", "Dunkel", "Standard", "Colour"])

        # aktuellen Wert aus settings vorwählen
        cur = getattr(settings, "THEME", "standard").lower()
        index_map = {"light": 0, "dark": 1, "standard": 2, "colour": 3}
        self.cmb_theme.setCurrentIndex(index_map.get(cur, 2))

        row_theme.addWidget(self.cmb_theme, 1)
        lay.addLayout(row_theme)
        self.cmb_theme.currentIndexChanged.connect(lambda *_: self._apply_theme_choice())


        # Buttons
        btns = QHBoxLayout()
        self.b_add = QPushButton("Hinzufügen")
        self.b_edit = QPushButton("Bearbeiten")
        self.b_del = QPushButton("Löschen")
        btns.addWidget(self.b_add); btns.addWidget(self.b_edit); btns.addWidget(self.b_del)
        btns.addStretch(1)
        self.b_close = QPushButton("Schließen")
        btns.addWidget(self.b_close)
        lay.addLayout(btns)

        self.b_add.clicked.connect(self._add)
        self.b_edit.clicked.connect(self._edit)
        self.b_del.clicked.connect(self._delete)
        self.b_close.clicked.connect(self.accept)

        self._reload()
        self._apply_theme_choice()

    def _reload(self):
        hosts = db.list_hosts()
        self.table.setRowCount(len(hosts))
        for r, h in enumerate(hosts):
            self.table.setItem(r, 0, QTableWidgetItem(h.get("name") or ""))
            self.table.setItem(r, 1, QTableWidgetItem(h.get("primary_ip") or ""))
            self.table.setItem(r, 2, QTableWidgetItem(str(h.get("port") or 22)))
            self.table.setItem(r, 3, QTableWidgetItem(h.get("user") or ""))
            self.table.setItem(r, 4, QTableWidgetItem(h.get("auth_method") or ""))
            self.table.setItem(r, 5, QTableWidgetItem(h.get("key_path") or ""))

            # host-id in row speichern
            self.table.setVerticalHeaderItem(r, QTableWidgetItem(str(h["id"])))
        if hosts:
            self.table.selectRow(0)

    def _current_host_id(self) -> int | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        vh = self.table.verticalHeaderItem(row)
        return int(vh.text()) if vh else None

    def _add(self):
        dlg = HostEditDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            h = dlg.result_host
            hid = db.add_or_update_host(
                proxmox_uid=None,
                name=h["name"],
                primary_ip=h["primary_ip"],
                ips=None,
                port=h["port"],
                user=h["user"],
                auth_method=h["auth_method"],
                key_path=h["key_path"],
                password_plain=h["password_plain"],
                distro=None,
                tags=None,
            )
            # falls Passwort separat: (hier schon in add_or_update enthalten)
            self._reload()

    def _edit(self):
        hid = self._current_host_id()
        if not hid:
            QMessageBox.information(self, "Hinweis", "Bitte einen Host auswählen.")
            return
        host = db.get_host(hid)
        dlg = HostEditDialog(self, host)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            h = dlg.result_host
            # Upsert (identifiziert über id → wir nutzen proxmox_uid/name)
            db.add_or_update_host(
                proxmox_uid=host.get("proxmox_uid"),
                name=h["name"],
                primary_ip=h["primary_ip"],
                ips=None,
                port=h["port"],
                user=h["user"],
                auth_method=h["auth_method"],
                key_path=h["key_path"],
                password_plain=None,  # Passwort separat, nur wenn geändert
                distro=host.get("distro"),
                tags=None,
            )
            if h["password_plain"]:
                db.set_host_password(host["id"], h["password_plain"])
            self._reload()

    def _delete(self):
        hid = self._current_host_id()
        if not hid:
            QMessageBox.information(self, "Hinweis", "Bitte einen Host auswählen.")
            return
        if QMessageBox.question(self, "Löschen", "Diesen Host wirklich löschen?") == QMessageBox.StandardButton.Yes:
            con = db._connect()
            con.execute("DELETE FROM hosts WHERE id=?", (hid,))
            con.commit(); con.close()
            self._reload()

    def _apply_theme_choice(self):
        """Auswahl aus self.cmb_theme anwenden und persistent speichern."""
        import sys  # lokal, um keinen globalen Import zu ändern
        from PyQt6 import QtWidgets

        # Index -> Theme-String
        idx = self.cmb_theme.currentIndex()
        theme = {0: "light", 1: "dark", 2: "standard", 3: "colour"}.get(idx, "standard")

        # in settings merken + persistent speichern
        settings.THEME = theme
        try:
            (settings.DATA_DIR / "theme.txt").write_text(theme, encoding="utf-8")
        except Exception:
            pass

        # QSS-Pfad bestimmen (Onefile kompatibel)
        def qss_path(name: str) -> Path:
            base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
            # dev:  .../sshupdater/ (ui_config.py liegt in diesem Ordner)
            # prod: _MEIPASS enthält 'assets/qss'
            dev_path = Path(__file__).resolve().parent / "assets" / "qss" / f"{name}.qss"
            bundled = base / "assets" / "qss" / f"{name}.qss"
            return bundled if bundled.exists() else dev_path

        app = QtWidgets.QApplication.instance()
        app.setStyleSheet("")  # altes Stylesheet vollständig entfernen
        if theme in ("light", "dark", "colour"):
            qss = qss_path(theme)
            if qss.exists():
                app.setStyleSheet(qss.read_text(encoding="utf-8"))
                return

        # Standard oder QSS nicht gefunden -> neutrales helles Fallback
        app.setStyleSheet("""
            QWidget { background-color: #f0f0f0; color: #000; font-family: DejaVu Sans, Arial; font-size: 10pt; }
            QPushButton { background-color: #e0e0e0; border: 1px solid #a0a0a0; padding: 4px 8px; }
            QPushButton:hover { background-color: #f8f8f8; }
        """)
