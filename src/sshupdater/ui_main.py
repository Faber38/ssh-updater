import os
import platform
import socket
import shutil
import subprocess
from PyQt6 import QtWidgets, QtGui, QtCore
from pathlib import Path
from datetime import datetime
from sshupdater.core import settings


class SysInfoWidget(QtWidgets.QFrame):
    """Zeigt lokale Systeminformationen an (Host, OS, Kernel, Uptime, Load, RAM, Disk, IPs)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        self.setFrameShadow(QtWidgets.QFrame.Shadow.Plain)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        # Schöner Stil: kleinere Schrift, dezente Farben
        self.setStyleSheet(
            """
            QFrame {
                background-color: #f2f2f2;
                border: 1px solid #ccc;
                border-radius: 8px;
            }
            QLabel {
                font-size: 8pt;
                color: #444;
            }
            QLabel.title {
                font-size: 11pt;
                font-weight: bold;
                color: #202020;
                padding-bottom: 4px;
            }
            QLabel.key {
                color: #666;
                font-weight: normal;
            }
            QLabel.value {
                color: #000;
                font-weight: bold;
            }
        """
        )

        # Überschrift
        title = QtWidgets.QLabel("Systeminfo (lokal)")
        title.setProperty("class", "title")
        lay.addWidget(title)

        # Layout für Key/Value-Paare
        grid = QtWidgets.QGridLayout()
        grid.setVerticalSpacing(4)
        grid.setHorizontalSpacing(10)
        lay.addLayout(grid)

        def kv_row(row, key_text):
            lab_k = QtWidgets.QLabel(key_text)
            lab_k.setProperty("class", "key")
            lab_v = QtWidgets.QLabel("–")
            lab_v.setProperty("class", "value")
            grid.addWidget(lab_k, row, 0)
            grid.addWidget(lab_v, row, 1)
            return lab_v

        self.lab_host = kv_row(0, "Hostname:")
        self.lab_os = kv_row(1, "OS:")
        self.lab_kernel = kv_row(2, "Kernel:")
        self.lab_uptime = kv_row(3, "Uptime:")
        self.lab_load = kv_row(4, "Load:")
        self.lab_mem = kv_row(5, "RAM:")
        self.lab_disk = kv_row(6, "Root-Disk:")
        self.lab_ip = kv_row(7, "IP(s):")
        self.lab_ssh = kv_row(8, "SSH-Dienst:")

        lay.addSpacing(6)
        btn_row = QtWidgets.QHBoxLayout()
        lay.addLayout(btn_row)
        self.btn_refresh = QtWidgets.QPushButton("Aktualisieren")
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_refresh)
        self.btn_refresh.clicked.connect(self.refresh)

        lay.addStretch(1)

        # Auto-Refresh alle 5s
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(5000)

        self.refresh()  # initial

    # -------- Helpers --------
    def _read_os_release(self) -> str:
        p = Path("/etc/os-release")
        if p.exists():
            txt = p.read_text(encoding="utf-8", errors="ignore")
            for line in txt.splitlines():
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
        return platform.system()

    def _uptime_str(self) -> str:
        try:
            with open("/proc/uptime", "r") as f:
                secs = float(f.read().split()[0])
            mins, sec = divmod(int(secs), 60)
            hrs, mins = divmod(mins, 60)
            days, hrs = divmod(hrs, 24)
            parts = []
            if days:
                parts.append(f"{days} Tage")
            if hrs:
                parts.append(f"{hrs} Std")
            if mins:
                parts.append(f"{mins} Min")
            return " ".join(parts) or f"{sec}s"
        except Exception:
            return "–"

    def _mem_str(self) -> str:
        try:
            meminfo = Path("/proc/meminfo").read_text().splitlines()
            kv = {}
            for line in meminfo:
                k, v = line.split(":", 1)
                kv[k.strip()] = v.strip()

            def _kb(v):
                return int(v.split()[0])

            total = _kb(kv["MemTotal"]) * 1024
            avail = _kb(kv.get("MemAvailable", kv["MemFree"])) * 1024
            used = total - avail

            def fmt(b):
                for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
                    if b < 1024 or unit == "TiB":
                        break
                    b /= 1024.0
                return f"{b:.1f} {unit}"

            return f"{fmt(used)} / {fmt(total)}"
        except Exception:
            return "–"

    def _disk_root_str(self) -> str:
        try:
            total, used, free = shutil.disk_usage("/")

            def fmt(b):
                for unit in ("B", "GiB", "TiB", "PiB"):
                    if b < (1024**3) or unit != "B":
                        break
                # einfache GiB-Ausgabe:
                return f"{b/1024**3:.1f} GiB"

            pct = used / total * 100 if total else 0
            return f"{fmt(used)} / {fmt(total)}  ({pct:.0f} %)"
        except Exception:
            return "–"

    def _ips_str(self) -> str:
        # robuste IP-Ermittlung über `ip -4 addr`
        try:
            out = subprocess.check_output(
                ["ip", "-4", "addr"], text=True, errors="ignore"
            )
            ips = []
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("inet "):
                    ip = line.split()[1].split("/")[0]
                    if not ip.startswith("127."):
                        ips.append(ip)
            return ", ".join(ips) if ips else "–"
        except Exception:
            return "–"

    def refresh(self):
        try:
            host = socket.gethostname()
        except Exception:
            host = "–"
        os_name = self._read_os_release()
        kernel = platform.release()
        uptime = self._uptime_str()
        load = (
            " / ".join(f"{v:.2f}" for v in os.getloadavg())
            if hasattr(os, "getloadavg")
            else "–"
        )
        mem = self._mem_str()
        disk = self._disk_root_str()
        ips = self._ips_str()
        # SSH-Dienst prüfen
        try:
            out = subprocess.run(
                ["systemctl", "is-active", "ssh"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            ssh_status = out.stdout.strip()
            if ssh_status == "active":
                ssh_state = "aktiv ✅"
            elif ssh_status == "inactive":
                ssh_state = "inaktiv ⚪"
            else:
                ssh_state = f"{ssh_status or 'unbekannt'} ⚠️"
        except Exception:
            ssh_state = "nicht installiert ❌"

        self.lab_host.setText(f"Hostname: <b>{host}</b>")
        self.lab_os.setText(f"OS: {os_name}")
        self.lab_kernel.setText(f"Kernel: {kernel}")
        self.lab_uptime.setText(f"Uptime: {uptime}")
        self.lab_load.setText(f"Load: {load}")
        self.lab_mem.setText(f"RAM: {mem}")
        self.lab_disk.setText(f"Root-Disk: {disk}")
        self.lab_ip.setText(f"IP(s): {ips}")
        self.lab_ssh.setText(f"SSH: {ssh_state}")


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SSH Updater")
        self.resize(1100, 680)

        # Toolbar
        tb = QtWidgets.QToolBar("Main")
        tb.setIconSize(QtCore.QSize(18, 18))
        self.addToolBar(tb)

        self.act_check = QtGui.QAction("Prüfen", self)
        self.act_sim = QtGui.QAction("Simulieren", self)
        self.act_upg = QtGui.QAction("Upgrade", self)
        self.act_clean = QtGui.QAction("Bereinigen", self)
        self.act_reboot = QtGui.QAction("Reboot", self)
        self.act_config = QtGui.QAction("Konfiguration", self)
        for a in (
            self.act_check,
            self.act_sim,
            self.act_upg,
            self.act_clean,
            self.act_reboot,
            self.act_config,
        ):
            tb.addAction(a)

        # Klick-Handler
        self.act_config.triggered.connect(self._open_config)
        self.act_check.triggered.connect(self._on_check)
        self.act_sim.triggered.connect(self._on_sim)
        self.act_upg.triggered.connect(self._on_upgrade)
        self.act_clean.triggered.connect(self._on_clean)
        self.act_reboot.triggered.connect(self._on_reboot)

        # Zentraler Bereich: resizable via QSplitter
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)

        # Linkes Panel (Systeminfo)
        left = SysInfoWidget()
        left.setMinimumWidth(260)  # untere Grenze
        left.setMaximumWidth(600)  # optionale obere Grenze (anpassbar)

        # Rechtes Panel (Tabelle + Log)
        right = QtWidgets.QWidget()
        rlay = QtWidgets.QVBoxLayout(right)
        rlay.setContentsMargins(0, 0, 0, 0)

        self.table = QtWidgets.QTableView()
        self._reload_hosts()
        self.table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.SingleSelection
        )

        self.log = QtWidgets.QTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("Logs …")

        rlay.addWidget(self.table, 3)
        rlay.addWidget(self.log, 2)

        # In den Splitter einsetzen
        splitter.addWidget(left)
        splitter.addWidget(right)

        # Dehnung: rechts bekommt den Platz
        splitter.setStretchFactor(0, 0)  # links fix(er)
        splitter.setStretchFactor(1, 1)  # rechts dehnt

        # Startbreiten (px) einstellen
        splitter.setSizes([320, 900])

        # Splitter als zentrales Widget
        self.setCentralWidget(splitter)
        # ---- Einstellungen wiederherstellen (Theme, Geometrie, Splitter)
        self._qset = QtCore.QSettings("Faber38", "SSH Updater")

        # Fenster-Geometrie
        geom = self._qset.value("win/geometry", None)
        if geom is not None:
            self.restoreGeometry(geom)

        # Splitter-Größen
        sizes = self._qset.value("ui/splitter_sizes", None)
        if sizes:
            try:
                self.centralWidget().setSizes([int(s) for s in sizes])
            except Exception:
                pass

        # Theme aus QSettings anwenden (Fallback auf settings.THEME)
        self._apply_theme()

        self._apply_theme()
        self.statusBar().showMessage("Bereit")

    def _get_selected_host_ids(self) -> list:
        """Return list of host IDs (int) that are checked in the table."""
        model = self.table.model()
        ids = []
        if model is None:
            return ids
        for r in range(model.rowCount()):
            chk_item = model.item(r, 0)
            if (
                chk_item is not None
                and chk_item.checkState() == QtCore.Qt.CheckState.Checked
            ):
                # Host-ID ist im Name-Item (Spalte 1) gespeichert
                name_item = model.item(r, 1)
                hid = name_item.data(QtCore.Qt.ItemDataRole.UserRole)
                if hid is not None:
                    ids.append(int(hid))
        return ids

    def _apply_theme(self):
        # 1) Theme aus QSettings lesen, 2) Fallback auf settings.THEME
        q_theme = QtCore.QSettings("Faber38", "SSH Updater").value("ui/theme", None)
        theme = (q_theme or settings.THEME or "standard").lower()

        # QSS-Datei anhand theme wählen
        from pathlib import Path

        base = Path(__file__).resolve().parents[1]  # src/sshupdater/..
        qss = None
        if theme == "dark":
            qss = base / "assets" / "qss" / "dark.qss"
        elif theme == "light":
            qss = base / "assets" / "qss" / "light.qss"
        elif theme == "colour":
            qss = base / "assets" / "qss" / "colour.qss"

        if qss and qss.exists():
            self.setStyleSheet(qss.read_text(encoding="utf-8"))
        else:
            self.setStyleSheet("")  # Standard-Qt-Theme

    def _open_config(self):
        from .ui_config import ConfigDialog

        dlg = ConfigDialog(self)
        dlg.exec()
        self._reload_hosts()

    def _find_row_by_host_id(self, host_id: int) -> int:
        model = self.table.model()
        for r in range(model.rowCount()):
            name_item = model.item(r, 1)
            if name_item and name_item.data(QtCore.Qt.ItemDataRole.UserRole) == host_id:
                return r
        return -1

    def _make_dot_icon(self, color: str) -> QtGui.QIcon:
        # Farbe -> runder Punkt als QIcon (gecacht)
        cache = getattr(self, "_dot_cache", {})
        if color in cache:
            return cache[color]
        pm = QtGui.QPixmap(14, 14)
        pm.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pm)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        brush = QtGui.QBrush(QtGui.QColor(color))
        pen = QtGui.QPen(QtGui.QColor("#333"))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.setBrush(brush)
        painter.drawEllipse(1, 1, 12, 12)
        painter.end()
        icon = QtGui.QIcon(pm)
        cache[color] = icon
        self._dot_cache = cache
        return icon

    def _status_icon_for(self, online: bool, updates: int | None) -> QtGui.QIcon:
        # online + keine Updates => grün, online + Updates => gelb, offline/Fehler => rot
        if not online:
            return self._make_dot_icon("#e23b3b")  # rot
        if updates is None:
            return self._make_dot_icon("#9e9e9e")  # grau (unbekannt)
        return self._make_dot_icon("#3ac569" if updates == 0 else "#f2b84b")

    # ========= Prüfen: Worker-Thread + Slots =========

    def _on_check(self):
        for a in (
            self.act_check,
            self.act_sim,
            self.act_upg,
            self.act_clean,
            self.act_reboot,
            self.act_config,
        ):
            a.setEnabled(False)

        selected = self._get_selected_host_ids()
        if not selected:
            QtWidgets.QMessageBox.information(
                self, "Keine Auswahl", "Bitte zuerst Hosts auswählen (Haken setzen)."
            )
            for a in (
                self.act_check,
                self.act_sim,
                self.act_upg,
                self.act_clean,
                self.act_reboot,
                self.act_config,
            ):
                a.setEnabled(True)
            return

        self.log.clear()
        self.log.append("Starte Prüfungen...\n")

        self.worker = _CheckWorker(selected)
        self.worker.one_result.connect(self._on_check_result)
        self.worker.finished_all.connect(self._on_check_done)
        self.worker.start()

    def _on_check_result(self, res: dict):
        # Log
        if res.get("status") == "ok":
            self.log.append(
                f"✔ {res['name']} [{res.get('distro', '?')}]: {res.get('updates', 0)} Updates"
            )
            online = True
            updates = int(res.get("updates", 0))
        else:
            self.log.append(f"✖ {res.get('name', '?')}: {res.get('note', 'Fehler')}")
            online = False
            updates = None
        self.log.moveCursor(QtGui.QTextCursor.MoveOperation.End)

        # UI-Zeile aktualisieren
        row = self._find_row_by_host_id(res.get("host_id"))
        if row >= 0:
            model = self.table.model()

            if online:
                status_text = f"Online – {updates} Updates"
                status_item = QtGui.QStandardItem(status_text)
                status_item.setIcon(self._status_icon_for(True, updates))
            else:
                status_item = QtGui.QStandardItem("Offline")
                status_item.setIcon(self._status_icon_for(False, None))

            model.setItem(row, 5, status_item)  # Spalte "Status"

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            model.setItem(row, 6, QtGui.QStandardItem(timestamp))  # "Letzte Prüfung"

            # in DB persistieren
            try:
                from .core import db

                db.set_check_result(res["host_id"], timestamp, updates)
            except Exception as e:
                self.statusBar().showMessage(f"Speicherfehler: {e}", 5000)

    def _on_check_done(self):
        self.log.append("\nFertig.")
        for a in (
            self.act_check,
            self.act_sim,
            self.act_upg,
            self.act_clean,
            self.act_reboot,
            self.act_config,
        ):
            a.setEnabled(True)

    # ========= Simulieren =========
    def _on_sim(self):
        for a in (
            self.act_check,
            self.act_sim,
            self.act_upg,
            self.act_clean,
            self.act_reboot,
            self.act_config,
        ):
            a.setEnabled(False)

        selected = self._get_selected_host_ids()
        if not selected:
            QtWidgets.QMessageBox.information(
                self, "Keine Auswahl", "Bitte zuerst Hosts auswählen (Haken setzen)."
            )
            for a in (
                self.act_check,
                self.act_sim,
                self.act_upg,
                self.act_clean,
                self.act_reboot,
                self.act_config,
            ):
                a.setEnabled(True)
            return

        self.log.clear()
        self.log.append("Starte Simulationen...\n")

        self.sim_worker = _SimWorker(selected)
        self.sim_worker.one_result.connect(self._on_sim_result)
        self.sim_worker.finished_all.connect(self._on_sim_done)
        self.sim_worker.start()

    def _on_sim_result(self, res: dict):
        if res.get("status") == "ok":
            n = res.get("packages", 0)
            self.log.append(
                f"🧪 {res['name']} [{res.get('distro', '?')}]: {n} Pakete geplant"
            )
            details = (res.get("details") or "").strip()
            if details:
                lines = details.splitlines()
                preview = "\n".join(lines[:20])
                if preview:
                    self.log.append(preview)
                    if len(lines) > 20:
                        self.log.append(f"... ({len(lines)-20} weitere Zeilen)\n")
                else:
                    self.log.append("(keine Details)\n")
        else:
            self.log.append(f"✖ {res.get('name', '?')}: {res.get('note', 'Fehler')}")
        self.log.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def _on_sim_done(self):
        self.log.append("\nFertig.")
        for a in (
            self.act_check,
            self.act_sim,
            self.act_upg,
            self.act_clean,
            self.act_reboot,
            self.act_config,
        ):
            a.setEnabled(True)

    # ========= Upgraden =========
    def _on_upgrade(self):
        # Sicherheitsabfrage
        ret = QtWidgets.QMessageBox.question(
            self,
            "Upgrade starten",
            "Alle gelisteten Hosts jetzt upgraden?\n\n"
            "Hinweis: Es werden Paket-Upgrades per sudo -n ausgeführt.\n"
            "Stelle sicher, dass NOPASSWD für die Paketbefehle konfiguriert ist.",
        )
        if ret != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        # Buttons sperren
        for a in (
            self.act_check,
            self.act_sim,
            self.act_upg,
            self.act_clean,
            self.act_reboot,
            self.act_config,
        ):
            a.setEnabled(False)

        selected = self._get_selected_host_ids()
        if not selected:
            QtWidgets.QMessageBox.information(
                self, "Keine Auswahl", "Bitte zuerst Hosts auswählen (Haken setzen)."
            )
            for a in (
                self.act_check,
                self.act_sim,
                self.act_upg,
                self.act_clean,
                self.act_reboot,
                self.act_config,
            ):
                a.setEnabled(True)
            return

        self.log.clear()
        self.log.append("Starte Upgrades...\n")

        self.upg_worker = _UpgradeWorker(selected)
        self.upg_worker.progress.connect(self._on_upgrade_progress)
        self.upg_worker.host_done.connect(self._on_upgrade_host_done)
        self.upg_worker.finished_all.connect(self._on_upgrade_done)
        self.upg_worker.start()

    def _on_upgrade_progress(self, payload: dict):
        # payload: {"name": "...", "line": "..."}
        self.log.append(f"{payload['name']}: {payload['line']}")
        self.log.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def _on_upgrade_host_done(self, res: dict):
        # res: {"host_id", "name", "status", "note", "distro"}
        if res.get("status") == "ok":
            self.log.append(
                f"✅ {res['name']}: Upgrade abgeschlossen ({res.get('distro', '?')})."
            )
            row = self._find_row_by_host_id(res.get("host_id"))
            if row >= 0:
                model = self.table.model()
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                model.setItem(
                    row, 5, QtGui.QStandardItem("Online – 0 Updates")
                )  # Spalte Status
                # Spalte Letzte Prüfung
                model.setItem(row, 6, QtGui.QStandardItem(ts))
                try:
                    from .core import db

                    db.set_check_result(res["host_id"], ts, 0)
                except Exception:
                    pass
        else:
            self.log.append(f"❌ {res.get('name', '?')}: {res.get('note', 'Fehler')}")
        self.log.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def _on_upgrade_done(self):
        self.log.append("\nAlle Upgrades beendet.")
        for a in (
            self.act_check,
            self.act_sim,
            self.act_upg,
            self.act_clean,
            self.act_reboot,
            self.act_config,
        ):
            a.setEnabled(True)

    # ========= Bereinigen ==========

    def _on_clean(self):
        # Auswahl prüfen
        selected = self._get_selected_host_ids()
        if not selected:
            QtWidgets.QMessageBox.information(
                self, "Keine Auswahl", "Bitte Hosts anhaken."
            )
            return
        self._clean_selected = selected

        # Buttons sperren
        for a in (
            self.act_check,
            self.act_sim,
            self.act_upg,
            self.act_clean,
            self.act_reboot,
            self.act_config,
        ):
            a.setEnabled(False)

        self.log.clear()
        self.log.append("Starte Autoremove-Simulation...\n")

        # 1) Simulation
        self.clean_sim_worker = _CleanSimWorker(selected)
        self.clean_sim_worker.one_result.connect(self._on_clean_sim_result)
        self.clean_sim_worker.finished_all.connect(self._on_clean_sim_done)
        self.clean_sim_worker.start()

    def _on_clean_sim_result(self, res: dict):
        if res.get("status") == "ok":
            n = res.get("packages", 0)
            self.log.append(f"🧪 {res['name']}: {n} Pakete würden entfernt.")
            details = (res.get("details") or "").strip()
            if details:
                lines = details.splitlines()
                preview = "\n".join(lines[:20])
                if preview:
                    self.log.append(
                        preview
                        + (
                            "\n"
                            if len(lines) <= 20
                            else f"\n... ({len(lines)-20} weitere Zeilen)\n"
                        )
                    )
        else:
            self.log.append(f"✖ {res.get('name', '?')}: {res.get('note', 'Fehler')}")
        self.log.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def _on_clean_sim_done(self):
        # Nachfrage nur, wenn irgendwo >0 Pakete
        text = self.log.toPlainText()
        any_removals = (
            "würden entfernt" in text and "0 Pakete würden entfernt" not in text
        )
        sel = getattr(self, "_clean_selected", [])
        if not sel:
            self.log.append("\nAbgebrochen (keine Auswahl).")
            for a in (
                self.act_check,
                self.act_sim,
                self.act_upg,
                self.act_clean,
                self.act_reboot,
                self.act_config,
            ):
                a.setEnabled(True)
            return

        ret = QtWidgets.QMessageBox.question(
            self,
            "Autoremove ausführen",
            "Simulation abgeschlossen.\nJetzt auf den ausgewählten Hosts 'apt autoremove --purge' ausführen?",
            QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.No,
        )
        if ret != QtWidgets.QMessageBox.StandardButton.Yes:
            self.log.append("\nAbgebrochen.")
            for a in (
                self.act_check,
                self.act_sim,
                self.act_upg,
                self.act_clean,
                self.act_reboot,
                self.act_config,
            ):
                a.setEnabled(True)
            return

        # 2) Live-Run
        self.log.append("\nStarte Autoremove...\n")
        self.clean_run_worker = _CleanRunWorker(sel)
        self.clean_run_worker.progress.connect(self._on_clean_progress)
        self.clean_run_worker.host_done.connect(self._on_clean_host_done)
        self.clean_run_worker.finished_all.connect(self._on_clean_done)
        self.clean_run_worker.start()

    def _on_clean_progress(self, payload: dict):
        self.log.append(f"{payload['name']}: {payload['line']}")
        self.log.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def _on_clean_host_done(self, res: dict):
        if res.get("status") == "ok":
            self.log.append(f"✅ {res['name']}: Autoremove abgeschlossen.")
        else:
            self.log.append(f"❌ {res.get('name', '?')}: {res.get('note', 'Fehler')}")
        self.log.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def _on_clean_done(self):
        self.log.append("\nBereinigung beendet.")
        for a in (
            self.act_check,
            self.act_sim,
            self.act_upg,
            self.act_clean,
            self.act_reboot,
            self.act_config,
        ):
            a.setEnabled(True)
        self._clean_selected = []

    # ========= reboot =========

    def _on_reboot(self):
        # Auswahl prüfen
        selected = self._get_selected_host_ids()
        if not selected:
            QtWidgets.QMessageBox.information(
                self, "Keine Auswahl", "Bitte Hosts anhaken."
            )
            return

        ret = QtWidgets.QMessageBox.question(
            self,
            "Reboot ausführen",
            f"Sollen {len(selected)} ausgewählte Host(s) neu gestartet werden?\n"
            "Hinweis: Der SSH-Stream bricht ggf. sofort ab.",
        )
        if ret != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        # Buttons sperren
        for a in (
            self.act_check,
            self.act_sim,
            self.act_upg,
            self.act_clean,
            self.act_reboot,
            self.act_config,
        ):
            a.setEnabled(False)

        self.log.clear()
        self.log.append("Starte Reboot...\n")

        self.reboot_worker = _RebootWorker(selected)
        self.reboot_worker.host_done.connect(self._on_reboot_host_done)
        self.reboot_worker.finished_all.connect(self._on_reboot_done)
        self.reboot_worker.start()

    def _on_reboot_host_done(self, res: dict):
        if res.get("status") == "ok":
            self.log.append(f"🔁 {res['name']}: {res.get('note', 'Reboot ausgelöst')}")
        else:
            self.log.append(f"❌ {res.get('name', '?')}: {res.get('note', 'Fehler')}")
        self.log.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def _on_reboot_done(self):
        self.log.append("\nReboot-Befehle abgesetzt.")
        for a in (
            self.act_check,
            self.act_sim,
            self.act_upg,
            self.act_clean,
            self.act_reboot,
            self.act_config,
        ):
            a.setEnabled(True)

    # ========= Hosts laden =========

    def _reload_hosts(self):
        from .core import db

        hosts = db.list_hosts()

        model = QtGui.QStandardItemModel()
        model.setHorizontalHeaderLabels(
            ["✓", "Name", "IP", "User", "Auth", "Status", "Letzte Prüfung"]
        )

        for h in hosts:
            # Checkbox
            chk = QtGui.QStandardItem()
            chk.setCheckable(True)
            chk.setCheckState(QtCore.Qt.CheckState.Unchecked)
            chk.setEditable(False)

            # Basisfelder
            name = QtGui.QStandardItem(h.get("name") or "")
            name.setData(h["id"], QtCore.Qt.ItemDataRole.UserRole)

            ip = QtGui.QStandardItem(h.get("primary_ip") or "")
            user = QtGui.QStandardItem(h.get("user") or "")
            auth = QtGui.QStandardItem(h.get("auth_method") or "")

            # Status + Icon
            pending = h.get("pending_updates")  # kann None sein
            if pending is None:
                status_text = "—"
                status = QtGui.QStandardItem(status_text)
                # Grau (unklar) – online=True, updates=None -> grau laut _status_icon_for
                status.setIcon(self._status_icon_for(True, None))
            else:
                status_text = f"Online – {int(pending)} Updates"
                status = QtGui.QStandardItem(status_text)
                status.setIcon(self._status_icon_for(True, int(pending)))

            # Letzte Prüfung
            last_item = QtGui.QStandardItem(h.get("last_check") or "—")

            # nicht editierbar (außer Checkbox)
            for it in (name, ip, user, auth, status, last_item):
                it.setEditable(False)

            model.appendRow([chk, name, ip, user, auth, status, last_item])

        self.table.setModel(model)
        self.table.resizeColumnsToContents()
        self.table.setColumnWidth(0, 30)

    def closeEvent(self, event):
        try:
            # Fenster-Geometrie speichern
            self._qset.setValue("win/geometry", self.saveGeometry())
            # Splitter-Größen speichern
            splitter = self.centralWidget()
            if isinstance(splitter, QtWidgets.QSplitter):
                self._qset.setValue("ui/splitter_sizes", splitter.sizes())
        finally:
            super().closeEvent(event)


class _CheckWorker(QtCore.QThread):
    one_result = QtCore.pyqtSignal(dict)
    finished_all = QtCore.pyqtSignal()

    def __init__(self, host_ids: list | None = None):
        super().__init__()
        self.host_ids = host_ids

    def run(self):
        import asyncio
        from .core import db, ssh_client

        # Lade Hosts aus DB (alle) und filtere ggf.
        all_hosts = db.list_hosts()
        hosts = [h for h in all_hosts if not self.host_ids or h["id"] in self.host_ids]

        async def _job():
            for h in hosts:
                if not h.get("primary_ip") or not h.get("user"):
                    self.one_result.emit(
                        {
                            "host_id": h["id"],  # <<<<
                            "name": h.get("name", "?"),
                            "status": "error",
                            "note": "IP/User fehlt",
                        }
                    )
                    continue
                res = await ssh_client.check_updates_for_host(h)
                # <<<< host_id sicherstellen
                res.setdefault("host_id", h["id"])
                self.one_result.emit(res)

        asyncio.run(_job())
        self.finished_all.emit()


class _SimWorker(QtCore.QThread):
    one_result = QtCore.pyqtSignal(dict)
    finished_all = QtCore.pyqtSignal()

    def __init__(self, host_ids: list | None = None):
        super().__init__()
        self.host_ids = host_ids

    def run(self):
        from .core import db, ssh_client
        import asyncio
        import traceback

        all_hosts = db.list_hosts()
        hosts = [h for h in all_hosts if not self.host_ids or h["id"] in self.host_ids]

        async def _job():
            for h in hosts:
                if not h.get("primary_ip") or not h.get("user"):
                    self.one_result.emit(
                        {
                            "host_id": h["id"],
                            "name": h.get("name", "?"),
                            "status": "error",
                            "note": "IP/User fehlt",
                        }
                    )
                    continue
                try:
                    # >>> richtige Funktion für die Simulation! <<<
                    res = await ssh_client.simulate_upgrade_for_host(h)
                    res.setdefault("host_id", h["id"])
                    self.one_result.emit(res)
                except Exception as ex:
                    self.one_result.emit(
                        {
                            "host_id": h["id"],
                            "name": h.get("name", "?"),
                            "status": "error",
                            "note": f"Sim-Fehler: {ex}",
                        }
                    )

        # Eigener Event-Loop pro QThread (robust für Python 3.13)
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_job())
        except Exception:
            self.one_result.emit(
                {
                    "status": "error",
                    "name": "SimWorker",
                    "note": "Uncaught: " + traceback.format_exc(limit=1),
                }
            )
        finally:
            loop.close()

        self.finished_all.emit()


class _UpgradeWorker(QtCore.QThread):
    progress = QtCore.pyqtSignal(dict)  # {"name","line"}
    # {"host_id","name","status","note","distro"?}
    host_done = QtCore.pyqtSignal(dict)
    finished_all = QtCore.pyqtSignal()

    def __init__(self, host_ids: list | None = None):
        super().__init__()
        self.host_ids = host_ids

    def run(self):
        import asyncio
        from .core import db, ssh_client

        # Hosts aus DB laden und ggf. auf Auswahl filtern
        all_hosts = db.list_hosts()
        hosts = [h for h in all_hosts if not self.host_ids or h["id"] in self.host_ids]

        async def _job():
            for h in hosts:
                name = h.get("name", "?")  # <<< vor der Nutzung setzen
                if not h.get("primary_ip") or not h.get("user"):
                    # hier NICHT one_result, sondern host_done:
                    self.host_done.emit(
                        {
                            "host_id": h["id"],
                            "name": name,
                            "status": "error",
                            "note": "IP/User fehlt",
                        }
                    )
                    continue
                try:
                    agen = ssh_client.upgrade_host_stream(h)
                    async for msg in agen:
                        if not isinstance(msg, dict):
                            continue
                        if msg.get("type") == "line":
                            self.progress.emit({"name": name, "line": msg["line"]})
                        elif msg.get("type") == "result":
                            res = msg["result"] or {}
                            res.update({"host_id": h["id"], "name": name})
                            self.host_done.emit(res)
                except Exception as ex:
                    self.host_done.emit(
                        {
                            "host_id": h["id"],
                            "name": name,
                            "status": "error",
                            "note": str(ex),
                        }
                    )

        asyncio.run(_job())
        self.finished_all.emit()


class _CleanSimWorker(QtCore.QThread):
    one_result = QtCore.pyqtSignal(dict)
    finished_all = QtCore.pyqtSignal()

    def __init__(self, host_ids: list[int]):
        super().__init__()
        self.host_ids = host_ids

    def run(self):
        import asyncio
        from .core import db, ssh_client

        all_hosts = db.list_hosts()
        hosts = [h for h in all_hosts if h["id"] in self.host_ids]

        async def _job():
            for h in hosts:
                res = await ssh_client.simulate_autoremove_for_host(h)
                self.one_result.emit(res)

        asyncio.run(_job())
        self.finished_all.emit()


class _CleanRunWorker(QtCore.QThread):
    progress = QtCore.pyqtSignal(dict)  # {"name","line"}
    # {"host_id","name","status","note","distro"?}
    host_done = QtCore.pyqtSignal(dict)
    finished_all = QtCore.pyqtSignal()

    def __init__(self, host_ids: list[int]):
        super().__init__()
        self.host_ids = host_ids

    def run(self):
        import asyncio
        from .core import db, ssh_client

        all_hosts = db.list_hosts()
        hosts = [h for h in all_hosts if h["id"] in self.host_ids]

        async def _job():
            for h in hosts:
                name = h.get("name", "?")
                try:
                    agen = ssh_client.autoremove_host_stream(h)
                    async for msg in agen:
                        if isinstance(msg, dict) and msg.get("type") == "line":
                            self.progress.emit({"name": name, "line": msg["line"]})
                        elif isinstance(msg, dict) and msg.get("type") == "result":
                            res = msg["result"] or {}
                            res.update({"host_id": h["id"], "name": name})
                            self.host_done.emit(res)
                except Exception as ex:
                    self.host_done.emit(
                        {
                            "host_id": h["id"],
                            "name": name,
                            "status": "error",
                            "note": str(ex),
                        }
                    )

        asyncio.run(_job())
        self.finished_all.emit()


class _RebootWorker(QtCore.QThread):
    host_done = QtCore.pyqtSignal(dict)
    finished_all = QtCore.pyqtSignal()

    def __init__(self, host_ids: list[int] | None = None):
        super().__init__()
        self.host_ids = host_ids or []

    def run(self):
        from .core import db, ssh_client
        import asyncio
        import traceback

        all_hosts = db.list_hosts()
        hosts = [h for h in all_hosts if not self.host_ids or h["id"] in self.host_ids]

        async def _job():
            for h in hosts:
                try:
                    res = await ssh_client.reboot_host(h)
                    res.setdefault("host_id", h["id"])
                    self.host_done.emit(res)
                except Exception as ex:
                    self.host_done.emit(
                        {
                            "host_id": h["id"],
                            "name": h.get("name", "?"),
                            "status": "error",
                            "note": f"Reboot-Fehler: {ex}",
                        }
                    )

        # Eigener Event-Loop pro Thread (stabil in Py 3.13)
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_job())
        finally:
            loop.close()

        self.finished_all.emit()
