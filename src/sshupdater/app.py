import sys
from pathlib import Path
from PyQt6 import QtWidgets
from PyQt6.QtWidgets import QInputDialog, QMessageBox, QLineEdit

from sshupdater.ui_main import MainWindow
from sshupdater.core import db, crypto, settings


# NEU: robustes Ressourcen-Root (funktioniert auch im PyInstaller-Onefile)
def resource_path(*parts):
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base.joinpath(*parts)


def main():
    app = QtWidgets.QApplication(sys.argv)

    # Theme laden nach Einstellung
    qss = None
    if settings.THEME == "dark":
        qss = resource_path("assets", "qss", "dark.qss")
    elif settings.THEME == "light":
        qss = resource_path("assets", "qss", "light.qss")
    elif settings.THEME == "colour":
        qss = resource_path("assets", "qss", "colour.qss")

    if qss and qss.exists():
        app.setStyleSheet(qss.read_text(encoding="utf-8"))

    # --- Masterpasswort-Handling ------------------------------------------------

    # Prüfen, ob Keystore bereits existiert (Erstlauf?)
    first_run = not crypto.keystore_exists()

    # Passwort abfragen (Erstlauf = mit Bestätigung)
    pw, ok = QInputDialog.getText(
        None,
        "Vault entsperren" if not first_run else "Master-Passwort setzen",
        "Master-Passwort:" if not first_run else "Neues Master-Passwort:",
        QLineEdit.EchoMode.Password
    )
    if not ok or not pw:
        QMessageBox.warning(None, "Abbruch", "Ohne Master-Passwort geht's nicht.")
        return 0

    if first_run:
        pw2, ok2 = QInputDialog.getText(
            None, "Bestätigung", "Master-Passwort wiederholen:",
            QLineEdit.EchoMode.Password
        )
        if not ok2 or pw != pw2:
            QMessageBox.critical(None, "Fehler", "Passwörter stimmen nicht überein.")
            return 1

    # Master-Passwort anwenden / prüfen
    try:
        crypto.set_master_password(pw)   # prüft bei Folgestart, legt bei Erstlauf an
    except crypto.WrongPassword as e:
        QMessageBox.critical(None, "Fehler", str(e))
        return 1
    except Exception as e:
        QMessageBox.critical(None, "Fehler", f"Schlüssel-Init fehlgeschlagen:\n{e}")
        return 1

    # ---------------------------------------------------------------------------

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
