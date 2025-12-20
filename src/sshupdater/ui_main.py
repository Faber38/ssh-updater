import os
import platform
import socket
import shutil
import subprocess
from pathlib import Path
from datetime import datetime, timedelta

from PyQt6 import QtWidgets, QtGui, QtCore

from sshupdater.core import settings

# Optional nur für Windows-Infos (auf Linux nicht nötig)
try:
    import ctypes
    import psutil
except Exception:
    ctypes = None
    psutil = None


class SysInfoWidget(QtWidgets.QFrame):
    """Zeigt lokale Systeminformationen an (Host, OS, Kernel, Uptime, Load, RAM, Disk, IPs)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        self.setFrameShadow(QtWidgets.QFrame.Shadow.Plain)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

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

        title = QtWidgets.QLabel("Systeminfo (lokal)")
        title.setProperty("class", "title")
        lay.addWidget(title)

        grid = QtWidgets.QGridLayout()
        grid.setVerticalSpacing(4)
        grid.setHorizontalSpacing(10)
        lay.addLayout(grid)

        def kv_row(row: int, key_text: str) -> QtWidgets.QLabel:
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

        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(5000)

        self.refresh()

    # -------- Linux Helpers --------
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

            def _kb(v: str) -> int:
                return int(v.split()[0])

            total = _kb(kv["MemTotal"]) * 1024
            avail = _kb(kv.get("MemAvailable", kv["MemFree"])) * 1024
            used = total - avail

            def fmt(b: float) -> str:
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
            total, used, _free = shutil.disk_usage("/")

            def fmt_gib(b: int) -> str:
                return f"{b / 1024**3:.1f} GiB"

            pct = used / total * 100 if total else 0
            return f"{fmt_gib(used)} / {fmt_gib(total)}  ({pct:.0f} %)"
        except Exception:
            return "–"

    def _ips_str(self) -> str:
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

    # -------- Windows Helpers --------
    @staticmethod
    def _windows_uptime_str() -> str:
        if ctypes is None:
            return "–"
        try:
            GetTickCount64 = ctypes.windll.kernel32.GetTickCount64
            GetTickCount64.restype = ctypes.c_ulonglong
            ms = GetTickCount64()
            return str(timedelta(milliseconds=ms))
        except Exception:
            return "–"

    @staticmethod
    def _windows_ram_str() -> str:
        if psutil is None:
            return "–"
        try:
            mem = psutil.virtual_memory()
            used = mem.used / (1024**3)
            total = mem.total / (1024**3)
            return f"{used:.1f} / {total:.1f} GB ({mem.percent:.0f}%)"
        except Exception:
            return "–"

    @staticmethod
    def _windows_ips_str() -> str:
        if psutil is None:
            return "–"
        try:
            addrs = psutil.net_if_addrs()
            ip_list = []
            for iface, entries in addrs.items():
                for e in entries:
                    if e.family == socket.AF_INET:
                        ip_list.append(f"{iface}: {e.address}")
            return ", ".join(ip_list) if ip_list else "–"
        except Exception:
            return "–"

    @staticmethod
    def _windows_cpu_load_str() -> str:
        if psutil is None:
            return "–"
        try:
            # interval=None blockiert nicht die UI
            return f"{psutil.cpu_percent(interval=None):.1f}%"
        except Exception:
            return "–"

    def refresh(self):
        try:
            host = socket.gethostname()
        except Exception:
            host = "–"

        system = platform.system()

        # ---------------------------
        # LINUX
        # ---------------------------
        if system == "Linux":
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

        # ---------------------------
        # WINDOWS
        # ---------------------------
        elif system == "Windows":
            os_name = platform.platform()
            kernel = platform.version()
            uptime = self._windows_uptime_str()
            load = self._windows_cpu_load_str()
            mem = self._windows_ram_str()
            disk = (
                self._disk_root_str()
            )  # shutil.disk_usage("/") klappt i.d.R. auch auf Windows
            ips = self._windows_ips_str()
            ssh_state = "nicht verfügbar"

        # ---------------------------
        # UNBEKANNT
        # ---------------------------
        else:
            os_name = system
            kernel = platform.release()
            uptime = "–"
            load = "–"
            mem = "–"
            disk = "–"
            ips = "–"
            ssh_state = "–"

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

        self.act_toggle_checks = QtGui.QAction("Haken", self)
        self.act_toggle_checks.setToolTip("Alle auswählen/abwählen")
        self.act_toggle_checks.setCheckable(True)
        self.act_toggle_checks.setIcon(self._make_dot_icon("#3a7cec"))

        for a in (
            self.act_check,
            self.act_sim,
            self.act_upg,
            self.act_clean,
            self.act_reboot,
            self.act_config,
        ):
            tb.addAction(a)
        tb.addSeparator()
        tb.addAction(self.act_toggle_checks)

        # --- Autor-Hinweis rechts in der Toolbar ---
        spacer = QtWidgets.QWidget()
        spacer.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        tb.addWidget(spacer)

        self.userLabel = QtWidgets.QLabel(" © Faber38 / © CalimerO")
        self.userLabel.setObjectName("userLabel")
        self.userLabel.setStyleSheet(
            "font-size: 10pt; font-weight: bold; padding-right: 10px;"
        )
        tb.addWidget(self.userLabel)

        # Klick-Handler
        self.act_config.triggered.connect(self._open_config)
        self.act_check.triggered.connect(self._on_check)
        self.act_sim.triggered.connect(self._on_sim)
        self.act_upg.triggered.connect(self._on_upgrade)
        self.act_clean.triggered.connect(self._on_clean)
        self.act_reboot.triggered.connect(self._on_reboot)
        self.act_toggle_checks.toggled.connect(self._on_toggle_checks)

        # ---- Splitter links/rechts
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)

        left = SysInfoWidget()
        left.setMinimumWidth(260)
        left.setMaximumWidth(600)

        # Tabelle
        self.table = QtWidgets.QTableView()
        self._reload_hosts()
        self.table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.SingleSelection
        )

        # Log
        self.log = QtWidgets.QTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("Logs …")

        # ✅ Rechter Bereich als vertikaler Splitter (Tabelle/Log verschiebbar)
        self.right_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.right_splitter.addWidget(self.table)
        self.right_splitter.addWidget(self.log)
        self.right_splitter.setSizes([420, 240])

        splitter.addWidget(left)
        splitter.addWidget(self.right_splitter)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([320, 900])

        self.setCentralWidget(splitter)

        # ---- Settings / Restore
        self._qset = QtCore.QSettings("Faber38", "SSH Updater")

        geom = self._qset.value("win/geometry", None)
        if geom is not None:
            self.restoreGeometry(geom)

        sizes_lr = self._qset.value("ui/splitter_sizes", None)
        if sizes_lr:
            try:
                self.centralWidget().setSizes([int(s) for s in sizes_lr])
            except Exception:
                pass

        sizes_r = self._qset.value("ui/right_splitter_sizes", None)
        if sizes_r:
            try:
                self.right_splitter.setSizes([int(s) for s in sizes_r])
            except Exception:
                pass

        self._apply_theme()
        self.statusBar().showMessage("Bereit")

    def _get_selected_host_ids(self) -> list:
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
                name_item = model.item(r, 1)
                hid = name_item.data(QtCore.Qt.ItemDataRole.UserRole)
                if hid is not None:
                    ids.append(int(hid))
        return ids

    def _apply_theme(self):
        q_theme = QtCore.QSettings("Faber38", "SSH Updater").value("ui/theme", None)
        theme = (q_theme or settings.THEME or "standard").lower()

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
            self.setStyleSheet("")

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
        if not online:
            return self._make_dot_icon("#e23b3b")
        if updates is None:
            return self._make_dot_icon("#9e9e9e")
        return self._make_dot_icon("#3ac569" if updates == 0 else "#f2b84b")

    # ========= Prüfen =========
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

            model.setItem(row, 5, status_item)

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            model.setItem(row, 6, QtGui.QStandardItem(timestamp))

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
                        self.log.append(f"... ({len(lines) - 20} weitere Zeilen)\n")
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
        ret = QtWidgets.QMessageBox.question(
            self,
            "Upgrade starten",
            "Alle gelisteten Hosts jetzt upgraden?\n\n"
            "Hinweis: Es werden Paket-Upgrades per sudo -n ausgeführt.\n"
            "Stelle sicher, dass NOPASSWD für die Paketbefehle konfiguriert ist.",
        )
        if ret != QtWidgets.QMessageBox.StandardButton.Yes:
            return

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
        self.log.append(f"{payload['name']}: {payload['line']}")
        self.log.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def _on_upgrade_host_done(self, res: dict):
        if res.get("status") == "ok":
            self.log.append(
                f"✅ {res['name']}: Upgrade abgeschlossen ({res.get('distro', '?')})."
            )
            row = self._find_row_by_host_id(res.get("host_id"))
            if row >= 0:
                model = self.table.model()
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                model.setItem(row, 5, QtGui.QStandardItem("Online – 0 Updates"))
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

    # ========= Bereinigen =========
    def _on_clean(self):
        selected = self._get_selected_host_ids()
        if not selected:
            QtWidgets.QMessageBox.information(
                self, "Keine Auswahl", "Bitte Hosts anhaken."
            )
            return
        self._clean_selected = selected

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
                            else f"\n... ({len(lines) - 20} weitere Zeilen)\n"
                        )
                    )
        else:
            self.log.append(f"✖ {res.get('name', '?')}: {res.get('note', 'Fehler')}")
        self.log.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def _on_clean_sim_done(self):
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
        selected = self._get_selected_host_ids()
        if not selected:
            QtWidgets.QMessageBox.information(
                self, "Keine Auswahl", "Bitte Hosts anhaken."
            )
            return

        ret = QtWidgets.QMessageBox.question(
            self,
            "Reboot ausführen",
            f"Sollen {len(selected)} ausgewählte Host(s) neu gestartet werden?\nHinweis: Der SSH-Stream bricht ggf. sofort ab.",
        )
        if ret != QtWidgets.QMessageBox.StandardButton.Yes:
            return

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
            chk = QtGui.QStandardItem()
            chk.setCheckable(True)
            chk.setCheckState(QtCore.Qt.CheckState.Unchecked)
            chk.setEditable(False)

            name = QtGui.QStandardItem(h.get("name") or "")
            name.setData(h["id"], QtCore.Qt.ItemDataRole.UserRole)

            ip = QtGui.QStandardItem(h.get("primary_ip") or "")
            user = QtGui.QStandardItem(h.get("user") or "")
            auth = QtGui.QStandardItem(h.get("auth_method") or "")

            pending = h.get("pending_updates")
            if pending is None:
                status = QtGui.QStandardItem("—")
                status.setIcon(self._status_icon_for(True, None))
            else:
                status = QtGui.QStandardItem(f"Online – {int(pending)} Updates")
                status.setIcon(self._status_icon_for(True, int(pending)))

            last_item = QtGui.QStandardItem(h.get("last_check") or "—")

            for it in (name, ip, user, auth, status, last_item):
                it.setEditable(False)

            model.appendRow([chk, name, ip, user, auth, status, last_item])

        self.table.setModel(model)
        self.table.resizeColumnsToContents()
        self.table.setColumnWidth(0, 30)
        self._sync_toggle_action()

    def closeEvent(self, event):
        try:
            self._qset.setValue("win/geometry", self.saveGeometry())

            splitter = self.centralWidget()
            if isinstance(splitter, QtWidgets.QSplitter):
                self._qset.setValue("ui/splitter_sizes", splitter.sizes())

            if hasattr(self, "right_splitter"):
                self._qset.setValue(
                    "ui/right_splitter_sizes", self.right_splitter.sizes()
                )
        finally:
            super().closeEvent(event)

    def _set_all_checks(self, state: bool):
        model = self.table.model()
        if not model:
            return
        target = (
            QtCore.Qt.CheckState.Checked if state else QtCore.Qt.CheckState.Unchecked
        )
        for r in range(model.rowCount()):
            item = model.item(r, 0)
            if item is not None:
                item.setCheckState(target)
        self._sync_toggle_action()

    def _are_all_checked(self) -> bool:
        model = self.table.model()
        if not model or model.rowCount() == 0:
            return False
        for r in range(model.rowCount()):
            item = model.item(r, 0)
            if item is None or item.checkState() != QtCore.Qt.CheckState.Checked:
                return False
        return True

    def _on_toggle_checks(self, checked: bool):
        self._set_all_checks(checked)

    def _sync_toggle_action(self):
        if hasattr(self, "act_toggle_checks"):
            self.act_toggle_checks.blockSignals(True)
            self.act_toggle_checks.setChecked(self._are_all_checked())
            self.act_toggle_checks.blockSignals(False)


class _CheckWorker(QtCore.QThread):
    one_result = QtCore.pyqtSignal(dict)
    finished_all = QtCore.pyqtSignal()

    def __init__(self, host_ids: list | None = None):
        super().__init__()
        self.host_ids = host_ids

    def run(self):
        import asyncio
        from .core import db, ssh_client

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
                res = await ssh_client.check_updates_for_host(h)
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
    progress = QtCore.pyqtSignal(dict)
    host_done = QtCore.pyqtSignal(dict)
    finished_all = QtCore.pyqtSignal()

    def __init__(self, host_ids: list | None = None):
        super().__init__()
        self.host_ids = host_ids

    def run(self):
        import asyncio
        from .core import db, ssh_client

        all_hosts = db.list_hosts()
        hosts = [h for h in all_hosts if not self.host_ids or h["id"] in self.host_ids]

        async def _job():
            for h in hosts:
                name = h.get("name", "?")
                if not h.get("primary_ip") or not h.get("user"):
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
    progress = QtCore.pyqtSignal(dict)
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

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_job())
        finally:
            loop.close()

        self.finished_all.emit()
