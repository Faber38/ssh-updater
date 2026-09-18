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

# Dieselbe Python-Major/Minor-Version wie im Release-Workflow verwenden.
python --version
python -c 'import pathlib, sys; expected = pathlib.Path("release-python.txt").read_text().strip(); actual = ".".join(map(str, sys.version_info[:2])); sys.exit(0 if actual == expected else "Bitte .venv mit Python " + expected + ".x neu erstellen.")'
python -m pip install -r requirements-build.txt
python -m pip check
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v

# Build starten
echo "Erstelle One-File-Binary ..."
python -m PyInstaller ssh-updater.spec --noconfirm

# Fertiges Binary anzeigen
echo
echo "Build abgeschlossen ✅"
echo "Datei: $(realpath dist/ssh-updater)"
echo "──────────────────────────────────────────────"
