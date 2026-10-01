import sys
from PyQt6 import QtWidgets
from PyQt6.QtWidgets import QInputDialog, QLineEdit

from sshupdater.ui_main import MainWindow
from sshupdater.ui_text import PlainMessageBox as QMessageBox
from sshupdater.core import db, crypto, host_keys, settings
from sshupdater.ui_resources import resource_path
from sshupdater.ui_theme import apply_theme


def main():
    if '--smoke-test' in sys.argv:
        from sshupdater.smoke import run
        return run(resource_path)
    app = QtWidgets.QApplication(sys.argv)
    try:
        settings.initialize()
    except (OSError, ValueError) as e:
        QMessageBox.critical(None, "Datenspeicher nicht sicher zugänglich", str(e))
        return 1

    apply_theme(app)

    # --- Masterpasswort-Handling ------------------------------------------------

    # Prüfen, ob Keystore bereits existiert (Erstlauf?)
    try:
        first_run = not crypto.keystore_exists()
    except OSError as e:
        QMessageBox.critical(None, 'Vault nicht verfügbar', str(e))
        return 1

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
        try:
            crypto.require_strong_password(pw)
        except crypto.WeakPassword as e:
            QMessageBox.critical(None, "Master-Passwort zu schwach", str(e))
            return 1
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
    except crypto.WeakPassword as e:
        QMessageBox.critical(None, "Master-Passwort zu schwach", str(e))
        return 1
    except crypto.WrongPassword as e:
        QMessageBox.critical(None, "Fehler", str(e))
        return 1
    except Exception as e:
        QMessageBox.critical(None, "Fehler", f"Schlüssel-Init fehlgeschlagen:\n{e}")
        return 1

    try:
        if first_run:
            host_keys.initialize_fresh()
        else:
            host_keys.load()
    except (host_keys.TruststoreInitializationRequired,
            host_keys.TruststoreMigrationRequired) as e:
        answer = QMessageBox.warning(
            None, "Serveridentitäten erneut bestätigen",
            f"{e}\n\nDer bisherige, nicht authentisierte Truststore wird nicht übernommen. "
            "Wenn Sie fortfahren, wird er nur als inaktive Sicherung abgelegt und alle "
            "Server-Fingerprints müssen vor der nächsten Aktion erneut über eine "
            "unabhängige Quelle bestätigt werden.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Yes:
            return 1
        try:
            host_keys.reset_for_reconfirmation()
        except OSError as reset_error:
            QMessageBox.critical(None, "Truststore nicht wiederherstellbar", str(reset_error))
            return 1
    except host_keys.TruststoreIntegrityError as e:
        QMessageBox.critical(None, "Truststore-Integrität verletzt", str(e))
        return 1
    except OSError as e:
        QMessageBox.critical(None, "Truststore nicht lesbar", str(e))
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
