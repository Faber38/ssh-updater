from PyQt6 import QtWidgets, QtGui, QtCore
from pathlib import Path
from datetime import datetime


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SSH Updater")
        self.resize(1100, 680)

        # Toolbar
        tb = QtWidgets.QToolBar("Main")
        tb.setIconSize(QtCore.QSize(18, 18))
        self.addToolBar(tb)

        self.act_check  = QtGui.QAction("Prüfen", self)
        self.act_sim    = QtGui.QAction("Simulieren", self)
        self.act_upg    = QtGui.QAction("Upgrade", self)
        self.act_clean  = QtGui.QAction("Bereinigen", self)
        self.act_reboot = QtGui.QAction("Reboot", self)
        self.act_config = QtGui.QAction("Konfiguration", self)
        self.act_import = QtGui.QAction("Import von Proxmox", self)
        for a in (self.act_check, self.act_sim, self.act_upg, self.act_clean, self.act_reboot, self.act_config, self.act_import):
            tb.addAction(a)

        # Klick-Handler
        self.act_config.triggered.connect(self._open_config)
        self.act_check.triggered.connect(self._on_check)
        self.act_sim.triggered.connect(self._on_sim)
        self.act_upg.triggered.connect(self._on_upgrade)
        self.act_clean.triggered.connect(self._on_clean)
        self.act_reboot.triggered.connect(self._on_reboot)


        # Zentraler Bereich
        central = QtWidgets.QWidget()
        main = QtWidgets.QHBoxLayout(central)

        left = QtWidgets.QFrame()
        left.setFixedWidth(240)
        left.setLayout(QtWidgets.QVBoxLayout())
        left.layout().addWidget(QtWidgets.QLabel("Filter (Stub)"))
        left.layout().addStretch(1)

        self.table = QtWidgets.QTableView()
        self._reload_hosts()
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)

        # Log-Feld
        self.log = QtWidgets.QTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("Logs …")

        right = QtWidgets.QWidget()
        rlay = QtWidgets.QVBoxLayout(right)
        rlay.addWidget(self.table, 3)
        rlay.addWidget(self.log, 2)

        main.addWidget(left)
        main.addWidget(right)
        self.setCentralWidget(central)

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
            if chk_item is not None and chk_item.checkState() == QtCore.Qt.CheckState.Checked:
                # Host-ID ist im Name-Item (Spalte 1) gespeichert
                name_item = model.item(r, 1)
                hid = name_item.data(QtCore.Qt.ItemDataRole.UserRole)
                if hid is not None:
                    ids.append(int(hid))
        return ids
 

    def _apply_theme(self):
        # Falls du Standard-Theme willst, diesen Block leeren/entfernen
        qss = Path(__file__).resolve().parents[1] / "assets" / "qss" / "dark.qss"
        if qss.exists():
            self.setStyleSheet(qss.read_text(encoding="utf-8"))

    def _open_config(self):
        from .ui_config import ConfigDialog
        dlg = ConfigDialog(self)
        dlg.exec()
        self._reload_hosts()

    def _find_row_by_host_id(self, host_id: int) -> int:
        model = self.table.model()
        for r in range(model.rowCount()):
            name_item = model.item(r, 1)  # <- Spalte 1 statt 0
            if name_item and name_item.data(QtCore.Qt.ItemDataRole.UserRole) == host_id:
                return r
        return -1


    # ========= Prüfen: Worker-Thread + Slots =========
    def _on_check(self):
        for a in (self.act_check, self.act_sim, self.act_upg, self.act_clean, self.act_reboot, self.act_config, self.act_import):
            a.setEnabled(False)

        selected = self._get_selected_host_ids()
        if not selected:
            QtWidgets.QMessageBox.information(self, "Keine Auswahl", "Bitte zuerst Hosts auswählen (Haken setzen).")
            for a in (self.act_check, self.act_sim, self.act_upg, self.act_clean, self.act_reboot, self.act_config, self.act_import):
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
            self.log.append(f"✔ {res['name']} [{res.get('distro','?')}]: {res.get('updates',0)} Updates")
            online = True
            updates = int(res.get("updates", 0))
        else:
            self.log.append(f"✖ {res.get('name','?')}: {res.get('note','Fehler')}")
            online = False
            updates = None
        self.log.moveCursor(QtGui.QTextCursor.MoveOperation.End)

        # UI-Zeile aktualisieren
        row = self._find_row_by_host_id(res.get("host_id"))
        if row >= 0:
            model = self.table.model()
            status_text = f"Online – {updates} Updates" if online else "Offline"
            model.setItem(row, 5, QtGui.QStandardItem(status_text))  # Spalte "Status"
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            model.setItem(row, 6, QtGui.QStandardItem(timestamp))    # Spalte "Letzte Prüfung"
            
            # in DB persistieren
            try:
                from .core import db
                db.set_check_result(res["host_id"], timestamp, updates)
            except Exception as e:
                self.statusBar().showMessage(f"Speicherfehler: {e}", 5000)

    def _on_check_done(self):
        self.log.append("\nFertig.")
        for a in (self.act_check, self.act_sim, self.act_upg, self.act_clean, self.act_reboot, self.act_config, self.act_import):
            a.setEnabled(True)

    # ========= Simulieren =========
    def _on_sim(self):
        for a in (self.act_check, self.act_sim, self.act_upg, self.act_clean, self.act_reboot, self.act_config, self.act_import):
            a.setEnabled(False)

        selected = self._get_selected_host_ids()
        if not selected:
            QtWidgets.QMessageBox.information(self, "Keine Auswahl", "Bitte zuerst Hosts auswählen (Haken setzen).")
            for a in (self.act_check, self.act_sim, self.act_upg, self.act_clean, self.act_reboot, self.act_config, self.act_import):
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
            self.log.append(f"🧪 {res['name']} [{res.get('distro','?')}]: {n} Pakete geplant")
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
            self.log.append(f"✖ {res.get('name','?')}: {res.get('note','Fehler')}")
        self.log.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def _on_sim_done(self):
        self.log.append("\nFertig.")
        for a in (self.act_check, self.act_sim, self.act_upg, self.act_clean, self.act_reboot, self.act_config, self.act_import):
            a.setEnabled(True)

    # ========= Upgraden =========
    def _on_upgrade(self):
        # Sicherheitsabfrage
        ret = QtWidgets.QMessageBox.question(
            self, "Upgrade starten",
            "Alle gelisteten Hosts jetzt upgraden?\n\n"
            "Hinweis: Es werden Paket-Upgrades per sudo -n ausgeführt.\n"
            "Stelle sicher, dass NOPASSWD für die Paketbefehle konfiguriert ist."
        )
        if ret != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        # Buttons sperren
        for a in (self.act_check, self.act_sim, self.act_upg, self.act_clean, self.act_reboot, self.act_config, self.act_import):
            a.setEnabled(False)

        selected = self._get_selected_host_ids()
        if not selected:
            QtWidgets.QMessageBox.information(self, "Keine Auswahl", "Bitte zuerst Hosts auswählen (Haken setzen).")
            for a in (self.act_check, self.act_sim, self.act_upg, self.act_clean, self.act_reboot, self.act_config, self.act_import):
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
            self.log.append(f"✅ {res['name']}: Upgrade abgeschlossen ({res.get('distro','?')}).")
            row = self._find_row_by_host_id(res.get("host_id"))
            if row >= 0:
                model = self.table.model()
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                model.setItem(row, 5, QtGui.QStandardItem("Online – 0 Updates"))  # Spalte Status
                model.setItem(row, 6, QtGui.QStandardItem(ts))                    # Spalte Letzte Prüfung
                try:
                    from .core import db
                    db.set_check_result(res["host_id"], ts, 0)
                except Exception:
                    pass
        else:
            self.log.append(f"❌ {res.get('name','?')}: {res.get('note','Fehler')}")
        self.log.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def _on_upgrade_done(self):
        self.log.append("\nAlle Upgrades beendet.")
        for a in (self.act_check, self.act_sim, self.act_upg, self.act_clean, self.act_reboot, self.act_config, self.act_import):
            a.setEnabled(True)

    # ========= Bereinigen ==========

    def _on_clean(self):
        # Auswahl prüfen
        selected = self._get_selected_host_ids()
        if not selected:
            QtWidgets.QMessageBox.information(self, "Keine Auswahl", "Bitte Hosts anhaken.")
            return
        self._clean_selected = selected 

        # Buttons sperren
        for a in (self.act_check, self.act_sim, self.act_upg, self.act_clean, self.act_reboot, self.act_config, self.act_import, self.act_clean):
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
                    self.log.append(preview + ("\n" if len(lines) <= 20 else f"\n... ({len(lines)-20} weitere Zeilen)\n"))
        else:
            self.log.append(f"✖ {res.get('name','?')}: {res.get('note','Fehler')}")
        self.log.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def _on_clean_sim_done(self):
        # Nachfrage nur, wenn irgendwo >0 Pakete
        text = self.log.toPlainText()
        any_removals = "würden entfernt" in text and "0 Pakete würden entfernt" not in text
        sel = getattr(self, "_clean_selected", [])
        if not sel:
            self.log.append("\nAbgebrochen (keine Auswahl).")
            for a in (self.act_check, self.act_sim, self.act_upg, self.act_clean, self.act_reboot, self.act_config, self.act_import, self.act_clean):
                a.setEnabled(True)
            return

        ret = QtWidgets.QMessageBox.question(
            self, "Autoremove ausführen",
            "Simulation abgeschlossen.\nJetzt auf den ausgewählten Hosts 'apt autoremove --purge' ausführen?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
        )
        if ret != QtWidgets.QMessageBox.StandardButton.Yes:
            self.log.append("\nAbgebrochen.")
            for a in (self.act_check, self.act_sim, self.act_upg, self.act_clean, self.act_reboot, self.act_config, self.act_import, self.act_clean):
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
            self.log.append(f"❌ {res.get('name','?')}: {res.get('note','Fehler')}")
        self.log.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def _on_clean_done(self):
        self.log.append("\nBereinigung beendet.")
        for a in (self.act_check, self.act_sim, self.act_upg, self.act_clean, self.act_reboot, self.act_config, self.act_import, self.act_clean):
            a.setEnabled(True)
        self._clean_selected = []


    # ========= reboot =========
    def _on_reboot(self):
        # Auswahl prüfen
        selected = self._get_selected_host_ids()
        if not selected:
            QtWidgets.QMessageBox.information(self, "Keine Auswahl", "Bitte Hosts anhaken.")
            return

        ret = QtWidgets.QMessageBox.question(
            self, "Reboot ausführen",
            f"Sollen {len(selected)} ausgewählte Host(s) neu gestartet werden?\n"
            "Hinweis: Der SSH-Stream bricht ggf. sofort ab."
        )
        if ret != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        # Buttons sperren
        for a in (self.act_check, self.act_sim, self.act_upg, self.act_clean, self.act_reboot, self.act_config, self.act_import):
            a.setEnabled(False)

        self.log.clear()
        self.log.append("Starte Reboot...\n")

        self.reboot_worker = _RebootWorker(selected)
        self.reboot_worker.host_done.connect(self._on_reboot_host_done)
        self.reboot_worker.finished_all.connect(self._on_reboot_done)
        self.reboot_worker.start()

    def _on_reboot_host_done(self, res: dict):
        if res.get("status") == "ok":
            self.log.append(f"🔁 {res['name']}: {res.get('note','Reboot ausgelöst')}")
        else:
            self.log.append(f"❌ {res.get('name','?')}: {res.get('note','Fehler')}")
        self.log.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def _on_reboot_done(self):
        self.log.append("\nReboot-Befehle abgesetzt.")
        for a in (self.act_check, self.act_sim, self.act_upg, self.act_clean, self.act_reboot, self.act_config, self.act_import):
            a.setEnabled(True)
   

    # ========= Hosts laden =========
    def _reload_hosts(self):
        from .core import db
        hosts = db.list_hosts()

        model = QtGui.QStandardItemModel()
        # erste Spalte: Auswahl (Checkbox)
        model.setHorizontalHeaderLabels(["✓", "Name", "IP", "User", "Auth", "Status", "Letzte Prüfung"])

        for h in hosts:
            chk = QtGui.QStandardItem()                # Checkbox item
            chk.setCheckable(True)
            chk.setCheckState(QtCore.Qt.CheckState.Unchecked)
            chk.setEditable(False)

            name = QtGui.QStandardItem(h.get("name") or "")
            ip = QtGui.QStandardItem(h.get("primary_ip") or "")
            user = QtGui.QStandardItem(h.get("user") or "")
            auth = QtGui.QStandardItem(h.get("auth_method") or "")
            status = QtGui.QStandardItem(
                f"Online – {h['pending_updates']} Updates" if h.get("pending_updates") is not None else "—"
            )
            last = QtGui.QStandardItem(h.get("last_check") or "—")

            # host_id im ersten echten Datenfeld (z.B. Name) speichern
            name.setData(h["id"], QtCore.Qt.ItemDataRole.UserRole)

            # set flags (optional: nicht editierbar außer checkbox)
            for it in (name, ip, user, auth, status, last):
                it.setEditable(False)

            model.appendRow([chk, name, ip, user, auth, status, last])

        self.table.setModel(model)
        self.table.resizeColumnsToContents()
        # Optional: die Checkbox-Spalte etwas schmaler machen
        self.table.setColumnWidth(0, 30)



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
                    self.one_result.emit({
                        "host_id": h["id"],                # <<<<
                        "name": h.get("name","?"),
                        "status": "error",
                        "note": "IP/User fehlt"
                    })
                    continue
                res = await ssh_client.check_updates_for_host(h)
                res.setdefault("host_id", h["id"])         # <<<< host_id sicherstellen
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
        import asyncio, traceback

        all_hosts = db.list_hosts()
        hosts = [h for h in all_hosts if not self.host_ids or h["id"] in self.host_ids]

        async def _job():
            for h in hosts:
                if not h.get("primary_ip") or not h.get("user"):
                    self.one_result.emit({
                        "host_id": h["id"],
                        "name": h.get("name", "?"),
                        "status": "error",
                        "note": "IP/User fehlt"
                    })
                    continue
                try:
                    # >>> richtige Funktion für die Simulation! <<<
                    res = await ssh_client.simulate_upgrade_for_host(h)
                    res.setdefault("host_id", h["id"])
                    self.one_result.emit(res)
                except Exception as ex:
                    self.one_result.emit({
                        "host_id": h["id"],
                        "name": h.get("name", "?"),
                        "status": "error",
                        "note": f"Sim-Fehler: {ex}"
                    })

        # Eigener Event-Loop pro QThread (robust für Python 3.13)
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_job())
        except Exception:
            self.one_result.emit({
                "status": "error",
                "name": "SimWorker",
                "note": "Uncaught: " + traceback.format_exc(limit=1)
            })
        finally:
            loop.close()

        self.finished_all.emit()

        

class _UpgradeWorker(QtCore.QThread):
    progress = QtCore.pyqtSignal(dict)   # {"name","line"}
    host_done = QtCore.pyqtSignal(dict)  # {"host_id","name","status","note","distro"?}
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
                    self.host_done.emit({
                        "host_id": h["id"],
                        "name": name,
                        "status": "error",
                        "note": "IP/User fehlt"
                    })
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
                    self.host_done.emit({
                        "host_id": h["id"],
                        "name": name,
                        "status": "error",
                        "note": str(ex)
                    })

        asyncio.run(_job())
        self.finished_all.emit()


class _CleanSimWorker(QtCore.QThread):
    one_result = QtCore.pyqtSignal(dict)
    finished_all = QtCore.pyqtSignal()
    def __init__(self, host_ids: list[int]): super().__init__(); self.host_ids = host_ids
    def run(self):
        import asyncio
        from .core import db, ssh_client
        all_hosts = db.list_hosts()
        hosts = [h for h in all_hosts if h["id"] in self.host_ids]
        async def _job():
            for h in hosts:
                res = await ssh_client.simulate_autoremove_for_host(h)
                self.one_result.emit(res)
        asyncio.run(_job()); self.finished_all.emit()

class _CleanRunWorker(QtCore.QThread):
    progress = QtCore.pyqtSignal(dict)   # {"name","line"}
    host_done = QtCore.pyqtSignal(dict)  # {"host_id","name","status","note","distro"?}
    finished_all = QtCore.pyqtSignal()
    def __init__(self, host_ids: list[int]): super().__init__(); self.host_ids = host_ids
    def run(self):
        import asyncio
        from .core import db, ssh_client
        all_hosts = db.list_hosts()
        hosts = [h for h in all_hosts if h["id"] in self.host_ids]
        async def _job():
            for h in hosts:
                name = h.get("name","?")
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
                    self.host_done.emit({"host_id": h["id"], "name": name, "status": "error", "note": str(ex)})
        asyncio.run(_job()); self.finished_all.emit()

class _RebootWorker(QtCore.QThread):
    host_done = QtCore.pyqtSignal(dict)
    finished_all = QtCore.pyqtSignal()

    def __init__(self, host_ids: list[int] | None = None):
        super().__init__()
        self.host_ids = host_ids or []

    def run(self):
        from .core import db, ssh_client
        import asyncio, traceback

        all_hosts = db.list_hosts()
        hosts = [h for h in all_hosts if not self.host_ids or h["id"] in self.host_ids]

        async def _job():
            for h in hosts:
                try:
                    res = await ssh_client.reboot_host(h)
                    res.setdefault("host_id", h["id"])
                    self.host_done.emit(res)
                except Exception as ex:
                    self.host_done.emit({
                        "host_id": h["id"],
                        "name": h.get("name","?"),
                        "status": "error",
                        "note": f"Reboot-Fehler: {ex}"
                    })

        # Eigener Event-Loop pro Thread (stabil in Py 3.13)
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_job())
        finally:
            loop.close()

        self.finished_all.emit()
