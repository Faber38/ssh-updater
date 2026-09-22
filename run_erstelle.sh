#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "──────────────────────────────────────────────"
echo " SSH-Updater – Buildskript (PyInstaller)"
echo "──────────────────────────────────────────────"

# Isolierte Release-Umgebung; die Entwicklungs-.venv bleibt unverändert.
RELEASE_VENV="${SSH_UPDATER_RELEASE_VENV:-.venv-release}"
if [ ! -d "$RELEASE_VENV" ]; then
    "${SSH_UPDATER_PYTHON:-python3.11}" -m venv "$RELEASE_VENV"
fi
source "$RELEASE_VENV/bin/activate"
python -c 'import pathlib, sys; expected = pathlib.Path("release-python.txt").read_text().strip(); actual = ".".join(map(str, sys.version_info[:2])); sys.exit(0 if actual == expected else "Bitte Release-Umgebung mit Python " + expected + ".x neu erstellen.")'
python -m pip install -r requirements-build.txt
python -m pip check
python scripts/release.py environment
python -B scripts/test_release.py

# Build starten
echo "Erstelle One-File-Binary ..."
python -m PyInstaller ssh-updater.spec --noconfirm --clean
QT_QPA_PLATFORM=offscreen ./dist/ssh-updater --smoke-test

# Fertiges Binary anzeigen
echo
echo "Build abgeschlossen ✅"
echo "Datei: $(realpath dist/ssh-updater)"
echo "──────────────────────────────────────────────"
