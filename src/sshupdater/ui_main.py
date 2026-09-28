import os
import platform
import socket
import shutil
import subprocess
import ipaddress
import asyncio
from copy import deepcopy

from pathlib import Path
from datetime import datetime, timedelta

from PyQt6 import QtWidgets, QtGui, QtCore

from sshupdater.core import settings
from .ui_text import PlainTextLog, PlainMessageBox
from .ui_docker_preview import DockerUpdatePreviewDialog
from .docker_plan import build_plan, selection
from .ui_docker import PROJECT_ROLE, DETAILS_ROLE, DockerHostTable, DockerDetailsDialog, details_available, engine_version


def _docker_status_item(discovery=None):
    """Format only this check's discovery; never infer package update status."""
    if not discovery:
        text = "Nicht geprüft"
    elif discovery.get("status") == "docker_missing":
        text = "—"
    elif discovery.get("status") == "permission_denied":
        text = "⚠ Keine Berechtigung"
    elif discovery.get("status") in ("ok", "compose_missing"):
        raw = discovery.get("docker_version") or ""
        version = engine_version(raw)
        text = f"🐳 Docker {version}" if version else "🐳 Docker (Version unbekannt)"
        if discovery.get("status") == "compose_missing":
            text += " – Compose fehlt"
    else:
        text = "⚠ Docker-Fehler"
    item = QtGui.QStandardItem(text)
    item.setEditable(False)
    if details_available(discovery):
        item.setData(True, DETAILS_ROLE)
        item.setToolTip('Docker-Details anzeigen' if discovery.get('status') == 'ok'
                        else 'Docker-Fehlerdetails anzeigen')
        font = item.font()
        font.setUnderline(True)
        item.setFont(font)
    return item

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
            lab_v.setTextFormat(QtCore.Qt.TextFormat.PlainText)
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

        # psutil CPU-Percent "primen", sonst beim ersten refresh manchmal 0.0%
        if psutil is not None:
            try:
                psutil.cpu_percent(interval=None)
            except Exception:
                pass

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
                ["ip", "-4", "addr"], text=True, errors="ignore", timeout=2
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

    @staticmethod
    def _fmt_uptime(td: timedelta) -> str:
        total = int(td.total_seconds())
        days, rem = divmod(total, 86400)
        hrs, rem = divmod(rem, 3600)
        mins, _secs = divmod(rem, 60)
        if days:
            return f"{days}d {hrs:02}h {mins:02}m"
        return f"{hrs:02}h {mins:02}m"

    # -------- Windows Helpers --------
    @staticmethod
    def _windows_uptime_str() -> str:
        if ctypes is None:
            return "–"
        try:
            GetTickCount64 = ctypes.windll.kernel32.GetTickCount64
            GetTickCount64.restype = ctypes.c_ulonglong
            ms = GetTickCount64()
            return SysInfoWidget._fmt_uptime(timedelta(milliseconds=int(ms)))

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
            stats = psutil.net_if_stats()

            ip_list = []
            seen = set()

            for iface, entries in addrs.items():
                st = stats.get(iface)
                if st is not None and not st.isup:
                    continue

                for e in entries:
                    if e.family != socket.AF_INET:
                        continue
                    ip = e.address

                    # Filter
                    try:
                        ip_obj = ipaddress.ip_address(ip)
                        if ip_obj.is_loopback or ip_obj.is_link_local:
                            continue
                    except Exception:
                        continue

                    key = (iface, ip)
                    if key in seen:
                        continue
                    seen.add(key)

                    ip_list.append(f"{iface}: {ip}")

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

    @staticmethod
    def _detect_platform() -> str:
        sysname = platform.system()  # Windows, Linux, Darwin
        if sysname == "Linux":
            # WSL erkennen
            try:
                pv = (
                    Path("/proc/version")
                    .read_text(encoding="utf-8", errors="ignore")
                    .lower()
                )
                if "microsoft" in pv or "wsl" in pv:
                    return "WSL"
            except Exception:
                pass
            return "Linux"
        return sysname

    @staticmethod
    def _windows_disk_root_str() -> str:
        try:
            drive = os.environ.get("SystemDrive", "C:") + "\\"
            total, used, _free = shutil.disk_usage(drive)
            pct = used / total * 100 if total else 0
            return f"{used / 1024**3:.1f} / {total / 1024**3:.1f} GiB ({pct:.0f} %)"
        except Exception:
            return "–"

    def refresh(self):
        try:
            host = socket.gethostname()
        except Exception:
            host = "–"

        system = self._detect_platform()

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
                    timeout=2,
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
            disk = self._windows_disk_root_str()
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

        self.lab_host.setText(f"Hostname: {host}")
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
        from .core.registry_session import RegistrySession
        self._registry_session = RegistrySession()
        self._docker_results = {}
        self._docker_connections = {}
        self._docker_context_versions = {}
        self._docker_result_versions = {}
        self._docker_plan = None
        self._preflight_passed = None
        self._docker_pull_state = None
        self._docker_apply_result = None
        self._docker_verification_result = None
        self._preflight_rejected = {}
        self._preflight_busy = False
        self._rebuilding_hosts = False
        self.setWindowTitle("SSH Updater")
        self.resize(1100, 680)

        # Toolbar
        from .ui_resources import ContainerToolBar
        tb = ContainerToolBar("Main")
        tb.setIconSize(QtCore.QSize(18, 18))
        self.addToolBar(tb)

        self.act_check = QtGui.QAction("Prüfen", self)
        self.act_sim = QtGui.QAction("Simulieren", self)
        self.act_upg = QtGui.QAction("Upgrade", self)
        self.act_clean = QtGui.QAction("Bereinigen", self)
        self.act_reboot = QtGui.QAction("Reboot", self)
        self.act_config = QtGui.QAction("Konfiguration", self)
        self.act_docker_preview = QtGui.QAction("Update-Vorschau", self)
        self.act_docker_update = QtGui.QAction("Docker-Update", self)
        self.act_docker_preview.setEnabled(False)
        self.act_docker_update.setEnabled(False)
        self.act_docker_preview.setToolTip("Aktionsplan aus der letzten Hostprüfung anzeigen")
        self.act_docker_update.setToolTip("Live-Preflight und gezielter Image-Pull – kein Containerwechsel.")
        self.act_stop = QtGui.QAction("Stopp", self)
        self.act_stop.setToolTip("Lokales Warten beenden; Remote-Zustand anschließend prüfen")
        self.act_stop.setEnabled(False)

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
        from .ui_resources import DockerToolbarMark
        self.docker_toolbar_mark = DockerToolbarMark(tb)
        self.act_docker_mark = tb.addWidget(self.docker_toolbar_mark)
        tb.addAction(self.act_docker_preview)
        tb.addAction(self.act_docker_update)
        tb.container_actions = (self.act_docker_mark, self.act_docker_preview, self.act_docker_update)

        # --- Rechter Bereich der Toolbar ---
        spacer = QtWidgets.QWidget()
        spacer.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        tb.addWidget(spacer)

        # Globale Aktionen rechts, außerhalb der Container-Gruppe
        tb.addSeparator()
        tb.addAction(self.act_stop)
        self.act_help = QtGui.QAction("Hilfe", self)
        self.act_help.triggered.connect(self._open_help)
        tb.addAction(self.act_help)
        tb.addSeparator()

        self.userLabel = QtWidgets.QLabel(" © @Faber38 / © @CalimerO")
        self.userLabel.setObjectName("userLabel")
        self.userLabel.setStyleSheet(
            "font-size: 10pt; font-weight: bold; padding-right: 10px;"
        )
        tb.addWidget(self.userLabel)

        # Klick-Handler
        self.act_docker_update.triggered.connect(self._start_docker_preflight)
        self.act_docker_preview.triggered.connect(self._open_docker_preview)
        self.act_config.triggered.connect(self._open_config)
        self.act_check.triggered.connect(self._on_check)
        self.act_sim.triggered.connect(self._on_sim)
        self.act_upg.triggered.connect(self._on_upgrade)
        self.act_clean.triggered.connect(self._on_clean)
        self.act_reboot.triggered.connect(self._on_reboot)
        self.act_stop.triggered.connect(self._on_stop_requested)
        self.act_toggle_checks.toggled.connect(self._on_toggle_checks)

        # ---- Splitter links/rechts
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)

        left = SysInfoWidget()
        left.setMinimumWidth(260)
        left.setMaximumWidth(600)

        # Tabelle
        self.table = DockerHostTable()
        self.table.docker_clicked.connect(self._open_docker_details)
        self._reload_hosts()
        self.table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.SingleSelection
        )

        # Log
        self.log = PlainTextLog()
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

        from sshupdater import __version__

        self.setWindowTitle(f"SSH Updater v{__version__}")
        self.statusBar().showMessage("Bereit")

    def _prepare_passwords(self, host_ids):
        from .ui_config import confirm_legacy_passwords
        from .core import db
        try:
            if confirm_legacy_passwords(self, host_ids):
                return True
        except (OSError, ValueError, db.sqlite3.Error) as exc:
            PlainMessageBox.warning(self, 'SSH-Passwort nicht verfügbar', str(exc))
        for action in (self.act_check, self.act_sim, self.act_upg,
                       self.act_clean, self.act_reboot, self.act_config):
            action.setEnabled(True)
        self.act_stop.setEnabled(False)
        self.log.append('Aktion abgebrochen; bestehende Passwörter wurden nicht automatisch geändert.')
        return False

    def _docker_preview_hosts(self):
        hosts = []
        model = self.table.model()
        for row in range(model.rowCount()):
            parent = model.item(row, 0)
            name = model.item(row, 1)
            host_id = name.data(QtCore.Qt.ItemDataRole.UserRole)
            discovery = self._docker_results.get(host_id) or {}
            if discovery.get('status') != 'ok':
                continue
            selected = {parent.child(i, 0).data(PROJECT_ROLE) for i in range(parent.rowCount())
                        if parent.child(i, 0).checkState() == QtCore.Qt.CheckState.Checked}
            projects = [p for p in discovery.get('projects', []) if
                        (p.get('name'), tuple(p.get('config_files') or []), p.get('config_files_raw')) in selected]
            if projects:
                context = tuple(self._docker_connections.get(host_id, ())[:5]) + (self._docker_context_versions.get(host_id, 0),)
                hosts.append(dict(host_id=host_id, name=name.text(), connection_identity=context,
                                  docker_version=discovery.get('docker_version'),
                                  compose_version=discovery.get('compose_version'),
                                  result_revision=self._docker_result_versions.get(host_id, 0), projects=projects))
        return hosts

    def _prepared_docker_apply(self):
        """Return only a completely prepared Phase-4b state; never table selection."""
        state = self._docker_pull_state
        if not state:
            return None
        result, plan = state['result'], state['plan']
        if (result.get('status') != 'pulled' or not result.get('apply_pending')
                or not result.get('mutation_attempted') or not plan.candidates):
            return None
        expected = {(p.host_id, p.name, image[0], image[3], image[4])
                    for p in plan.candidates for image in p.images}
        rows = result.get('pulls', [])
        actual = {(r.get('host_id'), r.get('project'), r.get('service'),
                   r.get('image'), r.get('platform')) for r in rows}
        if (not expected or actual != expected or len(rows) != len(expected)
                or any(r.get('status') != 'pulled' or r.get('exit_code') != 0 for r in rows)):
            return None
        return state

    def _invalidate_docker_plan(self):
        self._preflight_passed = None
        if self._preflight_busy and hasattr(self, 'preflight_worker'):
            self.preflight_worker.request_stop()
        self._docker_plan = None
        self.act_docker_update.setEnabled(self._prepared_docker_apply() is not None and not self._preflight_busy)

    def _open_docker_preview(self):
        if self._prepared_docker_apply() is not None or self._verification_pending() or self._preflight_busy:
            return
        self._invalidate_docker_plan()
        hosts = self._docker_preview_hosts()
        if hosts:
            plan = build_plan(hosts)
            dialog = DockerUpdatePreviewDialog(self, hosts)
            dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
            dialog.show()
            self._docker_plan = plan if plan.candidates else None
            self._sync_docker_actions()

    def _sync_docker_actions(self, *args):
        """Offer the next operation of the RAM-only update cycle."""
        if self._rebuilding_hosts:
            return
        model = self.table.model()
        selected = False
        if model is not None:
            for row in range(model.rowCount()):
                parent = model.item(row, 0)
                if any(parent.child(child, 0).checkState() == QtCore.Qt.CheckState.Checked
                       for child in range(parent.rowCount())):
                    selected = True
                    break
        prepared = self._prepared_docker_apply() is not None
        self.act_docker_preview.setEnabled(selected and not self._preflight_busy and not prepared and not self._verification_pending())
        if self._docker_plan is not None:
            if (selection(self._docker_preview_hosts()) != self._docker_plan.selection
                    or any(self._preflight_rejected.get(p.host_id) == p.revision
                           for p in self._docker_plan.selection)):
                self._invalidate_docker_plan()
        self.act_docker_update.setText('Docker prüfen' if self._verification_pending() else
                                      'Docker anwenden' if prepared else 'Docker-Update')
        self.act_docker_update.setToolTip(
            'Laufenden Container und Registry read-only prüfen; Registry-Backoff wird respektiert.' if self._verification_pending() else
            'Geladene Docker-Images auf die vorbereiteten Compose-Services anwenden.' if prepared else
            'Live-Preflight und gezielter Image-Pull – kein Containerwechsel.')
        self.act_docker_update.setEnabled((prepared or self._docker_plan is not None or self._verification_pending()) and not self._preflight_busy)

    def _start_docker_preflight(self):
        from .core import db, docker_preflight
        if self._preflight_busy:
            return
        self._sync_docker_actions()
        if self._verification_pending():
            self._start_docker_verification()
            return
        if self._prepared_docker_apply() is not None:
            self._start_docker_apply()
            return
        if self._docker_plan is None:
            return
        # Do not overlap a preflight with any existing system worker.
        if any(isinstance(getattr(self, name, None), QtCore.QThread)
               and getattr(self, name).isRunning() for name in
               ('worker', 'sim_worker', 'upg_worker', 'clean_sim_worker', 'clean_run_worker', 'reboot_worker')):
            self.statusBar().showMessage('Bitte laufende Hostaktion zuerst beenden.')
            return
        try:
            host_ids = {p.host_id for p in self._docker_plan.candidates}
            hosts = {h['id']: h for h in db.list_hosts() if h['id'] in host_ids}
            if set(hosts) != host_ids or any(
                    tuple(h.get(k) for k in ('primary_ip', 'user', 'port', 'auth_method', 'key_path', 'password_enc'))
                    != self._docker_connections.get(hid) for hid, h in hosts.items()):
                raise ValueError('changed')
        except Exception:
            for project in self._docker_plan.selection:
                self._preflight_rejected[project.host_id] = project.revision
            self._invalidate_docker_plan()
            self._show_preflight_result(docker_preflight.failed('host'))
            return
        self._preflight_passed = None
        self._preflight_busy = True
        self._preflight_actions = {a: a.isEnabled() for a in (
            self.act_check, self.act_sim, self.act_upg, self.act_clean, self.act_reboot, self.act_config)}
        for action in self._preflight_actions:
            action.setEnabled(False)
        self.act_stop.setEnabled(True)
        self._sync_docker_actions()
        self.statusBar().showMessage('Docker-Preflight …')
        self.preflight_worker = _DockerPreflightWorker(self._docker_plan, hosts)
        self.preflight_worker.progress.connect(self.statusBar().showMessage)
        self.preflight_worker.finished.connect(self._on_preflight_done)
        self.preflight_worker.start()

    def _on_preflight_done(self):
        worker = self.preflight_worker
        self._preflight_busy = False
        result = worker.result
        self._sync_docker_actions()
        if worker.stop_requested and result.get('status') != 'cancelled':
            result = worker.pull_run.interrupted()
        elif self._docker_plan is not worker.plan:
            result = dict(result, status='failed', reason='plan',
                          note='Plan zwischenzeitlich geändert. Weitere Schritte wurden gestoppt.')
        # Consume the approval, including after partial/unknown remote effects.
        for project in worker.plan.selection:
            self._preflight_rejected[project.host_id] = project.revision
        if result.get('mutation_attempted'):
            self._docker_pull_state = dict(plan=worker.plan, result=deepcopy(result))
        self._invalidate_docker_plan()
        for action, enabled in self._preflight_actions.items():
            action.setEnabled(enabled)
        self.act_stop.setEnabled(False)
        self._sync_docker_actions()
        self.statusBar().showMessage('Image geladen – Apply ausstehend' if result['status'] == 'pulled'
                                    else 'Docker-Image-Pull/Preflight nicht erfolgreich')
        if not getattr(self, '_closing', False):
            self._show_preflight_result(result)

    def _verification_pending(self):
        return bool(self._docker_apply_result and self._docker_apply_result.get('verification_pending'))

    def _start_docker_apply(self):
        from .core import db
        state = self._prepared_docker_apply()
        if state is None or self._preflight_busy or self._verification_pending():
            return
        if any(isinstance(getattr(self, name, None), QtCore.QThread)
               and getattr(self, name).isRunning() for name in
               ('worker', 'sim_worker', 'upg_worker', 'clean_sim_worker', 'clean_run_worker', 'reboot_worker')):
            self.statusBar().showMessage('Bitte laufende Hostaktion zuerst beenden.')
            return
        # Consume the prepared capability before starting; never retry automatically.
        self._docker_pull_state = None
        self._docker_plan = None
        self._docker_apply_result = None
        try:
            host_ids = {p.host_id for p in state['plan'].candidates}
            hosts = {h['id']: h for h in db.list_hosts() if h['id'] in host_ids}
            if set(hosts) != host_ids or any(
                    tuple(h.get(k) for k in ('primary_ip', 'user', 'port', 'auth_method', 'key_path', 'password_enc'))
                    != self._docker_connections.get(hid) for hid, h in hosts.items()):
                raise ValueError('host')
        except Exception:
            self._docker_apply_result = dict(status='failed', verification_pending=False,
                note='Host-Verbindungskontext verändert. Bitte Host erneut prüfen und neue Vorschau erstellen.', applies=[])
            self._sync_docker_actions()
            self._show_apply_result(self._docker_apply_result)
            return
        self._preflight_busy = True
        self._preflight_actions = {a: a.isEnabled() for a in (
            self.act_check, self.act_sim, self.act_upg, self.act_clean, self.act_reboot, self.act_config)}
        for action in self._preflight_actions:
            action.setEnabled(False)
        self.act_stop.setEnabled(True)
        self._sync_docker_actions()
        self.statusBar().showMessage('Docker-Apply-Precheck …')
        self.preflight_worker = _DockerApplyWorker(state, hosts)
        self.preflight_worker.progress.connect(self.statusBar().showMessage)
        self.preflight_worker.finished.connect(self._on_apply_done)
        self.preflight_worker.start()

    def _on_apply_done(self):
        worker = self.preflight_worker
        result = worker.apply_run.interrupted() if worker.stop_requested else worker.result
        self._docker_apply_result = deepcopy(result)
        self._docker_verification_result = None
        self._preflight_busy = False
        for action, enabled in self._preflight_actions.items():
            action.setEnabled(enabled)
        self.act_stop.setEnabled(False)
        self._sync_docker_actions()
        self.statusBar().showMessage('Apply erfolgreich – Verifikation ausstehend' if self._verification_pending()
                                    else 'Docker-Apply nicht erfolgreich')
        if not getattr(self, '_closing', False):
            self._show_apply_result(result)

    def _start_docker_verification(self):
        from .core import db
        if not self._verification_pending() or self._preflight_busy:
            return
        if any(isinstance(getattr(self, name, None), QtCore.QThread)
               and getattr(self, name).isRunning() for name in
               ('worker', 'sim_worker', 'upg_worker', 'clean_sim_worker', 'clean_run_worker', 'reboot_worker')):
            self.statusBar().showMessage('Bitte laufende Hostaktion zuerst beenden.')
            return
        try:
            hosts = {h['id']: h for h in db.list_hosts() if
                     tuple(h.get(k) for k in ('primary_ip', 'user', 'port', 'auth_method', 'key_path', 'password_enc'))
                     == self._docker_connections.get(h['id'])}
            for row in self._docker_apply_result['applies']:
                if not self._verification_context_matches(row):
                    hosts.pop(row['host_id'], None)
        except Exception:
            hosts = {}  # Report local context failure without losing the apply outcome.
        self._preflight_busy = True
        self._preflight_actions = {a: a.isEnabled() for a in (
            self.act_check, self.act_sim, self.act_upg, self.act_clean, self.act_reboot, self.act_config)}
        for action in self._preflight_actions:
            action.setEnabled(False)
        self.act_stop.setEnabled(True)
        self._sync_docker_actions()
        self.statusBar().showMessage('Docker prüfen …')
        self.preflight_worker = _DockerVerificationWorker(self._docker_apply_result, hosts,
            self._registry_session, self._docker_verification_result)
        self.preflight_worker.progress.connect(self.statusBar().showMessage)
        self.preflight_worker.finished.connect(self._on_verification_done)
        self.preflight_worker.start()

    def _verification_context_matches(self, row):
        identity = tuple(self._docker_connections.get(row['host_id'], ())[:5]) + (
            self._docker_context_versions.get(row['host_id'], 0),)
        return identity == tuple(row.get('connection', ()))

    def _on_verification_done(self):
        worker = self.preflight_worker
        result = worker.verification_run.interrupted() if worker.stop_requested else worker.result
        result = deepcopy(result)
        for row in result['projects']:
            if not self._verification_context_matches(row):
                row.update(status='local_changed', reason='host', note='Apply war erfolgreich; Host-Verbindungskontext inzwischen geändert.')
                row.pop('image_updates', None)
                result.update(completed=False, verification_pending=True, status='pending')
        self._docker_verification_result = result
        self.table.remember_projects()
        self._rebuilding_hosts = True
        try:
            for row in result['projects']:
                discovery = self._docker_results.get(row['host_id'])
                if not discovery:
                    continue
                project = next((p for p in discovery.get('projects', [])
                                if p['name'] == row['project'] and tuple(p.get('config_files', [])) == tuple(row['paths'])), None)
                if project is None:
                    continue
                if row.get('image_updates'):
                    project['image_updates'] = deepcopy(row['image_updates'])
                else:
                    from .core import docker_image_updates as images
                    project['image_updates'] = images.project_result([
                        images.ImageCheck().fail(images.CheckFailure('context'))])
                self._docker_result_versions[row['host_id']] = self._docker_result_versions.get(row['host_id'], 0) + 1
                index = self._find_row_by_host_id(row['host_id'])
                if index >= 0:
                    self.table.populate_projects(row['host_id'], self.table.model().item(index, 0), discovery)
        finally:
            self._rebuilding_hosts = False
        if result['completed']:
            self._docker_pull_state = None
            self._docker_apply_result = None
            self._preflight_passed = None
        self._docker_plan = None
        self._preflight_busy = False
        for action, enabled in self._preflight_actions.items():
            action.setEnabled(enabled)
        self.act_stop.setEnabled(False)
        self._sync_docker_actions()
        self.statusBar().showMessage('Docker-Update vollständig verifiziert' if result['status'] == 'verified' else
                                    'Apply erfolgreich – Ergebnis der Abschlussprüfung beachten')
        if not getattr(self, '_closing', False):
            self._show_verification_result(result)

    def _show_verification_result(self, result):
        dialog = QtWidgets.QDialog(self)
        dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle('Docker-Update erfolgreich' if result['status'] == 'verified' else 'Docker-Apply erfolgreich – Abschlussprüfung')
        dialog.resize(800, 550)
        layout = QtWidgets.QVBoxLayout(dialog)
        text = QtWidgets.QPlainTextEdit()
        text.setReadOnly(True)
        lines = ['Der Docker-Apply war erfolgreich. Die Abschlussprüfung ist read-only.']
        if result['status'] == 'cancelled':
            lines.append('Abschlussprüfung abgebrochen. Verifikation bleibt ausstehend; erneute Prüfung möglich.')
        for row in result['projects']:
            lines.extend(['', f"Host: {row.get('host', row['host_id'])}", f"Projekt: {row['project']}", row['note']])
            for i in row.get('image_updates', {}).get('images', []):
                lines.extend([f"Service: {i['service']}", f"Image: {i['image']}", f"Plattform: {i['platform']}",
                              {'current': '✓ aktuell', 'update_available': '↑ Image-Update'}.get(i['status'], 'Prüfung ausstehend')])
                if i.get('note'):
                    lines.append(i['note'])
        if result['status'] == 'verified':
            lines.append('Der laufende Container verwendet das aktualisierte Image. Docker-Update vollständig verifiziert.')
        text.setPlainText('\n'.join(lines))
        layout.addWidget(text)
        buttons = QtWidgets.QDialogButtonBox()
        buttons.addButton('Schließen', QtWidgets.QDialogButtonBox.ButtonRole.RejectRole)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.show()

    def _show_apply_result(self, result):
        dialog = QtWidgets.QDialog(self)
        dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle('Docker-Apply erfolgreich' if result['status'] == 'apply_succeeded' else 'Docker-Apply nicht erfolgreich')
        dialog.resize(760, 500)
        layout = QtWidgets.QVBoxLayout(dialog)
        text = QtWidgets.QPlainTextEdit()
        text.setReadOnly(True)
        lines = [result['note']]
        statuses = {'apply_succeeded': 'Angewendet – Verifikation ausstehend',
                    'unknown': 'Containerzustand nicht bestätigt', 'failed': 'Precheck fehlgeschlagen'}
        for row in result.get('applies', []):
            lines.extend(['', f"Host-ID: {row['host_id']}", f"Projekt: {row['project']}",
                          'Services: ' + ', '.join(row['services']), statuses.get(row['status'], row['status'])])
            for c in row.get('containers', []):
                lines.extend([f"Container: {c['container_id']}", f"Image: {c['image']}", f"Plattform: {c['platform']}"])
        text.setPlainText('\n'.join(lines))
        layout.addWidget(text)
        buttons = QtWidgets.QDialogButtonBox()
        buttons.addButton('Schließen', QtWidgets.QDialogButtonBox.ButtonRole.RejectRole)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.show()

    def _show_preflight_result(self, result):
        dialog = QtWidgets.QDialog(self)
        dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        title = ('Docker Image-Pull erfolgreich' if result['status'] == 'pulled' else
                 'Docker Image-Pull abgebrochen' if result['status'] == 'cancelled' else
                 'Docker Image-Pull / Preflight fehlgeschlagen')
        dialog.setWindowTitle(title)
        dialog.resize(760, 500)
        layout = QtWidgets.QVBoxLayout(dialog)
        text = QtWidgets.QPlainTextEdit()
        text.setReadOnly(True)
        lines = [result['note']]
        statuses = {'pulled': 'Image geladen · Apply ausstehend', 'failed': 'Pull fehlgeschlagen',
                    'unknown': 'Pull-Abschluss unbekannt', 'pulling': 'Pull-Abschluss unbekannt'}
        for row in result.get('pulls', []):
            lines.append(f"\nHost: {row['host']}\nProjekt: {row['project']}\nService: {row['service']}\nImage: {row['image']}\nStatus: {statuses.get(row['status'], row['status'])}")
            if row.get('note'):
                lines.append(row['note'])
        if result['status'] != 'pulled':
            context = result.get('context') or result
            for key, label in (('host_id', 'Host-ID'), ('project', 'Projekt'), ('service', 'Service')):
                if context.get(key) is not None:
                    lines.append(f"{label}: {context[key]}")
            lines.append('Weitere Pulls wurden gestoppt. Vor erneutem Versuch normale Prüfung und neue Vorschau erforderlich.')
        if result.get('mutation_attempted'):
            if result['status'] == 'pulled':
                lines.extend(['', 'Der laufende Container wurde noch nicht aktualisiert.',
                              'Es wurde kein Container neu erstellt oder neu gestartet.',
                              'Das neue Image wurde geladen. Die Container-Aktualisierung steht noch aus.'])
                if self._prepared_docker_apply() is not None:
                    lines.append('Das Update kann jetzt mit „Docker anwenden“ fortgesetzt werden.')
            else:
                lines.extend(['', 'Der lokale Imagebestand kann teilweise verändert worden sein.',
                              'SSH Updater hat keine Containeränderung ausgeführt.',
                              'Ein unterbrochener Remote-Pull kann weiterlaufen. Keine automatische Bereinigung.'])
        else:
            lines.extend(['', 'Kein Pull gestartet. Es wurden keine Änderungen durchgeführt.'])
        text.setPlainText('\n'.join(lines))
        layout.addWidget(text)
        buttons = QtWidgets.QDialogButtonBox()
        buttons.addButton('Schließen', QtWidgets.QDialogButtonBox.ButtonRole.RejectRole)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.show()

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

    def _open_help(self):
        from .ui_help import HelpDialog
        dialog = HelpDialog(self)
        dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()

    def _apply_theme(self):
        from .ui_theme import apply_theme
        apply_theme(QtWidgets.QApplication.instance())

    def _open_docker_details(self, index):
        if index.parent().isValid() or index.column() != 6 or not index.data(DETAILS_ROLE):
            return
        name = self.table.model().item(index.row(), 1)
        result = self._docker_results.get(name.data(QtCore.Qt.ItemDataRole.UserRole))
        if details_available(result):
            dialog = DockerDetailsDialog(self, name.text(), result)
            try:
                dialog.exec()
            finally:
                dialog.deleteLater()

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
            PlainMessageBox.information(
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

        # Results belong to this check only, including hosts not selected again.
        self._invalidate_docker_plan()
        self.table.remember_projects()
        self._docker_results.clear()
        model = self.table.model()
        for row in range(model.rowCount()):
            parent = model.item(row, 0)
            parent.removeRows(0, parent.rowCount())
            model.setItem(row, 6, _docker_status_item())

        if not self._prepare_passwords(selected):
            return
        self.worker = _CheckWorker(selected, registry_session=self._registry_session)
        self.worker.one_result.connect(self._on_check_result)
        self.worker.finished_all.connect(self._on_check_done)
        self.act_stop.setEnabled(True)
        self.worker.start()

    def _on_check_result(self, res: dict):
        host_id = res.get('host_id')
        self._docker_result_versions[host_id] = self._docker_result_versions.get(host_id, 0) + 1
        if self._docker_plan and any(p.host_id == host_id for p in self._docker_plan.selection):
            self._invalidate_docker_plan()
        if res.get("status") == "ok":
            self.log.append(
                f"✔ {res['name']} [{res.get('distro', '?')}]: {res.get('updates', 0)} Updates"
            )
            if res.get("note"):
                self.log.append(res["note"])
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
                status_item = QtGui.QStandardItem(
                    "Remote-Zustand unbekannt" if res.get("status") == "unknown" else "Prüfung fehlgeschlagen")
                status_item.setIcon(self._status_icon_for(False, None))

            model.setItem(row, 5, status_item)
            discovery = res.get("docker_compose")
            if discovery:
                self._docker_results[res["host_id"]] = deepcopy(discovery)
            else:
                self._docker_results.pop(res["host_id"], None)
            self.table.remember_projects()
            model.setItem(row, 6, _docker_status_item(discovery))
            self.table.populate_projects(res["host_id"], model.item(row, 0), discovery)
            self.table.resizeColumnToContents(1)
            self.table.resizeColumnToContents(6)

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            model.setItem(row, 7, QtGui.QStandardItem(timestamp))

            try:
                from .core import db

                db.set_check_result(res["host_id"], timestamp, updates)
            except Exception as e:
                self.statusBar().showMessage(f"Speicherfehler: {e}", 5000)

    def _on_check_done(self):
        fatal_error = getattr(self.worker, "fatal_error", None)
        if fatal_error:
            self.log.append(f"\nPrüfung wegen internem Fehler beendet: {fatal_error}")
        elif getattr(self.worker, "stop_requested", False):
            self.log.append("\nLokales Warten beendet; keine weiteren Hosts gestartet.")
        else:
            self.log.append("\nFertig.")
        self.act_stop.setEnabled(False)
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
            PlainMessageBox.information(
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

        if not self._prepare_passwords(selected):
            return
        self.sim_worker = _SimWorker(selected)
        self.sim_worker.one_result.connect(self._on_sim_result)
        self.sim_worker.finished_all.connect(self._on_sim_done)
        self.act_stop.setEnabled(True)
        self.sim_worker.start()

    def _on_sim_result(self, res: dict):
        if res.get("status") == "ok":
            n = res.get("packages", 0)
            self.log.append(
                f"🧪 {res['name']} [{res.get('distro', '?')}]: {n} Pakete geplant"
            )
            if res.get("note"):
                self.log.append(res["note"])
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
        fatal_error = getattr(self.sim_worker, "fatal_error", None)
        if fatal_error:
            self.log.append(f"\nSimulation wegen internem Fehler beendet: {fatal_error}")
        elif getattr(self.sim_worker, "stop_requested", False):
            self.log.append("\nLokales Warten beendet; keine weiteren Hosts gestartet.")
        else:
            self.log.append("\nFertig.")
        self.act_stop.setEnabled(False)
        for a in (
            self.act_check,
            self.act_sim,
            self.act_upg,
            self.act_clean,
            self.act_reboot,
            self.act_config,
        ):
            a.setEnabled(True)

    def _on_stop_requested(self):
        if self._preflight_busy:
            self.preflight_worker.request_stop()
        active = [getattr(self, name, None) for name in
                  ("worker", "sim_worker", "upg_worker", "clean_sim_worker", "clean_run_worker", "reboot_worker", "preflight_worker")]
        for worker in active:
            if isinstance(worker, _CancellableWorker) and worker.isRunning():
                worker.request_stop()
        self.act_stop.setEnabled(False)
        self.statusBar().showMessage(
            "Lokales Warten wird beendet – Remote-Zustand unbekannt ..."
        )
        self.log.append(
            "\n⏹ Lokales Warten wird beendet. Remote-Zustand unbekannt: "
            "Der Remote-Prozess kann weiterlaufen. Vor erneutem Start am Host prüfen. "
            "Weitere Hosts werden nicht gestartet."
        )
        self.log.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    # ========= Upgraden =========
    def _on_upgrade(self):
        ret = PlainMessageBox.question(
            self,
            "Upgrade starten",
            "Ausgewählte Hosts jetzt upgraden?\n\n"
            "Hinweis: Es werden Paket-Upgrades per sudo -n ausgeführt.\n"
            "Stelle sicher, dass NOPASSWD für die Paketbefehle konfiguriert ist.",
        )
        if ret != PlainMessageBox.StandardButton.Yes:
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
            PlainMessageBox.information(
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

        if not self._prepare_passwords(selected):
            return
        self.upg_worker = _UpgradeWorker(selected)
        self.upg_worker.progress.connect(self._on_upgrade_progress)
        self.upg_worker.host_started.connect(self._on_upgrade_host_started)
        self.upg_worker.host_done.connect(self._on_upgrade_host_done)
        self.upg_worker.finished_all.connect(self._on_upgrade_done)
        self.act_stop.setEnabled(True)
        self.statusBar().showMessage("Upgrade wird gestartet ...")
        self.upg_worker.start()

    def _on_upgrade_host_started(self, payload: dict):
        self.statusBar().showMessage(f"Upgrade läuft: {payload.get('name', '?')} ...")

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
                model.setItem(row, 7, QtGui.QStandardItem(ts))
                try:
                    from .core import db

                    db.set_check_result(res["host_id"], ts, 0)
                except Exception:
                    pass
        else:
            self.log.append(f"❌ {res.get('name', '?')}: {res.get('note', 'Fehler')}")
        self.log.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def _on_upgrade_done(self):
        fatal_error = getattr(self.upg_worker, "fatal_error", None)
        stopped = bool(getattr(self.upg_worker, "stop_requested", False))
        if fatal_error:
            self.log.append(f"\nLokaler Upgrade-Lauf wegen internem Fehler beendet: {fatal_error}")
            self.statusBar().showMessage("Lokaler Upgrade-Lauf mit Fehler beendet", 5000)
        elif stopped:
            self.log.append("\nLokales Warten beendet; Remote-Zustand gegebenenfalls unbekannt.")
            self.statusBar().showMessage("Lokales Warten beendet – Remote-Zustand prüfen", 5000)
        else:
            self.log.append("\nLokaler Upgrade-Lauf beendet; Ergebnisse der einzelnen Hosts beachten.")
            self.statusBar().showMessage("Lokaler Upgrade-Lauf beendet – Ergebnisse prüfen", 5000)
        self.act_stop.setEnabled(False)
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
            PlainMessageBox.information(
                self, "Keine Auswahl", "Bitte Hosts anhaken."
            )
            return
        self._clean_selected = selected
        self._clean_results = {}

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

        if not self._prepare_passwords(selected):
            return
        self.clean_sim_worker = _CleanSimWorker(selected)
        self.clean_sim_worker.one_result.connect(self._on_clean_sim_result)
        self.clean_sim_worker.finished_all.connect(self._on_clean_sim_done)
        self.act_stop.setEnabled(True)
        self.clean_sim_worker.start()

    def _on_clean_sim_result(self, res: dict):
        self._clean_results[res.get("host_id")] = res.get("status")
        if res.get("status") == "ok":
            n = res.get("packages", 0)
            self.log.append(f"🧪 {res['name']}: {n} Pakete würden entfernt.")
            if res.get("note"):
                self.log.append(res["note"])
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
        fatal_error = getattr(self.clean_sim_worker, "fatal_error", None)
        self.act_stop.setEnabled(False)
        stopped = (getattr(self.clean_sim_worker, "stop_requested", False)
                   or getattr(self, '_closing', False))
        if fatal_error or stopped:
            self.log.append(
                ("\nAutoremove-Simulation lokal gestoppt; keine Bereinigung gestartet." if stopped else
                 f"\nAutoremove-Simulation wegen internem Fehler beendet: {fatal_error}")
            )
            self.act_stop.setEnabled(False)
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
            return

        selected = getattr(self, "_clean_selected", [])
        results = getattr(self, "_clean_results", {})
        sel = [hid for hid in selected if results.get(hid) == "ok"]
        excluded = len(selected) - len(sel)
        if excluded:
            self.log.append(f"\n{excluded} Host(s) ohne erfolgreiche Simulation ausgeschlossen. "
                            "Für diese Hosts ist eine neue Simulation erforderlich.")
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

        ret = PlainMessageBox.question(
            self,
            "Autoremove ausführen",
            f"Simulation erfolgreich für {len(sel)} Host(s).\n"
            f"{excluded} Host(s) ausgeschlossen.\n"
            "Jetzt nur auf den erfolgreich simulierten Hosts 'apt autoremove --purge' ausführen?",
            PlainMessageBox.StandardButton.Yes
            | PlainMessageBox.StandardButton.No,
        )
        if ret != PlainMessageBox.StandardButton.Yes:
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
        if not self._prepare_passwords(sel):
            return
        self.clean_run_worker = _CleanRunWorker(sel)
        self.clean_run_worker.progress.connect(self._on_clean_progress)
        self.clean_run_worker.host_started.connect(self._on_clean_host_started)
        self.clean_run_worker.host_done.connect(self._on_clean_host_done)
        self.clean_run_worker.finished_all.connect(self._on_clean_done)
        self.act_stop.setEnabled(True)
        self.statusBar().showMessage("Bereinigung wird gestartet ...")
        self.clean_run_worker.start()

    def _on_clean_host_started(self, payload: dict):
        self.statusBar().showMessage(
            f"Bereinigung läuft: {payload.get('name', '?')} ..."
        )

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
        fatal_error = getattr(self.clean_run_worker, "fatal_error", None)
        stopped = bool(getattr(self.clean_run_worker, "stop_requested", False))
        if fatal_error:
            self.log.append(
                f"\nLokaler Bereinigungslauf wegen internem Fehler beendet: {fatal_error}"
            )
            self.statusBar().showMessage("Lokaler Bereinigungslauf mit Fehler beendet", 5000)
        elif stopped:
            self.log.append("\nLokales Warten beendet; Remote-Zustand gegebenenfalls unbekannt.")
            self.statusBar().showMessage("Lokales Warten beendet – Remote-Zustand prüfen", 5000)
        else:
            self.log.append("\nLokaler Bereinigungslauf beendet; Ergebnisse der einzelnen Hosts beachten.")
            self.statusBar().showMessage("Lokaler Bereinigungslauf beendet – Ergebnisse prüfen", 5000)
        self.act_stop.setEnabled(False)
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
            PlainMessageBox.information(
                self, "Keine Auswahl", "Bitte Hosts anhaken."
            )
            return

        ret = PlainMessageBox.question(
            self,
            "Reboot ausführen",
            f"Sollen {len(selected)} ausgewählte Host(s) neu gestartet werden?\nHinweis: Der SSH-Stream bricht ggf. sofort ab.",
        )
        if ret != PlainMessageBox.StandardButton.Yes:
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

        if not self._prepare_passwords(selected):
            return
        self.reboot_worker = _RebootWorker(selected)
        self.reboot_worker.host_done.connect(self._on_reboot_host_done)
        self.reboot_worker.finished_all.connect(self._on_reboot_done)
        self.act_stop.setEnabled(True)
        self.reboot_worker.start()

    def _on_reboot_host_done(self, res: dict):
        if res.get("status") == "ok":
            self.log.append(f"🔁 {res['name']}: {res.get('note', 'Reboot ausgelöst')}")
        else:
            self.log.append(f"❌ {res.get('name', '?')}: {res.get('note', 'Fehler')}")
        self.log.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def _on_reboot_done(self):
        fatal_error = getattr(self.reboot_worker, "fatal_error", None)
        if fatal_error:
            self.log.append(f"\nReboot wegen internem Fehler beendet: {fatal_error}")
        elif getattr(self.reboot_worker, "stop_requested", False):
            self.log.append("\nLokales Warten beendet; keine weiteren Hosts gestartet.")
        else:
            self.log.append("\nReboot-Befehle abgesetzt.")
        self.act_stop.setEnabled(False)
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
        self._rebuilding_hosts = True
        self.table.remember_projects()

        # Only retain session results for the same stored connection/auth data.
        connections = {
            h["id"]: tuple(h.get(key) for key in (
                "primary_ip", "user", "port", "auth_method", "key_path", "password_enc"
            ))
            for h in hosts
        }
        valid_hosts = {host_id for host_id in connections
                       if self._docker_connections.get(host_id) == connections[host_id]}
        self.table.retain_hosts(valid_hosts)
        self._docker_results = {
            host_id: result for host_id, result in self._docker_results.items()
            if host_id in connections
            and self._docker_connections.get(host_id) == connections[host_id]
        }
        for host_id in connections:
            if host_id not in valid_hosts:
                self._docker_context_versions[host_id] = self._docker_context_versions.get(host_id, 0) + 1
        self._docker_connections = connections

        model = QtGui.QStandardItemModel()
        model.setHorizontalHeaderLabels(
            ["✓", "Name", "IP", "User", "Auth", "Status", "Docker", "Letzte Prüfung"]
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

            docker = _docker_status_item(self._docker_results.get(h["id"]))
            model.appendRow([chk, name, ip, user, auth, status, docker, last_item])

        sort_column = self.table.header().sortIndicatorSection()
        sort_order = self.table.header().sortIndicatorOrder()
        self.table.setModel(model)
        for signal in (model.dataChanged, model.rowsInserted, model.rowsRemoved, model.modelReset):
            signal.connect(self._sync_docker_actions)
        self._sync_docker_actions()
        for row in range(model.rowCount()):
            host_id = model.item(row, 1).data(QtCore.Qt.ItemDataRole.UserRole)
            self.table.populate_projects(host_id, model.item(row, 0), self._docker_results.get(host_id))
        if self.table.isSortingEnabled():
            self.table.sortByColumn(sort_column, sort_order)
        for column in range(model.columnCount()):
            self.table.resizeColumnToContents(column)
        self.table.setColumnWidth(0, 30)
        self._rebuilding_hosts = False
        self._sync_docker_actions()
        self._sync_toggle_action()

    def closeEvent(self, event):
        already_closing = getattr(self, '_closing', False)
        self._closing = True
        self.setEnabled(False)
        active = [getattr(self, name, None) for name in
                  ("worker", "sim_worker", "upg_worker", "clean_sim_worker", "clean_run_worker", "reboot_worker", "preflight_worker")]
        if any(isinstance(worker, QtCore.QThread) and worker.isRunning() for worker in active):
            if not already_closing:
                self._on_stop_requested()
            self.statusBar().showMessage("Bitte warten, bis das lokale Warten beendet ist.")
            event.ignore()
            QtCore.QTimer.singleShot(100, self.close)
            return
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


class _CancellableWorker(QtCore.QThread):
    def __init__(self):
        super().__init__()
        self.stop_requested = False
        self._loop = None
        self._task = None

    def request_stop(self):
        if self.stop_requested:
            return
        self.stop_requested = True
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._cancel_current)
            except RuntimeError:
                pass  # The event loop already finished.

    def _cancel_current(self):
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def _call(self, host, operation):
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(operation(host))
        if self.stop_requested:
            self._task.cancel()
        try:
            return await self._task
        except asyncio.CancelledError:
            return {"host_id": host["id"], "name": host.get("name", "?"),
                    "status": "unknown",
                    "note": "Remote-Zustand unbekannt: Benutzer hat das lokale Warten beendet. "
                            "Der Remote-Prozess kann weiterlaufen; vor erneutem Start am Host prüfen."}
        finally:
            self._task = None
            self._loop = None


class _DockerPreflightWorker(_CancellableWorker):
    """One cancellable job: fresh preflight, targeted pull, stop before apply."""
    progress = QtCore.pyqtSignal(str)

    def __init__(self, plan, hosts):
        super().__init__()
        from .core.docker_pull import PullRun
        self.plan = plan
        self.hosts = deepcopy(hosts)
        self.result = None
        self.pull_run = PullRun(plan, self.hosts, self.progress.emit)

    def run(self):
        async def job():
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.create_task(self.pull_run.run())
            if self.stop_requested:
                self._task.cancel()
            try:
                return await self._task
            except asyncio.CancelledError:
                return self.pull_run.interrupted()
            finally:
                self._task = self._loop = None
        try:
            self.result = asyncio.run(job())
        except Exception:
            self.result = self.pull_run.outcome('failed', 'Pull-Abschluss nicht bestätigt.', 'error')


class _DockerApplyWorker(_CancellableWorker):
    progress = QtCore.pyqtSignal(str)

    def __init__(self, state, hosts):
        super().__init__()
        from .core.docker_apply import ApplyRun
        self.apply_run = ApplyRun(state, deepcopy(hosts), self.progress.emit)
        self.result = None

    def run(self):
        async def job():
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.create_task(self.apply_run.run())
            if self.stop_requested:
                self._task.cancel()
            try:
                return await self._task
            except asyncio.CancelledError:
                return self.apply_run.interrupted()
            finally:
                self._task = self._loop = None
        try:
            self.result = asyncio.run(job())
        except Exception:
            self.result = self.apply_run.outcome('failed', 'Apply-Abschluss nicht bestätigt; Containerzustand unbekannt.', 'error')


class _DockerVerificationWorker(_CancellableWorker):
    progress = QtCore.pyqtSignal(str)

    def __init__(self, applied, hosts, session, previous=None):
        super().__init__()
        from .core.docker_verification import VerificationRun
        self.verification_run = VerificationRun(applied, deepcopy(hosts), session, previous, self.progress.emit)
        self.result = None

    def run(self):
        async def job():
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.create_task(self.verification_run.run())
            if self.stop_requested:
                self._task.cancel()
            try:
                return await self._task
            except asyncio.CancelledError:
                return self.verification_run.interrupted()
            finally:
                self._task = self._loop = None
        try:
            self.result = asyncio.run(job())
        except Exception:
            self.result = self.verification_run.interrupted()


class _CheckWorker(_CancellableWorker):
    one_result = QtCore.pyqtSignal(dict)
    finished_all = QtCore.pyqtSignal()

    def __init__(self, host_ids: list | None = None, *, registry_session=None):
        super().__init__()
        self.registry_session = registry_session
        self.host_ids = host_ids
        self.fatal_error = None

    def run(self):
        try:
            import asyncio
            from .core import db, ssh_client

            all_hosts = db.list_hosts()
            hosts = [h for h in all_hosts if not self.host_ids or h["id"] in self.host_ids]

            async def _job():
                for h in hosts:
                    if self.stop_requested:
                        break
                    if not h.get("primary_ip"):
                        self.one_result.emit(
                            {
                                "host_id": h["id"],
                                "name": h.get("name", "?"),
                                "status": "error",
                                "note": "IP/Host fehlt",
                            }
                        )
                        continue
                    operation = ssh_client.check_updates_for_host
                    if self.registry_session is not None:
                        from functools import partial
                        operation = partial(operation, registry_session=self.registry_session)
                    res = await self._call(h, operation)
                    res.setdefault("host_id", h["id"])
                    self.one_result.emit(res)

            asyncio.run(_job())
        except Exception as ex:
            self.fatal_error = f"{type(ex).__name__}: {ex}"
            self.one_result.emit(
                {"status": "error", "name": "CheckWorker", "note": f"Interner Fehler: {ex}"}
            )
        finally:
            self.finished_all.emit()


class _SimWorker(_CancellableWorker):
    one_result = QtCore.pyqtSignal(dict)
    finished_all = QtCore.pyqtSignal()

    def __init__(self, host_ids: list | None = None):
        super().__init__()
        self.host_ids = host_ids
        self.fatal_error = None

    def run(self):
        try:
            from .core import db, ssh_client
            import asyncio

            all_hosts = db.list_hosts()
            hosts = [h for h in all_hosts if not self.host_ids or h["id"] in self.host_ids]

            async def _job():
                for h in hosts:
                    if self.stop_requested:
                        break
                    if not h.get("primary_ip"):
                        self.one_result.emit(
                            {
                                "host_id": h["id"],
                                "name": h.get("name", "?"),
                                "status": "error",
                                "note": "IP/Host fehlt",
                            }
                        )
                        continue
                    try:
                        res = await self._call(h, ssh_client.simulate_upgrade_for_host)
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

            asyncio.run(_job())
        except Exception as ex:
            self.fatal_error = f"{type(ex).__name__}: {ex}"
            self.one_result.emit(
                {"status": "error", "name": "SimWorker", "note": f"Interner Fehler: {ex}"}
            )
        finally:
            self.finished_all.emit()


class _ActionWorker(_CancellableWorker):
    async def _action(self, host, operation):
        name = host.get("name", "?")
        self._loop = asyncio.get_running_loop()
        pending = []
        size = 0

        def flush():
            nonlocal size
            if pending:
                self.progress.emit({"name": name, "line": "\n".join(pending)})
                pending.clear()
            size = 0

        async def flush_periodically():
            while True:
                await asyncio.sleep(0.1)
                flush()

        async def consume():
            nonlocal size
            from contextlib import aclosing
            async with aclosing(operation(host)) as messages:
                async for msg in messages:
                    if msg.get("type") == "line":
                        line = msg["line"]
                        pending.append(line)
                        size += len(line) + 1
                        if size >= 8192 or len(pending) >= 512:
                            flush()
                    elif msg.get("type") == "result":
                        flush()
                        res = dict(msg["result"], host_id=host["id"], name=name)
                        self.host_done.emit(res)

        flusher = asyncio.create_task(flush_periodically())
        self._task = asyncio.create_task(consume())
        if self.stop_requested:
            self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            flush()
            self.host_done.emit({"host_id": host["id"], "name": name, "status": "unknown",
                                 "note": "Remote-Zustand unbekannt: Benutzer hat das lokale Warten beendet. "
                                         "Der Remote-Prozess kann weiterlaufen; vor erneutem Start am Host prüfen."})
        finally:
            flusher.cancel()
            await asyncio.gather(flusher, return_exceptions=True)
            flush()
            self._task = None
            self._loop = None


class _UpgradeWorker(_ActionWorker):
    progress = QtCore.pyqtSignal(dict)
    host_started = QtCore.pyqtSignal(dict)
    host_done = QtCore.pyqtSignal(dict)
    finished_all = QtCore.pyqtSignal()

    def __init__(self, host_ids: list | None = None):
        super().__init__()
        self.host_ids = host_ids
        self.stop_requested = False
        self.fatal_error = None

    def run(self):
        try:
            import asyncio
            from .core import db, ssh_client

            all_hosts = db.list_hosts()
            hosts = [h for h in all_hosts if not self.host_ids or h["id"] in self.host_ids]

            async def _job():
                for h in hosts:
                    if self.stop_requested:
                        break

                    name = h.get("name", "?")
                    self.host_started.emit({"host_id": h["id"], "name": name})

                    if not h.get("primary_ip"):
                        self.host_done.emit(
                            {
                                "host_id": h["id"],
                                "name": name,
                                "status": "error",
                                "note": "IP/Host fehlt",
                            }
                        )
                        continue
                    try:
                        await self._action(h, ssh_client.upgrade_host_stream)
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
        except Exception as ex:
            self.fatal_error = f"{type(ex).__name__}: {ex}"
            self.host_done.emit(
                {"status": "error", "name": "UpgradeWorker", "note": f"Interner Fehler: {ex}"}
            )
        finally:
            self.finished_all.emit()


class _CleanSimWorker(_CancellableWorker):
    one_result = QtCore.pyqtSignal(dict)
    finished_all = QtCore.pyqtSignal()

    def __init__(self, host_ids: list[int]):
        super().__init__()
        self.host_ids = host_ids
        self.fatal_error = None

    def run(self):
        try:
            import asyncio
            from .core import db, ssh_client

            all_hosts = db.list_hosts()
            hosts = [h for h in all_hosts if h["id"] in self.host_ids]

            async def _job():
                for h in hosts:
                    if self.stop_requested:
                        break
                    res = await self._call(h, ssh_client.simulate_autoremove_for_host)
                    self.one_result.emit(res)

            asyncio.run(_job())
        except Exception as ex:
            self.fatal_error = f"{type(ex).__name__}: {ex}"
            self.one_result.emit(
                {"status": "error", "name": "CleanSimWorker", "note": f"Interner Fehler: {ex}"}
            )
        finally:
            self.finished_all.emit()


class _CleanRunWorker(_ActionWorker):
    progress = QtCore.pyqtSignal(dict)
    host_started = QtCore.pyqtSignal(dict)
    host_done = QtCore.pyqtSignal(dict)
    finished_all = QtCore.pyqtSignal()

    def __init__(self, host_ids: list[int]):
        super().__init__()
        self.host_ids = host_ids
        self.stop_requested = False
        self.fatal_error = None

    def run(self):
        try:
            import asyncio
            from .core import db, ssh_client

            all_hosts = db.list_hosts()
            hosts = [h for h in all_hosts if h["id"] in self.host_ids]

            async def _job():
                for h in hosts:
                    if self.stop_requested:
                        break

                    name = h.get("name", "?")
                    self.host_started.emit({"host_id": h["id"], "name": name})

                    try:
                        await self._action(h, ssh_client.autoremove_host_stream)
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
        except Exception as ex:
            self.fatal_error = f"{type(ex).__name__}: {ex}"
            self.host_done.emit(
                {"status": "error", "name": "CleanRunWorker", "note": f"Interner Fehler: {ex}"}
            )
        finally:
            self.finished_all.emit()


class _RebootWorker(_CancellableWorker):
    host_done = QtCore.pyqtSignal(dict)
    finished_all = QtCore.pyqtSignal()

    def __init__(self, host_ids: list[int] | None = None):
        super().__init__()
        self.host_ids = host_ids or []
        self.fatal_error = None

    def run(self):
        try:
            from .core import db, ssh_client
            import asyncio

            all_hosts = db.list_hosts()
            hosts = [h for h in all_hosts if not self.host_ids or h["id"] in self.host_ids]

            async def _job():
                for h in hosts:
                    if self.stop_requested:
                        break
                    try:
                        res = await self._call(h, ssh_client.reboot_host)
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

            asyncio.run(_job())
        except Exception as ex:
            self.fatal_error = f"{type(ex).__name__}: {ex}"
            self.host_done.emit(
                {"status": "error", "name": "RebootWorker", "note": f"Interner Fehler: {ex}"}
            )
        finally:
            self.finished_all.emit()
