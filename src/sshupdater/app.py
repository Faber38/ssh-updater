import sys
from pathlib import Path
from PyQt6 import QtWidgets
from PyQt6.QtWidgets import QInputDialog, QMessageBox, QLineEdit

from .ui_main import MainWindow
from .core import db, crypto


def main():
    app = QtWidgets.QApplication(sys.argv)

    # (Optional) Theme global laden
    qss = Path(__file__).resolve().parent / "assets" / "qss" / "dark.qss"
    if qss.exists():
        app.setStyleSheet(qss.read_text(encoding="utf-8"))

    # Masterpasswort abfragen
    pw, ok = QInputDialog.getText(
        None,
        "Vault entsperren",
        "Master-Passwort:",
        QLineEdit.EchoMode.Password
    )

    if not ok or not pw:
        QMessageBox.warning(None, "Abbruch", "Ohne Master-Passwort geht's nicht.")
        return 0

    try:
        crypto.set_master_password(pw)
    except Exception as e:
        QMessageBox.critical(None, "Fehler", f"Schlüssel-Ableitung fehlgeschlagen:\n{e}")
        return 1

    # DB initialisieren
    try:
        db.init_db()
    except Exception as e:
        QMessageBox.critical(None, "DB-Fehler", str(e))
        return 1

    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
