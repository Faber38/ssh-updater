#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

echo "──────────────────────────────────────────────"
echo " SSH-Updater – Buildskript (PyInstaller)"
echo "──────────────────────────────────────────────"

# Virtuelle Umgebung aktivieren
if [ ! -d ".venv" ]; then
    echo "Erstelle virtuelle Umgebung ..."
    python3 -m venv .venv
fi
source .venv/bin/activate

# Abhängigkeiten prüfen
echo "Installiere/aktualisiere Abhängigkeiten ..."
pip install --upgrade pip wheel pyinstaller cryptography

# Vorherige Builds löschen
echo "Bereinige alte Build-Dateien ..."
rm -rf src/build src/dist ssh-updater.spec

# Build starten
echo "Erstelle One-File-Binary ..."
pyinstaller --onefile --noconfirm \
  --name ssh-updater \
  --paths src \
  --add-data "src/sshupdater/assets/qss:assets/qss" \
  src/sshupdater/app.py

# Fertiges Binary anzeigen
echo
echo "Build abgeschlossen ✅"
echo "Datei: $(realpath src/dist/ssh-updater)"
echo "──────────────────────────────────────────────"
